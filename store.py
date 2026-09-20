"""
Persistence for the website: accounts, the board, the file library, and the
agent's HTML surface.

WHERE THIS CAME FROM
--------------------
This is a Python port of Rowan's store.js / store.pg.js (now kept for
reference in legacy/node/). The *interface* is deliberately unchanged --
get_board, upsert_items, add_item, update_item, remove_item, get_library,
set_library -- because app.html is written against it and Rowan's semantics
(upsert on canvasId, per-user boards, file store that falls back to Postgres)
were the right ones. What changed is the language, so the whole project runs
as one process on one runtime instead of a Node tier calling a Python tier
over HTTP.

Two things were added that the JS version didn't have:
  - surface chunks (see surface.py): the HTML the agent has put on the page.
  - student_id: every account maps 1:1 onto a Tiger Data `students` row, so
    two accounts don't read each other's assignments out of the orchestrator.

PASSWORD HASHING IS BYTE-COMPATIBLE WITH THE JS VERSION
-------------------------------------------------------
Node's crypto.scryptSync(password, salt, 64) uses N=16384, r=8, p=1, and it
hashes the salt as the *hex string* it is, not as the bytes that string
decodes to. hashlib.scrypt with the same parameters and salt.encode() (not
bytes.fromhex) reproduces it exactly, so an existing data.json written by
store.js still logs in against this file. Worth the five minutes: the
alternative is silently locking everyone out of accounts they already made.

CREDENTIALS WE DO NOT STORE
---------------------------
Account passwords: hashed here, never stored in plaintext, never logged.
Canvas passwords / 2FA codes: not accepted at all, anywhere. The crawler
reads an export or a scoped access token instead (see orchestrator/canvas.py
and the BANNED_WORDS filter in orchestrator/tools.py). The old chat mock
asked for a Canvas username and a Duo push; that flow is gone.

TWO BACKENDS
------------
    file        JSON on disk. The default. No database needed, so the whole
                site runs with zero configuration.
    postgres    Used automatically when DATABASE_URL points at something
                real. Same Tiger Data instance the orchestrator uses, via
                the same psycopg pool in orchestrator/db.py -- one driver,
                one pool, one place to fix a connection bug.

Force one with STORE_BACKEND=file|postgres.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KINDS: tuple[str, ...] = ("assignments", "exams", "events", "todos")

# Vercel's filesystem is read-only except /tmp, and /tmp does not survive
# between invocations. So on Vercel the file store is a per-instance scratch
# pad and DATABASE_URL is what makes data persist. Said plainly in /api/health
# rather than left as a surprise.
ON_VERCEL = bool(os.getenv("VERCEL") or os.getenv("VERCEL_ENV"))
_DEFAULT_STORE = "/tmp/officehours-data.json" if ON_VERCEL else "data.json"
STORE_FILE = Path(os.getenv("STORE_FILE", _DEFAULT_STORE))

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN = 16384, 8, 1, 64
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))

_USERNAME_OK = re.compile(r"^[A-Za-z0-9._-]{2,40}$")


class StoreError(RuntimeError):
    """Something the caller did wrong, with a `code` the API turns into JSON."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# Backend selection
# --------------------------------------------------------------------------

_backend: str | None = None
_degraded: str | None = None   # why we fell back off Postgres, if we did


