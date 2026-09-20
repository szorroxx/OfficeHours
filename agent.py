"""
The assistant. One prompt in; a reply, board writes, and HTML updates out.

THIS REPLACES backend/assistant.js
---------------------------------
That file was a placeholder whose mock conversation asked the student for
their Canvas username, then for a Duo two-factor code, then pretended to
sync. The real pipeline needs neither: orchestrator/canvas.py reads a Canvas
export (or a scoped access token), and orchestrator/tools.py refuses to store
anything matching 'password', 'token', or 'credential' at all. So the
credential flow is gone rather than ported. Everything else about that file
survives: its contract, its action types, and its item templates.

    input   {message, history, board, attachments}
    output  {reply, actions, surface, changes, trace}

`reply` and `actions` are byte-for-byte the shapes app.html already renders,
so the frontend needed no change to keep working. `surface`, `changes`, and
`trace` are additions: the HTML the agent put on the page, a plain-language
list of what it altered, and the tool calls it made.

THE ROUTE A PROMPT TAKES
------------------------
    prompt
      -> Nemotron picks tools          (orchestrator/orchestrator.py)
      -> tools run: read Tiger Data, and if it's stale or missing, crawl
         Canvas via Claude and write the rows back   (tools.py -> canvas.py -> db.py)
      -> Nemotron writes the answer
      -> Claude builds validated cards (display.py)
      -> Claude decides the page layout from those cards + the current HTML
         (surface.py)
      -> cards also become board rows, so Kenneth's panels fill in

Nemotron never touches the database and never emits HTML. It emits a tool
name; this project's Python runs the function. That asymmetry is the whole
design and it's why a confused model can't do much damage.
"""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import surface

# Tool results we know how to turn into board rows. Anything else still shows
# up in the reply and on the HTML surface; it just doesn't become a card on
# the board, because the board has four fixed columns.
_ASSIGNMENT_TOOLS = ("get_assignments", "get_overdue", "refresh_from_canvas",
                     "find_assignment")
_EVENT_TOOLS = ("get_events", "find_campus_events")
# Schedule blocks have to reach the board too, not just the schedule table.
#
# This was a real and very visible failure: a student asked for a viola lesson
# at 3pm Thursday, add_to_schedule wrote the row, the reply said it was added
# -- and it appeared nowhere on the dashboard, because the week view and the
# calendar read the board's four columns and a schedule block isn't in any of
# them. The student said "I don't see it on my dashboard" three times. An
# action that reports success and leaves no trace on screen is worse than one
# that fails.
_SCHEDULE_TOOLS = ("make_schedule", "add_to_schedule", "get_schedule")
_TASK_TOOLS = ("add_tasks",)
_REMOVAL_TOOLS = ("remove_from_schedule", "remove_events")
# Tools whose output is a DOCUMENT, not a board row. These go to the Files
# tab, which is where the UI says the assistant's generated files land.
_DOCUMENT_TOOLS = ("make_study_guide", "get_study_sets")


