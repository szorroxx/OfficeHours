"""
All database access lives here. Nothing else in the project touches SQL.

WHY THIS FILE IS SHAPED LIKE THIS
Nemotron picks *which* function to call. This file decides *what SQL runs*.
Every write function validates its input first and ignores fields it doesn't
recognize. So the worst a confused model can do is write a row with a silly
title -- it can't drop a table or invent a column.

Rowan: import from this module rather than writing your own queries, so we
can't disagree about the schema.

Setup:
    python3 db.py --init     create tables + one demo student
    python3 db.py --seed     add fake assignments (for testing without crawling)
    python3 db.py --check    print row counts, confirm the connection works
    python3 db.py --reset    DROP EVERYTHING and re-init (asks first)
"""

from __future__ import annotations

import config  # noqa: F401  - loads .env before anything reads it
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DATABASE_URL = os.getenv("DATABASE_URL", "")
DEFAULT_STUDENT = os.getenv("STUDENT_ID", "demo-student")

_pool = None


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------


def pool():
    """
    A connection pool is a small set of reusable open connections. Opening a
    fresh connection to a cloud database takes ~200ms, so reusing them makes
    every request noticeably faster.
    """
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. Put the Timescale connection string "
                "in your .env file (and make sure .env is in .gitignore)."
            )
        from psycopg_pool import ConnectionPool

        _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=4, open=True)
    return _pool


def query(sql: str, params: tuple = (), fetch: str = "all") -> Any:
    """
    Run one statement. `fetch` is 'all', 'one', or 'none'.

    Params are passed separately from the SQL string, never formatted into it.
    That is what makes SQL injection impossible here: the database treats them
    as values, never as code.
    """
    from psycopg.rows import dict_row

    with pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            if fetch == "all":
                return cur.fetchall()
            if fetch == "one":
                return cur.fetchone()
            return None


def make_id(*parts: str) -> str:
    """
    Build a stable id from text. Same inputs always give the same id, so
    crawling a page twice updates the existing row instead of adding a copy.
    """
    slug = ":".join(re.sub(r"\s+", " ", str(p or "")).strip().lower() for p in parts)
    return hashlib.sha1(slug.encode()).hexdigest()[:20]


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------


class ValidationError(ValueError):
    pass


_KINDS = {"homework", "quiz", "exam", "lab", "project", "reading", "other"}
_STATUSES = {"open", "submitted", "graded"}


def _as_datetime(value: Any, field: str) -> datetime | None:
    """Accept ISO strings, datetimes, or None. Reject anything else."""
    if value in (None, "", "null"):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ValidationError(f"{field}: could not read '{value}' as a date")
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise ValidationError(f"{field}: expected a date, got {type(value).__name__}")


def _as_number(value: Any, field: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field}: expected a number, got {value!r}")


def _as_int(value: Any, field: str) -> int | None:
    n = _as_number(value, field)
    return None if n is None else int(round(n))


def _text(value: Any, field: str, required: bool = False, limit: int = 2000) -> str | None:
    if value in (None, ""):
        if required:
            raise ValidationError(f"{field} is required")
        return None
    return str(value).strip()[:limit]


# --------------------------------------------------------------------------
# READS
# --------------------------------------------------------------------------


def get_profile(student_id: str = DEFAULT_STUDENT) -> dict:
    row = query(
        """SELECT id, display_name, email, timezone, canvas_base, prefs,
                  (auth_token IS NOT NULL) AS has_token
           FROM students WHERE id = %s""",
        (student_id,),
        fetch="one",
    )
    return dict(row) if row else {}


def get_courses(student_id: str = DEFAULT_STUDENT) -> list[dict]:
    return [
        dict(r)
        for r in query(
            """SELECT code, title, instructor, canvas_id, last_crawled_at
               FROM courses WHERE student_id = %s ORDER BY code""",
            (student_id,),
        )
    ]


