import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { RepairService, DomainError } from './service.js';
import { JsonFileStore } from './store.js';
import { seedScenario } from './scenarios.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(__dirname, '..', 'public');

export function createServer(options = {}) {
  const service = options.service;
  if (!service) throw new Error('createServer 需要注入 RepairService');

  return http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, 'http://localhost');
      const pathname = url.pathname;

      if (pathname.startsWith('/api/')) {
        return await handleApi(req, res, url, service);
      }
      return serveStatic(req, res, pathname);
    } catch (error) {
      return sendError(res, error);
    }
  });
}

async function handleApi(req, res, url, service) {
  const { pathname } = url;
  const method = req.method;
  const body = ['POST', 'PUT', 'PATCH'].includes(method)
    ? await readJsonBody(req)
    : {};

  // Health/meta
  if (method === 'GET' && pathname === '/api/health') {
    return sendJson(res, 200, {
      ok: true,
      service: 'repair-joint-service',
      time: service.clock(),
      ticketCount: service.state.tickets.length,
      ticketLimit: 6,
    });
  }

  if (method === 'GET' && pathname === '/api/state') {
    return sendJson(res, 200, service.state);
  }

  if (method === 'GET' && pathname === '/api/topology') {
    return sendJson(res, 200, service.state.topology);
  }

  if (method === 'POST' && pathname === '/api/topology') {
    return sendJson(res, 200, service.setTopology(body));
  }

  // Scenario helper
  if (method === 'POST' && pathname === '/api/scenarios/seed') {
    const summary = seedScenario(
      service,
      body.variant ?? 'baseline',
      body.options ?? {},
    );
    return sendJson(res, 200, { ok: true, summary, state: service.state });
  }

  // Workbench
  if (method === 'GET' && pathname === '/api/next-steps') {
    const role = url.searchParams.get('role') ?? 'resident';
    const actorId = url.searchParams.get('actorId');
    return sendJson(res, 200, service.getWorkbench(role, actorId));
  }

  if (method === 'GET' && pathname === '/api/policy/recommendation') {
    return sendJson(res, 200, service.getButlerRecommendation());
  }

  // Tickets
  if (method === 'GET' && pathname === '/api/tickets') {
    return sendJson(res, 200, service.state.tickets);
  }

  if (method === 'POST' && pathname === '/api/tickets') {
    return sendJson(res, 201, service.createTicket(body));
  }

  let match = pathname.match(/^\/api\/tickets\/([^/]+)$/);
  if (method === 'GET' && match) {
    return sendJson(res, 200, service.getTicketOrThrow(decodeURIComponent(match[1])));
  }

  match = pathname.match(/^\/api\/tickets\/([^/]+)\/next-step$/);
  if (method === 'GET' && match) {
    const role = url.searchParams.get('role') ?? 'resident';
    return sendJson(
      res,
      200,
      service.getTicketNextStep(decodeURIComponent(match[1]), role),
    );
  }

  match = pathname.match(/^\/api\/tickets\/([^/]+)\/closure$/);
  if (method === 'GET' && match) {
    return sendJson(
      res,
      200,
      service.evaluateClosure(decodeURIComponent(match[1])),
    );
  }

  match = pathname.match(/^\/api\/tickets\/([^/]+)\/closure-notice$/);
  if (method === 'POST' && match) {
    const notice = service.issueClosureNotice(decodeURIComponent(match[1]), {
      at: body.at,
    });
    return sendJson(res, 201, notice);
  }

  match = pathname.match(/^\/api\/tickets\/([^/]+)\/sign$/);
  if (method === 'POST' && match) {
    return sendJson(
      res,
      201,
      service.sign({ ...body, ticketId: decodeURIComponent(match[1]) }),
    );
  }

  match = pathname.match(/^\/api\/tickets\/([^/]+)\/recheck-requests$/);
  if (method === 'POST' && match) {
    return sendJson(
      res,
      201,
      service.requestRecheck({
        ...body,
        ticketId: decodeURIComponent(match[1]),
      }),
    );
  }

  match = pathname.match(/^\/api\/tickets\/([^/]+)\/compatibility$/);
  if (method === 'GET' && match) {
    return sendJson(
      res,
      200,
      service.getCompatibility(decodeURIComponent(match[1])),
    );
  }

  // Inspections/evidence
  if (method === 'GET' && pathname === '/api/inspections') {
    return sendJson(res, 200, service.state.inspections);
  }
  if (method === 'POST' && pathname === '/api/inspections') {
    return sendJson(res, 201, service.recordInspection(body));
  }

  match = pathname.match(/^\/api\/inspections\/([^/]+)\/retract$/);
  if (method === 'POST' && match) {
    return sendJson(
      res,
      200,
      service.retractInspection(decodeURIComponent(match[1]), body.reason, body.at),
    );
  }

  if (method === 'POST' && pathname === '/api/evidence/borrow') {
    return sendJson(res, 201, service.borrowEvidence(body));
  }

  if (method === 'GET' && pathname === '/api/borrows') {
    return sendJson(res, 200, service.state.borrows);
  }

  // Cases
  if (method === 'GET' && pathname === '/api/cases') {
    return sendJson(res, 200, service.state.cases);
  }
  if (method === 'POST' && pathname === '/api/cases') {
    return sendJson(res, 201, service.createCase(body));
  }

  match = pathname.match(/^\/api\/cases\/([^/]+)\/plan$/);
  if (method === 'POST' && match) {
    return sendJson(
      res,
      200,
      service.planCase({ ...body, caseId: decodeURIComponent(match[1]) }),
    );
  }

  match = pathname.match(/^\/api\/cases\/([^/]+)\/dispatch$/);
  if (method === 'POST' && match) {
    return sendJson(
      res,
      200,
      service.dispatchCase({ ...body, caseId: decodeURIComponent(match[1]) }),
    );
  }

  match = pathname.match(/^\/api\/cases\/([^/]+)\/revisit$/);
  if (method === 'POST' && match) {
    return sendJson(
      res,
      201,
      service.recordJointRevisit({
        ...body,
        caseId: decodeURIComponent(match[1]),
      }),
    );
  }

  match = pathname.match(/^\/api\/cases\/([^/]+)\/split$/);
  if (method === 'POST' && match) {
    return sendJson(
      res,
      200,
      service.splitCase({ ...body, caseId: decodeURIComponent(match[1]) }),
    );
  }

  // Repairs/revisits/notifications/signoffs
  if (method === 'GET' && pathname === '/api/repairs') {
    return sendJson(res, 200, service.state.repairs);
  }
  if (method === 'POST' && pathname === '/api/repairs') {
    return sendJson(res, 201, service.registerRepair(body));
  }

  if (method === 'GET' && pathname === '/api/revisits') {
    return sendJson(res, 200, service.state.revisits);
  }
  if (method === 'POST' && pathname === '/api/revisits') {
    return sendJson(res, 201, service.recordRevisit(body));
  }

  if (method === 'POST' && pathname === '/api/closures/run') {
    return sendJson(res, 200, service.dispatchClosureNotices(body));
  }

  if (method === 'GET' && pathname === '/api/notifications') {
    const ticketId = url.searchParams.get('ticketId');
    const notifications = ticketId
      ? service.state.notifications.filter((n) => n.ticketId === ticketId)
      : service.state.notifications;
    return sendJson(res, 200, notifications);
  }

  if (method === 'GET' && pathname === '/api/signoffs') {
    return sendJson(res, 200, service.state.signoffs);
  }
  if (method === 'POST' && pathname === '/api/signoffs') {
    return sendJson(res, 201, service.sign(body));
  }

  if (method === 'GET' && pathname === '/api/recheck-requests') {
    return sendJson(res, 200, service.state.recheckRequests);
  }

  return sendJson(res, 404, {
    ok: false,
    error: { code: 'NOT_FOUND', message: `无此 API：${method} ${pathname}` },
  });
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let raw = '';
    req.on('data', (chunk) => {
      raw += chunk;
      if (raw.length > 2_000_000) {
        reject(new DomainError('BODY_TOO_LARGE', '请求体过大'));
        req.destroy();
      }
    });
    req.on('end', () => {
      if (!raw.trim()) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch (error) {
        reject(new DomainError('INVALID_JSON', '请求体不是合法 JSON'));
      }
    });
    req.on('error', reject);
  });
}

