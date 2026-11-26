import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { JsonStore } from './src/store.js';
import {
  DomainError,
  SELECTED_CLASS_TYPE,
  addReceipt,
  checkIn,
  createCase,
  createRecord,
  createStudent,
  evaluateStudent,
  findDemoFillablePractice,
  getCase,
  listStamps,
  processMentor,
  recyclePiece,
  runBuiltInScenario,
  updateEvent,
  updateRule,
} from './src/model.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const publicDir = path.join(__dirname, 'public');

export function createServer(options = {}) {
  const dataFile =
    options.dataFile || process.env.DATA_FILE || path.join(__dirname, 'data', 'pottery.json');
  const store = options.store || new JsonStore(dataFile);

  const server = http.createServer(async (req, res) => {
    try {
      if (req.method === 'OPTIONS') {
        res.writeHead(204, {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Headers': 'Content-Type, Idempotency-Key',
          'Access-Control-Allow-Methods': 'GET,POST,PATCH,OPTIONS',
        });
        return res.end();
      }

      const url = new URL(req.url, 'http://localhost');
      const pathname = decodeURIComponent(url.pathname);
      req.urlPath = pathname;
      req.query = Object.fromEntries(url.searchParams.entries());

      if (pathname.startsWith('/api/')) {
        await handleApi(req, res, store);
        return;
      }
      serveStatic(req, res, pathname);
    } catch (error) {
      sendError(res, error);
    }
  });

  server.store = store;
  return server;
}

async function handleApi(req, res, store) {
  const { method, urlPath: p, query } = req;
  const body = ['POST', 'PUT', 'PATCH'].includes(method) ? await readJson(req) : {};

  if (p === '/api/health' && method === 'GET') {
    return sendJson(res, 200, {
      ok: true,
      service: store.db.meta.serviceName,
      selectedClassType: SELECTED_CLASS_TYPE,
      time: new Date().toISOString(),
      portNote: process.env.PORT || '3000',
    });
  }

  if (p === '/api/scenario' && method === 'GET') {
    return sendJson(res, 200, store.mutate((db) => runBuiltInScenario(db)));
  }

  if (p === '/api/admin/reset' && method === 'POST') {
    store.reset();
    return sendJson(res, 200, { ok: true, message: '已恢复初始虚构验题数据', db: store.db });
  }

  if (p === '/api/state' && method === 'GET') {
    return sendJson(res, 200, store.db);
  }

  if (p === '/api/students' && method === 'GET') {
    return sendJson(res, 200, store.db.students);
  }
  if (p === '/api/students' && method === 'POST') {
    const student = store.mutate((db) => createStudent(db, body));
    return sendJson(res, 201, student);
  }

  if (p === '/api/actions' && method === 'GET') {
    return sendJson(res, 200, store.db.actions);
  }

  if (p === '/api/rulebook' && method === 'GET') {
    return sendJson(res, 200, store.db.rulebook);
  }

  let match = p.match(/^\/api\/rules\/links\/([^/]+)$/);
  if (match && method === 'PATCH') {
    const result = store.mutate((db) => updateRule(db, match[1], body));
    return sendJson(res, 200, result);
  }

  if (p === '/api/sessions' && method === 'GET') {
    return sendJson(res, 200, store.db.sessions);
  }
  if (p === '/api/tables' && method === 'GET') {
    return sendJson(res, 200, store.db.tables);
  }
  if (p === '/api/pieces' && method === 'GET') {
    return sendJson(res, 200, store.db.pieces);
  }

  match = p.match(/^\/api\/pieces\/([^/]+)\/recycle$/);
  if (match && method === 'POST') {
    const piece = store.mutate((db) => recyclePiece(db, match[1], body));
    return sendJson(res, 200, piece);
  }

  if (p === '/api/records' && method === 'GET') {
    const studentId = query.studentId;
    return sendJson(
      res,
      200,
      studentId ? store.db.records.filter((r) => r.studentId === studentId) : store.db.records,
    );
  }
  if (p === '/api/records' && method === 'POST') {
    const record = store.mutate((db) => createRecord(db, body));
    return sendJson(res, 201, record);
  }

  match = p.match(/^\/api\/(?:events|records\/[^/]+\/events)\/([^/]+)$/);
  if (match && method === 'PATCH') {
    const result = store.mutate((db) => updateEvent(db, match[1], body));
    return sendJson(res, 200, result);
  }

  if (p === '/api/evaluate' && method === 'GET') {
    if (!query.studentId) throw new DomainError(400, 'STUDENT_REQUIRED', '请提供 studentId');
    return sendJson(res, 200, evaluateStudent(store.db, query.studentId));
  }

  match = p.match(/^\/api\/students\/([^/]+)\/evaluation$/);
  if (match && method === 'GET') {
    return sendJson(res, 200, evaluateStudent(store.db, match[1]));
  }

  if (p === '/api/demo-matches' && method === 'GET') {
    return sendJson(res, 200, findDemoFillablePractice(store.db, query.action || 'raise_wall'));
  }

  if (p === '/api/cases' && method === 'GET') {
    return sendJson(res, 200, store.db.cases);
  }
  if (p === '/api/cases' && method === 'POST') {
    const result = store.mutate((db) => createCase(db, body));
    return sendJson(res, result.existing ? 200 : 201, result);
  }

  match = p.match(/^\/api\/cases\/([^/]+)$/);
  if (match && method === 'GET') {
    const c = getCase(store.db, match[1]);
    return sendJson(res, 200, { case: c, evaluation: evaluateStudent(store.db, c.studentId) });
  }

  match = p.match(/^\/api\/cases\/([^/]+)\/attendance$/);
  if (match && method === 'POST') {
    const result = store.mutate((db) => checkIn(db, match[1], body));
    return sendJson(res, result.existing ? 200 : 201, result);
  }

  match = p.match(/^\/api\/cases\/([^/]+)\/receipt$/);
  if (match && method === 'POST') {
    const result = store.mutate((db) => addReceipt(db, match[1], body));
    return sendJson(res, result.existing ? 200 : 201, result);
  }

  match = p.match(/^\/api\/cases\/([^/]+)\/confirm$/);
  if (match && method === 'POST') {
    const result = store.mutate((db) => processMentor(db, match[1], body));
    return sendJson(res, 200, result);
  }

  if (p === '/api/stamps' && method === 'GET') {
    return sendJson(res, 200, listStamps(store.db, query));
  }

  if (p === '/api/audit-logs' && method === 'GET') {
    return sendJson(res, 200, store.db.auditLogs.slice().reverse());
  }

  throw new DomainError(404, 'ROUTE_NOT_FOUND', `没有这个接口：${method} ${p}`);
}

