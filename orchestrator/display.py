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
    """Ask Claude how to lay out the result, then validate hard."""
    payload = {
        "user_asked": prompt,
        "orchestrator_summary": summary,
        "tool_results": [
            {"tool": s["tool"], "arguments": s["arguments"], "result": s["result_preview"]}
            for s in steps
        ],
        "channel": channel,
    }

    spec = cache.claude(
        system="You decide how a study-assistant dashboard renders a result.\n\n"
               + RENDER_CONTRACT,
        user=_json(payload),
        max_tokens=3000,
        label=f"display:{channel}",
    )

    if "error" in spec:
        # The display agent failing should never lose the answer.
        return validate({"speech": summary, "headline": "Here's what I found",
                         "cards": [{"type": "text", "title": "Summary", "body": summary}]},
                        fallback_summary=summary)

    return validate(spec, fallback_summary=summary)


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
