'use strict';

const { HttpError } = require('./store');

const ITEM_CATALOG = {
  date: {
    code: 'date',
    name: '开放日期',
    requiredSkill: 'date',
    modelAnswer: '社区图书馆下周一上午九点开放。',
  },
  pickup: {
    code: 'pickup',
    name: '取件方式',
    requiredSkill: 'pickup',
    modelAnswer: '请凭取件码到北门服务台领取。',
  },
};

const ACTIVE_LEG_STATES = new Set(['ringing', 'ringback', 'connected', 'held']);

function nowIso() {
  return new Date().toISOString();
}

function normalizePhone(phone) {
  const normalized = String(phone || '').replace(/[^\d]/g, '');
  if (!normalized) throw new HttpError(400, 'invalid_phone', '必须提供来电号码');
  return normalized;
}

function newId(db, prefix) {
  db.seq += 1;
  return `${prefix}-${db.seq}`;
}

function checkRevision(db, expectedRevision) {
  if (expectedRevision === undefined || expectedRevision === null || expectedRevision === '') return;
  const expected = Number(expectedRevision);
  if (!Number.isInteger(expected)) {
    throw new HttpError(400, 'invalid_revision', 'expectedRevision 必须是整数');
  }
  if (expected !== db.revision) {
    throw new HttpError(409, 'stale_screen', '画面已过期：另一位坐席可能已经操作，请刷新后重试', {
      expectedRevision: expected,
      currentRevision: db.revision,
    });
  }
}

function touch(db, type, details = {}) {
  db.revision += 1;
  const event = {
    id: newId(db, 'evt'),
    revision: db.revision,
    at: nowIso(),
    type,
    ...details,
  };
  db.audit.push(event);
  if (db.audit.length > 5000) db.audit.splice(0, db.audit.length - 5000);
  return event;
}

function publicAgent(agent) {
  if (!agent) return null;
  return {
    id: agent.id,
    name: agent.name,
    extension: agent.extension,
    skills: [...(agent.skills || [])],
    onDuty: Boolean(agent.onDuty),
    presence: agent.presence || 'offline',
  };
}

function qualificationFor(agent, itemCode) {
  const item = ITEM_CATALOG[itemCode];
  if (!item) throw new HttpError(404, 'unknown_item', `未知事项 ${itemCode}`);
  if (!agent) {
    return {
      itemCode,
      requiredSkill: item.requiredSkill,
      qualified: false,
      agentId: null,
      onDuty: false,
      presence: null,
      hasSkill: false,
      reason: 'agent_not_found',
    };
  }
  const hasSkill = (agent.skills || []).includes(item.requiredSkill);
  const onDuty = Boolean(agent.onDuty);
  const activePresence = agent.presence !== 'offline';
  const qualified = hasSkill && onDuty && activePresence;
  return {
    itemCode,
    requiredSkill: item.requiredSkill,
    agentId: agent.id,
    onDuty,
    presence: agent.presence,
    hasSkill,
    activePresence,
    qualified,
    reason: qualified
      ? null
      : !hasSkill
        ? 'missing_skill'
        : !onDuty
          ? 'off_shift'
          : 'agent_offline',
  };
}

function freshItems() {
  return Object.fromEntries(
    Object.values(ITEM_CATALOG).map((item) => [
      item.code,
      {
        code: item.code,
        name: item.name,
        requiredSkill: item.requiredSkill,
        status: 'unasked', // unasked | open | answered
        questionText: null,
        askedAt: null,
        answerUtteranceId: null,
        answerText: null,
        answeredAt: null,
        answeredByAgentId: null,
        audioProof: null,
      },
    ]),
  );
}

function getCallerContext(db, phone) {
  const normalized = normalizePhone(phone);
  const caller = db.callers[normalized];
  if (!caller || !caller.activeCaseId) {
    throw new HttpError(404, 'no_active_case', `号码 ${normalized} 没有未结咨询台记录`, {
      callerNumber: normalized,
    });
  }
  const kase = db.cases[caller.activeCaseId];
  if (!kase) throw new HttpError(404, 'case_not_found', '工作台案件不存在');
  const call = db.calls[kase.primaryCallId];
  if (!call) throw new HttpError(404, 'call_not_found', '来电主支路不存在');
  return { phone: normalized, caller, kase, call, rootCallId: call.rootCallId || call.id };
}

function latestCallerContext(db, phone) {
  const normalized = normalizePhone(phone);
  const caller = db.callers[normalized];
  if (!caller) throw new HttpError(404, 'caller_not_found', `查不到号码 ${normalized}`);
  const caseId = caller.activeCaseId || caller.caseIds?.[caller.caseIds.length - 1];
  const kase = db.cases[caseId];
  if (!kase) throw new HttpError(404, 'case_not_found', '工作台案件不存在');
  const call = db.calls[kase.primaryCallId];
  return { phone: normalized, caller, kase, call, rootCallId: kase.primaryCallId };
}

function rootCalls(db, rootCallId) {
  return Object.values(db.calls).filter((call) => (call.rootCallId || call.id) === rootCallId);
}

function rootLegs(db, rootCallId) {
  const result = [];
  for (const call of rootCalls(db, rootCallId)) {
    for (const leg of call.legs || []) result.push({ call, leg });
  }
  return result;
}

function parseRef(ref) {
  const [callId, legId] = String(ref).split('/');
  return { callId, legId };
}

function resolveRef(db, ref) {
  const { callId, legId } = parseRef(ref);
  const call = db.calls[callId];
  const leg = call?.legs?.find((item) => item.id === legId);
  return call && leg ? { call, leg } : null;
}

function legRef(leg) {
  return `${leg.callId}/${leg.id}`;
}

function participantSummary(db, call, leg) {
  if (leg.participantType === 'caller') {
    return {
      ref: legRef(leg),
      type: 'caller',
      id: leg.participantId,
      label: `来电人 ${leg.participantId}`,
      callId: call.id,
      legId: leg.id,
      branchKind: call.kind,
      state: leg.state,
    };
  }
  const agent = db.agents?.[leg.participantId];
  return {
    ref: legRef(leg),
    type: 'agent',
    id: leg.participantId,
    label: agent ? `${agent.name}（${agent.id.toUpperCase()}）` : `坐席 ${leg.participantId}`,
    callId: call.id,
    legId: leg.id,
    branchKind: call.kind,
    state: leg.state,
  };
}

function computeAudio(db, rootCallId) {
  const rooms = Object.values(db.rooms).filter(
    (room) => room.rootCallId === rootCallId && !room.endedAt,
  );
  const audiblePairs = [];
  const whoHearsWhom = [];

  for (const room of rooms) {
    const present = room.legRefs
      .map((ref) => {
        const resolved = resolveRef(db, ref);
        if (!resolved) return null;
        const { call, leg } = resolved;
        if (leg.state !== 'connected' || leg.roomId !== room.id) return null;
        return { call, leg, participant: participantSummary(db, call, leg) };
      })
      .filter(Boolean);

    for (let i = 0; i < present.length; i += 1) {
      for (let j = i + 1; j < present.length; j += 1) {
        const a = present[i].participant;
        const b = present[j].participant;
        const pair = {
          roomId: room.id,
          roomKind: room.kind,
          a,
          b,
          bothCanHear: true,
          at: nowIso(),
        };
        audiblePairs.push(pair);
        whoHearsWhom.push({ listener: b, speaker: a, roomId: room.id, roomKind: room.kind });
        whoHearsWhom.push({ listener: a, speaker: b, roomId: room.id, roomKind: room.kind });
      }
    }
  }

  const legs = rootLegs(db, rootCallId).map(({ call, leg }) => participantSummary(db, call, leg));
  const callerLeg = rootLegs(db, rootCallId)
    .filter(({ leg }) => leg.participantType === 'caller')
    .map(({ leg }) => leg)
    .find(Boolean);

  const callerHears = {};
  const agentHearsCaller = {};
  for (const entry of whoHearsWhom) {
    if (entry.listener.type === 'caller' && entry.speaker.type === 'agent') {
      callerHears[entry.speaker.id] = true;
    }
    if (entry.listener.type === 'agent' && entry.speaker.type === 'caller') {
      agentHearsCaller[entry.listener.id] = true;
    }
  }

  return {
    at: nowIso(),
    rootCallId,
    revision: db.revision,
    activeRooms: rooms.map((room) => ({
      id: room.id,
      kind: room.kind,
      legRefs: room.legRefs,
      createdAt: room.createdAt,
    })),
    participants: legs,
    audiblePairs,
    whoHearsWhom,
    callerHears,
    agentHearsCaller,
    holdMusic: callerLeg?.state === 'held',
    callerState: callerLeg?.state || null,
  };
}