def handle(message: str, history: list[dict] | None = None,
           board: dict | None = None, attachments: list[dict] | None = None,
           current_surface: list[dict] | None = None,
           student_id: str | None = None) -> dict:
    """
    Run one turn. Never raises: a failure comes back as a reply the student
    can read, because a 500 in the chat box during a demo tells nobody
    anything.
    """
    import orchestrator

    message = str(message or "").strip()
    if not message and attachments:
        message = "I've attached some files. Take them into account."
    if not message:
        return {"reply": "Ask me what's due, or to check Canvas for new work.",
                "actions": [], "surface": current_surface or [],
                "changes": {}, "trace": {}}

    warning = _credential_warning(message)
    if warning:
        # Answer the safety point and stop. Not a refusal to help -- it says
        # what to do instead -- but the prompt does not go on to the model
        # with a password sitting in it.
        return {"reply": warning, "actions": [], "surface": current_surface or [],
                "changes": {"blocked": "message looked like it contained a credential"},
                "trace": {}}

    context = _context(board, attachments, history)

    try:
        run = orchestrator.run(message, channel="web", context=context)
    except Exception as exc:  # noqa: BLE001
        return {
            "reply": f"I hit an error before I could answer: {type(exc).__name__}. "
                     f"The rest of your board is untouched.",
            "actions": [], "surface": current_surface or [],
            "changes": {"error": str(exc)[:200]}, "trace": {},
        }

    payload = run.to_json()
    display = payload.get("display") or {}
    cards = display.get("cards") or []
    summary = payload.get("summary") or display.get("speech") or ""

    # 1. Board writes, from the tool results rather than from the prose.
    actions = board_actions(run.steps)

    # 2. The HTML surface: poll what's there, plan, apply.
    chunks, surface_report = surface.update(
        current=current_surface or [],
        prompt=message,
        summary=summary,
        cards=cards,
    )

    reply = summary or display.get("speech") or "Done."
    if payload.get("error"):
        reply = f"{reply}\n\n(Something went wrong mid-run: {payload['error']})"

    # Catch the model claiming a change it didn't make.
    #
    # The prompt now forbids this, but a prompt rule is a request and this is
    # a check. It exists because of a specific failure: asked twice to take a
    # group assignment off an overdue list, the model replied that the item
    # "has already been submitted by your groupmate, so it isn't considered
    # overdue" -- untrue, contradicted by the rows it had just read, and
    # nothing changed. The student had no way to tell that from a real
    # confirmation.
    #
    # So: if they asked for a change, no write tool ran, and the reply talks
    # as though one did, append a correction. Conservative on purpose -- it
    # only fires when the write set is completely empty.
    correction = _unbacked_claim(message, reply, run.steps)
    if correction:
        reply = f"{reply}\n\n{correction}"

    return {
        "reply": reply,
        "actions": actions,
        "surface": chunks,
        "changes": _changes(actions, surface_report, run.steps),
        "trace": payload.get("trace") or {},
        "display": display,
    }


# --------------------------------------------------------------------------
# Credentials: refused at the door
# --------------------------------------------------------------------------

# Note on the anchoring: an earlier version wrapped the whole alternation in
# \b...\b, which meant "password:" only matched when a word character came
# straight after the colon -- so "my password is x" was caught and
# "password: letmein" sailed through, because a space isn't a word character.
# The colon/equals forms now end the pattern themselves.
_CREDENTIAL_HINTS = re.compile(
    r"(\bmy password is\b|\bpassword\s*[:=]|\bpasswd\b|\bpwd\s*[:=]|"
    r"\bmy pin is\b|\bpin\s*[:=]|"
    r"\bduo (?:push |)code\b|\b2fa code\b|\btwo[- ]factor code\b|"
    r"\bverification code is\b|\bone[- ]time (?:code|password)\b|"
    r"\bssn\b|\bsocial security number\b)",
    re.IGNORECASE,
)


def _credential_warning(message: str) -> str | None:
    if not _CREDENTIAL_HINTS.search(message):
        return None
    return (
        "I'm not going to take a password or a verification code, and nothing "
        "like that gets stored here. I don't need one either: I read your "
        "coursework from the Canvas export already loaded on the server. If "
        "you want me reading Canvas live instead, generate an access token "
        "(Canvas → Account → Settings → New Access Token) and put it in the "
        "server's .env, where you can revoke it any time. Ask me what's due "
        "and I'll go look."
    )


# --------------------------------------------------------------------------
# Guard against claimed-but-unmade changes
# --------------------------------------------------------------------------

# Tools that change something. If none of these ran, nothing changed.
WRITE_TOOLS = frozenset({
    "refresh_from_canvas", "make_schedule", "add_to_schedule",
    "make_study_guide", "find_campus_events", "update_preferences",
    "log_time", "update_assignment", "add_tasks",
    "remove_from_schedule", "remove_events",
    "delete_assignments", "restore_assignments",
})

_ASKED_FOR_CHANGE = re.compile(
    r"\b(add|added|remove|removed|delete|deleted|drop|take (?:it |that |them )?off|"
    r"mark|marked|update|updated|change|changed|schedule|reschedule|"
    r"set|clear|move|rename|put|track|remind|make me|create)\b",
    re.IGNORECASE,
)
_CLAIMED_A_CHANGE = re.compile(
    r"\b(i(?:'ve| have)? (?:added|removed|deleted|updated|marked|scheduled|"
    r"saved|set|created|put|moved|cleared)|"
    r"(?:has|have|is|are|was|were) been (?:added|removed|deleted|updated|"
    r"marked|scheduled|saved|submitted)|"
    r"i(?:'ve| have)? (?:now )?(?:put|placed)|"
    r"(?:it|that|this|they)(?:'s| is| are) (?:now )?(?:added|removed|on your|"
    r"in your|off your|marked))\b",
    re.IGNORECASE,
)


