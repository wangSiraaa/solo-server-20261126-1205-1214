import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { DomainError, notFound } from './errors.mjs';
import { fingerprint } from './hash.mjs';
import {
  applyHand,
  buildSettlement,
  createGame,
  getMarkingState,
  openContinuation,
  publicMove,
  replayAt,
  setSuspension,
  submitMarking,
  verifyEndorsement,
} from './game.mjs';
import {
  eligibleReferees,
  evaluateAdjudication,
  evaluateCase,
  leagueStandings,
  openAdjudication,
  setRefereeDuty,
  submitRefereeVote,
} from './adjudication.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const publicDir = path.resolve(__dirname, '..', 'public');

function sendJson(res, statusCode, body) {
  const json = JSON.stringify(body, null, 2);
  res.writeHead(statusCode, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  });
  res.end(json);
}

async function readJsonBody(req) {
  if (req.method === 'GET' || req.method === 'HEAD') return {};
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 4_000_000) throw new DomainError('BODY_TOO_LARGE', '请求体过大', 413);
    chunks.push(chunk);
  }
  if (chunks.length === 0) return {};
  const text = Buffer.concat(chunks).toString('utf8');
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new DomainError('BAD_JSON', '请求体不是合法 JSON', 400);
  }
}

function gameSummary(game) {
  return {
    id: game.id,
    leagueId: game.leagueId,
    mode: game.mode,
    size: game.size,
    status: game.status,
    phase: game.phase,
    turn: game.turn,
    players: game.players,
    moveCount: game.moves.length,
    consecutivePasses: game.consecutivePasses,
    stop: game.stop && { moveSeq: game.stop.moveSeq, signature: game.stop.signature, at: game.stop.at },
    suspended: Boolean(game.suspension?.suspended),
    settled: Boolean(game.result),
    winner: game.result?.winner || null,
  };
}

function sgfCoord(value) {
  // SGF uses two lowercase letters; our 1..5 maps to a..e.
  return String.fromCharCode(96 + Number(value));
}

function toSgf(game) {
  const setup = [];
  for (let y = 1; y <= game.size; y += 1) {
    for (let x = 1; x <= game.size; x += 1) {
      const stone = game.initialBoard[(y - 1) * game.size + (x - 1)];
      if (stone) setup.push(`${stone === 'black' ? 'AB' : 'AW'}[${sgfCoord(x)}${sgfCoord(y)}]`);
    }
  }
  const moves = game.moves
    .map((m) => {
      const color = m.color === 'black' ? 'B' : 'W';
      if (m.type === 'pass') return `;${color}[]`;
      return `;${color}[${sgfCoord(m.x)}${sgfCoord(m.y)}]`;
    })
    .join('');
  return `(;GM[1]SZ[${game.size}]GN[${game.id}]${setup.join('')}${moves})\n`;
}