function recordAudioSnapshot(db, rootCallId, trigger, extra = {}) {
  const audio = computeAudio(db, rootCallId);
  const snapshot = {
    id: newId(db, 'snap'),
    revision: db.revision,
    at: nowIso(),
    trigger,
    ...extra,
    audio,
  };
  db.audioSnapshots.push(snapshot);
  if (db.audioSnapshots.length > 2000) {
    db.audioSnapshots.splice(0, db.audioSnapshots.length - 2000);
  }
  return snapshot;
}

function createRoom(db, rootCallId, kind, refs) {
  const room = {
    id: newId(db, 'room'),
    rootCallId,
    kind,
    legRefs: refs,
    createdAt: nowIso(),
    endedAt: null,
  };
  db.rooms[room.id] = room;
  for (const ref of refs) {
    const resolved = resolveRef(db, ref);
    if (resolved) resolved.leg.roomId = room.id;
  }
  return room;
}

function endRoom(db, roomId) {
  const room = db.rooms[roomId];
  if (!room || room.endedAt) return room || null;
  room.endedAt = nowIso();
  for (const ref of room.legRefs) {
    const resolved = resolveRef(db, ref);
    if (resolved && resolved.leg.roomId === room.id) resolved.leg.roomId = null;
  }
  return room;
}

function recomputeCallState(call) {
  if (!call) return;
  const active = call.legs.filter((leg) => ACTIVE_LEG_STATES.has(leg.state));
  if (active.length === 0) {
    call.state = 'ended';
    call.endedAt = call.endedAt || nowIso();
  } else if (active.some((leg) => leg.state === 'connected')) {
    call.state = 'active';
  } else if (active.some((leg) => leg.state === 'ringing' || leg.state === 'ringback')) {
    call.state = 'ringing';
  } else {
    call.state = 'held';
  }
}

function findConnectedAgentLeg(db, rootCallId, agentId) {
  const candidates = rootLegs(db, rootCallId).filter(({ leg }) => {
    return (
      leg.participantType === 'agent' &&
      leg.participantId === agentId &&
      leg.state === 'connected' &&
      leg.roomId &&
      db.rooms[leg.roomId] &&
      !db.rooms[leg.roomId].endedAt
    );
  });
  return candidates[candidates.length - 1] || null;
}

function findActiveConsult(db, rootCallId) {
  return (
    Object.values(db.calls)
      .filter(
        (call) =>
          call.kind === 'consult' &&
          (call.rootCallId || call.parentCallId) === rootCallId &&
          ['ringing', 'active'].includes(call.state),
      )
      .sort((a, b) => b.seq - a.seq)[0] || null
  );
}

function findLatestConsult(db, rootCallId) {
  return (
    Object.values(db.calls)
      .filter((call) => call.kind === 'consult' && (call.rootCallId || call.id) === rootCallId)
      .sort((a, b) => b.id.localeCompare(a.id, undefined, { numeric: true }))[0] || null
  );
}

function ensureHandover(db, kase, rootCallId, fromAgentId, toAgentId) {
  let handover = kase.currentHandoverId ? db.handovers[kase.currentHandoverId] : null;
  if (!handover) {
    handover = {
      id: newId(db, 'ho'),
      caseId: kase.id,
      rootCallId,
      type: 'accompanied',
      mode: '陪同转接',
      fromAgentId,
      toAgentId,
      status: 'initiated',
      createdAt: nowIso(),
      completedAt: null,
      phases: [],
      ticketOwnership: {
        currentHolderAgentId: kase.ticketHolderAgentId,
        returnableAgentId: kase.returnableAgentId,
      },
      responsibility: {
        currentReturnableAgentId: kase.returnableAgentId,
      },
      qualificationAtCompletion: null,
      audioSnapshotIdAtCompletion: null,
    };
    db.handovers[handover.id] = handover;
    kase.handoverIds.push(handover.id);
    kase.currentHandoverId = handover.id;
  }
  return handover;
}

function addHandoverPhase(db, handover, phase, details = {}) {
  const entry = { phase, at: nowIso(), revision: db.revision, ...details };
  handover.phases.push(entry);
  handover.latestPhase = phase;
  // “completed” is the aggregate lifecycle state; later departure is an audit phase.
  if (handover.status !== 'completed') handover.status = phase;
  return entry;
}

function openItemCodes(kase) {
  return Object.values(kase.items)
    .filter((item) => item.status === 'open')
    .map((item) => item.code);
}

function responsibilityView(db, kase) {
  const open = openItemCodes(kase);
  const owner = db.agents[kase.returnableAgentId] || null;
  const itemQualifications = Object.fromEntries(
    open.map((code) => [code, qualificationFor(owner, code)]),
  );
  const eligibleAgentIds = Object.values(db.agents)
    .filter((agent) => open.every((code) => qualificationFor(agent, code).qualified))
    .map((agent) => agent.id);
  const ownerQualified = open.every((code) => qualificationFor(owner, code).qualified);
  return {
    returnableAgentId: kase.returnableAgentId,
    returnableAgent: publicAgent(owner),
    ticketHolderAgentId: kase.ticketHolderAgentId,
    openItemCodes: open,
    ownerQualifiedForOpenItems: ownerQualified,
    itemQualifications,
    eligibleAgentIds,
    fallbackQueue: kase.fallbackQueue,
    promise: '未解答事项总有人接；若当前接办人失格，工作台立即显示资格缺口并由值班台/合格坐席接棒',
  };
}

function timelineForCaller(db, phone) {
  const normalized = normalizePhone(phone);
  const caller = db.callers[normalized];
  const caseIds = new Set(caller?.caseIds || []);
  const callIds = new Set(
    Object.values(db.calls)
      .filter((call) => caseIds.has(call.caseId))
      .map((call) => call.id),
  );
  return db.audit.filter((event) => {
    return (
      event.callerNumber === normalized ||
      (event.caseId && caseIds.has(event.caseId)) ||
      (event.callId && callIds.has(event.callId)) ||
      (event.rootCallId && callIds.has(event.rootCallId))
    );
  });
}

function callerDetail(db, phone) {
  const normalized = normalizePhone(phone);
  const caller = db.callers[normalized];
  if (!caller) throw new HttpError(404, 'caller_not_found', `查不到号码 ${normalized}`);
  const cases = caller.caseIds.map((id) => db.cases[id]).filter(Boolean);
  const active = caller.activeCaseId ? db.cases[caller.activeCaseId] : null;
  const primaryCall = active ? db.calls[active.primaryCallId] : null;
  const handovers = cases.flatMap((kase) =>
    (kase.handoverIds || []).map((id) => db.handovers[id]).filter(Boolean),
  );
  return {
    callerNumber: normalized,
    activeCaseId: caller.activeCaseId,
    activeCase: active,
    responsibility: active ? responsibilityView(db, active) : null,
    cases,
    calls: cases.flatMap((kase) =>
      Object.values(db.calls).filter((call) => call.caseId === kase.id),
    ),
    handovers,
    utterances: Object.values(db.utterances).filter((u) => cases.some((k) => k.id === u.caseId)),
    timeline: timelineForCaller(db, normalized),
    audio: primaryCall ? computeAudio(db, primaryCall.rootCallId || primaryCall.id) : null,
    queriedAt: nowIso(),
    revision: db.revision,
  };
}

