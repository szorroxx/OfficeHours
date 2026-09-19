// =============================================================================
//  AI TEAM: THIS FILE IS YOURS.
// =============================================================================
//  This is the only file you need to touch. The rest of the backend calls
//  handleAssistantMessage() for every chat message and does the plumbing
//  (persisting results, returning them to the frontend). You focus on the AI
//  and the Canvas crawl.
//
//  THE CONTRACT (do not change these shapes, or the frontend breaks):
//
//    input   { message:  string,                         // what the student typed
//              history:  [{ role, content }],            // prior chat turns
//              board:    { assignments, events, todos } } // current board, for context
//
//    return  { reply:    string,                          // shown as a chat bubble
//              actions?: [{ type, items: [...] }] }       // optional writes to the board
//
//  action.type is one of:
//    'addAssignments' -> items: { title, course, dueISO, url?, canvasId?, estimateMins? }
//    'addExams'       -> items: { title, course, dueISO, location?, canvasId?, estimateMins? }
//    'addEvents'      -> items: { title, location, startISO, url?, canvasId? }
//    'addTodos'       -> items: { text, estimateMins? }
//
//  For assignments, `dueISO` is the deadline. The student can also schedule when
//  to work on it (the frontend writes `scheduledISO`); you do not need to set it.
//  `estimateMins` is an optional time estimate (e.g. 120 for two hours).
//  For exams, `dueISO` is the exam date/time.
//
//  Include a `canvasId` on crawled items (Canvas's own id for the item). The
//  store upserts on it, so you can re-run a sync without creating duplicates.
//
//  Everything below the divider is a placeholder so the endpoint works today.
//  Delete it and drop in the real thing when you're ready.
// =============================================================================

async function handleAssistantMessage(payload) {
  return mockCrawlerReply(payload);   // <-- replace this line with your implementation
}

// =============================================================================
//  TEMPLATES: what the AI writes into.
//  For each item type, fill(type, data) stamps out an item from a template so
//  every field name is fixed and you never hand-build an object. A `?` field is
//  optional; leave it out and the template's null default applies. These field
//  names match the HTML display slots in app.html (the tpl-* templates), so the
//  same vocabulary describes the data and the display.
//
//  Usage in your real handler:
//    { type: 'addAssignments', items: [ fill('assignment', { title, course, dueISO, estimateMins }) ] }
// =============================================================================
const TEMPLATES = {
  //          -> action type        fields (dueISO/startISO are ISO 8601 strings)
  assignment: { title: '', course: '', dueISO: '', estimateMins: null, canvasId: null },   // addAssignments
  exam:       { title: '', course: '', dueISO: '', location: null, estimateMins: null, canvasId: null }, // addExams
  event:      { title: '', location: '', startISO: '', canvasId: null },                    // addEvents
  task:       { text: '', estimateMins: null },                                             // addTodos
};

function fill(type, data) {
  const t = TEMPLATES[type];
  if (!t) throw new Error('Unknown template type: ' + type);
  const out = { ...t };
  for (const key of Object.keys(t)) if (data[key] != null) out[key] = data[key];
  return out;
}

/*
  REAL IMPLEMENTATION SKETCH (two common approaches):

  A) Simple, no tools. Detect intent, run the crawl, summarize.

     async function handleAssistantMessage({ message, history, board }) {
       if (/canvas|assignment|due|sync/i.test(message)) {
         const found = await syncCanvas();               // your crawler, below
         return {
           reply: `Found ${found.assignments.length} assignments and ${found.events.length} events.`,
           actions: [
             { type: 'addAssignments', items: found.assignments },
             { type: 'addEvents',      items: found.events },
           ],
         };
       }
       return { reply: "Ask me to check Canvas for assignments and events." };
     }

  B) Claude with tool use (key stays server-side, read from process.env). Give
     Claude a `sync_canvas` tool; when it calls the tool, run syncCanvas(), feed
     the result back, and return Claude's final text as `reply` plus the found
     items as `actions`. Call the API with plain fetch:

       const r = await fetch('https://api.anthropic.com/v1/messages', {
         method: 'POST',
         headers: {
           'content-type': 'application/json',
           'x-api-key': process.env.ANTHROPIC_API_KEY,
           'anthropic-version': '2023-06-01',
         },
         body: JSON.stringify({ model: 'claude-sonnet-4-6', max_tokens: 1024, messages, tools }),
       });

  CANVAS: use the REST API, not screen-scraping. The student generates an access
  token (Canvas > Account > Settings > New Access Token). Store it server-side
  (see .env.example). Do NOT collect passwords or 2FA codes through the chat.
  Useful endpoints:
    GET {CANVAS_BASE_URL}/api/v1/courses?enrollment_state=active
    GET {CANVAS_BASE_URL}/api/v1/users/self/todo
    GET {CANVAS_BASE_URL}/api/v1/users/self/upcoming_events
  Map each result into the item shapes above, set `canvasId` to the Canvas id,
  and return them.
*/

// Optional: expose the crawl on its own so a "refresh from Canvas" button can
// call POST /api/sync without going through the chat. Return { assignments, events }.
async function syncCanvas() {
  // TODO (AI team): call Canvas with the stored token and map results.
  return { assignments: [], events: [] };
}

// ---------------------------------------------------------------------------
//  PLACEHOLDER MOCK (delete when the real AI lands)
//  Simulates the login -> two-factor -> sync conversation so the demo works.
//  crawlStage is per-process, which is fine for a single-user demo.
// ---------------------------------------------------------------------------
const daysFromNow = (n) => {
  const d = new Date();
  d.setHours(9, 0, 0, 0);
  d.setDate(d.getDate() + n);
  return d.toISOString();
};

let crawlStage = 'idle';

async function mockCrawlerReply({ message }) {
  const text = String(message || '').toLowerCase();
  await new Promise((r) => setTimeout(r, 400));

  if (crawlStage === 'idle') {
    if (/(canvas|assignment|homework|due|deadline|check|look|scan|sync|event|class|course)/.test(text)) {
      crawlStage = 'awaiting_login';
      return { reply: "I can pull that from Canvas. First I need to sign in — what's your Canvas username?" };
    }
    return { reply: "I can scan Canvas for upcoming assignments and events, or help manage your to-do list. Try: \"Look through my Canvas for upcoming assignments.\"" };
  }

  if (crawlStage === 'awaiting_login') {
    crawlStage = 'awaiting_2fa';
    return { reply: "Got it. Pitt requires two-factor authentication — approve the Duo push, then reply with the code (or just \"approved\")." };
  }

  crawlStage = 'idle';
  return {
    reply: "Approved. I scanned your active courses and found 1 assignment, 1 exam, and 1 event. Adding them to your board.",
    actions: [
      { type: 'addAssignments', items: [ fill('assignment', { title: 'Midterm study guide', course: 'CS 1501 — Algorithms', dueISO: daysFromNow(4), estimateMins: 120, canvasId: 'canvas-101' }) ] },
      { type: 'addExams',       items: [ fill('exam', { title: 'Final project demo', course: 'CS 1699 — Capstone', dueISO: daysFromNow(6), location: 'Sennott 5317', canvasId: 'canvas-301' }) ] },
      { type: 'addEvents',      items: [ fill('event', { title: 'Review session', location: 'Benedum G30', startISO: daysFromNow(2), canvasId: 'canvas-201' }) ] },
    ],
  };
}

module.exports = { handleAssistantMessage, syncCanvas, TEMPLATES, fill };
