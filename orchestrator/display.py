"""
The display agent. Turns a finished run into data the website can render.

THIS FILE IS THE CONTRACT WITH KENNETH. Freeze it early, paste it in the team
channel, then neither of you has to read the other's code.

RIGHT NOW: this returns structured card DATA, not HTML.
LATER:     when Kenneth's templates exist, fill them in render_html() at the
           bottom. Nothing else in the project changes.

Why data and not HTML:
  - Generating HTML per request is several seconds of latency, every request.
  - You get slightly different markup each time, so his CSS breaks randomly.
  - The prompt box means a user's text reaches a model whose output you render.
    If that output can contain markup, that's a script-injection hole in the
    thing you're projecting in front of judges.

So: the model picks from a CLOSED SET of card types and fills in their fields.
validate() throws away anything that isn't in the set. That means Kenneth's
frontend can trust every card it receives and needs no defensive code.
"""

from __future__ import annotations

import config  # noqa: F401  - loads .env before anything reads it
import os

import cache

MODE = os.getenv("MODE", "mock").lower()
TEMPLATE_DIR = os.getenv("TEMPLATE_DIR", "templates")

# --------------------------------------------------------------------------
# THE CONTRACT: card type -> the fields it must have
# --------------------------------------------------------------------------

CARD_TYPES: dict[str, set[str]] = {
    # items: [{title, course, due, priority, est_minutes}]
    "assignment_list": {"title", "items"},
    # blocks: [{day, start, end, task, est_minutes}]
    "schedule": {"title", "blocks"},
    # sections: [{topic, summary, questions[]}]
    "study_set": {"title", "sections"},
    # items: [{title, when, where}]
    "event_list": {"title", "items"},
    # series: [{label, points: [{x, y}]}]  -- the Tiger Data trend chart
    "workload_chart": {"title", "series"},
    "text": {"title", "body"},
    "alert": {"title", "body"},
}

MAX_CARDS = 4

RENDER_CONTRACT = """Return ONLY a JSON object. No prose, no markdown fences:

{
  "speech": "<1-2 sentences, read aloud by a voice assistant. No markdown, no lists, no URLs.>",
  "headline": "<max 8 words, shown at the top of the dashboard>",
  "cards": [ ... ]
}

Each card must be exactly one of these shapes:
  {"type":"assignment_list","title":str,"items":[{"title":str,"course":str,"due":str,"priority":int,"est_minutes":int}]}
  {"type":"schedule","title":str,"blocks":[{"day":str,"start":str,"end":str,"task":str,"est_minutes":int}]}
  {"type":"study_set","title":str,"sections":[{"topic":str,"summary":str,"questions":[str]}]}
  {"type":"event_list","title":str,"items":[{"title":str,"when":str,"where":str}]}
  {"type":"workload_chart","title":str,"series":[{"label":str,"points":[{"x":str,"y":number}]}]}
  {"type":"text","title":str,"body":str}
  {"type":"alert","title":str,"body":str}

Rules:
- Only include cards backed by real data in the tool results. Never invent rows.
- "due" and "when" are human-readable, e.g. "Mon 10:00am" or "Sep 22, 11:59pm".
- Order cards by what the student most needs to see first.
- At most 4 cards.
- If the tool results are empty or all errored, return one "alert" card saying so.
- "speech" must stand alone: it is everything a voice user hears.
- Output plain text only. Never emit HTML tags, <script>, or markdown."""


def render_spec(prompt: str, summary: str, steps: list[dict],
                channel: str = "web") -> dict:
    """
    Ask Claude how to lay out the result, then validate hard.

    If Claude is unavailable -- bad API key, no network, rate limited -- we fall
    back to building cards in plain Python from the tool results. The display
    agent makes the layout smarter; it is NOT allowed to be the reason a
    correct answer never reaches the screen.
    """
    fallback = fallback_spec(summary, steps)

    payload = {
        "user_asked": prompt,
        "orchestrator_summary": summary,
        "tool_results": [
            {"tool": s["tool"], "arguments": s["arguments"],
             "result": s.get("result", s["result_preview"])}
            for s in steps
        ],
        "channel": channel,
    }

    # The label carries which tools ran. Two reasons: in live mode it keeps
    # the cache key honest (a schedule result and an assignment result are
    # different requests and shouldn't collide), and in mock mode it's what
    # lets the fixture return a card that matches what actually happened.
    # Without it the mock returned the same assignment list for every prompt,
    # which then made the layout agent look like it was ignoring the data
    # when it was really being handed the same data every time.
    ran = "+".join(sorted({s["tool"] for s in steps})) or "none"

    try:
        spec = cache.claude(
            system="You decide how a study-assistant dashboard renders a result.\n\n"
                   + RENDER_CONTRACT,
            user=_json(payload),
            max_tokens=3000,
            label=f"display:{channel}:{ran}",
        )
    except Exception as exc:  # noqa: BLE001
        note = _explain(exc)
        print(f"[display] Claude unavailable, built cards locally: {note}")
        fallback["_display_note"] = note
        return fallback

    if "error" in spec:
        fallback["_display_note"] = str(spec["error"])[:200]
        return fallback

    validated = validate(spec, fallback_summary=summary)
    # If validation threw everything away, the deterministic version beats a
    # lone "Summary" card.
    if (len(validated["cards"]) == 1
            and validated["cards"][0]["type"] == "text"
            and len(fallback["cards"]) > 1):
        fallback["speech"] = validated["speech"]
        return fallback
    return validated


