"""
Office Hours: the whole thing, in one process.

    python3 app.py          ->  http://localhost:5000

WHAT THIS FILE IS
-----------------
The single backend. It serves Kenneth's site (app.html), exposes the API that
site is written against, and runs the Nemotron/Claude/Tiger Data agent behind
the chat box. One process, one runtime, one deployment.

HOW IT GOT HERE
---------------
The project had two backends. A Node tier (server.js + store.js +
backend/assistant.js) held the accounts, the board, and the API app.html
talks to; a Python tier (orchestrator/) held every bit of the actual agent.
They had never been connected: the Node one couldn't even start, because
server.js sat at the repo root requiring ./assistant while assistant.js was
in backend/.

Rather than run both and put an HTTP hop between the website and the agent --
which is what caused the last integration bug, see the note about response
shapes drifting -- the Node tier's API contract was ported to Python:

    server.js        -> this file
    store.js         -> store.py          (same interface, same scrypt params)
    store.pg.js      -> store.py          (same tables, now in schema.sql)
    assistant.js     -> agent.py          (same {reply, actions} contract)

The originals are in legacy/node/ for reference; nothing imports them.

ROUTES
------
    GET  /                    the site (app.html)
    GET  /classic             the minimal card dashboard, kept as a fallback
    GET  /api/health          what's configured, which mode, which store

    POST /api/register        {username, password} -> {token, username}
    POST /api/login           same shape
    POST /api/logout

    GET  /api/board                        the four columns
    POST /api/<kind>                       add one item
    PATCH/DELETE /api/<kind>/<id>          edit / remove one item
    POST /api/demo | /api/clear            seed / empty the board

    GET/PUT /api/library      the Files tab
    GET  /api/files           student attachments, flattened
    GET  /api/files/<id>      raw bytes of one attachment

    POST /api/assistant       the chat box. The main one.
    POST /api/sync            crawl Canvas without going through the chat
    GET  /api/surface         the HTML the agent has put on the page
    PUT  /api/surface         replace it (used by the reset button)
    GET  /api/workload        Tiger Data time-series, for the trend chart

    POST /api/voice           plain-speech answer (fast model, read-only tools)
    POST /alexa               Alexa Custom Skill requests

EVERY ROUTE EXCEPT /, /classic, /api/health, /api/register AND /api/login
REQUIRES A SESSION TOKEN. The board is per-account.

MODE STILL CONTROLS COST
------------------------
MODE=mock (the default) makes no API calls at all, so the entire site --
accounts, board, chat, the HTML surface, Alexa -- runs end to end for $0.
MODE=live spends money and records what it spends it on. MODE=replay plays
those recordings back. See orchestrator/README.md.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Import path and working directory
#
# orchestrator/'s modules are written to run with that folder as the working
# directory: plain `import cache`, `import tools`, and a couple of relative
# paths ("./cache", "./canvas_pages") that assume it. That's how ask.py and
# server.py both run. Reproduce both halves of that here instead of patching
# every relative path inside orchestrator/.
#
# Absolute-ising argv[0] first: in debug mode Werkzeug restarts by re-running
# the script, and once we chdir into orchestrator/ a relative "app.py" would
# resolve to orchestrator/app.py, which doesn't exist. On Python 3.10+ it
# rebuilds the command from sys.orig_argv rather than sys.argv, so both need
# fixing or only the old fallback path gets it right.
# --------------------------------------------------------------------------

_abs_script = os.path.abspath(sys.argv[0])
sys.argv[0] = _abs_script
if len(getattr(sys, "orig_argv", [])) > 1:
    sys.orig_argv[1] = _abs_script

ROOT = Path(__file__).parent.resolve()
ORCH_DIR = ROOT / "orchestrator"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ORCH_DIR))

# On Vercel the only writable directory is /tmp, so the orchestrator's disk
# cache has to live there. Set before importing cache.py, which reads it at
# import time and creates the directory.
if os.getenv("VERCEL") or os.getenv("VERCEL_ENV"):
    os.environ.setdefault("CACHE_DIR", "/tmp/officehours-cache")

os.chdir(ORCH_DIR)

from flask import (Flask, Response, jsonify, redirect,  # noqa: E402
                   render_template, request, send_from_directory)

import cache        # noqa: E402  orchestrator/cache.py
import canvas       # noqa: E402
import config       # noqa: E402
import orchestrator  # noqa: E402
import tools        # noqa: E402
from nemotron_client import FAST_MODEL, ORCHESTRATOR_MODEL  # noqa: E402

import agent        # noqa: E402  repo root
import store        # noqa: E402
import surface      # noqa: E402

app = Flask(
    __name__,
    template_folder=str(ROOT / "templates"),
    static_folder=None,  # static files are served explicitly below
)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # attachments ride as base64

try:
    store.init()
except Exception as exc:  # noqa: BLE001
    # A database that's down shouldn't stop the site from booting: without it
    # we fall back to the file store, and /api/health says so.
    print(f"[app] store init failed, continuing: {exc}")


# --------------------------------------------------------------------------
# Auth plumbing
# --------------------------------------------------------------------------


def current_user() -> dict | None:
    """Read the bearer token app.html sends and resolve it to an account."""
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else request.headers.get("X-Token", "")
    if not token:
        return None
    try:
        user = store.user_by_token(token)
    except Exception as exc:  # noqa: BLE001
        print(f"[app] token lookup failed: {exc}")
        return None
    if user:
        user["token"] = token
    return user


def require_user():
    """
    Returns (user, None) or (None, error_response).

    Written as a value rather than a decorator so each route can read as one
    straight line, and so the 401 body is the same shape everywhere --
    app.html reads .message off failures to show in the login box.
    """
    user = current_user()
    if user is None:
        return None, (jsonify({"error": "unauthorized",
                               "message": "Sign in again."}), 401)
    return user, None


def with_student(user: dict):
    """
    Point the orchestrator at this account's Tiger Data rows for this request.

    tools.py reads a module-level STUDENT_ID from the environment, which is
    right for a single-user CLI and wrong for a website: without this, two
    accounts share one set of assignments. tools.use_student() scopes it per
    request instead.
    """
    return tools.use_student(store.student_id_for(user["id"]))


# --------------------------------------------------------------------------
# The site
# --------------------------------------------------------------------------


@app.route("/")
def home():
    """Kenneth's single-file site. Served as a static file, not a template."""
    return send_from_directory(ROOT, "app.html")


