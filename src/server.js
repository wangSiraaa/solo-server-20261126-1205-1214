'use strict';

const http = require('http');
const { CoListeningService, ServiceError } = require('./service');

/**
 * Routes (all JSON):
 *
 *  POST   /rooms                                  create room
 *  GET    /rooms/:room                            room summary
 *  POST   /rooms/:room/members                    join a member
 *  POST   /rooms/:room/connections                open a connection (returns snapshot)
 *  DELETE /rooms/:room/connections/:connId?memberId=...   close a connection
 *  GET    /rooms/:room/snapshot?memberId=&connId=         effective permits + relisten
 *  POST   /rooms/:room/reports                    submit played intervals
 *  DELETE /rooms/:room/members/:memberId/reports  clear all reports of a member
 *  POST   /rooms/:room/manifest                   host edits a member's manifest
 *  POST   /rooms/:room/discussion/point           host sets discussion point
 *  POST   /rooms/:room/discussion/round           host sets round membership
 *  POST   /rooms/:room/discussion/defer           host moves a member to next round
 *  POST   /rooms/:room/discussion/open            host opens discussion
 *  POST   /rooms/:room/discussion/end             host ends discussion
 *  GET    /rooms/:room/discussion?memberId=       discussion + readiness
 *  GET    /rooms/:room/who-missing?memberId=      who still owes what
 *  POST   /rooms/:room/permits                    request a permit
 *  GET    /rooms/:room/permits?memberId=          list permits (effective = granted)
 *  POST   /rooms/:room/authority                  transfer host authority
 *  GET    /rooms/:room/audit?since=               event log
 *  GET    /health
 */

function send(res, status, body) {
  const data = JSON.stringify(body, null, 2);
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(data);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let chunks = '';
    let size = 0;
    req.on('data', (c) => {
      size += c.length;
      if (size > 2 * 1024 * 1024) {
        reject(new ServiceError(413, 'PAYLOAD_TOO_LARGE', 'body too large'));
        req.destroy();
        return;
      }
      chunks += c;
    });
    req.on('end', () => {
      if (!chunks.trim()) return resolve({});
      try {
        resolve(JSON.parse(chunks));
      } catch (e) {
        reject(new ServiceError(400, 'INVALID_JSON', 'could not parse JSON body'));
      }
    });
    req.on('error', reject);
  });
}

function createApp(options = {}) {
  const service = options.service || new CoListeningService(options);
  return { service };
}

function createServer(options = {}) {
  const service = options.service || new CoListeningService(options);

  const server = http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, 'http://localhost');
      const parts = url.pathname.split('/').filter(Boolean).map(decodeURIComponent);
      const q = Object.fromEntries(url.searchParams.entries());
      const method = req.method.toUpperCase();

      if (method === 'GET' && parts.length === 1 && parts[0] === 'health') {
        return send(res, 200, { ok: true });
      }

      if (parts[0] !== 'rooms') return send(res, 404, { error: 'NOT_FOUND' });

      // POST /rooms
      if (method === 'POST' && parts.length === 1) {
        const body = await readBody(req);
        return send(res, 201, service.createRoom(body));
      }

      const roomId = parts[1];
      if (!roomId) return send(res, 404, { error: 'NOT_FOUND' });

      const rest = parts.slice(2);
      const route = `${method} ${rest.join('/')}`;

      switch (route) {
        case 'GET ': {
          return send(res, 200, service.roomSummary(roomId));
        }
        case 'POST members': {
          const body = await readBody(req);
          return send(res, 201, service.join({ roomId, ...body }));
        }
        case 'POST members/leave': {
          const body = await readBody(req);
          return send(res, 200, service.leave({ roomId, ...body }));
        }
        case 'DELETE members': {
          // not supported as a whole-member delete; navigate to subroutes
          return send(res, 404, { error: 'NOT_FOUND' });
        }
        case 'POST connections': {
          const body = await readBody(req);
          return send(res, 201, service.connect({ roomId, ...body }));
        }
        case 'GET connections': {
          return send(res, 200, service.roomSummary(roomId).members);
        }
        case 'GET snapshot': {
          return send(res, 200, service.snapshot({ roomId, memberId: q.memberId, connId: q.connId }));
        }
        case 'POST reports': {
          const body = await readBody(req);
          return send(res, 201, service.report({ roomId, ...body }));
        }
        case 'POST manifest': {
          const body = await readBody(req);
          return send(res, 200, service.editManifest({ roomId, ...body }));
        }
        case 'POST discussion/point': {
          const body = await readBody(req);
          return send(res, 200, service.setDiscussionPoint({ roomId, ...body }));
        }
        case 'POST discussion/round': {
          const body = await readBody(req);
          return send(res, 200, service.setRound({ roomId, ...body }));
        }
        case 'POST discussion/defer': {
          const body = await readBody(req);
          return send(res, 200, service.deferMember({ roomId, ...body }));
        }
        case 'POST discussion/open': {
          const body = await readBody(req);
          return send(res, 200, service.openDiscussion({ roomId, ...body }));
        }
        case 'POST discussion/end': {
          const body = await readBody(req);
          return send(res, 200, service.endDiscussion({ roomId, ...body }));
        }
        case 'GET discussion': {
          return send(res, 200, service.discussionState({ roomId, memberId: q.memberId }));
        }
        case 'GET who-missing': {
          return send(res, 200, service.whoMissing({ roomId, memberId: q.memberId }));
        }
        case 'POST permits': {
          const body = await readBody(req);
          return send(res, 201, service.requestPermit({ roomId, ...body }));
        }
        case 'GET permits': {
          return send(res, 200, service.permitsFor({ roomId, memberId: q.memberId }));
        }
        case 'POST authority': {
          const body = await readBody(req);
          return send(res, 200, service.transferAuthority({ roomId, ...body }));
        }
        case 'GET audit': {
          return send(res, 200, service.audit({ roomId, since: q.since }));
        }
        default:
          break;
      }

      // Parameterised subroutes.
      // DELETE /rooms/:room/connections/:connId?memberId=...
      if (method === 'DELETE' && rest[0] === 'connections' && rest[1]) {
        return send(res, 200, service.disconnect({ roomId, memberId: q.memberId, connId: rest[1] }));
      }
      // DELETE /rooms/:room/members/:memberId/reports
      if (method === 'DELETE' && rest[0] === 'members' && rest[1] && rest[2] === 'reports') {
        return send(res, 200, service.deleteReports({ roomId, memberId: rest[1] }));
      }
      // GET /rooms/:room/members/:memberId/progress
      if (method === 'GET' && rest[0] === 'members' && rest[1] && rest[2] === 'progress') {
        return send(res, 200, service.progress({ roomId, memberId: rest[1] }));
      }

      return send(res, 404, { error: 'NOT_FOUND', method, path: url.pathname });
    } catch (e) {
      if (e instanceof ServiceError) {
        return send(res, e.status, { error: e.code, message: e.message, ...e.extra });
      }
      // eslint-disable-next-line no-console
      console.error('unhandled', e);
      return send(res, 500, { error: 'INTERNAL', message: e.message });
    }
  });

  server.service = service;
  return server;
}

module.exports = { createServer, createApp };

if (require.main === module) {
  const port = Number(process.env.PORT || 8787);
  const server = createServer();
  server.listen(port, () => {
    // eslint-disable-next-line no-console
    console.log(`co-listening service listening on http://localhost:${port}`);
  });
}
