// Postgres / TigerData store. Same async interface as store.js, so server.js
// uses it unchanged when DATABASE_URL is set. TigerData is PostgreSQL, so the
// standard `pg` driver and plain SQL work directly.
//
// No accounts yet: everything is one implicit demo user, one `items` table.
// Each row is a board item stored as JSONB, tagged with its kind and (for
// synced items) its Canvas id, so upserts dedupe on canvas_id the same way the
// file store does. When you add real accounts, add a user_id column and scope
// every query by it.
//
// NOTE: this module is written against the pg API but has not been run against
// a live database here. Point DATABASE_URL at a TigerData instance to exercise
// it. Requires `npm install pg`.

const { Pool } = require('pg');

const KINDS = ['assignments', 'exams', 'events', 'todos'];

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  // TigerData/Tiger Cloud requires TLS; most managed Postgres does too.
  ssl: process.env.PGSSL === 'disable' ? false : { rejectUnauthorized: false },
});

const daysFromNow = (n) => {
  const d = new Date();
  d.setHours(9, 0, 0, 0);
  d.setDate(d.getDate() + n);
  return d.toISOString();
};

function genId(kind) {
  return kind[0] + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}
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
  await pool.query(`
    CREATE TABLE IF NOT EXISTS items (
      id         text PRIMARY KEY,
      kind       text NOT NULL,
      canvas_id  text,
      data       jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now()
    )
  `);
  await pool.query(`CREATE INDEX IF NOT EXISTS items_kind_idx ON items (kind)`);
  await pool.query(`CREATE UNIQUE INDEX IF NOT EXISTS items_kind_canvas_idx ON items (kind, canvas_id) WHERE canvas_id IS NOT NULL`);
  const { rows } = await pool.query(`SELECT count(*)::int AS n FROM items`);
  if (rows[0].n === 0) await reset();
}

// Board item = the JSONB data with its id merged in.
function toItem(row) { return { ...row.data, id: row.id }; }

async function getBoard() {
  const { rows } = await pool.query(`SELECT id, kind, data FROM items`);
  const board = { assignments: [], exams: [], events: [], todos: [] };
  for (const r of rows) if (board[r.kind]) board[r.kind].push(toItem(r));
  return board;
}

async function insertRow(kind, item) {
  const id = item.id || genId(kind);
  const canvasId = item.canvasId || null;
  const data = { ...item, id };
  await pool.query(
    `INSERT INTO items (id, kind, canvas_id, data) VALUES ($1, $2, $3, $4)`,
    [id, kind, canvasId, data]
  );
  return data;
}

async function addItem(kind, item) {
  return insertRow(kind, { source: 'manual', completed: false, ...item });
}

// Insert new items, or update existing ones matched on canvas_id. Manual items
// (no canvas_id) are always inserted, so a student's own items are never
// clobbered by a sync. Returns only the newly-added items.
async function upsertItems(kind, items = []) {
  const added = [];
  for (const raw of items) {
    const it = { source: 'canvas', completed: false, ...raw };
    if (it.canvasId) {
      const found = await pool.query(`SELECT id, data FROM items WHERE kind = $1 AND canvas_id = $2`, [kind, it.canvasId]);
      if (found.rows.length) {
        const id = found.rows[0].id;
        const merged = { ...found.rows[0].data, ...it, id };
        await pool.query(`UPDATE items SET data = $1 WHERE id = $2`, [merged, id]);
        continue;
      }
    }
    added.push(await insertRow(kind, it));
  }
  return added;
}

async function updateItem(kind, id, patch) {
  const found = await pool.query(`SELECT data FROM items WHERE id = $1 AND kind = $2`, [id, kind]);
  if (!found.rows.length) return null;
  const merged = { ...found.rows[0].data, ...patch, id };
  await pool.query(`UPDATE items SET data = $1, canvas_id = $2 WHERE id = $3`, [merged, merged.canvasId || null, id]);
  return merged;
}

async function removeItem(kind, id) {
  const r = await pool.query(`DELETE FROM items WHERE id = $1 AND kind = $2`, [id, kind]);
  return r.rowCount > 0;
}

async function reset() {
  await pool.query(`TRUNCATE items`);
  for (const [kind, item] of seedRows()) await insertRow(kind, item);
  return getBoard();
}

module.exports = { KINDS, isKind, init, getBoard, addItem, upsertItems, updateItem, removeItem, reset };
