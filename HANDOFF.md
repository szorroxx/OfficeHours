# Handoff

What changed, what to do next, and the five things most likely to bite you.

## Run it

```bash
pip install -r requirements.txt
python3 app.py          # http://localhost:5000
```

`MODE=mock` is the default and costs nothing. Register any username, ask
"what's due this week?", and watch the board fill and the assistant's panels
appear.

```bash
MODE=mock python3 test_integration.py               # 161 tests
cd orchestrator && MODE=mock python3 test_loop.py   # 58 tests
```

## Who owns what now

| Was | Is now |
|---|---|
| Kenneth — `app.html` | unchanged in look; talks to `/api` and renders the agent's panels |
| Rowan — `server.js`, `store*.js`, `assistant.js` | `store.py`, `agent.py`, the API half of `app.py` |
| Jared — `/alexa` in `orchestrator/server.py` | `/alexa` in `app.py`, same translation logic |
| Finn — `orchestrator/` | unchanged, plus per-account student scoping |

Everything Node is in `legacy/node/` for reference. Nothing imports it.

## Five things most likely to bite you

**1. Your keys are burned.** The NVIDIA, Anthropic, and Timescale credentials
were in a `.env` that travelled in a zip. Rotate all three before the demo,
not after.

**2. `MODE` now comes from the root `.env`, which says `live`.** It used to
come from `orchestrator/.env`, which said `mock`. Same command, real money.
`app.py` prints a loud warning when it boots in a paid mode — but set
`MODE=mock` in `.env` while you're building, and run `MODE=live python3
app.py` deliberately.

**3. Nothing persists on Vercel without `DATABASE_URL`.** The file store
falls back to `/tmp`, which is wiped between invocations, so accounts will
seem to vanish at random. `/api/health` says so under `store.ephemeral`.

**4. `MODE=replay` needs `orchestrator/recorded/` committed.** Run your demo
prompts once in `live`, then `python3 cache.py --export`, then commit. Without
that, a replay deploy raises `ReplayMiss` on every prompt.

**5. One `MODE=live` run hasn't happened yet.** Everything below is verified
in mock only.

**6. Real student data is committed.** `orchestrator/canvas_pages/` holds
`rowanCanvas.html` (a saved Canvas dashboard with a name in it) and
`canvas_export.json` (180KB: 358 assignments, 23 courses, two finished
semesters). The `.gitignore` line that looked like it covered the export was
anchored to `<repo>/canvas_pages/` while the file lives under
`orchestrator/`, so it never matched and the file has been tracked since it
was added. The pattern is fixed now, but that doesn't untrack anything — git
only ignores files it isn't already following, and these are in earlier
commits too. If this repo goes public, deal with it first:

```bash
git rm --cached orchestrator/canvas_pages/canvas_export.json
# and for the history, one of: git filter-repo, or squash to a fresh initial commit
```

The crawler works fine without the export — `rowanCanvas.html` alone strips
to 5,090 characters of real assignments, and `cs1684_assignments.html` is a
synthetic page. Decide as a team whether Rowan minds.

## What still needs a real run

Do this well before you present, in this order:

```bash
cd orchestrator
python3 smoke_test.py                 # ~5 calls: is the key good? does tool calling work?
MODE=live python3 ask.py "what's due this week?"
MODE=live python3 ask.py "give me the latest from canvas"
MODE=live python3 ask.py "make me a schedule for this week"
MODE=live python3 ask.py "is this week busier than last week?"
python3 cache.py --export
git add recorded && git commit -m "Record demo runs"
```

Then start the site in `live` and send the same prompts through the chat box,
because the surface agent only runs on the web path. Two things to look at:

- **Does the layout agent behave?** Check `changes.surface` in the response.
  It should mostly be `updated` on repeat questions and `added` for new kinds
  of answer, with `removed` almost always empty. If it's churning — adding and
  removing the same panel every turn — tighten the "remove is a last resort"
  wording in `surface.PLAN_CONTRACT`.
- **Does the custom path get used, and is it sanitized?** If
  `changes.surface.sanitized` is non-empty, read what got stripped. A model
  reaching for `<style>` or an `<img>` means `STYLE_VOCAB` isn't giving it
  enough to work with.

Also worth one live check: `MODE=live python3 db.py --init` then
`--check`, and confirm it reports two hypertables. If it says
`hypertables: NONE`, the `CREATE EXTENSION` line didn't run and the workload
chart will be empty.

## Removing things

Three different acts, three different results — worth knowing which is which:

| The student says | Tool | What happens |
|---|---|---|
| "I submitted it" | `update_assignment` → `submitted` | ticked, stays on the board under "Show completed" |
| "my teammate submitted it" | `update_assignment` → `dismissed` | removed from the board, kept in Tiger Data |
| "delete it / I don't want to see it" | `delete_assignments` | deleted from the database, and the next Canvas crawl won't re-add it |
| "actually put it back" | `restore_assignments` + `refresh_from_canvas` | restored |

Two details that matter:

**Deleting has to outlive a crawl.** Assignment ids are a hash of student +
course + title, so a plain `DELETE` is undone by the next
`refresh_from_canvas`: it rebuilds the same id from the same Canvas page and
re-inserts the row. `suppressed_assignments` records what was deleted so the
crawler skips it. That table is also the undo.

**Completed work is hidden, not struck through.** The board used to render a
finished item with a line through it and leave it there, which meant
"I removed those for you" and thirteen visible rows. `showDone` in app.html
now filters them out of the panels, the week strip and the month calendar, with
a "Show N completed" toggle. It changes what renders, never what's stored.

## Known gaps, honestly

- **Attachment contents don't reach the model.** `/api/files` serves uploaded
  files and the chat passes their names and types, but nobody extracts text
  from a PDF yet. `agent._context()` marks where it would go. The design doc
  listed "study sets from professor notes + textbook" — this is the missing
  half of it.
- **`add_to_schedule` has no UI.** The tool works and Nemotron can call it,
  but nothing on the dashboard says "put this event on my calendar."
- **The workload chart needs history to look like anything.** One crawl is
  one data point. Run the crawler a few times over the weekend — in mock it's
  free and it still writes snapshot rows.
- **`orchestrator/server.py` duplicates `/voice` and `/alexa`.** Deliberate,
  documented at the top of that file, still a drift risk. If you don't need
  the FastAPI `/docs` page, delete it.
- **No rate limiting on `/api/register`.** Fine for a hackathon; not fine if
  this stays up.
- **Sessions never expire server-side** beyond the 30-day TTL, and there's no
  "log out everywhere."

## If something breaks on stage

- Board empty, chat answering → the agent ran but wrote nothing. Check
  `changes.applied` in the response; `seen` without `added` means the rows
  were already there.
- Panels not appearing → `GET /api/surface`. If chunks exist, it's the
  frontend; if not, read `changes.surface.note`, which says why the layout
  agent didn't produce a plan.
- Everything 401s → the token expired or the store was wiped. Sign out and
  back in.
- `ReplayMiss` → that exact prompt was never recorded. `python3 cache.py
  --list` shows what you have. Switch to `mock` and keep going.
- Anything weird and model-shaped → run `MODE=mock` and see if it still
  happens. If it does, it's your code, not the model.
