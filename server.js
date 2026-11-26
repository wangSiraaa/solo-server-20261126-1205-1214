import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Studio } from './src/studio.js';
import { HttpError } from './src/model.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const publicDir = path.join(__dirname, 'public');
const studio = new Studio(process.env.STUDIO_DATA || process.env.DATA_FILE);

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon'
};

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body)
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (chunk) => {
      data += chunk;
      if (data.length > 12_000_000) {
        reject(new HttpError(413, 'body_too_large', '请求体过大'));
        req.destroy();
      }
    });
    req.on('end', () => {
      if (!data) return resolve({});
      try { resolve(JSON.parse(data)); }
      catch (err) { reject(new HttpError(400, 'invalid_json', `JSON 无法解析: ${err.message}`)); }
    });
    req.on('error', reject);
  });
}

function serveStatic(req, res, pathname) {
  let rel = pathname === '/' ? 'index.html' : pathname.replace(/^\/+/, '');
  rel = rel.split('?')[0];
  const file = path.normalize(path.join(publicDir, rel));
  if (file !== publicDir && !file.startsWith(publicDir + path.sep)) return sendJson(res, 403, { ok: false, message: 'forbidden' });
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    // Friendly SPA/UI fallback for non-API routes.
    const index = path.join(publicDir, 'index.html');
    if (fs.existsSync(index)) {
      res.writeHead(200, { 'content-type': MIME['.html'] });
      return fs.createReadStream(index).pipe(res);
    }
    return sendJson(res, 404, { ok: false, message: 'not found' });
  }
  res.writeHead(200, { 'content-type': MIME[path.extname(file)] || 'application/octet-stream' });
  fs.createReadStream(file).pipe(res);
}

function ok(data, extra = {}) {
  return { ok: true, success: true, ...extra, ...(data && typeof data === 'object' ? data : { data }) };
}

function idFrom(parts, name) {
  // Tiny route parameter convention implemented by pattern matching below.
  return null;
}

