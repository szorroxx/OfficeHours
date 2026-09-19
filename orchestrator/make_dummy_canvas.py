"""
Turn a saved Canvas page into safe, realistic dummy data.

THE PROBLEM THIS SOLVES
Canvas is a single-page app. When you hit "Save as", the browser saves the HTML
the *server* sent, which is an empty skeleton -- the actual dashboard is drawn
afterwards by 45KB of JavaScript. So a saved page has no assignments in it. Not
a parser bug: there is genuinely nothing there to parse.

But the skeleton does contain one useful thing. Canvas embeds a big JavaScript
object called ENV with your real course list in it: codes, names, ids, terms.
That part is real data.

So this script:
  1. pulls your real course list out of the saved page
  2. throws away every personal and session field
  3. generates realistic dummy assignment pages, one per course

You end up with dummy Canvas HTML that uses your actual course codes (so the
demo looks real) and contains zero personal information (so it's safe to commit
to a public repo).

USE
    python3 make_dummy_canvas.py ~/Downloads/Dashboard.html
    python3 make_dummy_canvas.py ~/Downloads/Dashboard.html --term 2261
    python3 make_dummy_canvas.py ~/Downloads/Dashboard.html --list

Then:
    python3 canvas.py            # see what the parser reads
"""

from __future__ import annotations

import argparse
import html
import json
import random
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Fields in ENV that are personal or are session credentials. None of these
# ever leave this script.
SENSITIVE = (
    "OAK_SESSION_KEY", "current_user_uuid", "current_user_id", "USER_EMAIL",
    "current_user_global_id", "current_user_usage_metrics_id", "captcha_site_key",
    "DOMAIN_ROOT_ACCOUNT_UUID", "DOMAIN_ROOT_ACCOUNT_SFID", "current_user",
    "sentry-trace", "SENTRY_FRONTEND", "OBSERVED_USERS_LIST", "PREFERENCES",
)


# --------------------------------------------------------------------------
# Reading the ENV object out of a saved page
# --------------------------------------------------------------------------


