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
    """Which pages can we crawl right now?"""
    if CANVAS_SOURCE == "local":
        if not CANVAS_DIR.exists():
            return []
        return sorted(p.name for p in CANVAS_DIR.glob("*.htm*"))
    return [u.strip() for u in os.getenv("CANVAS_URLS", "").split(",") if u.strip()]


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
            raw = read_source(name)
            text = html_to_text(raw)
            page["html_chars"] = len(raw)
            page["text_chars"] = len(text)

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

    for name in pages:
        raw = read_source(name)
        text = html_to_text(raw)
        print("=" * 70)
        print(f"{name}: {len(raw):,} chars of HTML -> {len(text):,} chars of text "
              f"({len(raw) / max(len(text), 1):.0f}x smaller)")
        print("=" * 70)
        print(text)
        print()

    if "--extract" in sys.argv:
        for name in pages:
            print(f"\n--- extracting {name} ---")
            print(json.dumps(extract(html_to_text(read_source(name)), name), indent=2))
