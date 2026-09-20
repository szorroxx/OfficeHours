# Office Hours

An AI assistant that crawls a student's Canvas account (and other course sources) and surfaces what matters: upcoming assignments, events, and a personal to-do list, all on one dashboard. The student talks to the assistant in plain language ("Look through my Canvas for upcoming assignments"), it handles sign-in, scans their courses, and writes what it finds back to the board.

This repo is the **frontend / storage layer**. The AI crawler is a separate piece that plugs into a single, well-defined boundary described below.

Status: hackathon prototype. Last updated to reflect `app.html` (login + dashboard + chat).

---

## What's built

Everything currently lives in one self-contained file: **`app.html`**. It runs in the browser with no build step and has three parts. Items render from HTML `<template>` elements (one per type) whose `data-field` slots are filled by the renderer, so the structure each item exposes is visible in the markup. The AI fills a matching data template (see Templates).

1. **Sign-in screen.** Username + password. Demo mode, so any input gets you in. The password is never read or stored; only the username is kept, and only to personalize the greeting.
2. **Dashboard, in three tabs.** **Dashboard** is the "This week" schedule strip plus four folders: Assignments, Upcoming exams, Events, and a To-do list. **Files** is where the assistant's generated files land, with manual file upload and user-created collections to group them (each file tagged AI or Added). **Calendar** is a full month view of the same board data (assignments on their scheduled/due day, exams, events, completed to-dos) with month navigation; clicking a day opens the add form pre-set to that date, and the AI populates it through the board it already fills. Items reach a folder two ways, both through the same routing code: the crawler adds them via the chat, or the student adds them manually with **"+ Add item"**. **Click any item to expand it** for full details, its time estimate, the day to work on it, file attachments, or delete. The prominent **ask bar** in the hero is the main way to reach the assistant.
3. **Assistant chat.** A launcher in the bottom-right opens a chat panel. The student types, the assistant replies, and the assistant can add items to the board. This is the surface the AI crawler drives.

State persists between reloads using the browser storage API (see Data model). Right now that's a stand-in for a real backend.

---

## How to run it

Open `app.html` in a browser. That's it for the prototype. Sign in with anything, then click "Ask Office Hours" and try:

> Look through my Canvas for upcoming assignments

The assistant walks through a mock login → two-factor → sync flow and adds new items to the board so you can see the intended behavior end to end.

---

## Architecture

```
  Student
    |
    v
+-----------------------------+        +---------------------------+
|   app.html (this repo)      |        |   AI crawler (teammates)  |
|                             |        |                           |
|   login  ->  dashboard      |        |   - logs into Canvas      |
|                ^            |        |   - scans courses         |
|   chat  --->  getAssistantReply() -->|   - returns reply + items |
|                |            | <------|                           |
|                v            |        +---------------------------+
|   board updates (actions)   |
+-----------------------------+
```

The frontend never talks to Canvas directly. It sends every chat message to one function, `getAssistantReply`, and applies whatever comes back. Swapping the mock for the real crawler is a one-function change.

**Folder routing.** Both the crawler and the manual "+ Add item" form funnel through the same three helpers (`pushAssignment`, `pushEvent`, `pushTodo`). Whichever source an item comes from, it lands in the correct folder the same way, so there is one place to change routing behavior.

---

## Data model

All state is stored as JSON under these keys. When the real backend exists, these same shapes should come from the API instead of local storage.

| Key           | Shape                                                                                       | Owner                    |
| ------------- | ------------------------------------------------------------------------------------------- | ------------------------ | ---- |
| `assignments` | `{ id, title, course, dueISO, scheduledISO?, estimateMins?, source, completed, canvasId? }` | crawler + manual         |
| `exams`       | `{ id, title, course, dueISO, location?, estimateMins?, source, completed, canvasId? }`     | crawler + manual         |
| `events`      | `{ id, title, location, startISO, source, completed, canvasId? }`                           | crawler + manual         |
| `todos`       | `{ id, text, done, estimateMins?, createdISO, completedISO? }`                              | student                  |
| `completed`   | `[id, ...]` — ids of synced items checked off (frontend local mode)                         | student                  |
| `username`    | `string`                                                                                    | student                  |
| `chat`        | `[{ role: 'user'                                                                            | 'assistant', content }]` | both |