export function createApp(options = {}) {
  const store = options.store;
  if (!store) throw new Error('createApp 需要 store');
  const clock = options.clock || (() => new Date());
  const nowIso = () => clock().toISOString();
  const inFlightIdempotency = new Map();

  function domainErrorBody(error) {
    return { error: { code: error.code, message: error.message, details: error.details } };
  }

  async function idempotent(req, res, url, body, handler) {
    const key = req.headers['idempotency-key'] || body?.requestId || null;
    const requestHash = fingerprint({ method: req.method, path: url.pathname, body });
    const existing = store.idempotentResponse(key);
    if (existing) {
      if (existing.requestHash !== requestHash) {
        return sendJson(res, 409, {
          error: { code: 'IDEMPOTENCY_KEY_REUSED', message: '同一 requestId 被用于不同请求' },
        });
      }
      return sendJson(res, existing.statusCode, { ...existing.body, idempotentReplay: true });
    }

    const inFlight = key ? inFlightIdempotency.get(key) : null;
    if (inFlight && inFlight.requestHash !== requestHash) {
      return sendJson(res, 409, {
        error: { code: 'IDEMPOTENCY_KEY_REUSED', message: '同一 requestId 被用于不同并发请求' },
      });
    }

    const concurrentReplay = Boolean(inFlight);
    const promise = inFlight?.promise || (async () => {
      const output = (await handler()) || {};
      return { statusCode: output.statusCode || 200, body: output.body ?? output };
    })();
    if (key && !inFlight) inFlightIdempotency.set(key, { promise, requestHash });

    try {
      const output = await promise;
      // Thrown DomainErrors reject and are not stored; resolved 4xx audit
      // outcomes (for example a late referee opinion) are stored normally.
      if (key && !store.idempotentResponse(key)) {
        store.saveIdempotentResponse(key, requestHash, output.statusCode, output.body);
      }
      store.save();
      if (key && !concurrentReplay) inFlightIdempotency.delete(key);
      return sendJson(
        res,
        output.statusCode,
        concurrentReplay ? { ...output.body, idempotentReplay: true } : output.body,
      );
    } catch (error) {
      if (key && !concurrentReplay) inFlightIdempotency.delete(key);
      if (error instanceof DomainError) {
        return sendJson(res, error.statusCode, domainErrorBody(error));
      }
      console.error(error);
      return sendJson(res, 500, { error: { code: 'INTERNAL', message: error.message } });
    }
  }

  async function handler(req, res) {
    const url = new URL(req.url, 'http://club.local');
    const pathname = url.pathname;

    if (req.method === 'GET' && (pathname === '/' || pathname === '/index.html')) {
      const file = path.join(publicDir, 'index.html');
      try {
        const html = fs.readFileSync(file);
        res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
        return res.end(html);
      } catch {
        return sendJson(res, 200, { service: 'go-club-referee', apiPrefix: '/api' });
      }
    }

    if (!pathname.startsWith('/api/')) {
      const maybeFile = path.join(publicDir, path.normalize(pathname).replace(/^(\.\.[/\\])+/, ''));
      if (pathname.startsWith('/client/') && fs.existsSync(maybeFile) && fs.statSync(maybeFile).isFile()) {
        const ext = path.extname(maybeFile);
        const type = ext === '.mjs' || ext === '.js' ? 'text/javascript; charset=utf-8' : 'text/plain; charset=utf-8';
        res.writeHead(200, { 'content-type': type });
        return fs.createReadStream(maybeFile).pipe(res);
      }
      return sendJson(res, 404, { error: { code: 'NOT_FOUND', message: pathname } });
    }

    let body = {};
    try {
      body = await readJsonBody(req);
    } catch (error) {
      return sendJson(res, error.statusCode || 400, { error: { code: error.code || 'BAD_REQUEST', message: error.message } });
    }

    try {
      // Health and league -----------------------------------------------------
      if (req.method === 'GET' && pathname === '/api/health') {
        return sendJson(res, 200, { ok: true, time: nowIso(), games: Object.keys(store.state.games).length, results: Object.keys(store.state.results).length });
      }

      if (req.method === 'GET' && pathname === '/api/league') {
        return sendJson(res, 200, { league: store.state.league, eligiblePreview: eligibleReferees(store.state.league, { id: '*' }).map((r) => r.id) });
      }

      let match = pathname.match(/^\/api\/referees\/([^/]+)\/duty$/);
      if (req.method === 'POST' && match) {
        const refereeId = decodeURIComponent(match[1]);
        return idempotent(req, res, url, body, () => {
          const out = setRefereeDuty(store.state.league, refereeId, body.onDuty, { ...body, now: nowIso() });
          return { statusCode: 200, body: out };
        });
      }

      if (req.method === 'POST' && pathname === '/api/league/duty') {
        return idempotent(req, res, url, body, () => {
          const out = setRefereeDuty(store.state.league, body.refereeId, body.onDuty, { ...body, now: nowIso() });
          return { statusCode: 200, body: out };
        });
      }

      // Games -----------------------------------------------------------------
      if (req.method === 'POST' && pathname === '/api/games') {
        return idempotent(req, res, url, body, () => {
          if (body.id && store.getGame(body.id)) {
            const existing = store.getGame(body.id);
            const candidate = createGame({ ...body, now: nowIso() });
            if (candidate.creationSpecHash !== existing.creationSpecHash) {
              throw new DomainError(
                'GAME_ID_ALREADY_EXISTS',
                `对局号 ${body.id} 已存在且创建规格不同；同一对局号不能开第二盘`,
                409,
              );
            }
            return { statusCode: 200, body: { game: existing, alreadyExisted: true } };
          }
          const game = createGame({ ...body, now: nowIso() });
          store.putGame(game);
          return { statusCode: 201, body: { game } };
        });
      }

      if (req.method === 'GET' && pathname === '/api/games') {
        return sendJson(res, 200, { games: Object.values(store.state.games).map(gameSummary) });
      }

      match = pathname.match(/^\/api\/games\/([^/]+)$/);
      if (req.method === 'GET' && match) {
        const game = store.getGame(decodeURIComponent(match[1]));
        if (!game) throw notFound('GAME_NOT_FOUND', `找不到对局：${match[1]}`);
        return sendJson(res, 200, { game, markingState: getMarkingState(game) });
      }

      match = pathname.match(/^\/api\/games\/([^/]+)\/moves$/);
      if (req.method === 'POST' && match) {
        const game = store.getGame(decodeURIComponent(match[1]));
        if (!game) throw notFound('GAME_NOT_FOUND', `找不到对局：${match[1]}`);
        return idempotent(req, res, url, body, () => {
          const move = applyHand(game, { ...body, type: 'place', now: nowIso() });
          return { statusCode: 200, body: { move: publicMove(move), markingState: getMarkingState(game), game: gameSummary(game) } };
        });
      }

      match = pathname.match(/^\/api\/games\/([^/]+)\/pass$/);
      if (req.method === 'POST' && match) {
        const game = store.getGame(decodeURIComponent(match[1]));
        if (!game) throw notFound('GAME_NOT_FOUND', `找不到对局：${match[1]}`);
        return idempotent(req, res, url, body, () => {
          const move = applyHand(game, { ...body, type: 'pass', now: nowIso() });
          return { statusCode: 200, body: { move: publicMove(move), markingState: getMarkingState(game), game: gameSummary(game) } };
        });
      }

      match = pathname.match(/^\/api\/games\/([^/]+)\/markings$/);
      if (req.method === 'POST' && match) {
        const game = store.getGame(decodeURIComponent(match[1]));
        if (!game) throw notFound('GAME_NOT_FOUND', `找不到对局：${match[1]}`);
        return idempotent(req, res, url, body, () => {
          const out = submitMarking(game, { ...body, now: nowIso() });
          return { statusCode: 200, body: out };
        });
      }

      match = pathname.match(/^\/api\/games\/([^/]+)\/disputes$/);
      if (req.method === 'GET' && match) {
        const game = store.getGame(decodeURIComponent(match[1]));
        if (!game) throw notFound('GAME_NOT_FOUND', `找不到对局：${match[1]}`);
        return sendJson(res, 200, { markingState: getMarkingState(game) });
      }

      match = pathname.match(/^\/api\/games\/([^/]+)\/endorsements\/verify$/);
      if (req.method === 'POST' && match) {
        const game = store.getGame(decodeURIComponent(match[1]));
        if (!game) throw notFound('GAME_NOT_FOUND', `找不到对局：${match[1]}`);
        return idempotent(req, res, url, body, () => ({ statusCode: 200, body: verifyEndorsement(game, body) }));
      }

      match = pathname.match(/^\/api\/games\/([^/]+)\/continuation$/);
      if (req.method === 'POST' && match) {
        const game = store.getGame(decodeURIComponent(match[1]));
        if (!game) throw notFound('GAME_NOT_FOUND', `找不到对局：${match[1]}`);
        return idempotent(req, res, url, body, () => ({
          statusCode: 200,
          body: { continuation: openContinuation(game, { ...body, now: nowIso() }), game: gameSummary(game) },
        }));
      }

      match = pathname.match(/^\/api\/games\/([^/]+)\/adjudication\/evaluate$/);
      if (req.method === 'POST' && match) {
        const game = store.getGame(decodeURIComponent(match[1]));
        if (!game) throw notFound('GAME_NOT_FOUND', `找不到对局：${match[1]}`);
        return idempotent(req, res, url, body, () => ({
          statusCode: 200,
          body: { adjudication: evaluateAdjudication(game, store.state.league, { ...body, now: nowIso() }) },
        }));
      }

      match = pathname.match(/^\/api\/games\/([^/]+)\/adjudication$/);
      if (req.method === 'POST' && match) {
        const game = store.getGame(decodeURIComponent(match[1]));
        if (!game) throw notFound('GAME_NOT_FOUND', `找不到对局：${match[1]}`);
        return idempotent(req, res, url, body, () => ({
          statusCode: 201,
          body: openAdjudication(game, store.state.league, { ...body, now: nowIso() }),
        }));
      }

      match = pathname.match(/^\/api\/games\/([^/]+)\/suspension$/);
      if (req.method === 'POST' && match) {
        const game = store.getGame(decodeURIComponent(match[1]));
        if (!game) throw notFound('GAME_NOT_FOUND', `找不到对局：${match[1]}`);
        return idempotent(req, res, url, body, () => ({
          statusCode: 200,
          body: { suspension: setSuspension(game, { ...body, now: nowIso() }), game: gameSummary(game) },
        }));
      }

      match = pathname.match(/^\/api\/games\/([^/]+)\/settle$/);
      if (req.method === 'POST' && match) {
        const game = store.getGame(decodeURIComponent(match[1]));
        if (!game) throw notFound('GAME_NOT_FOUND', `找不到对局：${match[1]}`);
        return idempotent(req, res, url, body, () => {
          const result = buildSettlement(game, { ...body, now: nowIso() });
          game.status = 'settled';
          game.result = result;
          store.putResult(result);
          return { statusCode: 200, body: { result, game: gameSummary(game) } };
        });
      }

      match = pathname.match(/^\/api\/games\/([^/]+)\/replay$/);
      if (req.method === 'GET' && match) {
        const game = store.getGame(decodeURIComponent(match[1]));
        if (!game) throw notFound('GAME_NOT_FOUND', `找不到对局：${match[1]}`);
        if (url.searchParams.has('step')) {
          const step = Number(url.searchParams.get('step'));
          return sendJson(res, 200, replayAt(game, step));
        }
        return sendJson(res, 200, {
          gameId: game.id,
          size: game.size,
          initialBoard: game.initialBoard,
          moveHistoryHash: game.result?.moveHistoryHash || null,
          totalSteps: game.moves.length,
          steps: game.moves.map((m) => ({ ...publicMove(m), board: m.boardAfter })),
        });
      }

      match = pathname.match(/^\/api\/games\/([^/]+)\/sgf$/);
      if (req.method === 'GET' && match) {
        const game = store.getGame(decodeURIComponent(match[1]));
        if (!game) throw notFound('GAME_NOT_FOUND', `找不到对局：${match[1]}`);
        res.writeHead(200, { 'content-type': 'application/root-tree+sgf; charset=utf-8' });
        return res.end(toSgf(game));
      }

      // Cases -----------------------------------------------------------------
      match = pathname.match(/^\/api\/cases\/([^/]+)\/evaluate$/);
      if (req.method === 'POST' && match) {
        const found = store.findCase(decodeURIComponent(match[1]));
        if (!found) throw notFound('CASE_NOT_FOUND', `找不到裁定案：${match[1]}`);
        return idempotent(req, res, url, body, () => ({
          statusCode: 200,
          body: { case: evaluateCase(found.case, store.state.league, found.game, { ...body, now: nowIso() }) },
        }));
      }

      match = pathname.match(/^\/api\/cases\/([^/]+)\/votes$/);
      if (req.method === 'POST' && match) {
        const found = store.findCase(decodeURIComponent(match[1]));
        if (!found) throw notFound('CASE_NOT_FOUND', `找不到裁定案：${match[1]}`);
        return idempotent(req, res, url, body, () => {
          const out = submitRefereeVote(found.game, store.state.league, found.case.id, { ...body, now: nowIso() });
          return { statusCode: out.statusCode || 200, body: out.body };
        });
      }

      // Results ---------------------------------------------------------------
      if (req.method === 'GET' && pathname === '/api/results') {
        const results = Object.values(store.state.results).sort((a, b) => a.gameId.localeCompare(b.gameId));
        return sendJson(res, 200, { count: results.length, uniqueBy: 'gameId', results });
      }

      if (req.method === 'GET' && pathname === '/api/league/results') {
        const results = Object.values(store.state.results)
          .filter((result) => result.ranked)
          .sort((a, b) => a.gameId.localeCompare(b.gameId));
        return sendJson(res, 200, {
          count: results.length,
          uniqueBy: 'gameId',
          scope: 'formal-league-only',
          results,
        });
      }

      if (req.method === 'GET' && (pathname === '/api/standings' || pathname === '/api/league/standings')) {
        const allResults = Object.values(store.state.results);
        const results = allResults.filter((result) => result.ranked);
        return sendJson(res, 200, { standings: leagueStandings(results), resultsCount: results.length });
      }

      return sendJson(res, 404, { error: { code: 'NOT_FOUND', message: `未知接口：${req.method} ${pathname}` } });
    } catch (error) {
      if (error instanceof DomainError) {
        return sendJson(res, error.statusCode, { error: { code: error.code, message: error.message, details: error.details } });
      }
      console.error(error);
      return sendJson(res, 500, { error: { code: 'INTERNAL', message: error.message } });
    }
  }

  handler.store = store;
  return handler;
}
