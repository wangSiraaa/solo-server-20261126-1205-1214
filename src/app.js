import http from 'node:http';
import { URL } from 'node:url';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { HttpError } from './errors.js';
import { Store } from './store.js';
import {
  addVerification,
  buildDecisionReport,
  buildIndex,
  certifyExample,
  createExample,
  createQuestion,
  evaluateQuestion,
  getReview,
  getSenseImpact,
  mergeSenses,
  openAttempt,
  publishEdition,
  renameHeadword,
  splitSense,
  submitAttempt,
  updateQuestion,
  nextId,
} from './domain.js';

const JSON_LIMIT = 2_000_000;

function sendJson(res, status, body) {
  const json = JSON.stringify(body, null, 2);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'access-control-allow-origin': '*',
    'access-control-allow-methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS',
    'access-control-allow-headers': 'content-type,x-actor-id,idempotency-key',
  });
  res.end(json);
}

function sendError(res, error) {
  if (error instanceof HttpError) {
    return sendJson(res, error.status, {
      ok: false,
      error: { code: error.code, message: error.message, details: error.details },
    });
  }
  console.error('[unhandled]', error);
  return sendJson(res, 500, {
    ok: false,
    error: { code: 'INTERNAL_ERROR', message: error?.message || String(error) },
  });
}

async function readJson(req) {
  if (req.method === 'GET' || req.method === 'HEAD' || req.method === 'OPTIONS') return {};
  let raw = '';
  for await (const chunk of req) {
    raw += chunk;
    if (raw.length > JSON_LIMIT) throw new HttpError(413, 'BODY_TOO_LARGE', '请求体过大。');
  }
  if (!raw.trim()) return {};
  try {
    return JSON.parse(raw);
  } catch (error) {
    throw new HttpError(400, 'INVALID_JSON', '请求体不是合法 JSON。', { message: error.message });
  }
}

function actorFrom(req, body) {
  const headerActor = req.headers['x-actor-id'];
  if (headerActor && !body.actorId && !body.editorId && !body.teacherId && !body.studentId) {
    body.actorId = headerActor;
    if (String(headerActor).startsWith('ED')) body.editorId = headerActor;
    if (String(headerActor).startsWith('TEA')) body.teacherId = headerActor;
    if (String(headerActor).startsWith('STU')) body.studentId = headerActor;
  }
  return body;
}

function publicAttempt(attempt) {
  // 当前结构中 snapshot 已无 answerKey；保留函数作为未来裁剪点。
  return attempt;
}

