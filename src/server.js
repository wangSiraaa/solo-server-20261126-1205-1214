import { createServer as createHttpServer } from 'node:http';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { dirname, extname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { Store } from './store.js';
import { DomainError, PUBLIC_RULE, auditBallots, dutyRoster } from './election.js';
import { seedScenario } from './fixtures.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const publicDir = resolve(__dirname, '..', 'public');

const CONTENT_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
};

export function createServer({ store = new Store(), dataFile } = {}) {
  const effectiveStore = dataFile ? new Store(dataFile) : store;

  const server = createHttpServer(async (req, res) => {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Idempotency-Key');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, PATCH, OPTIONS');
    if (req.method === 'OPTIONS') {
      res.writeHead(204);
      res.end();
      return;
    }

    try {
      const url = new URL(req.url, 'http://127.0.0.1');
      const pathname = url.pathname.replace(/\/+$/, '') || '/';

      if (pathname.startsWith('/api/')) {
        await handleApi(req, res, url, effectiveStore);
        return;
      }
      await serveStatic(req, res, pathname);
    } catch (error) {
      sendError(res, error);
    }
  });

  server.store = effectiveStore;
  return server;
}

async function handleApi(req, res, url, store) {
  const path = url.pathname.replace(/\/+$/, '') || '/';
  const method = req.method;
  const body = ['POST', 'PUT', 'PATCH'].includes(method) ? await readBody(req) : {};
  const idempotencyKey = req.headers['idempotency-key'] ? String(req.headers['idempotency-key']) : null;
  const fingerprint = createHash('sha256')
    .update(`${method}\n${path}\n${JSON.stringify(body ?? {})}`)
    .digest('hex');

  const sendOutcome = (outcome) => {
    const headers = {};
    if (outcome.replay) headers['Idempotent-Replayed'] = 'true';
    sendJson(res, outcome.status, outcome.body, headers);
  };

  const runWrite = (operation) => {
    const outcome = store.transact({ key: idempotencyKey, fingerprint }, operation);
    sendOutcome(outcome);
  };

  if (method === 'GET' && (path === '/api' || path === '/api/info')) {
    sendJson(res, 200, {
      service: 'joint-election-backend',
      chosenProcedure: 'prior-joint-commitment',
      ruleVersion: 'joint-v1',
      ui: '/',
      endpoints: [
        '/api/health',
        '/api/rules',
        '/api/scenarios',
        '/api/scenarios/seed',
        '/api/rounds',
        '/api/rounds/:roundId',
        '/api/rounds/:roundId/ballots',
        '/api/rounds/:roundId/audit',
        '/api/rounds/:roundId/duty-roster',
        '/api/rounds/:roundId/settle',
        '/api/rounds/:roundId/appointments',
        '/api/receipts/:receiptCode',
      ],
    });
    return;
  }

  if (method === 'GET' && path === '/api/health') {
    sendJson(res, 200, { ok: true, service: 'joint-election-backend', time: new Date().toISOString() });
    return;
  }

  if (method === 'GET' && path === '/api/rules') {
    sendJson(res, 200, PUBLIC_RULE);
    return;
  }

  if (method === 'GET' && path === '/api/scenarios') {
    sendJson(res, 200, {
      scenarios: [
        'transfer',
        'ineligible-backup',
        'refused-backup',
        'both-willing',
        'overquota',
      ],
    });
    return;
  }

  if (method === 'POST' && path === '/api/scenarios/seed') {
    runWrite(() => {
      const scenarioName = body.scenario ?? body.name ?? 'transfer';
      const round = seedScenario(store, scenarioName, body.options ?? body);
      return { status: 201, body: store.getRound(round.id) };
    });
    return;
  }

  if (method === 'POST' && path === '/api/rounds') {
    runWrite(() => store.createRound(body));
    return;
  }

  if (method === 'GET' && path === '/api/rounds') {
    sendJson(res, 200, { rounds: store.listRounds().map(publicRoundSummary) });
    return;
  }

  let match = path.match(/^\/api\/rounds\/([^/]+)$/);
  if (match) {
    const round = store.getRound(decodeURIComponent(match[1]));
    if (method === 'GET') return sendJson(res, 200, round);
    throw new DomainError(405, 'METHOD_NOT_ALLOWED', '仅支持 GET');
  }

  match = path.match(/^\/api\/rounds\/([^/]+)\/candidates$/);
  if (match && method === 'POST') {
    runWrite(() => store.putCandidate(decodeURIComponent(match[1]), body));
    return;
  }

  match = path.match(/^\/api\/rounds\/([^/]+)\/candidates\/([^/]+)$/);
  if (match && (method === 'PUT' || method === 'POST')) {
    runWrite(() => store.putCandidate(decodeURIComponent(match[1]), { ...body, id: decodeURIComponent(match[2]) }));
    return;
  }

  match = path.match(/^\/api\/rounds\/([^/]+)\/candidates\/([^/]+)\/commitment$/);
  if (match && method === 'PATCH') {
    runWrite(() =>
      store.patchCommitment(decodeURIComponent(match[1]), decodeURIComponent(match[2]), body),
    );
    return;
  }

  match = path.match(/^\/api\/rounds\/([^/]+)\/ballots$/);
  if (match) {
    const roundId = decodeURIComponent(match[1]);
    if (method === 'GET') return sendJson(res, 200, { ballots: store.getRound(roundId).ballots });
    if (method === 'POST') {
      runWrite(() => store.addBallot(roundId, body));
      return;
    }
  }

  match = path.match(/^\/api\/rounds\/([^/]+)\/audit$/);
  if (match && method === 'GET') {
    const round = store.getRound(decodeURIComponent(match[1]));
    sendJson(res, 200, { roundId: round.id, destinations: auditBallots(round) });
    return;
  }

  match = path.match(/^\/api\/rounds\/([^/]+)\/duty-roster$/);
  if (match && method === 'GET') {
    const round = store.getRound(decodeURIComponent(match[1]));
    sendJson(res, 200, { roundId: round.id, roster: dutyRoster(round) });
    return;
  }

  match = path.match(/^\/api\/rounds\/([^/]+)\/appointments$/);
  if (match) {
    const roundId = decodeURIComponent(match[1]);
    if (method === 'GET') {
      const round = store.getRound(roundId);
      return sendJson(res, 200, { appointments: round.appointments ?? [] });
    }
    if (method === 'POST') {
      runWrite(() =>
        store.issueAppointment(roundId, { ...body, idempotencyKey: idempotencyKey ?? body.idempotencyKey }),
      );
      return;
    }
  }

  match = path.match(/^\/api\/rounds\/([^/]+)\/settle$/);
  if (match && method === 'POST') {
    const roundId = decodeURIComponent(match[1]);
    runWrite(() => store.settleRound(roundId, { idempotencyKey }));
    return;
  }

  match = path.match(/^\/api\/appointments\/([^/]+)$/);
  if (match && method === 'GET') {
    const result = store.findAppointment(decodeURIComponent(match[1]));
    sendJson(res, 200, {
      roundId: result.round.id,
      appointment: result.appointment,
      usedBallotIds: result.appointment.ballotIds,
    });
    return;
  }

  match = path.match(/^\/api\/receipts\/([^/]+)(\/resend)?$/);
  if (match) {
    const code = decodeURIComponent(match[1]);
    const result = store.findReceipt(code);
    if (method === 'GET' || (match[2] === '/resend' && method === 'POST')) {
      sendJson(res, 200, {
        resent: Boolean(match[2]),
        receiptCode: result.appointment.receiptCode,
        roundId: result.round.id,
        appointment: result.appointment,
        usedBallotIds: result.appointment.ballotIds,
        audit: auditBallots(result.round).filter((item) => result.appointment.ballotIds.includes(item.ballotId)),
      });
      return;
    }
  }

  sendJson(res, 404, { error: { code: 'NOT_FOUND', message: `没有接口 ${method} ${path}` } });
}

