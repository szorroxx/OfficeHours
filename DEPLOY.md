# Deploying to Vercel

One Python function serves the whole thing: the site, the API, the agent, and
the Alexa endpoint. `vercel.json` routes every path to `app.py`.

```bash
vercel                # preview
vercel --prod
```

The project is already linked (`.vercel/project.json`, project `office-hours`).

## Environment variables to set in the Vercel dashboard

Required for a real run:

| Variable | Why |
|---|---|
| `MODE` | `mock`, `live`, or `replay`. See the table below. |
| `NVIDIA_API_KEY` | Nemotron. Starts `nvapi-`. |
| `ANTHROPIC_API_KEY` | The crawler, the scheduler, and the display/layout agents. |
| `DATABASE_URL` | Tiger Data. **Without it, nothing persists** — see below. |
| `ALEXA_SKILL_ID` | Set in production so requests for another skill are rejected. |

Optional: `STUDENT_ID`, `CANVAS_SOURCE`, `CANVAS_DIR`, `CLAUDE_MODEL`,
`NEMOTRON_MODEL`, `ALLOWED_DOMAINS`, `STORE_BACKEND`.

Do **not** commit `.env`. It's gitignored; Vercel reads its own variables.

## Which MODE to deploy

| `MODE` | On Vercel | Cost |
|---|---|---|
| `mock` | Works perfectly. No network calls, nothing to time out. Everything — accounts, board, chat, the HTML surface, Alexa — runs end to end. | $0 |
| `replay` | Works, and serves genuine recorded model output. Needs `orchestrator/recorded/` committed first: run `python3 cache.py --export`. See `orchestrator/recorded/README.md`. | $0 |
| `live` | Works within the function's time limit. A multi-tool run that also crawls Canvas can take 20–40s, so `maxDuration` is set to 60. If you see timeouts, either raise it (needs a paid plan) or demo the same prompts from `replay`. | real money |

## Two things that behave differently on Vercel than on a laptop

**The filesystem is read-only except `/tmp`, and `/tmp` doesn't survive between
invocations.** Two consequences, both handled:

- The orchestrator's model cache moves to `/tmp` automatically (`app.py` sets
  `CACHE_DIR` when it sees `VERCEL`). So a `live` run still records, but the
  recording is gone by the next request — which is why `replay` reads the
  committed `recorded/` folder as well.
- `store.py` falls back to a `/tmp` JSON file when there's no `DATABASE_URL`.
  Accounts and boards will appear to vanish at random as instances recycle.
  `/api/health` says so explicitly under `store.ephemeral`. **Set
  `DATABASE_URL` for any deployment you plan to show someone.**

**Cold starts.** The first request after idle pays import time for flask,
anthropic, openai, and psycopg. Hit `/api/health` once before a demo.

## If the database is down

The site still boots. `store.py` probes the driver and the connection at
startup and degrades to the file store rather than 500ing every request;
`/api/health` reports `store.postgres_unavailable` with the reason. You lose
persistence, not the website.

## Not Vercel?

`orchestrator/README.md` recommends Render, Railway, or Fly for `MODE=live`,
and that advice still holds: a persistent process keeps the model cache, has
no execution limit, and makes `live` and `replay` behave the same as they do
locally. Vercel is the right call for `mock` and `replay`.
