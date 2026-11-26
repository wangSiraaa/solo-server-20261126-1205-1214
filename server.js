'use strict';

const http = require('node:http');
const path = require('node:path');
const { URL } = require('node:url');
const { JsonStore, HttpError } = require('./lib/store');
const hotline = require('./lib/hotline');

const PORT = Number(process.env.PORT || 3000);
const HOST = process.env.HOST || '127.0.0.1';
const DATA_FILE = process.env.HOTLINE_DATA || path.join(__dirname, 'data', 'hotline.json');
const store = new JsonStore(DATA_FILE);

const consoleActions = {
  inbound: hotline.inbound,
  ask: hotline.callerAsk,
  answer: hotline.answerInbound,
  speak: hotline.agentSpeak,
  'close-item': hotline.closeItemFromUtterance,
  hold: hotline.holdCaller,
  retrieve: hotline.retrieveCaller,
  consult: hotline.startConsult,
  'answer-consult': hotline.answerConsult,
  assign: hotline.assignTicket,
  bridge: hotline.bridgeToCaller,
  'complete-handoff': hotline.completeHandover,
  hangup: hotline.hangup,
};

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload, null, 2);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  });
  res.end(body);
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (chunk) => {
      data += chunk;
      if (data.length > 2_000_000) {
        reject(new HttpError(413, 'body_too_large', '请求体过大'));
        req.destroy();
      }
    });
    req.on('end', () => {
      if (!data) return resolve({});
      try {
        resolve(JSON.parse(data));
      } catch {
        reject(new HttpError(400, 'invalid_json', '请求体不是合法 JSON'));
      }
    });
    req.on('error', reject);
  });
}

function publicError(err) {
  if (err instanceof HttpError) {
    return {
      status: err.status,
      body: {
        ok: false,
        error: {
          code: err.code,
          message: err.message,
          details: err.details,
        },
        revision: store.db.revision,
      },
    };
  }
  console.error('[unhandled]', err);
  return {
    status: 500,
    body: {
      ok: false,
      error: { code: 'internal_error', message: err.message || '内部错误' },
      revision: store.db.revision,
    },
  };
}

