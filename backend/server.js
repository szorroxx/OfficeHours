// Office Hours backend. No accounts: one implicit demo user. The board lives in
// a store module (file-backed by default, Postgres/TigerData when DATABASE_URL
// is set). The AI lives in assistant.js (owned by the AI team). This is wiring.

const express = require('express');
const cors = require('cors');
const path = require('path');

// Swap storage by environment: set DATABASE_URL (a TigerData/Postgres connection
// string) to use Postgres, otherwise fall back to the local file store. Both
// expose the same async interface, so nothing else here changes.
const store = process.env.DATABASE_URL ? require('./store.pg') : require('./store');
const assistant = require('./assistant');

const app = express();
const PORT = process.env.PORT || 8787;

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

const TYPE_TO_KIND = { addAssignments: 'assignments', addExams: 'exams', addEvents: 'events', addTodos: 'todos' };

// Route AI output into the correct folders. Same idea as the frontend: one
// place decides where items land.
async function applyActions(actions = []) {
  const applied = [];
  for (const a of actions) {
    const kind = TYPE_TO_KIND[a.type];
    if (!kind || !Array.isArray(a.items)) continue;
    const added = await store.upsertItems(kind, a.items);
    applied.push({ type: a.type, added });
  }
  return applied;
}

// ---- Board ----
app.get('/api/board', async (_req, res) => {
  try { res.json(await store.getBoard()); }
  catch (e) { console.error('board error', e); res.status(500).json({ error: 'board_failed' }); }
});

app.post('/api/:kind', async (req, res) => {
  const { kind } = req.params;
  if (!store.isKind(kind)) return res.status(404).json({ error: 'unknown_kind' });
  try { res.status(201).json(await store.addItem(kind, req.body || {})); }
  catch (e) { console.error('add error', e); res.status(500).json({ error: 'add_failed' }); }
});

app.patch('/api/:kind/:id', async (req, res) => {
  const { kind, id } = req.params;
  if (!store.isKind(kind)) return res.status(404).json({ error: 'unknown_kind' });
  try {
    const updated = await store.updateItem(kind, id, req.body || {});
    if (!updated) return res.status(404).json({ error: 'not_found' });
    res.json(updated);
  } catch (e) { console.error('patch error', e); res.status(500).json({ error: 'patch_failed' }); }
});

app.delete('/api/:kind/:id', async (req, res) => {
  const { kind, id } = req.params;
  if (!store.isKind(kind)) return res.status(404).json({ error: 'unknown_kind' });
  try {
    const ok = await store.removeItem(kind, id);
    if (!ok) return res.status(404).json({ error: 'not_found' });
    res.status(204).end();
  } catch (e) { console.error('delete error', e); res.status(500).json({ error: 'delete_failed' }); }
});

// ---- Assistant (the AI team's hook) ----
// Frontend sends { message, history }. We add the current board as context,
// call the AI, apply whatever it returns, and send back the reply plus the
// fresh board. The backend-driven frontend adopts `board`; a local one uses `actions`.
app.post('/api/assistant', async (req, res) => {
  const { message = '', history = [] } = req.body || {};
  try {
    const board = await store.getBoard();
    const result = await assistant.handleAssistantMessage({ message, history, board });
    const reply = (result && result.reply) || '';
    const actions = (result && Array.isArray(result.actions)) ? result.actions : [];
    await applyActions(actions);
    res.json({ reply, actions, board: await store.getBoard() });
  } catch (e) {
    console.error('assistant error', e);
    res.status(500).json({ error: 'assistant_failed', reply: "The assistant hit an error. Try again." });
  }
});

// ---- Optional: explicit Canvas refresh, no chat ----
app.post('/api/sync', async (_req, res) => {
  try {
    const found = await assistant.syncCanvas();
    const added = {
      assignments: await store.upsertItems('assignments', found.assignments || []),
      events: await store.upsertItems('events', found.events || []),
    };
    res.json({ added, board: await store.getBoard() });
  } catch (e) { console.error('sync error', e); res.status(500).json({ error: 'sync_failed' }); }
});

// ---- Demo convenience ----
app.post('/api/reset', async (_req, res) => {
  try { res.json(await store.reset()); }
  catch (e) { console.error('reset error', e); res.status(500).json({ error: 'reset_failed' }); }
});

(async () => {
  if (store.init) await store.init();
  app.listen(PORT, () => console.log(`Office Hours backend on http://localhost:${PORT} (store: ${process.env.DATABASE_URL ? 'postgres' : 'file'})`));
})();
