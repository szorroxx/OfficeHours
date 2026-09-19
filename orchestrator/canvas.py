"""
The crawler. Reads Canvas pages, pulls out assignments, writes them to the DB.

THREE STEPS, and step 2 is the one that saves you money:

  1. READ      load the HTML (from a local file, or over HTTP later)
  2. STRIP     throw away tags, scripts, styles, nav menus -> plain text
               Canvas HTML is ~95% markup noise. A page that is 40,000
               characters of HTML becomes ~1,500 characters of text. You pay
               for tokens, so this is a ~25x cost reduction, and the model
               does a better job on clean text than on a wall of <div>s.
  3. EXTRACT   Claude reads the text and returns structured JSON

Local files are the default on purpose: your demo then doesn't depend on wifi,
a login, or a site being up, and it gives the same answer every time. It is
still genuinely parsing Canvas HTML -- just from disk.

Flip CANVAS_SOURCE=http later and only step 1 changes.
"""

from __future__ import annotations

import config  # noqa: F401  - loads .env before anything reads it
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import cache
import db

CANVAS_SOURCE = os.getenv("CANVAS_SOURCE", "local")  # 'local' | 'http'
CANVAS_DIR = Path(os.getenv("CANVAS_DIR", "canvas_pages"))
CANVAS_BASE_URL = os.getenv("CANVAS_BASE_URL", "")

# Tags whose entire contents are useless to us.
_DROP_TAGS = ["script", "style", "noscript", "svg", "head", "nav", "footer", "iframe"]