@app.route("/classic")
def classic():
    """
    The earlier minimal dashboard, kept deliberately.

    It renders the card contract directly with no accounts and no board, so
    it stays useful for two things: checking whether a problem is in the
    agent or in the big frontend, and demoing the pipeline on a laptop with
    nothing signed in.
    """
    return render_template("index.html")


@app.route("/assets/<path:filename>")
def assets(filename: str):
    return send_from_directory(ROOT / "assets", filename)


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


def _model_paths() -> dict:
    """What works without calling anything: package present, key present."""
    import importlib.util

    def probe(package: str, key: str) -> dict:
        installed = importlib.util.find_spec(package) is not None
        configured = bool(os.getenv(key, "").strip())
        state = "ready" if (installed and configured) else "unavailable"
        problems = []
        if not installed:
            problems.append(f"the {package} package is not installed "
                            f"(pip install -r requirements.txt)")
        if not configured:
            problems.append(f"{key} is not set")
        return {"state": state, "package_installed": installed,
                "key_configured": configured,
                "problems": problems or None}

    out = {
        "nemotron": probe("openai", "NVIDIA_API_KEY"),
        "claude": probe("anthropic", "ANTHROPIC_API_KEY"),
    }
    # Say plainly what still works when Claude is missing, so nobody spends
    # an evening assuming the whole agent is down.
    if out["claude"]["state"] != "ready":
        out["claude"]["degrades_to"] = (
            "Scheduling still works (scheduler.py places blocks in plain "
            "Python). Card layout and the HTML surface fall back to house "
            "templates. Canvas crawling works from a JSON export but not "
            "from raw HTML, and study guides are unavailable."
        )
    return out


