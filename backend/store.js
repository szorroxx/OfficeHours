// Data store for the demo. Now multi-user: each account has its own board.
// Passwords are hashed with Node's built-in scrypt; login issues a session
// token. State persists to data.json as { users, sessions, boards }.
// When you outgrow the file store, store.pg.js implements the same interface
// on Postgres/TigerData.

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const DATA_FILE = path.join(__dirname, 'data.json');
const KINDS = ['assignments', 'exams', 'events', 'todos'];

const daysFromNow = (n) => { const d = new Date(); d.setHours(9, 0, 0, 0); d.setDate(d.getDate() + n); return d.toISOString(); };
const emptyBoard = () => ({ assignments: [], exams: [], events: [], todos: [] });
function seedBoard() {
  return {
    assignments: [
      { id: 'a1', title: 'Problem Set 5: Hermitian operators', course: 'PHYS 1370 — Quantum', dueISO: daysFromNow(0), estimateMins: 120, source: 'canvas', completed: false },
      { id: 'a2', title: 'Lab writeup: Gauss-Jordan inverse', course: 'CS 1550 — Systems', dueISO: daysFromNow(2), estimateMins: 90, source: 'canvas', completed: false },
      { id: 'a3', title: 'Reading response, Ch. 3', course: 'CS 1501 — Algorithms', dueISO: daysFromNow(5), source: 'canvas', completed: false },
    ],
    exams: [
      { id: 'x1', title: 'Midterm 1', course: 'CS 1501 — Algorithms', dueISO: daysFromNow(6), location: 'Lawrence 106', estimateMins: 180, source: 'canvas', completed: false },
      { id: 'x2', title: 'Quiz 2', course: 'PHYS 1370 — Quantum', dueISO: daysFromNow(3), location: 'Thaw 102', source: 'canvas', completed: false },
    ],
    events: [
      { id: 'e1', title: 'HackPitt kickoff', location: 'Cathedral of Learning', startISO: daysFromNow(1), source: 'canvas', completed: false },
      { id: 'e2', title: 'Office hours: Dr. Reyes', location: 'Sennott Sq. 6203', startISO: daysFromNow(3), source: 'canvas', completed: false },
    ],
    todos: [],
  };
}

let db = load();
function load() {
  try { const d = JSON.parse(fs.readFileSync(DATA_FILE, 'utf8')); if (d && d.users && d.boards && d.sessions) return d; } catch {}
  return { users: {}, sessions: {}, boards: {} };
}
function save() { try { fs.writeFileSync(DATA_FILE, JSON.stringify(db, null, 2)); } catch (e) { console.error('store: save failed', e); } }

function genId(p) { return p + Date.now().toString(36) + Math.random().toString(36).slice(2, 6); }
function isKind(kind) { return KINDS.includes(kind); }
async function init() { /* file loads eagerly at require time */ }

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
  if (db.users[key]) throw Object.assign(new Error('username taken'), { code: 'exists' });
  const { salt, hash } = hashPassword(password);
  const id = genId('u');
  db.users[key] = { id, username: String(username).trim(), salt, hash };
  db.boards[id] = emptyBoard();
  save();
  return { id, username: db.users[key].username };
}
async function verifyUser(username, password) {
  const u = db.users[String(username || '').trim().toLowerCase()];
  if (!u || !verifyPassword(password, u.salt, u.hash)) return null;
  return { id: u.id, username: u.username };
}
async function createSession(userId) { const token = crypto.randomBytes(24).toString('hex'); db.sessions[token] = userId; save(); return token; }
async function userIdByToken(token) { return db.sessions[token] || null; }
async function deleteSession(token) { if (db.sessions[token]) { delete db.sessions[token]; save(); } return true; }

// ---- Board (scoped to a user) ----
function boardOf(userId) { if (!db.boards[userId]) db.boards[userId] = emptyBoard(); return db.boards[userId]; }

async function getBoard(userId) { return boardOf(userId); }

async function addItem(userId, kind, item) {
  const b = boardOf(userId);
  const withId = { source: 'manual', completed: false, ...item, id: item.id || genId(kind[0]) };
  b[kind].push(withId); save(); return withId;
}
async function upsertItems(userId, kind, items = []) {
  const b = boardOf(userId), added = [];
  for (const raw of items) {
    const it = { source: 'canvas', completed: false, ...raw };
    let idx = -1;
    if (it.canvasId) idx = b[kind].findIndex((x) => x.canvasId && x.canvasId === it.canvasId);
    if (idx < 0 && it.id) idx = b[kind].findIndex((x) => x.id === it.id);
    if (idx >= 0) b[kind][idx] = { ...b[kind][idx], ...it, id: b[kind][idx].id };
    else { const wi = { ...it, id: it.id || genId(kind[0]) }; b[kind].push(wi); added.push(wi); }
  }
  save(); return added;
}
async function updateItem(userId, kind, id, patch) {
  const b = boardOf(userId), i = b[kind].findIndex((x) => x.id === id);
  if (i < 0) return null;
  b[kind][i] = { ...b[kind][i], ...patch, id }; save(); return b[kind][i];
}
async function removeItem(userId, kind, id) {
  const b = boardOf(userId), before = b[kind].length;
  b[kind] = b[kind].filter((x) => x.id !== id); save(); return b[kind].length < before;
}
async function loadDemo(userId) { db.boards[userId] = seedBoard(); save(); return db.boards[userId]; }
async function clear(userId) { db.boards[userId] = emptyBoard(); save(); return db.boards[userId]; }

module.exports = {
  KINDS, isKind, init,
  createUser, verifyUser, createSession, userIdByToken, deleteSession,
  getBoard, addItem, upsertItems, updateItem, removeItem, loadDemo, clear,
};
