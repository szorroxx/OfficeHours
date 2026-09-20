"""
Every model call in the project goes through this file. That's what lets one
environment variable change how much the project costs to run.

    MODE=mock     No API calls at all. Returns canned fixtures.
                  Use this for ALL plumbing work: routing, the database, the
                  website wiring. Costs $0. Instant.

    MODE=live     Real API calls. Every response is saved to ./cache/.
                  Use this when you actually need to see what the models do.

    MODE=replay   Plays back saved responses. Costs $0, runs instantly, and
                  works with the wifi unplugged.

THE DEMO PLAN, given you have $60 and it only needs to work once:

    1. Build everything in mock.
    2. Switch to live, run your 3-4 demo prompts once each. Real model output,
       real cost, maybe a dollar. The responses get recorded.
    3. Switch to replay for the actual presentation.

In replay your demo is showing genuine model output from step 2 -- it just
isn't re-generating it on stage, so a dead network or an exhausted rate limit
can't ruin your ten minutes. Be upfront if a judge asks: "this is a recording
of a real run, here's the live mode" and then run one live if they want.

A miss in replay mode raises an error instead of quietly calling the API. That
is deliberate: a silent fallback is how you discover at 3am that you spent
your credits. Warm the cache first (`python3 cache.py --list` shows what you
have).
"""

from __future__ import annotations

import config  # noqa: F401  - loads .env before anything reads it
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

MODE = os.getenv("MODE", "mock").lower()
CACHE_DIR = Path(os.getenv("CACHE_DIR", "cache"))
RUN_DIR = CACHE_DIR / "runs"
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")

# --------------------------------------------------------------------------
# Committed recordings, so replay works on a deployed URL
#
# CACHE_DIR is written at runtime, which on Vercel means /tmp -- wiped between
# invocations. So a MODE=replay deploy would start with an empty cache and
# every prompt would raise ReplayMiss, which is the one failure this whole
# mechanism exists to prevent.
#
# RECORDED_DIR is the fix: a read-only folder that IS committed to the repo.
# Reads check the live cache first, then fall back to it; writes never touch
# it. `python3 cache.py --export` copies what a live run recorded into it.
#
#   MODE=live python3 ask.py "give me the latest from canvas"   # records
#   python3 cache.py --export                                   # commit-ready
#   git add orchestrator/recorded && git commit
#
# Now the deployed site can serve that exact run with no network and no keys.
# --------------------------------------------------------------------------
RECORDED_DIR = Path(os.getenv("RECORDED_DIR",
                              str(Path(__file__).parent / "recorded")))
RECORDED_RUN_DIR = RECORDED_DIR / "runs"

CACHE_DIR.mkdir(parents=True, exist_ok=True)
RUN_DIR.mkdir(parents=True, exist_ok=True)


class ReplayMiss(RuntimeError):
    """Asked to replay something that was never recorded."""


# --------------------------------------------------------------------------
# Key/value cache on disk
# --------------------------------------------------------------------------


def _key(payload: dict) -> str:
    """
    A fingerprint of the request. Same request in, same fingerprint out, so we
    can look up whether we've made this exact call before.

    sort_keys matters: {"a":1,"b":2} and {"b":2,"a":1} are the same request and
    must produce the same fingerprint.
    """
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def load(key: str) -> Any | None:
    """Live cache first, then the committed recordings."""
    for path in (_path(key), RECORDED_DIR / f"{key}.json"):
        if path.exists():
            return json.loads(path.read_text())["response"]
    return None


def store(key: str, label: str, request: dict, response: Any) -> None:
    _path(key).write_text(
        json.dumps(
            {
                "label": label,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "mode": MODE,
                "request": request,
                "response": response,
            },
            indent=2,
            default=str,
        )
    )


# --------------------------------------------------------------------------
# Wrapped model calls
# --------------------------------------------------------------------------


