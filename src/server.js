import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { JsonStore } from './store.js';
import { ApiError } from './errors.js';
import { buildSeededDb, resetSeeded } from './seed.js';
import { runRehearsal, rehearsalSummary } from './rehearsal.js';
import { planNextLink, shortestPath } from './planning.js';
import {
  addSweepReport,
  amendOrderToContract,
  assessOrder,
  closeOrder,
  completeComplaint,
  complaintView,
  createCar,
  createComplaint,
  createShift,
  createTrip,
  dispatchMakeup,
  evaluateMakeup,
  getMapView,
  mustFind,
  reassignFrontage,
  resegment,
  returnTrip,
  setContract,
} from './core.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');
const publicDir = path.join(root, 'public');
const dataFile = process.env.DATA_FILE || path.join(root, 'data', 'coop.json');
const store = new JsonStore(dataFile);
if (!store.db.meta?.seeded) {
  store.transact((db) => resetSeeded(db));
}

function sendJson(res, status, body) {
  const json = JSON.stringify(body, null, 2);
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
  res.end(json);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (chunk) => {
      data += chunk;
      if (data.length > 2_000_000) reject(new ApiError(413, 'BODY_TOO_LARGE', '请求体过大'));
    });
    req.on('end', () => {
      if (!data) return resolve({});
      try {
        resolve(JSON.parse(data));
      } catch {
        reject(new ApiError(400, 'BAD_JSON', '请求体不是合法 JSON'));
      }
    });
    req.on('error', reject);
  });
}

function findComplaintByNo(db, no) {
  return db.complaints.find((c) => c.id === no || c.complaintNo === no);
}