def backend() -> str:
    """
    'postgres' or 'file'. Decided once, then cached.

    Three things have to be true to use Postgres, and each was a real failure
    we hit rather than a hypothetical:
      1. DATABASE_URL is set and isn't the .env.example placeholder.
      2. psycopg can actually load. A venv built on another machine, or a
         host without libpq, imports fine and then raises "no pq wrapper
         available" on first use -- which otherwise surfaced as every single
         request 500ing with a stack trace about a missing wrapper.
      3. init() can reach the server (checked there, not here, because that
         one costs a round trip).
    Any of them failing means the file store, a note in /api/health, and a
    site that still works.
    """
    global _backend
    if _backend is not None:
        return _backend

    forced = os.getenv("STORE_BACKEND", "").strip().lower()
    if forced in ("file", "postgres"):
        _backend = forced
        return _backend

    url = os.getenv("DATABASE_URL", "")
    if not url:
        _backend = "file"
        return _backend

    try:
        # db.check_url() validates the string WITHOUT connecting, so a
        # placeholder or a typo doesn't cost a 12-second timeout on the first
        # page load.
        import db  # orchestrator/db.py, on sys.path via app.py

        problem = db.check_url(url)
        if problem:
            _degrade(problem.splitlines()[0])
            return _backend  # type: ignore[return-value]

        import psycopg  # noqa: F401  - the check that matters; see (2) above

        _backend = "postgres"
    except Exception as exc:  # noqa: BLE001
        _degrade(f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
    return _backend  # type: ignore[return-value]


def _degrade(reason: str) -> None:
    """Fall back to the file store, once, loudly."""
    global _backend, _degraded
    if _backend != "file":
        print(f"[store] using the file store instead of Postgres: {reason}")
    _backend = "file"
    _degraded = reason


def status() -> dict:
    """For /api/health."""
    info: dict[str, Any] = {"backend": backend()}
    if _degraded:
        info["postgres_unavailable"] = _degraded
    if backend() == "file":
        info["file"] = str(STORE_FILE)
        info["ephemeral"] = ON_VERCEL
        if ON_VERCEL:
            info["note"] = (
                "On Vercel the file store lives in /tmp and is wiped between "
                "invocations. Set DATABASE_URL for data that persists."
            )
    return info


# --------------------------------------------------------------------------
# File backend
# --------------------------------------------------------------------------

_EMPTY_DB = {"users": {}, "sessions": {}, "boards": {}, "libraries": {},
             "surfaces": {}}


def _load_file() -> dict:
    try:
        data = json.loads(STORE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return json.loads(json.dumps(_EMPTY_DB))
    if not isinstance(data, dict):
        return json.loads(json.dumps(_EMPTY_DB))
    for key, default in _EMPTY_DB.items():
        data.setdefault(key, json.loads(json.dumps(default)))
    return data


def _save_file(data: dict) -> None:
    try:
        STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file and move it into place, so a crash halfway
        # through a write can't leave a half-written JSON file that then
        # fails to parse and looks like "all my data vanished".
        tmp = STORE_FILE.with_suffix(STORE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(STORE_FILE)
    except OSError as exc:
        print(f"[store] save failed ({STORE_FILE}): {exc}")


# --------------------------------------------------------------------------
# Postgres backend helpers
# --------------------------------------------------------------------------


def _pg():
    import db

    return db


def init() -> dict:
    """
    Create whatever the chosen backend needs. Safe to call on every boot.

    This is the one place that actually talks to the database at startup, so
    it's where an unreachable server gets caught. Failing here degrades to the
    file store rather than raising: a paused Timescale instance during a demo
    should cost you persistence, not the whole website.
    """
    if backend() == "postgres":
        try:
            db = _pg()
            schema = (Path(__file__).parent / "orchestrator" / "schema.sql").read_text()
            with db.pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(schema)
            return {"backend": "postgres", "tables": "ready"}
        except Exception as exc:  # noqa: BLE001
            _degrade(f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}")

    if not STORE_FILE.exists():
        _save_file(_load_file())
    return {"backend": "file", "file": str(STORE_FILE),
            "postgres_unavailable": _degraded}


# --------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------


def _gen_id(prefix: str) -> str:
    return f"{prefix}{int(time.time() * 1000):x}{secrets.token_hex(2)}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def _hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(
        str(password).encode(),
        salt=salt.encode(),      # the hex STRING, matching Node. See module docstring.
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
        maxmem=64 * 1024 * 1024,
    )
    return salt, digest.hex()


def _verify_password(password: str, salt: str, expected: str) -> bool:
    _, got = _hash_password(password, salt)
    return secrets.compare_digest(got, expected)


def student_id_for(user_id: str) -> str:
    """
    The Tiger Data student key for an account.

    Kept separate from user_id so the orchestrator's rows are namespaced by
    account: two people signing up don't end up reading each other's
    assignments out of the shared `assignments` table.
    """
    return f"acct-{user_id}"


def create_user(username: str, password: str) -> dict:
    name = str(username or "").strip()
    if not _USERNAME_OK.match(name):
        raise StoreError("invalid", "Username must be 2-40 letters, numbers, dot, dash or underscore.")
    if len(str(password or "")) < 4:
        raise StoreError("weak", "Password needs at least 4 characters.")

    key = name.lower()
    salt, digest = _hash_password(password)
    user_id = _gen_id("u")

    if backend() == "file":
        data = _load_file()
        if key in data["users"]:
            raise StoreError("exists", "That username is taken.")
        data["users"][key] = {"id": user_id, "username": name, "salt": salt,
                              "hash": digest, "created_at": _now()}
        data["boards"][user_id] = _empty_board()
        data["libraries"][user_id] = {"collections": [], "files": []}
        data["surfaces"][user_id] = []
        _save_file(data)
    else:
        db = _pg()
        existing = db.query("SELECT 1 FROM app_users WHERE username_key = %s",
                            (key,), fetch="one")
        if existing:
            raise StoreError("exists", "That username is taken.")
        db.query(
            """INSERT INTO app_users (id, username_key, username, salt, hash)
               VALUES (%s, %s, %s, %s, %s)""",
            (user_id, key, name, salt, digest), fetch="none",
        )

    _ensure_student(user_id, name)
    return {"id": user_id, "username": name}


def _ensure_student(user_id: str, display_name: str) -> None:
    """Give this account a Tiger Data students row, if we have a database."""
    if backend() != "postgres":
        return
    try:
        _pg().ensure_student(student_id_for(user_id), display_name)
    except Exception as exc:  # noqa: BLE001
        # Not fatal: the account still works, the orchestrator just falls back
        # to whatever STUDENT_ID says until the row exists.
        print(f"[store] could not create students row: {exc}")


def verify_user(username: str, password: str) -> dict | None:
    key = str(username or "").strip().lower()
    if not key:
        return None

    if backend() == "file":
        row = _load_file()["users"].get(key)
    else:
        row = _pg().query(
            "SELECT id, username, salt, hash FROM app_users WHERE username_key = %s",
            (key,), fetch="one",
        )
    if not row or not _verify_password(password, row["salt"], row["hash"]):
        return None
    return {"id": row["id"], "username": row["username"]}


def create_session(user_id: str) -> str:
    token = secrets.token_hex(24)
    if backend() == "file":
        data = _load_file()
        data["sessions"][token] = {"user_id": user_id, "created_at": _now()}
        _save_file(data)
    else:
        _pg().query("INSERT INTO app_sessions (token, user_id) VALUES (%s, %s)",
                    (token, user_id), fetch="none")
    return token


def user_by_token(token: str) -> dict | None:
    """Returns {id, username} or None. Expired sessions count as None."""
    if not token:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=SESSION_TTL_DAYS)

    if backend() == "file":
        data = _load_file()
        session = data["sessions"].get(token)
        if not session:
            return None
        created = session.get("created_at")
        if created:
            try:
                if datetime.fromisoformat(created) < cutoff:
                    del data["sessions"][token]
                    _save_file(data)
                    return None
            except ValueError:
                pass
        user_id = session["user_id"]
        for row in data["users"].values():
            if row["id"] == user_id:
                return {"id": user_id, "username": row["username"]}
        return None

    row = _pg().query(
        """SELECT u.id, u.username FROM app_sessions s
           JOIN app_users u ON u.id = s.user_id
           WHERE s.token = %s AND s.created_at > %s""",
        (token, cutoff), fetch="one",
    )
    return {"id": row["id"], "username": row["username"]} if row else None


def delete_session(token: str) -> bool:
    if backend() == "file":
        data = _load_file()
        if token in data["sessions"]:
            del data["sessions"][token]
            _save_file(data)
    else:
        _pg().query("DELETE FROM app_sessions WHERE token = %s", (token,), fetch="none")
    return True


# --------------------------------------------------------------------------
# The board
# --------------------------------------------------------------------------


def _empty_board() -> dict:
    return {kind: [] for kind in KINDS}


def _clean_item(kind: str, raw: dict) -> dict:
    """
    Keep only fields the frontend knows how to display, and cap their sizes.

    This is the same argument display.py makes about cards: the agent writes
    here, so a confused model should at worst produce a row with a silly
    title -- never an unbounded blob, and never a key the renderer will hand
    to innerHTML.
    """
    allowed = {
        "id", "title", "course", "dueISO", "scheduledISO", "estimateMins",
        "location", "url", "canvasId", "source", "completed", "attachments",
        "text", "done", "createdISO", "completedISO", "startISO", "note",
    }
    out: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        if key not in allowed:
            continue
        if isinstance(value, str):
            out[key] = value[:2000]
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        elif key == "attachments" and isinstance(value, list):
            out[key] = value[:20]
        elif isinstance(value, (list, dict)):
            out[key] = value
    out.setdefault("source", "manual")
    out.setdefault("completed", False)
    if kind == "todos":
        out.setdefault("done", False)
    return out


def get_board(user_id: str) -> dict:
    if backend() == "file":
        board = _load_file()["boards"].get(user_id)
        return board if board else _empty_board()

    rows = _pg().query(
        "SELECT kind, data FROM app_items WHERE user_id = %s ORDER BY created_at",
        (user_id,),
    )
    board = _empty_board()
    for row in rows:
        kind = row["kind"]
        if kind in board:
            board[kind].append(row["data"])
    return board


def _save_board(user_id: str, board: dict) -> None:
    data = _load_file()
    data["boards"][user_id] = board
    _save_file(data)


def add_item(user_id: str, kind: str, item: dict) -> dict:
    if kind not in KINDS:
        raise StoreError("bad_kind", f"Unknown kind '{kind}'.")
    clean = _clean_item(kind, item)
    clean["id"] = clean.get("id") or _gen_id(kind[0])

    if backend() == "file":
        board = get_board(user_id)
        board[kind].append(clean)
        _save_board(user_id, board)
    else:
        _pg().query(
            """INSERT INTO app_items (id, user_id, kind, canvas_id, data)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data,
                                              updated_at = now()""",
            (clean["id"], user_id, kind, clean.get("canvasId"),
             json.dumps(clean, default=str)), fetch="none",
        )
    return clean


def upsert_items(user_id: str, kind: str, items: list[dict]) -> list[dict]:
    """
    Add items, updating in place when we've seen them before.

    Matching is by canvasId first, then id -- Rowan's rule, kept as-is. It is
    what makes "sync from Canvas" idempotent: run the crawler five times and
    the board still has one copy of each assignment.

    Returns only the rows that were newly ADDED, which is what the chat uses
    to say "added 3 assignments" honestly instead of counting updates too.
    """
    if kind not in KINDS:
        raise StoreError("bad_kind", f"Unknown kind '{kind}'.")

    board = get_board(user_id)
    existing = board[kind]
    added: list[dict] = []

    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        clean = _clean_item(kind, raw)
        clean.setdefault("source", "canvas")

        index = -1
        if clean.get("canvasId"):
            index = next((i for i, row in enumerate(existing)
                          if row.get("canvasId") == clean["canvasId"]), -1)
        if index < 0 and clean.get("id"):
            index = next((i for i, row in enumerate(existing)
                          if row.get("id") == clean["id"]), -1)

        if index >= 0:
            keep_id = existing[index].get("id")
            # Don't let a re-sync silently un-complete something the student
            # already ticked off.
            keep_done = existing[index].get("completed")
            merged = {**existing[index], **clean, "id": keep_id}
            if keep_done and clean.get("completed") is False:
                merged["completed"] = True
            existing[index] = merged
            _write_item(user_id, kind, merged)
        else:
            clean["id"] = clean.get("id") or _gen_id(kind[0])
            existing.append(clean)
            added.append(clean)
            _write_item(user_id, kind, clean)

    if backend() == "file":
        _save_board(user_id, board)
    return added


def _write_item(user_id: str, kind: str, item: dict) -> None:
    if backend() == "file":
        return  # the caller saves the whole board once
    _pg().query(
        """INSERT INTO app_items (id, user_id, kind, canvas_id, data)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data,
                                          canvas_id = EXCLUDED.canvas_id,
                                          updated_at = now()""",
        (item["id"], user_id, kind, item.get("canvasId"),
         json.dumps(item, default=str)), fetch="none",
    )


def update_item(user_id: str, kind: str, item_id: str, patch: dict) -> dict | None:
    if kind not in KINDS:
        raise StoreError("bad_kind", f"Unknown kind '{kind}'.")
    board = get_board(user_id)
    for index, row in enumerate(board[kind]):
        if row.get("id") == item_id:
            merged = {**row, **_clean_item(kind, patch), "id": item_id}
            board[kind][index] = merged
            if backend() == "file":
                _save_board(user_id, board)
            else:
                _write_item(user_id, kind, merged)
            return merged
    return None


def remove_item(user_id: str, kind: str, item_id: str) -> bool:
    if kind not in KINDS:
        raise StoreError("bad_kind", f"Unknown kind '{kind}'.")
    if backend() == "file":
        board = get_board(user_id)
        before = len(board[kind])
        board[kind] = [row for row in board[kind] if row.get("id") != item_id]
        _save_board(user_id, board)
        return len(board[kind]) < before

    row = _pg().query(
        "DELETE FROM app_items WHERE user_id = %s AND kind = %s AND id = %s RETURNING id",
        (user_id, kind, item_id), fetch="one",
    )
    return bool(row)


def complete_items(user_id: str, items: list[dict]) -> int:
    """
    Tick off board rows the agent marked submitted, graded or dismissed.

    Matches on canvasId first (the Tiger Data assignment id), then falls back
    to an exact title match, because the board row may predate the id -- an
    item added by hand, or crawled before the deterministic ids existed.

    Searches every column: one Tiger Data assignment row shows up as an
    assignment or an exam depending on its kind, and the caller shouldn't
    have to know which.
    """
    board = get_board(user_id)
    ticked = 0

    wanted_ids = {str(i.get("canvasId")) for i in items if i.get("canvasId")}
    wanted_titles = {str(i.get("title", "")).strip().lower()
                     for i in items if i.get("title")}

    for kind in KINDS:
        for index, row in enumerate(board[kind]):
            match = (str(row.get("canvasId")) in wanted_ids
                     or str(row.get("title", "")).strip().lower() in wanted_titles)
            if not match or row.get("completed"):
                continue
            updated = {**row, "completed": True}
            if kind == "todos":
                updated["done"] = True
            note = next((i.get("note") for i in items
                         if str(i.get("canvasId")) == str(row.get("canvasId"))
                         or str(i.get("title", "")).strip().lower()
                         == str(row.get("title", "")).strip().lower()), None)
            if note:
                updated["note"] = str(note)[:200]
            board[kind][index] = updated
            _write_item(user_id, kind, updated)
            ticked += 1

    if backend() == "file":
        _save_board(user_id, board)
    return ticked


def clear(user_id: str) -> dict:
    if backend() == "file":
        _save_board(user_id, _empty_board())
    else:
        _pg().query("DELETE FROM app_items WHERE user_id = %s", (user_id,), fetch="none")
    return get_board(user_id)


def load_demo(user_id: str) -> dict:
    """
    Seed one account with believable data.

    Same rows Rowan's seedBoard() used, so a demo recorded against the Node
    backend still looks the same.
    """
    def days(n: int) -> str:
        moment = datetime.now(timezone.utc).astimezone() + timedelta(days=n)
        return moment.replace(hour=9, minute=0, second=0, microsecond=0).isoformat()

    clear(user_id)
    upsert_items(user_id, "assignments", [
        {"title": "Problem Set 5: Hermitian operators", "course": "PHYS 1370 — Quantum",
         "dueISO": days(0), "estimateMins": 120, "source": "canvas", "canvasId": "demo-a1"},
        {"title": "Lab writeup: Gauss-Jordan inverse", "course": "CS 1550 — Systems",
         "dueISO": days(2), "estimateMins": 90, "source": "canvas", "canvasId": "demo-a2"},
        {"title": "Reading response, Ch. 3", "course": "CS 1501 — Algorithms",
         "dueISO": days(5), "source": "canvas", "canvasId": "demo-a3"},
    ])
    upsert_items(user_id, "exams", [
        {"title": "Midterm 1", "course": "CS 1501 — Algorithms", "dueISO": days(6),
         "location": "Lawrence 106", "estimateMins": 180, "source": "canvas",
         "canvasId": "demo-x1"},
        {"title": "Quiz 2", "course": "PHYS 1370 — Quantum", "dueISO": days(3),
         "location": "Thaw 102", "source": "canvas", "canvasId": "demo-x2"},
    ])
    upsert_items(user_id, "events", [
        {"title": "HackPitt kickoff", "location": "Cathedral of Learning",
         "startISO": days(1), "source": "canvas", "canvasId": "demo-e1"},
        {"title": "Office hours: Dr. Reyes", "location": "Sennott Sq. 6203",
         "startISO": days(3), "source": "canvas", "canvasId": "demo-e2"},
    ])
    return get_board(user_id)


# --------------------------------------------------------------------------
# Library (the Files tab)
# --------------------------------------------------------------------------


def get_library(user_id: str) -> dict:
    if backend() == "file":
        return _load_file()["libraries"].get(user_id) or {"collections": [], "files": []}
    row = _pg().query("SELECT data FROM app_library WHERE user_id = %s",
                      (user_id,), fetch="one")
    return row["data"] if row else {"collections": [], "files": []}


def set_library(user_id: str, library: dict) -> dict:
    clean = {
        "collections": (library or {}).get("collections") or [],
        "files": (library or {}).get("files") or [],
    }
    if backend() == "file":
        data = _load_file()
        data["libraries"][user_id] = clean
        _save_file(data)
    else:
        _pg().query(
            """INSERT INTO app_library (user_id, data) VALUES (%s, %s)
               ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data""",
            (user_id, json.dumps(clean, default=str)), fetch="none",
        )
    return clean


# --------------------------------------------------------------------------
# Surface chunks (see surface.py for what these are)
# --------------------------------------------------------------------------


def get_surface(user_id: str) -> list[dict]:
    if backend() == "file":
        return _load_file()["surfaces"].get(user_id) or []
    rows = _pg().query(
        """SELECT id, kind, title, html, source, position, updated_at
           FROM app_surface WHERE user_id = %s ORDER BY position, updated_at""",
        (user_id,),
    )
    return rows or []


def set_surface(user_id: str, chunks: list[dict]) -> list[dict]:
    chunks = chunks or []
    if backend() == "file":
        data = _load_file()
        data["surfaces"][user_id] = chunks
        _save_file(data)
        return chunks

    db = _pg()
    keep = [chunk["id"] for chunk in chunks]
    if keep:
        db.query(
            "DELETE FROM app_surface WHERE user_id = %s AND id <> ALL(%s)",
            (user_id, keep), fetch="none",
        )
    else:
        db.query("DELETE FROM app_surface WHERE user_id = %s", (user_id,), fetch="none")
    for position, chunk in enumerate(chunks):
        db.query(
            """INSERT INTO app_surface
                   (user_id, id, position, kind, title, html, source, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, now())
               ON CONFLICT (user_id, id) DO UPDATE
                   SET position = EXCLUDED.position, kind = EXCLUDED.kind,
                       title = EXCLUDED.title, html = EXCLUDED.html,
                       source = EXCLUDED.source, updated_at = now()""",
            (user_id, chunk["id"], position, chunk.get("kind"),
             chunk.get("title"), chunk.get("html"), chunk.get("source", "agent")),
            fetch="none",
        )
    return get_surface(user_id)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).parent / "orchestrator"))
    args = set(sys.argv[1:])
    print(json.dumps(status(), indent=2))
    if "--init" in args:
        print(json.dumps(init(), indent=2))
