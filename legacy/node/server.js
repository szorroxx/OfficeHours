// Office Hours backend. Basic accounts (username + password, no email); each
// account has its own board. Storage is file-backed by default, Postgres/
// TigerData when DATABASE_URL is set. The AI lives in assistant.js.

const express = require('express');
const cors = require('cors');
const path = require('path');

const store = process.env.DATABASE_URL ? require('./store.pg') : require('./store');
const assistant = require('./assistant');

const app = express();
const PORT = process.env.PORT || 8787;

app.use(cors());
app.use(express.json({ limit: '10mb' }));  // attachments ride inline as base64
app.use(express.static(path.join(__dirname, 'public')));

// ---- Auth ----
async function auth(req, res, next) {
  const h = req.headers.authorization || '';
  const token = h.startsWith('Bearer ') ? h.slice(7) : (req.headers['x-token'] || '');
  try {
    const userId = token ? await store.userIdByToken(token) : null;
    if (!userId) return res.status(401).json({ error: 'unauthorized' });
    req.userId = userId; req.token = token; next();
  } catch (e) { console.error('auth error', e); res.status(500).json({ error: 'auth_failed' }); }
}

app.post('/api/register', async (req, res) => {
  const { username = '', password = '' } = req.body || {};
  if (String(username).trim().length < 2 || String(password).length < 4)
    return res.status(400).json({ error: 'weak', message: 'Username needs 2+ characters and password 4+.' });
  try {
    const u = await store.createUser(username, password);
    const token = await store.createSession(u.id);
    res.status(201).json({ token, username: u.username });
  } catch (e) {
    if (e.code === 'exists') return res.status(409).json({ error: 'exists', message: 'That username is taken.' });
    console.error('register error', e); res.status(500).json({ error: 'register_failed' });
  }
});

app.post('/api/login', async (req, res) => {
  const { username = '', password = '' } = req.body || {};
  try {
    const u = await store.verifyUser(username, password);
    if (!u) return res.status(401).json({ error: 'bad_credentials', message: 'Wrong username or password.' });
    const token = await store.createSession(u.id);
    res.json({ token, username: u.username });
  } catch (e) { console.error('login error', e); res.status(500).json({ error: 'login_failed' }); }
});

app.post('/api/logout', auth, async (req, res) => {
  try { await store.deleteSession(req.token); res.json({ ok: true }); }
  catch (e) { console.error('logout error', e); res.status(500).json({ error: 'logout_failed' }); }
});

// ---- Board (all per-user, all require auth) ----
const TYPE_TO_KIND = { addAssignments: 'assignments', addExams: 'exams', addEvents: 'events', addTodos: 'todos' };
const KINDS_RE = ':kind(assignments|exams|events|todos)';  // keep /api/assistant etc. out of the generic route

async function applyActions(userId, actions = []) {
  const applied = [];
  for (const a of actions) {
    const kind = TYPE_TO_KIND[a.type];
    if (!kind || !Array.isArray(a.items)) continue;
    applied.push({ type: a.type, added: await store.upsertItems(userId, kind, a.items) });
  }
  return applied;
}

app.get('/api/board', auth, async (req, res) => {
  try { res.json(await store.getBoard(req.userId)); }
  catch (e) { console.error('board error', e); res.status(500).json({ error: 'board_failed' }); }
});

// ---- Library (Files tab: AI-output + manual files in collections) ----
app.get('/api/library', auth, async (req, res) => {
  try { res.json(await store.getLibrary(req.userId)); }
  catch (e) { console.error('library get error', e); res.status(500).json({ error: 'library_failed' }); }
});
app.put('/api/library', auth, async (req, res) => {
  try { res.json(await store.setLibrary(req.userId, req.body || {})); }
  catch (e) { console.error('library put error', e); res.status(500).json({ error: 'library_failed' }); }
});