/* ------------------------------ operations ------------------------------ */

function setQualification(db, agentId, skill, granted, expectedRevision) {
  checkRevision(db, expectedRevision);
  const agent = db.agents[agentId];
  if (!agent) throw new HttpError(404, 'agent_not_found', `没有坐席 ${agentId}`);
  const normalizedSkill = ITEM_CATALOG[skill]?.requiredSkill || skill;
  const before = [...agent.skills];
  const set = new Set(agent.skills || []);
  if (granted) set.add(normalizedSkill);
  else set.delete(normalizedSkill);
  agent.skills = [...set];
  const event = touch(db, granted ? 'agent.qualification_granted' : 'agent.qualification_revoked', {
    agentId,
    skill: normalizedSkill,
    before,
    after: [...agent.skills],
    audioPathChanged: false,
  });
  return {
    agent: publicAgent(agent),
    audioPathChanged: false,
    callPathNotice: '班次资格只影响工作台办结/接办资格，不会挂断或改变当前通话支路',
    event,
    revision: db.revision,
  };
}

function setPresence(db, agentId, presence, expectedRevision) {
  checkRevision(db, expectedRevision);
  const agent = db.agents[agentId];
  if (!agent) throw new HttpError(404, 'agent_not_found', `没有坐席 ${agentId}`);
  if (!['available', 'busy', 'offline'].includes(presence)) {
    throw new HttpError(400, 'invalid_presence', 'presence 必须是 available/busy/offline');
  }
  const before = agent.presence;
  agent.presence = presence;
  const event = touch(db, 'agent.presence_changed', { agentId, before, after: presence });
  return { agent: publicAgent(agent), event, revision: db.revision };
}

function inbound(db, args) {
  checkRevision(db, args.expectedRevision);
  const phone = normalizePhone(args.callerNumber || args.phone);
  const existing = db.callers[phone];
  if (existing?.activeCaseId && !args.allowActive) {
    const kase = db.cases[existing.activeCaseId];
    const primary = db.calls[kase.primaryCallId];
    if (primary && ['ringing', 'active', 'held'].includes(primary.state)) {
      throw new HttpError(409, 'active_call_exists', '该号码已有进行中的来电支路', {
        callerNumber: phone,
        callId: primary.id,
        caseId: kase.id,
        currentRevision: db.revision,
      });
    }
  }

  const kaseId = newId(db, 'case');
  const kase = {
    id: kaseId,
    callerNumber: phone,
    status: 'open',
    createdAt: nowIso(),
    resolvedAt: null,
    primaryCallId: null,
    ticketHolderAgentId: 'a',
    returnableAgentId: 'a',
    fallbackQueue: 'community-hotline-desk',
    workOrder: {
      id: `WO-${db.seq + 1}`,
      holderAgentId: 'a',
      status: 'open',
    },
    items: freshItems(),
    handoverIds: [],
    currentHandoverId: null,
  };
  db.cases[kaseId] = kase;

  if (!db.callers[phone]) db.callers[phone] = { phone, caseIds: [], activeCaseId: null };
  db.callers[phone].caseIds.push(kaseId);
  db.callers[phone].activeCaseId = kaseId;

  const callId = newId(db, 'call');
  const callerLegId = newId(db, 'leg');
  const agentLegId = newId(db, 'leg');
  const call = {
    id: callId,
    caseId: kaseId,
    rootCallId: callId,
    parentCallId: null,
    kind: 'inbound',
    state: 'ringing',
    providerCallId: `prov-call-${callId}`,
    callerNumber: phone,
    dialedNumber: 'community-hotline',
    createdAt: nowIso(),
    connectedAt: null,
    endedAt: null,
    failureReason: null,
    legs: [
      {
        id: callerLegId,
        callId,
        providerLegId: `prov-${callerLegId}`,
        participantType: 'caller',
        participantId: phone,
        label: `来电人 ${phone}`,
        direction: 'inbound',
        state: 'ringing',
        roomId: null,
        stateHistory: [{ state: 'ringing', at: nowIso() }],
      },
      {
        id: agentLegId,
        callId,
        providerLegId: `prov-${agentLegId}`,
        participantType: 'agent',
        participantId: args.agentId || 'a',
        label: '甲',
        direction: 'internal',
        state: 'ringing',
        roomId: null,
        stateHistory: [{ state: 'ringing', at: nowIso() }],
      },
    ],
  };
  db.calls[callId] = call;
  kase.primaryCallId = callId;
  kase.workOrder.id = `WO-${kaseId}`;

  const event = touch(db, 'call.inbound_created', {
    callerNumber: phone,
    caseId: kaseId,
    callId,
    agentId: args.agentId || 'a',
  });
  const snapshot = recordAudioSnapshot(db, callId, 'inbound_created', { eventId: event.id });
  return {
    case: kase,
    call,
    audioSnapshot: snapshot,
    audio: snapshot.audio,
    event,
    revision: db.revision,
  };
}

function answerInbound(db, args) {
  checkRevision(db, args.expectedRevision);
  const ctx = getCallerContext(db, args.callerNumber || args.phone);
  const agentId = args.agentId || 'a';
  const agentLeg = ctx.call.legs.find(
    (leg) => leg.participantType === 'agent' && leg.participantId === agentId,
  );
  if (!agentLeg) throw new HttpError(404, 'agent_leg_not_found', '找不到该坐席的来电支路');
  if (ctx.call.state === 'ended') throw new HttpError(409, 'call_ended', '来电主支路已结束');

  const callerLeg = ctx.call.legs.find((leg) => leg.participantType === 'caller');
  for (const leg of [callerLeg, agentLeg]) {
    leg.state = 'connected';
    leg.connectedAt = nowIso();
    leg.stateHistory.push({ state: 'connected', at: leg.connectedAt });
  }
  ctx.call.state = 'active';
  ctx.call.connectedAt = nowIso();
  createRoom(db, ctx.rootCallId, 'customer-bridge', [legRef(callerLeg), legRef(agentLeg)]);

  const event = touch(db, 'call.answered', {
    callerNumber: ctx.phone,
    caseId: ctx.kase.id,
    callId: ctx.call.id,
    rootCallId: ctx.rootCallId,
    agentId,
  });
  const snapshot = recordAudioSnapshot(db, ctx.rootCallId, 'inbound_answered', { eventId: event.id });
  return {
    case: ctx.kase,
    call: ctx.call,
    audio: snapshot.audio,
    audioSnapshot: snapshot,
    event,
    revision: db.revision,
  };
}

function callerAsk(db, args) {
  checkRevision(db, args.expectedRevision);
  const ctx = getCallerContext(db, args.callerNumber || args.phone);
  const itemCode = args.itemCode;
  const item = ctx.kase.items[itemCode];
  if (!item) throw new HttpError(404, 'unknown_item', `未知事项 ${itemCode}`);

  const callerLeg = ctx.call.legs.find((leg) => leg.participantType === 'caller');
  if (!callerLeg || callerLeg.state !== 'connected' || !callerLeg.roomId) {
    throw new HttpError(409, 'caller_not_live', '来电人当前不在可对话通路中（可能在等待音乐或已挂机）');
  }

  item.status = 'open';
  item.questionText = args.text || `询问${item.name}`;
  item.askedAt = nowIso();

  const utterance = {
    id: newId(db, 'utt'),
    caseId: ctx.kase.id,
    rootCallId: ctx.rootCallId,
    callId: ctx.call.id,
    legId: callerLeg.id,
    roomId: callerLeg.roomId,
    speakerType: 'caller',
    speakerAgentId: null,
    text: item.questionText,
    itemCode,
    at: nowIso(),
    audibleToCaller: false,
    audienceRefs: [],
    qualification: null,
    formalAnswer: false,
    blockers: [],
  };
  db.utterances[utterance.id] = utterance;

  const event = touch(db, 'caller.asked', {
    callerNumber: ctx.phone,
    caseId: ctx.kase.id,
    callId: ctx.call.id,
    itemCode,
    utteranceId: utterance.id,
  });
  return { item, utterance, event, revision: db.revision };
}