@app.route("/api/health")
def health():
    """Hit this first. Says what's wired up without spending anything."""
    return jsonify({
        "ok": True,
        "mode": cache.MODE,
        "config": config.status(),
        "store": store.status(),
        "models": {"orchestrator": ORCHESTRATOR_MODEL, "fast": FAST_MODEL},
        "canvas": {"source": canvas.CANVAS_SOURCE, "pages": canvas.list_sources()},
        "surface": {"premade_card_types": surface.premade_types(),
                    "max_chunks": surface.MAX_CHUNKS},
        # Which optional model paths actually work right now.
        #
        # This block exists because a missing anthropic package took down
        # scheduling on a live deployment and the only symptom the student
        # ever saw was "internal error (missing dependency)". One GET would
        # have named it. Nothing here calls an API: it checks that the
        # package imports and the key is present, which is what silently
        # fails.
        "model_paths": _model_paths(),
        # The exact tool list this build hands to Nemotron. If a capability
        # seems missing, check here first: a tool absent from this list is
        # invisible to the model, and a tool present here but denied by the
        # model means the deployment is current and the model is wrong (which
        # agent._repair_denied_capability now catches for filing).
        "tools": sorted(t["function"]["name"] for t in tools.TOOL_SCHEMAS),
        "recorded_runs": cache.recorded_count(),
        "alexa": {"skill_id_enforced": bool(os.getenv("ALEXA_SKILL_ID", "").strip())},
    })


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------


@app.route("/api/register", methods=["POST"])
def register():
    body = request.get_json(silent=True) or {}
    try:
        user = store.create_user(body.get("username", ""), body.get("password", ""))
    except store.StoreError as exc:
        code = 409 if exc.code == "exists" else 400
        return jsonify({"error": exc.code, "message": exc.message}), code
    except Exception as exc:  # noqa: BLE001
        print(f"[app] register failed: {exc}")
        return jsonify({"error": "register_failed",
                        "message": "Could not create the account."}), 500

    token = store.create_session(user["id"])
    return jsonify({"token": token, "username": user["username"]}), 201


