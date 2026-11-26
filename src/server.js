import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { SalonService, HttpError } from './service.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const publicDir = path.join(__dirname, '..', 'public');

export function createServer(service) {
  return http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const pathname = url.pathname;

    try {
      if (req.method === 'GET' && pathname === '/health') {
        sendJson(res, 200, { ok: true, timeUnit: 'integer-minutes', persistence: service.dataFile });
      } else if (pathname.startsWith('/api/')) {
        await handleApi(req, res, url, service);
      } else {
        serveStatic(req, res, pathname);
      }
    } catch (error) {
      sendError(res, error);
    }
  });
}

async function readJson(req) {
  if (req.method === 'GET' || req.method === 'HEAD') return {};
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 2_000_000) throw new HttpError(413, 'BODY_TOO_LARGE', 'Request body is too large');
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString('utf8');
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    throw new HttpError(400, 'INVALID_JSON', 'Request body must be valid JSON');
  }
}

function sendJson(res, status, body) {
  const payload = JSON.stringify(body, null, 2);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(payload),
    'cache-control': 'no-store',
  });
  res.end(payload);
}

function sendError(res, error) {
  if (error instanceof HttpError) {
    sendJson(res, error.status, { error: error.code, message: error.message, details: error.details ?? null });
    return;
  }
  if (error?.status && Number.isInteger(error.status) && error.status < 500) {
    sendJson(res, error.status, { error: error.code ?? 'REQUEST_ERROR', message: error.message, details: error.details ?? null });
    return;
  }
  console.error(error);
  sendJson(res, error.status ?? 500, { error: error.code ?? 'INTERNAL_ERROR', message: error.message ?? 'Unexpected error' });
}

function match(pattern, pathname) {
  const pp = pattern.split('/').filter(Boolean);
  const ap = pathname.split('/').filter(Boolean);
  if (pp.length !== ap.length) return null;
  const params = {};
  for (let i = 0; i < pp.length; i += 1) {
    if (pp[i].startsWith(':')) params[pp[i].slice(1)] = decodeURIComponent(ap[i]);
    else if (pp[i] !== ap[i]) return null;
  }
  return params;
}

