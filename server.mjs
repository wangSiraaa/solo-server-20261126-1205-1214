import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { readFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';

import { JsonStore } from './lib/store.mjs';
import {
  AGREEMENTS,
  HINTS,
  PROTOTYPES,
  DomainError,
  assessEligibility,
  availableActions,
  builtInTasks,
  evaluateCompletion,
  initialPrototypeState,
  nowIso,
  planPackage,
  terminalView,
  transition,
} from './lib/domain.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(__dirname, 'public');
const DATA_FILE = process.env.DATA_FILE
  ? path.resolve(process.env.DATA_FILE)
  : path.join(__dirname, 'data', 'local-db.json');
const HOST = process.env.HOST ?? '127.0.0.1';
const PORT = Number(process.env.PORT ?? 4173);

const store = new JsonStore(DATA_FILE);

function defaultData() {
  const createdAt = nowIso();
  return {
    meta: { schemaVersion: 1, createdAt, dataFile: DATA_FILE },
    testers: [],
    tasks: builtInTasks(),
    agreements: structuredClone(AGREEMENTS),
    hints: structuredClone(HINTS),
    packages: [],
    invitations: [],
    claims: [],
    sessions: [],
    exposures: [],
    demonstrations: [],
    completions: [],
    reviews: [],
    settlements: [],
    payouts: [],
    auditEvents: [],
    idempotencyRecords: [],
  };
}

await store.load(defaultData);

/* --------------------------------- helpers -------------------------------- */

function json(res, status, body) {
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  });
  res.end(JSON.stringify(body, null, 2));
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (!chunks.length) return {};
  const raw = Buffer.concat(chunks).toString('utf8');
  try {
    return JSON.parse(raw);
  } catch {
    throw new DomainError('INVALID_JSON', '请求体不是合法 JSON');
  }
}

function shortId(prefix) {
  return `${prefix}_${randomUUID().replace(/-/g, '').slice(0, 12)}`;
}

function receiptNo() {
  return `RC-${Date.now().toString(36).toUpperCase()}-${randomUUID().slice(0, 6).toUpperCase()}`;
}

function addAudit(db, type, payload = {}) {
  const event = { id: shortId('aud'), at: nowIso(), type, ...payload };
  db.auditEvents.push(event);
  if (db.auditEvents.length > 1000) db.auditEvents.splice(0, db.auditEvents.length - 1000);
  return event;
}

function findOrCreateTester(db, testerId, displayName) {
  let tester = db.testers.find((item) => item.id === testerId);
  if (!tester) {
    tester = {
      id: testerId || shortId('tester'),
      displayName: displayName || testerId || '匿名试用员',
      createdAt: nowIso(),
    };
    db.testers.push(tester);
  }
  return tester;
}

function agreementSnapshotForTask(db, task, acceptedAt = nowIso(), agreementId = task.agreementId) {
  const agreement = task.agreement ?? db.agreements[agreementId] ?? AGREEMENTS[agreementId];
  if (!agreement) throw new DomainError('AGREEMENT_NOT_FOUND', '任务缺少事前计件约定');
  return {
    agreementId: agreement.id,
    version: agreement.version ?? 1,
    title: agreement.title,
    kind: agreement.kind ?? 'self-service-discovery',
    currency: agreement.currency ?? 'points',
    unitPrice: agreement.unitPrice,
    terms: agreement.terms ?? [],
    acceptedAt,
  };
}

function createInvitation(db, { testerId, task, body = {}, packageId = null, dispatchedAt = nowIso() }) {
  if (!testerId) throw new DomainError('TESTER_REQUIRED', '请填写试用员身份 ID');
  findOrCreateTester(db, testerId, body.displayName);
  const requestedAgreementId = body.agreementId ?? task.agreementId;
  if (
    requestedAgreementId !== task.agreementId &&
    requestedAgreementId !== task.knownGestureAgreementId
  ) {
    throw new DomainError('AGREEMENT_NOT_ALLOWED_FOR_TASK', '不能在派出时临时套用未声明的计件约定');
  }
  const agreementSnapshot = agreementSnapshotForTask(db, task, dispatchedAt, requestedAgreementId);
  const history = buildTesterHistory(db, testerId, { beforeIso: dispatchedAt });
  const eligibilityPreview = assessEligibility(task, history, dispatchedAt);
  const invitation = {
    id: shortId('inv'),
    invitationNo: shortId('INV'),
    testerId,
    taskId: task.id,
    taskVersion: task.version,
    taskSnapshot: structuredClone(task),
    packageId,
    dispatchBatchId: body.batchId ?? null,
    channel: body.channel ?? 'local-recruiter-console',
    deviceId: body.deviceId ?? null,
    status: 'dispatched',
    dispatchedAt,
    agreementSnapshot,
    eligibilityPreview,
  };
  db.invitations.push(invitation);
  addAudit(db, 'invitation.dispatched', {
    invitationId: invitation.id,
    testerId,
    taskId: task.id,
    packageId,
    eligibilityPreview,
  });
  return invitation;
}