function sendJson(res, status, payload) {
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
  });
  res.end(JSON.stringify(payload, null, 2));
}

function sendError(res, error) {
  if (error instanceof DomainError) {
    const status = statusForCode(error.code);
    return sendJson(res, status, {
      ok: false,
      error: {
        code: error.code,
        message: error.message,
        details: error.details,
      },
    });
  }
  console.error(error);
  return sendJson(res, 500, {
    ok: false,
    error: { code: 'INTERNAL', message: error.message ?? '服务异常' },
  });
}

function statusForCode(code) {
  if (code === 'NOT_FOUND' || code.endsWith('_NOT_FOUND')) return 404;
  if (
    code === 'VALIDATION' ||
    code === 'INVALID_JSON' ||
    code === 'PLAN_REQUIRED' ||
    code === 'JOINT_RESULT_MISSING' ||
    code === 'TOO_MANY_SOURCES'
  ) {
    return 400;
  }
  if (code === 'TICKET_LIMIT') return 422;
  if (code === 'CLOSURE_NOT_ALLOWED') return 409;
  if (code === 'PRIVATE_EVIDENCE_NOT_TRANSFERABLE') return 403;
  if (code === 'PERMISSION_DENIED') return 403;
  if (code === 'IDEMPOTENCY_CONFLICT' || code === 'CASE_ALREADY_DISPATCHED') {
    return 409;
  }
  return 400;
}

function serveStatic(req, res, pathname) {
  let relativePath = pathname === '/' ? 'index.html' : pathname.slice(1);
  const filePath = path.normalize(path.join(PUBLIC_DIR, relativePath));
  if (!filePath.startsWith(PUBLIC_DIR)) {
    res.writeHead(403);
    return res.end('Forbidden');
  }
  if (!fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
    res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    return res.end('Not found');
  }
  const types = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.svg': 'image/svg+xml',
  };
  res.writeHead(200, {
    'Content-Type': types[path.extname(filePath)] ?? 'application/octet-stream',
  });
  fs.createReadStream(filePath).pipe(res);
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const dataFile =
    process.env.DATA_FILE ??
    path.join(__dirname, '..', 'data', 'service-state.json');
  const store = new JsonFileStore(dataFile);
  const service = new RepairService({ store });
  if (process.env.SEED_ON_START !== '0' && service.state.tickets.length === 0) {
    seedScenario(service, 'baseline', { includeBing: true });
  }
  const port = Number(process.env.PORT ?? 3000);
  createServer({ service }).listen(port, '0.0.0.0', () => {
    console.log(`报修联办服务已启动：http://127.0.0.1:${port}`);
    console.log(`数据文件：${dataFile}`);
    console.log('施工、加压和回访均为模拟；按 Ctrl+C 停止。');
  });
}