def extract_env(raw_html: str) -> dict:
    """
    Find `ENV = {...}` in a <script> tag and parse it.

    We can't use a regex for the closing brace, because the object contains
    thousands of nested braces. So we walk forward counting depth, while
    tracking whether we're inside a string (a '}' inside "text" doesn't count).
    """
    marker = re.search(r"\bENV\s*=\s*\{", raw_html)
    if not marker:
        raise ValueError(
            "No ENV object found. Either this isn't a Canvas page, or it's a "
            "rendered-DOM copy rather than a saved source file."
        )

    start = raw_html.index("{", marker.start())
    depth, i = 0, start
    in_string, escaped = False, False

    while i < len(raw_html):
        ch = raw_html[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        i += 1

    return json.loads(raw_html[start:i + 1])


def courses_from_env(env: dict) -> list[dict]:
    """Pull the course list and keep only the harmless fields."""
    raw = env.get("STUDENT_PLANNER_COURSES") or []
    out = []
    for c in raw:
        code, name = _split_course_name(c.get("shortName") or c.get("originalName") or "")
        out.append({
            "canvas_id": str(c.get("id") or ""),
            "code": code,
            "title": name,
            "term": _term_of(c.get("shortName") or ""),
            "long_name": (c.get("longName") or "").strip(),
        })
    return [c for c in out if c["code"]]


def _split_course_name(short_name: str) -> tuple[str, str]:
    """
    Pitt Canvas names look like:
        '2261 CS 0447 SEC1070 COMPUTR ORGZTN & ASSMBLY LANG'
         ^term ^^^^^^^^ code   ^^^^^^^ section  ^^^^ title
    Pull out the course code and a readable title.
    """
    text = re.sub(r"\s+", " ", short_name).strip()
    m = re.match(
        r"^(?:\d{4}\s+)?"                      # optional term number
        r"([A-Z]{2,8})\s*(\d{3,4}[A-Z]?)\s+"   # subject + number
        r"(?:SEC\s*\w+\s+)?"                   # optional section
        r"(.*)$",                              # the rest is the title
        text,
    )
    if not m:
        return ("", text)
    subject, number, title = m.groups()
    return (f"{subject} {number}", _titlecase(title))


def _titlecase(text: str) -> str:
    """Canvas titles are SHOUTED AND ABBREVIATED. Make them readable."""
    fixes = {
        "COMPUTR": "Computer", "ORGZTN": "Organization", "ASSMBLY": "Assembly",
        "LANG": "Language", "INTRO": "Introduction", "FNDTNS": "Foundations",
        "MATH": "Mathematical", "FNDTN": "Foundation", "INT": "Intermediate",
        "PHYS": "Physics", "SC": "Science", "&": "and",
    }
    words = [fixes.get(w, w.capitalize()) for w in text.split()]
    return " ".join(words).strip()


def _term_of(short_name: str) -> str:
    m = re.match(r"^(\d{4})\s", short_name.strip())
    return m.group(1) if m else ""


# --------------------------------------------------------------------------
# Generating dummy assignments
# --------------------------------------------------------------------------

# Templates chosen so the parser has something of every kind to chew on.
PATTERNS = {
    "CS": [
        ("Project {n}: {topic}", "project", 100, 12),
        ("Lab {n}", "lab", 20, 2),
        ("Homework {n}", "homework", 40, 4),
        ("Quiz {n}", "quiz", 25, 1),
        ("Midterm Exam", "exam", 150, 3),
    ],
    "MATH": [
        ("Problem Set {n}", "homework", 50, 3),
        ("Quiz {n}", "quiz", 20, 1),
        ("Midterm Exam {n}", "exam", 150, 2),
    ],
    "PHYS": [
        ("Problem Set {n}", "homework", 50, 3),
        ("Lab {n} Writeup", "lab", 30, 3),
        ("Quiz {n}", "quiz", 25, 1),
        ("Midterm Exam", "exam", 200, 2),
    ],
    "ENGCMP": [
        ("Essay {n} Draft", "project", 100, 6),
        ("Peer Review {n}", "homework", 20, 1),
        ("Reading Response {n}", "reading", 10, 1),
    ],
    "MUSIC": [
        ("Rehearsal Attendance Week {n}", "homework", 10, 2),
        ("Concert Performance", "project", 100, 4),
        ("Listening Journal {n}", "reading", 15, 1),
    ],
    "_default": [
        ("Assignment {n}", "homework", 50, 3),
        ("Quiz {n}", "quiz", 25, 1),
        ("Final Project", "project", 100, 8),
    ],
}

TOPICS = ["Data Structures", "Sorting", "Graph Traversal", "Memory Management",
          "Concurrency", "Optimization", "Numerical Methods", "Field Mapping"]


def make_assignments(course: dict, count: int, rng: random.Random,
                     start: datetime) -> list[dict]:
    subject = course["code"].split()[0]
    patterns = PATTERNS.get(subject, PATTERNS["_default"])

    items, day = [], 2
    for i in range(count):
        title_tpl, kind, points, hours = patterns[i % len(patterns)]
        n = (i // len(patterns)) + 1
        title = title_tpl.format(n=n, topic=rng.choice(TOPICS))

        due = start + timedelta(days=day, hours=rng.choice([10, 14, 23]))
        if due.hour == 23:
            due = due.replace(minute=59)
        day += rng.choice([3, 4, 5, 7])

        items.append({
            "title": title,
            "kind": kind,
            "points": points,
            "hours": hours,
            "due": due,
            "graded": i < max(1, count // 4),  # the first few are already done
        })
    return items


# --------------------------------------------------------------------------
# Writing the HTML
# --------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<!-- GENERATED DUMMY DATA. Real course names, invented assignments.
     Contains no personal information. Safe to commit. -->
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Assignments: {code} {title}</title>
</head>
<body class="course-menu-expanded">
<div id="application" class="ic-app">
  <nav aria-label="Courses Navigation Menu">
    <ul class="section-tabs">
      <li><a href="/courses/{cid}" class="home">Home</a></li>
      <li><a href="/courses/{cid}/assignments" class="active">Assignments</a></li>
      <li><a href="/courses/{cid}/grades">Grades</a></li>
    </ul>
  </nav>
  <div id="content" class="ic-Layout-contentMain" role="main">
    <div class="course-title-header">
      <h2>{code} &mdash; {title}</h2>
      <p class="instructor">Instructor: {instructor}</p>
    </div>
    <div id="ag-list" class="assignment_group">
{groups}
    </div>
  </div>
</div>
</body>
</html>
"""

GROUP = """      <h3 class="ig-header-title">{heading}</h3>
      <ul class="ig-list">
{rows}
      </ul>
"""

ROW = """        <li class="assignment" data-id="{aid}">
          <div class="ig-info">
            <a href="/courses/{cid}/assignments/{aid}" class="ig-title">{title}</a>
            <div class="ig-details">
              <span class="assignment-date-due">Due <span class="date_text">{due}</span></span>
              <span class="score-display">{score}</span>
              {status}
            </div>
            <div class="ig-description">{desc}</div>
          </div>
        </li>
"""

INSTRUCTORS = ["Dr. A. Ramirez", "Dr. J. Chen", "Prof. M. Okonkwo", "Dr. S. Patel",
               "Prof. L. Novak", "Dr. R. Whitfield", "Prof. D. Castellanos"]

DESCRIPTIONS = {
    "homework": "Complete the assigned problems and show your work. Expect about {h} hours.",
    "lab": "Formal writeup required, 4-6 pages. Roughly {h} hours including analysis.",
    "quiz": "In-class, closed book. About {h} hour of review recommended.",
    "exam": "Covers material through the current unit. Plan {h} hours of studying.",
    "project": "Submit code and a short report. Budget around {h} hours total.",
    "reading": "Read the assigned chapter and post a response. About {h} hour.",
}


def render_page(course: dict, items: list[dict], rng: random.Random) -> str:
    upcoming = [i for i in items if not i["graded"]]
    past = [i for i in items if i["graded"]]

    groups = []
    for heading, group in (("Upcoming Assignments", upcoming),
                           ("Past Assignments", past)):
        if not group:
            continue
        rows = []
        for item in group:
            aid = rng.randint(80000, 99999)
            if item["graded"]:
                earned = int(item["points"] * rng.uniform(0.82, 1.0))
                score = f"{earned}/{item['points']} pts"
                status = '<span class="submitted">Submitted &middot; Graded</span>'
            else:
                score = f"{item['points']} pts"
                status = ""
            rows.append(ROW.format(
                aid=aid, cid=course["canvas_id"] or "0000",
                title=html.escape(item["title"]),
                due=item["due"].strftime("%b %-d at %-I:%M%p").replace("AM", "am").replace("PM", "pm"),
                score=score, status=status,
                desc=DESCRIPTIONS[item["kind"]].format(h=item["hours"]),
            ))
        groups.append(GROUP.format(heading=heading, rows="".join(rows)))

    return PAGE.format(
        code=html.escape(course["code"]),
        title=html.escape(course["title"] or "Course"),
        cid=course["canvas_id"] or "0000",
        instructor=rng.choice(INSTRUCTORS),
        groups="".join(groups),
    )


def filename_for(course: dict, taken: set[str] | None = None) -> str:
    """
    Same course code can appear in two terms (University Orchestra every
    semester), so add the term if the plain name is already used.
    """
    slug = re.sub(r"[^a-z0-9]+", "", course["code"].lower())
    name = f"{slug}_assignments.html"
    if taken is not None and name in taken:
        name = f"{slug}_{course['term']}_assignments.html"
    if taken is not None:
        taken.add(name)
    return name


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("saved_page", help="a Canvas page you saved with Save As")
    ap.add_argument("--out", default="canvas_pages", help="output folder")
    ap.add_argument("--term", help="only courses from this term, e.g. 2261")
    ap.add_argument("--courses", help="comma-separated codes, e.g. 'CS 0447,PHYS 0477'")
    ap.add_argument("--count", type=int, default=8, help="assignments per course")
    ap.add_argument("--list", action="store_true", help="just list courses and exit")
    ap.add_argument("--seed", type=int, default=7,
                    help="same seed gives the same dummy data every run")
    args = ap.parse_args()

    path = Path(args.saved_page).expanduser()
    if not path.exists():
        print(f"No such file: {path}")
        return 1

    try:
        env = extract_env(path.read_text(encoding="utf-8", errors="replace"))
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"Could not read Canvas data from that file:\n  {exc}")
        return 1

    present = [f for f in SENSITIVE if f in json.dumps(env) or f in env]
    courses = courses_from_env(env)

    if not courses:
        print("Found the ENV object but no course list in it.")
        print("Try saving the Dashboard page rather than a single course page.")
        return 1

    print(f"Found {len(courses)} courses in {path.name}")
    if present:
        print(f"\n  !!  That file also contains {len(present)} personal/session "
              f"fields (session key, user id, email, feed token).")
        print(f"  !!  Do NOT commit it. None of it goes into the output below.")

    terms = sorted({c["term"] for c in courses if c["term"]}, reverse=True)
    if terms:
        print(f"\nterms present: {', '.join(terms)} (newest first)")

    if args.list:
        print()
        for c in courses:
            print(f"  [{c['term']}] {c['code']:<14} {c['title']}")
        print("\nNarrow it down with --term or --courses, then run without --list.")
        return 0

    selected = courses
    if args.term:
        selected = [c for c in selected if c["term"] == args.term]
    if args.courses:
        wanted = {w.strip().upper() for w in args.courses.split(",")}
        selected = [c for c in selected if c["code"].upper() in wanted]

    if not selected:
        print("\nNothing matched that filter. Run with --list to see the options.")
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    taken: set[str] = set()
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    print(f"\nwriting {len(selected)} page(s) to {out_dir}/")
    total = 0
    for course in selected:
        items = make_assignments(course, args.count, rng, start)
        target = out_dir / filename_for(course, taken)
        target.write_text(render_page(course, items, rng))
        total += len(items)
        print(f"  {target.name:<34} {course['code']:<14} {len(items)} assignments")

    print(f"\n{total} dummy assignments across {len(selected)} courses.")
    print("Real course codes, invented assignments, no personal data.\n")
    print("Next:")
    print("  python3 canvas.py          # see what the parser reads")
    print("  python3 ask.py \"what's due this week?\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