def wrap(label: str, request: dict, call, mock_value: Any):
    """
    The shared pattern for every model call.

    `call` is a function that does the real API request. It only ever runs in
    live mode. `mock_value` is what to return in mock mode.
    """
    key = _key(request)

    if MODE == "mock":
        return mock_value

    if MODE == "replay":
        hit = load(key)
        if hit is None:
            raise ReplayMiss(
                f"No recorded response for '{label}' (key {key}).\n"
                f"Run this exact prompt once with MODE=live to record it, then "
                f"switch back to replay. `python3 cache.py --list` shows what's "
                f"already recorded, and `--export` copies it into "
                f"{RECORDED_DIR.name}/ so a deployed instance can serve it too."
            )
        return hit

    # live
    response = call()
    store(key, label, request, response)
    return response


def claude(system: str, user: str, max_tokens: int = 2000,
           label: str = "claude", expect_json: bool = True,
           model: str | None = None) -> dict:
    """
    One-shot Claude call. Used by the crawler, the schedule builder, the study
    guide builder, and the display agent.
    """
    model = model or CLAUDE_MODEL
    request = {"provider": "anthropic", "model": model, "system": system,
               "user": user, "max_tokens": max_tokens}

    def do_call() -> dict:
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        msg = client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        if not expect_json:
            return {"text": text}
        return _parse_json(text)

    return wrap(label, request, do_call, mock_value=_mock_claude(label))


