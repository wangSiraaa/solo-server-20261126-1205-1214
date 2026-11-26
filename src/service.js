import { randomBytes, randomUUID, createHash } from 'node:crypto';
import {
  FORM_ID,
  POLICY,
  buildFormV1,
  publishSplitVisitForm,
  findQuestion,
  isSectionVisible,
  normalizeAnswerValue,
  validateDraftAnswers,
} from './form.js';

export class ServiceError extends Error {
  constructor(status, code, message, details = {}) {
    super(message);
    this.name = 'ServiceError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

const TARGET_ANSWER_KEYS = new Set(['attendanceMode', 'partnerName', 'partnerContact']);
const RESERVED_PATCH_KEYS = new Set([
  'answers',
  'response',
  'classId',
  'companionReceiptId',
  'idempotencyKey',
  'stationId',
  'requestedStationId',
  'stationIds',
  'pairGroupId',
  'submit',
]);

function token(prefix) {
  return `${prefix}_${randomBytes(24).toString('base64url')}`;
}

function uuidId(prefix) {
  return `${prefix}_${randomUUID()}`;
}

function occKey(classId, stationId) {
  return `${classId}::${stationId}`;
}

function pad(value, size = 6) {
  return String(value).padStart(size, '0');
}

function stableHash(value) {
  return createHash('sha256').update(JSON.stringify(value)).digest('hex');
}

function sameValue(a, b) {
  if (a === b) return true;
  if (a == null || b == null) return a == b;
  return JSON.stringify(a) === JSON.stringify(b);
}

function initialState() {
  const form = buildFormV1();
  return {
    schemaVersion: 1,
    policy: POLICY,
    createdAt: new Date().toISOString(),
    draftTtlMs: 30 * 24 * 60 * 60 * 1000,
    forms: [form],
    activeFormVersion: 1,
    classes: {},
    drafts: {},
    invitations: {},
    receipts: {},
    orders: {},
    occupancy: {},
    idempotency: {},
    counters: {
      order: 0,
      enrollment: 0,
    },
  };
}

function activeForm(state) {
  return state.forms.find((f) => f.formId === FORM_ID && f.version === state.activeFormVersion)
    ?? state.forms[state.forms.length - 1];
}

function getFormVersion(state, formId, version) {
  const form = state.forms.find(
    (f) => f.formId === (formId ?? FORM_ID) && f.version === Number(version),
  );
  if (!form) throw new ServiceError(404, 'FORM_VERSION_NOT_FOUND', '找不到指定表单版本');
  return form;
}

function chineseStationLabel(index) {
  const numerals = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十'];
  return `${numerals[index] ?? index + 1}号台`;
}

function normalizeStation(input, index) {
  if (typeof input === 'string') return { id: input, label: chineseStationLabel(index), order: index + 1 };
  const id = input.id ?? input.stationId ?? `S${index + 1}`;
  return {
    id,
    label: input.label ?? input.name ?? chineseStationLabel(index),
    order: input.order ?? index + 1,
  };
}

function resolveStationRef(reference, stations) {
  if (reference && typeof reference === 'object') reference = reference.id ?? reference.stationId;
  const exact = stations.find((station) => station.id === reference);
  if (exact) return exact.id;

  const numeric = Number(reference);
  if (Number.isInteger(numeric)) {
    const byOrder = stations.find((station) => station.order === numeric);
    if (byOrder) return byOrder.id;
    const byDefaultId = stations.find((station) => station.id === `S${numeric}`);
    if (byDefaultId) return byDefaultId.id;
  }

  const chineseMap = { 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6 };
  if (typeof reference === 'string' && chineseMap[reference[0]]) {
    const order = chineseMap[reference[0]];
    const byOrder = stations.find((station) => station.order === order);
    if (byOrder) return byOrder.id;
  }
  return null;
}

function normalizePairGroup(input, stations, index) {
  const rawIds = Array.isArray(input)
    ? input
    : input.stationIds ?? input.stations ?? input.pair ?? [];
  const requestedIds = rawIds.map((value) => (typeof value === 'string' || typeof value === 'number'
    ? value
    : value.id));
  if (requestedIds.length !== 2 || new Set(requestedIds.map(String)).size !== 2) {
    throw new ServiceError(400, 'INVALID_PAIR_GROUP', '每个可结伴组必须包含两个不同台号');
  }
  const stationIds = requestedIds.map((reference) => resolveStationRef(reference, stations));
  if (stationIds.some((stationId) => !stationId)) {
    throw new ServiceError(400, 'INVALID_PAIR_GROUP', `台号不存在或无法识别：${JSON.stringify(requestedIds)}`);
  }
  return {
    id: Array.isArray(input) ? `pg-${stationIds.join('-')}` : input.id ?? input.name ?? `pg-${index + 1}`,
    stationIds,
  };
}

function classView(state, cls) {
  const stations = cls.stations.map((station) => {
    const occupancy = state.occupancy[occKey(cls.id, station.id)] ?? null;
    return {
      ...station,
      available: !occupancy,
      occupancy,
    };
  });
  const pairGroups = (cls.pairGroups ?? []).map((group) => {
    const occupancies = group.stationIds.map((stationId) =>
      state.occupancy[occKey(cls.id, stationId)] ?? null);
    return {
      ...group,
      canBookPair: occupancies.every((item) => !item),
      occupancies,
    };
  });
  return {
    ...cls,
    stations,
    pairGroups,
    availableStationIds: stations.filter((s) => s.available).map((s) => s.id),
  };
}

function isDraftExpired(draft, now = Date.now()) {
  return new Date(draft.expiresAt).getTime() <= now;
}

function draftView(state, draft) {
  const form = getFormVersion(state, draft.formId, draft.formVersion);
  const validation = validateDraftAnswers(draft, form);
  const cls = state.classes[draft.classId] ? classView(state, state.classes[draft.classId]) : null;
  return {
    ...structuredClone(draft),
    expired: isDraftExpired(draft),
    canContinue: draft.status === 'open' && !isDraftExpired(draft),
    validation: {
      valid: validation.valid,
      errors: validation.errors,
      staleAnswers: validation.staleAnswers,
    },
    effectiveAnswers: validation.effectiveAnswers,
    effectiveResponses: validation.effectiveAnswers,
    form: structuredClone(form),
    class: cls,
  };
}

function effectiveReceiptForDraft(state, draft) {
  const explicitId = draft.companionReceiptId;
  if (explicitId) {
    return state.receipts[explicitId] ?? null;
  }
  const receipts = Object.values(state.receipts)
    .filter((r) => r.targetDraftId === draft.id && r.status === 'confirmed')
    .sort((a, b) => new Date(b.confirmedAt) - new Date(a.confirmedAt));
  return receipts[0] ?? null;
}

function counterOrderView(state, order) {
  const form = getFormVersion(state, order.formId, order.formVersion);
  const qa = [];
  for (const q of form.questions) {
    if (Object.hasOwn(order.effectiveAnswers, q.key)) {
      qa.push({
        key: q.key,
        title: q.title,
        sectionKey: null,
        value: order.effectiveAnswers[q.key],
      });
    }
  }
  for (const section of form.sections ?? []) {
    for (const q of section.questions) {
      if (Object.hasOwn(order.effectiveAnswers, q.key)) {
        qa.push({
          key: q.key,
          title: q.title,
          sectionKey: section.key,
          sectionTitle: section.title,
          value: order.effectiveAnswers[q.key],
        });
      }
    }
  }
  const cls = state.classes[order.classId] ? classView(state, state.classes[order.classId]) : null;
  return {
    ...structuredClone(order),
    ok: true,
    effectiveResponses: structuredClone(order.effectiveAnswers),
    counter: {
      formVersion: {
        formId: form.formId,
        version: form.version,
        title: form.title,
        policy: form.policy,
        publishedAt: form.publishedAt,
        terms: form.terms,
      },
      effectiveAnswers: structuredClone(order.effectiveAnswers),
      effectiveResponses: structuredClone(order.effectiveAnswers),
      effectiveQa: qa,
      enrollmentNumbers: order.enrollmentNumbers,
      seatedPeople: order.seats.map((seat) => ({
        stationId: seat.stationId,
        stationLabel: seat.stationLabel,
        name: seat.personName,
        role: seat.role,
        enrollmentNumber: seat.enrollmentNumber,
      })),
    },
    class: cls,
  };
}

export class NightSchoolService {
  constructor(options = {}) {
    this.dataFile = options.dataFile ?? process.env.DATA_FILE ?? 'data/night-school.json';
    this.store = options.store ?? null;
    this._storePromise = null;
  }

  async init() {
    if (this._storePromise) return this._storePromise;
    this._storePromise = (async () => {
      if (!this.store) {
        const { JsonStore } = await import('./store.js');
        this.store = new JsonStore(this.dataFile, initialState);
      }
      await this.store.init();
      return this;
    })();
    return this._storePromise;
  }

  async ready() {
    return this.init();
  }

  tx(fn) {
    return this.init().then(() => this.store.tx(fn));
  }

  read(fn) {
    return this.init().then(() => this.store.read(fn));
  }

  async health() {
    return this.read((state) => ({
      ok: true,
      service: 'night-school-form-service',
      policy: POLICY,
      activeFormVersion: state.activeFormVersion,
      time: new Date().toISOString(),
    }));
  }

  async reset() {
    return this.tx((state) => {
      const fresh = initialState();
      for (const key of Object.keys(state)) delete state[key];
      Object.assign(state, fresh);
      return { ok: true, resetAt: state.createdAt };
    });
  }

  // ---------- Forms ----------

  async listForms() {
    return this.read((state) => ({
      ok: true,
      policy: POLICY,
      activeFormVersion: state.activeFormVersion,
      forms: structuredClone(state.forms),
    }));
  }

  async getActiveForm() {
    return this.read((state) => ({ ok: true, form: structuredClone(activeForm(state)) }));
  }

  async getFormVersion(formId, version) {
    return this.read((state) => ({ ok: true, form: structuredClone(getFormVersion(state, formId, version)) }));
  }

  async publishSplitForm() {
    return this.tx((state) => {
      const current = activeForm(state);
      const next = publishSplitVisitForm(current);
      if (next.version !== current.version) {
        state.forms.push(next);
        state.activeFormVersion = next.version;
      }
      return { ok: true, alreadyPublished: next.version === current.version, form: structuredClone(next) };
    });
  }

  async publishForm(input = {}) {
    return this.tx((state) => {
      const current = activeForm(state);
      const formId = input.formId ?? FORM_ID;
      const version = Math.max(...state.forms.map((f) => f.version), 0) + 1;
      const next = {
        ...structuredClone(input),
        formId,
        version: input.version ?? version,
        status: 'published',
        policy: POLICY,
        publishedAt: new Date().toISOString(),
        questions: input.questions ?? current.questions,
        sections: input.sections ?? current.sections,
        terms: input.terms ?? current.terms,
      };
      state.forms = state.forms.filter((f) => !(f.formId === formId && f.version === next.version));
      state.forms.push(next);
      state.activeFormVersion = next.version;
      return { ok: true, form: structuredClone(next) };
    });
  }

  // ---------- Classes ----------

  async createClass(input = {}) {
    return this.tx((state) => {
      const id = input.id ?? uuidId('class');
      if (state.classes[id]) {
        throw new ServiceError(409, 'CLASS_EXISTS', `班级已存在：${id}`);
      }

      let stationInputs = input.stations ?? input.desks;
      if (!stationInputs) {
        const count = Number(input.stationCount ?? input.seatCount ?? input.stationNumber ?? 3);
        stationInputs = Array.from({ length: count }, (_, i) => `S${i + 1}`);
      }
      if (!Array.isArray(stationInputs) || stationInputs.length === 0) {
        throw new ServiceError(400, 'INVALID_STATIONS', '班级至少需要一个台位');
      }
      const stations = stationInputs.map(normalizeStation);

      let pairInputs = input.pairGroups ?? input.pairablePairs ?? input.pairEdges ?? input.adjacentPairs ?? [];
      if (input.allowAllAdjacent === true && pairInputs.length === 0) {
        pairInputs = stations.slice(0, -1).map((station, i) => [station.id, stations[i + 1].id]);
      }
      const pairGroups = pairInputs.map((group, i) => normalizePairGroup(group, stations, i));

      const cls = {
        id,
        name: input.name ?? input.title ?? `夜校班 ${id}`,
        courseTitle: input.courseTitle ?? input.course ?? '夜间实作课',
        stationCount: stations.length,
        stations,
        pairGroups,
        pairPolicy: 'only_explicit_pair_groups_can_book_as_pair',
        createdAt: new Date().toISOString(),
      };
      state.classes[id] = cls;
      const view = classView(state, cls);
      return { ok: true, class: view, session: view };
    });
  }

  async listClasses() {
    return this.read((state) => {
      const classes = Object.values(state.classes).map((cls) => classView(state, cls));
      return { ok: true, classes, sessions: classes };
    });
  }

  async getClass(classId) {
    return this.read((state) => {
      const cls = state.classes[classId];
      if (!cls) throw new ServiceError(404, 'CLASS_NOT_FOUND', '找不到班级/场次');
      const view = classView(state, cls);
      return { ok: true, class: view, session: view };
    });
  }

  // ---------- Draft answer mechanics ----------

  extractAnswerPatch(form, input = {}) {
    if (input.answers && typeof input.answers === 'object') return structuredClone(input.answers);
    if (input.response && typeof input.response === 'object') return structuredClone(input.response);
    const patch = {};
    for (const [key, value] of Object.entries(input)) {
      if (RESERVED_PATCH_KEYS.has(key)) continue;
      if (findQuestion(form, key)) patch[key] = value;
    }
    return patch;
  }

  applyAnswerPatch(state, draft, form, rawPatch) {
    if (!rawPatch || typeof rawPatch !== 'object' || Array.isArray(rawPatch)) {
      throw new ServiceError(400, 'INVALID_ANSWERS', 'answers 必须是键值对象');
    }

    const patch = {};
    for (const [key, rawValue] of Object.entries(rawPatch)) {
      const found = findQuestion(form, key);
      if (!found) throw new ServiceError(400, 'UNKNOWN_QUESTION', `此表单版本没有题目：${key}`);
      patch[key] = normalizeAnswerValue(found.question, rawValue);
    }

    const changedKeys = [];
    for (const [key, value] of Object.entries(patch)) {
      if (!sameValue(draft.rawAnswers[key], value)) changedKeys.push(key);
    }

    // A trigger answer change starts a new reveal generation for dependent
    // sections, even if the section becomes visible again later.
    for (const section of form.sections ?? []) {
      const triggerKeys = new Set();
      const collect = (condition) => {
        if (!condition) return;
        if (condition.question) triggerKeys.add(condition.question);
        for (const criterion of [...(condition.all ?? []), ...(condition.any ?? [])]) {
          if (criterion.question) triggerKeys.add(criterion.question);
        }
      };
      collect(section.showWhen);
      const triggerChanged = [...triggerKeys].some((key) => changedKeys.includes(key));
      if (triggerChanged) {
        draft.sectionEpochs[section.key] = (draft.sectionEpochs[section.key] ?? 0) + 1;
      }
    }

    Object.assign(draft.rawAnswers, patch);
    const nowIso = new Date().toISOString();

    for (const key of Object.keys(patch)) {
      const found = findQuestion(form, key);
      const section = found.section;
      if (!section) {
        draft.answerMeta[key] = { epoch: 0, updatedAt: nowIso };
        continue;
      }

      const visible = isSectionVisible(section, draft.rawAnswers);

      if (visible) {
        if (!(draft.sectionEpochs[section.key] > 0)) draft.sectionEpochs[section.key] = 1;
        draft.answerMeta[key] = {
          epoch: draft.sectionEpochs[section.key],
          updatedAt: nowIso,
        };
      } else {
        draft.answerMeta[key] = {
          epoch: draft.sectionEpochs[section.key] ?? 0,
          updatedAt: nowIso,
          whileHidden: true,
        };
      }
    }

    return {
      changedKeys,
      targetChanged: changedKeys.some((key) => TARGET_ANSWER_KEYS.has(key)),
    };
  }

  async createDraft(input = {}) {
    return this.tx((state) => {
      let classId = input.classId;
      if (!classId) {
        const ids = Object.keys(state.classes);
        if (ids.length === 1) classId = ids[0];
      }
      const cls = state.classes[classId];
      if (!cls) throw new ServiceError(404, 'CLASS_NOT_FOUND', '请先指定存在的班级/场次');

      const form = input.formVersion
        ? getFormVersion(state, FORM_ID, input.formVersion)
        : activeForm(state);

      const ttlMs = Number.isFinite(Number(input.ttlMs))
        ? Number(input.ttlMs)
        : Number.isFinite(Number(input.expiresInMs))
          ? Number(input.expiresInMs)
          : Number.isFinite(Number(input.ttl))
            ? Number(input.ttl)
            : Number.isFinite(Number(input.expiresInDays))
              ? Number(input.expiresInDays) * 24 * 60 * 60 * 1000
              : state.draftTtlMs;
      const now = Date.now();
      const expiresAt = input.expiresAt
        ? new Date(input.expiresAt).toISOString()
        : new Date(now + ttlMs).toISOString();
      const id = input.id ?? uuidId('draft');
      if (state.drafts[id]) throw new ServiceError(409, 'DRAFT_EXISTS', `草稿已存在：${id}`);

      const draft = {
        id,
        classId,
        formId: form.formId,
        formVersion: form.version,
        policy: POLICY,
        status: 'open',
        rawAnswers: {},
        answerMeta: {},
        sectionEpochs: Object.fromEntries((form.sections ?? []).map((s) => [s.key, 0])),
        targetRevision: 1,
        activeInvitationId: null,
        invitationIds: [],
        companionReceiptId: input.companionReceiptId ?? null,
        accessToken: token('drafttok'),
        expiresAt,
        createdAt: new Date(now).toISOString(),
        updatedAt: new Date(now).toISOString(),
        submittedAt: null,
        submittedOrderId: null,
      };
      state.drafts[id] = draft;

      const patch = this.extractAnswerPatch(form, input);
      if (Object.keys(patch).length) {
        this.applyAnswerPatch(state, draft, form, patch);
      }
      if (input.companionReceiptId) draft.companionReceiptId = input.companionReceiptId;

      return {
        ok: true,
        draftToken: draft.accessToken,
        continuationUrl: `/?draft=${draft.id}&token=${draft.accessToken}`,
        draft: draftView(state, draft),
      };
    });
  }

  async getDraft(draftId) {
    return this.read((state) => {
      const draft = state.drafts[draftId];
      if (!draft) throw new ServiceError(404, 'DRAFT_NOT_FOUND', '找不到草稿');
      return { ok: true, draft: draftView(state, draft) };
    });
  }

  mutateDraftTx(state, draftId, input = {}) {
    const draft = state.drafts[draftId];
    if (!draft) throw new ServiceError(404, 'DRAFT_NOT_FOUND', '找不到草稿');
    if (draft.status === 'submitted') {
      throw new ServiceError(409, 'DRAFT_ALREADY_SUBMITTED', '草稿已成功递交，不能再修改；重复递交会返回原单');
    }
    if (isDraftExpired(draft)) {
      throw new ServiceError(410, 'DRAFT_EXPIRED', '草稿已到期，旧表条款不再据其办理；请新建草稿');
    }

    const form = getFormVersion(state, draft.formId, draft.formVersion);
    let targetChanged = false;

    if (Object.hasOwn(input, 'classId') && input.classId !== draft.classId) {
      if (!state.classes[input.classId]) throw new ServiceError(404, 'CLASS_NOT_FOUND', '找不到新的班级/场次');
      draft.classId = input.classId;
      targetChanged = true;
    }

    const answerPatch = this.extractAnswerPatch(form, input);
    if (Object.keys(answerPatch).length) {
      const result = this.applyAnswerPatch(state, draft, form, answerPatch);
      targetChanged = targetChanged || result.targetChanged;
    }

    if (Object.hasOwn(input, 'companionReceiptId')) {
      draft.companionReceiptId = input.companionReceiptId || null;
    }

    if (targetChanged) {
      draft.targetRevision += 1;
      for (const invitationId of draft.invitationIds) {
        const invitation = state.invitations[invitationId];
        if (invitation && invitation.targetRevision < draft.targetRevision) {
          invitation.status = 'superseded';
          invitation.supersededAt = new Date().toISOString();
          invitation.supersedeReason = 'registration_target_changed';
        }
      }
      const active = draft.activeInvitationId ? state.invitations[draft.activeInvitationId] : null;
      if (!active || active.status === 'superseded') draft.activeInvitationId = null;
    }

    draft.updatedAt = new Date().toISOString();
    return draft;
  }

  async updateDraft(draftId, input = {}) {
    return this.tx((state) => {
      const draft = this.mutateDraftTx(state, draftId, input);
      return { ok: true, draft: draftView(state, draft) };
    });
  }

  // ---------- Companion invitations/receipts ----------

  async issueInvitation(draftId, input = {}) {
    return this.tx((state) => {
      let draft = state.drafts[draftId];
      if (!draft) throw new ServiceError(404, 'DRAFT_NOT_FOUND', '找不到草稿');

      // Convenience: allow partner fields in the issue call.
      if (input.partnerName || input.partnerContact || input.answers) {
        draft = this.mutateDraftTx(state, draftId, {
          answers: input.answers ?? {
            ...(input.partnerName ? { partnerName: input.partnerName } : {}),
            ...(input.partnerContact ? { partnerContact: input.partnerContact } : {}),
          },
        });
      } else if (draft.status !== 'open') {
        throw new ServiceError(409, 'DRAFT_NOT_OPEN', '草稿状态不允许发邀请');
      }
      if (isDraftExpired(draft)) throw new ServiceError(410, 'DRAFT_EXPIRED', '草稿已到期');

      const currentActive = draft.activeInvitationId ? state.invitations[draft.activeInvitationId] : null;
      if (
        currentActive
        && currentActive.targetRevision === draft.targetRevision
        && ['pending', 'confirmed'].includes(currentActive.status)
      ) {
        return {
          ok: true,
          invitation: structuredClone(currentActive),
          invitationUrl: `/?invitation=${currentActive.id}&token=${currentActive.token}`,
          reused: true,
        };
      }

      const form = getFormVersion(state, draft.formId, draft.formVersion);
      const validation = validateDraftAnswers(draft, form);
      const effective = validation.effectiveAnswers;
      const invitation = {
        id: uuidId('inv'),
        token: token('invtok'),
        draftId: draft.id,
        classId: draft.classId,
        targetRevision: draft.targetRevision,
        partnerName: input.companionName ?? effective.partnerName ?? null,
        partnerContact: effective.partnerContact ?? null,
        status: 'pending',
        receiptId: null,
        createdAt: new Date().toISOString(),
        confirmedAt: null,
      };
      state.invitations[invitation.id] = invitation;
      draft.invitationIds.push(invitation.id);
      draft.activeInvitationId = invitation.id;

      return {
        ok: true,
        invitation: structuredClone(invitation),
        invitationUrl: `/?invitation=${invitation.id}&token=${invitation.token}`,
      };
    });
  }

  async getInvitation(invitationId, suppliedToken) {
    return this.read((state) => {
      const invitation = state.invitations[invitationId];
      if (!invitation) throw new ServiceError(404, 'INVITATION_NOT_FOUND', '找不到邀请');
      if (suppliedToken !== undefined && suppliedToken !== invitation.token) {
        throw new ServiceError(403, 'INVITATION_TOKEN_INVALID', '邀请令牌不正确');
      }
      const draft = state.drafts[invitation.draftId];
      return {
        ok: true,
        invitation: {
          ...structuredClone(invitation),
          draftExpired: draft ? isDraftExpired(draft) : null,
          draftStatus: draft?.status ?? null,
        },
      };
    });
  }

  async confirmInvitation(invitationId, input = {}) {
    return this.tx((state) => {
      const invitation = state.invitations[invitationId];
      if (!invitation) throw new ServiceError(404, 'INVITATION_NOT_FOUND', '找不到同伴邀请');
      const suppliedToken = input.token ?? input.invitationToken;
      if (suppliedToken !== invitation.token) {
        throw new ServiceError(403, 'INVITATION_TOKEN_INVALID', '必须使用该同伴自己的邀请令牌确认');
      }
      if (input.consent !== true && input.decision !== 'confirm') {
        throw new ServiceError(400, 'COMPANION_CONSENT_REQUIRED', '同伴必须明确同意并确认');
      }

      const targetDraftId = input.draftId ?? input.targetDraftId ?? invitation.draftId;
      const targetDraft = state.drafts[targetDraftId];
      if (!targetDraft) throw new ServiceError(404, 'DRAFT_NOT_FOUND', '回执指定的草稿不存在');

      const draftMatches = targetDraftId === invitation.draftId;
      const revisionMatches = targetDraft.targetRevision === invitation.targetRevision;
      const classMatches = targetDraft.classId === invitation.classId;
      const usable = draftMatches && revisionMatches && classMatches;

      const receipt = {
        id: uuidId('rcp'),
        invitationId: invitation.id,
        companionName: input.companionName ?? invitation.partnerName ?? null,
        consent: true,
        draftId: targetDraftId,
        targetDraftId,
        targetClassId: targetDraft.classId,
        targetRevision: targetDraft.targetRevision,
        intendedDraftId: invitation.draftId,
        intendedRevision: invitation.targetRevision,
        status: usable ? 'confirmed' : !draftMatches ? 'target_mismatch' : 'target_changed',
        usable,
        mismatchReasons: [
          !draftMatches ? 'draft_id_mismatch' : null,
          !revisionMatches ? 'target_revision_mismatch' : null,
          !classMatches ? 'class_mismatch' : null,
        ].filter(Boolean),
        confirmedAt: new Date().toISOString(),
      };
      state.receipts[receipt.id] = receipt;

      if (usable) {
        invitation.status = 'confirmed';
        invitation.receiptId = receipt.id;
        invitation.confirmedAt = receipt.confirmedAt;
        targetDraft.companionReceiptId = receipt.id;
      } else {
        invitation.status = !draftMatches ? 'confirmed_other_draft' : 'confirmation_outdated';
        invitation.lastReceiptId = receipt.id;
      }

      return {
        ok: true,
        receipt: structuredClone(receipt),
        confirmation: structuredClone(receipt),
        attachedToDraftId: usable ? targetDraftId : null,
        warning: usable
          ? null
          : '该回执确认的不是本邀请指定的当前报名；不能用于为原草稿占台。',
      };
    });
  }

  // ---------- Submission / seats ----------

  validateCompanionForSubmit(state, draft, input) {
    const requestedReceiptId = Object.hasOwn(input, 'companionReceiptId')
      ? input.companionReceiptId
      : draft.companionReceiptId;
    const receipt = requestedReceiptId
      ? state.receipts[requestedReceiptId]
      : effectiveReceiptForDraft(state, draft);

    if (!receipt) {
      throw new ServiceError(422, 'COMPANION_CONFIRMATION_REQUIRED', '双人单必须有同伴邀请确认回执');
    }
    if (receipt.consent !== true) {
      throw new ServiceError(422, 'COMPANION_CONFIRMATION_INVALID', '同伴回执缺少明确同意', {
        receiptId: receipt.id,
        status: receipt.status,
      });
    }
    const receiptDraftId = receipt.draftId ?? receipt.targetDraftId;
    if (receiptDraftId !== draft.id || (receipt.targetDraftId !== undefined && receipt.targetDraftId !== draft.id)) {
      throw new ServiceError(422, 'COMPANION_RECEIPT_FOR_OTHER_DRAFT', '同伴回执确认的是另一张草稿，不能给本单占台', {
        receiptId: receipt.id,
        receiptDraftId,
        receiptTargetDraftId: receipt.targetDraftId ?? receipt.draftId,
        draftId: draft.id,
      });
    }
    if (receipt.status !== 'confirmed') {
      throw new ServiceError(422, 'COMPANION_CONFIRMATION_INVALID', '同伴回执不是当前报名的有效确认', {
        receiptId: receipt.id,
        status: receipt.status,
      });
    }
    if (receipt.targetRevision !== draft.targetRevision) {
      throw new ServiceError(422, 'COMPANION_CONFIRMATION_OUTDATED', '报名对象或同伴信息已更换，原邀请/回执已失效，请同伴重新确认', {
        receiptId: receipt.id,
        receiptRevision: receipt.targetRevision,
        draftRevision: draft.targetRevision,
      });
    }
    const invitation = state.invitations[receipt.invitationId];
    if (!invitation || invitation.draftId !== draft.id || invitation.token === undefined) {
      throw new ServiceError(422, 'COMPANION_INVITATION_INVALID', '回执缺少对应的本单邀请');
    }
    if (invitation.status !== 'confirmed' || invitation.targetRevision !== draft.targetRevision) {
      throw new ServiceError(422, 'COMPANION_INVITATION_SUPERSEDED', '邀请已因更换报名对象而作废，必须重新邀请并确认');
    }
    return { receipt, invitation };
  }

  allocateSolo(state, cls, draft, effective, input) {
    const requestedStationId = input.stationId
      ?? input.requestedStationId
      ?? effective.stationId
      ?? effective.stationPreference
      ?? null;

    let station;
    if (requestedStationId !== null && requestedStationId !== undefined && requestedStationId !== '') {
      const resolvedStationId = resolveStationRef(requestedStationId, cls.stations);
      station = cls.stations.find((s) => s.id === resolvedStationId);
      if (!station) throw new ServiceError(400, 'STATION_NOT_FOUND', `台号不存在：${requestedStationId}`);
      if (state.occupancy[occKey(cls.id, station.id)]) {
        throw new ServiceError(409, 'STATION_TAKEN', '指定台位已被占走，已占台位不会因新表或草稿固定版本而回来', {
          stationId: station.id,
        });
      }
    } else {
      station = cls.stations.find((s) => !state.occupancy[occKey(cls.id, s.id)]);
      if (!station) throw new ServiceError(409, 'NO_STATION_AVAILABLE', '本班已无空台');
    }

    state.counters.enrollment += 1;
    const enrollmentNumber = `ENR-${new Date().getFullYear()}-${pad(state.counters.enrollment)}`;
    const occupiedAt = new Date().toISOString();
    const occupancy = {
      classId: cls.id,
      stationId: station.id,
      stationLabel: station.label,
      orderId: null,
      personName: effective.guestName,
      role: 'primary',
      enrollmentNumber,
      occupiedAt,
    };
    return { station, occupancy, enrollmentNumber };
  }

  allocatePair(state, cls, draft, effective, input, receipt) {
    const free = (stationId) => !state.occupancy[occKey(cls.id, stationId)];
    let group = null;

    if (input.pairGroupId) {
      group = cls.pairGroups.find((g) => g.id === input.pairGroupId);
      if (!group) throw new ServiceError(400, 'PAIR_GROUP_NOT_FOUND', '班级没有这个可结伴组');
      if (!group.stationIds.every(free)) {
        throw new ServiceError(409, 'PAIR_STATIONS_UNAVAILABLE', '指定结伴组中已有台位被散客或其他报名占走，台位不可重新分配', {
          pairGroupId: group.id,
          pairGroups: classView(state, cls).pairGroups,
        });
      }
    } else if (Array.isArray(input.stationIds) && input.stationIds.length === 2) {
      const requested = input.stationIds
        .map((reference) => resolveStationRef(reference, cls.stations))
        .filter(Boolean)
        .sort();
      group = requested.length === 2
        ? cls.pairGroups.find((candidate) =>
          [...candidate.stationIds].sort().every((id, i) => id === requested[i]))
        : null;
      if (!group) {
        throw new ServiceError(422, 'STATIONS_NOT_PAIRABLE', '这两个台号没有被班级公布为可结伴台位；即使相邻或另一台仍空也不能凑成双台', {
          requestedStationIds: input.stationIds,
          pairGroups: cls.pairGroups,
        });
      }
      if (!group.stationIds.every(free)) {
        throw new ServiceError(409, 'PAIR_STATIONS_UNAVAILABLE', '申请的可结伴台位并非两台都空', {
          pairGroupId: group.id,
          pairGroups: classView(state, cls).pairGroups,
        });
      }
    } else {
      group = cls.pairGroups.find((candidate) => candidate.stationIds.every(free)) ?? null;
      if (!group) {
        const view = classView(state, cls);
        throw new ServiceError(409, 'PAIR_STATIONS_UNAVAILABLE', '没有任何一组公布的相邻可结伴台位同时为空；单个空台不能凑成双人单', {
          freeStationIds: view.availableStationIds,
          pairGroups: view.pairGroups,
        });
      }
    }

    const occupiedAt = new Date().toISOString();
    const primaryName = effective.guestName;
    const companionName = receipt.companionName || effective.partnerName;
    if (!companionName) {
      throw new ServiceError(422, 'COMPANION_NAME_REQUIRED', '缺少实际就坐同伴姓名');
    }

    const people = [
      { role: 'primary', name: primaryName },
      { role: 'companion', name: companionName },
    ];
    const resultSeats = [];
    const enrollments = [];

    group.stationIds.forEach((stationId, index) => {
      const station = cls.stations.find((s) => s.id === stationId);
      state.counters.enrollment += 1;
      const enrollmentNumber = `ENR-${new Date().getFullYear()}-${pad(state.counters.enrollment)}`;
      enrollments.push(enrollmentNumber);
      resultSeats.push({
        stationId,
        stationLabel: station.label,
        role: people[index].role,
        personName: people[index].name,
        enrollmentNumber,
        occupiedAt,
      });
    });

    return { group, seats: resultSeats, enrollmentNumbers: enrollments };
  }

  async submitDraft(draftId, input = {}) {
    return this.tx((state) => {
      let draft = state.drafts[draftId];
      if (!draft) throw new ServiceError(404, 'DRAFT_NOT_FOUND', '找不到草稿');

      // A successful order is the durable idempotency anchor. Even if the
      // client omits the original Idempotency-Key after restart, retrying the
      // same draft returns the original seats instead of allocating again.
      if (draft.status === 'submitted' && draft.submittedOrderId) {
        const order = state.orders[draft.submittedOrderId];
        if (!order) throw new ServiceError(500, 'ORDER_INDEX_BROKEN', '草稿指向的成功单不存在');
        return { ok: true, replayed: true, replayReason: 'draft_already_submitted', order: counterOrderView(state, order) };
      }

      const idempotencyKey = input.idempotencyKey ?? null;
      if (idempotencyKey && state.idempotency[idempotencyKey]) {
        const record = state.idempotency[idempotencyKey];
        const requestHash = stableHash({
          draftId,
          stationId: input.stationId ?? null,
          stationIds: input.stationIds ?? null,
          pairGroupId: input.pairGroupId ?? null,
          companionReceiptId: input.companionReceiptId ?? draft.companionReceiptId ?? null,
        });
        if (record.requestHash !== requestHash) {
          throw new ServiceError(409, 'IDEMPOTENCY_KEY_CONFLICT', '同一幂等键被用于不同的递交请求');
        }
        const order = state.orders[record.orderId];
        return { ok: true, replayed: true, replayReason: 'idempotency_key', order: counterOrderView(state, order) };
      }

      if (isDraftExpired(draft)) throw new ServiceError(410, 'DRAFT_EXPIRED', '草稿已到期，不能递交');

      const formBeforePatch = getFormVersion(state, draft.formId, draft.formVersion);
      const directAnswerPatch = this.extractAnswerPatch(formBeforePatch, input);
      const hasSubmissionPatch = Object.keys(directAnswerPatch).length > 0
        || Boolean(input.answers)
        || Boolean(input.response)
        || Object.hasOwn(input, 'classId')
        || Object.hasOwn(input, 'companionReceiptId');

      // Allow a caller to save and submit in one call, including direct answer
      // keys such as { attendanceMode: 'solo', guestName: '...' }.
      if (hasSubmissionPatch) {
        draft = this.mutateDraftTx(state, draftId, input);
      }

      const form = getFormVersion(state, draft.formId, draft.formVersion);
      const result = validateDraftAnswers(draft, form);
      if (!result.valid) {
        throw new ServiceError(422, 'FORM_VALIDATION_FAILED', '当前可见题目尚未完整有效填写；隐藏旧答案不能充数', {
          errors: result.errors,
          staleAnswers: result.staleAnswers,
          effectiveAnswers: result.effectiveAnswers,
        });
      }
      const mode = result.mode;
      if (!['solo', 'pair'].includes(mode)) {
        throw new ServiceError(422, 'REGISTRATION_MODE_REQUIRED', '必须选择独自报名或双人结伴');
      }

      const cls = state.classes[draft.classId];
      if (!cls) throw new ServiceError(404, 'CLASS_NOT_FOUND', '班级/场次不存在');

      let receipt = null;
      let allocation;
      if (mode === 'pair') {
        const companion = this.validateCompanionForSubmit(state, draft, input);
        receipt = companion.receipt;
        allocation = this.allocatePair(state, cls, draft, result.effectiveAnswers, input, receipt);
      } else {
        allocation = this.allocateSolo(state, cls, draft, result.effectiveAnswers, input);
      }

      state.counters.order += 1;
      const orderId = input.orderId ?? uuidId('order');
      const orderNumber = `NS-${new Date().getFullYear()}-${pad(state.counters.order)}`;
      const nowIso = new Date().toISOString();

      let seats;
      let enrollmentNumbers;
      if (mode === 'pair') {
        seats = allocation.seats;
        enrollmentNumbers = allocation.enrollmentNumbers;
      } else {
        seats = [{
          stationId: allocation.station.id,
          stationLabel: allocation.station.label,
          role: 'primary',
          personName: result.effectiveAnswers.guestName,
          enrollmentNumber: allocation.enrollmentNumber,
          occupiedAt: nowIso,
        }];
        enrollmentNumbers = [allocation.enrollmentNumber];
      }

      const order = {
        id: orderId,
        orderNumber,
        kind: mode,
        status: 'confirmed_seats_fixed',
        policy: POLICY,
        classId: cls.id,
        draftId: draft.id,
        formId: form.formId,
        formVersion: form.version,
        formTitle: form.title,
        terms: structuredClone(form.terms),
        effectiveAnswers: structuredClone(result.effectiveAnswers),
        staleAnswersAtSubmit: structuredClone(result.staleAnswers),
        companionReceipt: receipt ? {
          id: receipt.id,
          invitationId: receipt.invitationId,
          companionName: receipt.companionName,
          confirmedAt: receipt.confirmedAt,
          targetDraftId: receipt.targetDraftId,
        } : null,
        people: seats.map((seat) => ({
          role: seat.role,
          name: seat.personName,
          enrollmentNumber: seat.enrollmentNumber,
        })),
        enrollmentNumbers,
        seats,
        seatAssignments: seats,
        idempotencyKey: idempotencyKey,
        createdAt: nowIso,
      };

      // Occupancy is written only after every validation passes, in the same
      // serialized transaction as the order and draft state.
      for (const seat of seats) {
        state.occupancy[occKey(cls.id, seat.stationId)] = {
          classId: cls.id,
          stationId: seat.stationId,
          stationLabel: seat.stationLabel,
          orderId: order.id,
          orderNumber: order.orderNumber,
          personName: seat.personName,
          role: seat.role,
          enrollmentNumber: seat.enrollmentNumber,
          occupiedAt: nowIso,
        };
      }

      state.orders[order.id] = order;
      draft.status = 'submitted';
      draft.submittedOrderId = order.id;
      draft.submittedAt = nowIso;
      draft.updatedAt = nowIso;

      if (idempotencyKey) {
        state.idempotency[idempotencyKey] = {
          key: idempotencyKey,
          orderId: order.id,
          draftId: draft.id,
          requestHash: stableHash({
            draftId,
            stationId: input.stationId ?? null,
            stationIds: input.stationIds ?? null,
            pairGroupId: input.pairGroupId ?? null,
            companionReceiptId: input.companionReceiptId ?? draft.companionReceiptId ?? null,
          }),
          createdAt: nowIso,
        };
      }

      return { ok: true, replayed: false, order: counterOrderView(state, order) };
    });
  }

  // ---------- Counter / queries ----------

  async getOrder(orderId) {
    return this.read((state) => {
      const order = state.orders[orderId];
      if (!order) throw new ServiceError(404, 'ORDER_NOT_FOUND', '找不到报名单');
      return counterOrderView(state, order);
    });
  }

  async listOrders() {
    return this.read((state) => ({
      ok: true,
      orders: Object.values(state.orders)
        .sort((a, b) => new Date(a.createdAt) - new Date(b.createdAt))
        .map((order) => ({
          id: order.id,
          orderNumber: order.orderNumber,
          kind: order.kind,
          classId: order.classId,
          formVersion: order.formVersion,
          enrollmentNumbers: order.enrollmentNumbers,
          people: order.people,
          seats: order.seats,
          createdAt: order.createdAt,
        })),
    }));
  }

  async findByEnrollment(enrollmentNumber) {
    return this.read((state) => {
      const order = Object.values(state.orders).find((candidate) =>
        candidate.enrollmentNumbers.includes(enrollmentNumber));
      if (!order) throw new ServiceError(404, 'ENROLLMENT_NOT_FOUND', '找不到这个入班号');
      return counterOrderView(state, order);
    });
  }
}
