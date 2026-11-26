'use strict';

const http = require('node:http');
const { HttpError } = require('./errors');

function sendJson(res, status, body, headers = {}) {
  const payload = JSON.stringify(body, null, 2);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    ...headers,
  });
  res.end(payload);
}

async function readJsonBody(req) {
  if (req.method === 'GET' || req.method === 'HEAD') return {};
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 2_000_000) {
      throw new HttpError(413, 'BODY_TOO_LARGE', '请求体超过 2MB');
    }
    chunks.push(chunk);
  }
  if (chunks.length === 0) return {};
  const raw = Buffer.concat(chunks).toString('utf8');
  try {
    const parsed = JSON.parse(raw);
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('top-level JSON value must be an object');
    }
    return parsed;
  } catch (error) {
    throw new HttpError(400, 'INVALID_JSON', `请求体不是有效 JSON 对象: ${error.message}`);
  }
}

function createApp(service) {
  return http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const pathname = url.pathname.replace(/\/+$/, '') || '/';
    const method = req.method;

    try {
      if (method === 'OPTIONS') {
        res.writeHead(204, {
          'access-control-allow-origin': '*',
          'access-control-allow-methods': 'GET,POST,PATCH,OPTIONS',
          'access-control-allow-headers': 'content-type,idempotency-key,x-actor-id',
        });
        return res.end();
      }
      res.setHeader('access-control-allow-origin', '*');

      const body = await readJsonBody(req);
      const idemKey = req.headers['idempotency-key'];
      if (idemKey && !body.requestId) body.requestId = String(idemKey);

      // ---------- system ----------
      if (method === 'GET' && pathname === '/health') {
        return sendJson(res, 200, service.health());
      }

      if (method === 'GET' && pathname === '/') {
        return sendJson(res, 200, {
          name: '合租房带看与分佣服务',
          endpoints: [
            'GET /health',
            'GET /rooms',
            'POST /rooms',
            'PATCH /rooms/:id',
            'GET /listings',
            'POST /listings',
            'GET /accounts/:id',
            'POST /search-orders',
            'POST /search-orders/:id/companion-mandates',
            'POST /search-orders/:id/authorizations',
            'POST /appointments',
            'POST /protections',
            'POST /visits',
            'POST /visits/:id/confirm',
            'POST /negotiations',
            'POST /negotiations/:id/confirm',
            'POST /handoffs',
            'POST /handoffs/:id/tenant-confirm',
            'POST /handoffs/:id/store-accept',
            'POST /split-agreements',
            'POST /leases',
            'GET /leases/:id',
            'POST /settlements',
            'GET /settlements/:id',
            'POST /commission-items/:id/withdraw',
            'GET /tenants/:tenantId/trace/:reference',
            'GET /stores/:storeId/trace/:reference',
            'GET /trace/:reference?as=tenant|store&actorId=...',
            'POST /demo/run',
          ],
        });
      }

      // ---------- master data ----------
      if (method === 'GET' && pathname === '/rooms') {
        return sendJson(res, 200, { rooms: service.listRooms() });
      }
      if (method === 'POST' && pathname === '/rooms') {
        return sendJson(res, 201, service.createRoom(body));
      }

      let match = pathname.match(/^\/rooms\/([^/]+)$/);
      if (match && method === 'PATCH') {
        return sendJson(res, 200, service.renameRoom(decodeURIComponent(match[1]), body.name));
      }

      if (method === 'GET' && pathname === '/listings') {
        return sendJson(res, 200, { listings: service.listListings() });
      }
      if (method === 'POST' && pathname === '/listings') {
        return sendJson(res, 201, service.createListing(body));
      }

      match = pathname.match(/^\/accounts\/([^/]+)$/);
      if (match && method === 'GET') {
        return sendJson(res, 200, service.getAccount(decodeURIComponent(match[1])));
      }

      // ---------- search orders ----------
      if (method === 'POST' && pathname === '/search-orders') {
        return sendJson(res, 201, service.createSearchOrder(body));
      }

      match = pathname.match(/^\/search-orders\/([^/]+)$/);
      if (match && method === 'GET') {
        return sendJson(res, 200, service.requireOrder(decodeURIComponent(match[1])));
      }

      match = pathname.match(/^\/search-orders\/([^/]+)\/companion-mandates$/);
      if (match && method === 'POST') {
        return sendJson(
          res,
          201,
          service.addCompanionMandate(decodeURIComponent(match[1]), body),
        );
      }

      match = pathname.match(/^\/search-orders\/([^/]+)\/authorizations$/);
      if (match && method === 'POST') {
        return sendJson(res, 201, service.addStoreAuthorization(decodeURIComponent(match[1]), body));
      }

      // ---------- appointments / protections / visits ----------
      if (method === 'POST' && pathname === '/appointments') {
        return sendJson(res, 201, service.createAppointment(body));
      }
      if (method === 'POST' && pathname === '/protections') {
        return sendJson(res, 201, service.createProtection(body));
      }
      if (method === 'POST' && pathname === '/visits') {
        return sendJson(res, 201, service.submitVisit(body));
      }

      match = pathname.match(/^\/visits\/([^/]+)\/confirm$/);
      if (match && method === 'POST') {
        return sendJson(res, 200, service.confirmVisit(decodeURIComponent(match[1]), body));
      }

      // ---------- negotiation / handoff ----------
      if (method === 'POST' && pathname === '/negotiations') {
        return sendJson(res, 201, service.createNegotiation(body));
      }
      match = pathname.match(/^\/negotiations\/([^/]+)\/confirm$/);
      if (match && method === 'POST') {
        return sendJson(
          res,
          200,
          service.confirmNegotiation(decodeURIComponent(match[1]), body.tenantId || body.actorId),
        );
      }

      if (method === 'POST' && pathname === '/handoffs') {
        return sendJson(res, 201, service.createHandoff(body));
      }
      match = pathname.match(/^\/handoffs\/([^/]+)\/tenant-confirm$/);
      if (match && method === 'POST') {
        return sendJson(
          res,
          200,
          service.confirmHandoffByTenant(decodeURIComponent(match[1]), body.tenantId || body.actorId),
        );
      }
      match = pathname.match(/^\/handoffs\/([^/]+)\/store-accept$/);
      if (match && method === 'POST') {
        return sendJson(res, 200, service.acceptHandoffByStore(decodeURIComponent(match[1]), body));
      }

      if (method === 'POST' && pathname === '/split-agreements') {
        return sendJson(res, 201, service.createSplitAgreement(body));
      }
      match = pathname.match(/^\/split-agreements\/([^/]+)\/tenant-confirm$/);
      if (match && method === 'POST') {
        return sendJson(
          res,
          200,
          service.confirmSplitAgreementByTenant(
            decodeURIComponent(match[1]),
            body.tenantId || body.actorId,
          ),
        );
      }
      match = pathname.match(/^\/split-agreements\/([^/]+)\/store-confirm$/);
      if (match && method === 'POST') {
        return sendJson(
          res,
          200,
          service.confirmSplitAgreementByStore(decodeURIComponent(match[1]), body),
        );
      }

      // ---------- lease / money ----------
      if (method === 'POST' && pathname === '/leases') {
        return sendJson(res, 201, service.signLease(body));
      }
      match = pathname.match(/^\/leases\/([^/]+)$/);
      if (match && method === 'GET') {
        return sendJson(res, 200, service.getLeaseDetail(decodeURIComponent(match[1])));
      }

      if (method === 'POST' && pathname === '/settlements') {
        const result = service.createSettlement(body);
        return sendJson(res, result.replayed ? 200 : 201, result);
      }
      match = pathname.match(/^\/settlements\/([^/]+)$/);
      if (match && method === 'GET') {
        return sendJson(res, 200, service.getSettlementDetail(decodeURIComponent(match[1])));
      }

      match = pathname.match(/^\/commission-items\/([^/]+)\/withdraw$/);
      if (match && method === 'POST') {
        const result = service.withdrawItem(decodeURIComponent(match[1]), body);
        return sendJson(res, result.replayed ? 200 : 201, result);
      }

      if (method === 'GET' && pathname === '/commission-items') {
        const storeId = url.searchParams.get('storeId');
        const status = url.searchParams.get('status');
        let items = service.state.commissionItems;
        if (storeId) items = items.filter((i) => i.payeeStoreId === storeId);
        if (status) items = items.filter((i) => i.status === status);
        return sendJson(res, 200, { commissionItems: items });
      }

      // ---------- trace ----------
      match = pathname.match(/^\/tenants\/([^/]+)\/trace\/([^/]+)$/);
      if (match && method === 'GET') {
        return sendJson(
          res,
          200,
          service.traceForTenant(decodeURIComponent(match[2]), decodeURIComponent(match[1])),
        );
      }

      match = pathname.match(/^\/stores\/([^/]+)\/trace\/([^/]+)$/);
      if (match && method === 'GET') {
        return sendJson(
          res,
          200,
          service.traceForStore(decodeURIComponent(match[2]), { storeId: decodeURIComponent(match[1]) }),
        );
      }

      match = pathname.match(/^\/trace\/([^/]+)$/);
      if (match && method === 'GET') {
        const reference = decodeURIComponent(match[1]);
        const as = url.searchParams.get('as');
        const actorId = url.searchParams.get('actorId') || req.headers['x-actor-id'];
        if (as === 'tenant') return sendJson(res, 200, service.traceForTenant(reference, actorId));
        if (as === 'store') {
          return sendJson(res, 200, service.traceForStore(reference, actorId));
        }
        throw new HttpError(400, 'MISSING_VIEWER_ROLE', '请使用 ?as=tenant&actorId=... 或 ?as=store&actorId=...');
      }

      // ---------- optional deterministic demo bootstrap ----------
      if (method === 'POST' && pathname === '/demo/run') {
        const { runAllScenarios } = require('./scenarios');
        const summary = runAllScenarios(service, { withdraw: body.withdraw !== false });
        return sendJson(res, summary.replayed ? 200 : 201, summary);
      }

      return sendJson(res, 404, { error: { code: 'ROUTE_NOT_FOUND', message: `${method} ${pathname} 不存在` } });
    } catch (error) {
      if (error instanceof HttpError || error.status) {
        const status = error.status || 400;
        return sendJson(res, status, {
          error: {
            code: error.code || 'BUSINESS_ERROR',
            message: error.message,
            details: error.details,
          },
        });
      }
      console.error(error);
      return sendJson(res, 500, {
        error: { code: 'INTERNAL_ERROR', message: error.message || 'internal error' },
      });
    }
  });
}

module.exports = { createApp };
