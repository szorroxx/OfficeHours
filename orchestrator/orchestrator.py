"""
The orchestration loop. This is your deliverable.

Shape of one request:

  prompt  ->  Nemotron (with tools)  ->  emits tool_calls
                    ^                         |
                    |                         v
                    +---- tool results ---  YOUR code executes them
                                              |
                              (repeat until no more tool calls)
                                              |
                                              v
                              Nemotron writes a final summary
                                              |
                                              v
                              Claude display agent -> render spec
                                              |
                                              v
                                    website / Alexa

The model never calls anything. It emits a name and arguments; tools.execute
runs the real function. That asymmetry is the whole mental model.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import cache
import tools
from display import render_spec
from nemotron_client import FAST_MODEL, NemotronClient, Reply

MAX_TURNS = 6  # hard stop so a confused model can't loop forever on your credits

SYSTEM_PROMPT = """You are the orchestrator for Office Hours, a study assistant.

You have tools that read the student's coursework out of storage, refresh it \
from Canvas, and dispatch specialist agents. Your job is to pick which tools to \
call and in what order, then write a short factual summary of what you found.

How to decide:
- For anything about current coursework, call check_freshness first. It is \
instant and free, and it tells you whether the stored data can be trusted.
- If the data is fresh, just read it with get_assignments. Do not refresh.
- If check_freshness says stale, or the student asks for the latest, call \
refresh_from_canvas once, then read the data.
- make_schedule and make_study_guide need real data. Read before you write.
- Never invent an assignment, due date, grade, or event. If it isn't in the \
tool results, say it isn't there.
- Never ask for or store a password. If the student offers one, tell them to \
generate a revocable Canvas access token instead.

When you have enough, stop calling tools and write 2-4 sentences. Describe what \
the student needs to know, not which tools you used.
"""


@dataclass
class Run:
    """Everything that happened. Feed this to the UI and to your demo."""

    prompt: str
    summary: str = ""
    display: dict = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)  # one per tool call
    reasoning: list[str] = field(default_factory=list)
    turns: int = 0
    elapsed_ms: int = 0
    error: str | None = None
    replayed: bool = False   # True if served from a recorded run

    def to_json(self) -> dict:
        return {
            "prompt": self.prompt,
            "summary": self.summary,
            "display": self.display,
            "trace": {
                "steps": self.steps,
                "reasoning": self.reasoning,
                "turns": self.turns,
                "elapsed_ms": self.elapsed_ms,
                "mode": cache.MODE,
                "replayed": self.replayed,
            },
            "error": self.error,
        }


def run(
    prompt: str,
    *,
    nem: NemotronClient | None = None,
    channel: str = "web",  # "web" | "voice"
    context: dict | None = None,
    use_recorded: bool = True,
) -> Run:
    """
    channel="voice" uses the fast model, no reasoning, and a trimmed tool set,
    because Alexa will time out on you otherwise.

    In MODE=replay, if this exact prompt was recorded earlier we return the
    whole saved run immediately: no model calls, no database, no network. That
    is your stage-proof demo path.
    """
    started = time.time()

    if use_recorded and cache.MODE == "replay":
        recorded = cache.load_run(prompt)
        if recorded is not None:
            out = Run(prompt=prompt)
            out.summary = recorded.get("summary", "")
            out.display = recorded.get("display", {})
            trace = recorded.get("trace", {})
            out.steps = trace.get("steps", [])
            out.reasoning = trace.get("reasoning", [])
            out.turns = trace.get("turns", 0)
            out.elapsed_ms = int((time.time() - started) * 1000)
            out.replayed = True
            return out

    nem = nem or NemotronClient()
    voice = channel == "voice"

    schemas = tools.FAST_TOOL_SCHEMAS if voice else tools.TOOL_SCHEMAS
    model = FAST_MODEL if voice else None
    thinking = "off" if voice else "low"
    max_turns = 2 if voice else MAX_TURNS

    user_content = prompt
    if context:
        user_content = f"{prompt}\n\n<context>{json.dumps(context)}</context>"

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    out = Run(prompt=prompt)

    try:
        for turn in range(max_turns):
            out.turns = turn + 1
            reply: Reply = nem.chat(
                messages,
                tools=schemas,
                thinking=thinking,
                max_tokens=600 if voice else 2048,
                thinking_token_budget=None if voice else 1024,
                temperature=0.2,
                model=model,
            )

            if reply.reasoning:
                out.reasoning.append(reply.reasoning)

            if not reply.wants_tools:
                out.summary = reply.content
                break

            # IMPORTANT: append only `content` and `tool_calls` to history.
            # Never feed the reasoning trace back in — Nemotron's docs say not to,
            # and it wastes context.
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.content or None,
                    "tool_calls": reply.tool_calls,
                }
            )

            for call in reply.tool_calls:
                name = call["function"]["name"]
                raw_args = call["function"]["arguments"]
                t0 = time.time()
                result = tools.execute(name, raw_args)
                step = {
                    "tool": name,
                    "arguments": _safe_json(raw_args),
                    "ok": "error" not in result,
                    "ms": int((time.time() - t0) * 1000),
                    "result_preview": _preview(result),
                }
                out.steps.append(step)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(result)[:12000],  # keep context sane
                    }
                )
        else:
            # Ran out of turns with tools still pending.
            out.summary = out.summary or (
                "I gathered what I could but ran out of steps before finishing."
            )

        # Hand the whole run to the display agent.
        out.display = render_spec(
            prompt=prompt,
            summary=out.summary,
            steps=out.steps,
            channel=channel,
        )

    except Exception as exc:  # noqa: BLE001
        out.error = f"{type(exc).__name__}: {exc}"
        out.summary = out.summary or "Something went wrong on my end."
        out.display = {
            "speech": out.summary,
            "cards": [{"type": "text", "title": "Error", "body": out.error}],
        }

    out.elapsed_ms = int((time.time() - started) * 1000)

    # In live mode, save the whole run so MODE=replay can serve it later.
    if cache.MODE == "live" and out.error is None:
        cache.save_run(prompt, out.to_json())

    return out


# --------------------------------------------------------------------------


def _safe_json(raw: str | dict) -> dict | str:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return str(raw)[:300]


def _preview(result: dict, limit: int = 400) -> str:
    text = json.dumps(result)
    return text if len(text) <= limit else text[:limit] + "..."
