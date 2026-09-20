# Office Hours

A study assistant. Everything you owe, in one place, with an agent that keeps
it current.

You type a question. Nemotron decides which tools to call, reads your
coursework out of Tiger Data, crawls Canvas through Claude when what's stored
is stale, writes the new rows back, and answers. Claude then decides what the
dashboard should show and updates the page. Alexa asks the same questions
through a voice path.

```bash
python3 app.py          # http://localhost:5000
```

That's it — one command, one process. `MODE` defaults to `mock`, which makes
no API calls at all, so the whole site runs end to end for **$0**.

---

## What runs where

```
 app.html ──fetch──> app.py ──> agent.py ──> orchestrator/orchestrator.py
 (browser)           (Flask)                        │
                        │                           ├─ Nemotron picks tools
                        │                           ├─ tools.py runs them
                        │                           ├─ canvas.py + Claude crawl
                        │                           ├─ db.py writes Tiger Data
                        │                           └─ display.py builds cards
                        │
                        ├─ store.py    accounts, board, library  (Postgres or file)
                        └─ surface.py  the HTML the agent puts on the page
```

One process, no HTTP hop between the website and the agent.

**Nemotron never calls anything and never writes HTML.** It emits a tool name
and some arguments; this project's Python runs the function. That asymmetry is
the whole design, and it's why a confused model can write a row with a silly
title but can't drop a table or get a `<script>` onto the page.

| File | What it is |
|---|---|
| `app.html` | The site. Single file, no build step. |
| `app.py` | The backend: site, API, agent, Alexa. |
| `agent.py` | One prompt in; a reply, board writes, HTML updates out. |
| `store.py` | Accounts, board, library, surface. File-backed or Postgres. |
| `surface.py` | The HTML surface and the layout agent. |
| `templates/cards/` | The premade chunk templates, in the site's own CSS classes. |
| `orchestrator/` | Nemotron, the tools, Tiger Data, the Canvas crawler. Its own README goes deeper. |
| `legacy/node/` | The superseded Node backend, kept for reference. Nothing imports it. |

---

## The three modes

One setting decides what the project costs to run.

| `MODE=` | What happens | Cost |
|---|---|---|
| `mock` | No API calls. A fake Nemotron picks tools by keyword, fake Claude returns fixtures. Everything works end to end. | **$0** |
| `live` | Real calls. Every response is recorded to `orchestrator/cache/`. | real money |
| `replay` | Plays back what `live` recorded. Works with the wifi unplugged. | **$0** |

The plan, given a fixed budget and a demo that only has to work once:

1. **Build in `mock`.** All the plumbing needs no model.
2. **Switch to `live` once**, run your demo prompts, check the output.
3. **`python3 cache.py --export`**, commit `orchestrator/recorded/`.
4. **Demo in `replay`.** Genuine model output from step 2, just not regenerated
   on stage — so dead wifi or a rate limit can't cost you your ten minutes.

If a judge asks, say it plainly: this is a recording of a real run, here's live
mode — then run one live.

---

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in keys when you're ready to spend
python3 app.py
```

Nothing above is needed for `mock` beyond the install. The database, the API
keys, and the Canvas export are all optional until you switch to `live`.

**Tests.** Run both after every change. No network, no cost, ~2 seconds.

```bash
MODE=mock python3 test_integration.py       # 161 tests: store, surface, API, Alexa
cd orchestrator && MODE=mock python3 test_loop.py   # 58: agent loop, cards, parser
```

**Database** (only for `live`, and for anything that has to persist):

```bash
cd orchestrator
python3 db.py --url      # sanity-check DATABASE_URL without connecting
python3 db.py --init     # create every table
python3 db.py --check    # row counts; confirms the connection
```

Without `DATABASE_URL` the site uses a local JSON file and says so at
`/api/health`. It works; it just doesn't survive a redeploy.

---

## How the HTML surface works

The spec for this project asked for something specific: Claude generating
modular HTML, reading what the page currently shows, matching the existing
style, and being very hesitant to remove anything. `display.py` deliberately
returned card *data* instead, for three good reasons written at the top of
that file — latency, markup that drifts so the CSS breaks at random, and a
path from the prompt box to rendered markup.

`surface.py` does both, by splitting the job:

**The premade path (the default).** Claude picks *which* chunks the page should
show and what to title them. The contents render from *validated card data*
through a Jinja template in `templates/cards/`, written in `app.html`'s own
class vocabulary (`.panel`, `.item`, `.rail`, `--accent`). So chunks match the
site by construction, render identically every time, and the model never
writes their markup.

**The custom path (the escape hatch).** When no card type fits, Claude writes
the markup itself. That goes through an allowlist sanitizer — structural tags
only, no `script`/`style`/`iframe`/`form`, no `on*` handlers, `http(s)` links
only, and a narrow inline-style whitelist — *before* it is stored.

**Polling.** `GET /api/surface` returns the current chunks plus the style
vocabulary. The browser renders from the same endpoint the agent reads, so
there's no second copy of the truth to drift.

**"Hesitant to remove" is enforced, not requested.** The rules live in
`surface.apply_ops`, not in the prompt: removals must be listed explicitly,
at most one per turn, never a chunk the student made, never the last chunk on
the page. A model that ignores the instruction still cannot clear your
dashboard. Every refusal is reported back and shown under the reply.

---

## Credentials

**Account passwords** are hashed with scrypt and never stored in plaintext.
The parameters match the Node implementation this was ported from
byte-for-byte, so accounts created before the port still log in.

**Canvas passwords and 2FA codes are not accepted anywhere.** The crawler
reads a Canvas export, or a scoped access token you generate and can revoke
(Canvas → Account → Settings → New Access Token). `orchestrator/tools.py`
rejects any field matching `password`, `token`, `credential`, `ssn` and
friends; `schema.sql` has no password column by design; and `agent.py`
intercepts a message that looks like it contains one *before* it reaches a
model.

An earlier placeholder chat flow asked for a Canvas username and a Duo push.
It's gone — not ported — because the rest of the project is built to refuse
exactly that.

**Rotate your keys.** The NVIDIA, Anthropic, and Timescale credentials were
committed to a `.env` that travelled in a zip. Public repos get scraped by
bots within minutes. `git status` before every commit; `.env` should never
appear.

---

## Deploying

See **[DEPLOY.md](DEPLOY.md)**. Short version: `vercel --prod` works, `mock`
and `replay` are the right modes for it, and set `DATABASE_URL` or nothing
persists.

---

## Three things to show the judges

1. **The trace.** Every tool call, its arguments, whether it worked, and how
   long it took, plus the reasoning text — all in the response, rendered under
   each reply. Watching `check_freshness → refresh_from_canvas →
   get_assignments → make_schedule` appear live beats any slide.
2. **Recovery.** Ask about a course that isn't in the database. Nemotron gets
   an empty result, decides to crawl, then re-reads. That's an agent, not an
   if-statement.
3. **`time_bucket()`.** The workload chart comes straight out of a Timescale
   hypertable: one row per course per crawl, bucketed by day. Plain Postgres
   needs clumsier `date_trunc` + `generate_series` gymnastics for the same
   query. That sentence is the whole Tiger Data track pitch.

Plus the fourth, if you want it: ask the same question through `/api/voice`
and through `/api/prompt`. One prompt, two models, two latency budgets, a
deliberate routing decision — NVIDIA's multi-model story in twenty seconds.