def _explain(exc: Exception) -> str:
    """Turn an SDK exception into something worth reading."""
    text = str(exc)
    status = getattr(exc, "status_code", None)
    lowered = text.lower()
    if status == 401 or "invalid x-api-key" in lowered or "authentication" in lowered:
        return ("ANTHROPIC_API_KEY is invalid or missing. Get one at "
                "console.anthropic.com and put it in .env")
    if status == 429 or "rate limit" in lowered:
        return "Anthropic rate limit hit"
    if "credit" in lowered or "quota" in lowered:
        return "Anthropic account is out of credit"
    return f"{type(exc).__name__}: {text[:140]}"


# --------------------------------------------------------------------------
# Deterministic fallback: cards without a model
# --------------------------------------------------------------------------


def fallback_spec(summary: str, steps: list[dict]) -> dict:
    """
    Build cards directly from the tool results, in plain Python.

    No model, no cost, no latency, identical output every time. Each tool has a
    known result shape, so the mapping is mechanical. Worth having for three
    reasons: the site works with no Anthropic key at all, a rate limit can't
    break your demo, and it is the obvious thing to fall back to.
    """
    cards: list[dict] = []
    for step in steps:
        if not step.get("ok"):
            continue
        data = _result_of(step)
        if data is None:
            continue
        card = _card_for(step["tool"], data)
        if card:
            cards.append(card)

    if not cards:
        cards = [{"type": "text", "title": "Result",
                  "body": summary or "Nothing to show."}]

    return validate(
        {"speech": summary, "headline": _headline(cards, summary), "cards": cards},
        fallback_summary=summary,
    )