function agentSpeak(db, args) {
  checkRevision(db, args.expectedRevision);
  const ctx = getCallerContext(db, args.callerNumber || args.phone);
  const agentId = args.agentId;
  const agent = db.agents[agentId];
  if (!agent) throw new HttpError(404, 'agent_not_found', `没有坐席 ${agentId}`);
  if (!args.text || !String(args.text).trim()) {
    throw new HttpError(400, 'text_required', '说话内容不能为空');
  }

  const located = findConnectedAgentLeg(db, ctx.rootCallId, agentId);
  if (!located) {
    throw new HttpError(409, 'agent_not_on_audio', `${agent.name}当前没有已接通的通话支路`);
  }
  const { call: speakerCall, leg: speakerLeg } = located;
  const room = db.rooms[speakerLeg.roomId];
  const audioBefore = computeAudio(db, ctx.rootCallId);
  const audience = (room.legRefs || [])
    .map((ref) => resolveRef(db, ref))
    .filter(Boolean)
    .filter(({ leg }) => leg.id !== speakerLeg.id && leg.state === 'connected');
  const callerInRoom = audience.find(({ leg }) => leg.participantType === 'caller');
  const audibleToCaller = Boolean(callerInRoom);

  const itemCode = args.itemCode || null;
  const item = itemCode ? ctx.kase.items[itemCode] : null;
  if (itemCode && !item) throw new HttpError(404, 'unknown_item', `未知事项 ${itemCode}`);
  const qualification = itemCode ? qualificationFor(agent, item.requiredSkill) : null;
  const blockers = [];
  if (item) {
    if (item.status === 'answered') blockers.push('item_already_answered');
    if (item.status === 'unasked') blockers.push('item_not_asked_by_caller');
    if (!audibleToCaller) blockers.push('utterance_not_audible_to_caller');
    if (!qualification.qualified) blockers.push('agent_not_qualified_at_delivery');
  }
  const formalAnswer = Boolean(
    item &&
      item.status === 'open' &&
      audibleToCaller &&
      qualification.qualified,
  );

  const utterance = {
    id: newId(db, 'utt'),
    caseId: ctx.kase.id,
    rootCallId: ctx.rootCallId,
    callId: speakerCall.id,
    legId: speakerLeg.id,
    roomId: room.id,
    roomKind: room.kind,
    speakerType: 'agent',
    speakerAgentId: agentId,
    text: String(args.text),
    itemCode,
    at: nowIso(),
    revision: db.revision + 1,
    audienceRefs: audience.map(({ call, leg }) => legRef(leg)),
    audibleToCaller,
    audienceAgentIds: audience
      .filter(({ leg }) => leg.participantType === 'agent')
      .map(({ leg }) => leg.participantId),
    qualification,
    formalAnswer,
    blockers,
  };
  db.utterances[utterance.id] = utterance;

  if (formalAnswer) {
    item.status = 'answered';
    item.answerUtteranceId = utterance.id;
    item.answerText = utterance.text;
    item.answeredAt = utterance.at;
    item.answeredByAgentId = agentId;
    item.audioProof = {
      utteranceId: utterance.id,
      roomId: room.id,
      roomKind: room.kind,
      speakerRef: legRef(speakerLeg),
      callerRef: legRef(callerInRoom.leg),
      audienceRefs: utterance.audienceRefs,
      deliveredAudible: true,
      qualificationAtDelivery: qualification,
    };
  }

  const event = touch(db, formalAnswer ? 'answer.delivered' : 'agent.spoke_without_formal_answer', {
    callerNumber: ctx.phone,
    caseId: ctx.kase.id,
    callId: ctx.rootCallId,
    rootCallId: ctx.rootCallId,
    agentId,
    itemCode,
    utteranceId: utterance.id,
    audibleToCaller,
    formalAnswer,
    blockers,
  });
  utterance.revision = db.revision;
  const snapshot = recordAudioSnapshot(db, ctx.rootCallId, 'agent_spoke', {
    utteranceId: utterance.id,
    audibleToCaller,
  });

  return {
    utterance,
    item: item || null,
    formalAnswer,
    blockers,
    audio: snapshot.audio,
    audioBefore,
    event,
    revision: db.revision,
  };
}

function closeItemFromUtterance(db, args) {
  checkRevision(db, args.expectedRevision);
  const ctx = getCallerContext(db, args.callerNumber || args.phone);
  const { itemCode, utteranceId } = args;
  const item = ctx.kase.items[itemCode];
  if (!item) throw new HttpError(404, 'unknown_item', `未知事项 ${itemCode}`);
  const utterance = db.utterances[utteranceId];
  if (!utterance || utterance.caseId !== ctx.kase.id) {
    throw new HttpError(404, 'utterance_not_found', '找不到可用于办结的原话记录');
  }
  if (item.status === 'answered') {
    return { item, alreadyAnswered: true, revision: db.revision };
  }

  const blockers = [];
  if (utterance.itemCode !== itemCode) blockers.push('utterance_item_mismatch');
  if (args.agentId && utterance.speakerAgentId !== args.agentId) {
    blockers.push('not_original_speaker');
  }
  if (!utterance.audibleToCaller) blockers.push('utterance_not_audible_to_caller');
  if (!utterance.qualification?.qualified) blockers.push('agent_not_qualified_at_delivery');
  const currentAgent = db.agents[utterance.speakerAgentId];
  const currentQ = qualificationFor(currentAgent, item.requiredSkill);
  if (!currentQ.qualified) blockers.push('agent_not_qualified_now');

  if (blockers.length) {
    throw new HttpError(409, 'answer_cannot_close_item', '这句话不能补办为已答复', {
      blockers,
      utterance,
      itemCode,
      currentQualification: currentQ,
      pathCanRemainOpen: true,
      currentRevision: db.revision,
    });
  }

  item.status = 'answered';
  item.answerUtteranceId = utterance.id;
  item.answerText = utterance.text;
  item.answeredAt = nowIso();
  item.answeredByAgentId = utterance.speakerAgentId;
  item.audioProof = {
    utteranceId: utterance.id,
    acceptedManually: true,
    deliveredAudible: true,
    qualificationAtDelivery: utterance.qualification,
  };
  const event = touch(db, 'answer.accepted_from_utterance', {
    callerNumber: ctx.phone,
    caseId: ctx.kase.id,
    itemCode,
    utteranceId,
  });
  return { item, utterance, event, revision: db.revision };
}

