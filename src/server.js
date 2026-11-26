import http from 'node:http';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { DispatchService, DomainError, createInitialState } from './domain.js';
import { JsonStore } from './store.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_DATA_FILE = path.join(__dirname, '..', 'data', 'service-state.json');

async function readJsonBody(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 2_000_000) throw new DomainError(413, 'BODY_TOO_LARGE', '请求体过大');
    chunks.push(chunk);
  }
  if (chunks.length === 0) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    throw new DomainError(400, 'INVALID_JSON', '请求体不是合法 JSON');
  }
}

function hashRequest(method, pathname, body) {
  return crypto.createHash('sha256').update(`${method}\n${pathname}\n${JSON.stringify(body)}`).digest('hex');
}

export function createServer(options = {}) {
  const dataFile = options.dataFile || process.env.DATA_FILE || DEFAULT_DATA_FILE;
  const store = options.store || new JsonStore(dataFile);
  let service;
  let initialized = false;

  async function init() {
    if (initialized) return;
    const state = (await store.read()) || createInitialState();
    service = new DispatchService(state);
    server.service = service;
    initialized = true;
  }

  const server = http.createServer(async (req, res) => {
    try {
      await init();
      const url = new URL(req.url, 'http://localhost');
      const pathname = url.pathname.replace(/\/+$/, '') || '/';
      const method = req.method || 'GET';
      const body = ['POST', 'PUT', 'PATCH'].includes(method) ? await readJsonBody(req) : {};
      const requestId = req.headers['idempotency-key'] || body.requestId || null;
      const mutate = ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method);

      if (requestId && mutate) {
        const existing = service.state.idempotency[requestId];
        const requestHash = hashRequest(method, pathname, body);
        if (existing?.completedAt) {
          if (existing.requestHash !== requestHash) {
            throw new DomainError(409, 'IDEMPOTENCY_KEY_CONFLICT', '相同 requestId 不能用于不同请求', { requestId });
          }
          res.writeHead(existing.status, { 'content-type': 'application/json; charset=utf-8', 'x-idempotent-replay': 'true' });
          res.end(JSON.stringify({ ...existing.body, requestId, idempotentReplay: true, duplicate: true }));
          return;
        }
        service.state.idempotency[requestId] = { requestHash, status: 200, body: null, startedAt: new Date().toISOString() };
      }

      const result = await route({ method, pathname, url, body, service });
      const status = result.status || 200;
      const responseBody = result.body ?? result;

      if (requestId && mutate) {
        service.state.idempotency[requestId] = {
          requestHash: hashRequest(method, pathname, body),
          status,
          body: responseBody,
          completedAt: new Date().toISOString(),
        };
      }
      if (mutate) await store.write(service.state);

      res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify(responseBody));
    } catch (error) {
      const status = error instanceof DomainError ? error.status : 500;
      const payload = {
        error: error instanceof DomainError ? error.code : 'INTERNAL_ERROR',
        message: error.message,
        ...(error instanceof DomainError ? error.details : {}),
      };
      if (status >= 500) console.error(error);
      res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify(payload));
    }
  });

  server.service = service;
  server.init = init;
  server.store = store;
  return server;
}