# Text that shows up on every Canvas page and tells the model nothing.
_BOILERPLATE = re.compile(
    r"^(home|announcements|assignments|grades|people|pages|files|syllabus|quizzes|"
    r"modules|collaborations|dashboard|courses|calendar|inbox|history|help|"
    r"skip to content|instructure|privacy policy|acceptable use|facebook|twitter)$",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Step 1: read
# --------------------------------------------------------------------------


def list_sources() -> list[str]:
    """Which pages can we crawl right now? .html pages and .json exports."""
    if CANVAS_SOURCE == "local":
        if not CANVAS_DIR.exists():
            return []
        return sorted(
            p.name for p in CANVAS_DIR.iterdir()
            if p.suffix.lower() in (".html", ".htm", ".json", ".txt")
        )
    return [u.strip() for u in os.getenv("CANVAS_URLS", "").split(",") if u.strip()]


# --------------------------------------------------------------------------
# JSON exports from canvas_export.js
# --------------------------------------------------------------------------

# Checked IN ORDER, first match wins, so the specific phrases must come first.
# "Final Project Proposal" has to hit 'project' before it hits 'final',
# otherwise it gets filed as an exam.
_KIND_HINTS = (
    # two-word phrases first — they disambiguate the generic words below
    ("final exam", "exam"), ("final project", "project"),
    ("midterm exam", "exam"), ("problem set", "homework"),
    ("lab report", "lab"), ("lab writeup", "lab"), ("reading response", "reading"),
    # then specific types
    ("quiz", "quiz"), ("lab", "lab"),
    ("project", "project"), ("proposal", "project"),
    ("paper", "project"), ("essay", "project"), ("portfolio", "project"),
    ("presentation", "project"), ("recital", "project"),
    ("performance", "project"), ("concert", "project"), ("composition", "project"),
    ("discussion", "reading"), ("reading", "reading"), ("response", "reading"),
    ("journal", "reading"), ("annotation", "reading"),
    # generic exam words last
    ("midterm", "exam"), ("exam", "exam"), ("final", "exam"), ("test", "exam"),
    # catch-alls
    ("homework", "homework"), ("pset", "homework"), ("hw", "homework"),
    ("assignment", "homework"), ("attendance", "homework"),
)

# Rough effort estimates by type, used when nothing better is available.
_DEFAULT_HOURS = {"quiz": 1.0, "exam": 2.5, "lab": 2.5, "project": 8.0,
                  "reading": 1.0, "homework": 3.0, "other": 2.0}


def load_export(path: Path, term: str | None = None,
                include_untermed: bool = False) -> dict:
    """
    Read a canvas_export.json produced by canvas_export.js.

    This is the good path. The data came from Canvas's own API, so it's already
    structured -- no HTML to strip and no model call to pay for.

    TERM FILTERING, which matters more than it sounds

    Canvas's enrollment_state=active includes courses from semesters you
    finished. A real export looked like this:

        2261   87 assignments   (last spring)
        2264   90 assignments   (last summer)
        2271   13 assignments   (now)
        no term code:
          159 assignments       Open Lab @ Canvas  (a sandbox from 2020)
            7 assignments       Academic Integrity Modules
            2 assignments       SCI Academic Advising Hub

    358 rows, 13 of them real. Without filtering, "what's due this week"
    competes with two finished semesters and a training sandbox, and the
    sandbox alone outnumbers the actual coursework twelve to one.

    Pitt prefixes course codes with a four-digit term ('2271 CS 1684 SEC1010'),
    and the numbers increase over time, so the largest one present is the
    current semester. Courses with no term code aren't enrolled semester
    courses at all -- they're org sites and orientation modules -- so they're
    dropped unless you ask for them.

    Override with term='2271', or CURRENT_TERM in .env.
    """
    data = json.loads(path.read_text(encoding="utf-8"))

    if "courses" not in data:
        raise ValueError(
            f"{path.name} doesn't look like a Canvas export (no 'courses' key). "
            f"Generate one by pasting canvas_export.js into your browser console."
        )

    all_courses = data["courses"]
    wanted = term or os.getenv("CURRENT_TERM") or detect_current_term(all_courses)

    courses, assignments = [], []
    skipped: dict[str, int] = {}

    for course in all_courses:
        # `name` carries the descriptive title ('... INTRO TO MACHINE
        # LEARNING'); `course_code` is just '2271 CS 1675 SEC1100'. Prefer the
        # longer one so we get a real title.
        label = course.get("name") or course.get("course_code") or ""
        course_term = _term_of(label) or _term_of(course.get("course_code") or "")

        if course_term is None:
            if not include_untermed:
                skipped[f"no term code: {label[:40]}"] = len(
                    course.get("assignments") or []
                )
                continue
        elif wanted and course_term != wanted:
            skipped[f"term {course_term}: {label[:40]}"] = len(
                course.get("assignments") or []
            )
            continue

        code, title = _clean_course_name(label)
        if not code:
            continue
        courses.append({
            "code": code,
            "title": title,
            "canvas_id": str(course.get("id") or ""),
            "term": course_term,
        })

        for a in course.get("assignments") or []:
            name = (a.get("name") or "").strip()
            if not name:
                continue

            state = a.get("workflow_state") or "unsubmitted"
            if state == "graded" or a.get("score") is not None:
                status = "graded"
            elif a.get("submitted") or state == "submitted":
                status = "submitted"
            else:
                status = "open"

            kind = _guess_kind(name, a.get("submission_types") or [])
            assignments.append({
                "course": code,
                "title": name,
                "kind": kind,
                "due_at": a.get("due_at"),          # already ISO 8601 with zone
                "points": a.get("points_possible"),
                "est_hours": _estimate_hours(kind, a.get("points_possible")),
                "est_source": "heuristic",
                "status": status,
                "source_url": a.get("html_url"),
            })

    return {
        "courses": courses,
        "assignments": assignments,
        "exported_at": data.get("exported_at"),
        "term": wanted,
        "skipped_courses": len(skipped),
        "skipped_assignments": sum(skipped.values()),
        "skipped_detail": skipped,
    }


def detect_current_term(courses: list[dict]) -> str | None:
    """
    Work out which semester is current from the course list.

    Pitt term codes are four digits that increase over time (2261 spring, 2264
    summer, 2271 fall), so the largest present is now. Beats hardcoding a date,
    because it keeps working next semester without anyone editing anything.
    """
    found = set()
    for course in courses:
        code = (_term_of(course.get("name") or "")
                or _term_of(course.get("course_code") or ""))
        if code:
            found.add(code)
    return max(found) if found else None


def _term_of(label: str) -> str | None:
    """'2271 CS 1684 SEC1010 BIAS...' -> '2271'. None if there's no term code."""
    match = re.match(r"^\s*(\d{4})\s", str(label))
    return match.group(1) if match else None


def _clean_course_name(raw: str) -> tuple[str, str]:
    r"""
    '2271 CS 1684 SEC1010 BIAS & ETHICAL IMPLICTNS IN AI'
        -> ('CS 1684', 'Bias & Ethical Implictns In Ai')

    Two things that were wrong here:

    1. The SEC group was written `(?:SEC\s*\w+\s+)?` -- note the trailing
       \s+. When the section is the LAST thing in the string ('2271 CS 1675
       SEC1100', which is what course_code looks like), there's no trailing
       space, so the group didn't match and 'SEC1100' became the title.
    2. Canvas puts the descriptive name in `name`, not `course_code`. Reading
       course_code gave titles like 'Sec1100' for every course.
    """
    text = re.sub(r"\s+", " ", str(raw)).strip()
    m = re.match(
        r"^(?:\d{4}\s+)?"              # optional term code
        r"([A-Z]{2,8})\s*"              # subject
        r"(\d{3,4}[A-Z]?)"              # number
        r"(?:\s+SEC\s*\w+)?"           # optional section, may end the string
        r"\s*(.*)$",                    # the rest is the title
        text,
        re.IGNORECASE,
    )
    if not m:
        return ("", text)
    subject, number, title = m.groups()
    title = title.strip()
    # A leftover bare section number is not a title.
    if re.fullmatch(r"(?:SEC\s*\w+)?", title, re.IGNORECASE):
        title = ""
    return (f"{subject.upper()} {number}", _titlecase(title))


def _titlecase(text: str) -> str:
    """Canvas course names are SHOUTED AND ABBREVIATED. Make them readable."""
    if not text:
        return ""
    # Canvas abbreviations spelled out.
    expand = {
        "COMPUTR": "Computer", "ORGZTN": "Organization", "ASSMBLY": "Assembly",
        "LANG": "Language", "FNDTNS": "Foundations", "FNDTN": "Foundation",
        "IMPLICTNS": "Implications", "PRINC": "Principles", "PRGRMMNG": "Programming",
        "ALGRTHM": "Algorithm", "ALGRTHMS": "Algorithms", "STRCTRS": "Structures",
        "DISCRT": "Discrete", "MTHMTCS": "Mathematics", "ELECTR": "Electricity",
        "MAGNTSM": "Magnetism", "ORCH": "Orchestra", "INSTRMNTN": "Instrumentation",
    }
    # Left uppercase: they're acronyms, not words.
    acronyms = {"AI", "ML", "CS", "II", "III", "IV", "UI", "UX", "HCI", "OS", "DB"}
    # Left lowercase: joining words read wrong when capitalised mid-title.
    connectors = {"to", "in", "of", "and", "or", "for", "the", "a", "an", "on", "with"}

    words = []
    for i, w in enumerate(text.split()):
        upper = w.upper()
        if upper in expand:
            words.append(expand[upper])
        elif upper in acronyms:
            words.append(upper)
        elif upper.lower() in connectors and i > 0:
            words.append(upper.lower())
        elif w == "&":
            words.append("&")
        else:
            words.append(w.capitalize())
    return " ".join(words).strip()


def _guess_kind(title: str, submission_types: list) -> str:
    """Canvas doesn't say 'this is a lab'. Infer it from the title."""
    lowered = title.lower()
    for needle, kind in _KIND_HINTS:
        if needle in lowered:
            return kind
    if "online_quiz" in submission_types:
        return "quiz"
    if "discussion_topic" in submission_types:
        return "reading"
    return "other"


def _estimate_hours(kind: str, points: object) -> float:
    """
    A rough guess so the scheduler has something to work with. Bigger point
    values mean more work, so scale the type's baseline a little.
    """
    base = _DEFAULT_HOURS.get(kind, 2.0)
    try:
        pts = float(points) if points is not None else None
    except (TypeError, ValueError):
        pts = None
    if pts:
        if pts >= 100:
            base *= 1.8
        elif pts >= 50:
            base *= 1.2
        elif pts <= 10:
            base *= 0.6
    return round(base, 1)


def read_source(name: str) -> str:
    """Returns raw HTML. The only function that differs between local and http."""
    if CANVAS_SOURCE == "local":
        path = CANVAS_DIR / name
        if not path.exists():
            raise FileNotFoundError(
                f"No such page: {path}. Available: {list_sources() or '(none)'}"
            )
        return path.read_text(encoding="utf-8", errors="replace")

    import requests

    url = name if name.startswith("http") else f"{CANVAS_BASE_URL.rstrip('/')}/{name.lstrip('/')}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.text


# --------------------------------------------------------------------------
# Step 2: strip
# --------------------------------------------------------------------------


def is_plain_text(name: str) -> bool:
    """
    .txt files come from page_grab.js, which already extracted the rendered
    text in the browser. Running the HTML stripper over them would be harmless
    but pointless.
    """
    return name.lower().endswith(".txt")


def html_to_text(raw_html: str, max_chars: int = 12000) -> str:
    """
    Turn a Canvas page into clean text.

    BeautifulSoup is an HTML parser: it reads messy real-world HTML into a tree
    you can walk. `.decompose()` deletes a branch of that tree entirely.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw_html, "html.parser")

    for tag_name in _DROP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # get_text with a separator keeps items on separate lines, which matters:
    # without it "Problem Set 4Due Sep 22" runs together and the model has to
    # guess where one assignment ends and the next begins.
    text = soup.get_text(separator="\n")

    lines: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line or _BOILERPLATE.match(line):
            continue
        if len(line) == 1 and not line.isalnum():  # stray separators
            continue
        if lines and lines[-1] == line:  # Canvas repeats titles for screen readers
            continue
        lines.append(line)

    return "\n".join(lines)[:max_chars]


# --------------------------------------------------------------------------
# Is this page actually usable?
# --------------------------------------------------------------------------

# Telltales that a file is a saved single-page-app shell rather than content.
_SHELL_SIGNS = (
    "you need to have javascript enabled",
    "empty card",
    "loading...",
)


def diagnose(raw_html: str, name: str = "page") -> dict:
    """
    Decide whether a page has real content before we spend money on it.

    Canvas is a single-page app: the server sends a skeleton and JavaScript
    draws the page afterwards. "Save as -> Webpage, HTML Only" captures the
    skeleton, so there are no assignments in the file at all. Without this
    check you'd get a confident, empty answer and no idea why.
    """
    text = html_to_text(raw_html)
    lowered = text.lower()

    signs = [s for s in _SHELL_SIGNS if s in lowered]
    has_env = "ENV = {" in raw_html or "ENV =" in raw_html
    # Real assignment pages mention dates and points. Shells don't.
    looks_like_content = bool(
        re.search(r"\b(due|pts|points|assignment)\b", lowered)
    ) and len(text) > 200

    verdict = "ok"
    advice = ""

    if signs or (len(text) < 200 and has_env):
        verdict = "javascript_shell"
        advice = (
            f"'{name}' is a saved Canvas page with no content in it. Canvas "
            f"draws its pages with JavaScript, so a saved copy is an empty "
            f"skeleton ({len(raw_html):,} chars of HTML, only {len(text)} "
            f"chars of readable text).\n\n"
            f"Fix it one of these ways:\n"
            f"  1. python3 make_dummy_canvas.py <that file>\n"
            f"     Pulls your real course list out of the file and generates\n"
            f"     realistic dummy assignment pages. Recommended.\n"
            f"  2. Copy the RENDERED page instead: open the Canvas assignments\n"
            f"     page, press Cmd-Option-I, click Console, paste\n"
            f"       copy(document.documentElement.outerHTML)\n"
            f"     then paste into a new .html file.\n"
            f"  3. Use the Canvas REST API with a personal access token "
            f"(cleanest; see README)."
        )
    elif not looks_like_content:
        verdict = "no_assignments_found"
        advice = (
            f"'{name}' parsed to {len(text)} chars of text but nothing that "
            f"looks like an assignment (no dates, no point values). If this is "
            f"the Dashboard page, try a course's Assignments page instead -- "
            f"the dashboard never lists assignments."
        )

    return {
        "verdict": verdict,
        "usable": verdict == "ok",
        "html_chars": len(raw_html),
        "text_chars": len(text),
        "shell_signs": signs,
        "advice": advice,
    }


# --------------------------------------------------------------------------
# Step 3: extract
# --------------------------------------------------------------------------

EXTRACT_SYSTEM = """You read text scraped from a university Canvas page and \
return structured data about it.

Return ONLY a JSON object, no prose, no markdown fences:

{
  "course": {"code": "PHYS 1361", "title": "...", "instructor": "...", "canvas_id": "..."},
  "assignments": [
    {
      "title": "Problem Set 4",
      "kind": "homework|quiz|exam|lab|project|reading|other",
      "due_at": "2026-09-22T23:59:00-04:00",
      "points": 50,
      "est_hours": 3.0,
      "status": "open|submitted|graded",
      "description": "one short sentence"
    }
  ]
}

Rules:
- due_at must be full ISO 8601 with a timezone offset. The page shows dates \
like "Sep 22 at 11:59pm" with no year and no zone: use the year given in \
CONTEXT below and assume America/New_York (-04:00 during daylight saving, \
-05:00 otherwise).
- est_hours is YOUR estimate of how long the task takes a typical student. If \
the page states a duration, use it. Otherwise estimate from the type and point \
value: a quiz is usually 0.5-1.5, a problem set 2-4, a lab writeup 2-4, an \
exam 1.5-3, a project 6-20. Never leave it null.
- status: "graded" if a score out of total is shown, "submitted" if marked \
submitted, otherwise "open".
- Include past/graded assignments too, marked with the right status.
- Copy titles as written. Do not invent assignments that aren't in the text."""


def extract(page_text: str, source_name: str) -> dict:
    """Ask Claude to turn the cleaned text into structured data."""
    now = datetime.now(timezone.utc).astimezone()
    context = (
        f"CONTEXT: today is {now.strftime('%Y-%m-%d')}, current academic year "
        f"{now.year}, timezone America/New_York.\n"
        f"SOURCE: {source_name}\n\n--- PAGE TEXT ---\n{page_text}"
    )

    result = cache.claude(
        system=EXTRACT_SYSTEM,
        user=context,
        max_tokens=4000,
        label=f"extract:{source_name}",
    )
    if "error" in result:
        return result

    # Sanity-check the shape before it reaches the database.
    if not isinstance(result.get("assignments"), list):
        return {"error": "extractor returned no assignments list", "got": str(result)[:300]}
    return result


# --------------------------------------------------------------------------
# The whole job
# --------------------------------------------------------------------------


def crawl(student_id: str | None = None, pages: list[str] | None = None) -> dict:
    """
    Read every page, extract, write to the database, record a history snapshot.

    Returns a summary that Nemotron can read and act on.
    """
    student_id = student_id or db.DEFAULT_STUDENT
    targets = pages or list_sources()

    if not targets:
        return {
            "error": f"No Canvas pages found. Put .html files in ./{CANVAS_DIR}/ "
                     f"(or set CANVAS_SOURCE=http and CANVAS_URLS).",
            "synced": 0,
        }

    summary: dict = {"synced": 0, "courses": [], "pages": [], "errors": []}

    for name in targets:
        page: dict = {"page": name}
        try:
            # JSON exports are already structured: no stripping, no model call.
            if name.lower().endswith(".json"):
                data = load_export(CANVAS_DIR / name)
                db.upsert_courses(student_id, data["courses"], source=name)
                written = db.upsert_assignments(student_id, data["assignments"])
                page.update(written)
                page["source"] = "canvas api export"
                page["courses"] = [c["code"] for c in data["courses"]]
                page["exported_at"] = data.get("exported_at")
                summary["synced"] += written.get("assignments_written", 0)
                for c in data["courses"]:
                    if c["code"] not in summary["courses"]:
                        summary["courses"].append(c["code"])
                summary["pages"].append(page)
                continue

            raw = read_source(name)

            # A .txt capture is already clean text from the browser.
            if is_plain_text(name):
                text = raw[:12000]
                page["html_chars"] = len(raw)
                page["text_chars"] = len(text)
                page["source"] = "browser capture"
                data = extract(text, name)
                if "error" in data:
                    page["error"] = data["error"]
                    summary["errors"].append(f"{name}: {data['error']}")
                    summary["pages"].append(page)
                    continue
                course = data.get("course") or {}
                code = course.get("code")
                if not code:
                    page["error"] = "no course code found in capture"
                    summary["errors"].append(f"{name}: no course code")
                    summary["pages"].append(page)
                    continue
                db.upsert_courses(student_id, [course], source=name)
                items = data["assignments"]
                for item in items:
                    item["course"] = code
                    item.setdefault("est_source", "claude")
                written = db.upsert_assignments(student_id, items)
                page.update(written)
                page["course"] = code
                summary["synced"] += written.get("assignments_written", 0)
                if code not in summary["courses"]:
                    summary["courses"].append(code)
                summary["pages"].append(page)
                continue

            # Don't pay to extract from a page that has no content in it.
            check = diagnose(raw, name)
            page["html_chars"] = check["html_chars"]
            page["text_chars"] = check["text_chars"]
            if not check["usable"]:
                page["error"] = check["verdict"]
                page["advice"] = check["advice"]
                summary["errors"].append(f"{name}: {check['verdict']}")
                summary["pages"].append(page)
                continue

            text = html_to_text(raw)
            data = extract(text, name)
            if "error" in data:
                page["error"] = data["error"]
                summary["errors"].append(f"{name}: {data['error']}")
                summary["pages"].append(page)
                continue

            course = data.get("course") or {}
            code = course.get("code")
            if not code:
                page["error"] = "no course code found on page"
                summary["errors"].append(f"{name}: no course code")
                summary["pages"].append(page)
                continue

            db.upsert_courses(student_id, [course], source=name)

            items = data["assignments"]
            for item in items:
                item["course"] = code
                item.setdefault("est_source", "claude")
            written = db.upsert_assignments(student_id, items)

            page.update(written)
            page["course"] = code
            summary["synced"] += written.get("assignments_written", 0)
            if code not in summary["courses"]:
                summary["courses"].append(code)

        except Exception as exc:  # noqa: BLE001
            page["error"] = f"{type(exc).__name__}: {exc}"
            summary["errors"].append(f"{name}: {exc}")

        summary["pages"].append(page)

    # This is the row that makes the time-series chart work.
    if summary["synced"]:
        db.record_workload_snapshot(student_id)

    summary["note"] = (
        f"Wrote {summary['synced']} assignments across "
        f"{len(summary['courses'])} course(s) to Tiger Data."
    )
    return summary


# --------------------------------------------------------------------------
# Try it without the database or an API key
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    pages = list_sources()
    print(f"CANVAS_SOURCE={CANVAS_SOURCE}  dir={CANVAS_DIR}")
    print(f"pages found: {pages or '(none)'}\n")

    problems = 0
    for name in pages:
        if name.lower().endswith(".json"):
            print("=" * 70)
            try:
                data = load_export(CANVAS_DIR / name)
            except (ValueError, KeyError) as exc:
                problems += 1
                print(f"{name}: UNUSABLE — {exc}")
                print()
                continue
            print(f"{name}: Canvas API export  [OK — no model call needed]")
            print("=" * 70)
            print(f"exported {data.get('exported_at')}")
            print(f"current term: {data.get('term') or '(none detected)'}")
            print(f"{len(data['courses'])} courses, "
                  f"{len(data['assignments'])} assignments")
            if data.get("skipped_courses"):
                print(f"skipped {data['skipped_courses']} course(s) / "
                      f"{data['skipped_assignments']} assignments "
                      f"(other terms, or not semester courses):")
                for reason, count in sorted(
                        data["skipped_detail"].items(),
                        key=lambda kv: -kv[1])[:8]:
                    print(f"    {count:>4}  {reason}")
            print()
            for c in data["courses"]:
                items = [a for a in data["assignments"] if a["course"] == c["code"]]
                openn = sum(1 for a in items if a["status"] == "open")
                print(f"  {c['code']:<14} {len(items):>3} assignments "
                      f"({openn} open)  {c['title'][:34]}")
            print()
            upcoming = sorted(
                (a for a in data["assignments"] if a["status"] == "open" and a["due_at"]),
                key=lambda a: a["due_at"],
            )[:8]
            if upcoming:
                print("  next up:")
                for a in upcoming:
                    print(f"    {a['due_at'][:16]}  {a['course']:<12} "
                          f"{a['title'][:40]:<40} ~{a['est_hours']}h")
            print()
            continue

        raw = read_source(name)
        if is_plain_text(name):
            print("=" * 70)
            print(f"{name}: browser capture, {len(raw):,} chars  [OK]")
            print("=" * 70)
            print(raw[:2000])
            print()
            continue
        check = diagnose(raw, name)
        text = html_to_text(raw)

        print("=" * 70)
        status = "OK" if check["usable"] else f"UNUSABLE ({check['verdict']})"
        print(f"{name}: {check['html_chars']:,} chars HTML -> "
              f"{check['text_chars']:,} chars text  [{status}]")
        print("=" * 70)

        if not check["usable"]:
            problems += 1
            print(check["advice"])
            print()
            continue

        print(text)
        print()

    if problems:
        print(f"{problems} of {len(pages)} page(s) unusable. See the advice above.")

    if "--extract" in sys.argv:
        for name in pages:
            print(f"\n--- extracting {name} ---")
            print(json.dumps(extract(html_to_text(read_source(name)), name), indent=2))
