# Committed model recordings

Real responses from `MODE=live`, checked into the repo on purpose.

`MODE=replay` reads the live cache (`orchestrator/cache/`, gitignored) first
and then falls back to this folder. That matters for anything deployed: on
Vercel the writable cache lives in `/tmp` and is wiped between invocations,
so without these files a `MODE=replay` deployment would raise `ReplayMiss` on
every prompt — which is the exact failure replay exists to prevent.

To refresh:

```bash
MODE=live python3 ask.py "give me the latest from canvas"
MODE=live python3 ask.py "make me a schedule for this week"
python3 cache.py --export        # copies cache/ -> recorded/
git add recorded && git commit -m "Record demo runs"
```

`python3 cache.py --list` shows what replay can currently serve, and which
of it is committed.

These are genuine model outputs, not fixtures. If a judge asks, say so
plainly: it's a recording of a real run, and here's live mode — then run one
live.
