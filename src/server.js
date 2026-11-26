'use strict';

/**
 * Tiny zero-dependency HTTP server exposing the dance-party engine.
 *
 * All requests carry a session id in the path. Mutating commands return the
 * fresh view so phones/staff can render immediately. Because a single Node
 * process owns one Engine (single writer), concurrent phone requests are
 * serialised by the event loop — the "who confirmed first wins" rule is exact.
 *
 * Routes:
 *   POST /sessions                       { sessionId, rounds, zones }
 *   POST /sessions/:sid/guests           { number, name, roles, rhythms, targetPartners, roundsPurchased }
 *   POST /sessions/:sid/rest             { number, round, away }
 *   POST /sessions/:sid/zones            { zone, open }
 *   POST /sessions/:sid/consent          { number, round, rhythm, accept }
 *   POST /sessions/:sid/invitations      { round, from, to, fromRole, toRole, zone, source }
 *   POST /sessions/:sid/invitations/:id/confirm { by }
 *   POST /sessions/:sid/invitations/:id/decline { by }
 *   POST /sessions/:sid/invitations/:id/cancel  { reason }
 *   POST /sessions/:sid/rounds/:round/close
 *   POST /sessions/:sid/rounds/:round/plan    { source }
 *   POST /sessions/:sid/makeup
 *   GET  /sessions/:sid/guests/:number/record
 *   GET  /sessions/:sid/guests/:number/met
 *   GET  /sessions/:sid/guests/:number/quota
 *   GET  /sessions/:sid/guests/:number/effective?round=
 *   GET  /sessions/:sid/schedule
 *   POST /sessions/:sid/guests/:number/redeem
 */

const http = require('http');
const { Engine, DomainError } = require('./engine');

function createServer({ engine }) {
  return http.createServer(async (req, res) => {
    try {
      const result = await route(engine, req);
      send(res, 200, result);
    } catch (err) {
      if (err instanceof DomainError) {
        send(res, err.code === 'NO_SESSION' || err.code === 'NO_GUEST' ? 404 : 400, {
          error: err.code,
          message: err.message,
        });
      } else {
        send(res, 500, { error: 'INTERNAL', message: err.message });
      }
    }
  });
}

async function route(engine, req) {
  const url = new URL(req.url, 'http://localhost');
  const parts = url.pathname.split('/').filter(Boolean);
  const body = await readBody(req);
  const method = req.method.toUpperCase();

  // POST /sessions
  if (parts[0] === 'sessions' && parts.length === 1 && method === 'POST') {
    engine.createSession(body);
    return { ok: true };
  }
  if (parts[0] !== 'sessions') throw new DomainError('NOT_FOUND', 'unknown route');
  const sid = parts[1];
  if (!sid) throw new DomainError('NOT_FOUND', 'unknown route');

  // /sessions/:sid/guests ...
  if (parts[2] === 'guests' && parts.length === 3 && method === 'POST') {
    return engine.registerGuest(sid, body);
  }
  if (parts[2] === 'guests' && parts[3]) {
    const number = decodeURIComponent(parts[3]);
    if (parts.length === 4 && method === 'POST') {
      return engine.registerGuest(sid, { ...body, number });
    }
    if (parts[4] === 'record' && method === 'GET') {
      return engine.meetingRecord(sid, number);
    }
    if (parts[4] === 'met' && method === 'GET') {
      return { number, met: engine.metPartners(sid, number) };
    }
    if (parts[4] === 'quota' && method === 'GET') {
      return engine.quota(sid, number);
    }
    if (parts[4] === 'effective' && method === 'GET') {
      const round = Number(url.searchParams.get('round'));
      return engine.effectiveAssignment(sid, { number, round });
    }
    if (parts[4] === 'redeem' && method === 'POST') {
      return engine.redeem(sid, { number });
    }
  }

  if (parts[2] === 'rest' && method === 'POST') return engine.setRest(sid, body);
  if (parts[2] === 'zones' && method === 'POST') return engine.setZone(sid, body);
  if (parts[2] === 'consent' && method === 'POST') return engine.setConsent(sid, body);

  if (parts[2] === 'invitations') {
    if (parts.length === 3 && method === 'POST') return engine.proposeInvitation(sid, body);
    if (parts[3] && parts[4] === 'confirm' && method === 'POST') {
      return engine.confirmInvitation(sid, { invitationId: parts[3], by: body.by });
    }
    if (parts[3] && parts[4] === 'decline' && method === 'POST') {
      return engine.declineInvitation(sid, { invitationId: parts[3], by: body.by });
    }
    if (parts[3] && parts[4] === 'cancel' && method === 'POST') {
      return engine.cancelInvitation(sid, { invitationId: parts[3], reason: body.reason });
    }
  }

  if (parts[2] === 'rounds' && parts[3]) {
    const round = Number(parts[3]);
    if (parts[4] === 'close' && method === 'POST') return engine.closeRound(sid, { round });
    if (parts[4] === 'plan' && method === 'POST') {
      return engine.autoPlanRound(sid, { round, source: body.source });
    }
  }

  if (parts[2] === 'makeup' && method === 'POST') return engine.autoMakeup(sid);
  if (parts[2] === 'schedule' && method === 'GET') return engine.exportSchedule(sid);

  throw new DomainError('NOT_FOUND', `no route for ${method} ${url.pathname}`);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (c) => {
      data += c;
      if (data.length > 1e6) reject(new DomainError('BODY_TOO_LARGE', 'body too large'));
    });
    req.on('end', () => {
      if (!data) return resolve({});
      try {
        resolve(JSON.parse(data));
      } catch (err) {
        reject(new DomainError('BAD_JSON', 'invalid JSON body'));
      }
    });
    req.on('error', reject);
  });
}

function send(res, status, obj) {
  const payload = JSON.stringify(obj);
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(payload) });
  res.end(payload);
}

module.exports = { createServer };

// Allow `node src/server.js <port> <event-log>` for a real running back office.
if (require.main === module) {
  const port = Number(process.argv[2] || 0);
  const logPath = process.argv[3] || require('path').join(__dirname, '..', 'data', 'events.jsonl');
  const engine = new Engine({ filePath: logPath });
  const server = createServer({ engine });
  server.listen(port, () => {
    const addr = server.address();
    // eslint-disable-next-line no-console
    console.log(`dance back office listening on http://127.0.0.1:${addr.port} (log: ${logPath})`);
  });
}
