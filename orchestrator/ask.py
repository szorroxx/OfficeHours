"""
Ask one question from the terminal and see everything that happened.

    MODE=mock python3 ask.py "what's due this week?"
    MODE=live python3 ask.py "give me the latest from canvas"
    MODE=replay python3 ask.py "what's due this week?"

This is your fastest feedback loop -- much quicker than clicking through the
website while you're getting the prompts right.
"""

from __future__ import annotations

import json
import sys

import cache
import config
import orchestrator

BOLD, DIM, GREEN, RED, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    prompt = " ".join(sys.argv[1:])
    if not config.ENV_PATH.exists():
        print(f"{DIM}(no .env file — using defaults. `cp .env.example .env`){RESET}")
    print(f"{DIM}mode={cache.MODE}{RESET}")
    print(f"{BOLD}> {prompt}{RESET}\n")

    run = orchestrator.run(prompt)

    if run.replayed:
        print(f"{DIM}(served from a recorded run -- no API calls){RESET}\n")

    # What the model was thinking, if reasoning was on.
    for i, trace in enumerate(run.reasoning, 1):
        print(f"{DIM}reasoning {i}: {trace[:400]}{RESET}\n")

    # Which tools it chose, in order.
    if run.steps:
        print(f"{BOLD}tool calls{RESET}")
        for step in run.steps:
            mark = f"{GREEN}ok{RESET}" if step["ok"] else f"{RED}fail{RESET}"
            args = json.dumps(step["arguments"])[:70]
            print(f"  [{mark}] {step['tool']}({args})  {step['ms']}ms")
            if not step["ok"]:
                print(f"        {RED}{step['result_preview'][:200]}{RESET}")
        print()

    print(f"{BOLD}summary{RESET}\n  {run.summary}\n")

    print(f"{BOLD}display{RESET}")
    print(f"  headline: {run.display.get('headline')}")
    print(f"  speech:   {run.display.get('speech')}")
    for card in run.display.get("cards", []):
        print(f"  card: {card['type']} — {card.get('title')}")
    if run.display.get("_dropped"):
        print(f"  {RED}dropped: {run.display['_dropped']}{RESET}")

    print(f"\n{DIM}{run.turns} model turns, {run.elapsed_ms}ms total{RESET}")
    if run.error:
        print(f"{RED}error: {run.error}{RESET}")
        return 1

    if cache.MODE == "live":
        print(f"{DIM}recorded — you can now replay this with MODE=replay{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