async function handleApi(req, res, url) {
  const parts = url.pathname.split('/').filter(Boolean);
  const method = req.method.toUpperCase();

  if (method === 'GET' && url.pathname === '/health') {
    return sendJson(res, 200, { ok: true, service: 'community-hotline', at: new Date().toISOString() });
  }

  if (method === 'GET' && url.pathname === '/') {
    return sendJson(res, 200, {
      ok: true,
      service: '社区活动咨询台话务后台',
      selectedMode: 'accompanied-transfer（陪同转接）',
      dataFile: DATA_FILE,
      revision: store.db.revision,
      endpoints: [
        'GET /health',
        'GET /board',
        'GET /agents',
        'GET /caller/:phone',
        'GET /calls/:id/audio',
        'GET /calls/:id/audio/history',
        'POST /console/inbound',
        'POST /console/ask',
        'POST /console/answer',
        'POST /console/hold',
        'POST /console/consult',
        'POST /console/answer-consult',
        'POST /console/assign',
        'POST /console/bridge',
        'POST /console/complete-handoff',
        'POST /console/speak',
        'POST /console/close-item',
        'POST /console/hangup',
        'POST /telephony/events',
        'POST /agents/:id/qualifications',
        'POST /admin/reset',
      ],
    });
  }

  if (method === 'GET' && url.pathname === '/board') {
    return sendJson(res, 200, { ok: true, board: hotline.board(store.db) });
  }

  if (method === 'GET' && url.pathname === '/state') {
    return sendJson(res, 200, { ok: true, state: store.db });
  }

  if (method === 'POST' && url.pathname === '/admin/reset') {
    store.reset();
    return sendJson(res, 200, { ok: true, reset: true, revision: 0 });
  }

  if (method === 'GET' && url.pathname === '/agents') {
    return sendJson(res, 200, {
      ok: true,
      agents: Object.values(store.db.agents).map(hotline.publicAgent),
      catalog: hotline.ITEM_CATALOG,
    });
  }

  if (parts[0] === 'agents' && parts.length === 3 && parts[2] === 'qualifications' && method === 'POST') {
    const body = await readJson(req);
    const result = store.mutate((db) =>
      hotline.setQualification(
        db,
        parts[1],
        body.skill || body.itemCode,
        body.granted !== false,
        body.expectedRevision,
      ),
    );
    return sendJson(res, 200, { ok: true, ...result });
  }

  if (parts[0] === 'agents' && parts.length === 4 && parts[2] === 'skills' && method === 'PUT') {
    const body = await readJson(req);
    const result = store.mutate((db) =>
      hotline.setQualification(db, parts[1], parts[3], true, body.expectedRevision),
    );
    return sendJson(res, 200, { ok: true, ...result });
  }

  if (parts[0] === 'agents' && parts.length === 4 && parts[2] === 'skills' && method === 'DELETE') {
    const body = await readJson(req).catch(() => ({}));
    const result = store.mutate((db) =>
      hotline.setQualification(db, parts[1], parts[3], false, body.expectedRevision),
    );
    return sendJson(res, 200, { ok: true, ...result });
  }

  if (parts[0] === 'agents' && parts.length === 3 && parts[2] === 'presence' && method === 'POST') {
    const body = await readJson(req);
    const result = store.mutate((db) =>
      hotline.setPresence(db, parts[1], body.presence, body.expectedRevision),
    );
    return sendJson(res, 200, { ok: true, ...result });
  }

  if (method === 'GET' && parts[0] === 'caller' && parts[1]) {
    return sendJson(res, 200, { ok: true, ...hotline.callerDetail(store.db, decodeURIComponent(parts[1])) });
  }

  if (method === 'GET' && parts[0] === 'calls' && parts[1]) {
    const call = store.db.calls[parts[1]];
    if (!call) throw new HttpError(404, 'call_not_found', '通话支路不存在');
    if (parts[2] === 'audio' && !parts[3]) {
      const at = url.searchParams.get('at');
      if (at) {
        const rootId = call.rootCallId || call.id;
        const matching = store.db.audioSnapshots
          .filter((s) => s.audio?.rootCallId === rootId && new Date(s.at).getTime() <= new Date(at).getTime())
          .sort((a, b) => b.revision - a.revision)[0];
        if (!matching) return sendJson(res, 404, { ok: false, error: { code: 'no_snapshot_at_time' } });
        return sendJson(res, 200, { ok: true, snapshot: matching, audio: matching.audio });
      }
      const audio = hotline.computeAudio(store.db, call.rootCallId || call.id);
      return sendJson(res, 200, { ok: true, call, audio });
    }
    if (parts[2] === 'audio' && parts[3] === 'history') {
      const rootId = call.rootCallId || call.id;
      const snapshots = store.db.audioSnapshots.filter((s) => s.audio?.rootCallId === rootId);
      return sendJson(res, 200, { ok: true, snapshots });
    }
    return sendJson(res, 200, { ok: true, call });
  }

  if (method === 'POST' && parts[0] === 'telephony' && parts[1] === 'events') {
    const body = await readJson(req);
    const result = store.mutate((db) => hotline.receiveTelephonyEvent(db, body));
    return sendJson(res, result.duplicate ? 200 : 201, {
      ok: true,
      ...result,
      revision: store.db.revision,
    });
  }

  if (method === 'POST' && parts[0] === 'console' && parts[1] && consoleActions[parts[1]]) {
    const body = await readJson(req);
    const action = consoleActions[parts[1]];
    const result = store.mutate((db) => action(db, body));
    return sendJson(res, 200, { ok: true, ...result });
  }

  if (method === 'POST' && parts[0] === 'rpc' && parts[1] && consoleActions[parts[1]]) {
    const body = await readJson(req);
    const result = store.mutate((db) => consoleActions[parts[1]](db, body));
    return sendJson(res, 200, { ok: true, ...result });
  }

  return sendJson(res, 404, {
    ok: false,
    error: { code: 'route_not_found', message: `没有路由 ${method} ${url.pathname}` },
  });
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${req.headers.host || `${HOST}:${PORT}`}`);
    if (req.method === 'GET' && url.pathname === '/favicon.ico') {
      res.writeHead(204);
      return res.end();
    }
    await handleApi(req, res, url);
  } catch (err) {
    const rendered = publicError(err);
    if (!res.headersSent) sendJson(res, rendered.status, rendered.body);
    else res.end();
  }
});

server.listen(PORT, HOST, () => {
  console.log(`社区话务后台已启动: http://${HOST}:${PORT}`);
  console.log(`数据文件: ${DATA_FILE}`);
  console.log('模式: 陪同转接（accompanied transfer）');
});

function shutdown(signal) {
  console.log(`\n收到 ${signal}，已同步持久化，退出。`);
  try {
    store.save();
  } catch (err) {
    console.error('退出前保存失败', err);
  }
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 500).unref();
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

module.exports = server;
