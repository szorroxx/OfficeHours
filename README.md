# Office Hours

A study assistant. The text box on the website sends a prompt to Nemotron,
which calls tools to read/refresh coursework data, then a Claude display
agent turns the result into cards the site renders.

## Run it (one command)

```bash
python3 app.py
```

Open **http://localhost:5000**. That's the whole thing — one process, same
as running any other script in this project.

`MODE` defaults to `mock` (via `orchestrator/.env` — see the note below) —
$0, no API calls, canned data. For a real run, put `MODE=live` in
`orchestrator/.env`, or export it: `MODE=live python3 app.py`. See
`orchestrator/README.md` for what each mode does and the full setup (venv,
`.env`, database init, Canvas pages). That file is also the detailed doc for
everything under `orchestrator/`.

## How the two halves fit together

```
templates/index.html  --fetch-->  app.py (/api/prompt)  --orchestrator.run()-->  orchestrator/orchestrator.py
     (browser)                    same process, no HTTP hop, no CORS
```

`app.py` calls `orchestrator.run(prompt)` directly, in-process — the same
call `orchestrator/ask.py` makes from the terminal. See the comment at the
top of `app.py` for exactly how (`sys.path`, `chdir`, why both are needed).

**The contract** (frozen in `orchestrator/display.py`, `CARD_TYPES` /
`RENDER_CONTRACT`): `orchestrator.run(...).to_json()` returns
```json
{ "summary": "...", "display": { "speech": "...", "headline": "...", "cards": [...] }, "trace": {...} }
```
`templates/index.html`'s JS renders exactly that shape — one render function
per card type (`assignment_list`, `schedule`, `study_set`, `event_list`,
`workload_chart`, `text`, `alert`). If you change what either side produces
or expects, change both, or you'll get a page that renders `undefined`
instead of an error — that's what a frontend built against an old response
shape looks like against a new one, since `null`/missing fields don't throw,
they just print as `undefined`.

`orchestrator/server.py` (FastAPI) is still there and still works — run it
directly if you want Jared's `/voice` endpoint or the interactive `/docs`
page — it's just not what the website talks to anymore.

## Two things worth fixing before this goes further

**Two `.env` files, quietly disagreeing.** `app.py` now reads
`orchestrator/.env` (loaded by `orchestrator/config.py`), not the `.env` at
the repo root — same as `ask.py`. Right now those files have *different*
values for some of the same keys: a different `NVIDIA_API_KEY` in each, and
the root one additionally sets `MODE=live` and has `ANTHROPIC_API_KEY`,
neither of which exist in `orchestrator/.env`. Worth reconciling into one
file you both trust, since right now "which one wins" depends on which
script you happen to run.

**Vercel.** `orchestrator/README.md` explains why the orchestrator can't run
on Vercel as-is: it's a multi-turn agent loop with an on-disk cache, and
Vercel functions are stateless with short execution limits. That used to be
true only of `orchestrator/server.py`. Now that the same code runs inside
`app.py`, it's true of `app.py` on Vercel too:
- `MODE=mock` — fine there, no network calls, no disk cache, instant.
- `MODE=live` / `MODE=replay` — needs a host that keeps a persistent
  process (Render, Railway, Fly — same recommendation the README already
  makes), or run it locally for the demo.