def get_assignments(
    student_id: str = DEFAULT_STUDENT,
    course: str | None = None,
    due_within_days: int | None = None,
    status: str = "open",
) -> list[dict]:
    sql = [
        """SELECT id, course_code, title, kind, due_at, points, est_hours,
                  est_source, priority, status
           FROM assignments WHERE student_id = %s"""
    ]
    params: list[Any] = [student_id]

    if status and status != "any":
        sql.append("AND status = %s")
        params.append(status)
    if course:
        # ILIKE is case-insensitive matching, so 'phys 1361' finds 'PHYS 1361'.
        sql.append("AND course_code ILIKE %s")
        params.append(f"%{course}%")
    if due_within_days is not None:
        sql.append("AND due_at IS NOT NULL AND due_at <= now() + %s * interval '1 day'")
        params.append(int(due_within_days))

    sql.append("ORDER BY due_at NULLS LAST LIMIT 100")
    return [dict(r) for r in query(" ".join(sql), tuple(params))]


def get_schedule(student_id: str = DEFAULT_STUDENT) -> dict:
    """The newest generation of schedule blocks."""
    newest = query(
        """SELECT generation_id, generated_at, rationale
           FROM schedule_blocks WHERE student_id = %s
           ORDER BY generated_at DESC LIMIT 1""",
        (student_id,),
        fetch="one",
    )
    if not newest:
        return {"blocks": [], "generated_at": None}
    blocks = query(
        """SELECT assignment_id, task, starts_at, ends_at, est_minutes, priority
           FROM schedule_blocks WHERE generation_id = %s ORDER BY starts_at""",
        (newest["generation_id"],),
    )
    return {
        "generated_at": newest["generated_at"],
        "rationale": newest["rationale"],
        "blocks": [dict(b) for b in blocks],
    }


def get_study_sets(student_id: str = DEFAULT_STUDENT, course: str | None = None) -> list[dict]:
    sql = """SELECT id, course_code, topic, format, content, created_at
             FROM study_sets WHERE student_id = %s"""
    params: list[Any] = [student_id]
    if course:
        sql += " AND course_code ILIKE %s"
        params.append(f"%{course}%")
    sql += " ORDER BY created_at DESC LIMIT 20"
    return [dict(r) for r in query(sql, tuple(params))]


def get_events(within_days: int = 14) -> list[dict]:
    return [
        dict(r)
        for r in query(
            """SELECT id, title, starts_at, location, url, tags
               FROM campus_events
               WHERE starts_at BETWEEN now() AND now() + %s * interval '1 day'
               ORDER BY starts_at LIMIT 50""",
            (int(within_days),),
        )
    ]


def check_freshness(student_id: str = DEFAULT_STUDENT) -> dict:
    """
    How old is the data for each course? This is what the agent reads to
    decide "I should re-crawl before answering."
    """
    rows = query(
        """SELECT code,
                  last_crawled_at,
                  EXTRACT(EPOCH FROM (now() - last_crawled_at)) / 3600 AS age_hours
           FROM courses WHERE student_id = %s ORDER BY last_crawled_at NULLS FIRST""",
        (student_id,),
    )
    courses = []
    for r in rows:
        age = None if r["age_hours"] is None else round(float(r["age_hours"]), 1)
        courses.append(
            {
                "course": r["code"],
                "last_crawled_at": r["last_crawled_at"],
                "age_hours": age,
                "stale": age is None or age > 24,
            }
        )
    return {
        "courses": courses,
        "any_stale": any(c["stale"] for c in courses) or not courses,
        "never_crawled": not courses,
    }


def get_workload_history(student_id: str = DEFAULT_STUDENT, days: int = 30) -> list[dict]:
    """
    The Timescale query. time_bucket() rounds each timestamp down to the start
    of its day, so grouping by it gives you one point per course per day --
    exactly what a trend chart needs.
    """
    return [
        dict(r)
        for r in query(
            """SELECT time_bucket('1 day', observed_at) AS day,
                      course_code,
                      max(total_est_hours)  AS est_hours,
                      max(open_assignments) AS open_count
               FROM workload_snapshots
               WHERE student_id = %s
                 AND observed_at > now() - %s * interval '1 day'
               GROUP BY day, course_code
               ORDER BY day, course_code""",
            (student_id, int(days)),
        )
    ]


# --------------------------------------------------------------------------
# WRITES  (each one validates before touching the database)
# --------------------------------------------------------------------------