`dueISO` and `startISO` are ISO 8601 date strings. For an exam, `dueISO` is the exam date/time. Urgency and the "Next up" callout are computed from these on the client, so the crawler only needs to provide a valid date.

`scheduledISO` (assignments only) is the day the student plans to work on the assignment, set from the day dropdown. It is separate from `dueISO` (the deadline). `estimateMins` is an optional time estimate in minutes, stored for later use in planning. `source` is `'canvas'` or `'manual'`. `canvasId` is Canvas's own id; the backend upserts on it so re-syncing refreshes rows instead of duplicating them, and a manual item is never overwritten by a sync.

`attachments` is an optional array of files a student attaches to an item (notes, previous exams, and so on): `{ id, name, type, size, dataUrl }`. For the prototype the file content rides inline as a base64 data URL on the item, which works in both storage modes with no extra endpoints but keeps the blob in the board payload. **For production, offload the file to a blob store (Cloudinary, which the team already uses, or S3) and store only its URL on the attachment.** There's a 3 MB per-file cap in the demo to keep local storage and the board payload sane.

---

## Templates (how the AI writes items)

The AI does not build item objects by hand. It fills a **template** per type, so field names are fixed and identical on both ends. The templates live in `backend/assistant.js` as the `TEMPLATES` registry, and the matching display slots live in `app.html` as `<template id="tpl-*">` elements with `data-field` attributes. Same vocabulary describes the data and the display.

| Template     | Action type      | Fields (`?` optional)                                        |
| ------------ | ---------------- | ------------------------------------------------------------ |
| `assignment` | `addAssignments` | `title, course, dueISO, estimateMins?, canvasId?`            |
| `exam`       | `addExams`       | `title, course, dueISO, location?, estimateMins?, canvasId?` |
| `event`      | `addEvents`      | `title, location, startISO, canvasId?`                       |
| `task`       | `addTodos`       | `text, estimateMins?`                                        |

Fill one with the helper, which stamps out an item from the template and drops anything that isn't a known field:

```js
const { fill } = require("./assistant");
fill("assignment", {
  title: "PS6",
  course: "CS 1550",
  dueISO: "2026-02-01T09:00:00Z",
  estimateMins: 120,
});
// -> { title:'PS6', course:'CS 1550', dueISO:'...', estimateMins:120, canvasId:null }
```

Then hand it back as an action: `{ type:'addAssignments', items:[ filled ] }`. The server routes it to the right folder and the frontend maps each field into the matching `data-field` slot. Scheduling (`scheduledISO`) is deliberately not a template field yet; the student sets it, and how the AI might propose one is still open.

---

## AI integration boundary (read this if you're building the crawler)

This is the contract. Everything the assistant does flows through `getAssistantReply` in `app.html`. Replace its body with a call to your backend and change nothing else.

**Input** the frontend sends:

```js
{
  message:   string,                                // what the student typed
  history:   [{ role, content }],                   // prior chat turns
  dashboard: { assignments, exams, events, todos }, // current board state, for context
  attachments: [{ name, type, size, dataUrl }]      // files sent in this chat message (paperclip)
}
```

**Output** your backend must return:

```js
{
  reply:    string,                         // shown as a chat bubble
  actions?: [{ type, items: [...] }]        // optional writes to the board
}
```

`type` is one of `addAssignments`, `addExams`, `addEvents`, or `addTodos`, and `items` are template fills (see Templates above). The frontend renders `reply`, then appends every action's items to the right folder and re-renders.

Example real implementation:

```js
async function getAssistantReply(payload) {
  const r = await fetch("/api/assistant", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return await r.json();
}
```

The `dashboard` field lets your backend answer questions about what's already on the board (like "what's due first") without re-scanning Canvas.

---

## Security notes (important)

The mock login/2FA happens as plain chat text purely to demo the flow. **The real assistant must not collect Canvas passwords or 2FA codes through the chat.** Those would end up in the message history and storage.

For the real thing, credentials go through a proper auth handoff: OAuth, or Canvas API tokens the student generates in their Canvas settings. The chat should only ever link the student to a sign-in page, never receive the secret itself.

---

## Storage modes

The app runs in one of two modes, chosen by a single constant `API_BASE` at the top of `app.html`'s script.

**Local (default, `API_BASE = ''`).** State lives in the browser (Claude artifact storage, falling back to `localStorage` so it persists when hosted). The chat uses the built-in mock. This is the zero-setup demo.

