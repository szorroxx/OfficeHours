"""
RUN THIS FIRST, before you write any integration code.

    export NVIDIA_API_KEY=nvapi-...
    python3 smoke_test.py

Four checks, in the order that matters:
  1. key + endpoint reachable, and which models you can actually see
  2. plain completion, reasoning off
  3. reasoning on -- does the trace come back in a separate field, or inline?
  4. TOOL CALLING -- does the hosted model emit tool_calls? Everything you are
     building depends on this. If check 4 fails, stop and change models before
     writing another line.

Costs about 5 requests. Free tier is ~40/min and ~1000 credits, so this is fine.
"""

from __future__ import annotations

import json
import os
import sys

from nemotron_client import (
    FAST_MODEL,
    ORCHESTRATOR_MODEL,
    NemotronBudgetError,
    NemotronClient,
)

WEATHER_TOOL = [{
    "type": "function",
    "function": {
        "name": "get_bus_arrivals",
        "description": "Get next arrival times for a Pittsburgh bus route at a stop.",
        "parameters": {
            "type": "object",
            "properties": {
                "route": {"type": "string"},
                "stop_id": {"type": "string"},
            },
            "required": ["route"],
        },
    },
}]


def hr(label: str) -> None:
    print(f"\n{'=' * 62}\n{label}\n{'=' * 62}")


def check_models(nem: NemotronClient) -> None:
    hr("1. endpoint + key")
    try:
        models = nem._client.models.list()
        ids = sorted(m.id for m in models.data)
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}")
        print("\nIf this is a 401: your key is wrong or expired. Regenerate at")
        print("https://build.nvidia.com/settings/api-keys (must start with nvapi-)")
        sys.exit(1)

    print(f"{len(ids)} models visible.")
    nemo = [i for i in ids if "nemotron" in i.lower()]
    print("\nNemotron IDs available to you:")
    for i in nemo:
        marker = "  <-- configured" if i == ORCHESTRATOR_MODEL else ""
        print(f"  {i}{marker}")
    if ORCHESTRATOR_MODEL not in ids:
        print(f"\nWARNING: configured model '{ORCHESTRATOR_MODEL}' is NOT in the list.")
        print("Set NEMOTRON_MODEL in .env to one of the IDs above.")


def check_plain(nem: NemotronClient) -> None:
    hr("2. plain completion, thinking OFF")
    r = nem.chat(
        [{"role": "user", "content": "Name the three Nemotron 3 sizes. One line."}],
        thinking="off",
        max_tokens=120,
    )
    print(f"content:        {r.content[:300]}")
    print(f"reasoning:      {r.reasoning!r}   (expect None)")
    print(f"finish_reason:  {r.finish_reason}")
    print(f"usage:          {r.usage}")
    assert r.content, "empty content with thinking off — check force_nonempty_content"


def check_reasoning(nem: NemotronClient) -> None:
    hr("3. reasoning ON — where does the trace land?")
    try:
        r = nem.chat(
            [{"role": "user", "content":
              "A lab is worth 20 points and due in 2 days. A project is worth 100 "
              "and due in 7. Which should I start tonight? Answer in one sentence."}],
            thinking="on",
            max_tokens=1200,
            thinking_token_budget=800,
        )
    except NemotronBudgetError as exc:
        print(f"BUDGET ERROR (this is the #1 gotcha): {exc}")
        return

    if r.reasoning:
        print(f"reasoning field present, {len(r.reasoning)} chars:")
        print(f"  {r.reasoning[:400]}")
    else:
        print("no separate reasoning field.")
        print("If you see <think> in content below, the hosted endpoint has no")
        print("reasoning parser — nemotron_client strips it for you either way.")
    print(f"\nanswer: {r.content[:300]}")
    print(f"usage:  {r.usage}")


def check_tools(nem: NemotronClient) -> None:
    hr("4. TOOL CALLING — the one that matters")
    r = nem.chat(
        [{"role": "user", "content": "When does the next 61C come?"}],
        tools=WEATHER_TOOL,
        thinking="low",
        max_tokens=800,
        thinking_token_budget=400,
    )
    if not r.tool_calls:
        print("NO TOOL CALLS EMITTED.")
        print(f"content was: {r.content[:400]}")
        print("\nThis breaks the whole design. Try, in order:")
        print("  a) a different Nemotron model ID from check 1")
        print("  b) thinking='off'")
        print("  c) tool_choice='required'")
        print("  d) fall back to JSON-mode routing (see README, 'if tool calling fails')")
        sys.exit(1)

    print("tool_calls emitted:")
    for tc in r.tool_calls:
        print(f"  {tc['function']['name']}({tc['function']['arguments']})")
    print("\nGood. The orchestrator design works on this model.")

    hr("4b. round trip — feeding a tool result back")
    messages = [
        {"role": "user", "content": "When does the next 61C come?"},
        {"role": "assistant", "content": r.content or None, "tool_calls": r.tool_calls},
        {"role": "tool", "tool_call_id": r.tool_calls[0]["id"],
         "content": json.dumps({"route": "61C", "arrivals_minutes": [4, 17, 31]})},
    ]
    r2 = nem.chat(messages, tools=WEATHER_TOOL, thinking="off", max_tokens=200)
    print(f"final answer: {r2.content[:300]}")
    assert r2.content, "model gave nothing after the tool result"


if __name__ == "__main__":
    if os.getenv("MOCK") == "1":
        print("MOCK=1 is set — unset it, this test needs the real API.")
        sys.exit(1)

    nem = NemotronClient()
    print(f"orchestrator model: {ORCHESTRATOR_MODEL}")
    print(f"fast model:         {FAST_MODEL}")
    print(f"base url:           {nem.base_url}")

    check_models(nem)
    check_plain(nem)
    check_reasoning(nem)
    check_tools(nem)

    hr("all checks passed")
    print("Next: MOCK=1 python3 test_loop.py, then uvicorn server:app --reload")
