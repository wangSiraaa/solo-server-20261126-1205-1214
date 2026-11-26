'use strict';

const http = require('http');
const { CampaignService } = require('./service');
const { DomainError, codes } = require('./errors');

const STATUS_BY_CODE = {
  [codes.VALIDATION]: 400,
  [codes.NOT_FOUND]: 404,
  [codes.CONFLICT]: 409,
  [codes.BOX_UNAVAILABLE]: 409,
  [codes.TEAM_BUSY]: 409,
  [codes.LOAN_CLOSED]: 409,
  [codes.NO_LEGAL_CONTINUATION]: 422,
  [codes.DOOR_ILLEGAL]: 422,
  [codes.UNREVEALED_PLOT_UNAVAILABLE]: 422,
  [codes.INCOMPATIBLE_BOX]: 422,
  [codes.INCOMPATIBLE_POUCH]: 422,
  [codes.PLOT_REWRITE]: 422,
  [codes.ACTION_NOT_ALLOWED]: 422,
  [codes.IRREVERSIBLE]: 422,
};

function send(res, status, body) {
  const text = JSON.stringify(body, null, 2);
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(text);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (chunk) => {
      data += chunk;
      if (data.length > 1_000_000) {
        reject(new DomainError(codes.VALIDATION, '请求体过大'));
        req.destroy();
      }
    });
    req.on('end', () => {
      if (!data) return resolve({});
      try { resolve(JSON.parse(data)); } catch (e) {
        reject(new DomainError(codes.VALIDATION, '请求体不是合法 JSON'));
      }
    });
    req.on('error', reject);
  });
}

/** 把 "/loans/:id/return" 编译为正则与参数名列表。 */
function compilePattern(pattern) {
  const names = [];
  const regex = new RegExp(
    '^' + pattern.replace(/:[A-Za-z0-9_]+/g, (m) => {
      names.push(m.slice(1));
      return '([^/]+)';
    }) + '$'
  );
  return { regex, names };
}

function createServer({ service, stateFile } = {}) {
  const svc = service || CampaignService.open(stateFile);

  // 每条路由 = 方法 + 模式 + 处理函数。
  const defs = [
    ['GET', '/health', async () => ({ ok: true, time: new Date().toISOString() })],

    ['GET', '/rulebooks', async () => ({ rulebooks: svc.listRulebooks() })],
    ['POST', '/rulebooks', async (p, body) => ({ rulebook: await svc.createRulebook(body) })],
    ['GET', '/rulebooks/:id', async (p) => ({ rulebook: svc.getRulebook(p.id) })],

    ['POST', '/teams', async (p, body) => ({ team: await svc.createTeam(body) })],
    ['GET', '/teams', async () => ({ teams: svc.listTeams() })],
    ['GET', '/teams/:id', async (p) => ({ team: svc.getTeam(p.id) })],
    ['GET', '/teams/:id/continuation', async (p) => svc.getContinuationPlan(p.id)],
    ['GET', '/teams/:id/takeover-check/:boxId', async (p, body, q) => (
      svc.evaluateTakeover(p.id, p.boxId, { takeover: q.takeover === 'true', pouchId: q.pouchId })
    )],

    ['POST', '/boxes', async (p, body) => ({ box: await svc.createBox(body) })],
    ['GET', '/boxes', async (p, body, q) => ({ boxes: svc.listBoxes(q) })],
    ['GET', '/boxes/:id', async (p) => ({ box: svc.getBox(p.id) })],

    ['POST', '/cards', async (p, body) => ({ card: await svc.createCard(body) })],
    ['GET', '/cards', async (p, body, q) => ({ cards: svc.listCards(q) })],
    ['GET', '/cards/:id', async (p) => ({ card: svc.getCard(p.id) })],

    ['POST', '/pouches', async (p, body) => ({ pouch: await svc.createPouch(body) })],
    ['GET', '/pouches', async (p, body, q) => ({ pouches: svc.listPouches(q) })],
    ['GET', '/pouches/:id', async (p) => ({ pouch: svc.getPouch(p.id) })],

    ['POST', '/loans', async (p, body) => svc.checkout(body)],
    ['GET', '/loans', async (p, body, q) => ({ loans: svc.listLoans(q) })],
    ['GET', '/loans/:id', async (p) => ({ loan: svc.getLoan(p.id) })],
    ['POST', '/loans/:id/choose-door', async (p, body) => svc.chooseDoor(p.id, body)],
    ['POST', '/loans/:id/open-hidden-pouch', async (p, body) => svc.openHiddenPouch(p.id, body)],
    ['POST', '/loans/:id/affix-plate', async (p, body) => svc.affixPlate(p.id, body)],
    ['POST', '/loans/:id/open-card', async (p, body) => svc.openCard(p.id, body)],
    ['POST', '/loans/:id/return', async (p, body) => svc.returnLoan(p.id, body)],

    ['GET', '/receipts', async (p, body, q) => ({ receipts: svc.listReceipts(q) })],
    ['GET', '/receipts/:id', async (p) => ({ receipt: svc.getReceipt(p.id) })],

    ['GET', '/reports/rebindings', async () => svc.reportRebindings()],
  ];

  const routes = defs.map(([method, pattern, handler]) => ({
    method, pattern, handler, ...compilePattern(pattern),
  }));

  const server = http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, 'http://localhost');
      const pathname = url.pathname.replace(/\/+$/, '') || '/';
      const method = req.method.toUpperCase();

      for (const route of routes) {
        if (route.method !== method) continue;
        const m = route.regex.exec(pathname);
        if (!m) continue;
        const params = {};
        route.names.forEach((name, i) => { params[name] = decodeURIComponent(m[i + 1]); });
        const query = Object.fromEntries(url.searchParams.entries());
        const body = ['POST', 'PUT', 'PATCH'].includes(method) ? await readBody(req) : {};
        const result = await route.handler(params, body, query);
        return send(res, 200, result);
      }
      return send(res, 404, { error: { code: 'NOT_FOUND', message: `无此路由 ${method} ${pathname}` } });
    } catch (err) {
      if (err instanceof DomainError) {
        const status = STATUS_BY_CODE[err.code] || 400;
        return send(res, status, { error: { code: err.code, message: err.message, details: err.details } });
      }
      return send(res, 500, { error: { code: 'INTERNAL', message: err.message } });
    }
  });

  server.service = svc;
  return server;
}

module.exports = { createServer };
