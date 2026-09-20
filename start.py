"""
Starts the whole thing with one command: the orchestrator (FastAPI, its own
process) and the website (Flask, this process) -- in the right order, and
stops both together on Ctrl+C.

    python3 start.py

This replaces running these in two terminals:
    (terminal 1, in orchestrator/)  MODE=mock uvicorn server:app --port 8000
    (terminal 2, repo root)         python3 app.py

Nothing about the architecture changes -- orchestrator/README.md is still
right that the orchestrator can't run on Vercel, so this script is for local
dev only. It launches the exact same two processes; it just launches them
from one terminal instead of two, and waits for the first to answer /health
before starting the second, so you don't hit a "can't reach the
orchestrator" error just because the timing was off.

MODE defaults to mock here, same as every other script in this project, so
running this never spends money by accident:

    python3 start.py              # MODE=mock, the default everywhere else
    MODE=live python3 start.py    # a real env var, so (per config.py) it
                                   # wins over whatever either .env file says

Flags, if you need non-default ports (e.g. something else on your machine is
already using 8000):
    python3 start.py --orch-port 8001 --web-port 5001
(or set $ORCHESTRATOR_PORT / $PORT instead)
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
ORCH_DIR = os.path.join(ROOT, "orchestrator")


def wait_for_health(url: str, proc: subprocess.Popen, timeout: float) -> bool:
    """Poll the orchestrator's /health until it answers, or it dies trying."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"orchestrator exited early (exit code {proc.returncode}) -- "
                "scroll up for its output, that's the actual error."
            )
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.3)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--orch-port", type=int,
        default=int(os.environ.get("ORCHESTRATOR_PORT", 8000)),
        help="port for the orchestrator (default 8000, or $ORCHESTRATOR_PORT)",
    )
    parser.add_argument(
        "--web-port", type=int,
        default=int(os.environ.get("PORT", 5000)),
        help="port for the website (default 5000, or $PORT)",
    )
    args = parser.parse_args()

    if not os.path.isdir(ORCH_DIR):
        sys.exit(f"[start] can't find {ORCH_DIR} -- run this from the repo root.")

    # Real env vars win over .env, per orchestrator/config.py -- so if MODE is
    # already set in the shell this launches from, that's respected. If not,
    # default to mock: $0, so `python3 start.py` is always safe to run.
    env = os.environ.copy()
    env.setdefault("MODE", "mock")
    health_url = f"http://127.0.0.1:{args.orch_port}/health"

    print(f"[start] launching orchestrator on :{args.orch_port} (MODE={env['MODE']}) ...")
    orch = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(args.orch_port)],
        cwd=ORCH_DIR,
        env=env,
    )

    def cleanup() -> None:
        if orch.poll() is None:
            print("\n[start] stopping orchestrator...")
            orch.terminate()
            try:
                orch.wait(timeout=5)
            except subprocess.TimeoutExpired:
                orch.kill()

    def on_signal(signum, frame) -> None:
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        ready = wait_for_health(health_url, orch, timeout=30)
    except RuntimeError as exc:
        sys.exit(f"[start] {exc}")

    if ready:
        print(f"[start] orchestrator is up ({health_url})")
    else:
        print(
            f"[start] orchestrator didn't answer {health_url} within 30s -- "
            "starting the website anyway; expect connection errors until "
            "it's up. Check the output above for what it's stuck on."
        )

    # app.py reads ORCHESTRATOR_URL at import time. Only set it if the shell
    # didn't already give it one, same "explicit env wins" rule as above.
    os.environ.setdefault("ORCHESTRATOR_URL", f"http://127.0.0.1:{args.orch_port}")
    sys.path.insert(0, ROOT)

    print(f"[start] launching website on :{args.web_port} ...")
    try:
        import app as website  # local import: after ORCHESTRATOR_URL is set

        # use_reloader=False matters here, not just style: Flask's reloader
        # forks this same script as a child process to watch for file
        # changes, which would run everything above a second time and try
        # to bind the orchestrator's port twice.
        website.app.run(port=args.web_port, use_reloader=False)
    except OSError as exc:
        print(f"[start] website couldn't start: {exc}")
    finally:
        cleanup()


if __name__ == "__main__":
    main()