async function route({ method, pathname, url, body, service }) {
  if (method === 'GET' && (pathname === '/' || pathname === '')) {
    return {
      body: {
        service: 'neighbor-carpool-dispatch',
        version: '1.0.0',
        answer: '同站名不等于可换乘；必须有实际下/到站、可验证走行时间，并在上车截止前到达上客点。',
        endpoints: [
          'GET /health',
          'POST /stations',
          'POST /walk-paths',
          'PATCH /walk-paths/:id',
          'POST /cars',
          'POST /cars/:id/events',
          'POST /tickets',
          'GET /tickets/:code/location',
          'POST /tickets/:code/board',
          'POST /tickets/:code/alight',
          'POST /tickets/:code/change-station',
          'POST /transfers/evaluate',
          'POST /handoffs',
          'POST /handoffs/:id/confirm',
          'POST /dispatches/on-demand',
          'POST /dispatches/fallback',
          'POST /dispatches/:id/accept',
          'POST /operator/allocate-fallback',
          'GET /messages',
        ],
      },
    };
  }

  if (method === 'GET' && pathname === '/health') {
    return { body: { ok: true, service: 'neighbor-carpool-dispatch', time: new Date().toISOString() } };
  }

  if (method === 'GET' && pathname === '/admin/state') {
    return { body: service.state };
  }

  if (method === 'POST' && pathname === '/admin/reset') {
    service.state = createInitialState();
    return { status: 200, body: { reset: true } };
  }

  if (method === 'POST' && pathname === '/stations') return { status: 201, body: service.createStation(body) };
  if (method === 'GET' && pathname === '/stations') return { body: Object.values(service.state.stations) };

  if (method === 'POST' && pathname === '/walk-paths') return { status: 201, body: service.createWalkPath(body) };
  const walkMatch = pathname.match(/^\/walk-paths\/([^/]+)$/);
  if (method === 'PATCH' && walkMatch) return { body: service.updateWalkPath(decodeURIComponent(walkMatch[1]), body) };

  if (method === 'POST' && pathname === '/cars') return { status: 201, body: service.createCar(body) };
  if (method === 'GET' && pathname === '/cars') return { body: Object.values(service.state.cars) };
  const carMatch = pathname.match(/^\/cars\/([^/]+)$/);
  if (method === 'GET' && carMatch) return { body: service.getById('cars', decodeURIComponent(carMatch[1]), 'car') };
  const carEventMatch = pathname.match(/^\/cars\/([^/]+)\/events$/);
  if (method === 'POST' && carEventMatch) return { body: service.reportCarEvent(decodeURIComponent(carEventMatch[1]), body) };

  if (method === 'POST' && pathname === '/tickets') return { status: 201, body: service.createTicket(body) };
  const ticketMatch = pathname.match(/^\/tickets\/([^/]+)$/);
  if (method === 'GET' && ticketMatch) return { body: service.getById('tickets', decodeURIComponent(ticketMatch[1]), 'ticket') };
  const locationMatch = pathname.match(/^\/tickets\/([^/]+)\/location$/);
  if (method === 'GET' && locationMatch) return { body: service.getLocation(decodeURIComponent(locationMatch[1])) };
  const boardMatch = pathname.match(/^\/tickets\/([^/]+)\/board$/);
  if (method === 'POST' && boardMatch) return { body: service.board(decodeURIComponent(boardMatch[1]), body) };
  const alightMatch = pathname.match(/^\/tickets\/([^/]+)\/alight$/);
  if (method === 'POST' && alightMatch) return { body: service.alight(decodeURIComponent(alightMatch[1]), body) };
  const changeStationMatch = pathname.match(/^\/tickets\/([^/]+)\/change-station$/);
  if (method === 'POST' && changeStationMatch) return { body: service.changeTicketStation(decodeURIComponent(changeStationMatch[1]), body) };

  if (method === 'POST' && pathname === '/transfers/evaluate') return { body: service.evaluateTransfer(body) };

  if (method === 'POST' && pathname === '/handoffs') return { status: 201, body: service.createHandoff(body) };
  const handoffMatch = pathname.match(/^\/handoffs\/([^/]+)$/);
  if (method === 'GET' && handoffMatch) return { body: service.getById('handoffs', decodeURIComponent(handoffMatch[1]), 'handoff') };
  const confirmMatch = pathname.match(/^\/handoffs\/([^/]+)\/confirm$/);
  if (method === 'POST' && confirmMatch) return { body: service.confirmHandoff(decodeURIComponent(confirmMatch[1]), body) };

  if (method === 'POST' && pathname === '/dispatches/on-demand') return { status: 201, body: service.createOnDemandDispatch(body.ticketCode, body) };
  if (method === 'POST' && pathname === '/dispatches/fallback') return { status: 201, body: service.createFallbackDispatch(body.ticketCode, body) };
  const dispatchMatch = pathname.match(/^\/dispatches\/([^/]+)$/);
  if (method === 'GET' && dispatchMatch) return { body: service.getById('dispatches', decodeURIComponent(dispatchMatch[1]), 'dispatch') };
  const acceptDispatchMatch = pathname.match(/^\/dispatches\/([^/]+)\/accept$/);
  if (method === 'POST' && acceptDispatchMatch) return { body: service.acceptDispatch(decodeURIComponent(acceptDispatchMatch[1]), body) };

  if (method === 'POST' && pathname === '/operator/allocate-fallback') return { body: service.allocateFallback(body) };
  if (method === 'GET' && pathname === '/messages') return { body: service.state.messages };

  return { status: 404, body: { error: 'NOT_FOUND', message: `${method} ${pathname} 不存在` } };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const port = Number(process.env.PORT || 3000);
  const host = process.env.HOST || '127.0.0.1';
  const server = createServer();
  server.init().then(() => {
    server.listen(port, host, () => {
      console.log(`carpool dispatch service listening on http://${host}:${port}`);
      console.log(`state file: ${server.store.filePath}`);
    });
  });
}