export function createServer(options = {}) {
  const store = options.store || new Store(options);
  const db = store.db;

  const server = http.createServer(async (req, res) => {
    if (req.method === 'OPTIONS') {
      res.writeHead(204, {
        'access-control-allow-origin': '*',
        'access-control-allow-methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS',
        'access-control-allow-headers': 'content-type,x-actor-id,idempotency-key',
      });
      return res.end();
    }

    const url = new URL(req.url, 'http://localhost');
    let segments = url.pathname.split('/').filter(Boolean);
    // 常见命名空间别名：/api/dictionary/*、/api/teacher/questions、/api/student/attempts。
    if (segments[1] === 'dictionary' || segments[1] === 'dict') {
      segments = ['api', ...segments.slice(2)];
    } else if (
      (segments[1] === 'teacher' || segments[1] === 'student') &&
      ['questions', 'editions', 'attempts', 'reviews', 'decisions'].includes(segments[2])
    ) {
      segments = ['api', ...segments.slice(2)];
    } else if (
      segments[1] === 'editor' &&
      segments[2] === 'senses' &&
      segments[3] &&
      segments[3] !== 'split' &&
      segments[4] === 'split'
    ) {
      segments = ['api', 'senses', segments[3], 'split'];
    }
    if (segments[1] === 'exercises' || segments[1] === 'quizzes') segments[1] = 'questions';
    let body = {};

    try {
      body = actorFrom(req, await readJson(req));
      const idemHeader = req.headers['idempotency-key'];
      if (idemHeader && !body.clientSubmissionId && !body.idempotencyKey) {
        body.idempotencyKey = idemHeader;
      }

      // 轻量浏览器面板，便于直接点选；机器调用仍以 /api 为准。
      if (req.method === 'GET' && url.pathname === '/') {
        const htmlPath = path.join(process.cwd(), 'public', 'index.html');
        if (existsSync(htmlPath)) {
          res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
          return res.end(readFileSync(htmlPath));
        }
        return sendJson(res, 200, apiCatalog());
      }

      if (req.method === 'GET' && (url.pathname === '/health' || url.pathname === '/api/health')) {
        return sendJson(res, 200, {
          ok: true,
          service: db.meta.service,
          classroomModel: db.meta.classroomModel,
          scenario: db.meta.scenario,
          dictionaryVersion: db.meta.dictionaryVersion,
        });
      }

      if (segments[0] !== 'api') {
        throw new HttpError(404, 'NOT_FOUND', '请从 GET /api 查看可用接口。');
      }

      const resource = segments[1];

      if (req.method === 'GET' && resource === null) {
        return sendJson(res, 200, apiCatalog());
      }
      if (req.method === 'GET' && segments.length === 1) {
        return sendJson(res, 200, apiCatalog());
      }

      if (resource === 'admin' && segments[2] === 'reset' && req.method === 'POST') {
        store.reset(body.scenario || options.defaultScenario || 'pre-split');
        store.save();
        return sendJson(res, 200, { ok: true, reset: true, scenario: store.db.meta.scenario });
      }

      if (resource === 'state' && req.method === 'GET') {
        return sendJson(res, 200, db);
      }

      if (resource === 'actors' && req.method === 'GET') {
        return sendJson(res, 200, Object.values(db.actors));
      }

      if (resource === 'index' && req.method === 'GET') {
        return sendJson(res, 200, buildIndex(db, Object.fromEntries(url.searchParams)));
      }

      if (resource === 'events' && req.method === 'GET') {
        return sendJson(res, 200, db.events);
      }

      if (resource === 'impact' && segments[2] === 'senses' && segments[3] && req.method === 'GET') {
        return sendJson(res, 200, getSenseImpact(db, segments[3]));
      }

      /* ------------------------------ 词头/义项 ----------------------------- */

      if (resource === 'headwords') {
        const id = segments[2];
        if (!id && req.method === 'GET') return sendJson(res, 200, Object.values(db.headwords));
        const headword = db.headwords[id] || Object.values(db.headwords).find(
          (hw) => hw.text === id || (hw.formerTexts || []).some((f) => f.text === id),
        );
        const headwordId = headword?.id || id;
        if (id && req.method === 'GET') {
          const hw = headword;
          if (!hw) throw new HttpError(404, 'HEADWORD_NOT_FOUND', `找不到词头：${id}`);
          return sendJson(res, 200, {
            ...hw,
            senses: hw.senseIds.map((sid) => db.senses[sid]).filter(Boolean),
            activeSenses: hw.senseIds.map((sid) => db.senses[sid]).filter((s) => s.status === 'active'),
          });
        }
        if (id && (req.method === 'PATCH' || req.method === 'PUT')) {
          const hw = renameHeadword(db, headwordId, body);
          store.save();
          return sendJson(res, 200, hw);
        }
      }

      if (resource === 'senses') {
        const id = segments[2];
        const action = segments[3];
        if (!id && req.method === 'GET') return sendJson(res, 200, Object.values(db.senses));
        if (id && !action && req.method === 'GET') {
          const sense = db.senses[id];
          if (!sense) throw new HttpError(404, 'SENSE_NOT_FOUND', `找不到义项：${id}`);
          return sendJson(res, 200, sense);
        }
        if (id && action === 'split' && req.method === 'POST') {
          const result = splitSense(db, id, body);
          store.save();
          return sendJson(res, result.idempotentReplay ? 200 : 201, { ok: true, ...result });
        }
      }

      if (resource === 'editor' && segments[2] === 'senses' && segments[3] === 'split' && req.method === 'POST') {
        const senseId = body.oldSenseId || body.senseId || 'G';
        const result = splitSense(db, senseId, body);
        store.save();
        return sendJson(res, result.idempotentReplay ? 200 : 201, { ok: true, ...result });
      }

      if (resource === 'editor' && segments[2] === 'senses' && segments[3] === 'merge' && req.method === 'POST') {
        const sense = mergeSenses(db, body);
        store.save();
        return sendJson(res, 200, { ok: true, sense });
      }

      if (resource === 'senses' && segments[2] === 'merge' && req.method === 'POST') {
        const sense = mergeSenses(db, body);
        store.save();
        return sendJson(res, 200, { ok: true, sense });
      }

      /* -------------------------------- 标签 ------------------------------- */

      if (resource === 'tags') {
        if (req.method === 'GET') return sendJson(res, 200, Object.values(db.tags));
        if (req.method === 'POST') {
          const id = body.id || nextId(db, 'TAG');
          if (db.tags[id]) throw new HttpError(409, 'TAG_ID_EXISTS', `标签已存在：${id}`);
          db.tags[id] = {
            id,
            kind: 'discrimination-label',
            label: body.label,
            semanticKey: body.semanticKey || body.key || null,
            targetSenseId: body.targetSenseId || null,
          };
          store.save();
          return sendJson(res, 201, db.tags[id]);
        }
      }

      /* ------------------------------- 例句 -------------------------------- */

      if (resource === 'examples') {
        const id = segments[2];
        const sub = segments[3];
        if (!id && req.method === 'GET') return sendJson(res, 200, Object.values(db.examples));
        if (!id && req.method === 'POST') {
          const ex = createExample(db, body);
          store.save();
          return sendJson(res, 201, ex);
        }
        if (id && req.method === 'GET') {
          const ex = db.examples[id];
          if (!ex) throw new HttpError(404, 'EXAMPLE_NOT_FOUND', `找不到例句：${id}`);
          return sendJson(res, 200, {
            ...ex,
            usages: ex.usageIds.map((uid) => db.usages[uid]).filter(Boolean),
          });
        }
        if (id && (req.method === 'PATCH' || req.method === 'PUT') && !sub) {
          const usageList = Array.isArray(body.usages)
            ? body.usages.map((u) => (typeof u === 'string' ? u : u.senseId)).filter(Boolean)
            : null;
          const senseIds = body.certifiedSenseIds || body.senseIds || usageList;
          if (senseIds) {
            certifyExample(db, id, senseIds, {
              editorIds: body.editorIds || (body.editorId ? [body.editorId] : (body.actorId ? [body.actorId] : [])),
              source: body.source || 'editor-patch-certification',
              note: body.note,
            });
            store.save();
          }
          return sendJson(res, 200, db.examples[id]);
        }
        if (id && req.method === 'POST' && (sub === 'verifications' || sub === 'verify')) {
          const result = addVerification(db, id, body);
          store.save();
          return sendJson(res, 201, { ok: true, ...result });
        }
        if (id && req.method === 'PUT' && sub === 'certified-usages') {
          const usageList = Array.isArray(body.usages)
            ? body.usages.map((u) => (typeof u === 'string' ? u : u.senseId)).filter(Boolean)
            : null;
          const senseIds = body.senseIds || body.certifiedSenseIds || usageList;
          const ex = certifyExample(db, id, senseIds, {
            editorIds: body.editorIds || (body.editorId ? [body.editorId] : []),
            source: body.source || 'direct-certification',
            note: body.note,
          });
          store.save();
          return sendJson(res, 200, ex);
        }
      }

      /* ------------------------------- 练习题 ------------------------------- */

      if (resource === 'questions') {
        const id = segments[2];
        const sub = segments[3];
        if (!id && req.method === 'GET') return sendJson(res, 200, Object.values(db.questions));
        if (!id && req.method === 'POST') {
          const q = createQuestion(db, body);
          store.save();
          return sendJson(res, 201, q);
        }
        if (id && req.method === 'GET' && !sub) {
          const q = db.questions[id];
          if (!q) throw new HttpError(404, 'QUESTION_NOT_FOUND', `找不到练习：${id}`);
          return sendJson(res, 200, q);
        }
        if (id && (req.method === 'PATCH' || req.method === 'PUT') && !sub) {
          const q = updateQuestion(db, id, body);
          store.save();
          return sendJson(res, 200, q);
        }
        if (id && req.method === 'POST' && sub === 'evaluate') {
          const decision = evaluateQuestion(db, id);
          store.save();
          return sendJson(res, 200, decision);
        }
        if (id && req.method === 'GET' && sub === 'decision') {
          const q = db.questions[id];
          if (!q) throw new HttpError(404, 'QUESTION_NOT_FOUND', `找不到练习：${id}`);
          const decision = db.decisions.find((d) => d.id === q.latestDecisionId) || evaluateQuestion(db, q);
          return sendJson(res, 200, decision);
        }
      }

      /* ------------------------------- 决定 -------------------------------- */

      if (resource === 'decisions') {
        if (segments[2] === 'report' && req.method === 'GET') {
          return sendJson(res, 200, buildDecisionReport(db, Object.fromEntries(url.searchParams)));
        }
        if (segments[2] === 'evaluate' && req.method === 'POST') {
          const result = evaluateCandidate(db, body);
          const decision = { id: nextId(db, 'DEC'), at: new Date().toISOString(), ...result };
          db.decisions.push(decision);
          store.save();
          return sendJson(res, 200, decision);
        }
        if (!segments[2] && req.method === 'GET') {
          const subjectId = url.searchParams.get('subjectId');
          const eligible = url.searchParams.get('eligible');
          const verdict = url.searchParams.get('verdict');
          let items = db.decisions;
          if (subjectId) items = items.filter((d) => d.subjectId === subjectId);
          if (eligible === 'true') items = items.filter((d) => d.eligible === true);
          if (eligible === 'false') items = items.filter((d) => d.eligible === false);
          if (verdict) items = items.filter((d) => d.verdict === verdict);
          return sendJson(res, 200, items);
        }
      }

      /* -------------------------------- 试卷 ------------------------------- */

      if (resource === 'papers') {
        const id = segments[2];
        if (req.method === 'GET' && !id) return sendJson(res, 200, Object.values(db.papers));
        if (req.method === 'POST' && !id) {
          const paperId = body.id || nextId(db, 'PAPER');
          db.papers[paperId] = { id: paperId, title: body.title || paperId, questionIds: body.questionIds || [] };
          store.save();
          return sendJson(res, 201, db.papers[paperId]);
        }
        if (id && req.method === 'GET') return sendJson(res, 200, db.papers[id] || null);
      }

      /* -------------------------------- 版本 ------------------------------- */

      if (resource === 'editions') {
        const id = segments[2];
        const sub = segments[3];
        if (!id && req.method === 'GET') {
          // 默认不向学员泄露 answerKey。
          return sendJson(res, 200, Object.values(db.editions).map((e) => ({
            id: e.id,
            title: e.title,
            status: e.status,
            classroomModel: e.classroomModel,
            questionIds: e.questionIds,
            publishedAt: e.publishedAt,
            note: e.note,
          })));
        }
        if (!id && req.method === 'POST') {
          const result = publishEdition(db, body);
          store.save();
          return sendJson(res, result.idempotentReplay ? 200 : 201, {
            ok: true,
            idempotentReplay: result.idempotentReplay,
            edition: result.edition,
          });
        }
        if (id && req.method === 'GET' && !sub) {
          const edition = db.editions[id];
          if (!edition) throw new HttpError(404, 'EDITION_NOT_FOUND', `找不到版本：${id}`);
          const view = url.searchParams.get('view');
          const actor = req.headers['x-actor-id'] || '';
          if (view === 'teacher' || actor.startsWith('TEA')) return sendJson(res, 200, edition);
          return sendJson(res, 200, (await import('./domain.js')).toStudentEdition(edition));
        }
        if (id && req.method === 'GET' && sub === 'glossary') {
          const edition = db.editions[id];
          if (!edition) throw new HttpError(404, 'EDITION_NOT_FOUND', `找不到版本：${id}`);
          const senseId = segments[4];
          if (senseId) {
            const sense = edition.glossary[senseId];
            if (!sense) throw new HttpError(404, 'GLOSSARY_TERM_NOT_IN_EDITION', `该版释义表没有 ${senseId}。`);
            return sendJson(res, 200, sense);
          }
          return sendJson(res, 200, edition.glossary);
        }
        if (id && req.method === 'POST' && sub === 'attempts') {
          const result = openAttempt(db, { ...body, editionId: id });
          store.save();
          return sendJson(res, result.idempotentReplay ? 200 : 201, {
            ok: true,
            idempotentReplay: result.idempotentReplay,
            attempt: publicAttempt(result.attempt),
          });
        }
        if (id && req.method === 'POST' && sub === 'submit') {
          if (!body.attemptId) throw new HttpError(400, 'ATTEMPT_ID_REQUIRED', '版本路径交卷必须带 attemptId。');
          const result = submitAttempt(db, body.attemptId, body, {
            expectedEditionId: id,
            idempotencyKey: idemHeader,
          });
          store.save();
          return sendJson(res, 200, { ok: true, ...result });
        }
      }

      /* ------------------------------- 作答/复核 ---------------------------- */

      if (resource === 'attempts') {
        const id = segments[2];
        const sub = segments[3];
        if (req.method === 'POST' && !id) {
          const result = openAttempt(db, body);
          store.save();
          return sendJson(res, result.idempotentReplay ? 200 : 201, { ok: true, ...result });
        }
        if (id && req.method === 'GET' && !sub) {
          const attempt = db.attempts[id];
          if (!attempt) throw new HttpError(404, 'ATTEMPT_NOT_FOUND', `找不到作答：${id}`);
          return sendJson(res, 200, publicAttempt(attempt));
        }
        if (id && req.method === 'POST' && sub === 'submit') {
          const result = submitAttempt(db, id, body, { idempotencyKey: idemHeader });
          store.save();
          return sendJson(res, 200, { ok: true, ...result });
        }
        if (id && req.method === 'GET' && sub === 'review') {
          return sendJson(res, 200, getReview(db, id));
        }
      }

      if (resource === 'reviews' && segments[2] && req.method === 'GET') {
        return sendJson(res, 200, getReview(db, segments[2]));
      }

      throw new HttpError(404, 'API_ROUTE_NOT_FOUND', `未实现的请求：${req.method} ${url.pathname}`);
    } catch (error) {
      return sendError(res, error);
    }
  });

  server.locals = { store };
  return server;
}

function apiCatalog() {
  return {
    service: '方言社本地词典编辑与制册服务',
    selectedClassroomModel: 'versioned-edition（按版封题；不交付随编课动态传播）',
    endpoints: [
      'GET /health',
      'POST /api/admin/reset',
      'GET /api/state',
      'GET /api/actors',
      'GET /api/index?q=照明',
      'PATCH /api/headwords/:id',
      'GET /api/senses',
      'POST /api/senses/:id/split',
      'POST /api/editor/senses/merge',
      'GET/POST /api/examples',
      'POST /api/examples/:id/verifications',
      'PUT /api/examples/:id/certified-usages',
      'GET/POST/PATCH /api/questions/:id?',
      'POST /api/questions/:id/evaluate',
      'GET /api/decisions/report',
      'POST /api/decisions/evaluate',
      'GET /api/decisions?subjectId=...',
      'POST /api/editions',
      'GET /api/editions/:id?view=teacher',
      'POST /api/editions/:id/attempts',
      'POST /api/attempts',
      'POST /api/attempts/:id/submit',
      'GET /api/attempts/:id/review',
      'GET /api/reviews/:attemptId',
      'GET /api/impact/senses/:id',
      'GET /api/events',
    ],
  };
}
