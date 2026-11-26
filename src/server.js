import { createServer as createHttpServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Observatory } from './domain.js';
import { HttpError } from './errors.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PUBLIC_DIR = path.join(__dirname, '..', 'public');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
};

export function createServer(options = {}) {
  const observatory = options.observatory || new Observatory({ seed: options.seed ?? true });

  const server = createHttpServer(async (req, res) => {
    res.setHeader('access-control-allow-origin', '*');
    res.setHeader('access-control-allow-methods', 'GET,POST,PUT,PATCH,DELETE,OPTIONS');
    res.setHeader('access-control-allow-headers', 'content-type,idempotency-key,x-logical-clock');
    if (req.method === 'OPTIONS') {
      res.writeHead(204);
      res.end();
      return;
    }

    const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
    let pathname = decodeURIComponent(url.pathname);
    const topLevelResource = pathname.split('/').filter(Boolean)[0];
    const apiAliases = new Set([
      'stations', 'commissions', 'orders', 'frames', 'observations', 'arrivals',
      'windows', 'joint-windows', 'dispatch', 'dispatches', 'assignments',
      'relay', 'reports', 'cases', 'events', 'incidents', 'clock', 'trace',
    ]);
    if (topLevelResource && apiAliases.has(topLevelResource) && !pathname.startsWith('/api/')) {
      pathname = `/api${pathname}`;
    }

    try {
      if (pathname === '/api' || pathname.startsWith('/api/') || pathname === '/health') {
        await handleApi(req, res, url, pathname, observatory);
        return;
      }
      await serveStatic(req, res, pathname);
    } catch (err) {
      sendError(res, err);
    }
  });

  return { server, observatory };
}

async function readBody(req) {
  if (!['POST', 'PUT', 'PATCH', 'DELETE'].includes(req.method)) return {};
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 2_000_000) {
      throw new HttpError(413, 'BODY_TOO_LARGE', '请求体超过 2MB');
    }
    chunks.push(chunk);
  }
  if (chunks.length === 0) return {};
  const text = Buffer.concat(chunks).toString('utf8').trim();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch (err) {
    throw new HttpError(400, 'INVALID_JSON', '请求体不是合法 JSON');
  }
}

function remoteClockFrom(req, body, url) {
  return body.logicalClock
    ?? body.remoteLogicalClock
    ?? req.headers['x-logical-clock']
    ?? url.searchParams.get('logicalClock')
    ?? undefined;
}

function idempotencyKeyFrom(req, body) {
  return req.headers['idempotency-key'] || body.idempotencyKey || undefined;
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload, null, 2);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  });
  res.end(body);
}

function sendError(res, err) {
  const status = err.status || 500;
  if (status >= 500) console.error(err);
  sendJson(res, status, {
    error: {
      code: err.code || 'INTERNAL_ERROR',
      message: err.message || '服务器内部错误',
      details: err.details,
    },
  });
}

async function serveStatic(req, res, pathname) {
  let relative = pathname === '/' ? 'index.html' : pathname.replace(/^\/+/, '');
  if (pathname === '/reader' || pathname === '/reader/') relative = 'reader.html';
  const filePath = path.normalize(path.join(PUBLIC_DIR, relative));
  if (!filePath.startsWith(PUBLIC_DIR)) {
    throw new HttpError(403, 'FORBIDDEN', '非法路径');
  }
  if (!existsSync(filePath)) {
    // Simple SPA-ish fallback for reader entry.
    if (pathname.startsWith('/reader')) {
      const reader = path.join(PUBLIC_DIR, 'reader.html');
      const html = await readFile(reader);
      res.writeHead(200, { 'content-type': MIME['.html'] });
      res.end(html);
      return;
    }
    throw new HttpError(404, 'NOT_FOUND', `没有静态文件 ${pathname}`);
  }
  const data = await readFile(filePath);
  res.writeHead(200, { 'content-type': MIME[path.extname(filePath)] || 'application/octet-stream' });
  res.end(data);
}

function listPayload(items) {
  return { items, count: items.length };
}