@app.route("/api/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    try:
        user = store.verify_user(body.get("username", ""), body.get("password", ""))
    except Exception as exc:  # noqa: BLE001
        print(f"[app] login failed: {exc}")
        return jsonify({"error": "login_failed",
                        "message": "Could not sign in."}), 500
    if not user:
        return jsonify({"error": "bad_credentials",
                        "message": "Wrong username or password."}), 401
    return jsonify({"token": store.create_session(user["id"]),
                    "username": user["username"]})


@app.route("/api/logout", methods=["POST"])
def logout():
    user, error = require_user()
    if error:
        return error
    store.delete_session(user["token"])
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# The board
# --------------------------------------------------------------------------

# Flask's `any` converter constrains the URL rule itself, so /api/<kind> only
# ever matches the four real columns.
#
# This matters more than it looks. Without it, the rule matches ANY single
# path segment -- so GET /api/nothing-here matched the POST-only add-item rule
# and returned 405 Method Not Allowed with an HTML body, instead of a JSON
# 404. Every typo'd endpoint looked like a method problem. It's the same
# reason Kenneth's server.js pinned its kinds into the route pattern:
#     const KINDS_RE = ':kind(assignments|exams|events|todos)'
KINDS_RULE = "<any(assignments, exams, events, todos):kind>"


@app.route("/api/board")
def board():
    user, error = require_user()
    if error:
        return error
    return jsonify(store.get_board(user["id"]))


@app.route(f"/api/{KINDS_RULE}", methods=["POST"])
def add_item(kind: str):
    user, error = require_user()
    if error:
        return error
    try:
        return jsonify(store.add_item(user["id"], kind,
                                      request.get_json(silent=True) or {})), 201
    except store.StoreError as exc:
        return jsonify({"error": exc.code, "message": exc.message}), 400


@app.route(f"/api/{KINDS_RULE}/<item_id>", methods=["PATCH"])
def patch_item(kind: str, item_id: str):
    user, error = require_user()
    if error:
        return error
    updated = store.update_item(user["id"], kind, item_id,
                                request.get_json(silent=True) or {})
    if updated is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(updated)


@app.route(f"/api/{KINDS_RULE}/<item_id>", methods=["DELETE"])
def delete_item(kind: str, item_id: str):
    user, error = require_user()
    if error:
        return error
    if not store.remove_item(user["id"], kind, item_id):
        return jsonify({"error": "not_found"}), 404
    return ("", 204)


@app.route("/api/demo", methods=["POST"])
def demo():
    user, error = require_user()
    if error:
        return error
    return jsonify(store.load_demo(user["id"]))


@app.route("/api/clear", methods=["POST"])
def clear():
    user, error = require_user()
    if error:
        return error
    store.set_surface(user["id"], [])
    return jsonify(store.clear(user["id"]))


# --------------------------------------------------------------------------
# Library and files
# --------------------------------------------------------------------------


@app.route("/api/library")
def get_library():
    user, error = require_user()
    if error:
        return error
    return jsonify(store.get_library(user["id"]))


@app.route("/api/library", methods=["PUT"])
def put_library():
    user, error = require_user()
    if error:
        return error
    return jsonify(store.set_library(user["id"], request.get_json(silent=True) or {}))


def _attachments(board_data: dict) -> list[dict]:
    """Flatten every attachment on every item into one list."""
    out = []
    for kind in store.KINDS:
        for item in board_data.get(kind) or []:
            for attachment in item.get("attachments") or []:
                out.append({**attachment, "itemKind": kind, "itemId": item.get("id")})
    return out


@app.route("/api/files")
def list_files():
    user, error = require_user()
    if error:
        return error
    files = _attachments(store.get_board(user["id"]))
    return jsonify([
        {"id": f.get("id"), "name": f.get("name"), "type": f.get("type"),
         "size": f.get("size"), "itemKind": f["itemKind"], "itemId": f["itemId"],
         "url": f"/api/files/{f.get('id')}"}
        for f in files
    ])


@app.route("/api/files/<file_id>")
def get_file(file_id: str):
    user, error = require_user()
    if error:
        return error
    match = next((f for f in _attachments(store.get_board(user["id"]))
                  if f.get("id") == file_id), None)
    if not match or not match.get("dataUrl"):
        return jsonify({"error": "not_found"}), 404

    parsed = re.match(r"^data:([^;]+);base64,(.*)$", match["dataUrl"], re.DOTALL)
    if not parsed:
        return jsonify({"error": "bad_data"}), 422
    try:
        raw = base64.b64decode(parsed.group(2))
    except Exception:  # noqa: BLE001
        return jsonify({"error": "bad_data"}), 422

    safe_name = re.sub(r'["\r\n]', "", str(match.get("name") or "file"))
    return Response(raw, headers={
        "Content-Type": match.get("type") or parsed.group(1) or "application/octet-stream",
        # inline, not an attachment, so the Files tab can preview a PDF --
        # but with nosniff so a mislabelled upload can't be executed as HTML.
        "Content-Disposition": f'inline; filename="{safe_name}"',
        "X-Content-Type-Options": "nosniff",
    })


# --------------------------------------------------------------------------
# The assistant
# --------------------------------------------------------------------------


def _apply_actions(user_id: str, actions: list[dict]) -> list[dict]:
    """
    Write the agent's actions to the board.

    Same mapping the Node backend used, so the action names in agent.py (and
    in the old assistant.js docs) still mean what they said.
    """
    type_to_kind = {"addAssignments": "assignments", "addExams": "exams",
                    "addEvents": "events", "addTodos": "todos"}
    applied = []
    for action in actions or []:
        items = action.get("items")
        if not isinstance(items, list) or not items:
            continue

        if action.get("type") == "addFiles":
            filed = store.add_library_files(user_id, items)
            applied.append({"type": "addFiles", "added": filed,
                            "seen": len(items)})
            continue

        if action.get("type") == "removeItems":
            gone = store.remove_matching(user_id, items)
            applied.append({"type": "removeItems", "added": gone,
                            "seen": len(items)})
            # Chunks render a snapshot of the data, so a panel built before
            # the delete would still show the rows. Rebuilding is the layout
            # agent's job on the next turn; dropping the stale assignment
            # panels now is the honest interim.
            if gone:
                store.set_surface(user_id, [
                    chunk for chunk in store.get_surface(user_id)
                    if chunk.get("kind") not in ("assignment_list", "schedule")
                ])
            continue

        if action.get("type") == "completeItems":
            # Tick off rows the agent marked submitted/dismissed. Searches
            # every column because an exam and an assignment are the same
            # Tiger Data row, split across two board columns by kind.
            ticked = store.complete_items(user_id, items)
            applied.append({"type": "completeItems", "added": ticked,
                            "seen": len(items)})
            continue

        kind = type_to_kind.get(action.get("type"))
        if not kind:
            continue
        added = store.upsert_items(user_id, kind, items)
        applied.append({"type": action["type"], "added": len(added),
                        "seen": len(items)})
    return applied


@app.route("/api/assistant", methods=["POST"])
def assistant():
    """
    The chat box, and the centre of the whole project.

    Returns everything the page needs to update itself in one response:
      reply    what the assistant says          -> chat bubble
      board    the board after the writes       -> the four panels
      surface  the HTML chunks now on the page  -> the assistant's panels
      changes  what it altered, in words        -> "what changed" under the reply
      trace    every tool call it made          -> the agent view
    """
    user, error = require_user()
    if error:
        return error

    body = request.get_json(silent=True) or {}
    message = str(body.get("message") or "")
    history = body.get("history") or []
    attachments = body.get("attachments") or []

    try:
        with with_student(user):
            result = agent.handle(
                message=message,
                history=history,
                board=store.get_board(user["id"]),
                attachments=attachments,
                current_surface=store.get_surface(user["id"]),
                library=store.get_library(user["id"]),
            )
            applied = _apply_actions(user["id"], result.get("actions") or [])
            chunks = store.set_surface(user["id"], result.get("surface") or [])
    except Exception as exc:  # noqa: BLE001
        print(f"[app] assistant failed: {type(exc).__name__}: {exc}")
        return jsonify({
            "error": "assistant_failed",
            "reply": "The assistant hit an error. Your board is unchanged.",
            "board": store.get_board(user["id"]),
            "surface": store.get_surface(user["id"]),
        }), 500

    changes = result.get("changes") or {}
    changes["applied"] = applied
    return jsonify({
        "reply": result.get("reply", ""),
        "actions": result.get("actions") or [],
        "board": store.get_board(user["id"]),
        "surface": chunks,
        "changes": changes,
        "trace": result.get("trace") or {},
        "display": result.get("display") or {},
    })


@app.route("/api/sync", methods=["POST"])
def sync():
    """Crawl Canvas without going through the chat. The refresh button."""
    user, error = require_user()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    try:
        with with_student(user):
            found = agent.sync_canvas(body.get("pages"))
            added = {
                "assignments": len(store.upsert_items(user["id"], "assignments",
                                                      found.get("assignments") or [])),
                "exams": len(store.upsert_items(user["id"], "exams",
                                                found.get("exams") or [])),
                "events": len(store.upsert_items(user["id"], "events",
                                                 found.get("events") or [])),
            }
    except Exception as exc:  # noqa: BLE001
        print(f"[app] sync failed: {exc}")
        return jsonify({"error": "sync_failed", "message": str(exc)[:200]}), 500

    return jsonify({"added": added, "error": found.get("error"),
                    "board": store.get_board(user["id"])})


# --------------------------------------------------------------------------
# The HTML surface
# --------------------------------------------------------------------------


@app.route("/api/surface")
def get_surface():
    """
    What the page is currently showing.

    This is the endpoint the display agent polls before it decides what to
    change (see surface.py). The browser renders from the same endpoint, so
    what the agent reads is by definition what the student sees -- there's no
    second copy of the truth to drift.
    """
    user, error = require_user()
    if error:
        return error
    return jsonify({
        "chunks": store.get_surface(user["id"]),
        "style": surface.STYLE_VOCAB,
        "premade_card_types": surface.premade_types(),
        "limits": {"max_chunks": surface.MAX_CHUNKS,
                   "max_removals_per_turn": surface.MAX_REMOVALS_PER_TURN},
    })


@app.route("/api/surface", methods=["PUT"])
def put_surface():
    """
    Replace the surface. Used by the reset button in the UI.

    A person clearing their own dashboard is a different act from the agent
    deciding to delete a panel, so this endpoint has none of apply_ops's
    removal limits -- and the agent can't reach it.
    """
    user, error = require_user()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    incoming = body.get("chunks")
    if not isinstance(incoming, list):
        return jsonify({"error": "bad_request",
                        "message": "Send {\"chunks\": [...]}"}), 400

    clean = []
    for chunk in incoming[:surface.MAX_CHUNKS]:
        if not isinstance(chunk, dict) or not chunk.get("id"):
            continue
        html, _ = surface.sanitize(chunk.get("html") or "")
        clean.append({"id": str(chunk["id"])[:64], "kind": chunk.get("kind", "custom"),
                      "title": str(chunk.get("title") or "")[:80], "html": html,
                      "source": "user"})
    return jsonify({"chunks": store.set_surface(user["id"], clean)})


@app.route("/api/workload")
def workload():
    """Time-series straight out of the Timescale hypertable, for the chart."""
    user, error = require_user()
    if error:
        return error
    days = request.args.get("days", default=30, type=int)
    with with_student(user):
        return jsonify(tools.get_workload_history(days))


# --------------------------------------------------------------------------
# Voice: /api/voice and Alexa
#
# Jared's endpoints, moved here from orchestrator/server.py so there's one
# deployment instead of two processes. The translation logic is his, unchanged
# apart from reading the account's own data instead of a fixed STUDENT_ID.
# --------------------------------------------------------------------------

ALEXA_HELP = ("You can ask about what is due, overdue work, your schedule, "
              "campus events, data freshness, or workload.")
ALEXA_CONFUSED = ("I did not understand that. Ask what is due, what is "
                  "overdue, or what is on your schedule.")


def alexa_response(speech: str, *, title: str = "Office Hours",
                   end_session: bool = True, reprompt: str | None = None) -> dict:
    response: dict = {"shouldEndSession": end_session,
                      "outputSpeech": {"type": "PlainText", "text": speech}}
    if end_session:
        response["card"] = {"type": "Simple", "title": title, "content": speech}
    elif reprompt:
        response["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
    return {"version": "1.0", "response": response}


def _voice_run(utterance: str, user: dict | None) -> dict:
    """
    One voice turn: fast model, reasoning off, read-only tools, 2 turns max.

    An Alexa skill endpoint times out after a few seconds, which is why this
    doesn't reuse the web path.
    """
    if user is not None:
        with tools.use_student(store.student_id_for(user["id"])):
            run = orchestrator.run(utterance, channel="voice")
    else:
        # No account attached to the device: answer from the demo student's
        # data rather than failing, which is what makes the skill testable
        # before account linking is set up.
        run = orchestrator.run(utterance, channel="voice")

    speech = (run.display.get("speech") or run.summary
              or "I could not get your Office Hours update.")
    return {"speech": speech,
            "card_title": run.display.get("headline", "Office Hours"),
            "elapsed_ms": run.elapsed_ms, "error": run.error}


@app.route("/api/voice", methods=["POST"])
def voice():
    body = request.get_json(silent=True) or {}
    utterance = str(body.get("utterance") or body.get("prompt") or "").strip()
    if not utterance:
        return jsonify({"error": "no_utterance"}), 400
    return jsonify(_voice_run(utterance, current_user()))


@app.route("/alexa", methods=["POST"])
def alexa():
    """
    Alexa Custom Skill requests, translated into the voice path.

    ALEXA_SKILL_ID is checked when set, so a request meant for someone else's
    skill is rejected. Leaving it unset is fine for local testing and wrong in
    production; /api/health reports which it is.
    """
    body = request.get_json(silent=True) or {}

    expected = os.getenv("ALEXA_SKILL_ID", "").strip()
    actual = (
        (body.get("session") or {}).get("application", {}).get("applicationId")
        or ((body.get("context") or {}).get("System", {})
            .get("application", {}).get("applicationId"))
    )
    if expected and actual != expected:
        return jsonify({"error": "forbidden", "message": "Invalid Alexa skill ID"}), 403

    alexa_request = body.get("request") or {}
    request_type = alexa_request.get("type")

    if request_type == "LaunchRequest":
        return jsonify(alexa_response(
            "Welcome to Office Hours. You can ask what is due, what is "
            "overdue, or what is on your schedule.",
            end_session=False, reprompt="What would you like to know?"))

    if request_type == "SessionEndedRequest":
        return jsonify({"version": "1.0", "response": {"shouldEndSession": True}})

    if request_type != "IntentRequest":
        return jsonify(alexa_response(ALEXA_CONFUSED, end_session=False,
                                      reprompt="What would you like to know?"))

    intent = alexa_request.get("intent") or {}
    name = intent.get("name", "")
    slots = intent.get("slots") or {}

    if name == "AMAZON.HelpIntent":
        return jsonify(alexa_response(ALEXA_HELP, end_session=False,
                                      reprompt="What would you like to know?"))
    if name in {"AMAZON.CancelIntent", "AMAZON.StopIntent"}:
        return jsonify(alexa_response("Goodbye."))

    def slot(slot_name: str) -> str | None:
        value = (slots.get(slot_name) or {}).get("value")
        return value.strip() if isinstance(value, str) and value.strip() else None

    course, days = slot("Course"), slot("Days")
    utterances = {
        "DueIntent": ("What assignments are due"
                      + (f" in {course}" if course else "")
                      + (f" within {days} days" if days else "") + "?"),
        "OverdueIntent": "What assignments are overdue?",
        "ScheduleIntent": "What is on my schedule?",
        "EventsIntent": ("What events are coming up"
                         + (f" within {days} days" if days else "") + "?"),
        "FreshnessIntent": "Is my coursework up to date?",
        "WorkloadIntent": ("How has my workload changed"
                           + (f" over the last {days} days" if days else "") + "?"),
    }
    utterance = utterances.get(name)
    if utterance is None:
        return jsonify(alexa_response(ALEXA_CONFUSED, end_session=False,
                                      reprompt="What would you like to know?"))

    try:
        result = _voice_run(utterance, current_user())
    except Exception as exc:  # noqa: BLE001
        print(f"[app] alexa run failed: {exc}")
        return jsonify(alexa_response(
            "Office Hours is temporarily unavailable. Please try again.",
            end_session=False, reprompt="Please try your question again."))

    if result.get("error"):
        return jsonify(alexa_response(
            "I could not get that Office Hours update. Please try again.",
            end_session=False, reprompt="Please try your question again."))

    return jsonify(alexa_response(result["speech"],
                                  title=result.get("card_title", "Office Hours")))


# --------------------------------------------------------------------------
# Compatibility: the old /api/prompt route
#
# templates/index.html (now at /classic) posts here. Kept so that page keeps
# working -- it's the no-account debug view.
# --------------------------------------------------------------------------


@app.route("/api/prompt", methods=["POST"])
def prompt():
    body = request.get_json(silent=True) or {}
    text = str(body.get("prompt") or "").strip()
    if not text:
        return jsonify({"error": "No prompt provided"}), 400
    run = orchestrator.run(text, context=body.get("context"))
    return jsonify(run.to_json())


@app.errorhandler(405)
def wrong_method(_error):
    return jsonify({"error": "method_not_allowed", "path": request.path,
                    "method": request.method}), 405


@app.errorhandler(404)
def not_found(_error):
    # An unknown /api/* path is a bug worth seeing as JSON; anything else is
    # someone typing a URL, so send them to the site.
    if request.path.startswith("/api/"):
        return jsonify({"error": "not_found", "path": request.path}), 404
    return redirect("/")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))

    # MODE now comes from the repo-root .env (see the note in
    # orchestrator/config.py about the two files). That root file says
    # MODE=live, where the old behaviour -- reading only orchestrator/.env --
    # gave you mock. Same command, real money. So say it out loud rather than
    # let the first page load be the thing that tells you.
    paths = _model_paths()
    for name, info in paths.items():
        if info["state"] != "ready" and cache.MODE != "mock":
            print()
            print(f"  !!  {name} is unavailable: "
                  f"{'; '.join(info['problems'] or [])}")
            if info.get("degrades_to"):
                print(f"      {info['degrades_to']}")

    if cache.MODE != "mock":
        print()
        print(f"  !!  MODE={cache.MODE.upper()} — this will make real API calls "
              f"and spend credits.")
        print(f"      Every prompt hits Nemotron and Claude. To work for free:")
        print(f"        MODE=mock python3 app.py")
        print(f"      or set MODE=mock in {ROOT / '.env'}")
        print()

    print(f"Office Hours on http://localhost:{port}  "
          f"(MODE={cache.MODE}, store={store.backend()})")
    app.run(debug=bool(os.getenv("FLASK_DEBUG")), port=port)