function buildTesterHistory(db, testerId, { beforeIso } = {}) {
  const exposures = db.exposures.filter(
    (item) => item.testerId === testerId && (!beforeIso || item.shownAt < beforeIso),
  );
  const taught = db.demonstrations.filter(
    (item) => item.testerId === testerId && (!beforeIso || item.createdAt < beforeIso),
  );

  const knownKeys = new Set();
  for (const completion of db.completions) {
    if (completion.testerId !== testerId) continue;
    if (beforeIso && completion.submittedAt >= beforeIso) continue;
    if (!completion.verdict?.behaviorPassed) continue;
    const task = db.tasks.find((item) => item.id === completion.taskId);
    for (const key of task?.teachesKeys ?? []) knownKeys.add(key);
  }
  for (const exposure of exposures) for (const key of exposure.reveals ?? []) knownKeys.add(key);
  for (const item of taught) for (const key of item.taughtKeys ?? []) knownKeys.add(key);

  return { testerId, exposures, taught, knownKeys: [...knownKeys] };
}

function eligibilityForClaim(db, claim, atIso = nowIso()) {
  const task = claim.taskSnapshot;
  const history = buildTesterHistory(db, claim.testerId, { beforeIso: claim.acceptedAt });
  const currentExposures = db.exposures.filter(
    (item) => item.claimId === claim.id && item.shownAt <= atIso,
  );
  history.exposures.push(...currentExposures);
  return assessEligibility(task, history, atIso);
}

function getClaimOrThrow(db, claimId) {
  const claim = db.claims.find((item) => item.id === claimId || item.receiptNo === claimId);
  if (!claim) throw new DomainError('CLAIM_NOT_FOUND', '找不到受理单/领取回条', { claimId });
  return claim;
}

function getSessionView(db, session) {
  const claim = getClaimOrThrow(db, session.claimId);
  const prototype = PROTOTYPES[session.prototypeId];
  if (!prototype) throw new DomainError('UNKNOWN_PROTOTYPE', session.prototypeId);
  const eligibility = eligibilityForClaim(db, claim);
  return terminalView({ task: claim.taskSnapshot, prototype, claim, session, eligibility });
}

function claimReceipt(db, claim) {
  return {
    receiptNo: claim.receiptNo,
    claimId: claim.id,
    invitationId: claim.invitationId,
    testerId: claim.testerId,
    taskId: claim.taskId,
    taskTitle: claim.taskSnapshot.title,
    prototype: { id: claim.prototypeId, version: claim.prototypeVersion },
    agreementSnapshot: claim.agreementSnapshot,
    acceptedAt: claim.acceptedAt,
    eligibilityAtAcceptance: claim.eligibilityAtAcceptance,
    devices: claim.devices,
    hintExposures: db.exposures.filter((item) => item.claimId === claim.id),
    settlement: db.settlements.find((item) => item.claimId === claim.id) ?? null,
    payout: db.payouts.find((item) => item.claimId === claim.id) ?? null,
  };
}

async function replayIdempotent(req, res, key, handler) {
  if (key) {
    const existing = store.data.idempotencyRecords.find((item) => item.key === key);
    if (existing) {
      res.setHeader('Idempotent-Replay', 'true');
      return json(res, existing.status, existing.body);
    }
  }

  const result = await handler();
  if (key && result?.status && result?.body) {
    await store.mutate((db) => {
      if (!db.idempotencyRecords.some((item) => item.key === key)) {
        db.idempotencyRecords.push({ key, ...result, createdAt: nowIso() });
      }
    });
  }
  return json(res, result.status, result.body);
}

/* --------------------------------- API ------------------------------------ */