def _result_of(step: dict) -> dict | None:
    """
    Get the tool's result as a dict.

    Prefer step["result"], which is the real object. step["result_preview"] is
    truncated to 400 chars for the UI trace, so parsing that silently failed
    on anything longer -- which was every multi-item result.
    """
    import json

    result = step.get("result")
    if isinstance(result, dict):
        return result

    raw = step.get("result_preview") or ""
    if raw.endswith("..."):
        return None  # genuinely truncated, nothing to parse
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _card_for(tool: str, data: dict) -> dict | None:
    items = data.get("items") or []

    if tool == "get_overdue":
        if not items:
            return {"type": "alert", "title": "Nothing overdue",
                    "body": "Everything with a due date is still ahead of you."}
        return {"type": "assignment_list", "title": f"Overdue ({len(items)})",
                "items": [_assignment_row(a, late=True) for a in items[:12]]}

    if tool == "get_assignments":
        if not items:
            return {"type": "alert", "title": "No assignments stored",
                    "body": "Try refreshing from Canvas."}
        return {"type": "assignment_list", "title": "Assignments",
                "items": [_assignment_row(a) for a in items[:12]]}

    if tool in ("get_schedule", "make_schedule"):
        blocks = data.get("blocks") or []
        if not blocks:
            return None
        return {"type": "schedule", "title": "Your plan",
                "blocks": [{
                    "day": _day(b.get("starts_at") or b.get("day")),
                    "start": _time(b.get("starts_at") or b.get("start")),
                    "end": _time(b.get("ends_at") or b.get("end")),
                    "task": str(b.get("task", ""))[:120],
                    "est_minutes": _int(b.get("est_minutes")),
                } for b in blocks[:20]]}

    if tool == "make_study_guide":
        sections = data.get("sections") or []
        if not sections:
            return None
        return {"type": "study_set",
                "title": ("Study set: " + str(data.get("course", ""))).strip(": "),
                "sections": [{
                    "topic": str(s.get("topic", ""))[:120],
                    "summary": str(s.get("summary", ""))[:1000],
                    "questions": [str(q)[:300] for q in (s.get("questions") or [])][:6],
                } for s in sections[:6]]}

    if tool == "find_campus_events":
        if not items:
            return None
        return {"type": "event_list",
                "title": f"Campus events ({data.get('source', 'live')})",
                "items": [{"title": str(e.get("title", ""))[:120],
                           "when": _when(e.get("starts_at")),
                           "where": str(e.get("location") or "")[:80]}
                          for e in items[:10]]}

    if tool == "fetch_page":
        text = str(data.get("text") or "")
        # Strip the untrusted-content markers before showing a human.
        for marker in ("--- BEGIN UNTRUSTED WEB CONTENT ---",
                       "--- END UNTRUSTED WEB CONTENT ---"):
            text = text.replace(marker, "")
        body = "\n".join(
            line for line in text.splitlines()
            if line.strip() and not line.startswith("The text below was")
        )[:900]
        if not body:
            return None
        return {"type": "text",
                "title": f"From {str(data.get('url', ''))[:60]}", "body": body}

    if tool == "get_events":
        if not items:
            return None
        return {"type": "event_list", "title": "On campus",
                "items": [{"title": str(e.get("title", ""))[:120],
                           "when": _when(e.get("starts_at")),
                           "where": str(e.get("location") or "")[:80]}
                          for e in items[:10]]}

    if tool == "get_workload_history":
        points = data.get("points") or []
        if not points:
            return None
        by_course: dict[str, list] = {}
        for pt in points:
            by_course.setdefault(str(pt.get("course_code", "?")), []).append(
                {"x": str(pt.get("day"))[:10], "y": _float(pt.get("est_hours"))})
        return {"type": "workload_chart", "title": "Estimated hours over time",
                "series": [{"label": c, "points": p}
                           for c, p in list(by_course.items())[:6]]}

    if tool == "check_freshness":
        stale = [c for c in (data.get("courses") or []) if c.get("stale")]
        if not stale:
            return None
        names = ", ".join(str(c.get("course")) for c in stale[:5])
        return {"type": "alert", "title": "Data may be out of date",
                "body": f"{len(stale)} course(s) not refreshed recently: {names}"}

    if tool == "refresh_from_canvas":
        note = data.get("note") or f"Synced {data.get('synced', 0)} assignments."
        return {"type": "text", "title": "Refreshed from Canvas",
                "body": str(note)[:400]}

    if tool == "update_preferences":
        saved = data.get("prefs_keys") or []
        body = f"Saved: {', '.join(map(str, saved))}." if saved else "Nothing saved."
        if data.get("rejected_fields"):
            body += (f" Refused to store {', '.join(data['rejected_fields'])} - "
                     f"credentials are never saved.")
        return {"type": "text", "title": "Preferences", "body": body[:400]}

    return None


def _assignment_row(a: dict, late: bool = False) -> dict:
    hours = _float(a.get("est_hours"))
    row = {
        "title": str(a.get("title", ""))[:140],
        "course": str(a.get("course_code") or a.get("course") or "")[:40],
        "due": _when(a.get("due_at")),
        "priority": _int(a.get("priority")),
        "est_minutes": int(hours * 60) if hours else 0,
    }
    if late and a.get("days_late") is not None:
        row["due"] = f"{row['due']} ({_float(a.get('days_late')):.1f} days late)"
    return row


def _when(value: object) -> str:
    dt = _dt(value)
    if dt is None:
        return "no due date"
    return dt.strftime("%a %b %d, %I:%M%p").replace(" 0", " ").replace("AM", "am").replace("PM", "pm")


def _day(value: object) -> str:
    dt = _dt(value)
    return dt.strftime("%a") if dt else str(value or "")[:12]


def _time(value: object) -> str:
    dt = _dt(value)
    if dt is None:
        return str(value or "")[:10]
    return dt.strftime("%I:%M%p").lstrip("0").replace("AM", "am").replace("PM", "pm")


def _dt(value: object):
    """
    Parse a timestamp and put it in the student's timezone.

    Belt and braces with db.jsonable(), which already converts on the way out
    of the database. This layer converts too, because not every value reaching
    a card comes from a query: some arrive from a model's JSON, some from rows
    written before the conversion existed. A block stored as 19:00Z rendered
    as "7:00pm" on a dashboard whose owner had asked for 3pm, and the fix has
    to cover the data that is already there, not just the next write.
    """
    from datetime import datetime

    if value in (None, ""):
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
        except Exception:  # noqa: BLE001 - formatting must not fail on tz setup
            pass
    return parsed