async function handleApi(req, res, url) {
  const { pathname } = url;
  const method = req.method;
  const body = ['POST', 'PUT', 'PATCH'].includes(method) ? await readBody(req) : {};

  if (method === 'GET' && pathname === '/api/health') {
    return sendJson(res, 200, { ok: true, service: 'street-coop-backoffice', minutePolicy: 'integer' });
  }

  if (method === 'GET' && pathname === '/api/graph') {
    return sendJson(res, 200, store.query((db) => ({ nodes: db.nodes, segments: db.segments, curbs: db.curbs, links: db.links })));
  }

  if (method === 'GET' && pathname === '/api/map') {
    return sendJson(res, 200, store.query((db) => getMapView(db)));
  }

  if (method === 'POST' && pathname === '/api/contracts/reassign-face') {
    return sendJson(res, 200, store.transact((db) => reassignFrontage(db, body)));
  }

  if (method === 'POST' && pathname === '/api/cars') {
    return sendJson(res, 201, store.transact((db) => createCar(db, body)));
  }

  if (method === 'GET' && pathname === '/api/merchants') {
    return sendJson(res, 200, store.query((db) => db.merchants.map((m) => ({ ...m, currentContract: db.currentContracts[m.id] ?? [] }))));
  }

  if (method === 'GET' && pathname === '/api/cars') return sendJson(res, 200, store.db.cars);

  if (method === 'POST' && pathname === '/api/reset') {
    const db = store.transact((db) => resetSeeded(db));
    return sendJson(res, 200, { ok: true, seeded: true, counts: { nodes: db.nodes.length, merchants: db.merchants.length, cars: db.cars.length } });
  }

  if (method === 'POST' && pathname === '/api/rehearsal') {
    const result = store.transact((db) => runRehearsal(db, body ?? {}));
    return sendJson(res, result.ok ? 200 : 409, { summary: rehearsalSummary(result), result });
  }

  if (method === 'POST' && pathname === '/api/contracts') {
    const version = store.transact((db) => setContract(db, body));
    return sendJson(res, 201, version);
  }

  if (method === 'POST' && pathname === '/api/graph/resegment') {
    const links = store.transact((db) => resegment(db, body));
    return sendJson(res, 201, { ok: true, links });
  }

  if (method === 'POST' && pathname === '/api/shifts') {
    const shift = store.transact((db) => createShift(db, body));
    return sendJson(res, 201, shift);
  }

  const orderAmendMatch = pathname.match(/^\/api\/orders\/([^/]+)\/amend-contract$/);
  if (method === 'POST' && orderAmendMatch) {
    const result = store.transact((db) => amendOrderToContract(db, { ...body, orderId: decodeURIComponent(orderAmendMatch[1]) }));
    return sendJson(res, 200, result);
  }

  const orderAssessMatch = pathname.match(/^\/api\/orders\/([^/]+)\/assess$/);
  if (method === 'POST' && orderAssessMatch) {
    const assessment = store.query((db) => assessOrder(db, decodeURIComponent(orderAssessMatch[1]), body.at ?? null));
    return sendJson(res, 200, assessment);
  }

  const orderCloseMatch = pathname.match(/^\/api\/orders\/([^/]+)\/close$/);
  if (method === 'POST' && orderCloseMatch) {
    const result = store.transact((db) => closeOrder(db, { ...body, orderId: decodeURIComponent(orderCloseMatch[1]) }));
    return sendJson(res, 200, result);
  }

  const orderMatch = pathname.match(/^\/api\/orders\/([^/]+)$/);
  if (method === 'GET' && orderMatch) {
    const id = decodeURIComponent(orderMatch[1]);
    const result = store.query((db) => ({
      order: mustFind(db.orders, id, '结单'),
      assessment: assessOrder(db, id, url.searchParams.get('at') === null ? null : Number(url.searchParams.get('at'))),
    }));
    return sendJson(res, 200, result);
  }

  if (method === 'POST' && pathname === '/api/complaints') {
    const complaint = store.transact((db) => createComplaint(db, body));
    return sendJson(res, 201, complaintView(store.db, complaint.id));
  }

  if (method === 'POST' && pathname === '/api/dispatch/evaluate') {
    return sendJson(res, 200, store.query((db) => evaluateMakeup(db, body)));
  }

  if (method === 'POST' && (pathname === '/api/dispatch/makeup' || pathname === '/api/trips/makeup')) {
    const result = store.transact((db) => dispatchMakeup(db, body));
    return sendJson(res, 201, result);
  }

  if (method === 'POST' && pathname === '/api/trips/fixed-loop') {
    const trip = store.transact((db) => createTrip(db, { ...body, mode: 'fixed_loop' }));
    return sendJson(res, 201, trip);
  }

  if (method === 'POST' && pathname === '/api/trips/on-demand') {
    const trip = store.transact((db) => createTrip(db, { ...body, mode: 'on_demand' }));
    return sendJson(res, 201, trip);
  }

  const nextMatch = pathname.match(/^\/api\/trips\/([^/]+)\/next-link$/);
  if (method === 'POST' && nextMatch) {
    const plan = store.query((db) => planNextLink(db, decodeURIComponent(nextMatch[1]), body));
    return sendJson(res, 200, plan);
  }

  const returnMatch = pathname.match(/^\/api\/trips\/([^/]+)\/return$/);
  if (method === 'POST' && returnMatch) {
    const trip = store.transact((db) => returnTrip(db, { ...body, tripId: decodeURIComponent(returnMatch[1]) }));
    return sendJson(res, 200, trip);
  }

  const sweepMatch = pathname.match(/^\/api\/trips\/([^/]+)\/sweeps$/);
  if (method === 'POST' && sweepMatch) {
    const result = store.transact((db) => addSweepReport(db, { ...body, tripId: decodeURIComponent(sweepMatch[1]) }));
    return sendJson(res, result.duplicate ? 200 : 201, result);
  }

  const tripMatch = pathname.match(/^\/api\/trips\/([^/]+)$/);
  if (method === 'GET' && tripMatch) {
    return sendJson(res, 200, store.query((db) => mustFind(db.trips, decodeURIComponent(tripMatch[1]), '趟次')));
  }

  const complaintCompleteMatch = pathname.match(/^\/api\/complaints\/([^/]+)\/complete$/);
  if (method === 'POST' && complaintCompleteMatch) {
    const view = store.transact((db) => completeComplaint(db, { ...body, complaintId: decodeURIComponent(complaintCompleteMatch[1]) }));
    return sendJson(res, 200, view);
  }

  const complaintMatch = pathname.match(/^\/api\/complaints\/([^/]+)$/);
  if (method === 'GET' && complaintMatch) {
    const id = decodeURIComponent(complaintMatch[1]);
    const asOfRaw = url.searchParams.get('asOf');
    const asOf = asOfRaw === null ? null : Number(asOfRaw);
    const view = store.query((db) => {
      const c = findComplaintByNo(db, id);
      if (!c) throw new ApiError(404, 'COMPLAINT_NOT_FOUND', `投诉号 ${id} 不存在`);
      return complaintView(db, c, Number.isInteger(asOf) ? asOf : null);
    });
    return sendJson(res, 200, view);
  }

  const ownerMatch = pathname.match(/^\/api\/owner\/complaints\/([^/]+)$/);
  if (method === 'GET' && ownerMatch) {
    const no = decodeURIComponent(ownerMatch[1]);
    const asOfRaw = url.searchParams.get('asOf');
    const asOf = asOfRaw === null ? null : Number(asOfRaw);
    const view = store.query((db) => {
      const c = findComplaintByNo(db, no);
      if (!c) throw new ApiError(404, 'COMPLAINT_NOT_FOUND', `投诉号 ${no} 不存在`);
      return complaintView(db, c, Number.isInteger(asOf) ? asOf : null);
    });
    return sendJson(res, 200, view);
  }

  if (method === 'GET' && pathname === '/api/complaints') {
    return sendJson(res, 200, store.db.complaints.map((c) => ({ id: c.id, complaintNo: c.complaintNo, merchantId: c.merchantId, status: c.status })));
  }

  if (method === 'POST' && pathname === '/api/route/preview') {
    const route = store.query((db) => shortestPath(db, body.startNodeId, body.targetNodeId, body));
    return sendJson(res, 200, route ?? { reachable: false });
  }

  if (method === 'GET' && pathname === '/api/state') {
    return sendJson(res, 200, store.db);
  }

  return null;
}

function serveStatic(req, res, pathname) {
  const requested = pathname === '/' ? '/index.html' : pathname;
  const file = path.normalize(path.join(publicDir, requested));
  if (!file.startsWith(publicDir)) {
    res.writeHead(403);
    return res.end('forbidden');
  }
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
    return res.end('Not found');
  }
  const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
  res.writeHead(200, { 'content-type': types[path.extname(file)] ?? 'application/octet-stream' });
  fs.createReadStream(file).pipe(res);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  try {
    const apiResult = url.pathname.startsWith('/api/') ? await handleApi(req, res, url) : false;
    if (apiResult === null && !res.writableEnded) sendJson(res, 404, { error: 'NOT_FOUND', path: url.pathname });
    if (!url.pathname.startsWith('/api/')) serveStatic(req, res, url.pathname);
  } catch (err) {
    const status = err instanceof ApiError ? err.status : 500;
    sendJson(res, status, {
      error: err.code ?? 'INTERNAL',
      message: err.message,
      details: err.details,
      ...(status === 500 ? { stack: err.stack } : {}),
    });
  }
});

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const port = Number(process.env.PORT || 3000);
  server.listen(port, '127.0.0.1', () => {
    console.log(`街道合作社后台: http://127.0.0.1:${port}`);
    console.log(`数据文件: ${dataFile}`);
  });
}

export { server, store, dataFile };