async function handleApi(req, res, url, service) {
  const { pathname } = url;
  const body = await readJson(req);
  const query = Object.fromEntries(url.searchParams.entries());

  if (req.method === 'GET' && pathname === '/api/health') {
    return sendJson(res, 200, { ok: true, timeUnit: 'integer-minutes', persistence: service.dataFile });
  }

  if (req.method === 'GET' && pathname === '/api/state') {
    return sendJson(res, 200, service.getState());
  }

  if (req.method === 'POST' && pathname === '/api/admin/reset') {
    if (process.env.ALLOW_RESET === 'false') throw new HttpError(403, 'RESET_DISABLED', 'Reset is disabled');
    return sendJson(res, 200, service.reset());
  }

  if (req.method === 'GET' && pathname === '/api/staff') return sendJson(res, 200, service.db.staff);
  if (req.method === 'POST' && pathname === '/api/staff') return sendJson(res, 201, service.addStaff(body));
  let params = match('/api/staff/:id/schedule', pathname);
  if (req.method === 'POST' && params) return sendJson(res, 200, service.updateStaffSchedule(params.id, body));
  params = match('/api/staff/:id', pathname);
  if (req.method === 'PATCH' && params) return sendJson(res, 200, service.updateStaffSchedule(params.id, body));

  if (req.method === 'GET' && pathname === '/api/chairs') return sendJson(res, 200, service.db.chairs);
  if (req.method === 'POST' && pathname === '/api/chairs') return sendJson(res, 201, service.addChair(body));
  params = match('/api/chairs/:id', pathname);
  if (req.method === 'PATCH' && params) return sendJson(res, 200, service.updateChair(params.id, body));

  if (req.method === 'GET' && pathname === '/api/services') return sendJson(res, 200, service.db.services);
  if (req.method === 'POST' && pathname === '/api/services') return sendJson(res, 201, service.addService(body));

  if (req.method === 'POST' && pathname === '/api/quotes/insert') {
    return sendJson(res, 200, service.quoteInsertion({ ...query, ...body }));
  }
  if (req.method === 'POST' && (pathname === '/api/insertions' || pathname === '/api/appointments/insert')) {
    return sendJson(res, 201, service.confirmInsertion(body));
  }
  if ((req.method === 'POST' || req.method === 'GET') && pathname === '/api/schedule/recover') {
    const input = req.method === 'GET' ? query : body;
    return sendJson(res, 200, service.recover(input));
  }

  if (req.method === 'GET' && pathname === '/api/appointments') return sendJson(res, 200, service.db.appointments);
  if (req.method === 'POST' && pathname === '/api/appointments') {
    const result = service.bookAppointment(body);
    return sendJson(res, 201, result);
  }

  params = match('/api/appointments/:id/receipt', pathname);
  if (req.method === 'GET' && params) return sendJson(res, 200, service.getReceipt(params.id));

  params = match('/api/appointments/:id/movable-chairs', pathname);
  if (req.method === 'GET' && params) return sendJson(res, 200, service.movableChairs(params.id, query));

  const routes = [
    ['POST', '/api/appointments/:id/check-in', (p, b) => service.checkIn(p.id, b)],
    ['POST', '/api/appointments/:id/sections/:sectionId/complete', (p, b) => service.completeSection(p.id, p.sectionId, b)],
    ['POST', '/api/appointments/:id/mobility', (p, b) => service.assessMobility(p.id, b)],
    ['POST', '/api/appointments/:id/move', (p, b) => service.moveChair(p.id, b)],
    ['POST', '/api/appointments/:id/handoffs', (p, b) => service.acceptHandoff(p.id, b)],
    ['POST', '/api/appointments/:id/unroll/start', (p, b) => service.startUnroll(p.id, b)],
    ['POST', '/api/appointments/:id/unroll/complete', (p, b) => service.completeUnroll(p.id, b)],
    ['POST', '/api/appointments/:id/checkout', (p, b) => service.checkout(p.id, b)],
    ['POST', '/api/appointments/:id/cancel', (p, b) => service.cancel(p.id, b)],
    ['POST', '/api/appointments/:id/lateness', (p, b) => service.reportLateness(p.id, b)],
  ];

  for (const [method, pattern, handler] of routes) {
    const p = match(pattern, pathname);
    if (p && req.method === method) {
      const result = handler(p, body);
      return sendJson(res, 200, result);
    }
  }

  params = match('/api/appointments/:id', pathname);
  if (req.method === 'GET' && params) return sendJson(res, 200, service.findAppointment(params.id));

  if (pathname.startsWith('/api/')) throw new HttpError(404, 'ROUTE_NOT_FOUND', `No route for ${req.method} ${pathname}`);
}

function serveStatic(req, res, pathname) {
  let relative = pathname === '/' ? 'index.html' : pathname.replace(/^\/+/, '');
  let file = path.normalize(path.join(publicDir, relative));
  if (file !== publicDir && !file.startsWith(publicDir + path.sep)) {
    res.writeHead(403);
    return res.end('Forbidden');
  }
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    file = path.join(publicDir, 'index.html');
  }
  const ext = path.extname(file);
  const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json' };
  res.writeHead(200, { 'content-type': types[ext] ?? 'application/octet-stream' });
  fs.createReadStream(file).pipe(res);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const dataFile = process.env.SALON_DATA_FILE
    ? path.resolve(process.env.SALON_DATA_FILE)
    : path.join(process.cwd(), 'data', 'salon.json');
  const service = new SalonService({ dataFile });
  const port = Number(process.env.PORT ?? 3000);
  const host = process.env.HOST ?? '0.0.0.0';
  createServer(service).listen(port, host, () => {
    console.log(`Salon booking service listening on http://${host}:${port}`);
    console.log(`Data file: ${dataFile}`);
  });
}
