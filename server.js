import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { createReadStream, existsSync, statSync } from 'node:fs';
import { extname, join, normalize, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { Store, HttpError } from './src/store.js';
import { seedDemo } from './src/demo.js';
import { renderPatternText } from './src/domain.js';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const publicDir = join(__dirname, 'public');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml'
};

export function createApp(store) {
  return http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const path = url.pathname;
    try {
      if (path.startsWith('/api/')) {
        await handleApi(req, res, url, store);
      } else {
        await serveStatic(req, res, path);
      }
    } catch (error) {
      sendError(res, error);
    }
  });
}

async function handleApi(req, res, url, store) {
  const { pathname } = url;
  const method = req.method || 'GET';
  const body = ['POST', 'PUT', 'PATCH'].includes(method) ? await readJson(req) : {};

  if (method === 'GET' && pathname === '/api/health') {
    return sendJson(res, 200, { ok: true, revision: store.db.revision, time: new Date().toISOString() });
  }

  if (method === 'POST' && pathname === '/api/admin/reset') {
    const result = await seedDemo(store, { empty: body.empty === true });
    return sendJson(res, 200, result);
  }

  if (method === 'GET' && pathname === '/api/state') {
    return sendJson(res, 200, store.db);
  }

  if (method === 'GET' && pathname === '/api/palette') {
    const { TOKEN_COLORS } = await import('./src/domain.js');
    return sendJson(res, 200, TOKEN_COLORS);
  }

  // Cards / sample cards.
  if (method === 'GET' && pathname === '/api/cards') {
    return sendJson(res, 200, store.db.cards);
  }
  if (method === 'POST' && pathname === '/api/cards') {
    const card = await store.mutate(() => store.createCard(body));
    return sendJson(res, 201, card);
  }
  let m = pathname.match(/^\/api\/cards\/([^/]+)$/);
  if (m && method === 'GET') return sendJson(res, 200, store.findCard(decodeURIComponent(m[1])));
  if (m && (method === 'PATCH' || method === 'PUT')) {
    const card = await store.mutate(() => store.updateCard(decodeURIComponent(m[1]), body));
    return sendJson(res, 200, card);
  }

  // Bars / molds.
  if (method === 'GET' && pathname === '/api/bars') return sendJson(res, 200, store.db.bars);
  if (method === 'POST' && pathname === '/api/bars') {
    const bar = await store.mutate(() => {
      const created = store.createBar(body);
      if (Array.isArray(body.layers)) {
        body.layers.forEach((layer, i) => {
          const index = layer.index ?? i;
          store.pourLayer(created.code, index, layer.pattern ?? layer, { by: body.by });
        });
      }
      if (body.autoClose !== false) store.closeBar(created.code, body.closeMode ? { closeMode: body.closeMode } : {});
      if (body.precut) store.precutBar({ barCode: created.code, ...(body.precut === true ? {} : body.precut) });
      return store.findBar(created.code);
    });
    return sendJson(res, 201, bar);
  }

  m = pathname.match(/^\/api\/bars\/([^/]+)$/);
  if (m && method === 'GET') return sendJson(res, 200, store.findBar(decodeURIComponent(m[1])));

  m = pathname.match(/^\/api\/bars\/([^/]+)\/pours$/);
  if (m && method === 'POST') {
    const code = decodeURIComponent(m[1]);
    const result = await store.mutate(() => {
      if (Array.isArray(body.layers)) return body.layers.map((l, i) => store.pourLayer(code, l.index ?? i, l.pattern ?? l, { by: body.by, reason: body.reason }));
      if (body.index === undefined) throw new HttpError(400, 'INDEX_REQUIRED', '请提供 index 或 layers[]');
      return store.pourLayer(code, body.index, body.pattern ?? body, { by: body.by, reason: body.reason });
    });
    return sendJson(res, 200, result);
  }

  m = pathname.match(/^\/api\/bars\/([^/]+)\/cells\/(\d+)$/);
  if (m && (method === 'PATCH' || method === 'PUT')) {
    const [, code, index] = m;
    const result = await store.mutate(() => store.amendPourCell(decodeURIComponent(code), Number(index), body.row, body.col, body.color, body.reason));
    return sendJson(res, 200, result);
  }

  m = pathname.match(/^\/api\/bars\/([^/]+)\/close$/);
  if (m && method === 'POST') {
    const bar = await store.mutate(() => store.closeBar(decodeURIComponent(m[1]), body));
    return sendJson(res, 200, bar);
  }

  m = pathname.match(/^\/api\/bars\/([^/]+)\/restore-original$/);
  if (m && method === 'POST') {
    const bar = await store.mutate(() => store.restoreOriginalPours(decodeURIComponent(m[1])));
    return sendJson(res, 200, bar);
  }

  m = pathname.match(/^\/api\/bars\/([^/]+)\/replay$/);
  if (m && method === 'GET') {
    const replay = store.replayBar(decodeURIComponent(m[1]), { source: url.searchParams.get('source') || 'current' });
    replay.layers = replay.layers.map((layer) => ({
      ...layer,
      text: renderPatternText(layer.barHeadView)
    }));
    return sendJson(res, 200, replay);
  }

  m = pathname.match(/^\/api\/bars\/([^/]+)\/remnants$/);
  if (m && method === 'GET') return sendJson(res, 200, store.remnants(decodeURIComponent(m[1])));

  m = pathname.match(/^\/api\/bars\/([^/]+)\/cut$/);
  if (m && method === 'POST') {
    const piece = await store.mutate(() => store.cutFromBar({ ...body, barCode: decodeURIComponent(m[1]) }));
    return sendJson(res, 201, piece);
  }

  m = pathname.match(/^\/api\/bars\/([^/]+)\/precut$/);
  if (m && method === 'POST') {
    const pieces = await store.mutate(() => store.precutBar({ ...body, barCode: decodeURIComponent(m[1]) }));
    return sendJson(res, 201, pieces);
  }

  // Pieces and matching.
  if (method === 'GET' && pathname === '/api/pieces') return sendJson(res, 200, store.db.pieces);
  m = pathname.match(/^\/api\/pieces\/([^/]+)$/);
  if (m && method === 'GET') {
    const piece = store.findPiece(decodeURIComponent(m[1]));
    return sendJson(res, 200, { ...piece, liveFaces: store.liveFaces(piece) });
  }
  m = pathname.match(/^\/api\/pieces\/([^/]+)\/claim$/);
  if (m && method === 'POST') {
    const claim = await store.mutate(() => store.claimPieces({ ...body, pieceId: decodeURIComponent(m[1]) }));
    return sendJson(res, 201, claim);
  }

  if (method === 'GET' && pathname === '/api/matches') {
    const query = Object.fromEntries(url.searchParams);
    query.availableOnly = query.availableOnly === 'true' ? true : query.availableOnly === 'false' ? false : undefined;
    if (query.pieceIds) query.pieceIds = query.pieceIds.split(',');
    return sendJson(res, 200, store.findMatches(query));
  }
  if (method === 'POST' && pathname === '/api/matches') return sendJson(res, 200, store.findMatches(body));

  if (method === 'POST' && pathname === '/api/quotes') return sendJson(res, 200, store.quoteOrder(body));
  if (method === 'GET' && pathname === '/api/recommendations') {
    return sendJson(res, 200, store.recommendCards(Object.fromEntries(url.searchParams)));
  }
  if (method === 'POST' && pathname === '/api/recommendations') return sendJson(res, 200, store.recommendCards(body));

  // Claims.
  if (method === 'GET' && pathname === '/api/claims') return sendJson(res, 200, store.db.claims);
  if (method === 'POST' && pathname === '/api/claims') {
    const claim = await store.mutate(() => store.claimPieces(body));
    return sendJson(res, 201, claim);
  }
  m = pathname.match(/^\/api\/claims\/([^/]+)\/release$/);
  if (m && method === 'POST') {
    const claim = await store.mutate(() => store.releaseClaim(decodeURIComponent(m[1])));
    return sendJson(res, 200, claim);
  }

  // Orders and boxes.
  if (method === 'GET' && pathname === '/api/orders') return sendJson(res, 200, store.db.orders);
  if (method === 'POST' && pathname === '/api/orders') {
    const result = await store.mutate(() => store.createOrder(body));
    return sendJson(res, 201, result);
  }
  m = pathname.match(/^\/api\/orders\/([^/]+)$/);
  if (m && method === 'GET') {
    const code = decodeURIComponent(m[1]);
    const order = store.db.orders.find((o) => o.id === code || o.code === code);
    if (!order) throw new HttpError(404, 'ORDER_NOT_FOUND', `找不到订单 ${code}`);
    return sendJson(res, 200, order);
  }

  if (method === 'GET' && pathname === '/api/boxes') return sendJson(res, 200, store.db.boxes);
  m = pathname.match(/^\/api\/boxes\/([^/]+)$/);
  if (m && method === 'GET') return sendJson(res, 200, store.findBox(decodeURIComponent(m[1])));
  m = pathname.match(/^\/api\/boxes\/([^/]+)\/pickup$/);
  if (m && method === 'POST') {
    const box = await store.mutate(() => store.pickupBox(decodeURIComponent(m[1]), body));
    return sendJson(res, 200, box);
  }

  throw new HttpError(404, 'ROUTE_NOT_FOUND', `没有接口 ${method} ${pathname}`);
}

