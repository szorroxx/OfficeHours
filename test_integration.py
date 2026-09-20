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
section("regressions from the first live run")
# ==========================================================================
# Each of these is a failure a real student hit in MODE=live. The transcript
# is the spec: the agent claimed changes it hadn't made, wrote a calendar
# entry nowhere visible, couldn't create a to-do at all, told someone a
# course was empty while showing its assignments two messages later, and
# reported every late-evening deadline a day late.

# --- it claimed changes it hadn't made ---
for message, reply, steps, should_flag in [
    ("remove the preproposal from my overdue list",
     "The Preproposal has already been submitted by your groupmate, so it "
     "isn't considered overdue. There's nothing to remove.",
     [{"tool": "check_freshness", "ok": True}], True),
    ("remove it from the overdue section",
     "Since the system only lists open assignments, it simply doesn't appear "
     "there once it's marked as submitted.",
     [{"tool": "check_freshness", "ok": True}], True),
    ("add a 2 hour viola lesson thursday",
     "I've added a 2-hour viola lesson to your schedule.",
     [{"tool": "check_freshness", "ok": True}], True),
    ("add a 2 hour viola lesson thursday",
     "I've added a 2-hour viola lesson to your schedule.",
     [{"tool": "add_to_schedule", "ok": True}], False),
    ("remove the preproposal",
     "I marked the Preproposal as dismissed, so it's off your overdue list.",
     [{"tool": "update_assignment", "ok": True}], False),
    ("what's due this week?", "You have three things due.",
     [{"tool": "get_assignments", "ok": True}], False),
    ("list my assignments",
     "Here are the assignments currently stored for you: HW01, Preproposal.",
     [{"tool": "get_assignments", "ok": True}], False),
    ("am I behind?", "Nothing is overdue right now.",
     [{"tool": "get_overdue", "ok": True}], False),
]:
    flagged = agent._unbacked_claim(message, reply, steps) is not None
    check(f"{'flags' if should_flag else 'allows'}: {reply[:38]}",
          flagged == should_flag,
          f"expected flag={should_flag}, got {flagged}")

failed_write = agent._unbacked_claim(
    "add a viola lesson", "I've added it to your schedule.",
    [{"tool": "add_to_schedule", "ok": False}])
check("a failed write is reported as a failure, not a success",
      failed_write is not None and "didn't go through" in failed_write,
      str(failed_write))

# --- a calendar entry has to reach the visible board ---
sched_actions = agent.board_actions([{
    "tool": "add_to_schedule", "ok": True,
    "result": {"blocks_added": 1, "blocks": [
        {"task": "Viola lesson", "starts_at": "2026-09-24T15:00:00-04:00",
         "ends_at": "2026-09-24T17:00:00-04:00", "est_minutes": 120}]}}])
sched_events = next((a["items"] for a in sched_actions
                     if a["type"] == "addEvents"), [])
check("a schedule block becomes a board row the calendar renders",
      len(sched_events) == 1 and sched_events[0]["startISO"].startswith("2026-09-24T15:00"),
      str(sched_actions))
check("schedule rows are tagged so they're distinguishable from Canvas events",
      sched_events and sched_events[0]["source"] == "schedule")
check("schedule rows have deterministic ids, so re-reading doesn't duplicate",
      agent._schedule_row({"task": "Viola lesson",
                           "starts_at": "2026-09-24T15:00:00-04:00"})["canvasId"]
      == sched_events[0]["canvasId"])

# --- to-dos were impossible to create ---
task_actions = agent.board_actions([{
    "tool": "add_tasks", "ok": True,
    "result": {"added": 2, "tasks": [{"text": "Read ch. 3", "est_minutes": 45},
                                     {"text": "Email the professor"}]}}])
todos = next((a["items"] for a in task_actions if a["type"] == "addTodos"), [])
check("the agent can put to-dos on the board", len(todos) == 2, str(task_actions))
check("to-do estimates survive", todos[0].get("estimateMins") == 45, str(todos[0]))

# --- "take that off my list" has to change something ---
# NOTE: this assertion used to read "a dismissed assignment is ticked off the
# board", and it passed while the product was broken. Ticking was the bug: a
# student dismissed thirteen assignments, was told they were removed, and saw
# thirteen struck-through rows. The test encoded my wrong assumption about
# what the student wanted, so it defended the behaviour instead of catching
# it. Dismissed means gone; submitted means ticked.
done_actions = agent.board_actions([{
    "tool": "update_assignment", "ok": True,
    "result": {"updated": 1, "status": "submitted", "items": [
        {"id": "abc123", "title": "HW01", "status": "submitted"}]}}])
completed = next((a["items"] for a in done_actions
                  if a["type"] == "completeItems"), [])
check("a submitted assignment is ticked off the board",
      len(completed) == 1 and completed[0]["completed"] is True, str(done_actions))
check("and the reason is recorded, not silently ticked",
      "submitted" in completed[0].get("note", ""), str(completed))
check("an assignment left open is not ticked",
      not [a for a in agent.board_actions([{
          "tool": "update_assignment", "ok": True,
          "result": {"items": [{"id": "x", "title": "T", "status": "open"}]}}])
          if a["type"] == "completeItems"])