function holdCaller(db, args) {
  checkRevision(db, args.expectedRevision);
  const ctx = getCallerContext(db, args.callerNumber || args.phone);
  const callerLeg = ctx.call.legs.find((leg) => leg.participantType === 'caller');
  if (!callerLeg || callerLeg.state !== 'connected') {
    throw new HttpError(409, 'caller_not_connected', '来电人当前不在通话中');
  }
  const oldRoomId = callerLeg.roomId;
  if (oldRoomId) endRoom(db, oldRoomId);

  callerLeg.state = 'held';
  callerLeg.roomId = null;
  callerLeg.stateHistory.push({ state: 'held', at: nowIso() });

  const agentId = args.agentId || ctx.kase.returnableAgentId || 'a';
  const agentLeg = ctx.call.legs.find(
    (leg) => leg.participantType === 'agent' && leg.participantId === agentId,
  );
  if (agentLeg && agentLeg.state === 'connected') {
    agentLeg.state = 'held';
    agentLeg.roomId = null;
    agentLeg.stateHistory.push({ state: 'held_for_consult', at: nowIso() });
  }

  createRoom(db, ctx.rootCallId, 'hold-music', [legRef(callerLeg)]);
  ctx.call.state = 'held';
  const event = touch(db, 'caller.placed_on_hold', {
    callerNumber: ctx.phone,
    caseId: ctx.kase.id,
    callId: ctx.call.id,
    rootCallId: ctx.rootCallId,
    agentId,
    holdMusic: true,
  });
  const snapshot = recordAudioSnapshot(db, ctx.rootCallId, 'caller_hold', { eventId: event.id });
  return { call: ctx.call, audio: snapshot.audio, event, revision: db.revision };
}

function retrieveCaller(db, args) {
  checkRevision(db, args.expectedRevision);
  const ctx = getCallerContext(db, args.callerNumber || args.phone);
  const activeConsult = findActiveConsult(db, ctx.rootCallId);
  if (activeConsult) {
    throw new HttpError(409, 'active_consult_branch', '咨询支路仍在接通中；先结束它再取回来电人', {
      consultCallId: activeConsult.id,
    });
  }
  const agentId = args.agentId || 'a';
  const callerLeg = ctx.call.legs.find((leg) => leg.participantType === 'caller');
  const agentLeg = ctx.call.legs.find(
    (leg) => leg.participantType === 'agent' && leg.participantId === agentId,
  );
  if (!callerLeg) throw new HttpError(404, 'caller_leg_not_found');
  if (callerLeg.roomId) endRoom(db, callerLeg.roomId);
  callerLeg.state = 'connected';
  callerLeg.roomId = null;
  callerLeg.stateHistory.push({ state: 'retrieved', at: nowIso() });
  if (agentLeg) {
    agentLeg.state = 'connected';
    agentLeg.roomId = null;
  }
  createRoom(db, ctx.rootCallId, 'customer-bridge', [legRef(callerLeg), legRef(agentLeg)].filter(Boolean));
  ctx.call.state = 'active';
  const event = touch(db, 'caller.retrieved', {
    callerNumber: ctx.phone,
    caseId: ctx.kase.id,
    callId: ctx.call.id,
    agentId,
  });
  const snapshot = recordAudioSnapshot(db, ctx.rootCallId, 'caller_retrieved');
  return { call: ctx.call, audio: snapshot.audio, event, revision: db.revision };
}

function startConsult(db, args) {
  checkRevision(db, args.expectedRevision);
  const ctx = getCallerContext(db, args.callerNumber || args.phone);
  const fromAgentId = args.fromAgentId || args.agentId || 'a';
  const targetAgentId = args.targetAgentId || args.toAgentId || 'b';
  const target = db.agents[targetAgentId];
  if (!target) throw new HttpError(404, 'agent_not_found', `没有目标坐席 ${targetAgentId}`);
  if (findActiveConsult(db, ctx.rootCallId)) {
    throw new HttpError(409, 'consult_exists', '已经有一条进行中的咨询支路');
  }

  const callerLeg = ctx.call.legs.find((leg) => leg.participantType === 'caller');
  if (callerLeg?.state !== 'held' && !args.allowWithoutHold) {
    throw new HttpError(409, 'caller_not_on_hold', '先把来电人置于等待音乐，再另拨咨询电话');
  }

  const callId = newId(db, 'call');
  const fromLegId = newId(db, 'leg');
  const targetLegId = newId(db, 'leg');
  const simulatedFailure = args.simulate || null;
  const targetBusy = target.presence === 'busy' || simulatedFailure === 'busy';
  const noAnswer = simulatedFailure === 'no-answer' || target.presence === 'offline';

  const consult = {
    id: callId,
    seq: db.seq,
    caseId: ctx.kase.id,
    rootCallId: ctx.rootCallId,
    parentCallId: ctx.rootCallId,
    kind: 'consult',
    state: targetBusy || noAnswer ? 'failed' : 'ringing',
    providerCallId: `prov-call-${callId}`,
    dialedNumber: target.extension,
    targetAgentId,
    fromAgentId,
    createdAt: nowIso(),
    connectedAt: null,
    endedAt: targetBusy || noAnswer ? nowIso() : null,
    failureReason: targetBusy ? 'busy' : noAnswer ? 'no-answer' : null,
    legs: [
      {
        id: fromLegId,
        callId,
        providerLegId: `prov-${fromLegId}`,
        participantType: 'agent',
        participantId: fromAgentId,
        direction: 'outbound-consult',
        state: targetBusy || noAnswer ? 'disconnected' : 'ringback',
        roomId: null,
        stateHistory: [{ state: targetBusy ? 'busy-tone' : noAnswer ? 'no-answer' : 'ringback', at: nowIso() }],
      },
      {
        id: targetLegId,
        callId,
        providerLegId: `prov-${targetLegId}`,
        participantType: 'agent',
        participantId: targetAgentId,
        direction: 'inbound-consult',
        state: targetBusy ? 'busy' : noAnswer ? 'failed' : 'ringing',
        roomId: null,
        stateHistory: [{ state: targetBusy ? 'busy' : noAnswer ? 'no-answer' : 'ringing', at: nowIso() }],
      },
    ],
  };
  db.calls[callId] = consult;

  if (targetBusy || noAnswer) {
    const event = touch(db, 'consult.failed', {
      callerNumber: ctx.phone,
      caseId: ctx.kase.id,
      callId,
      rootCallId: ctx.rootCallId,
      fromAgentId,
      targetAgentId,
      reason: consult.failureReason,
      originalOwnerRemains: ctx.kase.returnableAgentId,
    });
    recordAudioSnapshot(db, ctx.rootCallId, 'consult_failed', { consultCallId: callId });
    const error = new HttpError(
      targetBusy ? 409 : 504,
      targetBusy ? 'target_agent_busy' : 'consult_no_answer',
      targetBusy ? '接班坐席忙线，仍由原接办人跟进' : '咨询电话无人接听，仍由原接办人跟进',
      { consult, event, persist: true, currentRevision: db.revision },
    );
    throw error;
  }

  const event = touch(db, 'consult.started', {
    callerNumber: ctx.phone,
    caseId: ctx.kase.id,
    callId,
    rootCallId: ctx.rootCallId,
    fromAgentId,
    targetAgentId,
  });
  return { consult, call: consult, event, revision: db.revision };
}

function answerConsult(db, args) {
  checkRevision(db, args.expectedRevision);
  const ctx = getCallerContext(db, args.callerNumber || args.phone);
  const agentId = args.agentId || 'b';
  let consult = args.consultCallId ? db.calls[args.consultCallId] : findLatestConsult(db, ctx.rootCallId);
  if (!consult || consult.kind !== 'consult') {
    throw new HttpError(404, 'consult_not_found', '找不到另拨的咨询支路');
  }
  const targetLeg = consult.legs.find(
    (leg) => leg.participantType === 'agent' && leg.participantId === agentId,
  );
  const fromLeg = consult.legs.find(
    (leg) => leg.participantType === 'agent' && leg.participantId !== agentId,
  );
  if (!targetLeg || !fromLeg) throw new HttpError(404, 'consult_leg_not_found', '咨询支路不完整');
  if (consult.state === 'failed' || consult.state === 'ended') {
    throw new HttpError(409, 'consult_unavailable', '咨询支路已失败或结束', { consult });
  }

  for (const leg of [targetLeg, fromLeg]) {
    leg.state = 'connected';
    leg.connectedAt = nowIso();
    leg.stateHistory.push({ state: 'connected', at: leg.connectedAt });
  }
  consult.state = 'active';
  consult.connectedAt = nowIso();
  createRoom(db, ctx.rootCallId, 'private-consult', [legRef(fromLeg), legRef(targetLeg)]);

  const handover = ensureHandover(db, ctx.kase, ctx.rootCallId, fromLeg.participantId, agentId);
  if (!handover.phases.some((p) => p.phase === 'private_consult_connected')) {
    touch(db, 'consult.answered', {
      callerNumber: ctx.phone,
      caseId: ctx.kase.id,
      callId: consult.id,
      rootCallId: ctx.rootCallId,
      agentId,
    });
    addHandoverPhase(db, handover, 'private_consult_connected', {
      consultCallId: consult.id,
      roomKind: 'private-consult',
    });
  }

  const snapshot = recordAudioSnapshot(db, ctx.rootCallId, 'consult_answered', {
    consultCallId: consult.id,
  });
  return { consult, audio: snapshot.audio, handover, revision: db.revision };
}