async function handleApi(req, res, url) {
  const parts = url.pathname.split('/').filter(Boolean); // api, ...
  const method = req.method;
  const body = ['POST', 'PUT', 'PATCH'].includes(method) ? await readJson(req) : {};

  if (method === 'GET' && url.pathname === '/api/health') {
    return json(res, 200, { ok: true, service: 'trial-acceptance', time: nowIso(), dataFile: DATA_FILE });
  }

  if (method === 'GET' && url.pathname === '/api/config') {
    return json(res, 200, {
      tasks: store.data.tasks,
      agreements: store.data.agreements,
      hints: store.data.hints,
      prototype: PROTOTYPES['books-v1'],
    });
  }

  if (method === 'GET' && url.pathname === '/api/tasks') {
    return json(res, 200, store.data.tasks);
  }

  if (method === 'POST' && url.pathname === '/api/tasks') {
    const result = await store.mutate((db) => {
      const targetBookId = body.targetBookId ?? 'book-blue';
      const targetName = PROTOTYPES['books-v1'].books.find((b) => b.id === targetBookId)?.name;
      if (!targetName) throw new DomainError('UNKNOWN_BOOK', '目标册子不在原型中');
      const id = body.id ?? shortId('task');
      const normalizeList = (value) => {
        if (Array.isArray(value)) return value;
        if (typeof value === 'string' && value.trim()) return value.split(',').map((item) => item.trim()).filter(Boolean);
        return undefined;
      };
      const unitPrice = Number(body.unitPrice ?? 3);
      if (!Number.isFinite(unitPrice) || unitPrice < 0) {
        throw new DomainError('BAD_RATE', '计件单价必须是非负数字');
      }
      const agreement = {
        id: `${id}-rate`,
        version: 1,
        title: body.agreementTitle ?? `单价 ${unitPrice} 点的自助计件约定`,
        currency: body.currency ?? 'points',
        unitPrice,
        acceptedBeforeWork: true,
        terms: body.terms ?? ['按登记任务书在签发原型上完成精确轨迹；重复完成只结算一次。'],
      };
      db.agreements[id] = agreement;
      const task = {
        id,
        version: 1,
        clientName: body.clientName ?? '本地委托人',
        title: body.title ?? `把${targetName}加入收藏后再移出`,
        brief: body.brief ?? '从空收藏夹开始，独立完成收藏与取消收藏。',
        prototypeId: 'books-v1',
        agreementId: agreement.id,
        agreement,
        mode: 'unmoderated',
        initialScreenId: 'favorites-empty',
        initialState: { favorites: [], loanFlags: [], flags: {} },
        steps: [
          { ordinal: 1, verb: 'collect', targetType: 'book', targetId, label: `收藏${targetName}` },
          { ordinal: 2, verb: 'uncollect', targetType: 'book', targetId, label: `取消收藏${targetName}` },
        ],
        requiredDiscoveryKeys: normalizeList(body.requiredDiscoveryKeys) ?? [
          'gesture:enter-library',
          'gesture:favorite-toggle',
          `binding:collect-${targetBookId.replace('book-', '')}`,
          `binding:uncollect-${targetBookId.replace('book-', '')}`,
        ],
        teachesKeys: normalizeList(body.teachesKeys) ?? [
          'gesture:enter-library',
          'gesture:favorite-toggle',
          `binding:collect-${targetBookId.replace('book-', '')}`,
          `binding:uncollect-${targetBookId.replace('book-', '')}`,
        ],
        exclusionHintIds: normalizeList(body.exclusionHintIds) ?? [],
        allowedHintIds: normalizeList(body.allowedHintIds) ?? ['hint-safe-scroll'],
        finalState: { favorites: [] },
        registeredAt: nowIso(),
      };
      db.tasks.push(task);
      addAudit(db, 'task.registered', { taskId: task.id, title: task.title, unitPrice });
      return { status: 201, body: task };
    });
    return json(res, result.status, result.body);
  }

  if (method === 'POST' && url.pathname === '/api/testers') {
    const result = await store.mutate((db) => {
      const tester = findOrCreateTester(db, body.id, body.displayName);
      addAudit(db, 'tester.registered', { testerId: tester.id });
      return { status: 200, body: tester };
    });
    return json(res, result.status, result.body);
  }

  if (method === 'GET' && parts[1] === 'testers' && parts[3] === 'receipts') {
    const testerId = parts[2];
    const claims = store.data.claims.filter((claim) => claim.testerId === testerId);
    return json(res, 200, {
      testerId,
      tester: store.data.testers.find((t) => t.id === testerId) ?? null,
      history: buildTesterHistory(store.data, testerId),
      receipts: claims.map((claim) => claimReceipt(store.data, claim)),
    });
  }

  if (method === 'GET' && parts[1] === 'testers' && parts[3] === 'profile') {
    const testerId = parts[2];
    return json(res, 200, {
      tester: store.data.testers.find((t) => t.id === testerId) ?? null,
      history: buildTesterHistory(store.data, testerId),
    });
  }

  if (method === 'POST' && url.pathname === '/api/packages/plan') {
    const taskIds = body.taskIds ?? [];
    const tasks = store.data.tasks;
    return json(res, 200, planPackage(tasks, taskIds));
  }

  if (method === 'POST' && url.pathname === '/api/packages') {
    const result = await store.mutate((db) => {
      const taskIds = body.taskIds ?? [];
      const plan = planPackage(db.tasks, taskIds);
      if (!plan.safeForOneUnmoderatedPackage && !body.allowConflicts) {
        throw new DomainError('PACKAGE_SPOILER_CONFLICT', '任务之间存在提前泄题冲突', { plan });
      }
      const pkg = {
        id: body.id ?? shortId('pkg'),
        title: body.title ?? '无主持密封任务包',
        taskIds,
        plan,
        sealed: true,
        hintsSealed: true,
        createdAt: nowIso(),
      };
      db.packages.push(pkg);
      addAudit(db, 'package.sealed', { packageId: pkg.id, taskIds });
      return { status: 201, body: pkg };
    });
    return json(res, result.status, result.body);
  }

  if (method === 'GET' && url.pathname === '/api/packages') {
    return json(res, 200, store.data.packages);
  }

  if (method === 'POST' && url.pathname === '/api/demonstrations') {
    const result = await store.mutate((db) => {
      const task = db.tasks.find((item) => item.id === body.taskId);
      if (!task) throw new DomainError('TASK_NOT_FOUND', '任务不存在');
      const prototype = PROTOTYPES[task.prototypeId];
      let state = structuredClone(task.initialState ?? initialPrototypeState(task.prototypeId));
      let screenId = task.initialScreenId ?? prototype.initialScreenId;
      const events = [];
      for (const [index, item] of (body.actions ?? []).entries()) {
        const result = transition(prototype, state, screenId, {
          actionId: item.actionId,
          targetId: item.targetId ?? item.targetBookId,
          deviceId: item.deviceId ?? 'host-proxy',
          actorRole: 'host',
          provenance: 'host-proxy',
        });
        state = result.state;
        screenId = result.screenId;
        events.push({ ...result.event, seq: index + 1 });
      }
      const demonstration = {
        id: shortId('demo'),
        taskId: task.id,
        testerId: body.testerId ?? null,
        directlyTaught: body.directlyTaught === true,
        taughtKeys: body.directlyTaught === true ? task.requiredDiscoveryKeys ?? [] : [],
        prototypeId: prototype.id,
        prototypeVersion: prototype.version,
        finalScreenId: screenId,
        finalState: state,
        events,
        createdAt: nowIso(),
      };
      db.demonstrations.push(demonstration);
      addAudit(db, 'demonstration.recorded', {
        demonstrationId: demonstration.id,
        testerId: demonstration.testerId,
        directlyTaught: demonstration.directlyTaught,
      });
      return { status: 201, body: demonstration };
    });
    return json(res, result.status, result.body);
  }

  if (method === 'POST' && parts[1] === 'claims' && parts[3] === 'replay-demonstration') {
    const result = await store.mutate((db) => {
      const claim = getClaimOrThrow(db, parts[2]);
      const demonstration = db.demonstrations.find((item) => item.id === body.demonstrationId);
      if (!demonstration) throw new DomainError('DEMONSTRATION_NOT_FOUND', '主持人代点轨迹不存在');
      if (demonstration.taskId !== claim.taskId) {
        throw new DomainError('TASK_MISMATCH', '代点轨迹不属于该受理任务');
      }
      const session = {
        id: shortId('sess-replay'),
        claimId: claim.id,
        testerId: claim.testerId,
        prototypeId: claim.prototypeId,
        prototypeVersion: claim.prototypeVersion,
        screenId: demonstration.finalScreenId,
        state: structuredClone(demonstration.finalState),
        events: demonstration.events.map((event, index) => ({
          ...structuredClone(event),
          seq: index + 1,
          actorRole: 'host',
          provenance: 'replayed-host-trajectory',
        })),
        rejectedEvents: [],
        provenance: 'host-trajectory-replay',
        sourceDemonstrationId: demonstration.id,
        createdAt: nowIso(),
      };
      db.sessions.push(session);
      addAudit(db, 'demonstration.replayed-into-claim', {
        claimId: claim.id,
        sessionId: session.id,
        demonstrationId: demonstration.id,
      });
      return { status: 201, body: { sessionId: session.id, terminal: getSessionView(db, session) } };
    });
    return json(res, result.status, result.body);
  }

  if (method === 'POST' && url.pathname === '/api/hints') {
    const result = await store.mutate((db) => {
      if (!body.id || !body.title) throw new DomainError('HINT_FIELDS_REQUIRED', '提示至少需要 id 和 title');
      if (db.hints[body.id]) throw new DomainError('HINT_EXISTS', '提示 ID 已存在');
      const hint = {
        id: body.id,
        title: body.title,
        body: body.body ?? '',
        answerRevealing: body.answerRevealing === true,
        reveals: Array.isArray(body.reveals) ? body.reveals : [],
        registeredAt: nowIso(),
      };
      db.hints[hint.id] = hint;
      addAudit(db, 'hint.registered', { hintId: hint.id, answerRevealing: hint.answerRevealing });
      return { status: 201, body: hint };
    });
    return json(res, result.status, result.body);
  }

  if (method === 'POST' && url.pathname === '/api/invitations/dispatch') {
    const result = await store.mutate((db) => {
      const task = db.tasks.find((item) => item.id === body.taskId);
      if (!task) throw new DomainError('TASK_NOT_FOUND', '任务不存在');
      const invitation = createInvitation(db, {
        testerId: body.testerId,
        task,
        body,
        packageId: body.packageId ?? null,
      });
      return { status: 201, body: invitation };
    });
    return json(res, result.status, result.body);
  }

  if (method === 'POST' && parts[1] === 'packages' && parts[3] === 'dispatch') {
    const packageId = parts[2];
    const result = await store.mutate((db) => {
      const pkg = db.packages.find((item) => item.id === packageId);
      if (!pkg) throw new DomainError('PACKAGE_NOT_FOUND', '任务包不存在');
      const dispatchedAt = nowIso();
      const invitations = pkg.taskIds.map((taskId) => {
        const task = db.tasks.find((item) => item.id === taskId);
        if (!task) throw new DomainError('TASK_NOT_FOUND', `任务包中的任务不存在：${taskId}`);
        return createInvitation(db, { testerId: body.testerId, task, body, packageId: pkg.id, dispatchedAt });
      });
      addAudit(db, 'package.dispatched', { packageId, testerId: body.testerId, count: invitations.length });
      return { status: 201, body: { packageId: pkg.id, invitations } };
    });
    return json(res, result.status, result.body);
  }

  if (method === 'GET' && url.pathname === '/api/invitations') {
    const testerId = url.searchParams.get('testerId');
    const items = store.data.invitations
      .filter((inv) => !testerId || inv.testerId === testerId)
      .sort((a, b) => b.dispatchedAt.localeCompare(a.dispatchedAt));
    return json(res, 200, items);
  }

  if (method === 'POST' && parts[1] === 'invitations' && parts[3] === 'accept') {
    const invitationId = parts[2];
    const result = await store.mutate((db) => {
      const invitation = db.invitations.find((item) => item.id === invitationId || item.invitationNo === invitationId);
      if (!invitation) throw new DomainError('INVITATION_NOT_FOUND', '邀约不存在');
      if (body.testerId && body.testerId !== invitation.testerId) {
        throw new DomainError('IDENTITY_MISMATCH', '邀约收件人与受理身份不一致');
      }
      if (body.acceptTerms !== true) {
        throw new DomainError('TERMS_NOT_ACCEPTED', '试用员必须先接受事前计件约定');
      }

      const existingClaim = db.claims.find((claim) => claim.invitationId === invitation.id);
      if (existingClaim) {
        if (body.deviceId && !existingClaim.devices.some((d) => d.deviceId === body.deviceId)) {
          existingClaim.devices.push({ deviceId: body.deviceId, deviceName: body.deviceName ?? body.deviceId, addedAt: nowIso() });
        }
        const session = db.sessions.find((s) => s.claimId === existingClaim.id);
        return {
          status: 200,
          body: { alreadyAccepted: true, claim: claimReceipt(db, existingClaim), terminal: getSessionView(db, session) },
        };
      }

      const acceptedAt = nowIso();
      const history = buildTesterHistory(db, invitation.testerId, { beforeIso: acceptedAt });
      const eligibilityAtAcceptance = assessEligibility(invitation.taskSnapshot, history, acceptedAt);
      const claim = {
        id: shortId('claim'),
        receiptNo: receiptNo(),
        invitationId: invitation.id,
        testerId: invitation.testerId,
        taskId: invitation.taskId,
        taskSnapshot: invitation.taskSnapshot,
        prototypeId: invitation.taskSnapshot.prototypeId,
        prototypeVersion: PROTOTYPES[invitation.taskSnapshot.prototypeId].version,
        agreementSnapshot: invitation.agreementSnapshot,
        status: 'active',
        acceptedAt,
        devices: body.deviceId
          ? [{ deviceId: body.deviceId, deviceName: body.deviceName ?? body.deviceId, addedAt: acceptedAt }]
          : [],
        eligibilityAtAcceptance,
      };
      db.claims.push(claim);

      const session = {
        id: shortId('sess'),
        claimId: claim.id,
        testerId: claim.testerId,
        prototypeId: claim.prototypeId,
        prototypeVersion: claim.prototypeVersion,
        screenId: claim.taskSnapshot.initialScreenId,
        state: structuredClone(claim.taskSnapshot.initialState ?? initialPrototypeState(claim.prototypeId)),
        events: [],
        rejectedEvents: [],
        provenance: 'self-service',
        sourceDemonstrationId: null,
        createdAt: acceptedAt,
      };
      db.sessions.push(session);
      invitation.status = 'accepted';
      invitation.acceptedAt = acceptedAt;
      addAudit(db, 'claim.accepted', {
        claimId: claim.id,
        receiptNo: claim.receiptNo,
        testerId: claim.testerId,
        eligibilityAtAcceptance,
      });

      return {
        status: 201,
        body: { claim: claimReceipt(db, claim), terminal: getSessionView(db, session) },
      };
    });
    return json(res, result.status, result.body);
  }

  if (method === 'GET' && parts[1] === 'claims' && parts.length === 3) {
    const claim = getClaimOrThrow(store.data, parts[2]);
    return json(res, 200, claimReceipt(store.data, claim));
  }

  if (method === 'GET' && parts[1] === 'claims' && parts[3] === 'terminal') {
    const claim = getClaimOrThrow(store.data, parts[2]);
    const session = store.data.sessions.find((s) => s.claimId === claim.id);
    return json(res, 200, getSessionView(store.data, session));
  }

  if (method === 'POST' && parts[1] === 'claims' && parts[3] === 'devices') {
    const result = await store.mutate((db) => {
      const claim = getClaimOrThrow(db, parts[2]);
      if (!body.deviceId) throw new DomainError('DEVICE_REQUIRED', '缺少 deviceId');
      if (!claim.devices.some((d) => d.deviceId === body.deviceId)) {
        claim.devices.push({ deviceId: body.deviceId, deviceName: body.deviceName ?? body.deviceId, addedAt: nowIso() });
      }
      return { status: 200, body: claimReceipt(db, claim) };
    });
    return json(res, result.status, result.body);
  }

  if (method === 'POST' && parts[1] === 'claims' && parts[3] === 'hints' && parts[5] === 'view') {
    const hintId = parts[4];
    const result = await store.mutate((db) => {
      const claim = getClaimOrThrow(db, parts[2]);
      const hint = db.hints[hintId] ?? HINTS[hintId];
      if (!hint) throw new DomainError('HINT_NOT_FOUND', '提示不存在');
      if (!(claim.taskSnapshot.allowedHintIds ?? []).includes(hintId)) {
        throw new DomainError('HINT_NOT_ALLOWED', '该提示不属于本单封存提示');
      }
      let exposure = db.exposures.find((e) => e.claimId === claim.id && e.hintId === hintId);
      if (!exposure) {
        exposure = {
          id: shortId('exp'),
          testerId: claim.testerId,
          claimId: claim.id,
          sourceReceiptNo: claim.receiptNo,
          orderId: claim.id,
          hintId,
          hintTitle: hint.title,
          answerRevealing: hint.answerRevealing,
          reveals: hint.reveals ?? [],
          actuallyShown: true,
          shownAt: nowIso(),
          deviceId: body.deviceId ?? null,
        };
        db.exposures.push(exposure);
        addAudit(db, 'hint.actually-shown', {
          exposureId: exposure.id,
          testerId: claim.testerId,
          claimId: claim.id,
          hintId,
        });
      }
      const eligibility = eligibilityForClaim(db, claim);
      return { status: 200, body: { exposure, eligibility, receipt: claimReceipt(db, claim) } };
    });
    return json(res, result.status, result.body);
  }

  if (method === 'POST' && parts[1] === 'sessions' && parts[3] === 'click') {
    const sessionId = parts[2];
    let status = 200;
    let responseBody;
    await store.mutate((db) => {
      const session = db.sessions.find((s) => s.id === sessionId);
      if (!session) throw new DomainError('SESSION_NOT_FOUND', '试用会话不存在');
      const claim = getClaimOrThrow(db, session.claimId);
      if (body.eventId) {
        const existing = session.events.find((e) => e.eventId === body.eventId);
        if (existing) {
          responseBody = { deduplicated: true, terminal: getSessionView(db, session) };
          return;
        }
      }
      const prototype = PROTOTYPES[session.prototypeId];
      try {
        const result = transition(prototype, session.state, session.screenId, {
          actionId: body.actionId,
          targetId: body.targetId ?? body.targetBookId,
          eventId: body.eventId,
          deviceId: body.deviceId,
          actorRole: 'tester',
          provenance: 'live-click',
        });
        session.state = result.state;
        session.screenId = result.screenId;
        const event = { ...result.event, seq: session.events.length + 1 };
        event.seq = session.events.length + 1;
        session.events.push(event);
        if (body.deviceId && !claim.devices.some((d) => d.deviceId === body.deviceId)) {
          claim.devices.push({ deviceId: body.deviceId, addedAt: nowIso() });
        }
        addAudit(db, 'prototype.click', {
          sessionId,
          claimId: claim.id,
          seq: event.seq,
          actionId: event.actionId,
          targetId: event.targetId,
        });
        responseBody = { terminal: getSessionView(db, session) };
      } catch (error) {
        if (error instanceof DomainError) {
          session.rejectedEvents.push({
            at: nowIso(),
            actionId: body.actionId,
            targetId: body.targetId ?? body.targetBookId ?? null,
            code: error.code,
            message: error.message,
          });
          status = 409;
          responseBody = { error: error.code, message: error.message, details: error.details, terminal: getSessionView(db, session) };
          return;
        }
        throw error;
      }
    });
    return json(res, status, responseBody);
  }

  if (method === 'POST' && parts[1] === 'claims' && parts[3] === 'completions') {
    const claimId = parts[2];
    const idemKey = req.headers['idempotency-key'] ? String(req.headers['idempotency-key']) : body.requestId;
    return replayIdempotent(req, res, idemKey, async () => {
      let outcome;
      await store.mutate((db) => {
        const claim = getClaimOrThrow(db, claimId);
        const completionId = body.completionId ?? shortId('cmp');
        const existing = db.completions.find((c) => c.id === completionId);
        if (existing) {
          if (body.deviceId && !existing.deviceIds.includes(body.deviceId)) {
            existing.deviceIds.push(body.deviceId);
            existing.retransmissions.push({ deviceId: body.deviceId, at: nowIso(), kind: 'same-completion-retransmit' });
          }
          const settlement = db.settlements.find((s) => s.completionId === existing.id) ?? null;
          const payout = db.payouts.find((p) => p.completionId === existing.id) ?? null;
          outcome = {
            status: 200,
            body: { deduplicated: true, completion: existing, verdict: existing.verdict, settlement, payout },
          };
          return;
        }

        const activeNonFailed = db.completions.find(
          (c) => c.claimId === claim.id && c.status !== 'rejected-auto' && c.id !== completionId,
        );
        if (activeNonFailed) {
          throw new DomainError('CLAIM_ALREADY_SUBMITTED', '该受理单已有一次进入审核的完成；重复完成不会另立报酬', {
            existingCompletionId: activeNonFailed.id,
          });
        }

        const session = body.sessionId
          ? db.sessions.find((s) => s.id === body.sessionId)
          : db.sessions.find((s) => s.claimId === claim.id);
        if (!session) throw new DomainError('SESSION_NOT_FOUND', '找不到试用轨迹会话');
        if (session.claimId !== claim.id) throw new DomainError('SESSION_CLAIM_MISMATCH', '会话不属于该受理单');

        const prototype = PROTOTYPES[claim.prototypeId];
        const submittedAt = nowIso();
        const eligibility = eligibilityForClaim(db, claim, submittedAt);
        const verdict = evaluateCompletion({
          task: claim.taskSnapshot,
          prototype,
          claim,
          session,
          eligibility,
          submittedAt,
        });

        const initialDeviceId = body.deviceId ?? claim.devices[0]?.deviceId ?? null;
        const completion = {
          id: completionId,
          claimId: claim.id,
          invitationId: claim.invitationId,
          testerId: claim.testerId,
          taskId: claim.taskId,
          taskVersion: claim.taskSnapshot.version,
          sessionId: session.id,
          deviceId: initialDeviceId,
          deviceIds: initialDeviceId ? [initialDeviceId] : [],
          retransmissions: [],
          submittedAt,
          eventCount: session.events.length,
          verdict,
          status: verdict.behaviorPassed ? 'awaiting-review' : 'rejected-auto',
        };
        db.completions.push(completion);
        claim.status = verdict.behaviorPassed ? 'submitted' : 'active';
        addAudit(db, 'completion.submitted', {
          completionId,
          claimId: claim.id,
          behaviorPassed: verdict.behaviorPassed,
          discoveryPayable: verdict.discoveryPayable,
        });
        outcome = { status: 201, body: { completion, verdict } };
      });
      return outcome;
    });
  }

  if (method === 'GET' && url.pathname === '/api/review/queue') {
    const statusFilter = url.searchParams.get('status');
    const items = store.data.completions
      .filter((c) => !statusFilter || c.status === statusFilter)
      .sort((a, b) => b.submittedAt.localeCompare(a.submittedAt))
      .map((completion) => ({
        completion,
        claim: claimReceipt(store.data, getClaimOrThrow(store.data, completion.claimId)),
        settlement: store.data.settlements.find((s) => s.completionId === completion.id) ?? null,
        review: store.data.reviews.find((r) => r.completionId === completion.id) ?? null,
      }));
    return json(res, 200, items);
  }

  if (method === 'POST' && parts[1] === 'completions' && parts[3] === 'review') {
    const completionId = parts[2];
    const result = await store.mutate((db) => {
      const completion = db.completions.find((c) => c.id === completionId);
      if (!completion) throw new DomainError('COMPLETION_NOT_FOUND', '完成记录不存在');
      const claim = getClaimOrThrow(db, completion.claimId);
      const decision = body.decision;
      const review = {
        id: shortId('rev'),
        completionId,
        claimId: claim.id,
        reviewerName: body.reviewerName ?? '招募方审核员',
        decision,
        note: body.note ?? '',
        reviewedAt: nowIso(),
      };
      db.reviews.push(review);

      if (decision === 'approve') {
        if (!completion.verdict?.discoveryPayable) {
          throw new DomainError(
            'NOT_SETTLABLE_UNDER_AGREED_RATE',
            '系统复核不通过或资格已排除；不能在结账时重新定义计件价格并批准发现奖',
            { verdict: completion.verdict },
          );
        }
        let settlement = db.settlements.find((s) => s.claimId === claim.id);
        if (!settlement) {
          settlement = {
            id: shortId('set'),
            settlementNo: shortId('STL'),
            claimId: claim.id,
            completionId: completion.id,
            testerId: claim.testerId,
            taskId: claim.taskId,
            amount: completion.verdict.payableAmount,
            currency: completion.verdict.currency,
            rateSnapshot: claim.agreementSnapshot,
            status: 'approved-unpaid',
            approvedAt: nowIso(),
            reviewId: review.id,
          };
          db.settlements.push(settlement);
        }
        completion.status = 'approved';
        claim.status = 'approved-payable';
        addAudit(db, 'settlement.approved', { settlementId: settlement.id, claimId: claim.id, amount: settlement.amount });
        return { status: 200, body: { review, completion, settlement } };
      }

      if (decision === 'acknowledge-blocked') {
        completion.status = 'settlement-blocked';
        claim.status = 'settlement-blocked';
        addAudit(db, 'review.blocked-confirmed', { completionId, reason: completion.verdict.eligibility?.reason });
        return { status: 200, body: { review, completion } };
      }

      completion.status = 'rejected';
      claim.status = 'rejected';
      addAudit(db, 'completion.rejected', { completionId, note: body.note });
      return { status: 200, body: { review, completion } };
    });
    return json(res, result.status, result.body);
  }

  if (method === 'GET' && url.pathname === '/api/settlements') {
    const testerId = url.searchParams.get('testerId');
    return json(
      res,
      200,
      store.data.settlements
        .filter((s) => !testerId || s.testerId === testerId)
        .map((settlement) => ({
          settlement,
          payout: store.data.payouts.find((p) => p.settlementId === settlement.id) ?? null,
        })),
    );
  }

  if (method === 'POST' && parts[1] === 'claims' && parts[3] === 'payout') {
    const claimId = parts[2];
    const idemKey = req.headers['idempotency-key'] ? String(req.headers['idempotency-key']) : body.requestId;
    return replayIdempotent(req, res, idemKey, async () => {
      let outcome;
      await store.mutate((db) => {
        const claim = getClaimOrThrow(db, claimId);
        const settlement = db.settlements.find((s) => s.claimId === claim.id);
        if (!settlement) throw new DomainError('SETTLEMENT_NOT_APPROVED', '没有审核通过、可领取的报酬');
        const existingPayout = db.payouts.find((p) => p.settlementId === settlement.id || p.claimId === claim.id);
        if (existingPayout) {
          outcome = { status: 200, body: { deduplicated: true, payout: existingPayout, settlement } };
          return;
        }
        const payout = {
          id: shortId('pay'),
          settlementId: settlement.id,
          claimId: claim.id,
          completionId: settlement.completionId,
          testerId: claim.testerId,
          amount: settlement.amount,
          currency: settlement.currency,
          deviceId: body.deviceId ?? null,
          status: 'paid',
          paidAt: nowIso(),
        };
        db.payouts.push(payout);
        settlement.status = 'paid';
        settlement.paidAt = payout.paidAt;
        claim.status = 'paid';
        addAudit(db, 'payout.paid', { payoutId: payout.id, claimId: claim.id, amount: payout.amount });
        outcome = { status: 201, body: { payout, settlement } };
      });
      return outcome;
    });
  }

  if (method === 'GET' && url.pathname === '/api/payments') {
    const testerId = url.searchParams.get('testerId');
    const payouts = store.data.payouts.filter((p) => !testerId || p.testerId === testerId);
    const total = payouts.reduce((sum, item) => sum + item.amount, 0);
    return json(res, 200, { testerId: testerId ?? null, totalPaid: total, payouts });
  }

  if (method === 'GET' && url.pathname === '/api/audit') {
    return json(res, 200, {
      counts: {
        testers: store.data.testers.length,
        tasks: store.data.tasks.length,
        invitations: store.data.invitations.length,
        claims: store.data.claims.length,
        exposures: store.data.exposures.length,
        completions: store.data.completions.length,
        settlements: store.data.settlements.length,
        payouts: store.data.payouts.length,
      },
      ...store.data,
    });
  }

  if (method === 'POST' && url.pathname === '/api/admin/reset') {
    await store.mutate((db) => {
      const fresh = defaultData();
      for (const key of Object.keys(db)) delete db[key];
      Object.assign(db, fresh);
    });
    return json(res, 200, { ok: true, message: '已重置为内置任务，不保留本地受理记录' });
  }

  return json(res, 404, { error: 'NOT_FOUND', path: url.pathname });
}

