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
    path = _path(key)
    if not path.exists():
        return None
    return json.loads(path.read_text())["response"]


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
                f"already recorded."
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
    path = RUN_DIR / f"{_run_key(prompt)}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())["run"]


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
    if label.startswith("display"):
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

    if "--clear" in args:
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
        runs = sorted(RUN_DIR.glob("*.json"))
        print(f"\nrecorded full runs: {len(runs)}")
        for f in runs:
            print(f"  {json.loads(f.read_text())['prompt'][:70]}")