**Backend (`API_BASE = 'https://.../api'`).** The server is the source of truth. On sign-in the board loads from `GET /board`; every change (manual add, complete, delete, estimate edit, reschedule) goes through the REST endpoints; and a chat message goes to `POST /assistant`, whose response includes the updated board, which the frontend adopts and re-renders. **This is how the site changes when a request comes through the AI chatbot:** the server applies the AI's actions to storage and returns the new board, so the dashboard reflects exactly what was stored, not a local guess.

### Backend storage: file or TigerData/Postgres

The backend picks its store from the environment:

- No `DATABASE_URL` -> `store.js`, a local file store (`backend/data.json`). Fine for the demo.
- `DATABASE_URL` set -> `store.pg.js`, Postgres. TigerData is PostgreSQL, so the standard `pg` driver and a normal connection string work directly. Put the TigerData connection string in `.env` (see `.env.example`).

Both stores expose the same async interface (`getBoard`, `addItem`, `upsertItems`, `updateItem`, `removeItem`, `loadDemo`, `clear`, `init`), so swapping is just the env var. The board starts empty; demo data is opt-in. The Postgres store keeps one `items` table (JSONB per item, tagged by kind and Canvas id) and dedupes syncs on `canvas_id`. It's written but not yet run against a live database, so exercise it once you point `DATABASE_URL` at a TigerData instance.

---

## Backend (`backend/`)

A runnable Express server that owns the board and exposes the AI as one file. **Basic accounts** (username + password, no email): each account has its own board. Passwords are hashed with scrypt and login issues a session token the frontend sends as `Authorization: Bearer <token>`. The board persists to `backend/data.json` (file store) or Postgres/TigerData. This is the surface the AI team builds against.

**Run it:**

```
cd backend
npm install
npm start        # http://localhost:8787
```

Open `http://localhost:8787` for a built-in tester that hits every endpoint, including the assistant.

**Endpoints:** (everything except register/login requires `Authorization: Bearer <token>`)

| Method | Path             | What it does                                                         |
| ------ | ---------------- | -------------------------------------------------------------------- |
| POST   | `/api/register`  | Create an account, returns `{ token, username }`                     |
| POST   | `/api/login`     | Sign in, returns `{ token, username }`                               |
| POST   | `/api/logout`    | Invalidate the current session token                                 |
| GET    | `/api/board`     | Returns the signed-in user's `{ assignments, exams, events, todos }` |
| GET    | `/api/library`   | Files tab: `{ collections, files }` for the user                     |
| PUT    | `/api/library`   | Replace the user's library (collections + files)                     |
| GET    | `/api/files`     | Lists uploaded attachments (metadata + a fetch URL each)             |
| GET    | `/api/files/:id` | Returns one attachment's raw bytes (for the AI to read)              |
| POST   | `/api/:kind`     | Add one item (`kind` = assignments/exams/events/todos)               |
| PATCH  | `/api/:kind/:id` | Update an item (e.g. toggle `completed`/`done`)                      |
| DELETE | `/api/:kind/:id` | Remove an item                                                       |
| POST   | `/api/assistant` | Chat. In `{message, history}`, out `{reply, actions, board}`         |
| POST   | `/api/sync`      | Optional Canvas refresh without chat                                 |
| POST   | `/api/demo`      | Load the demo seed data onto the board                               |
| POST   | `/api/clear`     | Empty the board                                                      |

**AI team: you edit one file, `backend/assistant.js`.** It ships with the working mock and the full contract in comments. Replace `handleAssistantMessage`, and optionally implement `syncCanvas`. The server handles persistence and routing; when you return `actions`, they land in the right folders through the same path a manual add uses. Keep the `ANTHROPIC_API_KEY` and the Canvas token server-side (see `backend/.env.example`). Student-uploaded files (notes, previous exams) are readable at `GET /api/files` and `GET /api/files/:id`; fetch the bytes and extract text if you want the assistant to read them.

**Connecting the frontend:** in `app.html`, set `API_BASE` to your backend's `/api` URL (e.g. `http://localhost:8787/api`) to make the whole board server-driven. Left empty (the default), the app runs fully local and the chat uses the built-in mock.

---

## Roadmap