function assignTicket(db, args) {
  checkRevision(db, args.expectedRevision);
  const ctx = getCallerContext(db, args.callerNumber || args.phone);
  const fromAgentId = args.fromAgentId || 'a';
  const toAgentId = args.toAgentId || args.agentId || 'b';
  const target = db.agents[toAgentId];
  if (!target) throw new HttpError(404, 'agent_not_found', '找不到接班坐席');
  const consult = findLatestConsult(db, ctx.rootCallId);
  if (!consult || consult.state !== 'active') {
    throw new HttpError(409, 'private_consult_required', '陪同转接要求原接办人先与新坐席私聊');
  }

  const open = openItemCodes(ctx.kase);
  const qualifications = Object.fromEntries(open.map((code) => [code, qualificationFor(target, code)]));
  const missing = open.filter((code) => !qualifications[code].qualified);
  if (missing.length) {
    throw new HttpError(409, 'target_not_qualified_for_open_items', '接班坐席不具备所有未答事项的资格', {
      missing,
      qualifications,
      audioPathChanged: false,
      currentRevision: db.revision,
    });
  }

  const beforeHolder = ctx.kase.ticketHolderAgentId;
  ctx.kase.ticketHolderAgentId = toAgentId;
  ctx.kase.workOrder.holderAgentId = toAgentId;
  const handover = ensureHandover(db, ctx.kase, ctx.rootCallId, fromAgentId, toAgentId);
  handover.ticketOwnership = {
    beforeHolderAgentId: beforeHolder,
    currentHolderAgentId: toAgentId,
    returnableAgentId: ctx.kase.returnableAgentId,
    changedAt: nowIso(),
    changedRevision: db.revision + 1,
  };

  const event = touch(db, 'work_order.assigned_for_handover', {
    callerNumber: ctx.phone,
    caseId: ctx.kase.id,
    rootCallId: ctx.rootCallId,
    fromAgentId,
    toAgentId,
    beforeHolder,
    openItemCodes: open,
    qualifications,
  });
  handover.ticketOwnership.changedRevision = db.revision;
  addHandoverPhase(db, handover, 'ticket_assigned_privately', {
    eventId: event.id,
    ticketHolderAgentId: toAgentId,
    returnableAgentId: ctx.kase.returnableAgentId,
    note: '工单归属可变；可回访责任人仍为原接办人，直到连通关系与资格同时核对通过',
  });
  return { case: ctx.kase, handover, responsibility: responsibilityView(db, ctx.kase), event, revision: db.revision };
}

function bridgeToCaller(db, args) {
  checkRevision(db, args.expectedRevision);
  const ctx = getCallerContext(db, args.callerNumber || args.phone);
  const consult = findActiveConsult(db, ctx.rootCallId) || findLatestConsult(db, ctx.rootCallId);
  if (!consult || consult.kind !== 'consult' || consult.state !== 'active') {
    throw new HttpError(409, 'active_consult_required', '需要先接通甲乙私聊咨询支路');
  }
  const privateRoom = Object.values(db.rooms).find(
    (room) => room.rootCallId === ctx.rootCallId && room.kind === 'private-consult' && !room.endedAt,
  );
  if (!privateRoom) throw new HttpError(409, 'private_room_not_found', '私聊房间不存在');

  const callerLeg = ctx.call.legs.find((leg) => leg.participantType === 'caller');
  if (!callerLeg) throw new HttpError(404, 'caller_leg_not_found');
  const consultLegs = consult.legs.filter((leg) => leg.state === 'connected');
  if (consultLegs.length < 2) throw new HttpError(409, 'consult_not_fully_connected');

  if (callerLeg.roomId) endRoom(db, callerLeg.roomId);
  endRoom(db, privateRoom.id);

  const refs = [legRef(callerLeg), ...consultLegs.map((leg) => legRef(leg))];
  callerLeg.state = 'connected';
  callerLeg.stateHistory.push({ state: 'bridged_to_agents', at: nowIso() });
  for (const leg of consultLegs) {
    leg.state = 'connected';
    leg.stateHistory.push({ state: 'bridged_to_caller', at: nowIso() });
  }
  createRoom(db, ctx.rootCallId, 'accompanied-bridge', refs);
  ctx.call.state = 'active';

  const fromAgentId = args.fromAgentId || consult.fromAgentId || 'a';
  const toAgentId = args.toAgentId || consult.targetAgentId || 'b';
  const handover = ensureHandover(db, ctx.kase, ctx.rootCallId, fromAgentId, toAgentId);
  const event = touch(db, 'handover.caller_bridged', {
    callerNumber: ctx.phone,
    caseId: ctx.kase.id,
    callId: ctx.call.id,
    rootCallId: ctx.rootCallId,
    consultCallId: consult.id,
    roomKind: 'accompanied-bridge',
  });
  const snapshot = recordAudioSnapshot(db, ctx.rootCallId, 'caller_bridged', {
    handoverId: handover.id,
  });
  addHandoverPhase(db, handover, 'caller_bridged', {
    eventId: event.id,
    audioSnapshotId: snapshot.id,
    roomId: snapshot.audio.activeRooms.find((r) => r.kind === 'accompanied-bridge')?.id,
    note: '三方在同一音频桥；此前私聊原话不会自动补发给来电人',
  });
  return { case: ctx.kase, handover, audio: snapshot.audio, event, revision: db.revision };
}