/* -------------------------------- static ---------------------------------- */

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
};

async function serveStatic(req, res, pathname) {
  let relative = pathname === '/' ? '/index.html' : pathname;
  const filePath = path.normalize(path.join(PUBLIC_DIR, relative));
  if (!filePath.startsWith(PUBLIC_DIR)) {
    return json(res, 403, { error: 'FORBIDDEN' });
  }
  try {
    const content = await readFile(filePath);
    res.writeHead(200, { 'content-type': MIME[path.extname(filePath)] ?? 'application/octet-stream' });
    res.end(content);
  } catch {
    // 简单 SPA-ish fallback to portal for unknown non-API paths.
    try {
      const index = await readFile(path.join(PUBLIC_DIR, 'index.html'));
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      res.end(index);
    } catch {
      json(res, 404, { error: 'STATIC_NOT_FOUND' });
    }
  }
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${req.headers.host ?? `${HOST}:${PORT}`}`);
    if (url.pathname.startsWith('/api/')) {
      return await handleApi(req, res, url);
    }
    if (req.method !== 'GET') return json(res, 405, { error: 'METHOD_NOT_ALLOWED' });
    return await serveStatic(req, res, url.pathname);
  } catch (error) {
    if (error instanceof DomainError) {
      return json(res, error.code === 'NOT_FOUND' || error.code.endsWith('_NOT_FOUND') ? 404 : 409, {
        error: error.code,
        message: error.message,
        details: error.details,
      });
    }
    console.error(error);
    return json(res, 500, { error: 'INTERNAL', message: error.message });
  }
});

server.on('error', (error) => {
  if (error.code === 'EADDRINUSE') {
    console.error(`端口 ${PORT} 已被占用。请停止旧服务，或使用 PORT=<其他端口> npm start。`);
  }
  throw error;
});

server.listen(PORT, HOST, () => {
  console.log(`试用受理台已启动: http://${HOST}:${PORT}`);
  console.log(`数据文件: ${DATA_FILE}`);
});

function shutdown() {
  server.close(() => process.exit(0));
  // 本地验证脚本重启时，不必等待 keep-alive 连接自然超时。
  server.closeAllConnections?.();
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