def _parse_json(text: str) -> dict:
    """Models sometimes wrap JSON in ```json fences. Strip them before parsing."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        cleaned = parts[1] if len(parts) > 1 else cleaned
        cleaned = cleaned.removeprefix("json").strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return {"error": f"model did not return valid JSON: {exc}", "raw": text[:600]}
    return parsed if isinstance(parsed, dict) else {"error": "expected a JSON object",
                                                    "raw": text[:300]}


# --------------------------------------------------------------------------
# Run-level snapshots (for the presentation)
# --------------------------------------------------------------------------


def _run_key(prompt: str) -> str:
    return hashlib.sha256(prompt.strip().lower().encode()).hexdigest()[:16]


def save_run(prompt: str, run_json: dict) -> None:
    path = RUN_DIR / f"{_run_key(prompt)}.json"
    path.write_text(json.dumps({"prompt": prompt, "run": run_json}, indent=2, default=str))


def load_run(prompt: str) -> dict | None:
    name = f"{_run_key(prompt)}.json"
    for path in (RUN_DIR / name, RECORDED_RUN_DIR / name):
        if path.exists():
            return json.loads(path.read_text())["run"]
    return None


def recorded_count() -> dict:
    """What replay can serve right now. Shown in /api/health."""
    live = len(list(RUN_DIR.glob("*.json"))) if RUN_DIR.exists() else 0
    committed = (len(list(RECORDED_RUN_DIR.glob("*.json")))
                 if RECORDED_RUN_DIR.exists() else 0)
    return {"live_cache": live, "committed": committed,
            "recorded_dir": str(RECORDED_DIR)}


def export() -> dict:
    """
    Copy the live cache into the committed recordings folder.

    Deliberately a copy rather than a move: a live run stays replayable
    locally even if you never commit it, and re-running --export is
    idempotent.
    """
    import shutil

    RECORDED_DIR.mkdir(parents=True, exist_ok=True)
    RECORDED_RUN_DIR.mkdir(parents=True, exist_ok=True)
    copied = {"responses": 0, "runs": 0}
    for src in CACHE_DIR.glob("*.json"):
        shutil.copy2(src, RECORDED_DIR / src.name)
        copied["responses"] += 1
    for src in RUN_DIR.glob("*.json"):
        shutil.copy2(src, RECORDED_RUN_DIR / src.name)
        copied["runs"] += 1
    return copied


# --------------------------------------------------------------------------
# Mock fixtures
# --------------------------------------------------------------------------


def _mock_claude(label: str) -> dict:
    if label.startswith("extract"):
        return {
            "course": {"code": "PHYS 1361", "title": "Electricity and Magnetism",
                       "instructor": "Dr. A. Ramirez", "canvas_id": "1103"},
            "assignments": [
                {"title": "Problem Set 4 – Gauss's Law", "kind": "homework",
                 "due_at": "2026-09-22T23:59:00-04:00", "points": 50,
                 "est_hours": 3.0, "status": "open",
                 "description": "Griffiths problems from chapter 2."},
                {"title": "Quiz 3 (Chapter 2)", "kind": "quiz",
                 "due_at": "2026-09-21T10:00:00-04:00", "points": 25,
                 "est_hours": 1.0, "status": "open",
                 "description": "In-class quiz on electric potential."},
                {"title": "Midterm Exam 1", "kind": "exam",
                 "due_at": "2026-10-08T14:00:00-04:00", "points": 200,
                 "est_hours": 2.0, "status": "open",
                 "description": "Covers chapters 1-3."},
            ],
        }
    if label.startswith("schedule"):
        return {
            "blocks": [
                {"task": "[MOCK] Review Ch. 2 for Quiz 3", "starts_at": "2026-09-20T19:00:00-04:00",
                 "ends_at": "2026-09-20T20:00:00-04:00", "est_minutes": 60, "priority": 1},
                {"task": "[MOCK] Problem Set 4", "starts_at": "2026-09-21T18:00:00-04:00",
                 "ends_at": "2026-09-21T21:00:00-04:00", "est_minutes": 180, "priority": 2},
            ],
            "rationale": "[MOCK] Quiz is soonest, so it goes first.",
        }
    if label.startswith("study"):
        return {"sections": [{"topic": "[MOCK] Gauss's Law",
                              "summary": "Flux through a closed surface is proportional to enclosed charge.",
                              "questions": ["State Gauss's law in integral form."]}]}
    if label.startswith("triage"):
        # Ordering advice for the scheduler. Titles must match real ones or
        # the planner ignores them, which is the correct behaviour and is
        # what this fixture deliberately exercises.
        return {"order": ["Quiz 3 (Chapter 2)", "Problem Set 4 – Gauss's Law",
                          "Lab 3: cross-validation", "Midterm Project proposal"],
                "rationale": "[MOCK] soonest deadlines first, project last."}
    if label.startswith("surface"):
        # label is "surface:<primary card type>" -- see the note in
        # surface.plan_ops. Keying the fixture off it means mock mode shows
        # the panel the prompt actually called for.
        kind = label.split(":", 1)[1] if ":" in label else "text"
        # The layout plan (see surface.py). Exercises the real path in mock
        # mode: one premade chunk keyed off card 0, one custom chunk written
        # in app.html's class vocabulary, and no removals -- which is what a
        # well-behaved plan looks like.
        #
        # The custom chunk deliberately includes an <img onerror=...> so that
        # running anything in mock mode also proves the sanitizer strips it.
        # A safety net you never see fire is one you don't know is connected.
        return {
            "upsert": [{"id": kind.replace("_", "-"), "card_index": 0,
                        "title": f"[MOCK] {kind.replace('_', ' ').title()}"}],
            "custom": [{
                "id": "mock-note",
                "title": "[MOCK] Heads up",
                "html": '<section class="panel"><div class="panel-head">'
                        '<div class="panel-title">[MOCK] Heads up</div></div>'
                        '<div class="list"><div class="item">'
                        '<div class="rail" style="background: var(--amber)"></div>'
                        '<div class="item-body"><div class="item-meta">'
                        'Written by the mock layout agent.</div></div>'
                        '</div></div></section>'
                        '<img src=x onerror="alert(1)">',
            }],
            "remove": [],
            "note": "[MOCK] refreshed the assignment panel, added a note",
        }
    if label.startswith("display"):
        # label is "display:<channel>:<tool>+<tool>" -- see the note in
        # display.render_spec. The card the fixture returns follows from which
        # tools ran, so mock mode exercises every card type and every premade
        # template rather than only the assignment list.
        if "make_schedule" in label or "get_schedule" in label:
            return {
                "speech": "Your plan starts tonight with an hour of review.",
                "headline": "2 study blocks this week",
                "cards": [{"type": "schedule", "title": "Your plan", "blocks": [
                    {"day": "Sun", "start": "7:00pm", "end": "8:00pm",
                     "task": "[MOCK] Review Ch. 2 for Quiz 3", "est_minutes": 60},
                    {"day": "Mon", "start": "6:00pm", "end": "9:00pm",
                     "task": "[MOCK] Problem Set 4", "est_minutes": 180},
                ]}],
            }
        if "workload" in label:
            return {
                "speech": "Your estimated hours are up from four to six and a half.",
                "headline": "Workload trending up",
                "cards": [{"type": "workload_chart",
                           "title": "Estimated hours over time", "series": [
                               {"label": "PHYS 1361", "points": [
                                   {"x": "2026-09-15", "y": 4.0},
                                   {"x": "2026-09-19", "y": 6.5}]}]}],
            }
        if "study_guide" in label:
            return {
                "speech": "I put together a set on Gauss's law.",
                "headline": "Study set ready",
                "cards": [{"type": "study_set", "title": "Study set: PHYS 1361",
                           "sections": [{"topic": "[MOCK] Gauss's Law",
                                         "summary": "Flux through a closed surface "
                                                    "is proportional to enclosed charge.",
                                         "questions": ["State Gauss's law in integral form.",
                                                       "When is it easier than Coulomb's law?"]}]}],
            }
        if "events" in label:
            return {
                "speech": "There's a career fair Thursday afternoon.",
                "headline": "1 event this week",
                "cards": [{"type": "event_list", "title": "On campus", "items": [
                    {"title": "[MOCK] SCI Career Fair", "when": "Thu 4:00pm",
                     "where": "Alumni Hall"}]}],
            }
        if "overdue" in label:
            return {
                "speech": "Two things are past due.",
                "headline": "2 overdue",
                "cards": [{"type": "alert", "title": "2 overdue",
                           "body": "[MOCK] Quiz 3 is 3 days late and Lab 3 is "
                                   "half a day late."}],
            }
        return {
            "speech": "You have a quiz Monday and a problem set Tuesday.",
            "headline": "3 things due this week",
            "cards": [{"type": "assignment_list", "title": "Due soon", "items": [
                {"title": "Quiz 3 (Chapter 2)", "course": "PHYS 1361",
                 "due": "Mon 10:00am", "priority": 1, "est_minutes": 60},
                {"title": "Problem Set 4", "course": "PHYS 1361",
                 "due": "Tue 11:59pm", "priority": 2, "est_minutes": 180},
            ]}],
        }
    return {"text": f"[MOCK {label}]"}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

if __name__ == "__main__":
    args = set(sys.argv[1:])
    print(f"MODE={MODE}  CACHE_DIR={CACHE_DIR.resolve()}\n")

    if "--export" in args:
        print(f"exported {export()} into {RECORDED_DIR}")
    elif "--clear" in args:
        n = 0
        for f in CACHE_DIR.glob("*.json"):
            f.unlink()
            n += 1
        print(f"deleted {n} cached responses (runs/ kept)")
    else:
        entries = sorted(CACHE_DIR.glob("*.json"))
        if not entries:
            print("no cached model responses yet. Run something with MODE=live.")
        for f in entries:
            data = json.loads(f.read_text())
            print(f"  {f.stem}  {data['label']:<28} {data['saved_at']}")
        counts = recorded_count()
        print(f"\nrecorded full runs: {counts['live_cache']} in the live cache, "
              f"{counts['committed']} committed")
        seen = set()
        for directory in (RUN_DIR, RECORDED_RUN_DIR):
            if not directory.exists():
                continue
            for f in sorted(directory.glob("*.json")):
                prompt = json.loads(f.read_text())["prompt"]
                if prompt in seen:
                    continue
                seen.add(prompt)
                where = "committed" if directory == RECORDED_RUN_DIR else "cache"
                print(f"  [{where}] {prompt[:66]}")
