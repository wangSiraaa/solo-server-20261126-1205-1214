import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { JsonStore, DEFAULT_DATA_FILE } from './store.js';
import {
  analyzeReceipt,
  buildOpportunities,
  canonicalHash,
  dayNumber,
  evaluateGate,
  getPracticeDays,
  makeItem,
  newId,
  normalizeHints,
  rebuildEvidence,
  reconcileRound,
  selectAdaptiveItems,
  uniqueSorted,
} from './domain.js';

function sendJson(res, statusCode, body) {
  const payload = JSON.stringify(body, null, 2);
  res.writeHead(statusCode, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  });
  res.end(payload);
}

function badRequest(message, details) {
  const err = new Error(message);
  err.statusCode = 400;
  err.code = 'bad_request';
  err.details = details;
  return err;
}

function notFound(message = 'not_found') {
  const err = new Error(message);
  err.statusCode = 404;
  err.code = 'not_found';
  return err;
}

function conflict(codeOrMessage, details) {
  const err = new Error(codeOrMessage);
  err.statusCode = 409;
  err.code = /^[a-z0-9_]+$/.test(codeOrMessage) ? codeOrMessage : 'conflict';
  err.details = details;
  return err;
}

async function readJson(req) {
  if (req.method === 'GET' || req.method === 'HEAD') return {};
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 5_000_000) throw badRequest('request_body_too_large');
    chunks.push(chunk);
  }
  if (chunks.length === 0) return {};
  const text = Buffer.concat(chunks).toString('utf8');
  try {
    return JSON.parse(text);
  } catch (err) {
    throw badRequest('invalid_json', { parseError: err.message });
  }
}

function publicRound(round) {
  if (!round) return null;
  return {
    ...round,
    submissionIds: round.submissionIds ?? [],
  };
}

function submissionSummary(sub) {
  return {
    id: sub.id,
    requestId: sub.requestId ?? null,
    roundId: sub.roundId,
    deviceId: sub.deviceId,
    copyId: sub.copyId,
    deliveryId: sub.deliveryId ?? null,
    returnedAt: sub.returnedAt,
    receivedAt: sub.receivedAt,
    factsHash: sub.factsHash,
    extraCardCount: sub.analysis?.extraCardCount ?? 0,
    kpClaims: sub.analysis?.kpClaims ?? {},
  };
}