# The other shape the failure took, and the more damaging one: rather than
# claiming to have acted, the model explains the request away by asserting the
# item is ALREADY in the state the student asked for. Asked to drop a group
# preproposal from an overdue list, it said the item "has already been
# submitted by your groupmate, so it isn't considered overdue" and that
# "there's nothing to remove" -- while the same assignment sat in the rows it
# had just read, and turned up again two messages later. No action is claimed,
# so the pattern above misses it entirely, and the student is told their
# request was unnecessary instead of being told it wasn't done.
_EXPLAINED_AWAY = re.compile(
    r"(already (?:been )?(?:submitted|marked|removed|completed|handled|done)|"
    r"nothing to (?:remove|delete|change|do)|"
    r"(?:is|are|it)(?:n't| not) (?:considered|listed|included|showing)|"
    r"no longer (?:appears|appear|considered|on|an? )|"
    r"(?:doesn't|does not|won't|will not) appear|"
    r"simply (?:doesn't|does not)|"
    r"there (?:is|are) (?:currently )?(?:no|none|nothing))",
    re.IGNORECASE,
)


def _unbacked_claim(message: str, reply: str, steps: list[dict]) -> str | None:
    """Return a correction to append, or None if the reply is honest."""
    wrote = [s for s in steps or []
             if s.get("ok") and s.get("tool") in WRITE_TOOLS]
    if wrote:
        return None
    if not _ASKED_FOR_CHANGE.search(message or ""):
        return None
    claimed = bool(_CLAIMED_A_CHANGE.search(reply or ""))
    explained_away = bool(_EXPLAINED_AWAY.search(reply or ""))
    if not claimed and not explained_away:
        return None

    tried = sorted({s.get("tool") for s in steps or []
                    if s.get("tool") in WRITE_TOOLS})
    if tried:
        return ("**Correction: that didn't go through.** I tried "
                f"({', '.join(tried)}) and it failed, so nothing on your "
                "board or schedule changed. Worth trying again.")

    if explained_away and not claimed:
        return ("**Correction: nothing changed, and I shouldn't have implied "
                "it was already handled.** I only read your data this turn. "
                "If you can still see the item on your dashboard, it's still "
                "there — tell me its title and I'll mark it dismissed, which "
                "takes it off your open and overdue lists without pretending "
                "you submitted it.")

    return ("**Correction: I didn't actually change anything.** I only read "
            "your data this turn — no write ran, so your board and schedule "
            "are exactly as they were. If you want me to make that change, "
            "say so directly and I'll use the tool for it; if I don't have "
            "one, I'll tell you.")


# --------------------------------------------------------------------------
# Context handed to the orchestrator
# --------------------------------------------------------------------------


def _context(board: dict | None, attachments: list[dict] | None,
             history: list[dict] | None) -> dict:
    """
    What the model gets to know besides the prompt.

    Deliberately a summary, not the raw board: counts and titles are enough
    for it to say "you already have that one" without paying to re-read every
    row, and a board with 300 rows would otherwise crowd out the tool results.
    """
    board = board or {}
    context: dict[str, Any] = {
        "today": datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes"),
        "timezone": os.getenv("TIMEZONE", os.getenv("STUDENT_TZ", "America/New_York")),
        "times_are_local": True,
        "board_counts": {kind: len(board.get(kind) or [])
                         for kind in ("assignments", "exams", "events", "todos")},
        "board_titles": [str(row.get("title") or row.get("text") or "")[:60]
                         for kind in ("assignments", "exams")
                         for row in (board.get(kind) or [])][:25],
    }

    if attachments:
        # File CONTENTS are not sent to the orchestrator. Names and types are
        # enough for it to mention them; extracting text from an uploaded PDF
        # is a separate job (see /api/files in app.py) and doing it silently
        # here would mean every chat message carried a base64 blob.
        context["attachments"] = [
            {"name": str(a.get("name") or "")[:120],
             "type": str(a.get("type") or "")[:60],
             "size": a.get("size")}
            for a in attachments[:10]
        ]

    if history:
        # Twelve turns, not four. Four was enough for one follow-up question
        # and not enough for "what have we talked about", which got answered
        # with a recap that started three messages ago and read as amnesia.
        context["recent_turns"] = [
            {"role": turn.get("role"), "content": str(turn.get("content") or "")[:400]}
            for turn in history[-12:]
        ]
        context["turns_before_this"] = max(0, len(history) - 12)
    return context


