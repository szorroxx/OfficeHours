# Office Hours — Nemotron orchestration layer

Your part of the project. A prompt comes in from the website, Nemotron decides
what to do, your code does it, Claude decides what the page should show.

```
website text box
      |
      v
 POST /prompt ──> NEMOTRON ──> "call get_assignments" ──> your code ──> Tiger Data
                     ^                                                      |
                     |                                                       v
                     +────────────────── the rows ──────────────────────────+
                     |
                     |  (loops until it stops asking for tools)
                     v
              short summary
                     |
                     v
          CLAUDE display agent ──> {cards: [...]} ──> Kenneth's templates
```

**The one idea everything else follows from:** Nemotron never calls anything.
You give it a list of tool *descriptions*; it replies with a tool name and some
arguments; **your Python runs the actual function**. So "Nemotron updates Tiger
Data" means Nemotron says `refresh_from_canvas()`, your code runs the crawler,
and `db.py` writes the rows with SQL you wrote. The model never sees a query.

---

## The three modes (read this before running anything)

One setting in `.env` controls what the project costs to run.

| `MODE=` | What happens | Cost |
|---|---|---|
| `mock` | No API calls at all. A fake Nemotron picks tools by keyword, fake Claude returns canned data. Everything runs end to end. | **$0** |
| `live` | Real API calls. Every response is saved to `./cache/`. | real money |
| `replay` | Plays back what `live` recorded. Works with the wifi unplugged. | **$0** |

You have $60 and it only needs to work once, so:

1. **Build in `mock`.** All the plumbing — database, website wiring, error
   handling — needs no model at all. This is most of your work.
2. **Switch to `live`** once, run your 3–4 demo prompts, check the output is
   good. Probably costs a couple dollars.
3. **Demo in `replay`.** You're showing real model output from step 2, it just
   isn't being regenerated on stage, so dead wifi or a rate limit can't ruin
   your presentation.

If a judge asks, say so plainly: "this is a recorded run of the real pipeline,
here's live mode" — then run one live. That's honest and it's also the answer
of someone who thought about reliability.

---

## Setup, step by step

### 1. Make a virtual environment

A virtual environment is a private folder of Python packages for this project,
so installing something here can't break other projects on your machine.

```bash
cd OfficeHours/orchestrator
python3 -m venv .venv          # creates the folder
source .venv/bin/activate      # start using it (run this every new terminal)
pip install -r requirements.txt
```

You'll know it worked because your prompt now starts with `(.venv)`.

### 2. Make your `.env` file

```bash
cp .env.example .env
```

Open `.env` and fill in the real values. **Leave `MODE=mock` for now.**

`.env` holds your secrets. It's already listed in `.gitignore`, which is the
file that tells git to pretend certain files don't exist. The habit that saves
you: run `git status` before every commit and make sure `.env` never appears.

> Rotate the NVIDIA key and the Timescale password if they've ever been pasted
> into a chat, a screenshot, or a commit. Public repos get scraped by bots
> within minutes.

### 3. Check that nothing is on fire

```bash
MODE=mock python3 test_loop.py
```

22 tests, no network, no cost, takes a second. They should all pass. Run this
after every change you make — it catches the boring bugs (a renamed tool, a
malformed message history) that otherwise look like the model acting weird.

### 4. Ask it something

```bash
MODE=mock python3 ask.py "what's due this week?"
```

You'll see which tools it chose, the summary, and the cards. Try:

```bash
MODE=mock python3 ask.py "give me the latest from canvas"
MODE=mock python3 ask.py "make me a schedule, no work Friday nights"
MODE=mock python3 ask.py "is this week busier than last week?"
```

All free. This is your fastest feedback loop — much quicker than clicking
through a website.

### 5. Set up the database

Only this step needs the real Timescale connection string.

```bash
python3 db.py --init     # creates all the tables
python3 db.py --check    # prints row counts; confirms it connected
python3 db.py --seed     # optional: fake assignments so you can test reads
```

