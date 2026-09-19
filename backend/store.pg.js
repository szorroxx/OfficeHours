// Postgres / TigerData store. Same async interface as store.js, multi-user.
// TigerData is PostgreSQL, so the standard `pg` driver and plain SQL work.
// Tables: users, sessions, and items (each row scoped by user_id).
//
// NOTE: written against the pg API but not yet run against a live database.
// Point DATABASE_URL at a TigerData instance to exercise it. Requires `npm i pg`.

const crypto = require('crypto');
const { Pool } = require('pg');

const KINDS = ['assignments', 'exams', 'events', 'todos'];
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.PGSSL === 'disable' ? false : { rejectUnauthorized: false },
});

const daysFromNow = (n) => { const d = new Date(); d.setHours(9, 0, 0, 0); d.setDate(d.getDate() + n); return d.toISOString(); };
function genId(p) { return p + Date.now().toString(36) + Math.random().toString(36).slice(2, 6); }
function isKind(kind) { return KINDS.includes(kind); }

function seedRows() {
  return [
    ['assignments', { title: 'Problem Set 5: Hermitian operators', course: 'PHYS 1370 — Quantum', dueISO: daysFromNow(0), estimateMins: 120, source: 'canvas', completed: false }],
    ['assignments', { title: 'Lab writeup: Gauss-Jordan inverse', course: 'CS 1550 — Systems', dueISO: daysFromNow(2), estimateMins: 90, source: 'canvas', completed: false }],
    ['assignments', { title: 'Reading response, Ch. 3', course: 'CS 1501 — Algorithms', dueISO: daysFromNow(5), source: 'canvas', completed: false }],
    ['exams', { title: 'Midterm 1', course: 'CS 1501 — Algorithms', dueISO: daysFromNow(6), location: 'Lawrence 106', estimateMins: 180, source: 'canvas', completed: false }],
    ['exams', { title: 'Quiz 2', course: 'PHYS 1370 — Quantum', dueISO: daysFromNow(3), location: 'Thaw 102', source: 'canvas', completed: false }],
    ['events', { title: 'HackPitt kickoff', location: 'Cathedral of Learning', startISO: daysFromNow(1), source: 'canvas', completed: false }],
    ['events', { title: 'Office hours: Dr. Reyes', location: 'Sennott Sq. 6203', startISO: daysFromNow(3), source: 'canvas', completed: false }],
  ];
}

async function init() {
  await pool.query(`CREATE TABLE IF NOT EXISTS users (
    id text PRIMARY KEY, username_key text UNIQUE NOT NULL, username text NOT NULL,
    salt text NOT NULL, hash text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())`);
  await pool.query(`CREATE TABLE IF NOT EXISTS sessions (
    token text PRIMARY KEY, user_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())`);
  await pool.query(`CREATE TABLE IF NOT EXISTS items (
    id text PRIMARY KEY, user_id text NOT NULL, kind text NOT NULL, canvas_id text,
    data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now())`);
  await pool.query(`CREATE INDEX IF NOT EXISTS items_user_kind_idx ON items (user_id, kind)`);
  await pool.query(`CREATE UNIQUE INDEX IF NOT EXISTS items_user_canvas_idx ON items (user_id, kind, canvas_id) WHERE canvas_id IS NOT NULL`);
}

