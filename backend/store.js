// Data store for the demo. No accounts: everything belongs to one implicit
// user. State is a single board persisted to data.json so it survives restarts.
// When you move to real accounts, swap this file for a Postgres layer keyed by
// user_id. Nothing else in the backend needs to change if the exported function
// signatures stay the same.

const fs = require('fs');
const path = require('path');

const DATA_FILE = path.join(__dirname, 'data.json');
const KINDS = ['assignments', 'exams', 'events', 'todos'];

const daysFromNow = (n) => {
  const d = new Date();
  d.setHours(9, 0, 0, 0);
  d.setDate(d.getDate() + n);
  return d.toISOString();
};

function seed() {
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

let board = load();

function load() {
  try {
    return JSON.parse(fs.readFileSync(DATA_FILE, 'utf8'));
  } catch {
    const s = seed();
    try { fs.writeFileSync(DATA_FILE, JSON.stringify(s, null, 2)); } catch {}
    return s;
  }
}

function save() {
  try { fs.writeFileSync(DATA_FILE, JSON.stringify(board, null, 2)); }
  catch (e) { console.error('store: save failed', e); }
}

function genId(kind) {
  return kind[0] + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

function isKind(kind) { return KINDS.includes(kind); }

async function init() { /* file store loads eagerly at require time; nothing to do */ }

async function getBoard() { return board; }

async function addItem(kind, item) {
  const withId = { source: 'manual', completed: false, ...item, id: item.id || genId(kind) };
  board[kind].push(withId);
  save();
  return withId;
}

// Insert new items, or update existing ones matched on canvasId (preferred) or
// id. This is what makes re-syncing safe: the crawler can run repeatedly and
// will refresh existing rows instead of creating duplicates. Manual items
// (source: 'manual') are never touched by a canvasId match, so a student's own
// edits survive a sync. Returns only the newly-added items.
async function upsertItems(kind, items = []) {
  const added = [];
  for (const raw of items) {
    const it = { source: 'canvas', completed: false, ...raw };
    let idx = -1;
    if (it.canvasId) idx = board[kind].findIndex((x) => x.canvasId && x.canvasId === it.canvasId);
    if (idx < 0 && it.id) idx = board[kind].findIndex((x) => x.id === it.id);
    if (idx >= 0) {
      board[kind][idx] = { ...board[kind][idx], ...it, id: board[kind][idx].id };
    } else {
      const withId = { ...it, id: it.id || genId(kind) };
      board[kind].push(withId);
      added.push(withId);
    }
  }
  save();
  return added;
}

async function updateItem(kind, id, patch) {
  const idx = board[kind].findIndex((x) => x.id === id);
  if (idx < 0) return null;
  board[kind][idx] = { ...board[kind][idx], ...patch, id };
  save();
  return board[kind][idx];
}

async function removeItem(kind, id) {
  const before = board[kind].length;
  board[kind] = board[kind].filter((x) => x.id !== id);
  save();
  return board[kind].length < before;
}

async function reset() {
  board = seed();
  save();
  return board;
}

module.exports = { KINDS, isKind, init, getBoard, addItem, upsertItems, updateItem, removeItem, reset };