function completeHandover(db, args) {
  checkRevision(db, args.expectedRevision);
  const ctx = getCallerContext(db, args.callerNumber || args.phone);
  const fromAgentId = args.fromAgentId || 'a';
  const toAgentId = args.toAgentId || 'b';
  const toAgent = db.agents[toAgentId];
  const handover = ctx.kase.currentHandoverId ? db.handovers[ctx.kase.currentHandoverId] : null;
  if (!handover || !handover.phases.some((p) => p.phase === 'caller_bridged')) {
    throw new HttpError(409, 'caller_not_bridged', '必须先把新坐席和来电人接到同一通路');
  }

  const audio = computeAudio(db, ctx.rootCallId);
  const callerHearsTo = Boolean(audio.callerHears[toAgentId]);
  const toHearsCaller = Boolean(audio.agentHearsCaller[toAgentId]);
  const callerHearsFrom = Boolean(audio.callerHears[fromAgentId]);
  const blockers = [];
  if (!callerHearsTo || !toHearsCaller) blockers.push('successor_not_audibly_connected_to_caller');
  if (!callerHearsFrom) blockers.push('original_agent_not_present_for_warm_handover');

  const open = openItemCodes(ctx.kase);
  const qualifications = Object.fromEntries(open.map((code) => [code, qualificationFor(toAgent, code)]));
  const missingQualifications = open.filter((code) => !qualifications[code].qualified);
  if (missingQualifications.length) blockers.push('successor_missing_open_item_qualification');

  if (blockers.length) {
    throw new HttpError(409, 'handover_not_ready', '通话连通关系或工作台接办资格尚未同时对齐', {
      blockers,
      missingQualifications,
      qualifications,
      audio,
      audioPathChanged: false,
      currentRevision: db.revision,
    });
  }

  const beforeReturnable = ctx.kase.returnableAgentId;
  ctx.kase.returnableAgentId = toAgentId;
  ctx.kase.ticketHolderAgentId = toAgentId;
  ctx.kase.workOrder.holderAgentId = toAgentId;
  handover.completedAt = nowIso();
  handover.qualificationAtCompletion = qualifications;
  handover.responsibility = {
    beforeReturnableAgentId: beforeReturnable,
    currentReturnableAgentId: toAgentId,
    changedAt: handover.completedAt,
    changedRevision: db.revision + 1,
    openItemCodes: open,
  };

  const event = touch(db, 'handover.completed', {
    callerNumber: ctx.phone,
    caseId: ctx.kase.id,
    rootCallId: ctx.rootCallId,
    handoverId: handover.id,
    fromAgentId,
    toAgentId,
    beforeReturnable,
    openItemCodes: open,
  });
  handover.responsibility.changedRevision = db.revision;
  const snapshot = recordAudioSnapshot(db, ctx.rootCallId, 'handover_completed', {
    handoverId: handover.id,
    eventId: event.id,
  });
  handover.audioSnapshotIdAtCompletion = snapshot.id;
  addHandoverPhase(db, handover, 'responsibility_transferred', {
    eventId: event.id,
    audioSnapshotId: snapshot.id,
    returnableAgentId: toAgentId,
  });
  handover.status = 'completed';

  return {
    case: ctx.kase,
    handover,
    audio: snapshot.audio,
    responsibility: responsibilityView(db, ctx.kase),
    event,
    revision: db.revision,
  };
}

function resolveHangupTargets(db, ctx, args) {
  if (args.legId) {
    const call = Object.values(db.calls).find((c) => c.legs.some((l) => l.id === args.legId));
    const leg = call?.legs.find((l) => l.id === args.legId);
    if (!leg) throw new HttpError(404, 'leg_not_found', '找不到指定通话支路');
    return [{ call, leg }];
  }
  if (args.providerLegId) {
    const call = Object.values(db.calls).find((c) =>
      c.legs.some((l) => l.providerLegId === args.providerLegId),
    );
    const leg = call?.legs.find((l) => l.providerLegId === args.providerLegId);
    if (!leg) throw new HttpError(404, 'provider_leg_not_found', '运营商支路不存在');
    return [{ call, leg }];
  }
  if (args.callId) {
    const call = db.calls[args.callId];
    if (!call) throw new HttpError(404, 'call_not_found', '找不到指定通话支路');
    return call.legs
      .filter((leg) => !['disconnected', 'failed'].includes(leg.state))
      .map((leg) => ({ call, leg }));
  }

  const who = args.who || args.agentId;
  if (who === 'caller') {
    const leg = ctx.call.legs.find((item) => item.participantType === 'caller');
    if (!leg) throw new HttpError(404, 'caller_leg_not_found');
    return [{ call: ctx.call, leg }];
  }
  if (who && db.agents[who]) {
    let targets = rootLegs(db, ctx.rootCallId).filter(
      ({ leg }) =>
        leg.participantType === 'agent' &&
        leg.participantId === who &&
        !['disconnected', 'failed'].includes(leg.state),
    );
    if (args.branch === 'primary') targets = targets.filter(({ call }) => call.kind === 'inbound');
    if (args.branch === 'consult') targets = targets.filter(({ call }) => call.kind === 'consult');
    return targets;
  }
  throw new HttpError(400, 'hangup_target_required', '必须提供 who、legId、providerLegId 或 callId');
}

function disconnectTargets(db, rootCallId, targets, options = {}) {
  const affectedCallIds = new Set();
  const affectedRoomIds = new Set();
  const disconnected = [];

  for (const { call, leg } of targets) {
    if (['disconnected', 'failed'].includes(leg.state)) continue;
    if (leg.roomId) affectedRoomIds.add(leg.roomId);
    leg.state = 'disconnected';
    leg.disconnectedAt = nowIso();
    leg.hangupReason = options.reason || options.eventType || 'local_hangup';
    leg.stateHistory.push({ state: 'disconnected', at: leg.disconnectedAt, reason: leg.hangupReason });
    leg.roomId = null;
    affectedCallIds.add(call.id);
    disconnected.push({ callId: call.id, legId: leg.id, participantId: leg.participantId });
  }

  for (const roomId of affectedRoomIds) {
    const room = db.rooms[roomId];
    if (!room) continue;
    const remaining = room.legRefs
      .map((ref) => resolveRef(db, ref))
      .filter(Boolean)
      .some(({ leg }) => leg.state === 'connected' && leg.roomId === room.id);
    if (!remaining) endRoom(db, roomId);
  }

  for (const callId of affectedCallIds) recomputeCallState(db.calls[callId]);

  const callerRoot = Object.values(db.calls).find(
    (call) => call.id === rootCallId && call.kind === 'inbound',
  );
  const callerLeg = callerRoot?.legs.find((leg) => leg.participantType === 'caller');
  let rootEnded = false;
  if (options.cascadeCaller || callerLeg?.state === 'disconnected') {
    rootEnded = true;
    for (const { call, leg } of rootLegs(db, rootCallId)) {
      if (!['disconnected', 'failed'].includes(leg.state)) {
        leg.state = 'disconnected';
        leg.disconnectedAt = leg.disconnectedAt || nowIso();
        leg.roomId = null;
        affectedCallIds.add(call.id);
      }
    }
    for (const room of Object.values(db.rooms)) {
      if (room.rootCallId === rootCallId && !room.endedAt) endRoom(db, room.id);
    }
    for (const call of rootCalls(db, rootCallId)) recomputeCallState(call);
  }

  return { disconnected, affectedCallIds: [...affectedCallIds], rootEnded };
}

function settleCaseAfterAudioChange(db, ctx) {
  const callerLeg = ctx.call.legs.find((leg) => leg.participantType === 'caller');
  const rootEnded = callerLeg?.state === 'disconnected' || ctx.call.state === 'ended';
  if (!rootEnded) return { rootEnded: false };

  const open = openItemCodes(ctx.kase);
  const callerRecord = db.callers[ctx.phone];
  if (open.length === 0) {
    ctx.kase.status = 'resolved';
    ctx.kase.resolvedAt = nowIso();
    ctx.kase.workOrder.status = 'closed';
    if (callerRecord && callerRecord.activeCaseId === ctx.kase.id) callerRecord.activeCaseId = null;
  } else {
    ctx.kase.status = 'pending_followup';
    ctx.kase.workOrder.status = 'pending_followup';
    // Do not clobber a newer active case from a late event on an older branch.
    if (callerRecord && (!callerRecord.activeCaseId || callerRecord.activeCaseId === ctx.kase.id)) {
      callerRecord.activeCaseId = ctx.kase.id;
    }
  }
  return { rootEnded: true, openItemCodes: open, responsibility: responsibilityView(db, ctx.kase) };
}