def upsert_courses(student_id: str, items: list[dict], source: str | None = None) -> dict:
    """
    "Upsert" = insert if new, update if it already exists. Postgres does it in
    one statement with ON CONFLICT.
    """
    written = 0
    for item in items or []:
        code = _text(item.get("code"), "code", required=True, limit=40)
        cid = make_id(student_id, code)
        query(
            """INSERT INTO courses
                   (id, student_id, code, title, instructor, canvas_id, source,
                    last_crawled_at, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s, now(), now())
               ON CONFLICT (id) DO UPDATE SET
                   title           = COALESCE(EXCLUDED.title, courses.title),
                   instructor      = COALESCE(EXCLUDED.instructor, courses.instructor),
                   canvas_id       = COALESCE(EXCLUDED.canvas_id, courses.canvas_id),
                   source          = COALESCE(EXCLUDED.source, courses.source),
                   last_crawled_at = now(),
                   updated_at      = now()""",
            (
                cid,
                student_id,
                code,
                _text(item.get("title"), "title"),
                _text(item.get("instructor"), "instructor", limit=200),
                _text(item.get("canvas_id"), "canvas_id", limit=64),
                source or _text(item.get("source"), "source"),
            ),
            fetch="none",
        )
        written += 1
    return {"courses_written": written}


def upsert_assignments(student_id: str, items: list[dict]) -> dict:
    """
    The main write path. Anything the crawler finds lands here.

    Re-crawling the same page updates rows rather than duplicating them,
    because the id is derived from course + title. That is how "update
    outdated information" works in practice.
    """
    written, skipped, errors = 0, 0, []

    for raw in items or []:
        try:
            course = _text(raw.get("course") or raw.get("course_code"),
                           "course", required=True, limit=40)
            title = _text(raw.get("title"), "title", required=True, limit=300)
            kind = (_text(raw.get("kind") or raw.get("type"), "kind") or "other").lower()
            if kind not in _KINDS:
                kind = "other"
            status = (_text(raw.get("status"), "status") or "open").lower()
            if status not in _STATUSES:
                status = "open"

            aid = make_id(student_id, course, title)
            query(
                """INSERT INTO assignments
                       (id, student_id, course_code, title, kind, due_at, points,
                        est_hours, est_source, priority, status, description,
                        source_url, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (id) DO UPDATE SET
                       kind        = EXCLUDED.kind,
                       due_at      = COALESCE(EXCLUDED.due_at, assignments.due_at),
                       points      = COALESCE(EXCLUDED.points, assignments.points),
                       est_hours   = COALESCE(EXCLUDED.est_hours, assignments.est_hours),
                       est_source  = COALESCE(EXCLUDED.est_source, assignments.est_source),
                       priority    = COALESCE(EXCLUDED.priority, assignments.priority),
                       status      = EXCLUDED.status,
                       description = COALESCE(EXCLUDED.description, assignments.description),
                       source_url  = COALESCE(EXCLUDED.source_url, assignments.source_url),
                       updated_at  = now()""",
                (
                    aid, student_id, course, title, kind,
                    _as_datetime(raw.get("due_at") or raw.get("due"), "due_at"),
                    _as_number(raw.get("points"), "points"),
                    _as_number(raw.get("est_hours"), "est_hours"),
                    _text(raw.get("est_source"), "est_source", limit=20) or "claude",
                    _as_int(raw.get("priority"), "priority"),
                    status,
                    _text(raw.get("description"), "description", limit=4000),
                    _text(raw.get("source_url"), "source_url", limit=500),
                ),
                fetch="none",
            )
            written += 1
        except ValidationError as exc:
            skipped += 1
            if len(errors) < 5:
                errors.append(str(exc))

    out = {"assignments_written": written, "skipped": skipped}
    if errors:
        out["validation_errors"] = errors
    return out