// ---- Auth ----
function hashPassword(password, salt) {
  salt = salt || crypto.randomBytes(16).toString('hex');
  const hash = crypto.scryptSync(String(password), salt, 64).toString('hex');
  return { salt, hash };
}
function verifyPassword(password, salt, hash) {
  const h = crypto.scryptSync(String(password), salt, 64).toString('hex');
  const a = Buffer.from(h), b = Buffer.from(hash);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
async function createUser(username, password) {
  const key = String(username || '').trim().toLowerCase();
  if (!key) throw Object.assign(new Error('username required'), { code: 'invalid' });
  const exists = await pool.query(`SELECT 1 FROM users WHERE username_key = $1`, [key]);
  if (exists.rows.length) throw Object.assign(new Error('username taken'), { code: 'exists' });
  const { salt, hash } = hashPassword(password);
  const id = genId('u');
  await pool.query(`INSERT INTO users (id, username_key, username, salt, hash) VALUES ($1,$2,$3,$4,$5)`,
    [id, key, String(username).trim(), salt, hash]);
  return { id, username: String(username).trim() };
}
async function verifyUser(username, password) {
  const key = String(username || '').trim().toLowerCase();
  const r = await pool.query(`SELECT id, username, salt, hash FROM users WHERE username_key = $1`, [key]);
  const u = r.rows[0];
  if (!u || !verifyPassword(password, u.salt, u.hash)) return null;
  return { id: u.id, username: u.username };
}
async function createSession(userId) {
  const token = crypto.randomBytes(24).toString('hex');
  await pool.query(`INSERT INTO sessions (token, user_id) VALUES ($1,$2)`, [token, userId]);
  return token;
}
async function userIdByToken(token) {
  if (!token) return null;
  const r = await pool.query(`SELECT user_id FROM sessions WHERE token = $1`, [token]);
  return r.rows.length ? r.rows[0].user_id : null;
}
async function deleteSession(token) { await pool.query(`DELETE FROM sessions WHERE token = $1`, [token]); return true; }

// ---- Board (scoped to a user) ----
function toItem(row) { return { ...row.data, id: row.id }; }
async function getBoard(userId) {
  const { rows } = await pool.query(`SELECT id, kind, data FROM items WHERE user_id = $1`, [userId]);
  const board = { assignments: [], exams: [], events: [], todos: [] };
  for (const r of rows) if (board[r.kind]) board[r.kind].push(toItem(r));
  return board;
}
async function insertRow(userId, kind, item) {
  const id = item.id || genId(kind[0]);
  const data = { ...item, id };
  await pool.query(`INSERT INTO items (id, user_id, kind, canvas_id, data) VALUES ($1,$2,$3,$4,$5)`,
    [id, userId, kind, item.canvasId || null, data]);
  return data;
}
async function addItem(userId, kind, item) { return insertRow(userId, kind, { source: 'manual', completed: false, ...item }); }
async function upsertItems(userId, kind, items = []) {
  const added = [];
  for (const raw of items) {
    const it = { source: 'canvas', completed: false, ...raw };
    if (it.canvasId) {
      const found = await pool.query(`SELECT id, data FROM items WHERE user_id = $1 AND kind = $2 AND canvas_id = $3`, [userId, kind, it.canvasId]);
      if (found.rows.length) {
        const id = found.rows[0].id;
        await pool.query(`UPDATE items SET data = $1 WHERE id = $2`, [{ ...found.rows[0].data, ...it, id }, id]);
        continue;
      }
    }
    added.push(await insertRow(userId, kind, it));
  }
  return added;
}
async function updateItem(userId, kind, id, patch) {
  const found = await pool.query(`SELECT data FROM items WHERE id = $1 AND user_id = $2 AND kind = $3`, [id, userId, kind]);
  if (!found.rows.length) return null;
  const merged = { ...found.rows[0].data, ...patch, id };
  await pool.query(`UPDATE items SET data = $1, canvas_id = $2 WHERE id = $3`, [merged, merged.canvasId || null, id]);
  return merged;
}
async function removeItem(userId, kind, id) {
  const r = await pool.query(`DELETE FROM items WHERE id = $1 AND user_id = $2 AND kind = $3`, [id, userId, kind]);
  return r.rowCount > 0;
}
async function loadDemo(userId) {
  await pool.query(`DELETE FROM items WHERE user_id = $1`, [userId]);
  for (const [kind, item] of seedRows()) await insertRow(userId, kind, item);
  return getBoard(userId);
}
async function clear(userId) {
  await pool.query(`DELETE FROM items WHERE user_id = $1`, [userId]);
  return getBoard(userId);
}

module.exports = {
  KINDS, isKind, init,
  createUser, verifyUser, createSession, userIdByToken, deleteSession,
  getBoard, addItem, upsertItems, updateItem, removeItem, loadDemo, clear,
};