# --- store.complete_items finds the row whichever column it's in ---
store_t = fresh_store()
u = store_t.create_user("ticker", "password")
store_t.upsert_items(u["id"], "assignments", [
    {"title": "Preproposal", "canvasId": "abc123"}])
store_t.upsert_items(u["id"], "exams", [{"title": "Midterm 1", "canvasId": "x1"}])
ticked = store_t.complete_items(u["id"], [
    {"canvasId": "abc123", "title": "Preproposal", "note": "marked dismissed"},
    {"canvasId": "x1", "title": "Midterm 1"}])
check("completing items searches every board column", ticked == 2, str(ticked))
b = store_t.get_board(u["id"])
check("the assignment is ticked", b["assignments"][0]["completed"] is True)
check("the exam is ticked too", b["exams"][0]["completed"] is True)
check("completing by title works when there's no canvasId",
      store_t.complete_items(u["id"], [{"title": "nope"}]) == 0)
check("already-completed rows aren't re-counted",
      store_t.complete_items(u["id"], [{"canvasId": "abc123"}]) == 0)

# --- the change log shouldn't name the same panel twice ---
dupe_cards = [SAMPLES["assignment_list"]]
_, dupe_report = surface.apply_ops(
    [], {"upsert": [{"id": "assignment-list", "card_index": 0, "title": "A"},
                    {"id": "assignment-list", "card_index": 0, "title": "B"}],
         "custom": [], "remove": [], "note": ""}, dupe_cards)
check("a panel touched twice is reported once",
      dupe_report["added"] == ["assignment-list"]
      and dupe_report["updated"] == [], str(dupe_report))

