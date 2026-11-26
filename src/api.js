// HTTP API. Framework-free: a small router over node:http with JSON bodies.
// Every route maps onto the service modules; accounting endpoints are derived.

import http from 'node:http';
import { ApiError, badRequest } from './util.js';
import {
  createHousehold, getHousehold, setApplication, getApplication, normalizeApplication,
} from './domain.js';
import {
  signContract, getContract, reportNights, confirmBooking, correctReport,
  cancelNight, closeContract, reopenContract, listStatements, getStatement,
  contractSettlement, reviseContract, signRevisedContract, getDispute,
} from './contracts.js';
import { planContracts } from './schedule.js';
import { bookingView } from './accounting.js';

function send(res, status, body) {
  const data = body === undefined ? '' : JSON.stringify(body);
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(data);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (c) => { data += c; if (data.length > 5_000_000) reject(badRequest('body too large')); });
    req.on('end', () => {
      if (!data) return resolve({});
      try { resolve(JSON.parse(data)); } catch { reject(badRequest('invalid JSON body')); }
    });
    req.on('error', reject);
  });
}

const routes = [];
function route(method, pattern, handler) {
  const keys = [];
  const regex = new RegExp(`^${pattern.replace(/:(\w+)/g, (_, k) => { keys.push(k); return '([^/]+)'; })}$`);
  routes.push({ method, regex, keys, handler });
}

// ---- households & applications ------------------------------------------
route('POST', '/households', async (ctx) => {
  const hh = createHousehold(ctx.store, ctx.body);
  return { status: 201, body: hh };
});
route('GET', '/households/:id', async (ctx) => ({ body: getHousehold(ctx.store, ctx.params.id) }));

route('PUT', '/households/:id/application', async (ctx) => {
  const app = setApplication(ctx.store, ctx.params.id, ctx.body, {
    expectedVersion: ctx.body?.expectedVersion,
  });
  return { body: app };
});
route('GET', '/households/:id/application', async (ctx) => ({ body: getApplication(ctx.store, ctx.params.id) }));
route('POST', '/households/:id/application/validate', async (ctx) => {
  return { body: normalizeApplication(ctx.store, ctx.params.id, ctx.body) };
});

// ---- planning ------------------------------------------------------------
route('POST', '/plans', async (ctx) => {
  const { type, households } = ctx.body ?? {};
  return { body: planContracts(ctx.store, { type: type ?? 'pair', households }) };
});

// ---- contracts -----------------------------------------------------------
route('POST', '/contracts', async (ctx) => {
  const body = ctx.body ?? {};
  const c = body.declines ? signRevisedContract(ctx.store, body) : signContract(ctx.store, body);
  return { status: 201, body: c };
});
route('GET', '/contracts/:id', async (ctx) => ({ body: getContract(ctx.store, ctx.params.id) }));
route('GET', '/contracts/:id/settlement', async (ctx) => ({ body: contractSettlement(ctx.store, ctx.params.id) }));

route('POST', '/contracts/:id/edges/:edge/report', async (ctx) => {
  const { side, nights, confirm } = ctx.body ?? {};
  const view = reportNights(ctx.store, ctx.params.id, Number(ctx.params.edge), side, nights, { confirm: Boolean(confirm) });
  return { body: view };
});
route('POST', '/contracts/:id/edges/:edge/confirm', async (ctx) => {
  const { side } = ctx.body ?? {};
  return { body: confirmBooking(ctx.store, ctx.params.id, Number(ctx.params.edge), side) };
});
route('POST', '/contracts/:id/edges/:edge/correct', async (ctx) => {
  const { side, nights } = ctx.body ?? {};
  return { body: correctReport(ctx.store, ctx.params.id, Number(ctx.params.edge), side, nights) };
});
route('GET', '/contracts/:id/edges/:edge/dispute', async (ctx) => {
  return { body: getDispute(ctx.store, ctx.params.id, Number(ctx.params.edge)) };
});
route('POST', '/contracts/:id/edges/:edge/cancel', async (ctx) => {
  const { night } = ctx.body ?? {};
  return { body: cancelNight(ctx.store, ctx.params.id, Number(ctx.params.edge), night) };
});
route('POST', '/contracts/:id/close', async (ctx) => ({ body: closeContract(ctx.store, ctx.params.id) }));
route('POST', '/contracts/:id/reopen', async (ctx) => ({ body: reopenContract(ctx.store, ctx.params.id) }));
route('GET', '/contracts/:id/statements', async (ctx) => ({ body: listStatements(ctx.store, ctx.params.id) }));
route('POST', '/contracts/:id/revise', async (ctx) => {
  const { declines, splits } = ctx.body ?? {};
  return { body: reviseContract(ctx.store, ctx.params.id, { declines, splits }) };
});

// ---- statements & bookings ----------------------------------------------
route('GET', '/statements/:id', async (ctx) => ({ body: getStatement(ctx.store, ctx.params.id) }));
route('GET', '/bookings/:id', async (ctx) => {
  const b = ctx.store.state.bookings[ctx.params.id];
  if (!b) throw new ApiError(404, 'NOT_FOUND', `booking ${ctx.params.id} not found`);
  return { body: bookingView(b) };
});

route('GET', '/health', async () => ({ body: { ok: true } }));

export function createServer(store) {
  return http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, 'http://local');
      const path = url.pathname.replace(/\/+$/, '') || '/';
      for (const r of routes) {
        if (r.method !== req.method) continue;
        const m = r.regex.exec(path);
        if (!m) continue;
        const params = {};
        r.keys.forEach((k, i) => { params[k] = decodeURIComponent(m[i + 1]); });
        const body = req.method === 'GET' ? {} : await readBody(req);
        const ctx = { store, params, body, query: url.searchParams, req };
        const out = await r.handler(ctx);
        return send(res, out.status ?? 200, out.body);
      }
      send(res, 404, { error: { code: 'NOT_FOUND', message: `no route for ${req.method} ${path}` } });
    } catch (e) {
      if (e instanceof ApiError) {
        return send(res, e.status, { error: { code: e.code, message: e.message, details: e.details } });
      }
      // eslint-disable-next-line no-console
      console.error(e);
      send(res, 500, { error: { code: 'INTERNAL', message: e.message } });
    }
  });
}

export { routes };