def _int(value: object) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _headline(cards: list[dict], summary: str) -> str:
    first = cards[0] if cards else {}
    kind = first.get("type")
    if kind == "assignment_list":
        n = len(first.get("items") or [])
        if "Overdue" in str(first.get("title", "")):
            return f"{n} overdue" if n else "Nothing overdue"
        return f"{n} assignment{'s' if n != 1 else ''}"
    if kind == "schedule":
        return f"{len(first.get('blocks') or [])} study blocks"
    if kind == "study_set":
        return "Study set ready"
    if kind == "event_list":
        return f"{len(first.get('items') or [])} events"
    if kind == "workload_chart":
        return "Workload trend"
    if kind == "alert":
        return str(first.get("title", ""))[:60]
    return (summary.split(".")[0][:60] or "Your coursework")


def validate(spec: dict, fallback_summary: str = "") -> dict:
    """
    Drop anything not in the contract.

    Three things get stripped:
      1. card types we don't know about
      2. cards missing a required field
      3. extra keys on a valid card (an 'onclick' or 'src' has no business here)
    """
    speech = _clean(spec.get("speech") or fallback_summary or "Done.", 600)
    headline = _clean(spec.get("headline") or "Your coursework", 80)

    clean: list[dict] = []
    dropped: list[str] = []

    for card in spec.get("cards") or []:
        if not isinstance(card, dict):
            dropped.append("not-an-object")
            continue
        ctype = card.get("type")
        required = CARD_TYPES.get(ctype)
        if required is None:
            dropped.append(f"unknown-type:{ctype}")
            continue
        if not required.issubset(card.keys()):
            dropped.append(f"missing-fields:{ctype}")
            continue
        clean.append({k: v for k, v in card.items() if k in required | {"type"}})
        if len(clean) == MAX_CARDS:
            break

    if not clean:
        clean = [{"type": "text", "title": "Summary",
                  "body": fallback_summary or "No data available."}]

    out = {"speech": speech, "headline": headline, "cards": clean}
    if dropped:
        out["_dropped"] = dropped  # visible in the trace, useful while debugging
    return out


def _clean(text: object, limit: int) -> str:
    """Strip anything that looks like markup before it reaches the page."""
    s = str(text)
    for bad in ("<script", "</script", "<iframe", "javascript:", "<style", "onerror="):
        s = s.replace(bad, "")
    return s.strip()[:limit]


def _json(obj: object) -> str:
    import json

    return json.dumps(obj, default=str)


# --------------------------------------------------------------------------
# WAITING ON KENNETH
#
# When his templates land, drop them in ./templates/ named after the card type
# (assignment_list.html, schedule.html, ...) and call render_html(spec) to get
# a finished page. Each template gets the card dict as its variables.
#
# This uses Jinja2, which is the standard Python templating library: you write
# normal HTML with {{ placeholders }} and {% for %} loops, and it fills them in.
# It also escapes values by default, so a card containing "<script>" renders as
# harmless text rather than running.
#
# Nothing calls this yet. It's here so the shape is agreed in advance.
# --------------------------------------------------------------------------


def render_html(spec: dict) -> str:
    """Fill Kenneth's templates with the validated card data."""
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader, select_autoescape

    tpl_dir = Path(TEMPLATE_DIR)
    if not tpl_dir.exists():
        raise FileNotFoundError(
            f"No ./{TEMPLATE_DIR}/ folder yet. Kenneth's templates go there, "
            f"one per card type: {sorted(CARD_TYPES)}"
        )

    env = Environment(
        loader=FileSystemLoader(tpl_dir),
        autoescape=select_autoescape(["html"]),  # escapes values -> no injection
    )

    chunks = []
    for card in spec["cards"]:
        name = f"{card['type']}.html"
        try:
            chunks.append(env.get_template(name).render(**card))
        except Exception:  # noqa: BLE001 - missing template shouldn't kill the page
            chunks.append(env.get_template("text.html").render(
                title=card.get("title", ""), body="(no template for this card yet)"
            ))

    page = tpl_dir / "page.html"
    if page.exists():
        return env.get_template("page.html").render(
            headline=spec["headline"], cards_html="\n".join(chunks)
        )
    return "\n".join(chunks)