# --- more conversation history than four turns ---
long_history = [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": f"turn {i}"} for i in range(20)]
ctx = agent._context({}, None, long_history)
check("the model sees more than four turns of history",
      len(ctx["recent_turns"]) == 12, str(len(ctx.get("recent_turns", []))))
check("and is told how many it can't see",
      ctx["turns_before_this"] == 8, str(ctx.get("turns_before_this")))
check("the model is told the timezone", bool(ctx.get("timezone")), str(ctx))
check("and that times are already local", ctx.get("times_are_local") is True)


# ==========================================================================
section("regressions from the second live run")
# ==========================================================================
import db as _db  # noqa: E402

# --- the 2pm lesson that showed up at 7pm ---
naive = _db.jsonable(_db._as_datetime("2026-09-24T14:00:00", "starts_at"))
explicit = _db.jsonable(_db._as_datetime("2026-09-24T14:00:00-04:00", "starts_at"))
check("a naive timestamp means local time, not UTC",
      naive == explicit, f"naive={naive} explicit={explicit}")
check("2pm stays 2pm", naive.startswith("2026-09-24T14:00"), naive)
check("_tz never raises, even with a junk timezone name",
      bool(_db._tz()) and bool(
          (lambda: (setattr(_db, "TIMEZONE", "Mars/Olympus"), _db._tz())[1])()))
_db.TIMEZONE = "America/New_York"

# --- the block that said 2:00pm-4:00pm / 60 min ---
block = _db._normalize_block({"task": "Viola lesson",
                              "starts_at": "2026-09-24T14:00:00-04:00",
                              "ends_at": "2026-09-24T16:00:00-04:00",
                              "est_minutes": 60})
check("the clock wins over a wrong duration", block["est_minutes"] == 120,
      str(block["est_minutes"]))
filled = _db._normalize_block({"task": "x", "starts_at": "2026-09-24T14:00:00-04:00",
                               "est_minutes": 120})
check("a missing end time is derived from the duration",
      _db.jsonable(filled["ends_at"]).startswith("2026-09-24T16:00"),
      _db.jsonable(filled["ends_at"]))
backwards = _db._normalize_block({"task": "x",
                                  "starts_at": "2026-09-24T16:00:00-04:00",
                                  "ends_at": "2026-09-24T14:00:00-04:00",
                                  "est_minutes": 120})
check("an end before its start is repaired, not stored",
      backwards["ends_at"] > backwards["starts_at"], str(backwards))
bare = _db._normalize_block({"task": "x", "starts_at": "2026-09-24T14:00:00-04:00"})
check("a block with only a start gets an hour",
      bare["est_minutes"] == 60 and bare["ends_at"] is not None, str(bare))

# --- "Overdue by 20716 days" ---
undated = agent._assignment_row({"id": "r1", "title": "Roll Call Attendance",
                                 "course_code": "MUSIC 0620", "due_at": None,
                                 "est_hours": 5.4, "status": "open"})
check("an assignment with no due date omits dueISO entirely",
      "dueISO" not in undated,
      "a null dueISO becomes 1970 in the browser and renders as "
      "'Overdue by 20716 days'")
dated = agent._assignment_row({"id": "r2", "title": "HW01", "course_code": "PHYS",
                               "due_at": "2026-09-03T03:59:59+00:00"})
check("a real due date is still sent", "dueISO" in dated, str(dated))
check("and it lands on the local day, not the UTC one",
      dated["dueISO"].startswith("2026-09-02T23:59"), dated["dueISO"])
check("an event with no start time is dropped rather than dated 1970",
      agent._event_row({"title": "Mystery", "starts_at": None}) is None)

# --- the frontend's half of the same fix ---
page = (ROOT / "app.html").read_text()
check("the page refuses to do date maths on a missing date",
      "function hasDate" in page and "No due date" in page)
for guard in ("function urgency", "function examWhen", "function eventWhen"):
    index = page.index(guard)
    check(f"{guard.split()[1]} checks the date first",
          "hasDate" in page[index:index + 260], guard)

# --- things can now be deleted ---
check("schedule blocks can be removed",
      "remove_from_schedule" in __import__("tools").DISPATCH)
check("campus events can be removed",
      "remove_events" in __import__("tools").DISPATCH)
import tools as _tools  # noqa: E402

check("a removal with no target is refused, not treated as 'everything'",
      "error" in _tools.execute("remove_from_schedule", {}),
      "there must be no way to delete a whole schedule by accident")
_tools.execute("add_to_schedule", {"items": [
    {"task": "Viola lesson", "starts_at": "2026-09-24T14:00:00-04:00"}]})
gone = _tools.execute("remove_from_schedule", {"task_match": "viola"})
check("removing by title works", gone.get("removed") == 1, str(gone))

removal_actions = agent.board_actions([{
    "tool": "remove_from_schedule", "ok": True,
    "result": {"removed": 1, "blocks": [
        {"id": "9", "task": "Viola lesson",
         "starts_at": "2026-09-24T15:00:00-04:00"}]}}])
check("a deleted block is removed from the board too",
      any(a["type"] == "removeItems" for a in removal_actions),
      str(removal_actions))

# The read-after-delete trap: "remove it, then show me my schedule" runs both,
# and the read can still return the row the delete just took out.
mixed = agent.board_actions([
    {"tool": "remove_from_schedule", "ok": True,
     "result": {"removed": 1, "blocks": [
         {"task": "Viola lesson", "starts_at": "2026-09-24T15:00:00-04:00"}]}},
    {"tool": "get_schedule", "ok": True,
     "result": {"blocks": [
         {"task": "Viola lesson", "starts_at": "2026-09-24T15:00:00-04:00"},
         {"task": "Problem Set 4", "starts_at": "2026-09-21T18:00:00-04:00"}]}},
])
re_added = [i["title"] for a in mixed if a["type"] == "addEvents"
            for i in a["items"]]
check("a block deleted this turn isn't re-added by a read in the same turn",
      "Viola lesson" not in re_added, str(re_added))
check("the other blocks survive", "Problem Set 4" in re_added, str(re_added))

store_r = fresh_store()
ur = store_r.create_user("remover", "password")
store_r.upsert_items(ur["id"], "events", [
    {"title": "Lunch-and-Learn", "canvasId": "ev1", "startISO": "2026-09-21T09:00:00-04:00"},
    {"title": "SteelHacks", "canvasId": "ev2", "startISO": "2026-09-20T09:00:00-04:00"}])
store_r.upsert_items(ur["id"], "assignments", [{"title": "HW01", "canvasId": "a1"}])
removed_count = store_r.remove_matching(ur["id"], [
    {"canvasId": "ev1", "title": "Lunch-and-Learn", "kind": "events"}])
check("removing an event takes exactly that row", removed_count == 1)
after_board = store_r.get_board(ur["id"])
check("the other event stays", len(after_board["events"]) == 1)
check("and removing events can't take an assignment with it",
      len(after_board["assignments"]) == 1)

# --- multi-step capacity ---
import orchestrator as _orch  # noqa: E402

check("the loop has room for a multi-step request", _orch.MAX_TURNS >= 12,
      f"MAX_TURNS={_orch.MAX_TURNS}")
check("answers aren't truncated mid-plan", _orch.MAX_TOKENS >= 8000,
      f"MAX_TOKENS={_orch.MAX_TOKENS}")
check("a 40-event tool result isn't cut off before the model reads it",
      _orch.TOOL_RESULT_CHARS >= 24000, f"{_orch.TOOL_RESULT_CHARS}")
prompt = _orch.SYSTEM_PROMPT.lower()
check("the prompt tells it to finish multi-step work",
      "multi-step" in prompt and "before you answer" in prompt)
check("the prompt tells it removal is possible now",
      "remove_from_schedule" in prompt and "remove_events" in prompt)
check("the prompt demands explicit offsets",
      "utc offset" in prompt, "a bare 14:00 is what put a lesson at 7pm")
check("running out of turns says what got done",
      "ran out of steps partway" in _orch.SYSTEM_PROMPT
      or "ran out of steps partway" in open(
          ROOT / "orchestrator" / "orchestrator.py").read())


# ==========================================================================
section("marking is not removing")
# ==========================================================================
# Third live run: the assistant dismissed thirteen assignments, said they no
# longer appear on the dashboard, and they all still appeared -- struck
# through. Asked again, it blamed the browser cache. Two separate faults:
# 'dismissed' mapped to a tick rather than a removal, and the dashboard
# rendered ticked rows instead of hiding them.

dismissed_actions = agent.board_actions([{
    "tool": "update_assignment", "ok": True,
    "result": {"updated": 1, "status": "dismissed", "items": [
        {"id": "d1", "title": "Preproposal", "status": "dismissed"}]}}])
check("a dismissed assignment is REMOVED from the board, not ticked",
      any(a["type"] == "removeItems" for a in dismissed_actions)
      and not any(a["type"] == "completeItems" for a in dismissed_actions),
      str(dismissed_actions))

submitted_actions = agent.board_actions([{
    "tool": "update_assignment", "ok": True,
    "result": {"items": [{"id": "s1", "title": "HW01", "status": "submitted"}]}}])
check("work the student actually did is kept and ticked",
      any(a["type"] == "completeItems" for a in submitted_actions),
      str(submitted_actions))

deleted_actions = agent.board_actions([{
    "tool": "delete_assignments", "ok": True,
    "result": {"deleted": 2, "items": [
        {"id": "x1", "title": "Quiz 01"}, {"id": "x2", "title": "HW01"}]}}])
check("a deleted assignment is removed from the board",
      next((len(a["items"]) for a in deleted_actions
            if a["type"] == "removeItems"), 0) == 2, str(deleted_actions))

check("there is a tool that really deletes coursework",
      "delete_assignments" in _tools.DISPATCH)
check("and one to undo it", "restore_assignments" in _tools.DISPATCH)
check("deleting refuses an empty id list",
      "error" in _tools.execute("delete_assignments", {"assignment_ids": []}))
check("deleting accepts a bare id, not just a list",
      _tools.execute("delete_assignments",
                     {"assignment_ids": "m1"}).get("deleted") == 1)

# A delete has to survive the next crawl, or it's a no-op with extra steps.
db_src = (ROOT / "orchestrator" / "db.py").read_text()
upsert = db_src[db_src.index("def upsert_assignments("):]
upsert = upsert[:upsert.index("\ndef ")]
check("the crawler skips assignments the student deleted",
      "suppressed_assignments" in upsert,
      "otherwise refresh_from_canvas rebuilds the same id and re-adds the row")
check("a crawl can't reset a status the student set",
      "WHEN assignments.status IN" in upsert and "dismissed" in upsert,
      "status = EXCLUDED.status turned every dismissal back into 'open'")
check("deleting also clears orphaned schedule blocks",
      "schedule_blocks" in db_src[db_src.index("def delete_assignments("):
                                  db_src.index("def restore_assignments(")])

# --- the dashboard's half ---
page = (ROOT / "app.html").read_text()
check("the page has one visibility rule for finished work",
      "function visible(rows)" in page and "showDone" in page)
for renderer in ("renderAssignments", "renderExams", "renderEvents",
                 "renderTodos"):
    index = page.index("function " + renderer)
    check(f"{renderer} hides finished work",
          "visible(" in page[index:index + 420], renderer)
check("the week strip hides it too",
      "visible(events)" in page[page.index("function renderSchedule"):
                                page.index("function renderSchedule") + 900])
check("the month calendar hides it too",
      "visible(assignments)" in page[page.index("function calItemsByDay"):
                                     page.index("function calItemsByDay") + 900])
check("finished work can still be brought back into view",
      "Show ' + n + ' completed" in page or "completed'" in page,
      "hiding must be reversible, or it's data loss from the user's view")

# --- the change log said "3 completeItemss" ---
plural = agent._changes(
    [{"type": "completeItems", "items": [{"title": "a"}, {"title": "b"}]},
     {"type": "removeItems", "items": [{"title": "c"}]}],
    {}, [])
check("counts are pluralised properly",
      plural["board"] == ["2 items ticked off", "1 item removed"],
      str(plural["board"]))
single = agent._changes([{"type": "addAssignments", "items": [{"title": "a"}]}],
                        {}, [])
check("and singular when there's one", single["board"] == ["1 assignment"],
      str(single["board"]))

# --- the store really drops the rows ---
store_d = fresh_store()
ud = store_d.create_user("deleter", "password")
store_d.upsert_items(ud["id"], "assignments", [
    {"title": "Quiz 01", "canvasId": "q1"}, {"title": "HW01", "canvasId": "h1"}])
store_d.upsert_items(ud["id"], "exams", [{"title": "FinalExam", "canvasId": "f1"}])
store_d.remove_matching(ud["id"], [
    {"canvasId": "q1", "title": "Quiz 01"},
    {"canvasId": "f1", "title": "FinalExam"}])
left = store_d.get_board(ud["id"])
check("removed rows are gone from the store, not flagged",
      len(left["assignments"]) == 1 and len(left["exams"]) == 0,
      str({k: len(v) for k, v in left.items()}))
check("the row that wasn't named survives",
      left["assignments"][0]["title"] == "HW01")

check("the prompt forbids blaming the browser",
      "never blame the browser" in _orch.SYSTEM_PROMPT.lower()
      and "clear their cache" in _orch.SYSTEM_PROMPT.lower())
check("the prompt says marking is not removing",
      "marking is not removing" in _orch.SYSTEM_PROMPT.lower())


# ==========================================================================
section("the scheduler")
# ==========================================================================
# Fourth live run: make_schedule failed four times in one conversation with
# "an internal error (missing dependency)". The dependency was the anthropic
# package, absent on the server; scheduling was the only feature with a hard
# model dependency and no fallback. It's now plain Python, so these tests are
# about whether the plan is actually correct.

import scheduler as sched  # noqa: E402
from datetime import datetime as _dtm, timedelta as _td  # noqa: E402
from zoneinfo import ZoneInfo as _ZI  # noqa: E402

_tz_ny = _ZI("America/New_York")
_now = _dtm(2026, 9, 19, 23, 22, tzinfo=_tz_ny)

REAL_BOARD = [
    {"id": "a1", "title": "HW01", "course_code": "PHYS 1351",
     "due_at": "2026-09-02T23:59:00-04:00", "est_hours": 1.8, "kind": "homework"},
    {"id": "a2", "title": "Homework 03", "course_code": "CS 1675",
     "due_at": "2026-09-21T23:59:00-04:00", "est_hours": 5.4, "kind": "homework"},
    {"id": "a3", "title": "Proposal", "course_code": "CS 1684",
     "due_at": "2026-09-27T23:59:00-04:00", "est_hours": 14.4, "kind": "project"},
    {"id": "a4", "title": "Ethics in AI", "course_code": "CS 1684",
     "due_at": "2026-09-30T13:00:00-04:00", "est_hours": 3.6, "kind": "reading"},
    {"id": "a5", "title": "FinalExam", "course_code": "PHYS 1351",
     "due_at": None, "est_hours": 4.5, "kind": "exam"},
]
REHEARSAL = sched.busy_from_blocks([
    {"task": "Orchestra rehearsal", "starts_at": "2026-09-23T19:30:00-04:00",
     "ends_at": "2026-09-23T22:00:00-04:00"}])

result = sched.plan(sched.tasks_from_assignments(REAL_BOARD), now=_now,
                    tz=_tz_ny, horizon_days=7,
                    constraints=sched.parse_constraints(
                        "class 9-11am weekdays, no work friday nights, "
                        "no more than 4 hours a day"),
                    busy=list(REHEARSAL))
plan_blocks = result["blocks"]

check("a plan is produced with no model at all", len(plan_blocks) > 0,
      "this is the whole point: scheduling must not need Claude")
check("the planner says it was deterministic",
      result["planner"] == "deterministic", result["planner"])

check("nothing is scheduled in the past",
      all(b["starts_at"] > _now.isoformat() for b in plan_blocks),
      "the model scheduled study time for work due three weeks earlier")

due_by = {a["title"]: a["due_at"] for a in REAL_BOARD}
late = [b for b in plan_blocks
        if due_by.get(b["assignment_title"])
        and b["assignment_title"] != "HW01"          # overdue: no deadline left
        and b["ends_at"] > due_by[b["assignment_title"]]]
check("no block runs past its own due date", not late, str(late[:2]))

overlaps = []
ordered = sorted(plan_blocks, key=lambda b: b["starts_at"])
for earlier, later in zip(ordered, ordered[1:]):
    if later["starts_at"] < earlier["ends_at"]:
        overlaps.append((earlier["task"], later["task"]))
check("blocks never overlap each other", not overlaps, str(overlaps[:2]))

clash = [b for b in plan_blocks
         if b["starts_at"] < "2026-09-23T22:00" and b["ends_at"] > "2026-09-23T19:30"]
check("an existing commitment is never double-booked", not clash,
      f"scheduled over the orchestra rehearsal: {clash[:1]}")

per_day: dict[str, int] = {}
for b in plan_blocks:
    per_day[b["starts_at"][:10]] = per_day.get(b["starts_at"][:10], 0) + b["est_minutes"]
check("the daily cap is respected", all(v <= 240 for v in per_day.values()),
      str(per_day))

fri_evening = [b for b in plan_blocks
               if _dtm.fromisoformat(b["starts_at"]).weekday() == 4
               and _dtm.fromisoformat(b["starts_at"]).hour >= 17]
check("'no work friday nights' is honoured", not fri_evening, str(fri_evening[:1]))

in_class = [b for b in plan_blocks
            if _dtm.fromisoformat(b["starts_at"]).weekday() < 5
            and 9 <= _dtm.fromisoformat(b["starts_at"]).hour < 11]
check("'class 9-11am weekdays' is honoured", not in_class, str(in_class[:1]))

check("sessions are a sensible length",
      all(30 <= b["est_minutes"] <= 120 for b in plan_blocks),
      str(sorted({b["est_minutes"] for b in plan_blocks})))
check("durations land on quarter hours",
      all(b["est_minutes"] % 15 == 0 for b in plan_blocks),
      "66- and 36-minute blocks are correct and look like a bug")
check("long work is split across days",
      len({b["starts_at"][:10] for b in plan_blocks
           if b["assignment_title"] == "Proposal"}) > 1,
      "14 hours in one sitting isn't a plan")
check("what couldn't fit is reported, not dropped",
      all("reason" in u for u in result["unscheduled"]),
      str(result["unscheduled"][:1]))
check("an assignment with no due date still gets time",
      any(b["assignment_title"] == "FinalExam" for b in plan_blocks))
check("blocks carry the assignment id, so they link back",
      all(b.get("assignment_id") for b in plan_blocks))

# --- strategies ---
day_before = sched.plan(
    sched.tasks_from_assignments(REAL_BOARD), now=_now, tz=_tz_ny,
    horizon_days=14, strategy="day_before",
    constraints=sched.parse_constraints("each one hour, between 6pm and 7pm"))
check("'an hour each, the day before' gives one session per assignment",
      len(day_before["blocks"]) == len({b["assignment_title"]
                                        for b in day_before["blocks"]}),
      str([b["task"] for b in day_before["blocks"]]))
check("and puts it in the requested window",
      all(_dtm.fromisoformat(b["starts_at"]).hour == 18
          for b in day_before["blocks"]),
      str([b["starts_at"] for b in day_before["blocks"]][:3]))
check("and the day before the deadline where possible",
      any(b["starts_at"][:10] == "2026-09-26"
          for b in day_before["blocks"] if b["assignment_title"] == "Proposal"),
      str([b["starts_at"] for b in day_before["blocks"]]))

# --- constraint parsing ---
for text, expected_field, expected in [
    ("nothing after 9pm", "day_end", 21),
    ("after 6pm", "day_start", 18),
    ("between 6 and 7pm", "day_start", 18),
    ("at most 2 hours a day", "daily_cap_minutes", 120),
    ("45 minute blocks", "session_minutes", 45),
    ("one hour for each", "session_minutes", 60),
]:
    parsed = sched.parse_constraints(text)
    check(f"parses: {text}", getattr(parsed, expected_field) == expected,
          f"{expected_field}={getattr(parsed, expected_field)} wanted {expected}")

check("parses a day off",
      4 in sched.parse_constraints("keep fridays free").blackout_weekdays)
check("a constraint it can't read is reported, not silently dropped",
      sched.parse_constraints("only when Mercury is in retrograde").ignored,
      "silently ignoring a constraint means the student finds out by having "
      "that time scheduled")
check("what WAS understood is reported too",
      sched.parse_constraints("no more than 3 hours a day").understood)

check("an empty board is handled", sched.plan([], now=_now, tz=_tz_ny)["blocks"] == [])
check("a zero-length task still gets a usable session",
      sched.plan([sched.Task(title="x", minutes=0,
                             due=_now + _td(days=3))],
                 now=_now, tz=_tz_ny)["blocks"][0]["est_minutes"] >= 30)

# --- make_schedule survives Claude being missing, which is what happened ---
import cache as _cache  # noqa: E402

_real_claude = _cache.claude


def _no_anthropic(*_a, **_k):
    raise ModuleNotFoundError("No module named 'anthropic'")


_cache.claude = _no_anthropic
try:
    degraded = _tools.execute("make_schedule", {"horizon_days": 7})
finally:
    _cache.claude = _real_claude

check("make_schedule still works with the anthropic package missing",
      "error" not in degraded and degraded.get("blocks"),
      str(degraded)[:200])
check("and says so, naming the fix",
      "anthropic package isn't installed" in str(degraded.get("note", "")),
      str(degraded.get("note")))

# --- events go on the schedule at their real time ---
stored_event = _tools.execute("get_events", {"within_days": 14})["items"][0]
scheduled = _tools.execute("schedule_events", {"within_days": 14})
check("scheduling an event copies its stored start time",
      scheduled["blocks"][0]["starts_at"] == stored_event["starts_at"],
      f"{scheduled['blocks'][0]['starts_at']} != {stored_event['starts_at']}")
check("a career fair at 4pm does not land at 23:16",
      "T16:00" in scheduled["blocks"][0]["starts_at"],
      scheduled["blocks"][0]["starts_at"])
check("scheduling events with none stored says so, rather than inventing one",
      _tools.execute("schedule_events",
                     {"within_days": 7, "keyword": "nonexistent"}
                     ).get("blocks_added") == 0)

# --- a failed tool is reported verbatim ---
failed_changes = agent._changes([], {}, [
    {"tool": "make_schedule", "ok": False,
     "result_preview": '{"error": "make_schedule failed: ModuleNotFoundError: '
                       'No module named \'anthropic\'"}'}])
check("a tool failure is surfaced with its real error",
      failed_changes["failures"]
      and "anthropic" in failed_changes["failures"][0]["error"],
      str(failed_changes.get("failures")))
check("a successful run reports no failures",
      agent._changes([], {}, [{"tool": "get_assignments", "ok": True}])["failures"]
      == [])
page = (ROOT / "app.html").read_text()
check("the page shows tool failures to the student",
      "failed: " in page and "changes.failures" in page)


# ==========================================================================
section("generated documents reach the Files tab")
# ==========================================================================
# A student asked for a study guide module, got one -- saved in Tiger Data,
# rendered as a dashboard panel -- and found the Files tab empty. The tab's
# own header says "Where the assistant's generated files land"; it reads
# app_library, make_study_guide wrote study_sets, and no code joined them.

guide = _tools.execute("make_study_guide",
                       {"course": "PHYS 1351", "topics": ["Kinematics"]})
check("a study guide is produced", bool(guide.get("sections")), str(guide)[:160])

guide_actions = agent.board_actions([
    {"tool": "make_study_guide", "ok": True, "result": guide}])
files = next((a["items"] for a in guide_actions if a["type"] == "addFiles"), [])
check("a study guide becomes a file", len(files) == 1, str(guide_actions))
check("named after the course", files and "PHYS 1351" in files[0]["name"],
      str(files[:1]))
check("filed in a collection", files[0].get("collectionName") == "Study guides")
check("with a real byte size", files[0]["size"] > 200, str(files[0]["size"]))

import base64 as _b64  # noqa: E402

rendered = _b64.b64decode(files[0]["dataUrl"].split(",", 1)[1]).decode()
check("the file is a complete html document",
      rendered.startswith("<!DOCTYPE html>") and "</html>" in rendered)
check("the guide's content is in it",
      "Kinematics" in rendered or "Gauss" in rendered, rendered[:200])

# Model output is being written to a file the student opens in a browser.
nasty_guide = {"course": "X", "sections": [
    {"topic": "<script>alert(1)</script>",
     "summary": "<img src=x onerror=alert(1)>",
     "questions": ["</style><script>alert(2)</script>"]}]}
nasty_file = agent._study_guide_file(nasty_guide)
nasty_html = _b64.b64decode(nasty_file["dataUrl"].split(",", 1)[1]).decode()
check("markup in a guide is escaped, not executable",
      "<script>alert" not in nasty_html and "&lt;script&gt;" in nasty_html,
      nasty_html[nasty_html.index("<body>"):][:200])
# The escaped text legitimately contains the words "onerror=alert(1)" as
# visible characters, and should. What must not survive is a live tag.
check("and an onerror attribute can't survive",
      "<img" not in nasty_html and "&lt;img" in nasty_html,
      nasty_html[nasty_html.index("<body>"):][:200])

check("a guide with no sections produces no file",
      agent._study_guide_file({"course": "X", "sections": []}) is None)

# --- saved guides can be read back ---
check("there is a tool to read saved study guides",
      "get_study_sets" in _tools.DISPATCH,
      "make_study_guide was write-only, so 'show me that guide' meant "
      "regenerating it")
saved = _tools.execute("get_study_sets", {})
check("it returns the stored content", bool(saved.get("items")), str(saved)[:120])
refiled = agent.board_actions([
    {"tool": "get_study_sets", "ok": True, "result": saved}])
check("reading a saved guide re-files it",
      any(a["type"] == "addFiles" for a in refiled), str(refiled))

# --- the store side ---
store_l = fresh_store()
ul = store_l.create_user("filer", "password")
count = store_l.add_library_files(ul["id"], [
    {"name": "PHYS study guide.html", "type": "text/html", "size": 900,
     "dataUrl": "data:text/html;base64,PHA+aGk8L3A+",
     "collectionName": "Study guides"}])
check("a file is stored", count == 1)
lib = store_l.get_library(ul["id"])
check("the collection is created once",
      len(lib["collections"]) == 1
      and lib["collections"][0]["name"] == "Study guides", str(lib["collections"]))
check("the file is linked to it",
      lib["files"][0]["collectionId"] == lib["collections"][0]["id"])
check("and marked as AI-generated, which the UI badges",
      lib["files"][0]["source"] == "ai")

store_l.add_library_files(ul["id"], [
    {"name": "PHYS study guide.html", "type": "text/html", "size": 950,
     "dataUrl": "data:text/html;base64,PHA+bmV3PC9wPg==",
     "collectionName": "Study guides"}])
lib = store_l.get_library(ul["id"])
check("regenerating replaces rather than duplicating",
      len(lib["files"]) == 1 and lib["files"][0]["size"] == 950,
      f"{len(lib['files'])} files")
check("and doesn't create a second collection", len(lib["collections"]) == 1)

store_l.add_library_files(ul["id"], [
    {"name": "manual.pdf", "dataUrl": "data:application/pdf;base64,AAA",
     "collectionName": "Study guides"}])
check("a differently-named file is kept alongside",
      len(store_l.get_library(ul["id"])["files"]) == 2)
check("a file with no data is refused",
      store_l.add_library_files(ul["id"], [{"name": "empty.html"}]) == 0)

# --- a study guide failure names the fix ---
_real = _cache.claude
_cache.claude = _no_anthropic
try:
    broken = _tools.execute("make_study_guide",
                            {"course": "PHYS", "topics": ["x"]})
finally:
    _cache.claude = _real
check("a study guide that can't be written explains why",
      "error" in broken and "hint" in broken, str(broken)[:160])
check("and the hint is actionable",
      "anthropic" in broken["hint"] or "ANTHROPIC_API_KEY" in broken["hint"]
      or "model_paths" in broken["hint"], broken.get("hint"))

# --- the change log wording ---
page = (ROOT / "app.html").read_text()
check("the change log knows how to word each action type",
      "to your Files tab" in page and "Removed ${a.added}" in page,
      "'Added 1 removeItems to your board' was the old wording")


# ==========================================================================
section("the model can name the filing capability")
# ==========================================================================
# Asked "add the module to the files section", Nemotron answered: "I'm not
# able to add modules or files to a 'files' section -- my tools let me create
# study guides, schedule study time, manage assignments, and handle campus
# events, but there isn't a way to write or upload arbitrary files."
#
# That was an accurate description of its tool list. Filing existed, but as a
# side effect inside agent.py that no tool named -- so the student could not
# ask for it. A capability the model can't name is a capability that doesn't
# exist as far as anyone using it is concerned.

check("there is a tool for filing documents", "save_to_files" in _tools.DISPATCH)
schema_names = {t["function"]["name"] for t in _tools.TOOL_SCHEMAS}
check("and the model is told about it", "save_to_files" in schema_names,
      "a tool missing from TOOL_SCHEMAS is invisible to Nemotron")
save_schema = next(t["function"] for t in _tools.TOOL_SCHEMAS
                   if t["function"]["name"] == "save_to_files")
check("its description uses the words a student would",
      all(word in save_schema["description"].lower()
          for word in ("files tab", "save", "file")),
      save_schema["description"][:120])

filed = _tools.execute("save_to_files",
                       {"title": "PHYS checklist",
                        "content": "# Before the exam\n- Review Ch 2\n1. Sleep"})
check("filing free text works", filed.get("filed") == 1, str(filed)[:140])
check("the result names the file, so the reply can too",
      filed.get("saved_to_files") == "PHYS checklist.html",
      str(filed.get("saved_to_files")))

filed_html = _b64.b64decode(filed["files"][0]["dataUrl"].split(",", 1)[1]).decode()
check("markdown headings become headings", "<h2>Before the exam</h2>" in filed_html)
check("bullets become a list", "<ul><li>Review Ch 2</li></ul>" in filed_html)
check("numbered items become an ordered list", "<ol><li>Sleep</li></ol>" in filed_html)

by_course = _tools.execute("save_to_files", {"course": "PHYS 1361"})
check("filing an existing guide by course works",
      by_course.get("filed") == 1, str(by_course)[:140])
check("it reuses the stored guide rather than asking for it to be retyped",
      "study guide" in by_course.get("saved_to_files", ""),
      str(by_course.get("saved_to_files")))

check("filing with nothing to file is refused clearly",
      "error" in _tools.execute("save_to_files", {}))
check("a title with no content is refused",
      "error" in _tools.execute("save_to_files", {"title": "Empty"}))
check("a filename can't escape its directory",
      "/" not in _tools.execute(
          "save_to_files",
          {"title": "../../etc/passwd", "content": "x"}
      )["files"][0]["name"],
      "a model-supplied title becomes a filename")

# --- tools that make documents say so in their result ---
guide_result = _tools.execute("make_study_guide",
                              {"course": "PHYS 1351", "topics": ["Kinematics"]})
check("make_study_guide reports the file it created",
      guide_result.get("saved_to_files"), str(guide_result.get("saved_to_files")))
check("and which collection it's in",
      guide_result.get("collection") == "Study guides")

plan_result = _tools.execute("make_schedule", {"horizon_days": 7})
check("a schedule can be filed too", plan_result.get("files"),
      str(plan_result.get("saved_to_files")))
plan_html = _b64.b64decode(
    plan_result["files"][0]["dataUrl"].split(",", 1)[1]).decode()
check("the schedule file is grouped by day",
      plan_html.count("<section>") >= 2, str(plan_html.count("<section>")))

# --- filing is generic, not a hardcoded list of tools ---
invented = agent.board_actions([{
    "tool": "some_future_tool", "ok": True,
    "result": {"files": [{"name": "x.html", "dataUrl": "data:text/html;base64,eA=="}]}}])
check("any tool returning files gets its files filed",
      any(a["type"] == "addFiles" for a in invented), str(invented))
check("a file with no data is ignored",
      not [a for a in agent.board_actions([
          {"tool": "t", "ok": True, "result": {"files": [{"name": "x"}]}}])
          if a["type"] == "addFiles"])
check("a study guide isn't filed twice when the tool already returned a file",
      sum(len(a["items"]) for a in agent.board_actions([
          {"tool": "make_study_guide", "ok": True, "result": guide_result}])
          if a["type"] == "addFiles") == 1)

# --- the model gets told what's already in the tab ---
ctx = agent._context({}, None, None,
                     {"collections": [{"name": "Study guides"}],
                      "files": [{"name": "PHYS 1351 study guide.html"}]})
check("the model can see what's in the Files tab",
      ctx["files_tab"]["count"] == 1
      and "Study guides" in ctx["files_tab"]["collections"], str(ctx.get("files_tab")))
check("and no files means no files_tab noise in the context",
      "files_tab" not in agent._context({}, None, None, None))

check("the prompt tells the model it can write files",
      "save_to_files" in _orch.SYSTEM_PROMPT
      and "never tell a student you have no way to write files"
      in _orch.SYSTEM_PROMPT.lower(),
      "the prompt has to name the tool, or the model reasons from an "
      "out-of-date idea of what it can do")


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