async function handleApi(req, res, url, pathname, obs) {
  const body = await readBody(req);
  const rc = remoteClockFrom(req, body, url);
  const idem = idempotencyKeyFrom(req, body);
  const method = req.method;
  const segments = pathname.split('/').filter(Boolean); // api, ...

  if (method === 'GET' && pathname === '/health') {
    sendJson(res, 200, { ok: true, service: 'occultation-duty-service', ...obs.publicState() });
    return;
  }

  if (segments[0] !== 'api') {
    throw new HttpError(404, 'NOT_FOUND', pathname);
  }
  const rest = segments.slice(1);
  if (rest[0] === 'orders') rest[0] = 'commissions';
  if (rest[0] === 'joint-windows') rest[0] = 'windows';
  if (rest[0] === 'assignments') rest[0] = 'dispatches';
  if (rest[0] === 'observations') rest[0] = 'frames';

  if (method === 'GET' && rest.length === 0) {
    sendJson(res, 200, {
      service: 'occultation-duty-service',
      roles: ['stations', 'host', 'reader'],
      endpoints: [
        'GET /health',
        'POST /api/admin/reset',
        'GET /api/events',
        'GET /api/incidents',
        'GET /api/stations',
        'POST /api/stations',
        'POST /api/stations/:id/sync',
        'PUT /api/stations/:id/clock',
        'POST /api/stations/:id/lost',
        'POST /api/stations/:id/online',
        'POST /api/stations/:id/frames',
        'GET /api/frames',
        'POST /api/arrivals',
        'GET /api/arrivals',
        'GET/POST /api/commissions',
        'PATCH /api/commissions/:id',
        'POST /api/commissions/:id/evaluate',
        'POST /api/commissions/:id/close',
        'GET /api/commissions/:id/trace',
        'GET /reader.html?commissionId=:id',
        'GET/POST /api/windows',
        'POST /api/windows/:id/confirm',
        'POST /api/dispatch/window',
        'POST /api/relay/plan',
        'POST /api/relay/dispatch',
        'GET /api/dispatches',
        'GET /api/reports/:id',
      ],
    });
    return;
  }

  if (method === 'POST' && rest[0] === 'admin' && rest[1] === 'reset') {
    const state = obs.reset({ seed: body.seed !== false });
    sendJson(res, 200, { ok: true, ...state });
    return;
  }

  if (method === 'GET' && rest[0] === 'events') {
    sendJson(res, 200, listPayload(obs.events));
    return;
  }

  if (method === 'GET' && rest[0] === 'incidents') {
    sendJson(res, 200, listPayload(obs.listIncidents(Object.fromEntries(url.searchParams))));
    return;
  }

  if (method === 'GET' && rest[0] === 'clock') {
    sendJson(res, 200, { logicalClock: obs.logicalClock, formula: 'L = max(L, remoteL)+1' });
    return;
  }

  // Stations
  if (rest[0] === 'stations') {
    if (method === 'GET' && rest.length === 1) {
      sendJson(res, 200, listPayload(obs.listStations()));
      return;
    }
    if (method === 'POST' && rest.length === 1) {
      sendJson(res, 201, obs.registerStation(body, rc));
      return;
    }
    const stationId = rest[1];
    if (method === 'GET' && rest.length === 2) {
      sendJson(res, 200, obs.getStation(stationId));
      return;
    }
    if ((method === 'PATCH' || method === 'PUT') && rest.length === 2) {
      sendJson(res, 200, obs.updateStation(stationId, body, rc));
      return;
    }
    if (rest[2] === 'sync' && method === 'POST' && rest.length === 3) {
      sendJson(res, 200, obs.addSync(stationId, body, rc));
      return;
    }
    if ((rest[2] === 'clock' || rest[2] === 'correction')
      && (method === 'PUT' || method === 'PATCH' || method === 'POST') && rest.length === 3) {
      sendJson(res, 200, obs.setClock(stationId, body, rc));
      return;
    }
    if (rest[2] === 'lost' && method === 'POST' && rest.length === 3) {
      sendJson(res, 200, obs.setStationStatus(stationId, 'lost', body, rc));
      return;
    }
    if ((rest[2] === 'online' || rest[2] === 'heartbeat') && method === 'POST' && rest.length === 3) {
      sendJson(res, 200, obs.setStationStatus(stationId, 'online', body, rc));
      return;
    }
    if ((rest[2] === 'frames' || rest[2] === 'observations') && method === 'POST' && rest.length === 3) {
      sendJson(res, 201, obs.submitFrame(stationId, body, rc, idem));
      return;
    }
  }

  // Frames collection
  if (rest[0] === 'frames') {
    if (method === 'GET') {
      sendJson(res, 200, listPayload(obs.listFrames(Object.fromEntries(url.searchParams))));
      return;
    }
    if (method === 'POST') {
      if (!body.stationId) throw new HttpError(400, 'STATION_REQUIRED', 'POST /api/frames 需要 stationId');
      sendJson(res, 201, obs.submitFrame(body.stationId, body, rc, idem));
      return;
    }
  }

  // Arrivals
  if (rest[0] === 'arrivals') {
    if (method === 'GET') {
      sendJson(res, 200, listPayload(obs.listArrivals(url.searchParams.get('commissionId'))));
      return;
    }
    if (method === 'POST') {
      sendJson(res, 201, obs.recordArrival(body, rc));
      return;
    }
  }

  // Commissions
  if (rest[0] === 'commissions') {
    if (method === 'GET' && rest.length === 1) {
      sendJson(res, 200, listPayload(obs.listCommissions()));
      return;
    }
    if (method === 'POST' && rest.length === 1) {
      sendJson(res, 201, obs.createCommission(body, rc));
      return;
    }
    const id = rest[1];
    if (rest.length === 2) {
      if (method === 'GET') return sendJson(res, 200, obs.getCommission(id));
      if (method === 'PATCH' || method === 'PUT') return sendJson(res, 200, obs.updateCommission(id, body, rc));
    }
    if (['evaluate', 'assess', 'verify'].includes(rest[2]) && method === 'POST') {
      sendJson(res, 200, obs.evaluateCommission(id, rc));
      return;
    }
    if (['close', 'close-case', 'decide'].includes(rest[2]) && method === 'POST') {
      sendJson(res, 200, obs.closeCommission(id, body, rc));
      return;
    }
    if (rest[2] === 'trace' && method === 'GET') {
      sendJson(res, 200, obs.traceCommission(id));
      return;
    }
    if (rest[2] === 'makeup-plan' && method === 'GET') {
      sendJson(res, 200, obs.makeupPlan(id));
      return;
    }
  }

  // Reader aliases
  if ((rest[0] === 'trace' || rest[0] === 'reader') && rest[1] && method === 'GET') {
    sendJson(res, 200, obs.traceCommission(rest[1]));
    return;
  }

  // Windows
  if (rest[0] === 'windows') {
    if (method === 'GET' && rest.length === 1) {
      sendJson(res, 200, listPayload(obs.listWindows(Object.fromEntries(url.searchParams))));
      return;
    }
    if (method === 'POST' && rest.length === 1) {
      sendJson(res, 201, obs.createWindow(body, rc));
      return;
    }
    if (rest[2] === 'confirm' && method === 'POST') {
      sendJson(res, 200, obs.confirmWindow(rest[1], rc));
      return;
    }
  }

  // Dispatch
  if ((rest[0] === 'dispatch' || rest[0] === 'dispatches')) {
    if (method === 'GET' && rest.length === 1) {
      sendJson(res, 200, listPayload(obs.listDispatches(url.searchParams.get('commissionId'))));
      return;
    }
    if (method === 'POST' && rest.length === 1) {
      if (body.mode === 'relay') return sendJson(res, 201, obs.dispatchRelay(body, rc, idem));
      if (body.windowId) return sendJson(res, 201, obs.dispatchFromWindow(body, rc, idem));
      throw new HttpError(400, 'DISPATCH_MODE_REQUIRED', '请提供 windowId，或 mode:"relay" 与预报/转向参数');
    }
    if (rest[1] === 'window' && method === 'POST') {
      sendJson(res, 201, obs.dispatchFromWindow(body, rc, idem));
      return;
    }
    if (rest[1] === 'relay' && method === 'POST') {
      sendJson(res, 201, obs.dispatchRelay(body, rc, idem));
      return;
    }
    const dispatchId = rest[1];
    if (rest[2] === 'cancel' && method === 'POST') {
      sendJson(res, 200, obs.cancelDispatch(dispatchId, body, rc));
      return;
    }
    if ((rest[2] === 'transfer' && method === 'POST') || (method === 'PATCH' && rest.length === 2 && body.commissionId)) {
      obs.transferDispatch();
      return;
    }
  }

  // Relay aliases
  if (rest[0] === 'relay') {
    if (rest[1] === 'plan' && method === 'POST') {
      sendJson(res, 200, obs.planRelay(body));
      return;
    }
    if (rest[1] === 'dispatch' && method === 'POST') {
      sendJson(res, 201, obs.dispatchRelay(body, rc, idem));
      return;
    }
  }

  // Reports
  if (rest[0] === 'reports') {
    if (method === 'GET' && rest.length === 1) {
      sendJson(res, 200, listPayload(obs.listReports(url.searchParams.get('commissionId'))));
      return;
    }
    if (method === 'GET' && rest.length === 2) {
      sendJson(res, 200, obs.getReport(rest[1]));
      return;
    }
  }

  if (rest[0] === 'cases' && method === 'GET' && rest.length === 2) {
    const item = obs.caseDecisions.get(rest[1]);
    if (!item) throw new HttpError(404, 'CASE_NOT_FOUND', `找不到结案/待补记录 ${rest[1]}`);
    sendJson(res, 200, JSON.parse(JSON.stringify(item)));
    return;
  }

  throw new HttpError(404, 'API_NOT_FOUND', `没有接口 ${method} ${pathname}`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const port = Number(process.env.PORT || 3000);
  const host = process.env.HOST || '0.0.0.0';
  const { server, observatory } = createServer({ seed: process.env.SEED !== 'false' });
  server.listen(port, host, () => {
    console.log(`掩星联合值守服务已启动: http://${host}:${port}`);
    console.log(`读片员入口示例: http://localhost:${port}/reader.html?commissionId=C-DONE`);
    console.log(JSON.stringify(observatory.publicState()));
  });
}
