const crypto = require('crypto');

const GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth';
const GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token';
const CALENDAR_API = 'https://www.googleapis.com/calendar/v3';
const SCOPES = ['https://www.googleapis.com/auth/calendar.events'];

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is not configured`);
  return value;
}

function redirectUri() {
  return process.env.GOOGLE_REDIRECT_URI || `http://localhost:${process.env.PORT || 8787}/api/google/callback`;
}

function authorizationUrl(state) {
  const params = new URLSearchParams({
    client_id: required('GOOGLE_CLIENT_ID'),
    redirect_uri: redirectUri(),
    response_type: 'code',
    access_type: 'offline',
    prompt: 'consent',
    scope: SCOPES.join(' '),
    state,
  });
  return `${GOOGLE_AUTH_URL}?${params}`;
}

async function googleJson(url, options) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error_description || body.error || `Google request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return body;
}

async function exchangeCode(code) {
  return googleJson(GOOGLE_TOKEN_URL, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      code,
      client_id: required('GOOGLE_CLIENT_ID'),
      client_secret: required('GOOGLE_CLIENT_SECRET'),
      redirect_uri: redirectUri(),
      grant_type: 'authorization_code',
    }),
  });
}

async function accessToken(connection) {
  if (connection.accessToken && connection.expiresAt > Date.now() + 60_000) return connection;
  const refreshed = await googleJson(GOOGLE_TOKEN_URL, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      client_id: required('GOOGLE_CLIENT_ID'),
      client_secret: required('GOOGLE_CLIENT_SECRET'),
      refresh_token: connection.refreshToken,
      grant_type: 'refresh_token',
    }),
  });
  return {
    ...connection,
    accessToken: refreshed.access_token,
    expiresAt: Date.now() + (Number(refreshed.expires_in) || 3600) * 1000,
  };
}

async function listCalendars(connection) {
  const current = await accessToken(connection);
  const result = await googleJson(`${CALENDAR_API}/users/me/calendarList`, {
    headers: { authorization: `Bearer ${current.accessToken}` },
  });
  return { connection: current, calendars: result.items || [] };
}

function eventId(item) {
  return `officehours_${crypto.createHash('sha256').update(String(item.id)).digest('hex')}`;
}

function toGoogleEvent(kind, item) {
  const start = kind === 'events' ? item.startISO : item.dueISO;
  if (!start || !item.id) return null;
  const title = item.title || item.text || 'Office Hours item';
  const location = item.location || undefined;
  const description = kind === 'assignments' || kind === 'exams'
    ? [item.course, item.estimateMins ? `Estimated effort: ${item.estimateMins} minutes` : ''].filter(Boolean).join('\n')
    : '';
  const startDate = new Date(start);
  if (Number.isNaN(startDate.getTime())) return null;
  const endDate = new Date(startDate.getTime() + (kind === 'events' ? 60 : 30) * 60 * 1000);
  return {
    id: eventId(item),
    summary: title,
    location,
    description,
    start: { dateTime: startDate.toISOString() },
    end: { dateTime: endDate.toISOString() },
    extendedProperties: { private: { officeHoursId: String(item.id), officeHoursKind: kind } },
  };
}

async function upsertEvent(connection, calendarId, event) {
  const current = await accessToken(connection);
  const response = await fetch(`${CALENDAR_API}/calendars/${encodeURIComponent(calendarId)}/events/${encodeURIComponent(event.id)}`, {
    method: 'PUT',
    headers: { authorization: `Bearer ${current.accessToken}`, 'content-type': 'application/json' },
    body: JSON.stringify(event),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(body.error?.message || `Calendar event failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function syncBoard(connection, board, calendarId = 'primary') {
  let current = await accessToken(connection);
  const synced = [];
  for (const kind of ['assignments', 'exams', 'events']) {
    for (const item of board[kind] || []) {
      const event = toGoogleEvent(kind, item);
      if (!event) continue;
      const saved = await upsertEvent(current, calendarId, event);
      synced.push({ kind, id: item.id, googleEventId: saved.id, link: saved.htmlLink });
      current = await accessToken(current);
    }
  }
  return { connection: current, synced };
}

module.exports = { authorizationUrl, exchangeCode, listCalendars, syncBoard };