- [x] Login screen, dashboard with three folders, assistant chat.
- [x] Manual "+ Add item" input that routes tasks/events/assignments to their folders.
- [x] Upcoming exams folder, a This-week schedule, per-day assignment scheduling, and time estimates.
- [x] Expandable items: click to see full details, edit the estimate, reschedule, or delete.
- [x] Template system: HTML display templates + an AI-facing `fill(type, data)` registry.
- [x] Backend: Express server with board storage + CRUD + the AI hook.
- [x] Basic accounts: username + password (scrypt-hashed), session tokens, per-user boards.
- [x] Tabs: Dashboard, Files (collections + manual/AI files), and a month Calendar view.
- [x] Backend-driven frontend: board loads from and writes to the server; AI changes return as the updated board.
- [x] Swappable storage: local file store, or Postgres/TigerData via `DATABASE_URL`.
- [ ] AI team fills in `backend/assistant.js` (real Claude call + Canvas crawl).
- [ ] Run `store.pg.js` against a live TigerData instance and confirm the flows.
- [ ] Split `app.html` into a Vite/React app with `/login` and `/dashboard` routes. **This is where the code splits into multiple files** (components, styles, helpers); the single-file setup exists only so the prototype runs with no build step.
- [ ] Harden auth for production (token expiry, rate-limit login, HTTPS-only).
- [ ] Real Canvas access via access token or OAuth (no credentials in chat).
- [ ] Decide sync model: crawler pushes on a schedule vs. frontend pulls on load.

---

## File structure

```
app.html                  Login + dashboard + chat (the frontend prototype)
README.md                 This file
assets/logos/             Source logo files (the app inlines them, kept here for the team)
backend/
  server.js               Express app: board CRUD, /api/assistant, /api/sync
  store.js                Board storage, file-backed (default; async interface)
  store.pg.js             Board storage, Postgres/TigerData (used when DATABASE_URL set)
  assistant.js            THE AI TEAM'S FILE. AI + Canvas crawl go here.
  package.json            Backend dependencies
  .env.example            Keys for the real AI (copy to .env)
  public/index.html       Served tester for the endpoints
```

---

## TL;DR

**Inputs**

- Student's chat messages (typed into the assistant).
- Manual "+ Add item" entries: assignments, exams, events, or tasks, routed to the matching folder, with an optional time estimate.
- Per-assignment scheduling: which day the student plans to work on it.
- Student's to-do entries.
- Sign-in username (demo only; no real auth yet).
- From the crawler, once hooked up: assignments, exams, and events as JSON matching the shapes in the Data model.

**Outputs**

- A dashboard with a This-week schedule and four folders (assignments, exams, events, to-dos), color-coded by urgency, with time-estimate chips.
- Assistant chat replies.
- Board updates the assistant makes (new assignments/exams/events/todos it "finds").

**What teammates should do**

- **AI team:** edit `backend/assistant.js`. Replace `handleAssistantMessage` with your real AI + Canvas crawl. Return `{ reply, actions }` per the contract in that file. The server handles storage and routing. Do not accept passwords or 2FA codes through the chat; use a Canvas access token kept server-side.
- **Frontend/Kenneth:** to test end to end, set `ASSISTANT_API` in `app.html` to the backend URL. Next up is the React split and wiring the board fully to the backend.
- **Everyone:** the Data model table is the source of truth for field names. If you add a field, update this README.
- **To run the demo:** open `app.html` (standalone), or `cd backend && npm install && npm start` then open `http://localhost:8787` for the backend tester.

## Google Calendar sync

The backend supports one-way sync from the Office Hours board to Google Calendar. It creates or updates events for assignments, exams, and events; todos are not calendar events. Repeating syncs update the same Google event instead of creating duplicates.

1. In Google Cloud Console, create a project, enable **Google Calendar API**, configure the OAuth consent screen, and create a **Web application** OAuth client.
2. Add `http://localhost:8787/api/google/callback` as an authorized redirect URI.
3. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REDIRECT_URI` in the root `.env` file.
4. With a logged-in user's bearer token, call `POST /api/google/connect`, open the returned `url` in a browser, and authorize access. `GET /api/google/connect` is also available when the request can carry the bearer token and should redirect directly.
5. Call `GET /api/google/calendars` to list calendars, then `POST /api/google/sync` with `{ "calendarId": "primary" }` or a selected calendar ID.

The OAuth refresh token is stored with the user's connection record so future syncs can refresh access without asking the user to reconnect. Use a protected database and encryption at rest before deploying this beyond the prototype.
