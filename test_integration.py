"""
Tests for the integration layer: the store, the HTML surface, the sanitizer,
the board bridge, and the HTTP API.

    MODE=mock python3 test_integration.py

No network, no database, no cost, about a second. Run it after every change,
same as orchestrator/test_loop.py (which still covers the orchestrator loop,
the display contract, and the Canvas parser -- this file does not repeat
those).

WHAT'S WORTH TESTING HERE, AND WHY
The interesting tests in this file are the ones that check a REFUSAL rather
than a result: that the sanitizer strips a script tag, that the surface
refuses to delete more than one chunk a turn, that one account can't read
another's board, that a password in a chat message never reaches a model.
Those are the properties that quietly stop being true when someone refactors,
because nothing looks broken when a safety net stops working.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "orchestrator"))

os.environ.setdefault("MODE", "mock")
os.environ["STORE_BACKEND"] = "file"
os.environ["STORE_FILE"] = "/tmp/officehours-test-store.json"
# Don't let a real DATABASE_URL in .env pull these tests onto a live database.
os.environ["DATABASE_URL"] = ""

_passed, _failed = 0, 0
_section = ""


def section(name: str) -> None:
    global _section
    _section = name
    print(f"\n{name}")


def check(label: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  ok    {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))


def fresh_store():
    """A clean file store for each group of tests."""
    path = Path(os.environ["STORE_FILE"])
    if path.exists():
        path.unlink()
    import store

    store._backend = None  # re-decide the backend after the env changes above
    store._degraded = None
    store.STORE_FILE = path
    return store


# ==========================================================================
section("sanitizer")
# ==========================================================================

import surface  # noqa: E402

ATTACKS = [
    ("script tag", '<div>ok<script>alert(1)</script></div>', "alert"),
    ("event handler", '<div onclick="steal()">hi</div>', "onclick"),
    ("img onerror", '<img src=x onerror="alert(1)">', "onerror"),
    ("javascript: href", '<a href="javascript:alert(1)">x</a>', "javascript:"),
    ("iframe", '<iframe src="https://evil.test"></iframe>', "iframe"),
    ("inline style url", '<div style="background:url(https://evil.test/x)">y</div>', "url("),
    ("style block", '<style>body{display:none}</style><p>hi</p>', "display:none"),
    ("form", '<form action="/api/clear"><input name="x"></form>', "<form"),
    ("svg onload", '<svg onload="alert(1)"><circle/></svg>', "onload"),
    ("data: uri", '<a href="data:text/html,<script>alert(1)</script>">x</a>', "data:"),
    ("nested script", '<div><span><script>x=1</script></span></div>', "x=1"),
    ("uppercase tag", '<DIV><SCRIPT>alert(1)</SCRIPT></DIV>', "alert"),
]

for label, payload, forbidden in ATTACKS:
    cleaned, dropped = surface.sanitize(payload)
    check(f"strips {label}", forbidden.lower() not in cleaned.lower(),
          f"got: {cleaned[:120]}")

cleaned, _ = surface.sanitize('<div>keep <strong>this</strong> text</div>')
check("keeps allowed markup", "<strong>this</strong>" in cleaned, cleaned)

cleaned, _ = surface.sanitize('<marquee>still readable</marquee>')
check("unwraps unknown tags but keeps text",
      "still readable" in cleaned and "marquee" not in cleaned, cleaned)

cleaned, _ = surface.sanitize('<div class="panel">a')
check("closes unbalanced tags", cleaned.endswith("</div>"), cleaned)

cleaned, _ = surface.sanitize('<div style="width: 40%; background: var(--accent)">x</div>')
check("keeps the narrow style allowlist",
      "width:40%" in cleaned.replace(" ", "") and "var(--accent)" in cleaned, cleaned)

cleaned, _ = surface.sanitize('<div style="position:fixed; top:0">x</div>')
check("drops styles outside the allowlist", "position" not in cleaned, cleaned)

cleaned, _ = surface.sanitize('<a href="https://calendar.pitt.edu/e/1">event</a>')
check("http links survive with noopener",
      'href="https://calendar.pitt.edu/e/1"' in cleaned
      and "noopener" in cleaned, cleaned)

cleaned, _ = surface.sanitize('<p>2 < 3 & "quoted"</p>')
check("escapes stray text characters", "&lt;" in cleaned or "< 3" not in cleaned,
      cleaned)

big = surface.sanitize("<p>" + "x" * 50000 + "</p>")[0]
check("caps chunk size", len(big) <= surface.MAX_CUSTOM_HTML_CHARS + 100,
      f"len={len(big)}")


# ==========================================================================
section("premade templates")
# ==========================================================================

check("every card type has a template",
      set(surface.premade_types()) >= {"assignment_list", "schedule",
                                       "study_set", "event_list",
                                       "workload_chart", "text", "alert"},
      str(surface.premade_types()))

SAMPLES = {
    "assignment_list": {"type": "assignment_list", "title": "Due soon", "items": [
        {"title": "Problem Set 4", "course": "PHYS 1361", "due": "Tue 11:59pm",
         "priority": 1, "est_minutes": 180}]},
    "schedule": {"type": "schedule", "title": "Plan", "blocks": [
        {"day": "Mon", "start": "7:00pm", "end": "8:00pm", "task": "Review",
         "est_minutes": 60}]},
    "study_set": {"type": "study_set", "title": "Set", "sections": [
        {"topic": "Gauss's law", "summary": "Flux and charge.",
         "questions": ["State it."]}]},
    "event_list": {"type": "event_list", "title": "Campus", "items": [
        {"title": "Career Fair", "when": "Wed 4pm", "where": "Alumni Hall"}]},
    "workload_chart": {"type": "workload_chart", "title": "Trend", "series": [
        {"label": "PHYS 1361", "points": [{"x": "2026-09-15", "y": 4.0},
                                          {"x": "2026-09-19", "y": 6.5}]}]},
    "text": {"type": "text", "title": "Note", "body": "Plain prose."},
    "alert": {"type": "alert", "title": "Heads up", "body": "Something."},
}

for kind, card in SAMPLES.items():
    html = surface.render_card(card)
    check(f"renders {kind}", len(html) > 50 and "panel" in html, html[:120])

# The data in a card comes from validated tool results, but defence in depth:
# a card whose title contains markup must not produce markup.
nasty = surface.render_card({"type": "text", "title": "<script>alert(1)</script>",
                             "body": "<img onerror=x>"})
# The markup must be inert, which means ESCAPED, not absent: the words
# "script" and "onerror" still appear as visible text, and should. What must
# not appear is a live tag.
check("card data can't inject markup",
      "<script" not in nasty and "<img" not in nasty
      and "&lt;script&gt;" in nasty, nasty[:200])

empty = surface.render_card({"type": "assignment_list", "title": "None", "items": []})
check("empty card renders an empty state", "Nothing here" in empty, empty[:120])

missing = surface.render_card({"type": "brand_new_type", "title": "X", "rows": [1]})
check("unknown card type falls back to text", "panel" in missing, missing[:120])

titled = surface.render_card(SAMPLES["assignment_list"], title="Due before Friday")
check("plan's title overrides the card's", "Due before Friday" in titled,
      titled[:160])


# ==========================================================================
section("surface: applying a plan")
# ==========================================================================

cards = [SAMPLES["assignment_list"], SAMPLES["alert"]]

chunks, report = surface.apply_ops([], surface.fallback_ops(cards, []), cards)
check("fallback renders one chunk per card", len(chunks) == 2, str(report))
check("fallback needs no model", report["note"].startswith("house layout"))

# Re-running the same plan must update in place, not duplicate.
chunks2, report2 = surface.apply_ops(chunks, surface.fallback_ops(cards, chunks), cards)
check("re-applying updates in place", len(chunks2) == 2 and not report2["added"],
      str(report2))

# Three chunks, all three requested for removal. The per-turn cap is the rule
# under test here, so there have to be enough chunks that the "never remove
# the last one" guard isn't what fires instead.
three = chunks2 + [{"id": "spare", "kind": "text", "title": "t",
                    "html": "<p>x</p>", "source": "agent"}]
ops = {"upsert": [], "custom": [], "remove": [c["id"] for c in three], "note": ""}
after, report3 = surface.apply_ops(three, ops, cards)
check("removal is capped at one per turn", len(after) == 2, str(report3))
check("the refusal is reported",
      any("one-removal-per-turn" in r for r in report3["refused"]), str(report3))

user_chunk = {"id": "mine", "kind": "custom", "title": "Mine",
              "html": "<p>mine</p>", "source": "user"}
after, report4 = surface.apply_ops(
    [user_chunk, dict(chunks2[0])],
    {"upsert": [], "custom": [], "remove": ["mine"], "note": ""}, cards)
check("never removes a student-created chunk",
      any(c["id"] == "mine" for c in after), str(report4))
check("says why it kept it",
      any("student-created" in r for r in report4["refused"]), str(report4))

only = [{"id": "solo", "kind": "text", "title": "t", "html": "<p>x</p>",
         "source": "agent"}]
after, report5 = surface.apply_ops(
    only, {"upsert": [], "custom": [], "remove": ["solo"], "note": ""}, cards)
check("never removes the last chunk", len(after) == 1, str(report5))

many = [{"id": f"c{i}", "kind": "text", "title": "t", "html": "<p>x</p>",
         "source": "agent", "updated_at": f"2026-09-{i + 1:02d}"}
        for i in range(surface.MAX_CHUNKS)]
after, report6 = surface.apply_ops(
    many, {"upsert": [{"id": "brand-new", "card_index": 0, "title": "New"}],
           "custom": [], "remove": [], "note": ""}, cards)
check("page is bounded at MAX_CHUNKS", len(after) <= surface.MAX_CHUNKS,
      f"{len(after)} chunks")
check("the new chunk is the one that survives",
      any(c["id"] == "brand-new" for c in after), str(report6))
check("eviction is oldest-first and reported",
      "c0" in report6["removed"], str(report6))

# A plan referring to a card index that doesn't exist must be dropped, not crash.
bad = surface._validate_ops(
    {"upsert": [{"id": "x", "card_index": 99}, {"id": "y", "card_index": "nope"},
                {"id": "z", "card_index": 0}],
     "custom": [{"id": "no-html"}], "remove": [None, "ok-id"]}, cards)
check("drops out-of-range card indexes", len(bad["upsert"]) == 1, str(bad))
check("drops custom chunks with no html", not bad["custom"], str(bad))
check("drops non-string removals", bad["remove"] == ["ok-id"], str(bad))

custom_ops = {"upsert": [], "remove": [], "note": "",
              "custom": [{"id": "note", "title": "N",
                          "html": '<section class="panel">ok</section>'
                                  '<script>alert(1)</script>'}]}
after, report7 = surface.apply_ops([], custom_ops, cards)
check("custom chunk is stored sanitized",
      "script" not in after[0]["html"], after[0]["html"][:120])
check("sanitizing is reported to the user", bool(report7["sanitized"]), str(report7))

after, report8 = surface.apply_ops(
    [], {"upsert": [], "remove": [], "note": "",
         "custom": [{"id": "all-bad", "title": "X", "html": "<script>x</script>"}]},
    cards)
check("a chunk that sanitizes to nothing is refused",
      not after and any("nothing left" in r for r in report8["refused"]),
      str(report8))

plan = surface.plan_ops("what's due?", "You have two things due.", cards, [])
check("mock layout agent returns a usable plan",
      bool(plan["upsert"]) or bool(plan["custom"]), str(plan))


# ==========================================================================
section("store")
# ==========================================================================

store = fresh_store()
check("defaults to the file store", store.backend() == "file", store.backend())

user = store.create_user("Finn", "hunter2")
check("creates an account", user["username"] == "Finn", str(user))
check("verifies the right password",
      store.verify_user("finn", "hunter2") is not None)
check("rejects the wrong password", store.verify_user("finn", "nope") is None)
check("username lookup is case-insensitive",
      store.verify_user("FINN", "hunter2") is not None)

raw = json.loads(Path(os.environ["STORE_FILE"]).read_text())
stored_user = raw["users"]["finn"]
check("no plaintext password anywhere",
      "hunter2" not in json.dumps(raw), "password found in the store file")
check("hash is scrypt-sized", len(stored_user["hash"]) == 128,
      str(len(stored_user["hash"])))

# Node's crypto.scryptSync(password, salt, 64) with the salt used as the hex
# STRING, not the bytes it decodes to. This vector was produced by store.js;
# if this breaks, every account made before the port stops being able to log
# in -- silently, and only for existing users.
import hashlib  # noqa: E402

NODE_SALT = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
_, our_hash = store._hash_password("hunter2", NODE_SALT)
# Node: crypto.scryptSync(password, salt, 64) -> N=16384, r=8, p=1, and the
# salt hashed as the hex string it is. Reproducing that call here is the
# check: if these parameters ever drift, accounts created by store.js stop
# verifying, silently, and only for users who already existed.
expected = hashlib.scrypt(b"hunter2", salt=NODE_SALT.encode(),
                          n=16384, r=8, p=1, dklen=64,
                          maxmem=64 * 1024 * 1024).hex()
check("scrypt parameters match Node's (N=16384, r=8, p=1, dklen=64)",
      our_hash == expected, f"{our_hash[:24]} != {expected[:24]}")
check("salt is hashed as the hex string, not decoded bytes",
      store._hash_password("x", "abcd")[1]
      != hashlib.scrypt(b"x", salt=bytes.fromhex("abcd"), n=16384, r=8, p=1,
                        dklen=64, maxmem=64 * 1024 * 1024).hex(),
      "matching the decoded-bytes form would lock out existing accounts")

try:
    store.create_user("finn", "another")
    check("refuses a duplicate username", False)
except store.StoreError as exc:
    check("refuses a duplicate username", exc.code == "exists", exc.code)

for bad_name, why in [("f", "too short"), ("a" * 60, "too long"),
                      ("has space", "space"), ("drop;--", "punctuation")]:
    try:
        store.create_user(bad_name, "password")
        check(f"rejects username: {why}", False)
    except store.StoreError:
        check(f"rejects username: {why}", True)

try:
    store.create_user("shorty", "abc")
    check("rejects a short password", False)
except store.StoreError as exc:
    check("rejects a short password", exc.code == "weak", exc.code)

token = store.create_session(user["id"])
check("session resolves to the account",
      (store.user_by_token(token) or {}).get("id") == user["id"])
check("a bad token resolves to nobody", store.user_by_token("garbage") is None)
store.delete_session(token)
check("logout invalidates the token", store.user_by_token(token) is None)

check("student ids are namespaced per account",
      store.student_id_for(user["id"]).startswith("acct-"),
      store.student_id_for(user["id"]))

board = store.get_board(user["id"])
check("a new board is empty",
      all(board[kind] == [] for kind in store.KINDS), str(board))

added = store.upsert_items(user["id"], "assignments", [
    {"title": "PS4", "course": "PHYS", "canvasId": "c1"},
    {"title": "Lab 3", "course": "CS", "canvasId": "c2"},
])
check("upsert adds new rows", len(added) == 2, str(added))

again = store.upsert_items(user["id"], "assignments", [
    {"title": "PS4 (updated)", "course": "PHYS", "canvasId": "c1"}])
board = store.get_board(user["id"])
check("upsert on canvasId updates instead of duplicating",
      len(board["assignments"]) == 2 and not again, str(board["assignments"]))
check("the update landed",
      any(row["title"] == "PS4 (updated)" for row in board["assignments"]))

store.update_item(user["id"], "assignments", board["assignments"][0]["id"],
                  {"completed": True})
store.upsert_items(user["id"], "assignments", [
    {"title": "PS4 (resynced)", "course": "PHYS", "canvasId": "c1",
     "completed": False}])
board = store.get_board(user["id"])
done = next(row for row in board["assignments"] if row.get("canvasId") == "c1")
check("a re-sync can't un-complete something you ticked off",
      done["completed"] is True, str(done))

store.upsert_items(user["id"], "assignments", [
    {"title": "Evil", "canvasId": "c3", "onclick": "alert(1)",
     "dataUrl": "x" * 100, "junk": "y"}])
board = store.get_board(user["id"])
evil = next(row for row in board["assignments"] if row.get("canvasId") == "c3")
check("unknown item fields are dropped",
      "onclick" not in evil and "junk" not in evil, str(evil))

other = store.create_user("rowan", "tigerdata")
check("a second account sees its own empty board",
      store.get_board(other["id"])["assignments"] == [])
store.set_surface(other["id"], [{"id": "x", "kind": "text", "title": "t",
                                 "html": "<p>x</p>", "source": "agent"}])
check("surfaces are per-account",
      len(store.get_surface(user["id"])) == 0
      and len(store.get_surface(other["id"])) == 1)

store.load_demo(user["id"])
board = store.get_board(user["id"])
check("demo data loads",
      len(board["assignments"]) == 3 and len(board["exams"]) == 2, str(
          {k: len(v) for k, v in board.items()}))
store.clear(user["id"])
check("clear empties the board",
      all(store.get_board(user["id"])[k] == [] for k in store.KINDS))

store.set_library(user["id"], {"collections": [{"id": "c", "name": "Notes"}],
                               "files": []})
check("library round-trips",
      store.get_library(user["id"])["collections"][0]["name"] == "Notes")


# ==========================================================================
section("agent: tool results -> board rows")
# ==========================================================================

import agent  # noqa: E402

steps = [{
    "tool": "get_assignments", "ok": True, "result": {"items": [
        {"id": "a1", "title": "Problem Set 4", "course_code": "PHYS 1361",
         "due_at": "2026-09-22T23:59:00-04:00", "est_hours": 3.0, "status": "open"},
        {"id": "a2", "title": "Midterm Exam 1", "course_code": "PHYS 1361",
         "due_at": "2026-10-08T14:00:00-04:00", "est_hours": 2.0,
         "kind": "exam", "status": "open"},
        {"id": "a3", "title": "Quiz 3", "course_code": "CS 1675",
         "due_at": None, "kind": "quiz", "status": "graded"},
    ]},
}, {
    "tool": "get_events", "ok": True, "result": {"items": [
        {"title": "Career Fair", "starts_at": "2026-09-24T16:00:00-04:00",
         "location": "Alumni Hall"}]},
}, {
    "tool": "get_schedule", "ok": False, "result": {"error": "nope"},
}]

actions = agent.board_actions(steps)
by_type = {action["type"]: action["items"] for action in actions}
check("assignments and exams are split",
      len(by_type.get("addAssignments", [])) == 1
      and len(by_type.get("addExams", [])) == 2, str(by_type.keys()))
check("exam detection uses kind and title",
      {item["title"] for item in by_type["addExams"]} == {"Midterm Exam 1", "Quiz 3"},
      str(by_type.get("addExams")))
check("events come through", len(by_type.get("addEvents", [])) == 1)
check("est_hours becomes estimateMins",
      by_type["addAssignments"][0]["estimateMins"] == 180,
      str(by_type["addAssignments"][0]))
check("a graded item arrives completed",
      next(i for i in by_type["addExams"] if i["title"] == "Quiz 3")["completed"]
      is True)
check("the orchestrator's id becomes canvasId, so re-crawls upsert",
      by_type["addAssignments"][0]["canvasId"] == "a1")
check("failed tool steps contribute nothing",
      "addTodos" not in by_type)

check("a step with no result dict is survivable",
      agent.board_actions([{"tool": "get_assignments", "ok": True,
                            "result": "not a dict"}]) == [])
check("rows with no title are dropped",
      agent.board_actions([{"tool": "get_assignments", "ok": True,
                            "result": {"items": [{"id": "x", "title": "  "}]}}]) == [])

for message in ["my password is hunter2",
                "the duo code is 123456",
                "password: letmein"]:
    out = agent.handle(message)
    check(f"refuses to take a credential: {message[:24]}",
          "not going to take a password" in out["reply"]
          and not out["actions"], out["reply"][:80])
check("the credential path never reaches a model",
      agent.handle("my password is hunter2")["trace"] == {})

out = agent.handle("")
check("an empty prompt asks for one, and changes nothing",
      not out["actions"] and not out["surface"], str(out)[:120])


# ==========================================================================
section("HTTP API")
# ==========================================================================

Path(os.environ["STORE_FILE"]).unlink(missing_ok=True)
import app as web  # noqa: E402

web.store._backend = None
web.store.STORE_FILE = Path(os.environ["STORE_FILE"])
client = web.app.test_client()

check("the site is served at /", client.get("/").status_code == 200)
check("the fallback dashboard is served at /classic",
      client.get("/classic").status_code == 200)

health = client.get("/api/health").get_json()
check("health reports the mode", health["mode"] == "mock", str(health.get("mode")))
check("health reports the store backend", health["store"]["backend"] == "file",
      str(health.get("store")))
check("health lists the premade card types",
      len(health["surface"]["premade_card_types"]) >= 7)

check("the board needs a session", client.get("/api/board").status_code == 401)
check("a bad token is rejected",
      client.get("/api/board",
                 headers={"Authorization": "Bearer nope"}).status_code == 401)
check("the assistant needs a session",
      client.post("/api/assistant", json={"message": "hi"}).status_code == 401)

registered = client.post("/api/register",
                         json={"username": "kenneth", "password": "website"})
check("register returns a token", registered.status_code == 201
      and "token" in registered.get_json(), registered.get_data(as_text=True)[:120])
auth = {"Authorization": "Bearer " + registered.get_json()["token"]}

# A valid password, so the duplicate-username check is what's being tested
# rather than the length check.
dupe = client.post("/api/register", json={"username": "kenneth",
                                          "password": "different"})
check("a duplicate username is a 409", dupe.status_code == 409,
      str(dupe.status_code))
check("the message is human", "taken" in (dupe.get_json().get("message") or ""))

bad_login = client.post("/api/login", json={"username": "kenneth",
                                            "password": "wrong"})
check("a wrong password is a 401", bad_login.status_code == 401)

answer = client.post("/api/assistant",
                     json={"message": "what's due this week?", "history": []},
                     headers=auth)
check("the assistant answers", answer.status_code == 200, str(answer.status_code))
body = answer.get_json()
check("there is a reply", bool(body["reply"]), str(body)[:120])
check("the board came back", "assignments" in body["board"])
check("the board was written to",
      len(body["board"]["assignments"]) + len(body["board"]["exams"]) > 0,
      str({k: len(v) for k, v in body["board"].items()}))
check("HTML chunks came back", len(body["surface"]) > 0)
check("every chunk has rendered html",
      all(chunk.get("html") for chunk in body["surface"]))
check("no chunk contains a script tag",
      not any("<script" in (chunk.get("html") or "").lower()
              for chunk in body["surface"]))
check("changes are reported", bool(body["changes"].get("applied")),
      str(body["changes"])[:200])
check("the trace is included", bool(body["trace"].get("steps")))

repeat = client.post("/api/assistant", json={"message": "what's due this week?"},
                     headers=auth).get_json()
check("asking twice doesn't duplicate board rows",
      len(repeat["board"]["assignments"]) == len(body["board"]["assignments"]),
      f'{len(body["board"]["assignments"])} then {len(repeat["board"]["assignments"])}')
check("asking twice doesn't duplicate chunks",
      len(repeat["surface"]) == len(body["surface"]))

polled = client.get("/api/surface", headers=auth).get_json()
check("the surface endpoint returns what the page shows",
      len(polled["chunks"]) == len(repeat["surface"]))
check("the surface endpoint publishes the style vocabulary",
      "containers" in polled["style"])
check("the surface endpoint publishes its limits",
      polled["limits"]["max_removals_per_turn"] == 1)

reset = client.put("/api/surface", json={"chunks": []}, headers=auth)
check("a person can clear their own surface",
      reset.status_code == 200 and reset.get_json()["chunks"] == [])

injected = client.put("/api/surface", json={"chunks": [
    {"id": "mine", "html": '<p>ok</p><script>alert(1)</script>'}]}, headers=auth)
check("even a person's own chunks are sanitized",
      "script" not in injected.get_json()["chunks"][0]["html"],
      str(injected.get_json())[:160])
check("chunks written by a person are marked as theirs",
      injected.get_json()["chunks"][0]["source"] == "user")

item = client.post("/api/todos", json={"text": "Email professor"},
                   headers=auth)
check("adding a todo works", item.status_code == 201, str(item.status_code))
todo_id = item.get_json()["id"]
check("patching an item works",
      client.patch(f"/api/todos/{todo_id}", json={"done": True},
                   headers=auth).get_json()["done"] is True)
check("deleting an item works",
      client.delete(f"/api/todos/{todo_id}", headers=auth).status_code == 204)
check("deleting a missing item is a 404",
      client.delete("/api/todos/nope", headers=auth).status_code == 404)
check("an unknown kind is a 404",
      client.post("/api/pizzas", json={}, headers=auth).status_code == 404)

check("demo data can be loaded",
      len(client.post("/api/demo", headers=auth).get_json()["assignments"]) == 3)
cleared = client.post("/api/clear", headers=auth).get_json()
check("clear empties the board", cleared["assignments"] == [])
check("clear also clears the agent's panels",
      client.get("/api/surface", headers=auth).get_json()["chunks"] == [])

check("library round-trips over HTTP",
      client.put("/api/library",
                 json={"collections": [{"id": "c", "name": "Exams"}], "files": []},
                 headers=auth).status_code == 200
      and client.get("/api/library",
                     headers=auth).get_json()["collections"][0]["name"] == "Exams")

synced = client.post("/api/sync", json={}, headers=auth)
check("sync works without the chat", synced.status_code == 200,
      str(synced.status_code))

check("workload history is exposed for the chart",
      "points" in client.get("/api/workload", headers=auth).get_json())

check("an unknown api path is a json 404",
      client.get("/api/nothing-here").status_code == 404,
      str(client.get("/api/nothing-here").status_code))
check("a real but POST-only item route is a 405, not a 404",
      client.get("/api/assignments").status_code == 405,
      "GET /api/assignments is a method error, not a missing resource; "
      "the board is read at GET /api/board")
wrong_method = client.get("/api/assistant")
check("a wrong method on a real route is a json 405",
      wrong_method.status_code == 405
      and wrong_method.get_json()["error"] == "method_not_allowed",
      str(wrong_method.status_code))
check("an unknown page redirects to the site",
      client.get("/typo").status_code in (301, 302))

# --- Alexa ---
launch = client.post("/alexa", json={"request": {"type": "LaunchRequest"}})
check("alexa launch is answered", launch.status_code == 200
      and "Welcome" in launch.get_json()["response"]["outputSpeech"]["text"])
check("alexa launch keeps the session open",
      launch.get_json()["response"]["shouldEndSession"] is False)

due = client.post("/alexa", json={"request": {
    "type": "IntentRequest",
    "intent": {"name": "DueIntent", "slots": {"Days": {"value": "7"}}}}})
check("alexa DueIntent gets speech",
      bool(due.get_json()["response"]["outputSpeech"]["text"]))
check("alexa speech has no markup",
      "<" not in due.get_json()["response"]["outputSpeech"]["text"])

for intent in ("OverdueIntent", "ScheduleIntent", "EventsIntent",
               "FreshnessIntent", "WorkloadIntent"):
    reply = client.post("/alexa", json={"request": {
        "type": "IntentRequest", "intent": {"name": intent, "slots": {}}}})
    check(f"alexa {intent} is handled",
          reply.status_code == 200
          and bool(reply.get_json()["response"]["outputSpeech"]["text"]))

helped = client.post("/alexa", json={"request": {
    "type": "IntentRequest", "intent": {"name": "AMAZON.HelpIntent"}}})
check("alexa help is handled",
      helped.get_json()["response"]["shouldEndSession"] is False)
stopped = client.post("/alexa", json={"request": {
    "type": "IntentRequest", "intent": {"name": "AMAZON.StopIntent"}}})
check("alexa stop ends the session",
      stopped.get_json()["response"]["shouldEndSession"] is True)
unknown = client.post("/alexa", json={"request": {
    "type": "IntentRequest", "intent": {"name": "MadeUpIntent"}}})
check("an unknown alexa intent is handled, not crashed",
      unknown.status_code == 200)
ended = client.post("/alexa", json={"request": {"type": "SessionEndedRequest"}})
check("alexa session end is acknowledged", ended.status_code == 200)

os.environ["ALEXA_SKILL_ID"] = "amzn1.ask.skill.expected"
wrong_skill = client.post("/alexa", json={
    "request": {"type": "LaunchRequest"},
    "session": {"application": {"applicationId": "amzn1.ask.skill.someone-else"}}})
check("a request for another skill is rejected when a skill id is set",
      wrong_skill.status_code == 403, str(wrong_skill.status_code))
right_skill = client.post("/alexa", json={
    "request": {"type": "LaunchRequest"},
    "session": {"application": {"applicationId": "amzn1.ask.skill.expected"}}})
check("the configured skill id is accepted", right_skill.status_code == 200)
os.environ["ALEXA_SKILL_ID"] = ""

check("the classic dashboard's endpoint still works",
      client.post("/api/prompt", json={"prompt": "what's due?"}).status_code == 200)

# --- one account cannot see another's data over HTTP ---
second = client.post("/api/register", json={"username": "jared", "password": "alexa2"})
auth2 = {"Authorization": "Bearer " + second.get_json()["token"]}
client.post("/api/demo", headers=auth2)
# Compare the actual rows rather than counts: the first account has been
# through /api/sync by this point, so "is it empty" is the wrong question --
# "do these two boards share any row" is the right one.
first_ids = {row["id"] for row
             in client.get("/api/board", headers=auth).get_json()["assignments"]}
second_ids = {row["id"] for row
              in client.get("/api/board", headers=auth2).get_json()["assignments"]}
check("accounts are isolated over HTTP",
      not (first_ids & second_ids) and len(second_ids) == 3,
      f"first={len(first_ids)} second={len(second_ids)} shared={first_ids & second_ids}")
check("one account can't delete another's items",
      client.delete("/api/assignments/"
                    + client.get("/api/board", headers=auth2)
                    .get_json()["assignments"][0]["id"],
                    headers=auth).status_code == 404)


# ==========================================================================
section("config")
# ==========================================================================

import config  # noqa: E402

check("the root .env is the canonical one",
      config.ROOT_ENV_PATH.name == ".env"
      and config.ROOT_ENV_PATH.parent == ROOT, str(config.ROOT_ENV_PATH))
check("status() reports which files were read", "env_file" in config.status())

import tools  # noqa: E402

check("student scoping falls back to the environment",
      tools.student() == os.getenv("STUDENT_ID", "demo-student"))
with tools.use_student("acct-test"):
    check("student scoping applies inside the block",
          tools.student() == "acct-test")
check("student scoping is restored after the block",
      tools.student() != "acct-test")


# ==========================================================================
print(f"\n{_passed} passed, {_failed} failed")
Path(os.environ["STORE_FILE"]).unlink(missing_ok=True)
sys.exit(1 if _failed else 0)