async function readJson(req) {
  return new Promise((resolveBody, reject) => {
    let data = '';
    req.on('data', (chunk) => {
      data += chunk;
      if (data.length > 4_000_000) {
        reject(new HttpError(413, 'BODY_TOO_LARGE', '请求体过大'));
        req.destroy();
      }
    });
    req.on('end', () => {
      if (!data) return resolveBody({});
      try {
        resolveBody(JSON.parse(data));
      } catch (error) {
        reject(new HttpError(400, 'BAD_JSON', '请求体不是合法 JSON', error.message));
      }
    });
    req.on('error', reject);
  });
}

function sendJson(res, status, body) {
  const json = JSON.stringify(body, (_key, value) => (value === undefined ? null : value), 2);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store'
  });
  res.end(json);
}

function sendError(res, error) {
  const status = error.status || 500;
  if (status >= 500) console.error(error);
  sendJson(res, status, {
    error: {
      status,
      code: error.code || 'INTERNAL',
      message: error.message || '服务器错误',
      details: error.details || null
    }
  });
}

async function serveStatic(req, res, pathname) {
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    res.writeHead(405);
    return res.end('Method Not Allowed');
  }
  const path = pathname === '/' ? '/index.html' : pathname;
  const filePath = normalize(join(publicDir, path));
  if (!filePath.startsWith(publicDir)) {
    res.writeHead(403);
    return res.end('Forbidden');
  }
  if (!existsSync(filePath) || !statSync(filePath).isFile()) {
    res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
    return res.end('Not found');
  }
  res.writeHead(200, { 'content-type': MIME[extname(filePath)] || 'application/octet-stream' });
  if (req.method === 'HEAD') return res.end();
  createReadStream(filePath).pipe(res);
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href;
if (isMain) {
  const dataFile = process.env.DATA_FILE ? resolve(process.env.DATA_FILE) : resolve(__dirname, 'data/db.json');
  const port = Number(process.env.PORT || 4310);
  const host = process.env.HOST || '0.0.0.0';
  const store = new Store(dataFile);
  await store.init();
  if (process.env.SEED_DEMO !== 'false' && store.db.bars.length === 0) {
    await seedDemo(store);
  }
  const server = createApp(store);
  server.listen(port, host, () => {
    console.log(`彩皂订单后台已启动: http://127.0.0.1:${port} (监听 ${host})`);
    console.log(`数据文件: ${dataFile}`);
  });

  const shutdown = () => server.close(() => process.exit(0));
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}