function hangup(db, args) {
  checkRevision(db, args.expectedRevision);
  const ctx = getCallerContext(db, args.callerNumber || args.phone);
  const targets = resolveHangupTargets(db, ctx, args);
  if (!targets.length) {
    return { alreadyDisconnected: true, revision: db.revision, audio: computeAudio(db, ctx.rootCallId) };
  }

  const result = disconnectTargets(db, ctx.rootCallId, targets, {
    reason: args.reason || 'agent_or_caller_hangup',
    cascadeCaller: args.who === 'caller',
  });

  let handoverPhase = null;
  const handover = ctx.kase.currentHandoverId ? db.handovers[ctx.kase.currentHandoverId] : null;
  const alreadyDeparted = handover?.phases.some((phase) => phase.phase === 'original_agent_departed');
  if (handover?.status === 'completed' && args.who === handover.fromAgentId && !alreadyDeparted) {
    handoverPhase = addHandoverPhase(db, handover, 'original_agent_departed', {
      at: nowIso(),
      note: '原接办人在责任和音频交接完成后离场；该支路重发挂机通知不得结束整通来电',
    });
  }

  const event = touch(db, args.who === 'caller' ? 'caller.hangup' : 'leg.hangup', {
    callerNumber: ctx.phone,
    caseId: ctx.kase.id,
    callId: ctx.call.id,
    rootCallId: ctx.rootCallId,
    who: args.who || null,
    legId: args.legId || null,
    callBranchId: args.callId || null,
    result,
  });
  const settlement = settleCaseAfterAudioChange(db, ctx);
  const snapshot = recordAudioSnapshot(db, ctx.rootCallId, 'hangup', {
    hangupResult: result,
    settlement,
  });

  return {
    hangup: result,
    settlement,
    handoverPhase,
    case: ctx.kase,
    audio: snapshot.audio,
    event,
    revision: db.revision,
  };
}

function receiveTelephonyEvent(db, eventBody) {
  const eventId = eventBody.eventId || eventBody.id;
  if (!eventId) throw new HttpError(400, 'event_id_required', '运营商通知必须带 eventId 以便幂等重发');
  if (db.providerEvents[eventId]) {
    const stored = db.providerEvents[eventId];
    return {
      duplicate: true,
      eventId,
      ...stored.response,
      storedAt: stored.receivedAt,
      originalRevision: stored.revision,
    };
  }

  checkRevision(db, eventBody.expectedRevision); // Usually absent for webhooks.
  const providerLegId = eventBody.providerLegId || eventBody.legId;
  if (!providerLegId) throw new HttpError(400, 'provider_leg_id_required', '通知必须带 providerLegId');
  const located = Object.values(db.calls)
    .flatMap((call) => call.legs.map((leg) => ({ call, leg })))
    .find(({ leg }) => leg.providerLegId === providerLegId || leg.id === providerLegId);
  if (!located) throw new HttpError(404, 'leg_not_found', '通知落到了未知支路', { providerLegId });

  const { call, leg } = located;
  const rootCallId = call.rootCallId || call.id;
  let phone = call.callerNumber;
  if (!phone) {
    const root = db.calls[rootCallId];
    phone = root?.callerNumber || db.cases[call.caseId]?.callerNumber;
  }
  // Late/redelivered provider events can arrive after the active case is closed.
  // Build context from the event's own case instead of requiring an active case.
  let ctx = null;
  const eventCase = call.caseId ? db.cases[call.caseId] : null;
  if (eventCase) {
    ctx = {
      phone: phone || eventCase.callerNumber,
      kase: eventCase,
      call: db.calls[eventCase.primaryCallId] || call,
      rootCallId,
    };
    phone = ctx.phone;
  }
  const eventType = eventBody.type || eventBody.eventType;
  let actionResult = null;

  if (eventType === 'leg.disconnected' || eventType === 'hangup') {
    actionResult = disconnectTargets(db, rootCallId, [{ call, leg }], {
      eventType,
      reason: eventBody.reason || 'provider_disconnect',
    });
    if (ctx) settleCaseAfterAudioChange(db, ctx);
  } else if (eventType === 'leg.connected' || eventType === 'answer') {
    if (['disconnected', 'failed'].includes(leg.state)) {
      actionResult = { ignored: 'leg_terminal', legState: leg.state };
    } else {
      leg.state = 'connected';
      leg.connectedAt = leg.connectedAt || nowIso();
      leg.stateHistory.push({ state: 'provider_connected', at: nowIso(), eventId });
      // If the other consult leg is already live, provider event can complete the private room.
      if (call.kind === 'consult') {
        const other = call.legs.find((item) => item.id !== leg.id);
        if (other?.state === 'connected' && !leg.roomId && !other.roomId) {
          createRoom(db, rootCallId, 'private-consult', [legRef(leg), legRef(other)]);
        }
      }
      recomputeCallState(call);
      actionResult = { connected: true, legId: leg.id };
    }
  } else if (eventType === 'call.failed') {
    leg.state = 'failed';
    call.state = 'failed';
    call.failureReason = eventBody.reason || 'failed';
    call.endedAt = nowIso();
    actionResult = { failed: true };
  } else {
    throw new HttpError(400, 'unsupported_event_type', `暂不支持事件类型 ${eventType}`);
  }

  if (ctx?.kase.currentHandoverId) {
    const handover = db.handovers[ctx.kase.currentHandoverId];
    if (
      eventType === 'leg.disconnected' &&
      handover?.status === 'completed' &&
      leg.participantType === 'agent' &&
      leg.participantId === handover.fromAgentId &&
      !handover.phases.some((p) => p.phase === 'original_agent_departed')
    ) {
      addHandoverPhase(db, handover, 'original_agent_departed', {
        eventId,
        providerLegId,
        note: '由重发/运营商挂机通知确认原接办人离场',
      });
    }
  }

  const auditEvent = touch(db, 'telephony.event_received', {
    providerEventId: eventId,
    eventType,
    providerLegId,
    callId: call.id,
    rootCallId,
    actionResult,
  });
  const audio = computeAudio(db, rootCallId);
  const response = {
    duplicate: false,
    eventId,
    applied: true,
    actionResult,
    audio,
    caseId: ctx?.kase.id || call.caseId,
    callerNumber: phone,
    currentReturnableAgentId: ctx?.kase.returnableAgentId || null,
    revision: db.revision,
    auditEventId: auditEvent.id,
  };
  db.providerEvents[eventId] = {
    eventId,
    type: eventType,
    providerLegId,
    receivedAt: nowIso(),
    revision: db.revision,
    response: {
      applied: true,
      actionResult,
      caseId: response.caseId,
      callerNumber: phone,
      currentReturnableAgentId: response.currentReturnableAgentId,
      audio,
    },
  };
  return response;
}

function board(db) {
  return {
    at: nowIso(),
    revision: db.revision,
    agents: Object.values(db.agents).map((agent) => ({
      ...publicAgent(agent),
      activeLegs: Object.values(db.calls).flatMap((call) =>
        call.legs
          .filter((leg) => leg.participantType === 'agent' && leg.participantId === agent.id && ACTIVE_LEG_STATES.has(leg.state))
          .map((leg) => ({ callId: call.id, kind: call.kind, state: leg.state })),
      ),
    })),
    activeCases: Object.values(db.cases)
      .filter((kase) => kase.status !== 'resolved')
      .map((kase) => ({
        id: kase.id,
        callerNumber: kase.callerNumber,
        status: kase.status,
        ticketHolderAgentId: kase.ticketHolderAgentId,
        returnableAgentId: kase.returnableAgentId,
        openItemCodes: openItemCodes(kase),
        responsibility: responsibilityView(db, kase),
        primaryCallState: db.calls[kase.primaryCallId]?.state || null,
      })),
    recentHandovers: Object.values(db.handovers).slice(-10).reverse(),
  };
}

module.exports = {
  ITEM_CATALOG,
  publicAgent,
  qualificationFor,
  computeAudio,
  recordAudioSnapshot,
  callerDetail,
  board,
  setQualification,
  setPresence,
  inbound,
  answerInbound,
  callerAsk,
  agentSpeak,
  closeItemFromUtterance,
  holdCaller,
  retrieveCaller,
  startConsult,
  answerConsult,
  assignTicket,
  bridgeToCaller,
  completeHandover,
  hangup,
  receiveTelephonyEvent,
  getCallerContext,
  latestCallerContext,
};
