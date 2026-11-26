import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { JsonStore } from './store.mjs';
import {
  HttpError,
  orderView,
  reconcile,
  listMachines,
  restock,
  createOrder,
  insertCoin,
  vendOrder,
  reportGoodsDelivery,
  dispenseNext,
  reportSensor,
  reportArrival,
  reportJam,
  repairChannel,
  setChannelFault,
  recoverCoin,
  recordPickupDoor,
  confirmPickup,
  prepareCup,
  sealCup,
  sendCupToPickup,
  cancelOrder,
  claimEntitlement,
} from './core.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const dataFile = process.env.DATA_FILE || path.join(__dirname, '..', 'data', 'vending-state.json');
const store = new JsonStore(dataFile);

function stableStringify(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`;
  const keys = Object.keys(value).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${stableStringify(value[k])}`).join(',')}}`;
}

function sendJson(res, status, body) {
  const payload = JSON.stringify(body, null, 2);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  });
  res.end(payload);
}

async function readJson(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 2_000_000) throw new HttpError(413, 'BODY_TOO_LARGE', 'Request body is too large.');
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString('utf8');
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    throw new HttpError(400, 'INVALID_JSON', 'Request body must be valid JSON.');
  }
}

function requestIdOf(req, body) {
  return body.requestId || body.eventId || req.headers['idempotency-key'];
}

async function idempotentRoute(req, res, machineId, url, params, body, fn) {
  const requestId = requestIdOf(req, body);
  const fingerprint = stableStringify({
    method: req.method,
    path: url.pathname,
    params,
    // requestId/eventId is the dedupe key, not part of semantic fingerprint.
    body: Object.fromEntries(Object.entries(body).filter(([k]) => k !== 'requestId' && k !== 'eventId')),
  });
  const result = await store.idempotent(machineId, requestId, fingerprint, (machine) => fn(machine, body));
  sendJson(res, result.status || 200, result.body);
}

function handleError(res, error) {
  if (error instanceof HttpError) {
    sendJson(res, error.status, { ok: false, error: error.code, message: error.message, details: error.details });
    return;
  }
  console.error(error);
  sendJson(res, 500, { ok: false, error: 'INTERNAL_ERROR', message: error.message });
}

function match(pathname, pattern) {
  const p = pathname.split('/').filter(Boolean);
  const pat = pattern.split('/').filter(Boolean);
  if (p.length !== pat.length) return null;
  const params = {};
  for (let i = 0; i < pat.length; i += 1) {
    if (pat[i].startsWith(':')) params[pat[i].slice(1)] = decodeURIComponent(p[i]);
    else if (pat[i] !== p[i]) return null;
  }
  return params;
}

function createPosHandler(machineId) {
  return async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const pathname = url.pathname.replace(/\/+$/, '') || '/';
    try {
      if (req.method === 'GET' && pathname === '/health') {
        sendJson(res, 200, { ok: true, endpoint: 'sales', machineId, dataFile });
        return;
      }

      let params = match(pathname, '/orders/:orderId');
      if (req.method === 'GET' && params) {
        const m = store.state.machines[machineId];
        const order = m.orders[params.orderId];
        if (!order) throw new HttpError(404, 'ORDER_NOT_FOUND_ON_THIS_TERMINAL',
          `Order ${params.orderId} is not on sales terminal ${machineId}. The adjacent machine cannot pay its debt.`);
        sendJson(res, 200, { ok: true, terminal: machineId, order: orderView(m, order.id) });
        return;
      }

      const routes = [
        ['POST', '/orders', (p, body) => createOrder(p.m, body)],
        ['POST', '/orders/:orderId/coins', (p, body) => insertCoin(p.m, p.orderId, body)],
        ['POST', '/orders/:orderId/vend', (p, body) => vendOrder(p.m, p.orderId, body)],
        ['POST', '/orders/:orderId/cancel', (p, body) => cancelOrder(p.m, p.orderId, body)],
        ['POST', '/orders/:orderId/dispense-next', (p, body) => dispenseNext(p.m, p.orderId, body)],
        ['POST', '/orders/:orderId/cup/prepare', (p, body) => prepareCup(p.m, p.orderId, body)],
        ['POST', '/orders/:orderId/claims/:claimId/claim', (p, body) => claimEntitlement(p.m, p.orderId, p.claimId, body)],
        ['POST', '/orders/:orderId/claims/:claimId/dispense-next', (p, body) =>
          dispenseNext(p.m, p.orderId, { ...body, claimId: p.claimId })],
        ['POST', '/claims/:claimId/dispense-next', (p, body) => {
          const claim = p.m.claims[p.claimId];
          if (!claim) throw new HttpError(404, 'CLAIM_NOT_FOUND_ON_THIS_TERMINAL', `Claim ${p.claimId} is not on terminal ${machineId}.`);
          return dispenseNext(p.m, claim.orderId, { ...body, claimId: claim.id });
        }],
      ];

      for (const [method, pattern, handler] of routes) {
        const p = match(pathname, pattern);
        if (p && req.method === method) {
          const body = await readJson(req);
          await idempotentRoute(req, res, machineId, url, p, body, (m) => handler({ ...p, m }, body));
          return;
        }
      }

      sendJson(res, 404, { ok: false, error: 'ROUTE_NOT_FOUND', method: req.method, path: pathname });
    } catch (error) {
      handleError(res, error);
    }
  };
}

async function maintenanceHandler(req, res) {
  const url = new URL(req.url, 'http://localhost');
  const pathname = url.pathname.replace(/\/+$/, '') || '/';
  try {
    if (req.method === 'GET' && (pathname === '/health' || pathname === '/')) {
      sendJson(res, 200, { ok: true, endpoint: 'maintenance', machines: listMachines(store.state), dataFile });
      return;
    }
    if (req.method === 'GET' && pathname === '/machines') {
      sendJson(res, 200, { ok: true, machines: listMachines(store.state) });
      return;
    }

    let p = match(pathname, '/machines/:machineId/reconciliation');
    if (req.method === 'GET' && p) {
      const m = store.state.machines[p.machineId];
      if (!m) throw new HttpError(404, 'MACHINE_NOT_FOUND', `Unknown machine ${p.machineId}`);
      sendJson(res, 200, { ok: true, reconciliation: reconcile(m) });
      return;
    }

    p = match(pathname, '/machines/:machineId/state');
    if (req.method === 'GET' && p) {
      const m = store.state.machines[p.machineId];
      if (!m) throw new HttpError(404, 'MACHINE_NOT_FOUND', `Unknown machine ${p.machineId}`);
      sendJson(res, 200, { ok: true, machine: m });
      return;
    }

    const routes = [
      ['POST', '/machines/:machineId/restock', (x, body) => restock(x.m, body)],
      ['POST', '/machines/:machineId/orders/:orderId/coins', (x, body) => insertCoin(x.m, x.orderId, body)],
      ['POST', '/machines/:machineId/audit', (x, body) => ({ status: 200, body: { ok: true, reconciliation: reconcile(x.m, body) } })],
      ['POST', '/machines/:machineId/coins/:coinId/recover', (x, body) => recoverCoin(x.m, x.coinId, body)],
      ['POST', '/machines/:machineId/pickup/confirm', (x, body) => confirmPickup(x.m, body)],
      ['POST', '/machines/:machineId/pickup-count', (x, body) => confirmPickup(x.m, body)],
      ['POST', '/machines/:machineId/cups/:cupId/seal', (x, body) => sealCup(x.m, x.cupId, body)],
      ['POST', '/machines/:machineId/cups/:cupId/send-to-pickup', (x) => sendCupToPickup(x.m, x.cupId)],
      ['POST', '/machines/:machineId/events/goods-delivery', (x, body) => reportGoodsDelivery(x.m, body)],
      ['POST', '/machines/:machineId/events/channel-sensor', (x, body) => reportSensor(x.m, body)],
      ['POST', '/machines/:machineId/events/coin-arrived', (x, body) => reportArrival(x.m, body)],
      ['POST', '/machines/:machineId/events/coin-jam', (x, body) => reportJam(x.m, body)],
      ['POST', '/machines/:machineId/channels/:channelId/fault', (x, body) => setChannelFault(x.m, { ...body, channelId: x.channelId })],
      ['POST', '/machines/:machineId/events/channel-repaired', (x, body) => repairChannel(x.m, body)],
      ['POST', '/machines/:machineId/events/pickup-door', (x, body) => recordPickupDoor(x.m, body)],
    ];

    for (const [method, pattern, handler] of routes) {
      const params = match(pathname, pattern);
      if (params && req.method === method) {
        if (!store.state.machines[params.machineId]) {
          throw new HttpError(404, 'MACHINE_NOT_FOUND', `Unknown machine ${params.machineId}`);
        }
        const body = await readJson(req);
        await idempotentRoute(req, res, params.machineId, url, params, body,
          (m) => handler({ ...params, m }, body));
        return;
      }
    }

    sendJson(res, 404, { ok: false, error: 'ROUTE_NOT_FOUND', method: req.method, path: pathname });
  } catch (error) {
    handleError(res, error);
  }
}

const posAPort = Number(process.env.POS_A_PORT || 4101);
const posBPort = Number(process.env.POS_B_PORT || 4102);
const maintenancePort = Number(process.env.MAINT_PORT || 4200);
const host = process.env.HOST || '127.0.0.1';

http.createServer(createPosHandler('A')).listen(posAPort, host, () => {
  console.log(`[sales-A] http://${host}:${posAPort} -> machine A`);
});
http.createServer(createPosHandler('B')).listen(posBPort, host, () => {
  console.log(`[sales-B] http://${host}:${posBPort} -> machine B`);
});
http.createServer(maintenanceHandler).listen(maintenancePort, host, () => {
  console.log(`[maintenance] http://${host}:${maintenancePort} (A/B)`);
  console.log(`[store] ${dataFile}`);
});

export { store };
