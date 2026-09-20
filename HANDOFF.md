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

## Turning up quality

In rough order of effect per unit of effort:

| Change | Why | Cost |
|---|---|---|
| `REASONING=on` | Nemotron is a reasoning model and the default was `low`, which sets its `low_effort` flag. Tool choice is exactly where this shows. | slower, more tokens per turn |
| Sharpen a tool description | Tool-calling accuracy tracks description quality more than model size. If it picks the wrong tool, read that tool's `description` as if you were the model. | free |
| `MAX_TURNS`, `MAX_TOKENS` | Already raised to 14 / 8192. Raise further if a complex request still stops short. | more tokens |
| `CLAUDE_MODEL` | Used for triage ordering, card layout and study guides — not for tool choice. Upgrading improves prose and study guides, not reliability. | more expensive |
| `NEMOTRON_MODEL` | Check what your key can see: `python3 smoke_test.py`. | varies |
| Fewer tools | 24 tools is a lot to choose between. Trimming the set for a given intent usually beats any token increase. | a code change |

**`TEMPERATURE` has to move with `REASONING`.** NVIDIA's guidance, quoted in
this project's own `config.yml`, is ~1.0 with reasoning on and 0.0 with it
off — 0.0 plus reasoning gives degenerate traces. Leave `TEMPERATURE` unset
and it is paired for you.

**`THINKING_TOKEN_BUDGET` does nothing on the hosted endpoint.** It returns
400 for that parameter, so `nemotron_client` strips it. It now prints a
warning if you set it. Self-hosted NIM accepts it with
`NEMOTRON_ALLOW_THINKING_BUDGET=1`.

**What more tokens will not fix.** Every failure in this project so far was a
missing tool, a wrong mapping, or a silent dependency — not a model too small
to think. `REASONING=on` is worth trying; if something is still wrong after
that, read the `⚠` line under the reply and `/api/health` before turning
dials.

## Is my deployment current?

```bash
curl localhost:5000/api/health | python3 -m json.tool
```

`tools` lists exactly what this build hands to Nemotron. If `save_to_files`
isn't in it, the deployment is stale — that's the difference between "the
model is confused" and "the code isn't there", and it took a round to tell
them apart.

If a tool IS listed and the assistant still says it can't do that, the
deployment is fine and the model is wrong. `agent._repair_denied_capability`
catches that for filing: it performs the action and rewrites the reply. That
guard is narrow by design (one intent, one tool, only when the tool wasn't
called) — if you add capabilities, consider whether they need one too.

## If something looks broken, check this first

```bash
curl localhost:5000/api/health | python3 -m json.tool
```

`model_paths` tells you whether Nemotron and Claude can actually be reached —
package installed, key set — without spending anything. A missing `anthropic`
package took scheduling down on a live deployment for an entire evening, and
the only symptom anyone saw was the assistant saying "internal error (missing
dependency)". One GET would have named it.

What still works when Claude is unreachable:

| Feature | Without Claude |
|---|---|
| Scheduling | **works** — `scheduler.py` places blocks in plain Python |
| Card layout / HTML panels | works — falls back to house templates |
| Canvas crawl from a JSON export | works — no model needed |
| Canvas crawl from raw HTML | unavailable — extraction needs a model |
| Study guides | unavailable |

Tool failures now appear in the chat under the reply, with the real error
text. If you see `⚠ make_schedule failed: …`, that line is the bug report —
don't ask the model what went wrong, it will guess.

## The Files tab

`save_to_files` is the tool. Two ways to call it:

- `content` — the text of a document. Headings (`#`), bullets (`-`) and
  numbered lists are formatted; everything is escaped.
- `course` — files that course's most recent study guide, reusing the stored
  content rather than having the model retype it.

`make_study_guide` and `make_schedule` file their output automatically and
their tool results say the filename, so the assistant can name it when it
reports back. Files are HTML: the frontend opens one by decoding its data URL
into a blob, so it displays in a tab with no reader and prints cleanly.

Any tool that returns a `files` list gets those files stored — filing is not
a hardcoded list of tools, so a new document-producing tool needs no change
to the web layer.

**The lesson, if you add capabilities later:** filing worked for a full round
before this and the assistant still told a student it couldn't write files,
because the work happened as a side effect that no tool named. If the model
can't name a capability in its tool list, the student can't ask for it, and
the model will correctly deny having it.

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