def save_schedule(student_id: str, blocks: list[dict], rationale: str = "") -> dict:
    """
    Saves a whole schedule as one generation. Old generations stay in the table
    so you can show "before / after Claude re-planned it".
    """
    gen = uuid.uuid4().hex[:12]
    written, errors = 0, []
    for b in blocks or []:
        try:
            starts = _as_datetime(b.get("starts_at") or b.get("start"), "starts_at")
            if starts is None:
                raise ValidationError("starts_at is required")
            query(
                """INSERT INTO schedule_blocks
                       (student_id, assignment_id, task, starts_at, ends_at,
                        est_minutes, priority, generation_id, rationale)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    student_id,
                    _text(b.get("assignment_id"), "assignment_id", limit=40),
                    _text(b.get("task"), "task", required=True, limit=300),
                    starts,
                    _as_datetime(b.get("ends_at") or b.get("end"), "ends_at"),
                    _as_int(b.get("est_minutes"), "est_minutes"),
                    _as_int(b.get("priority"), "priority"),
                    gen,
                    _text(rationale, "rationale", limit=2000),
                ),
                fetch="none",
            )
            written += 1
        except ValidationError as exc:
            if len(errors) < 5:
                errors.append(str(exc))

    out = {"blocks_written": written, "generation_id": gen}
    if errors:
        out["validation_errors"] = errors
    return out


def save_study_set(student_id: str, course: str, topic: str,
                   format: str, content: dict) -> dict:  # noqa: A002
    row = query(
        """INSERT INTO study_sets (student_id, course_code, topic, format, content)
           VALUES (%s,%s,%s,%s,%s) RETURNING id""",
        (
            student_id,
            _text(course, "course", limit=40),
            _text(topic, "topic", limit=200),
            _text(format, "format", limit=40),
            json.dumps(content or {}),
        ),
        fetch="one",
    )
    return {"study_set_id": row["id"]}


def upsert_events(items: list[dict], source: str = "manual") -> dict:
    written = 0
    for e in items or []:
        title = _text(e.get("title"), "title", required=True, limit=300)
        eid = make_id(title, str(e.get("starts_at") or e.get("when") or ""))
        query(
            """INSERT INTO campus_events
                   (id, title, starts_at, location, url, tags, source, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s, now())
               ON CONFLICT (id) DO UPDATE SET
                   starts_at  = COALESCE(EXCLUDED.starts_at, campus_events.starts_at),
                   location   = COALESCE(EXCLUDED.location, campus_events.location),
                   url        = COALESCE(EXCLUDED.url, campus_events.url),
                   tags       = COALESCE(EXCLUDED.tags, campus_events.tags),
                   updated_at = now()""",
            (
                eid, title,
                _as_datetime(e.get("starts_at") or e.get("when"), "starts_at"),
                _text(e.get("location") or e.get("where"), "location", limit=200),
                _text(e.get("url"), "url", limit=500),
                [str(t)[:40] for t in (e.get("tags") or [])],
                source,
            ),
            fetch="none",
        )
        written += 1
    return {"events_written": written}


def update_profile(student_id: str, patch: dict) -> dict:
    """
    Updates the student record. Only these fields can be changed, and
    'password' is deliberately not one of them -- anything else the caller
    sends goes into the prefs JSON blob instead.
    """
    allowed = {"display_name", "email", "timezone", "canvas_base"}

    sets, params = [], []
    for field in allowed & set(patch or {}):
        sets.append(f"{field} = %s")
        params.append(_text(patch[field], field, limit=300))

    # Second layer of the same check that tools.py does.
    from tools import is_banned

    prefs = {k: v for k, v in (patch or {}).items()
             if k not in allowed and not is_banned(k)}
    rejected = sorted(k for k in (patch or {}) if is_banned(k))

    if prefs:
        sets.append("prefs = students.prefs || %s::jsonb")
        params.append(json.dumps(prefs))

    if not sets:
        return {"updated": False, "rejected_fields": rejected}

    params.append(student_id)
    query(
        f"UPDATE students SET {', '.join(sets)}, updated_at = now() WHERE id = %s",
        tuple(params),
        fetch="none",
    )
    out = {"updated": True, "prefs_keys": sorted(prefs)}
    if rejected:
        out["rejected_fields"] = rejected
        out["note"] = "Credentials are never stored. Use a scoped Canvas token instead."
    return out


def log_study_session(student_id: str, assignment_id: str | None,
                      minutes: int, started_at: Any = None) -> dict:
    query(
        """INSERT INTO study_sessions (started_at, student_id, assignment_id, minutes, source)
           VALUES (COALESCE(%s, now()), %s, %s, %s, 'user')""",
        (_as_datetime(started_at, "started_at"), student_id,
         _text(assignment_id, "assignment_id", limit=40),
         _as_int(minutes, "minutes")),
        fetch="none",
    )
    return {"logged": True}


def record_workload_snapshot(student_id: str = DEFAULT_STUDENT) -> dict:
    """
    Writes one history row per course, computed from the current assignments.
    Call this after every crawl. This is what fills the time-series chart.
    """
    query(
        """INSERT INTO workload_snapshots
               (observed_at, student_id, course_code, open_assignments,
                total_est_hours, nearest_due)
           SELECT now(), student_id, course_code,
                  count(*), COALESCE(sum(est_hours), 0), min(due_at)
           FROM assignments
           WHERE student_id = %s AND status = 'open'
           GROUP BY student_id, course_code""",
        (student_id,),
        fetch="none",
    )
    return {"snapshot_recorded": True}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def init(student_id: str = DEFAULT_STUDENT) -> None:
    sql = (Path(__file__).parent / "schema.sql").read_text()
    with pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    query(
        """INSERT INTO students (id, display_name, email, canvas_base, auth_token)
           VALUES (%s, 'Demo Student', 'demo@monstate.edu', 'canvas_pages',
                   'fake-token-for-demo')
           ON CONFLICT (id) DO NOTHING""",
        (student_id,),
        fetch="none",
    )
    print(f"schema created, student '{student_id}' ready")


def seed(student_id: str = DEFAULT_STUDENT) -> None:
    """Fake data so you can test reads without running the crawler."""
    now = datetime.now(timezone.utc)
    upsert_courses(student_id, [
        {"code": "PHYS 1361", "title": "Electricity and Magnetism",
         "instructor": "Dr. Ramirez", "canvas_id": "1103"},
        {"code": "CS 1675", "title": "Intro to Machine Learning",
         "instructor": "Dr. Chen", "canvas_id": "2204"},
    ], source="seed")
    print(upsert_assignments(student_id, [
        {"course": "PHYS 1361", "title": "Problem Set 4", "kind": "homework",
         "due_at": (now + timedelta(days=3)).isoformat(), "points": 50, "est_hours": 2.5},
        {"course": "PHYS 1361", "title": "Quiz 3 (Ch. 2)", "kind": "quiz",
         "due_at": (now + timedelta(days=2)).isoformat(), "points": 25, "est_hours": 1.5},
        {"course": "CS 1675", "title": "Lab 3: cross-validation", "kind": "lab",
         "due_at": (now + timedelta(days=4)).isoformat(), "points": 20, "est_hours": 1},
        {"course": "CS 1675", "title": "Midterm Project proposal", "kind": "project",
         "due_at": (now + timedelta(days=7)).isoformat(), "points": 100, "est_hours": 4},
    ]))
    upsert_events([
        {"title": "SCI Career Fair", "starts_at": (now + timedelta(days=5)).isoformat(),
         "location": "Alumni Hall", "tags": ["career"]},
    ], source="seed")
    record_workload_snapshot(student_id)
    print("seeded")


def check() -> None:
    tables = ["students", "courses", "assignments", "schedule_blocks",
              "study_sets", "campus_events", "workload_snapshots", "study_sessions"]
    print(f"connected to {DATABASE_URL.split('@')[-1].split('/')[0]}\n")
    for t in tables:
        try:
            row = query(f"SELECT count(*) AS n FROM {t}", fetch="one")
            print(f"  {t:22} {row['n']:>6} rows")
        except Exception as exc:  # noqa: BLE001
            print(f"  {t:22} ERROR: {str(exc)[:70]}")
    hyper = query(
        "SELECT hypertable_name FROM timescaledb_information.hypertables", fetch="all"
    )
    print(f"\n  hypertables: {[h['hypertable_name'] for h in hyper] or 'NONE - check schema.sql ran'}")


def reset() -> None:
    if input("This DROPS every table. Type 'yes' to continue: ").strip() != "yes":
        print("cancelled")
        return
    for t in ["study_sessions", "workload_snapshots", "campus_events", "study_sets",
              "schedule_blocks", "assignments", "courses", "students"]:
        query(f"DROP TABLE IF EXISTS {t} CASCADE", fetch="none")
    print("dropped")
    init()


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--init" in args:
        init()
    elif "--seed" in args:
        seed()
    elif "--check" in args:
        check()
    elif "--reset" in args:
        reset()
    else:
        print(__doc__)