`--check` should list every table plus two **hypertables**. A hypertable is
Timescale's special table type for data that accumulates over time. If it says
`hypertables: NONE`, the `CREATE EXTENSION` line in `schema.sql` didn't run —
check the connection string is the one from Timescale Cloud.

### 6. Drop your Canvas HTML in

```bash
cp ~/wherever/your-canvas-page.html canvas_pages/
python3 canvas.py           # shows the stripped text for every page
```

It prints what Claude will actually read. If the assignment titles and due
dates are visible in that text, the extractor will find them. There's a sample
page in there already you can delete.

Filenames don't matter; anything ending in `.html` gets read.

### 7. Run the server

```bash
MODE=mock uvicorn server:app --reload --port 8000
```

Then open **http://localhost:8000/docs** in a browser. FastAPI generates an
interactive page from your code where you can click "Try it out" on any
endpoint and see the response. Use that instead of writing curl by hand.

`--reload` means it restarts automatically when you save a file.

Start with `GET /health` — it tells you which keys are configured and which
mode you're in, without spending anything.

### 8. When you're ready to spend money

```bash
python3 smoke_test.py       # ~5 calls: is the key good? does tool calling work?
```

Run this *before* the real thing. Check 4 confirms Nemotron actually emits tool
calls on your chosen model. If that fails, the whole design needs a different
model, and you want to know in minute five.

Then:

```bash
MODE=live python3 ask.py "give me the latest from canvas"
MODE=live python3 ask.py "make me a schedule for this week"
python3 cache.py --list     # see what got recorded
MODE=replay python3 ask.py "give me the latest from canvas"   # free forever now
```

---

## What each file does

| File | What it is | Will you edit it? |
|---|---|---|
| `schema.sql` | The database tables | rarely |
| `db.py` | All SQL lives here. Fixed read/write functions. | sometimes |
| `canvas.py` | Reads Canvas HTML → strips tags → Claude extracts assignments | **yes** |
| `tools.py` | The tools Nemotron can call, and what they do | **yes, most** |
| `orchestrator.py` | The loop: ask model → run tools → repeat → summarize | rarely |
| `display.py` | Claude decides the cards. **The contract with Kenneth.** | when templates land |
| `nemotron_client.py` | Nemotron wrapper: reasoning control, retries, the mock model | rarely |
| `cache.py` | mock / live / replay | rarely |
| `server.py` | The HTTP endpoints | sometimes |
| `ask.py` | Ask one question from the terminal | no |
| `test_loop.py` | 22 offline tests | add to it |
| `smoke_test.py` | Real-API validation | no |

If you only understand two files, make them `tools.py` and `display.py`. Those
are the two places your teammates' work meets yours.

---

## Contracts to freeze with your team

Hackathons die at integration, not implementation. Paste these in the channel.

### You → Kenneth (website)

`POST /prompt` with `{"prompt": "..."}` returns:

```json
{
  "summary": "plain text answer",
  "display": {
    "speech": "one or two sentences",
    "headline": "max 8 words",
    "cards": [ {"type": "assignment_list", "title": "...", "items": [...]} ]
  },
  "trace": { "steps": [...], "reasoning": [...], "turns": 3 }
}
```

`cards[].type` is one of exactly seven values, and nothing else ever gets
through: `assignment_list`, `schedule`, `study_set`, `event_list`,
`workload_chart`, `text`, `alert`. Max 4 cards. His modular site shows/hides a
component per type. Full field list is `CARD_TYPES` and `RENDER_CONTRACT` in
`display.py`.

**The cards contain data, never HTML.** When his templates are ready they go in
`./templates/` named after the card type (`assignment_list.html`, etc.) and
`display.render_html()` fills them in. That function is already written and
waiting. Using templates instead of generating markup per request means: fast,
identical every time, and a user can't get `<script>` onto the page through the
prompt box.

### You → Rowan (Tiger Data)

`db.py` owns the schema. Import from it rather than writing your own queries so
you two can't disagree about column names. Tables:
`students`, `courses`, `assignments`, `schedule_blocks`, `study_sets`,
`campus_events`, plus hypertables `workload_snapshots` and `study_sessions`.

### You → Jared (Alexa, later)

