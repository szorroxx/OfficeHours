"""
The website backend (this also gets deployed to Vercel).

WHAT CHANGED
------------
This used to proxy /api/prompt over HTTP to a separately-run FastAPI process
(orchestrator/server.py). Now it calls the orchestrator directly, in-process
-- the exact same call orchestrator/ask.py makes from the terminal:

    run = orchestrator.run(prompt)
    run.to_json()

One process, one command (`python3 app.py`), and no HTTP/JSON boundary
between this file and orchestrator/ for the two sides to drift out of sync
on -- which is what caused the last bug (this file and the page's JS
disagreeing about the response's shape).

orchestrator/server.py itself is untouched. It still runs standalone if you
want it for Jared's /voice endpoint or the interactive /docs page:
    cd orchestrator && MODE=mock uvicorn server:app --reload --port 8000
It's just no longer what the website talks to.

THE DEPLOYMENT TRADE-OFF THIS BRINGS BACK
------------------------------------------
orchestrator/README.md's note that the orchestrator can't run on Vercel
as-is (stateless, short execution limits; the agent loop and its on-disk
cache don't fit) used to apply only to server.py. Now that the same code
runs inside this file, it applies to app.py on Vercel too:
  - MODE=mock: fine there. No network calls, no disk cache, effectively
    instant.
  - MODE=live / MODE=replay: run this locally, or behind a host that keeps a
    persistent process (Render/Railway/Fly), same as the README already
    recommends for the orchestrator itself.

WHICH .env THIS READS
----------------------
orchestrator/'s modules load orchestrator/.env (via orchestrator/config.py),
not the .env at the repo root -- same as when you run ask.py. Right now
those two files hold different values for some of the same keys (a
different NVIDIA_API_KEY; root's .env also sets MODE=live and has
ANTHROPIC_API_KEY, which orchestrator/.env doesn't). Worth reconciling into
one file, since which one "wins" used to depend on which script you ran,
and now depends on whether you're running app.py or something inside
orchestrator/ directly. Until then: running `python3 app.py` gets you
MODE=mock (orchestrator/.env's default), regardless of what the root .env
says.
"""

from __future__ import annotations

import os
import sys

# Make this absolute *before* Flask's debug reloader ever reads it. In debug
# mode, Werkzeug restarts by re-running the script -- and once we chdir into
# orchestrator/ below, a relative "app.py" (exactly what `python3 app.py`
# puts here) resolves to orchestrator/app.py, which doesn't exist. Absolute
# up front sidesteps that regardless of cwd.
#
# On Python 3.10+ Werkzeug rebuilds the restart command from sys.orig_argv,
# not sys.argv (see werkzeug._reloader._get_args_for_reloading) -- it's a
# separate list recording the literal original command line, so both need
# patching, or only the pre-3.10 fallback path would actually be fixed.
_abs_script = os.path.abspath(sys.argv[0])
sys.argv[0] = _abs_script
if len(getattr(sys, "orig_argv", [])) > 1:
    sys.orig_argv[1] = _abs_script

from flask import Flask, jsonify, render_template, request

# orchestrator/'s modules are written to run with that folder as the working
# directory -- plain `import cache`, `import tools`, etc. (no package
# prefix), plus a couple of relative on-disk paths (cache.py's "./cache",
# canvas.py's "./canvas_pages") that assume it. That's how ask.py and
# server.py both run. Reproduce both parts of that here rather than patching
# every relative path inside orchestrator/:
#   1. put the folder on sys.path so `import orchestrator` finds
#      orchestrator/orchestrator.py -- a real module match wins immediately
#      over the surrounding orchestrator/ directory itself ever being
#      mistaken for a namespace package, so this is safe however sys.path
#      is ordered elsewhere.
#   2. chdir into it so those on-disk defaults land in orchestrator/cache
#      and orchestrator/canvas_pages, not new folders at the repo root.
ROOT = os.path.dirname(os.path.abspath(__file__))
ORCH_DIR = os.path.join(ROOT, "orchestrator")
sys.path.insert(0, ORCH_DIR)
os.chdir(ORCH_DIR)

import cache  # noqa: E402
import canvas  # noqa: E402
import config  # noqa: E402
import orchestrator  # noqa: E402
from nemotron_client import FAST_MODEL, ORCHESTRATOR_MODEL  # noqa: E402

# Explicit, not relative to cwd (which is now orchestrator/) -- so
# render_template keeps finding the repo root's templates/ either way.
app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/prompt", methods=["POST"])
def prompt():
    data = request.get_json(silent=True) or {}
    user_prompt = (data.get("prompt") or "").strip()

    if not user_prompt:
        return jsonify({"error": "No prompt provided"}), 400

    run = orchestrator.run(user_prompt)
    print(f"prompt: {user_prompt}\nsummary: {run.summary}\nJSON: {run.to_json()}\n")
    return jsonify(run.to_json())


@app.route("/api/health")
def health():
    return jsonify(
        {
            "ok": True,
            "mode": cache.MODE,
            "config": config.status(),
            "models": {"orchestrator": ORCHESTRATOR_MODEL, "fast": FAST_MODEL},
            "canvas": {"source": canvas.CANVAS_SOURCE, "pages": canvas.list_sources()},
            "recorded_runs": len(list(cache.RUN_DIR.glob("*.json"))),
        }
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