function publicRoundSummary(round) {
  return {
    id: round.id,
    name: round.name,
    status: round.status,
    ballotCount: round.ballots?.length ?? 0,
    appointmentCount: round.appointments?.length ?? 0,
    settled: Boolean(round.settlement),
    createdAt: round.createdAt,
  };
}

async function serveStatic(req, res, pathname) {
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    sendJson(res, 405, { error: { code: 'METHOD_NOT_ALLOWED' } });
    return;
  }
  const relative = pathname === '/' ? 'index.html' : pathname.replace(/^\/+/, '');
  const filePath = resolve(publicDir, relative);
  if (!filePath.startsWith(publicDir)) {
    sendJson(res, 403, { error: { code: 'FORBIDDEN' } });
    return;
  }
  try {
    const content = await readFile(filePath);
    res.writeHead(200, { 'Content-Type': CONTENT_TYPES[extname(filePath)] ?? 'application/octet-stream' });
    res.end(req.method === 'HEAD' ? undefined : content);
  } catch {
    sendJson(res, 404, { error: { code: 'NOT_FOUND', message: '静态页面不存在；API 位于 /api' } });
  }
}

function readBody(req) {
  return new Promise((resolveBody, reject) => {
    let raw = '';
    req.on('data', (chunk) => {
      raw += chunk;
      if (raw.length > 2_000_000) {
        reject(new DomainError(413, 'BODY_TOO_LARGE', '请求体过大'));
        req.destroy();
      }
    });
    req.on('end', () => {
      if (!raw) return resolveBody({});
      try {
        resolveBody(JSON.parse(raw));
      } catch (error) {
        reject(new DomainError(400, 'INVALID_JSON', `JSON 无法解析: ${error.message}`));
      }
    });
    req.on('error', reject);
  });
}

function sendJson(res, status, body, headers = {}) {
  if (res.writableEnded) return;
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', ...headers });
  res.end(JSON.stringify(body, null, 2));
}

function sendError(res, error) {
  if (error instanceof DomainError) {
    sendJson(res, error.status, { error: { code: error.code, message: error.message, details: error.details } });
    return;
  }
  console.error(error);
  sendJson(res, 500, { error: { code: 'INTERNAL_ERROR', message: error.message } });
}

const port = Number(process.env.PORT ?? 3000);
const host = process.env.HOST ?? '0.0.0.0';
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const server = createServer();
  server.listen(port, host, () => {
    console.log(`联选后台已启动: http://127.0.0.1:${port}`);
    console.log(`数据文件: ${server.store.file}`);
  });
}