# --------------------------------------------------------------------------
# Tool results -> board rows
# --------------------------------------------------------------------------


def board_actions(steps: list[dict]) -> list[dict]:
    """
    Build the board writes from what the tools actually returned.

    Reading the tool results rather than the model's prose is the point: the
    board can only ever contain rows that came out of Tiger Data or the
    crawler. There is no path where the assistant talks a row onto your
    dashboard.

    Assignments whose kind is 'exam' go to the exams column, because that is
    the split app.html's UI makes; everything else is an assignment.
    """
    assignments: list[dict] = []
    exams: list[dict] = []
    events: list[dict] = []
    todos: list[dict] = []
    completed: list[dict] = []
    removed: list[dict] = []
    documents: list[dict] = []
    seen: set[str] = set()

    for step in steps or []:
        if not step.get("ok"):
            continue
        result = step.get("result")
        if not isinstance(result, dict):
            continue
        tool = step.get("tool")

        if tool in _ASSIGNMENT_TOOLS:
            for row in result.get("items") or []:
                item = _assignment_row(row)
                if not item or item["canvasId"] in seen:
                    continue
                seen.add(item["canvasId"])
                (exams if _is_exam(row) else assignments).append(item)

        elif tool in _EVENT_TOOLS:
            for row in result.get("items") or []:
                item = _event_row(row)
                if item and item["canvasId"] not in seen:
                    seen.add(item["canvasId"])
                    events.append(item)

        elif tool in _SCHEDULE_TOOLS:
            # Study blocks and fixed commitments both land in the events
            # column, which is what the calendar and the week strip render.
            # source='schedule' keeps them distinguishable from Canvas events.
            for row in result.get("blocks") or []:
                item = _schedule_row(row)
                if item and item["canvasId"] not in seen:
                    seen.add(item["canvasId"])
                    events.append(item)

        elif tool in _DOCUMENT_TOOLS:
            for guide in _guides_in(result):
                document = _study_guide_file(guide)
                if document:
                    documents.append(document)

        elif tool in _TASK_TOOLS:
            for row in result.get("tasks") or []:
                item = _task_row(row)
                if item and item["canvasId"] not in seen:
                    seen.add(item["canvasId"])
                    todos.append(item)

        elif tool in _REMOVAL_TOOLS:
            # A delete in Tiger Data has to delete the board row too, or the
            # thing the student asked you to remove stays on screen and the
            # reply is a lie by omission.
            for row in (result.get("blocks") or []):
                task = str(row.get("task") or "").strip()
                starts = _as_iso(row.get("starts_at"))
                if task and starts:
                    removed.append({"canvasId": f"sched:{task[:60]}:{starts[:16]}",
                                    "title": task[:200], "kind": "events"})
            for row in (result.get("events") or []):
                title = str(row.get("title") or "").strip()
                if title:
                    removed.append({"canvasId": str(row.get("id") or "")[:160],
                                    "title": title[:200], "kind": "events"})

        elif tool == "update_assignment":
            # 'dismissed' means "this is not mine to do" -- the student wants
            # it GONE, so the board row goes. 'submitted'/'graded' mean they
            # did the work, so the row is ticked and kept.
            #
            # This distinction is the whole bug from the last round. Dismissed
            # items were being ticked, the dashboard renders ticked items with
            # a line through them rather than hiding them, and a student who
            # asked three times was told to clear their browser cache. Marking
            # is not removing.
            for row in result.get("items") or []:
                status = row.get("status")
                if status == "dismissed":
                    removed.append({
                        "canvasId": str(row.get("id") or "")[:120],
                        "title": str(row.get("title") or "")[:200],
                    })
                elif status in ("submitted", "graded"):
                    completed.append({
                        "canvasId": str(row.get("id") or "")[:120],
                        "title": str(row.get("title") or "")[:200],
                        "completed": True,
                        "note": f"marked {status} by the assistant",
                    })

        elif tool in ("delete_assignments",):
            for row in result.get("items") or []:
                removed.append({
                    "canvasId": str(row.get("id") or "")[:120],
                    "title": str(row.get("title") or "")[:200],
                })

    actions = []
    if assignments:
        actions.append({"type": "addAssignments", "items": assignments[:40]})
    if exams:
        actions.append({"type": "addExams", "items": exams[:20]})
    if events:
        actions.append({"type": "addEvents", "items": events[:40]})
    if todos:
        actions.append({"type": "addTodos", "items": todos[:30]})
    if completed:
        # Upserts on canvasId, so this ticks the existing row rather than
        # adding a second copy of it.
        actions.append({"type": "completeItems", "items": completed[:40]})
    if removed:
        actions.append({"type": "removeItems", "items": removed[:200]})
    if documents:
        actions.append({"type": "addFiles", "items": documents[:10]})

    # A block that was just deleted must not be re-added by a get_schedule in
    # the same turn -- "remove the 3pm lesson, then show me my schedule" runs
    # both, and the read can return stale rows from before the delete.
    if removed:
        gone = {r["canvasId"] for r in removed}
        for action in actions:
            if action["type"] in ("addEvents", "addAssignments", "addExams"):
                action["items"] = [i for i in action["items"]
                                   if i.get("canvasId") not in gone]
        actions = [a for a in actions
                   if a["items"] or a["type"] == "removeItems"]
    return actions