function serveStatic(req, res, pathname) {
  let relative = pathname === '/' ? '/index.html' : pathname;
  const filePath = path.normalize(path.join(publicDir, relative));
  if (!filePath.startsWith(publicDir)) {
    res.writeHead(403);
    return res.end('Forbidden');
  }
  if (!fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
    res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    return res.end('Not found');
  }
  const ext = path.extname(filePath);
  const types = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
  };
  res.writeHead(200, { 'Content-Type': types[ext] || 'application/octet-stream' });
  fs.createReadStream(filePath).pipe(res);
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let raw = '';
    req.on('data', (chunk) => {
      raw += chunk;
      if (raw.length > 2_000_000) {
        reject(new DomainError(413, 'BODY_TOO_LARGE', '请求体过大'));
        req.destroy();
      }
    });
    req.on('end', () => {
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch (error) {
        reject(new DomainError(400, 'INVALID_JSON', '请求体不是合法 JSON'));
      }
    });
    req.on('error', reject);
  });
}

function sendJson(res, status, data) {
  const body = JSON.stringify(data, null, 2);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type, Idempotency-Key',
    'Access-Control-Allow-Methods': 'GET,POST,PATCH,OPTIONS',
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

function sendError(res, error) {
  if (error instanceof DomainError) {
    return sendJson(res, error.status, { error: { code: error.code, message: error.message, details: error.details } });
  }
  console.error(error);
  return sendJson(res, 500, { error: { code: 'INTERNAL_ERROR', message: error.message || '服务内部错误' } });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.PORT || 3000);
  const host = process.env.HOST || '0.0.0.0';
  const store = new JsonStore(process.env.DATA_FILE || path.join(__dirname, 'data', 'pottery.json'));
  const server = createServer({ store });
  server.listen(port, host, () => {
    console.log(`陶艺夜校补修办理服务已启动: http://127.0.0.1:${port}`);
    console.log(`班型: 保留原件辅导 (${SELECTED_CLASS_TYPE})`);
  });

  const extraPorts = process.env.EXTRA_PORTS
    ? process.env.EXTRA_PORTS.split(',').map(Number)
    : [8000, 8080, 5000, 4173, 5173];
  if (!process.env.PORT) {
    for (const extraPort of extraPorts) {
      const extra = createServer({ store });
      extra.on('error', (error) => {
        if (error.code !== 'EADDRINUSE') console.warn(`附加端口 ${extraPort} 启动失败:`, error.message);
      });
      extra.listen(extraPort, host, () => console.log(`兼容入口: http://127.0.0.1:${extraPort}`));
    }
  }
}