export function createServer(options = {}) {
  const store = options.store ?? new JsonStore(options.dataFile ?? DEFAULT_DATA_FILE);

  async function handler(req, res) {
    const url = new URL(req.url, 'http://localhost');
    const path = url.pathname.replace(/\/+$/, '') || '/';
    const method = req.method;

    res.setHeader('access-control-allow-origin', '*');
    res.setHeader('access-control-allow-methods', 'GET,POST,PATCH,PUT,DELETE,OPTIONS');
    res.setHeader('access-control-allow-headers', 'content-type,idempotency-key');
    if (method === 'OPTIONS') {
      res.writeHead(204);
      return res.end();
    }

    try {
      if (method === 'GET' && path === '/health') {
        return sendJson(res, 200, {
          ok: true,
          service: 'phonics-review-room',
          persistence: store.filename === ':memory:' ? 'memory' : 'json-file',
          dataFile: store.filename === ':memory:' ? null : store.filename,
          time: new Date().toISOString(),
        });
      }

      if (method === 'GET' && path === '/') {
        return sendJson(res, 200, {
          service: 'Phonics Review Room API',
          docs: '/README.md',
          endpoints: [
            'POST /api/teacher/kps',
            'POST /api/teacher/cards',
            'POST /api/teacher/units',
            'POST /api/teacher/assignments',
            'POST /api/teacher/gates',
            'POST /api/teacher/packs',
            'POST /api/teacher/clock',
            'POST /api/students',
            'GET  /api/students/:id/opportunities?day=8',
            'GET  /api/students/:id/progress',
            'POST /api/rounds/claim',
            'POST /api/receipts',
            'GET  /api/rounds/:id/evidence',
            'POST /api/makeup-orders',
            'POST /api/teacher/rounds/:id/adjudicate',
            'GET  /api/worksheets/:worksheetId',
          ],
        });
      }

      if (method === 'GET' && path === '/api/debug/state') {
        return sendJson(res, 200, store.db);
      }

      // ---------- teacher setup ----------

      if (method === 'POST' && path === '/api/teacher/clock') {
        const body = await readJson(req);
        const day = dayNumber(body.day, null);
        if (day === null) throw badRequest('day_required');
        store.mutate((db) => {
          db.meta.currentDay = day;
        });
        return sendJson(res, 200, store.db.meta);
      }

      if (method === 'POST' && path === '/api/teacher/kps') {
        const body = await readJson(req);
        if (!body.id && !body.kpId) throw badRequest('kp_id_required');
        const kp = {
          id: body.id ?? body.kpId,
          name: body.name ?? body.label ?? body.id ?? body.kpId,
          description: body.description ?? '',
          defaultIndependentRounds: body.independentRounds ?? 2,
          createdAt: new Date().toISOString(),
        };
        if (store.find('kps', kp.id)) throw conflict('kp_id_already_exists', { id: kp.id });
        store.insert('kps', kp);
        return sendJson(res, 201, kp);
      }

      if (method === 'POST' && path === '/api/teacher/cards') {
        const body = await readJson(req);
        const card = {
          id: body.id ?? newId('card'),
          glyph: body.glyph ?? body.text ?? body.id,
          label: body.label ?? body.name ?? '',
          createdAt: new Date().toISOString(),
        };
        if (store.find('cards', card.id)) throw conflict('card_id_already_exists', { id: card.id });
        store.insert('cards', card);
        return sendJson(res, 201, card);
      }

      if (method === 'POST' && path === '/api/teacher/units') {
        const body = await readJson(req);
        const unit = {
          id: body.id ?? newId('unit'),
          name: body.name ?? body.id ?? 'Unit',
          order: body.order ?? body.day ?? null,
          createdAt: new Date().toISOString(),
        };
        if (store.find('units', unit.id)) throw conflict('unit_id_already_exists', { id: unit.id });
        store.insert('units', unit);
        return sendJson(res, 201, unit);
      }

      if (method === 'POST' && path === '/api/teacher/assignments') {
        const body = await readJson(req);
        if (!body.unitId) throw badRequest('unitId_required');
        if (!body.cardId) throw badRequest('cardId_required');
        if (!body.kpId) throw badRequest('kpId_required');
        if (body.expected === undefined || body.expected === null || body.expected === '') {
          throw badRequest('expected_pronunciation_required');
        }
        if (!store.find('cards', body.cardId)) throw notFound(`card_not_found:${body.cardId}`);
        if (!store.find('kps', body.kpId)) throw notFound(`kp_not_found:${body.kpId}`);

        const id = body.id ?? newId('assignment');
        if (store.find('assignments', id)) throw conflict('assignment_id_already_exists', { id });
        const assignment = {
          id,
          unitId: body.unitId,
          cardId: body.cardId,
          kpId: body.kpId,
          expected: String(body.expected),
          hints: normalizeHints(body.hints ?? [], id),
          createdAt: new Date().toISOString(),
        };
        store.insert('assignments', assignment);
        return sendJson(res, 201, assignment);
      }

      if (method === 'POST' && path === '/api/teacher/gates') {
        const body = await readJson(req);
        if (body.opensOnDay === undefined) throw badRequest('opensOnDay_required');
        const gate = {
          id: body.id ?? newId('gate'),
          name: body.name ?? body.id ?? 'Application gate',
          applicationLevelId: body.applicationLevelId ?? body.appLevelId ?? body.id ?? null,
          opensOnDay: Number(body.opensOnDay),
          requirements: (body.requirements ?? []).map((r) =>
            typeof r === 'string'
              ? { kpId: r, independentRounds: body.independentRounds ?? 2 }
              : { independentRounds: 2, ...r },
          ),
          enabled: body.enabled !== false,
          createdAt: new Date().toISOString(),
        };
        if (!Number.isInteger(gate.opensOnDay)) throw badRequest('opensOnDay_must_be_integer_day');
        if (gate.requirements.length === 0) throw badRequest('at_least_one_requirement_required');
        if (store.find('gates', gate.id)) throw conflict('gate_id_already_exists', { id: gate.id });
        store.insert('gates', gate);
        return sendJson(res, 201, gate);
      }

      if (method === 'PATCH' && path.startsWith('/api/teacher/gates/')) {
        const gateId = path.split('/').pop();
        const body = await readJson(req);
        const gate = store.find('gates', gateId);
        if (!gate) throw notFound(`gate_not_found:${gateId}`);
        store.mutate(() => {
          if (body.name !== undefined) gate.name = body.name;
          if (body.opensOnDay !== undefined) gate.opensOnDay = Number(body.opensOnDay);
          if (body.enabled !== undefined) gate.enabled = body.enabled;
          if (body.requirements !== undefined) {
            gate.requirements = body.requirements.map((r) =>
              typeof r === 'string' ? { kpId: r, independentRounds: 2 } : { independentRounds: 2, ...r },
            );
          }
          gate.updatedAt = new Date().toISOString();
          rebuildEvidence(store.db);
        });
        return sendJson(res, 200, gate);
      }

      if (method === 'POST' && path === '/api/teacher/packs') {
        const body = await readJson(req);
        const day = dayNumber(body.day, null);
        if (day === null) throw badRequest('integer_day_required_for_closed_pack');

        const packId = body.id ?? body.packId ?? newId('pack');
        if (store.find('packs', packId)) throw conflict('pack_id_already_exists', { id: packId });

        let assignments = [];
        if (Array.isArray(body.assignmentIds) && body.assignmentIds.length > 0) {
          assignments = body.assignmentIds.map((id) => store.find('assignments', id)).filter(Boolean);
          if (assignments.length !== body.assignmentIds.length) {
            throw badRequest('some_assignmentIds_not_found', { assignmentIds: body.assignmentIds });
          }
        } else if (body.unitId) {
          assignments = store.db.assignments.filter((a) => a.unitId === body.unitId);
        } else {
          throw badRequest('unitId_or_assignmentIds_required');
        }

        if (Array.isArray(body.cardIds) && body.cardIds.length > 0) {
          assignments = assignments.filter((a) => body.cardIds.includes(a.cardId));
        }
        if (assignments.length === 0) throw badRequest('pack_has_no_assignments');

        const items = assignments.map((assignment) =>
          makeItem({ assignment, card: store.find('cards', assignment.cardId) }),
        );

        const pack = {
          id: packId,
          type: 'daily_closed_pack',
          day,
          unitId: body.unitId ?? assignments[0].unitId,
          classId: body.classId ?? 'default',
          title: body.title ?? `Day ${day} phonics pack`,
          worksheetId: body.worksheetId ?? `ws_${packId}`,
          items,
          kpIds: uniqueSorted(items.map((i) => i.kpId)),
          allowsExtraCards: false,
          createdAt: new Date().toISOString(),
        };
        store.insert('packs', pack);
        return sendJson(res, 201, pack);
      }

      // ---------- students ----------

      if (method === 'POST' && path === '/api/students') {
        const body = await readJson(req);
        const student = {
          id: body.id ?? body.studentId ?? newId('student'),
          name: body.name ?? body.id ?? 'Student',
          classId: body.classId ?? 'default',
          classroomMode: body.mode ?? body.classroomMode ?? 'daily_pack',
          practiceDays: (body.practiceDays ?? []).map(Number).filter(Number.isInteger),
          adaptiveRoundSize: body.adaptiveRoundSize ?? 2,
          createdAt: new Date().toISOString(),
        };
        if (store.find('students', student.id)) throw conflict('student_id_already_exists', { id: student.id });
        store.insert('students', student);
        return sendJson(res, 201, student);
      }

      const practiceDaysMatch = path.match(/^\/api\/students\/([^/]+)\/practice-days$/);
      if (method === 'POST' && practiceDaysMatch) {
        const studentId = decodeURIComponent(practiceDaysMatch[1]);
        const student = store.find('students', studentId);
        if (!student) throw notFound(`student_not_found:${studentId}`);
        const body = await readJson(req);
        const days = (body.practiceDays ?? body.days ?? []).map(Number).filter(Number.isInteger);
        store.mutate(() => {
          student.practiceDays = uniqueSorted(days);
        });
        return sendJson(res, 200, student);
      }

      const progressMatch = path.match(/^\/api\/students\/([^/]+)\/progress$/);
      if (method === 'GET' && progressMatch) {
        const studentId = decodeURIComponent(progressMatch[1]);
        const student = store.find('students', studentId);
        if (!student) throw notFound(`student_not_found:${studentId}`);
        const rounds = store.db.rounds
          .filter((r) => r.studentId === studentId)
          .sort((a, b) => (a.roundNo ?? 0) - (b.roundNo ?? 0) || String(a.id).localeCompare(String(b.id)));
        return sendJson(res, 200, {
          student,
          practiceDays: getPracticeDays(store.db, student),
          mastery: Object.values(store.db.masteryByStudentKp ?? {}).filter((m) => m.studentId === studentId),
          evidence: store.db.evidence.filter((e) => e.studentId === studentId),
          rounds: rounds.map(publicRound),
        });
      }

      const opportunitiesMatch = path.match(/^\/api\/students\/([^/]+)\/opportunities$/);
      if (method === 'GET' && opportunitiesMatch) {
        const studentId = decodeURIComponent(opportunitiesMatch[1]);
        const student = store.find('students', studentId);
        if (!student) throw notFound(`student_not_found:${studentId}`);
        const query = { day: url.searchParams.get('day'), gateId: url.searchParams.get('gateId') };
        return sendJson(res, 200, buildOpportunities(store.db, student, query));
      }

      if (method === 'GET' && path === '/api/opportunities') {
        const studentId = url.searchParams.get('studentId');
        const student = store.find('students', studentId);
        if (!student) throw notFound(`student_not_found:${studentId ?? ''}`);
        const query = { day: url.searchParams.get('day'), gateId: url.searchParams.get('gateId') };
        return sendJson(res, 200, buildOpportunities(store.db, student, query));
      }

      const studentMakeupMatch = path.match(/^\/api\/students\/([^/]+)\/makeup-orders$/);
      if (method === 'POST' && studentMakeupMatch) {
        const body = await readJson(req);
        body.studentId = decodeURIComponent(studentMakeupMatch[1]);
        return createMakeupOrder(body);
      }

      if (
        method === 'POST' &&
        (path === '/api/makeup-orders' || path === '/api/practice/makeup-orders' || path === '/api/makeups')
      ) {
        const body = await readJson(req);
        return createMakeupOrder(body);
      }

      async function createMakeupOrder(body) {
        const student = store.find('students', body.studentId);
        if (!student) throw notFound(`student_not_found:${body.studentId}`);
        if (!body.kpId) throw badRequest('kpId_required');
        if (!store.find('kps', body.kpId)) throw notFound(`kp_not_found:${body.kpId}`);

        if (body.conflictRoundId) {
          const round = store.find('rounds', body.conflictRoundId);
          if (!round || round.studentId !== student.id) throw notFound('conflict_round_not_found');
        }

        const order = {
          id: body.id ?? newId('makeup'),
          studentId: student.id,
          kpId: body.kpId,
          gateId: body.gateId ?? null,
          conflictRoundId: body.conflictRoundId ?? null,
          reason: body.reason ?? (body.conflictRoundId ? 'device_evidence_conflict' : 'missing_independent_round'),
          requestedDay: dayNumber(body.day ?? body.requestedDay, null),
          status: 'open',
          attempts: 0,
          roundId: null,
          createdAt: new Date().toISOString(),
        };
        store.insert('makeupOrders', order);
        return sendJson(res, 201, order);
      }

      // ---------- claim practice ----------

      if (
        method === 'POST' &&
        (path === '/api/rounds/claim' ||
          path === '/api/practice/claim' ||
          path === '/api/practice/start' ||
          path === '/api/claim')
      ) {
        const body = await readJson(req);
        const student = store.find('students', body.studentId);
        if (!student) throw notFound(`student_not_found:${body.studentId}`);
        const deviceId = body.deviceId ?? null;

        // Makeup rounds are server-issued adaptive rounds.
        if (body.makeupOrderId) {
          const order = store.find('makeupOrders', body.makeupOrderId);
          if (!order || order.studentId !== student.id) throw notFound('makeup_order_not_found');
          if (order.status === 'claimed' && order.roundId) {
            const existing = store.find('rounds', order.roundId);
            if (existing) {
              if (deviceId && !existing.deviceIds.includes(deviceId)) existing.deviceIds.push(deviceId);
              store.save();
              return sendJson(res, 200, { replayed: true, makeupOrder: order, round: publicRound(existing) });
            }
          }
          if (order.status === 'completed' && order.roundId) {
            const existing = store.find('rounds', order.roundId);
            return sendJson(res, 200, { alreadyCompleted: true, makeupOrder: order, round: publicRound(existing) });
          }

          const day = dayNumber(body.day, order.requestedDay);
          const practiceDays = getPracticeDays(store.db, student);
          if (day === null) throw badRequest('day_required_for_makeup_round');
          if (practiceDays.length > 0 && !practiceDays.includes(day)) {
            throw badRequest('day_not_practicable', { day, practiceDays });
          }

          const selection = selectAdaptiveItems(store.db, {
            student,
            focusKpIds: [order.kpId],
            count: body.count ?? 1,
          });
          if (selection.items.length === 0) throw conflict('no_cards_available_for_makeup', { kpId: order.kpId });

          const roundId = body.roundId ?? `r_${student.id}_makeup_${order.id.replace(/^makeup_/, '')}`;
          if (store.find('rounds', roundId)) throw conflict('round_id_already_exists', { roundId });
          const round = {
            id: roundId,
            studentId: student.id,
            mode: 'adaptive',
            purpose: 'makeup',
            makeupOrderId: order.id,
            packId: null,
            worksheetId: body.worksheetId ?? `ws_${roundId}`,
            day,
            roundNo: store.db.rounds.filter((r) => r.studentId === student.id).length + 1,
            eligibleKpIds: selection.kpIds,
            focusKpIds: selection.focusKpIds,
            items: selection.items,
            status: 'issued',
            deviceIds: deviceId ? [deviceId] : [],
            submissionIds: [],
            createdAt: new Date().toISOString(),
          };

          store.mutate((db) => {
            db.rounds.push(round);
            order.status = 'claimed';
            order.roundId = round.id;
            order.attempts += 1;
            order.lastClaimedAt = new Date().toISOString();
          });
          return sendJson(res, 201, { replayed: false, makeupOrder: order, round: publicRound(round) });
        }

        // Daily closed pack: student+pack is the canonical learning round.
        if (body.packId) {
          const pack = store.find('packs', body.packId);
          if (!pack) throw notFound(`pack_not_found:${body.packId}`);

          let round =
            store.db.rounds.find((r) => r.studentId === student.id && r.packId === pack.id) ??
            (body.roundId ? store.find('rounds', body.roundId) : null);

          if (round) {
            if (round.studentId !== student.id) throw conflict('round_belongs_to_another_student');
            if (deviceId && !round.deviceIds.includes(deviceId)) {
              store.mutate(() => round.deviceIds.push(deviceId));
            }
            return sendJson(res, 200, { replayed: true, round: publicRound(round), pack });
          }

          const roundId = body.roundId ?? `r_${student.id}_${pack.id}`;
          if (store.find('rounds', roundId)) throw conflict('round_id_already_exists', { roundId });
          round = {
            id: roundId,
            studentId: student.id,
            mode: 'daily_pack',
            purpose: 'daily_practice',
            packId: pack.id,
            worksheetId: pack.worksheetId,
            day: pack.day,
            roundNo: store.db.rounds.filter((r) => r.studentId === student.id).length + 1,
            eligibleKpIds: pack.kpIds,
            items: pack.items,
            status: 'issued',
            deviceIds: deviceId ? [deviceId] : [],
            submissionIds: [],
            createdAt: new Date().toISOString(),
          };
          store.insert('rounds', round);
          return sendJson(res, 201, { replayed: false, round: publicRound(round), pack });
        }

        // Ordinary adaptive claim. The previous server-known round must be
        // confirmed before the server chooses the next real worksheet.
        if (body.mode === 'adaptive' || student.classroomMode === 'adaptive') {
          const mine = store.db.rounds
            .filter((r) => r.studentId === student.id && r.mode === 'adaptive')
            .sort((a, b) => (a.roundNo ?? 0) - (b.roundNo ?? 0));
          const latest = mine[mine.length - 1] ?? null;

          if (latest?.status === 'issued') {
            if (deviceId && !latest.deviceIds.includes(deviceId)) {
              store.mutate(() => latest.deviceIds.push(deviceId));
            }
            throw conflict('previous_round_outstanding', {
              message: 'Submit the already-issued round before claiming a new adaptive round.',
              round: publicRound(latest),
            });
          }
          if (latest?.status === 'conflicted') {
            const orders = store.db.makeupOrders.filter(
              (o) => o.studentId === student.id && o.conflictRoundId === latest.id && o.status === 'open',
            );
            throw conflict('previous_round_conflicted', {
              message: 'The previous round has conflicting device evidence. Claim a server-issued makeup round.',
              round: publicRound(latest),
              makeupOrders: orders,
            });
          }

          const day = dayNumber(body.day, store.db.meta.currentDay);
          if (day === null) throw badRequest('day_required_for_adaptive_round');
          const selection = selectAdaptiveItems(store.db, {
            student,
            previousRound: latest,
            focusKpIds: body.focusKpIds ?? [],
            count: body.count ?? student.adaptiveRoundSize ?? 2,
          });
          if (selection.items.length === 0) throw conflict('no_assignments_available');

          const roundNo = (latest?.roundNo ?? 0) + 1;
          const roundId = body.roundId ?? `r_${student.id}_adaptive_${roundNo}`;
          if (store.find('rounds', roundId)) throw conflict('round_id_already_exists', { roundId });
          const round = {
            id: roundId,
            studentId: student.id,
            mode: 'adaptive',
            purpose: 'adaptive_practice',
            packId: null,
            worksheetId: body.worksheetId ?? `ws_${roundId}`,
            day,
            roundNo,
            eligibleKpIds: selection.kpIds,
            focusKpIds: selection.focusKpIds,
            items: selection.items,
            status: 'issued',
            deviceIds: deviceId ? [deviceId] : [],
            submissionIds: [],
            createdAt: new Date().toISOString(),
          };
          store.insert('rounds', round);
          return sendJson(res, 201, { replayed: false, round: publicRound(round) });
        }

        throw badRequest('packId_required_for_daily_pack_claim_or_mode_adaptive');
      }

      // ---------- receipts / delayed return ----------

      if (
        method === 'POST' &&
        ['/api/receipts', '/api/receipt', '/api/practice/receipts', '/api/practice/receipt'].includes(path)
      ) {
        const body = await readJson(req);
        const requestId = body.requestId ?? req.headers['idempotency-key'] ?? null;
        const requestHash = canonicalHash({ ...body, requestId: undefined });

        if (requestId && store.db.requestLog[requestId]) {
          const prior = store.db.requestLog[requestId];
          if (prior.hash !== requestHash) {
            throw conflict('idempotency_key_reused_with_different_body', { requestId });
          }
          return sendJson(res, prior.statusCode ?? 200, { ...prior.body, idempotentReplay: true });
        }

        const studentId = body.studentId;
        const student = store.find('students', studentId);
        if (!student) throw notFound(`student_not_found:${studentId}`);

        let round = body.roundId ? store.find('rounds', body.roundId) : null;
        const worksheetId = body.worksheetId ?? body.worksheet;

        if (!round && body.packId) {
          round = store.db.rounds.find((r) => r.studentId === studentId && r.packId === body.packId) ?? null;
        }

        if (!round && worksheetId) {
          round =
            store.db.rounds.find(
              (r) => r.studentId === studentId && r.worksheetId === worksheetId,
            ) ?? null;

          // A valid daily pack worksheet is server-known. This only materializes
          // the canonical round; it never trusts client-supplied card content.
          if (!round) {
            const pack = store.db.packs.find((p) => p.worksheetId === worksheetId);
            if (pack) {
              const roundId = `r_${studentId}_${pack.id}`;
              round = {
                id: roundId,
                studentId,
                mode: 'daily_pack',
                purpose: 'daily_practice_auto_claimed_from_receipt',
                packId: pack.id,
                worksheetId: pack.worksheetId,
                day: pack.day,
                roundNo: store.db.rounds.filter((r) => r.studentId === studentId).length + 1,
                eligibleKpIds: pack.kpIds,
                items: pack.items,
                status: 'issued',
                deviceIds: body.deviceId ? [body.deviceId] : [],
                submissionIds: [],
                createdAt: new Date().toISOString(),
              };
              store.db.rounds.push(round);
            }
          }
        }

        if (!round) {
          throw conflict('unknown_round_or_worksheet', {
            message: 'The server did not issue this round/worksheet; a disconnected client cannot invent one later.',
            roundId: body.roundId ?? null,
            packId: body.packId ?? null,
            worksheetId: worksheetId ?? null,
          });
        }
        if (round.studentId !== studentId) throw conflict('round_student_mismatch');
        if (worksheetId && worksheetId !== round.worksheetId) {
          throw conflict('worksheet_does_not_match_round', {
            expected: round.worksheetId,
            received: worksheetId,
          });
        }

        const analysis = analyzeReceipt(round, body);
        const existingExact = store.db.submissions.find(
          (s) => s.roundId === round.id && s.factsHash === analysis.factsHash,
        );
        if (existingExact) {
          if (body.deviceId && !round.deviceIds.includes(body.deviceId)) round.deviceIds.push(body.deviceId);
          const submissions = store.db.submissions.filter((s) => s.roundId === round.id);
          const reconciliation = reconcileRound(round, submissions);
          round.reconciliation = reconciliation;
          Object.assign(round, reconciliation);
          rebuildEvidence(store.db);
          store.save();
          const responseBody = {
            duplicate: true,
            countedAs: 'same_learning_round_only_once',
            existingSubmission: submissionSummary(existingExact),
            round: publicRound(round),
          };
          if (requestId) {
            store.mutate((db) => {
              db.requestLog[requestId] = { hash: requestHash, statusCode: 200, body: responseBody };
            });
          }
          return sendJson(res, 200, responseBody);
        }

        const now = new Date().toISOString();
        const submission = {
          id: body.submissionId ?? newId('sub'),
          requestId,
          roundId: round.id,
          studentId,
          worksheetId: round.worksheetId,
          deviceId: body.deviceId ?? null,
          copyId: body.copyId ?? body.clientCopyId ?? null,
          deliveryId: body.deliveryId ?? null,
          reprint: body.reprint === true,
          returnedAt: body.returnedAt ?? now,
          receivedAt: now,
          factsHash: analysis.factsHash,
          analysis,
          receipt: body,
        };

        const responseBody = store.mutate((db) => {
          db.submissions.push(submission);
          if (body.deviceId && !round.deviceIds.includes(body.deviceId)) round.deviceIds.push(body.deviceId);
          round.submissionIds = uniqueSorted([...(round.submissionIds ?? []), submission.id]);
          round.updatedAt = now;

          const submissions = db.submissions.filter((s) => s.roundId === round.id);
          const reconciliation = reconcileRound(round, submissions);
          round.reconciliation = reconciliation;
          Object.assign(round, reconciliation);
          if (round.status === 'confirmed') {
            round.firstConfirmedAt = round.firstConfirmedAt ?? now;
            round.confirmedAt = now;
          }

          for (const kpId of reconciliation.repairKpIds) {
            const exists = db.makeupOrders.find(
              (o) => o.studentId === studentId && o.conflictRoundId === round.id && o.kpId === kpId && o.status === 'open',
            );
            if (!exists) {
              db.makeupOrders.push({
                id: newId('makeup'),
                studentId,
                kpId,
                gateId: null,
                conflictRoundId: round.id,
                reason: 'device_evidence_conflict',
                requestedDay: null,
                status: 'open',
                attempts: 0,
                roundId: null,
                createdAt: now,
              });
            }
          }

          if (round.makeupOrderId) {
            const order = db.makeupOrders.find((o) => o.id === round.makeupOrderId);
            const targetKp = order?.kpId;
            const state = targetKp ? reconciliation.kpResults[targetKp]?.state : null;
            if (order && state === 'independent') {
              order.status = 'completed';
              order.completedRoundId = round.id;
              order.completedAt = now;
            } else if (order) {
              order.status = 'open';
              order.lastRoundId = round.id;
              order.lastAttemptAt = now;
            }
          }

          rebuildEvidence(db);

          const result = {
            duplicate: false,
            submission: submissionSummary(submission),
            round: publicRound(round),
          };
          if (requestId) db.requestLog[requestId] = { hash: requestHash, statusCode: 201, body: result };
          return result;
        });

        return sendJson(res, 201, responseBody);
      }

      // ---------- evidence explanation ----------

      const evidenceMatch = path.match(/^\/api\/rounds\/([^/]+)\/evidence$/);
      if (method === 'GET' && evidenceMatch) {
        const roundId = decodeURIComponent(evidenceMatch[1]);
        const round = store.find('rounds', roundId);
        if (!round) throw notFound(`round_not_found:${roundId}`);
        if (url.searchParams.get('studentId') && round.studentId !== url.searchParams.get('studentId')) {
          throw conflict('round_student_mismatch');
        }
        const submissions = store.db.submissions.filter((s) => s.roundId === roundId);
        const evidence = store.db.evidence.filter((e) => e.roundId === roundId);
        return sendJson(res, 200, {
          roundId: round.id,
          studentId: round.studentId,
          day: round.day,
          worksheetId: round.worksheetId,
          identityRule: 'One canonical round/worksheet is one review, even when reprinted or returned by two devices.',
          status: round.status,
          deviceIds: uniqueSorted(submissions.map((s) => s.deviceId).filter(Boolean)),
          copies: submissions.map(submissionSummary),
          answerTimeline: submissions.map((s) => ({
            submissionId: s.id,
            deviceId: s.deviceId,
            copyId: s.copyId,
            answers: s.analysis?.answers ?? [],
          })),
          reconciliation: round.reconciliation ?? null,
          ledgerEvidence: evidence,
          sufficiency: Object.fromEntries(
            Object.entries(round.reconciliation?.kpResults ?? {}).map(([kpId, result]) => {
              const ledger = evidence.find((e) => e.kpId === kpId);
              return [
                kpId,
                {
                  oneRoundCredit: result.state === 'independent',
                  state: result.state,
                  reasons: result.reasons,
                  dailyLedger: ledger
                    ? {
                        selected: ledger.selected,
                        countsTowardMastery: ledger.countsTowardMastery,
                        reason: ledger.reason,
                        day: ledger.day,
                      }
                    : null,
                  requiredForGate: 'A gate additionally needs the required number of distinct rounds/days.',
                },
              ];
            }),
          ),
          makeupOrders: store.db.makeupOrders.filter((o) => o.conflictRoundId === roundId),
        });
      }

      // ---------- teacher adjudication ----------

      const adjudicateMatch = path.match(/^\/api\/teacher\/rounds\/([^/]+)\/adjudicate$/);
      if (method === 'POST' && adjudicateMatch) {
        const roundId = decodeURIComponent(adjudicateMatch[1]);
        const body = await readJson(req);
        const round = store.find('rounds', roundId);
        if (!round) throw notFound(`round_not_found:${roundId}`);

        if (body.decision === 'repair' || !body.submissionId) {
          const kpIds = body.kpIds ?? round.reconciliation?.repairKpIds ?? round.eligibleKpIds;
          const orders = [];
          store.mutate((db) => {
            for (const kpId of kpIds) {
              let order = db.makeupOrders.find(
                (o) => o.studentId === round.studentId && o.conflictRoundId === round.id && o.kpId === kpId,
              );
              if (!order) {
                order = {
                  id: newId('makeup'),
                  studentId: round.studentId,
                  kpId,
                  gateId: body.gateId ?? null,
                  conflictRoundId: round.id,
                  reason: 'teacher_requested_repair',
                  requestedDay: body.day ?? null,
                  status: 'open',
                  attempts: 0,
                  roundId: null,
                  createdAt: new Date().toISOString(),
                };
                db.makeupOrders.push(order);
              } else if (order.status === 'completed') {
                order.status = 'open';
              }
              orders.push(order);
            }
          });
          return sendJson(res, 200, { decision: 'repair', makeupOrders: orders, round: publicRound(round) });
        }

        const submission = store.db.submissions.find((s) => s.id === body.submissionId && s.roundId === roundId);
        if (!submission) throw notFound(`submission_not_found_in_round:${body.submissionId}`);

        store.mutate((db) => {
          round.adjudication = {
            submissionId: submission.id,
            teacherId: body.teacherId ?? 'teacher',
            note: body.note ?? '',
            adjudicatedAt: new Date().toISOString(),
          };
          const submissions = db.submissions.filter((s) => s.roundId === round.id);
          const reconciliation = reconcileRound(round, submissions);
          round.reconciliation = reconciliation;
          Object.assign(round, reconciliation);

          for (const order of db.makeupOrders.filter(
            (o) => o.conflictRoundId === round.id && (o.status === 'open' || o.status === 'claimed'),
          )) {
            if (reconciliation.kpResults[order.kpId]?.state === 'independent') {
              order.status = 'superseded_by_adjudication';
              order.closedAt = new Date().toISOString();
              order.adjudicationSubmissionId = submission.id;
            }
          }

          rebuildEvidence(db);
        });
        return sendJson(res, 200, { decision: 'use_submission', round: publicRound(round) });
      }

      // ---------- worksheets ----------

      const worksheetMatch = path.match(/^\/api\/worksheets\/([^/]+)$/);
      if (method === 'GET' && worksheetMatch) {
        const worksheetId = decodeURIComponent(worksheetMatch[1]);
        const pack = store.db.packs.find((p) => p.worksheetId === worksheetId);
        if (pack) {
          return sendJson(res, 200, { kind: 'daily_closed_pack', ...pack });
        }
        const round = store.db.rounds.find((r) => r.worksheetId === worksheetId);
        if (round) {
          return sendJson(res, 200, {
            kind: round.mode === 'adaptive' ? 'adaptive_round_worksheet' : 'round_worksheet',
            ...publicRound(round),
          });
        }
        throw notFound(`worksheet_not_found:${worksheetId}`);
      }

      // Convenience gate access check: GET /api/gates/:id/access?studentId=&day=8
      const gateAccessMatch = path.match(/^\/api\/gates\/([^/]+)\/access$/);
      if (method === 'GET' && gateAccessMatch) {
        const gateId = decodeURIComponent(gateAccessMatch[1]);
        const gate = store.find('gates', gateId);
        if (!gate) throw notFound(`gate_not_found:${gateId}`);
        const studentId = url.searchParams.get('studentId');
        const student = store.find('students', studentId);
        if (!student) throw notFound(`student_not_found:${studentId}`);
        return sendJson(
          res,
          200,
          evaluateGate(store.db, student, gate, url.searchParams.get('day') ?? undefined),
        );
      }

      if (method === 'GET' && path === '/api/rounds') {
        const studentId = url.searchParams.get('studentId');
        let rounds = store.db.rounds;
        if (studentId) rounds = rounds.filter((r) => r.studentId === studentId);
        return sendJson(res, 200, rounds.map(publicRound));
      }

      const roundMatch = path.match(/^\/api\/rounds\/([^/]+)$/);
      if (method === 'GET' && roundMatch) {
        const round = store.find('rounds', decodeURIComponent(roundMatch[1]));
        if (!round) throw notFound('round_not_found');
        return sendJson(res, 200, publicRound(round));
      }

      return sendJson(res, 404, { error: { code: 'route_not_found', message: `${method} ${path}` } });
    } catch (err) {
      const statusCode = err.statusCode ?? 500;
      if (statusCode >= 500) console.error(err);
      return sendJson(res, statusCode, {
        error: {
          code: err.code ?? 'internal_error',
          message: err.message,
          ...(err.details ? { details: err.details } : {}),
        },
      });
    }
  }

  const server = http.createServer(handler);
  return { server, store };
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const port = Number(process.env.PORT ?? 3000);
  const host = process.env.HOST ?? '0.0.0.0';
  const dataFile = process.env.DATA_FILE ?? DEFAULT_DATA_FILE;
  const { server } = createServer({ dataFile });
  server.listen(port, host, () => {
    console.log(`Phonics Review Room listening on http://${host}:${port}`);
    console.log(`Data file: ${dataFile === ':memory:' ? 'memory' : dataFile}`);
  });
}