def _is_exam(row: dict) -> bool:
    kind = str(row.get("kind") or "").lower()
    if kind in ("exam", "quiz", "midterm", "final"):
        return True
    return bool(re.search(r"\b(exam|midterm|final)\b",
                          str(row.get("title") or ""), re.IGNORECASE))


def _assignment_row(row: dict) -> dict | None:
    title = str(row.get("title") or "").strip()
    if not title:
        return None
    hours = _as_float(row.get("est_hours"))
    # dueISO is OMITTED when there isn't one, rather than set to null.
    #
    # This one was ugly on screen: the frontend runs date arithmetic on
    # dueISO, and `new Date(null)` is the Unix epoch, so an assignment with no
    # due date -- MUSIC 0620's "Roll Call Attendance" -- rendered as "Overdue
    # by 20716 days" at the top of the dashboard. A missing key is falsy and
    # gets the "no due date" branch; a null is a date in 1970.
    due = _as_iso(row.get("due_at"))
    item = {
        "title": title[:200],
        "course": str(row.get("course_code") or row.get("course") or "")[:80],
        # The orchestrator's ids are deterministic (db.make_id hashes student +
        # course + title), so using one as canvasId is what makes a re-crawl
        # update the row instead of adding a second copy of it.
        "canvasId": str(row.get("id") or f"oh:{title}")[:120],
        "source": "canvas",
    }
    if due:
        item["dueISO"] = due
    if hours:
        item["estimateMins"] = int(round(hours * 60))
    if row.get("status") in ("submitted", "graded"):
        item["completed"] = True
    if row.get("location"):
        item["location"] = str(row["location"])[:120]
    return item


def _schedule_row(row: dict) -> dict | None:
    """A schedule block, as a board event so it shows up on the calendar."""
    task = str(row.get("task") or "").strip()
    starts = _as_iso(row.get("starts_at") or row.get("start"))
    if not task or not starts:
        return None
    return {
        "title": task[:200],
        "startISO": starts,
        "endISO": _as_iso(row.get("ends_at") or row.get("end")),
        "estimateMins": row.get("est_minutes") or None,
        # Deterministic, so re-reading the schedule updates the same rows
        # instead of stacking duplicates every time get_schedule runs.
        "canvasId": f"sched:{task[:60]}:{starts[:16]}",
        "source": "schedule",
        "location": str(row.get("location") or "")[:120] or None,
    }


def _guides_in(result: dict) -> list[dict]:
    """
    Pull study sets out of a tool result.

    make_study_guide returns one guide inline; get_study_sets returns a list
    of stored rows whose content is nested under `content`. Both shapes end up
    here so a guide written last week can be re-filed as easily as one made a
    second ago.
    """
    if result.get("sections"):
        return [result]
    guides = []
    for row in result.get("items") or result.get("study_sets") or []:
        if not isinstance(row, dict):
            continue
        content = row.get("content") if isinstance(row.get("content"), dict) else row
        if content.get("sections"):
            guides.append({**content,
                           "course": row.get("course_code") or content.get("course"),
                           "topic": row.get("topic"),
                           "format": row.get("format") or content.get("format")})
    return guides