async function handleApi(req, res, url) {
  let { pathname } = url;
  const method = req.method.toUpperCase();
  // Common domain aliases: designs/projects/bands are the same aggregate as orders.
  const orderAlias = pathname.match(/^\/api\/(?:designs?|patterns?|projects?|bands?|workshops?|embroidery-orders?|xiudan)\/([^/]+)(?:\/(.*))?$/);
  if (orderAlias) pathname = `/api/orders/${orderAlias[1]}${orderAlias[2] ? `/${orderAlias[2]}` : ''}`;
  if (/^\/api\/(?:designs?|patterns?|projects?|bands?|workshops?|xiudan)\/?$/.test(pathname)) pathname = '/api/orders';
  const ticketAlias = pathname.match(/^\/api\/(?:sheets|needle-sheets|workorders|work_orders|jobs|tasks|hoops|gongdan)\/([^/]+)(?:\/(.*))?$/);
  if (ticketAlias) pathname = `/api/tickets/${ticketAlias[1]}${ticketAlias[2] ? `/${ticketAlias[2]}` : ''}`;
  const parts = pathname.split('/').filter(Boolean); // api, ...
  const parsedBody = ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method) ? await readBody(req) : {};
  let body = parsedBody && typeof parsedBody === 'object' ? parsedBody : {};
  if (Array.isArray(body)) {
    body = /segment|sample|section|replace/i.test(pathname) ? { segments: body } : { items: body };
  }
  const versionHeader = req.headers['if-match'] || req.headers['x-expected-version'] ||
    req.headers['x-report-version'] || req.headers['x-version'];
  if (body.expectedVersion === undefined && versionHeader) {
    const n = Number(String(versionHeader).replace(/W\//i, '').replace(/"/g, '').trim());
    if (Number.isFinite(n)) body.expectedVersion = n;
  }
  if (body.terminalId === undefined && req.headers['x-terminal-id']) body.terminalId = req.headers['x-terminal-id'];

  if (method === 'OPTIONS') {
    res.writeHead(204, corsHeaders());
    return res.end();
  }

  // Health and introspection.
  if (pathname === '/health' || pathname === '/api/health') {
    return sendJson(res, 200, ok({ status: 'up', service: 'embroidery-lan-studio', time: new Date().toISOString() }));
  }
  if (pathname === '/api' || pathname === '/api/endpoints') {
    return sendJson(res, 200, ok({
      service: '局域网编作台 / Embroidery LAN Studio',
      workshopMode: 'parallel-default; relay supported per order',
      endpoints: [
        'POST /api/orders', 'GET /api/orders', 'GET /api/orders/:id',
        'PUT/PATCH/POST /api/orders/:id/design',
        'POST /api/orders/:id/revisions', '/changes', '/change-requests',
        'POST /api/orders/:id/claim', '/tickets',
        'GET /api/tickets/:id', 'POST /api/tickets/:id/report',
        'POST /api/tickets/:id/return', 'POST /api/tickets/:id/reissue',
        'POST /api/scenarios/red-band', 'POST /api/scenarios/three-segments',
        'POST /api/reset', 'GET /api/state'
      ]
    }));
  }
  if ((pathname === '/api/reset' || pathname === '/reset' || pathname === '/api/test/reset') && method === 'POST') {
    const result = studio.reset(body.seed ?? body.scenario ?? false);
    return sendJson(res, 200, ok(result));
  }
  if ((pathname === '/api/state' || pathname === '/api/snapshot') && method === 'GET') {
    return sendJson(res, 200, ok(studio.snapshot()));
  }
  if (pathname === '/api/events' && method === 'GET') {
    return sendJson(res, 200, ok({ events: studio.db.events }));
  }

  // Built-in scenarios.
  if (method === 'POST' && ['/api/scenarios/red-band', '/api/scenarios/red', '/api/acceptance/red-band'].includes(pathname)) {
    return sendJson(res, 200, ok(studio.createRedBandSites(body)));
  }
  if (method === 'POST' && ['/api/scenarios/three-segments', '/api/scenarios/three', '/api/acceptance/three-segments'].includes(pathname)) {
    return sendJson(res, 200, ok(studio.createThreeSegmentOrder(body)));
  }

  // Flat convenience APIs for test clients that address an action rather than a nested resource.
  if (method === 'POST' && ['/api/claims', '/api/claim', '/api/pickups', '/api/issue'].includes(pathname)) {
    const orderId = body.orderId || body.order || body.id;
    if (!orderId) throw new HttpError(400, 'order_id_required', '请提供 orderId');
    const ticket = studio.claim(orderId, body);
    return sendJson(res, 200, ok({ ...ticket, ticket, workOrder: ticket, needleSheet: ticket }));
  }
  if (method === 'POST' && ['/api/reports', '/api/progress'].includes(pathname)) {
    const ticketId = body.ticketId || body.workOrderId || body.needleSheetId || body.id;
    if (!ticketId) throw new HttpError(400, 'ticket_id_required', '请提供 ticketId');
    const ticket = studio.report(ticketId, body);
    return sendJson(res, 200, ok({ ...ticket, ticket, version: ticket.version, reportVersion: ticket.version }));
  }
  if (method === 'POST' && ['/api/returns', '/api/return-requests', '/api/handbacks'].includes(pathname)) {
    const ticketId = body.ticketId || body.workOrderId || body.needleSheetId || body.id;
    if (!ticketId) throw new HttpError(400, 'ticket_id_required', '请提供 ticketId');
    const ticket = studio.returnTicket(ticketId, body);
    return sendJson(res, 200, ok({ ...ticket, ticket, version: ticket.version, reportVersion: ticket.version }));
  }
  if (method === 'POST' && ['/api/replace-segments', '/api/segments/replace', '/api/design/replace'].includes(pathname)) {
    const orderId = body.orderId || body.order;
    if (!orderId) throw new HttpError(400, 'order_id_required', '请提供 orderId');
    return sendJson(res, 200, ok(studio.updateDesign(orderId, body)));
  }

  if (method === 'POST' && ['/api/segments', '/api/samples', '/api/sections'].includes(pathname)) {
    const orderId = body.orderId || body.order;
    if (!orderId) throw new HttpError(400, 'order_id_required', '请提供 orderId');
    if (body.segments !== undefined && body.append !== true) {
      return sendJson(res, 200, ok(studio.updateDesign(orderId, body)));
    }
    return sendJson(res, 200, ok(studio.registerSegment(orderId, body)));
  }

  // Segment-scoped convenience routes for clients that naturally address /api/segments/:id/claim.
  const globalSegment = pathname.match(/^\/api\/(?:segments|samples|sections)\/([^/]+)(?:\/(claim|issue|receive|replace|design))?\/?$/);
  if (globalSegment) {
    const ref = decodeURIComponent(globalSegment[1]);
    const action = globalSegment[2] || '';
    let orderId = body.orderId || body.order || body.order_id;
    if (!orderId) {
      orderId = Object.keys(studio.db.orders).find((oid) => {
        const o = studio.db.orders[oid];
        return o.segmentInputs.some((s, i) => String(s.id) === ref || String(s.segmentId) === ref || String(i) === ref);
      });
    }
    if (!orderId) throw new HttpError(404, 'segment_not_found', `找不到样段 ${ref} 所属绣单；请在 body 中提供 orderId`);
    const order = studio.getOrder(orderId);
    const seg = order.segmentViews.find((s) => String(s.segmentId) === ref || String(s.index) === ref);
    if (!seg) throw new HttpError(404, 'segment_not_found', `绣单 ${orderId} 没有样段 ${ref}`);
    if (action === 'claim' || action === 'issue' || action === 'receive') {
      const ticket = studio.claim(orderId, { ...body, segmentIndex: seg.index });
      return sendJson(res, 200, ok({ ...ticket, ticket, workOrder: ticket, needleSheet: ticket }));
    }
    if (action === 'replace' || action === 'design') {
      const payload = body.segment || body.sample ? body : { segment: body, segmentIndex: seg.index };
      return sendJson(res, 200, ok(studio.updateDesign(orderId, { ...body, ...payload, segmentIndex: seg.index })));
    }
    if (method === 'GET' && !action) {
      return sendJson(res, 200, ok({ orderId, segment: seg, ticket: order.tickets.find((t) => t.id === seg.ticketId) || null }));
    }
  }

  // Orders collection.
  if (pathname === '/api/orders' || pathname === '/api/orders/' || pathname === '/api/dingdan') {
    if (method === 'GET') {
      const orders = studio.listOrders();
      return sendJson(res, 200, ok({ orders, data: orders, items: orders }));
    }
    if (method === 'POST') {
      const order = studio.createOrder(body);
      return sendJson(res, 200, ok({ ...order, order, orderId: order.id }));
    }
  }

  const orderMatch = pathname.match(/^\/api\/(?:orders|dingdan)\/([^/]+)(?:\/(.*))?$/);
  if (orderMatch) {
    const orderId = decodeURIComponent(orderMatch[1]);
    const rest = orderMatch[2] || '';
    const restParts = rest.split('/').filter(Boolean);
    const action = restParts[0] || '';
    const subId = restParts[1] ? decodeURIComponent(restParts[1]) : null;
    const subAction = restParts[2] || '';

    if (!action && method === 'GET') {
      const order = studio.getOrder(orderId);
      return sendJson(res, 200, ok({ ...order, order }));
    }
    if (!action && ['PUT', 'PATCH', 'POST'].includes(method)) {
      return sendJson(res, 200, ok(studio.updateDesign(orderId, body)));
    }
    if (['plan', 'feasibility', 'validate', 'completable', 'can-complete', 'canComplete', 'status', 'completion', 'complete'].includes(action)) {
      const view = studio.getOrder(orderId);
      return sendJson(res, 200, ok({
        orderId, feasible: view.feasible, completable: view.completable,
        canComplete: view.canComplete, complete: view.complete,
        violations: view.violations, plan: view.plan, seams: view.seams
      }));
    }
    if (['versions', 'version', 'history', 'revisions'].includes(action) && method === 'GET') {
      const view = studio.getOrder(orderId);
      return sendJson(res, 200, ok({
        orderId,
        designVersion: view.designVersion,
        currentVersion: view.designVersion,
        revisions: view.revisions || [],
        issuedVersions: view.issuedVersions
      }));
    }
    if (action === 'path' || action === 'full-path' || action === 'needle-path' || action === 'stitches' || action === 'needle-diagram') {
      const view = studio.getOrder(orderId);
      return sendJson(res, 200, ok({
        orderId, fullPath: view.fullPath, completeNeedlePath: view.completeNeedlePath,
        needlePath: view.needlePath, actualPath: view.actualPath,
        actualPathByColor: view.actualPathByColor, issuedVersions: view.issuedVersions,
        segments: view.segmentViews, seams: view.seams
      }));
    }
    if (['design', 'pattern', 'revision', 'revisions', 'revise', 'modify', 'update', 'segments', 'samples', 'replace-segments'].includes(action) &&
        ['POST', 'PUT', 'PATCH'].includes(method)) {
      let payload = body;
      if (['segments', 'samples'].includes(action) && subId && !subAction &&
          !['replace', 'batch', 'all'].includes(subId) && !body.segments && !body.samples) {
        payload = {
          ...body,
          segmentIndex: body.segmentIndex ?? body.index ?? subId,
          segment: body.segment ?? body.sample ?? body
        };
      }
      if (['segments', 'samples'].includes(action) && !subId && method === 'POST' &&
          body.segments === undefined && body.samples === undefined &&
          (body.segment || body.sample || body.threads || body.colors || body.path || body.cells || body.rows !== undefined)) {
        return sendJson(res, 200, ok(studio.registerSegment(orderId, body)));
      }
      // Explicit strict application can be selected with ?apply=strict or /change-requests.
      if (action === 'revisions' && url.searchParams.get('strict') === '1') {
        return sendJson(res, 200, ok({ changeRequest: studio.submitChangeRequest(orderId, payload) }));
      }
      return sendJson(res, 200, ok(studio.updateDesign(orderId, payload)));
    }
    if (['change', 'changes'].includes(action) && method === 'POST') {
      // Designer-style change: persist the proposed head and return a feasibility decision.
      // Clients that need a hard 409 on rejection should call /change-requests.
      return sendJson(res, 200, ok(studio.updateDesign(orderId, body)));
    }
    if (['change-requests', 'applications', 'apply-change'].includes(action) && method === 'POST') {
      const result = studio.submitChangeRequest(orderId, body);
      return sendJson(res, 200, ok({ changeRequest: result.request, ...result }));
    }
    if (action === 'segments' && subId && method === 'GET') {
      const order = studio.getOrder(orderId);
      const segment = order.segmentViews.find((s) => String(s.index) === String(subId) || s.segmentId === subId);
      if (!segment) throw new HttpError(404, 'segment_not_found', `找不到样段 ${subId}`);
      return sendJson(res, 200, ok({ segment, boardSegment: order.board?.segments?.[segment.index] || null }));
    }
    if (['claim', 'receive', 'pickup', 'checkout', 'issue', 'tickets', 'work-orders', 'needle-sheets'].includes(action) && method === 'POST') {
      // Support /orders/:id/segments/:idx/claim too.
      if (restParts[0] === 'segments' && restParts[2] === 'claim') {
        body.segmentIndex ??= Number(restParts[1]);
      }
      const ticket = studio.claim(orderId, body);
      return sendJson(res, 200, ok({ ...ticket, ticket, workOrder: ticket, needleSheet: ticket }));
    }
    if (['tickets', 'work-orders', 'needle-sheets'].includes(action) && method === 'GET') {
      const order = studio.getOrder(orderId);
      return sendJson(res, 200, ok({ tickets: order.tickets, issuedVersions: order.issuedVersions }));
    }
    if (action === 'segments' && subId && ['claim', 'issue', 'receive'].includes(subAction) && method === 'POST') {
      body.segmentIndex ??= Number(subId);
      const ticket = studio.claim(orderId, body);
      return sendJson(res, 200, ok({ ticket }));
    }
  }

  // Tickets collection aliases.
  if (['/api/tickets', '/api/work-orders', '/api/workorders', '/api/needle-sheets', '/api/sheets', '/api/jobs', '/api/tasks'].includes(pathname) && method === 'GET') {
    return sendJson(res, 200, ok({ tickets: Object.values(studio.db.tickets), jobs: Object.values(studio.db.tickets) }));
  }

  const ticketMatch = pathname.match(/^\/api\/(?:tickets|work-orders|needle-sheets|gongdan)\/([^/]+)(?:\/(.*))?$/);
  if (ticketMatch) {
    const ticketId = decodeURIComponent(ticketMatch[1]);
    const action = ticketMatch[2] || '';
    if (!action && method === 'GET') {
      const ticket = studio.getTicket(ticketId);
      return sendJson(res, 200, ok({ ...ticket, ticket, workOrder: ticket, needleSheet: ticket }));
    }
    if (!action && ['POST', 'PUT', 'PATCH'].includes(method)) {
      const ticket = studio.report(ticketId, body);
      return sendJson(res, 200, ok({ ...ticket, ticket, workOrder: ticket, needleSheet: ticket, reportVersion: ticket.version, version: ticket.version }));
    }
    if (['report', 'reports', 'progress', 'stitch', 'stitch-next', 'confirm', 'advance', 'submit'].includes(action) && ['POST', 'PUT', 'PATCH'].includes(method)) {
      if (action === 'stitch' || action === 'confirm') body.action ??= action;
      const ticket = studio.report(ticketId, body);
      return sendJson(res, 200, ok({ ...ticket, ticket, workOrder: ticket, needleSheet: ticket, reportVersion: ticket.version, version: ticket.version }));
    }
    if (['return', 'return-for-revision', 'handback', 'hand-back', 'give-back', 'release', 'cancel-unstitched'].includes(action) && method === 'POST') {
      const ticket = studio.returnTicket(ticketId, body);
      return sendJson(res, 200, ok({ ...ticket, ticket, workOrder: ticket, needleSheet: ticket, reportVersion: ticket.version, version: ticket.version }));
    }
    if (['reissue', 'replace', 'claim-replacement'].includes(action) && method === 'POST') {
      const old = studio.getTicketRecord(ticketId);
      const ticket = studio.claim(old.orderId, { ...body, segmentIndex: old.segmentIndex, reissue: true });
      return sendJson(res, 200, ok({ ticket }));
    }
  }

  // Nested ticket actions under orders.
  const nestedTicket = pathname.match(/^\/api\/orders\/([^/]+)\/tickets\/([^/]+)\/(report|return|reissue)?$/);
  if (nestedTicket && method === 'POST') {
    const [, orderId, ticketId, action] = nestedTicket;
    if (action === 'report' || !action) return sendJson(res, 200, ok({ ticket: studio.report(ticketId, body) }));
    if (action === 'return') return sendJson(res, 200, ok({ ticket: studio.returnTicket(ticketId, body) }));
    if (action === 'reissue') {
      const old = studio.getTicketRecord(ticketId);
      return sendJson(res, 200, ok({ ticket: studio.claim(orderId, { ...body, segmentIndex: old.segmentIndex }) }));
    }
  }

  return sendJson(res, 404, { ok: false, success: false, error: 'not_found', message: `没有这个 API：${method} ${pathname}` });
}

function corsHeaders() {
  return {
    'access-control-allow-origin': '*',
    'access-control-allow-methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS',
    'access-control-allow-headers': 'content-type,authorization,if-match,x-terminal-id,x-request-id'
  };
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  Object.entries(corsHeaders()).forEach(([k, v]) => res.setHeader(k, v));
  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    return res.end();
  }
  try {
    if (url.pathname === '/api' || url.pathname.startsWith('/api/')) return await handleApi(req, res, url);
    if (url.pathname === '/health') {
      return sendJson(res, 200, { ok: true, status: 'up' });
    }
    return serveStatic(req, res, url.pathname);
  } catch (err) {
    const status = err.status || 500;
    if (status >= 500) console.error('[server error]', err);
    return sendJson(res, status, {
      ok: false,
      success: false,
      error: err.code || 'internal_error',
      message: err.message,
      ...(err.details ? { details: err.details } : {}),
      ...(err.requestRecord ? { changeRequest: err.requestRecord } : {})
    });
  }
});

const preferred = Number(process.env.PORT || 3000);
const ports = [...new Set([preferred, 3000, 5000, 8000, 8080, 4173].filter(Boolean))];
let started = false;
for (const p of ports) {
  const s = p === preferred ? server : http.createServer(server.listeners('request')[0]);
  s.on('error', (err) => {
    if (err.code === 'EADDRINUSE' && !started) {
      console.warn(`端口 ${p} 已占用，尝试下一个常用端口...`);
    } else if (!started) {
      console.error(err);
    }
  });
  s.listen(p, '0.0.0.0', () => {
    started = true;
    console.log(`绣样局域网编作台已启动: http://0.0.0.0:${p} (LAN http://<本机IP>:${p})`);
    console.log(`API 说明: http://localhost:${p}/api`);
  });
  if (p === preferred) {
    // Give preferred listener a tick to fail before creating alternatives is awkward;
    // still create fallback servers immediately. Each works independently.
  }
}

function shutdown() {
  studio.save();
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