// ---- Files (student attachments), per-user ----
function collectAttachments(board) {
  const out = [];
  for (const kind of store.KINDS) for (const it of (board[kind] || []))
    for (const att of (it.attachments || [])) out.push({ ...att, itemKind: kind, itemId: it.id });
  return out;
}
app.get('/api/files', auth, async (req, res) => {
  try {
    const board = await store.getBoard(req.userId);
    res.json(collectAttachments(board).map(a => ({ id: a.id, name: a.name, type: a.type, size: a.size, itemKind: a.itemKind, itemId: a.itemId, url: '/api/files/' + a.id })));
  } catch (e) { console.error('files error', e); res.status(500).json({ error: 'files_failed' }); }
});
app.get('/api/files/:id', auth, async (req, res) => {
  try {
    const att = collectAttachments(await store.getBoard(req.userId)).find(a => a.id === req.params.id);
    if (!att || !att.dataUrl) return res.status(404).json({ error: 'not_found' });
    const m = /^data:([^;]+);base64,(.*)$/s.exec(att.dataUrl);
    if (!m) return res.status(422).json({ error: 'bad_data' });
    res.setHeader('Content-Type', att.type || m[1] || 'application/octet-stream');
    res.setHeader('Content-Disposition', 'inline; filename="' + String(att.name || 'file').replace(/["\r\n]/g, '') + '"');
    res.send(Buffer.from(m[2], 'base64'));
  } catch (e) { console.error('file error', e); res.status(500).json({ error: 'file_failed' }); }
});

// ---- Item CRUD (kind constrained so it can't swallow the routes below) ----
app.post('/api/' + KINDS_RE, auth, async (req, res) => {
  try { res.status(201).json(await store.addItem(req.userId, req.params.kind, req.body || {})); }
  catch (e) { console.error('add error', e); res.status(500).json({ error: 'add_failed' }); }
});
app.patch('/api/' + KINDS_RE + '/:id', auth, async (req, res) => {
  try {
    const updated = await store.updateItem(req.userId, req.params.kind, req.params.id, req.body || {});
    if (!updated) return res.status(404).json({ error: 'not_found' });
    res.json(updated);
  } catch (e) { console.error('patch error', e); res.status(500).json({ error: 'patch_failed' }); }
});
app.delete('/api/' + KINDS_RE + '/:id', auth, async (req, res) => {
  try {
    const ok = await store.removeItem(req.userId, req.params.kind, req.params.id);
    if (!ok) return res.status(404).json({ error: 'not_found' });
    res.status(204).end();
  } catch (e) { console.error('delete error', e); res.status(500).json({ error: 'delete_failed' }); }
});

// ---- Assistant (the AI team's hook) ----
app.post('/api/assistant', auth, async (req, res) => {
  const { message = '', history = [], attachments = [] } = req.body || {};
  try {
    const board = await store.getBoard(req.userId);
    const result = await assistant.handleAssistantMessage({ message, history, board, attachments });
    const reply = (result && result.reply) || '';
    const actions = (result && Array.isArray(result.actions)) ? result.actions : [];
    await applyActions(req.userId, actions);
    res.json({ reply, actions, board: await store.getBoard(req.userId) });
  } catch (e) {
    console.error('assistant error', e);
    res.status(500).json({ error: 'assistant_failed', reply: "The assistant hit an error. Try again." });
  }
});

// ---- Optional Canvas refresh, no chat ----
app.post('/api/sync', auth, async (req, res) => {
  try {
    const found = await assistant.syncCanvas();
    const added = {
      assignments: await store.upsertItems(req.userId, 'assignments', found.assignments || []),
      events: await store.upsertItems(req.userId, 'events', found.events || []),
    };
    res.json({ added, board: await store.getBoard(req.userId) });
  } catch (e) { console.error('sync error', e); res.status(500).json({ error: 'sync_failed' }); }
});

// ---- Demo data helpers (board starts empty for each account) ----
app.post('/api/demo', auth, async (req, res) => {
  try { res.json(await store.loadDemo(req.userId)); }
  catch (e) { console.error('demo error', e); res.status(500).json({ error: 'demo_failed' }); }
});
app.post('/api/clear', auth, async (req, res) => {
  try { res.json(await store.clear(req.userId)); }
  catch (e) { console.error('clear error', e); res.status(500).json({ error: 'clear_failed' }); }
});

(async () => {
  if (store.init) await store.init();
  app.listen(PORT, () => console.log(`Office Hours backend on http://localhost:${PORT} (store: ${process.env.DATABASE_URL ? 'postgres' : 'file'})`));
})();