def _study_guide_file(guide: dict) -> dict | None:
    """
    Render a study set as a standalone HTML file for the Files tab.

    HTML rather than markdown or a PDF: the frontend opens a file by turning
    its data URL into a blob and opening that in a tab, so HTML displays
    immediately with no reader, no dependency, and no print step. Everything
    interpolated is escaped -- this is model output being written to a file
    the student will open in a browser.
    """
    sections = guide.get("sections") or []
    if not sections:
        return None

    course = str(guide.get("course") or "").strip()
    topic = str(guide.get("topic") or "").strip()
    title = " ".join(part for part in [course, "study guide"] if part) or "Study guide"

    def esc(value: object) -> str:
        return (str(value or "")
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))

    body = []
    for section in sections[:20]:
        body.append(f"<section><h2>{esc(section.get('topic'))}</h2>")
        summary = str(section.get("summary") or "").strip()
        if summary:
            body.append(f"<p>{esc(summary)}</p>")
        questions = [q for q in (section.get("questions") or []) if q][:12]
        if questions:
            body.append("<h3>Practice questions</h3><ol>")
            body.extend(f"<li>{esc(q)}</li>" for q in questions)
            body.append("</ol>")
        body.append("</section>")

    made = datetime.now(timezone.utc).astimezone().strftime("%d %b %Y, %H:%M")
    document = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
          max-width: 46rem; margin: 3rem auto; padding: 0 1.5rem 4rem;
          color: #1B1F3B; line-height: 1.6; }}
  h1 {{ font-size: 1.6rem; margin-bottom: .25rem; }}
  .sub {{ color: #6B7280; font-size: .9rem; margin-bottom: 2.5rem; }}
  section {{ border-top: 1px solid #E6E7EB; padding-top: 1.5rem;
             margin-top: 2rem; }}
  h2 {{ font-size: 1.15rem; margin-bottom: .5rem; }}
  h3 {{ font-size: .8rem; text-transform: uppercase; letter-spacing: .05em;
        color: #6B7280; margin: 1.25rem 0 .5rem; }}
  ol {{ padding-left: 1.4rem; }} li {{ margin-bottom: .4rem; }}
  @media print {{ body {{ margin: 0; }} }}
</style></head>
<body>
<h1>{esc(title)}</h1>
<div class="sub">{esc(topic) + " &middot; " if topic else ""}{len(sections)} topic{"s" if len(sections) != 1 else ""} &middot; made by Office Hours, {esc(made)}</div>
{"".join(body)}
</body></html>"""

    encoded = base64.b64encode(document.encode("utf-8")).decode("ascii")
    filename = (f"{course} study guide.html" if course else "Study guide.html")
    return {
        "name": filename,
        "type": "text/html",
        "size": len(document.encode("utf-8")),
        "dataUrl": f"data:text/html;base64,{encoded}",
        "collectionName": "Study guides",
    }


def _task_row(row: dict) -> dict | None:
    text = str(row.get("text") or "").strip()
    if not text:
        return None
    item = {"text": text[:300], "canvasId": f"task:{text[:80]}",
            "source": "assistant", "done": False}
    if row.get("est_minutes"):
        item["estimateMins"] = row["est_minutes"]
    if row.get("due_at"):
        item["dueISO"] = _as_iso(row["due_at"])
    return item


def _event_row(row: dict) -> dict | None:
    title = str(row.get("title") or "").strip()
    if not title:
        return None
    starts = _as_iso(row.get("starts_at") or row.get("when"))
    if not starts:
        # An event with no time can't be placed on a calendar, and a null
        # startISO renders as 1970 for the same reason dueISO did.
        return None
    return {
        "title": title[:200],
        "location": str(row.get("location") or row.get("where") or "")[:120],
        "startISO": starts,
        "canvasId": str(row.get("id") or row.get("url") or f"ev:{title}")[:160],
        "source": "canvas",
        "url": str(row.get("url") or "")[:400] or None,
    }


def _as_iso(value: object) -> str | None:
    """
    Normalise a timestamp for the board, in the student's timezone.

    The instant is the same either way -- the browser renders any offset in
    local time -- but emitting local offsets keeps the board rows readable and
    consistent with the cards. Two representations of one moment in two parts
    of the same response is how "is it 2pm or 7pm?" becomes a question at all.
    """
    if not value:
        return None
    parsed = value if isinstance(value, datetime) else None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if parsed.tzinfo is not None:
        try:
            import db

            parsed = parsed.astimezone(db._tz())
        except Exception:  # noqa: BLE001
            pass
    return parsed.isoformat()


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# "What changed", in words
# --------------------------------------------------------------------------


def _changes(actions: list[dict], surface_report: dict,
             steps: list[dict]) -> dict:
    """
    The visible record of what this turn did.

    The spec asks for the agent's changes to be visible alongside its reply,
    and that has to include the parts that DIDN'T happen: a chunk the server
    refused to remove, or markup that got stripped, is exactly the kind of
    thing that otherwise turns into "why does the page look like that".
    """
    board_summary = []
    # Singular/plural pairs. The old version appended an "s" to whatever the
    # label was, which produced "Checked 3 completeItemss, already on your
    # board" in the change log -- a double plural on an internal action name.
    labels = {"addAssignments": ("assignment", "assignments"),
              "addExams": ("exam", "exams"),
              "addEvents": ("event", "events"),
              "addTodos": ("to-do", "to-dos"),
              "completeItems": ("item ticked off", "items ticked off"),
              "removeItems": ("item removed", "items removed"),
              "addFiles": ("file in your Files tab",
                           "files in your Files tab")}
    for action in actions:
        count = len(action.get("items") or [])
        if count:
            singular, plural = labels.get(action["type"],
                                          (action["type"], action["type"]))
            board_summary.append(f"{count} {singular if count == 1 else plural}")

    writes = [step["tool"] for step in steps or []
              if step.get("ok") and step.get("tool") in
              ("refresh_from_canvas", "make_schedule", "add_to_schedule",
               "make_study_guide", "find_campus_events", "update_preferences",
               "log_time", "update_assignment", "add_tasks",
               "remove_from_schedule", "remove_events",
               "delete_assignments", "restore_assignments",
               "make_study_guide")]

    # Tools that FAILED, with the actual error. The student was told four
    # times that "the scheduling tool encountered an internal error (missing
    # dependency)" and, when they asked which dependency, got an invented
    # answer. The real message was
    # "ModuleNotFoundError: No module named 'anthropic'" -- one line that
    # names the fix. Paraphrasing an error through a language model loses
    # exactly the part you need.
    failures = []
    for step in steps or []:
        if step.get("ok"):
            continue
        detail = str(step.get("result_preview") or "")
        match = re.search(r'"error"\s*:\s*"(.*?)"', detail)
        failures.append({
            "tool": step.get("tool"),
            "error": (match.group(1) if match else detail)[:300],
        })

    return {
        "board": board_summary,
        "failures": failures,
        "tiger_data_writes": sorted(set(writes)),
        "surface": {
            "added": surface_report.get("added", []),
            "updated": surface_report.get("updated", []),
            "removed": surface_report.get("removed", []),
            "kept_despite_request": surface_report.get("refused", []),
            "sanitized": surface_report.get("sanitized", []),
            "note": surface_report.get("note", ""),
        },
        "tools_run": [step.get("tool") for step in steps or []],
    }


# --------------------------------------------------------------------------
# Canvas sync without the chat (the "refresh" button)
# --------------------------------------------------------------------------


def sync_canvas(pages: list[str] | None = None) -> dict:
    """
    Run the crawler directly and return board items. Rowan's syncCanvas(),
    with an implementation behind it.
    """
    import tools

    crawl = tools.refresh_from_canvas(pages)
    if "error" in crawl:
        return {"assignments": [], "exams": [], "events": [], "error": crawl["error"]}

    stored = tools.get_assignments(status="open")
    rows = stored.get("items") or []
    assignments = [item for row in rows
                   if not _is_exam(row) and (item := _assignment_row(row))]
    exams = [item for row in rows if _is_exam(row) and (item := _assignment_row(row))]

    events = [item for row in (tools.get_events().get("items") or [])
              if (item := _event_row(row))]

    return {"assignments": assignments, "exams": exams, "events": events,
            "crawled": crawl}


if __name__ == "__main__":
    os.environ.setdefault("MODE", "mock")
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "orchestrator"))
    out = handle(sys.argv[1] if len(sys.argv) > 1 else "what's due this week?")
    print(json.dumps({k: v for k, v in out.items() if k != "surface"},
                     indent=2, default=str)[:3000])
    print(f"\n{len(out['surface'])} surface chunk(s)")
