# Office Hours

A study assistant. The text box on the website sends a prompt to Nemotron,
which calls tools to read/refresh coursework data, then a Claude display
agent turns the result into cards the site renders.

## Run it (one command)

```bash
python3 start.py
```

That starts the orchestrator (`orchestrator/server.py`, FastAPI, port 8000)
and the website (`app.py`, Flask, port 5000) together, waits for the first to
be healthy before starting the second, and stops both on Ctrl+C. It's the
same two processes you'd otherwise start by hand in two terminals:

```bash
# what start.py does for you:
(cd orchestrator && MODE=mock uvicorn server:app --port 8000)   # terminal 1
python3 app.py                                                  # terminal 2
```

Open **http://localhost:5000**.

`MODE` defaults to `mock` — $0, no API calls, canned data — same as
everywhere else in this project. For a real run: `MODE=live python3
start.py`. See `orchestrator/README.md` for what each mode does and the full
setup (venv, `.env`, database init, Canvas pages). That file is also the
detailed doc for everything under `orchestrator/`.

Non-default ports: `python3 start.py --orch-port 8001 --web-port 5001`.

## How the two halves fit together

```
templates/index.html  --fetch-->  app.py (/api/prompt)  --requests-->  orchestrator/server.py (/prompt)
     (browser)              same origin, no CORS needed         separate process, not deployed to Vercel
```

`app.py` is a thin proxy: it forwards `/api/prompt` to whatever
`ORCHESTRATOR_URL` points at (`http://localhost:8000` by default; set it to
an ngrok URL for a demo, or wherever the orchestrator ends up hosted) and
passes the response straight through, unchanged.

**The contract** (frozen in `orchestrator/display.py`, `CARD_TYPES` /
`RENDER_CONTRACT`): `POST /prompt` returns
```json
{ "summary": "...", "display": { "speech": "...", "headline": "...", "cards": [...] }, "trace": {...} }
```
`templates/index.html`'s JS renders exactly that shape — one render function
per card type (`assignment_list`, `schedule`, `study_set`, `event_list`,
`workload_chart`, `text`, `alert`).

If you change what either side sends or expects, change both, or you'll get
a page that renders `undefined` instead of an error — that's what a
frontend built against an old response shape looks like against a new one,
since `null`/missing fields don't throw, they just print as `undefined`.
Vercel deployment only ships `app.py` + `templates/` (see `.vercel/`); the
orchestrator runs as its own process, per the deployment note at the bottom
of `orchestrator/README.md`.