`POST /voice` with `{"utterance": "..."}` returns `{"speech": "...",
"card_title": "..."}`. One line in his handler. The `speech` field is plain
prose — no markdown, no lists, no URLs. It uses the fast model with reasoning
off and read-only tools, because an Alexa skill endpoint times out in a few
seconds.

---

## The Tiger Data track

Timescale is a *time-series* database, and most of your data isn't time-series
— assignments are just rows. So there are two hypertables that genuinely are:

- **`workload_snapshots`** — one row per course every time the crawler runs.
- **`study_sessions`** — actual minutes spent, so estimates can be checked
  against reality.

`GET /workload` runs this, which is the query to put on screen:

```sql
SELECT time_bucket('1 day', observed_at) AS day,
       course_code, max(total_est_hours) AS est_hours
FROM workload_snapshots
WHERE student_id = 'demo-student' AND observed_at > now() - interval '30 days'
GROUP BY day, course_code ORDER BY day;
```

`time_bucket()` is Timescale's signature function — it rounds timestamps into
fixed-width buckets so you can graph a trend. Plain Postgres needs clumsier
`date_trunc` + `generate_series` gymnastics to do the same thing. Saying that
sentence to a judge is the whole track pitch.

To have a chart with more than one point, run the crawler a few times over the
weekend (in mock mode it's free, and it still writes snapshot rows).

---

## Three things to show the judges

1. **The trace.** `trace.steps` has every tool call, its arguments, whether it
   worked, and how long it took, plus the reasoning text. Render it as a
   sidebar. Judges watching `check_freshness → refresh_from_canvas →
   get_assignments → make_schedule` appear live beats any slide.
2. **Recovery.** Ask about a course that isn't in the database. Nemotron gets
   an empty result, decides to crawl, then re-reads. Two sentences that prove
   it's an agent and not an if-statement.
3. **Routing.** The same question through `/prompt` (reasoning on, all tools)
   and `/voice` (fast model, reasoning off, read-only). One prompt, two latency
   budgets, a deliberate model choice. That's NVIDIA's multi-model story in 20
   seconds.

---

## Troubleshooting

**`ModuleNotFoundError`** — your virtual environment isn't active. Run
`source .venv/bin/activate`.

**`NVIDIA_API_KEY is not set`** — either `.env` isn't being loaded, or you're
in `live`/`replay` when you meant `mock`.

**Nemotron returns an empty answer** — reasoning ate the token budget. This is
the #1 Nemotron gotcha: reasoning tokens and answer tokens share one
`max_tokens`. `nemotron_client.py` raises `NemotronBudgetError` with the actual
numbers instead of handing you `""`. Raise `max_tokens` or lower
`thinking_token_budget`.

**`429 Too Many Requests`** — NVIDIA's free tier is about 40 requests/minute.
The client retries automatically. If it keeps happening, you're in `live` when
you should be in `mock`.

**`ReplayMiss`** — you asked `replay` for a prompt that was never recorded.
Run that exact prompt once in `live` first. `python3 cache.py --list` shows
what you have. This raises instead of quietly calling the API on purpose — a
silent fallback is how you discover at 3am that the credits are gone.

**Browser says "failed to fetch" with nothing in the server log** — that's
CORS, the browser blocking a cross-origin request. Already handled in
`server.py`. But note: a page served over **https** (Vercel) cannot call
**http** (localhost) — browsers block it as mixed content. For the demo either
run the frontend locally too, or put the backend behind an https tunnel
(`ngrok http 8000`). Worth testing Saturday morning, not Saturday night.

**`hypertables: NONE`** — `CREATE EXTENSION timescaledb` didn't run. Confirm
`DATABASE_URL` points at Timescale Cloud, then `python3 db.py --init` again.

---

## Note on deployment

This backend can't run on Vercel as-is. Vercel functions have short execution
limits and are stateless, so a multi-step agent loop and an on-disk cache don't
fit. For the hackathon, run it on your laptop — that's the right call and one
less thing to break. If you want it hosted afterward, Render, Railway, or Fly
all run a persistent Python process for free or cheap.
