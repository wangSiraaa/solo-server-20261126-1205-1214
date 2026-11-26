import http from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { NightSchoolService, ServiceError } from './service.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const indexHtmlPath = path.join(__dirname, '..', 'public', 'index.html');

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload, null, 2);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  });
  res.end(body);
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (chunks.length === 0) return {};
  const text = Buffer.concat(chunks).toString('utf8');
  if (!text.trim()) return {};
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed;
    throw new Error('body must be a JSON object');
  } catch (error) {
    throw new ServiceError(400, 'INVALID_JSON', `请求体不是有效 JSON 对象：${error.message}`);
  }
}

function notFound(res) {
  sendJson(res, 404, { ok: false, error: { code: 'ROUTE_NOT_FOUND', message: '找不到接口或页面' } });
}

export function createServer(service) {
  return http.createServer(async (req, res) => {
    res.setHeader('access-control-allow-origin', '*');
    res.setHeader('access-control-allow-methods', 'GET,POST,PATCH,PUT,DELETE,OPTIONS');
    res.setHeader('access-control-allow-headers', 'content-type,idempotency-key,x-invitation-token,x-draft-token');
    if (req.method === 'OPTIONS') {
      res.writeHead(204);
      res.end();
      return;
    }

    const url = new URL(req.url, 'http://night-school.local');
    const pathname = decodeURIComponent(url.pathname);
    const segments = pathname.split('/').filter(Boolean);
    if (segments[1] === 'sessions') segments[1] = 'classes';

    try {
      if (req.method === 'GET' && (pathname === '/' || pathname === '/index.html')) {
        const html = await readFile(indexHtmlPath, 'utf8');
        res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
        res.end(html);
        return;
      }

      if (segments[0] !== 'api') {
        notFound(res);
        return;
      }

      // Health / admin -----------------------------------------------------
      if (segments[1] === 'health' && req.method === 'GET') {
        sendJson(res, 200, await service.health());
        return;
      }

      if (segments[1] === 'admin' && segments[2] === 'reset' && req.method === 'POST') {
        sendJson(res, 200, await service.reset());
        return;
      }

      if (segments[1] === 'admin' && segments[2] === 'forms' && req.method === 'POST') {
        const body = await readJson(req);
        if (segments[3] === 'publish-split') {
          sendJson(res, 200, await service.publishSplitForm());
        } else {
          sendJson(res, 201, await service.publishForm(body));
        }
        return;
      }

      // Forms --------------------------------------------------------------
      if (segments[1] === 'forms' && segments[2] === 'publish-split' && req.method === 'POST') {
        sendJson(res, 200, await service.publishSplitForm());
        return;
      }
      if (segments[1] === 'forms' && req.method === 'GET') {
        if (segments[2] === 'active') {
          sendJson(res, 200, await service.getActiveForm());
          return;
        }
        if (segments[3] === 'versions' && segments[4]) {
          sendJson(res, 200, await service.getFormVersion(segments[2], segments[4]));
          return;
        }
        sendJson(res, 200, await service.listForms());
        return;
      }

      // Classes ------------------------------------------------------------
      if (segments[1] === 'classes') {
        if (!segments[2] && req.method === 'POST') {
          const body = await readJson(req);
          sendJson(res, 201, await service.createClass(body));
          return;
        }
        if (!segments[2] && req.method === 'GET') {
          sendJson(res, 200, await service.listClasses());
          return;
        }
        if (segments[2] && !segments[3] && req.method === 'GET') {
          sendJson(res, 200, await service.getClass(segments[2]));
          return;
        }
        if (segments[2] && segments[3] === 'occupancy' && req.method === 'GET') {
          sendJson(res, 200, await service.getClass(segments[2]));
          return;
        }
      }

      // Drafts -------------------------------------------------------------
      if (segments[1] === 'drafts') {
        if (!segments[2] && req.method === 'POST') {
          const body = await readJson(req);
          sendJson(res, 201, await service.createDraft(body));
          return;
        }

        const draftId = segments[2];
        if (draftId && !segments[3] && req.method === 'GET') {
          sendJson(res, 200, await service.getDraft(draftId));
          return;
        }
        if (draftId && !segments[3] && ['PATCH', 'PUT'].includes(req.method)) {
          const body = await readJson(req);
          sendJson(res, 200, await service.updateDraft(draftId, body));
          return;
        }
        if (draftId && segments[3] === 'invitations' && req.method === 'POST') {
          const body = await readJson(req);
          sendJson(res, 201, await service.issueInvitation(draftId, body));
          return;
        }
        if (draftId && segments[3] === 'submit' && req.method === 'POST') {
          const body = await readJson(req);
          const result = await service.submitDraft(draftId, {
            ...body,
            idempotencyKey: body.idempotencyKey ?? req.headers['idempotency-key'] ?? null,
          });
          sendJson(res, result.replayed ? 200 : 201, result);
          return;
        }
      }

      // Invitations / companion receipts -----------------------------------
      if (segments[1] === 'invitations' && segments[2]) {
        const invitationId = segments[2];
        if (!segments[3] && req.method === 'GET') {
          sendJson(res, 200, await service.getInvitation(invitationId, url.searchParams.get('token') ?? undefined));
          return;
        }
        if (segments[3] === 'confirm' && req.method === 'POST') {
          const body = await readJson(req);
          sendJson(res, 200, await service.confirmInvitation(invitationId, {
            ...body,
            token: body.token ?? req.headers['x-invitation-token'],
          }));
          return;
        }
      }

      if (segments[1] === 'companion-receipts' && req.method === 'POST') {
        const body = await readJson(req);
        if (!body.invitationId) {
          throw new ServiceError(400, 'INVITATION_ID_REQUIRED', '创建同伴回执需要 invitationId');
        }
        sendJson(res, 201, await service.confirmInvitation(body.invitationId, body));
        return;
      }

      // Orders -------------------------------------------------------------
      if (segments[1] === 'orders') {
        if (!segments[2] && req.method === 'POST') {
          const body = await readJson(req);
          if (!body.draftId) throw new ServiceError(400, 'DRAFT_ID_REQUIRED', '递交报名需要 draftId');
          const result = await service.submitDraft(body.draftId, {
            ...body,
            idempotencyKey: body.idempotencyKey ?? req.headers['idempotency-key'] ?? null,
          });
          sendJson(res, result.replayed ? 200 : 201, result);
          return;
        }
        if (!segments[2] && req.method === 'GET') {
          sendJson(res, 200, await service.listOrders());
          return;
        }
        if (segments[2] && !segments[3] && req.method === 'GET') {
          sendJson(res, 200, await service.getOrder(segments[2]));
          return;
        }
      }

      // Counter ------------------------------------------------------------
      if (segments[1] === 'counter' && segments[2] === 'orders' && segments[3] && req.method === 'GET') {
        sendJson(res, 200, await service.getOrder(segments[3]));
        return;
      }
      if (segments[1] === 'counter' && segments[2] === 'enrollments' && segments[3] && req.method === 'GET') {
        sendJson(res, 200, await service.findByEnrollment(segments[3]));
        return;
      }
      if (segments[1] === 'enrollments' && segments[2] && req.method === 'GET') {
        sendJson(res, 200, await service.findByEnrollment(segments[2]));
        return;
      }

      notFound(res);
    } catch (error) {
      if (error instanceof ServiceError) {
        sendJson(res, error.status, {
          ok: false,
          error: { code: error.code, message: error.message, details: error.details },
        });
        return;
      }
      console.error('[unhandled]', error);
      sendJson(res, 500, {
        ok: false,
        error: { code: 'INTERNAL_ERROR', message: error.message ?? '服务内部错误' },
      });
    }
  });
}

export async function startDefaultServer() {
  const port = Number(process.env.PORT ?? 3000);
  const host = process.env.HOST ?? '0.0.0.0';
  const dataFile = process.env.DATA_FILE
    ?? path.join(__dirname, '..', 'data', 'night-school.json');
  const service = new NightSchoolService({ dataFile });
  await service.init();
  const server = createServer(service);
  server.listen(port, host, () => {
    console.log(`Night school service listening on http://${host}:${port}`);
    console.log(`Policy: pinned-form-version; data: ${dataFile}`);
  });

  const shutdown = () => {
    server.close(() => process.exit(0));
    server.closeAllConnections?.();
    setTimeout(() => process.exit(0), 500).unref();
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
  return server;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  startDefaultServer().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
