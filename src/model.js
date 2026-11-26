import { createHash } from 'node:crypto';

export const SELECTED_CLASS_TYPE = 'retained_original';

export class DomainError extends Error {
  constructor(status, code, message, details = {}) {
    super(message);
    this.name = 'DomainError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function clone(value) {
  return structuredClone(value);
}

export function seedDb() {
  return {
    meta: {
      serviceName: '泥火夜校补修办理服务',
      selectedClassType: SELECTED_CLASS_TYPE,
      selectedClassTypeName: '保留原件辅导',
      today: '2026-11-26',
      nextOpenSessionId: 'OPEN-1126',
      rulebookVersion: 3,
      historyRevision: 1,
      startedAt: '2026-11-26T10:30:00+08:00',
    },
    students: [
      { id: 'S-JIA', name: '甲', note: '在 P-JIA-01 上完成开孔、立壁后提前离场' },
      { id: 'S-YI', name: '乙', note: '动手历史固定的跨件/同件规则对照学员' },
      { id: 'S-BING', name: '丙', note: '迟到后只看到后半段，记录以观摩为主' },
      { id: 'S-DING', name: '丁', note: '练习中段缺失，后补看允许旁听补入的示范' },
      { id: 'S-WU', name: '戊', note: '留有一件已回收重揉泥件，用于反例' },
    ],
    actions: [
      { code: 'center', label: '定中心' },
      { code: 'open', label: '开孔' },
      { code: 'raise_wall', label: '立壁' },
      { code: 'close', label: '收口' },
      { code: 'attach_handle', label: '装把' },
      { code: 'smooth_join', label: '接缝抹平' },
    ],
    rulebook: {
      version: 3,
      groups: [
        {
          code: 'BASIC_CYLINDER',
          name: '开孔—立壁—收口完整动作组',
          stampTitle: '陶艺拉坯基础结课章',
          stampType: 'completion',
          sequence: ['open', 'raise_wall', 'close'],
          acceptedModes: ['hands_on'],
          links: [
            {
              id: 'L-OPEN-WALL',
              from: 'open',
              to: 'raise_wall',
              samePiece: true,
              sameContinuousChain: true,
              acceptedModes: ['hands_on'],
              supplementByAudit: false,
              supplementByPersonalAssignment: true,
              description: '开孔与立壁须在同一泥件上前后衔接',
            },
            {
              id: 'L-WALL-CLOSE',
              from: 'raise_wall',
              to: 'close',
              samePiece: true,
              sameContinuousChain: true,
              acceptedModes: ['hands_on'],
              supplementByAudit: false,
              supplementByPersonalAssignment: true,
              description: '立壁与收口须在同一未回收泥件上连续完成；观摩不能替代亲手收口',
            },
          ],
        },
        {
          code: 'HANDLE_JOIN_CONTROL',
          name: '装把—接缝规则对照组',
          stampTitle: '装把衔接练习章',
          stampType: 'completion',
          sequence: ['attach_handle', 'smooth_join'],
          acceptedModes: ['hands_on'],
          links: [
            {
              id: 'L-HANDLE-JOIN',
              from: 'attach_handle',
              to: 'smooth_join',
              samePiece: false,
              sameContinuousChain: false,
              acceptedModes: ['hands_on'],
              supplementByAudit: false,
              supplementByPersonalAssignment: true,
              description: '对照项：初规允许另一件个人作业跨件补入；验题时可改为必须同件',
            },
          ],
        },
        {
          code: 'WORKFLOW_AUDIT',
          name: '拉坯流程见闻补全档',
          stampTitle: '流程见闻补全标记（非亲手结课章）',
          stampType: 'audit_only',
          sequence: ['open', 'raise_wall', 'close'],
          acceptedModes: ['hands_on', 'observed'],
          links: [
            {
              id: 'L-AUDIT-OPEN-WALL',
              from: 'open',
              to: 'raise_wall',
              samePiece: false,
              sameContinuousChain: false,
              acceptedModes: ['hands_on', 'observed'],
              supplementByAudit: true,
              supplementByPersonalAssignment: true,
              description: '流程见闻档允许另一次旁听示范补入立壁',
            },
            {
              id: 'L-AUDIT-WALL-CLOSE',
              from: 'raise_wall',
              to: 'close',
              samePiece: false,
              sameContinuousChain: false,
              acceptedModes: ['hands_on', 'observed'],
              supplementByAudit: true,
              supplementByPersonalAssignment: true,
              description: '流程见闻档允许跨件或观摩，但不换发亲手结课章',
            },
          ],
        },
      ],
      transferableCredits: [
        {
          code: 'CREDIT-CENTER',
          action: 'center',
          name: '定中心独立完成项',
          transferable: true,
          acceptedModes: ['hands_on'],
          description: '原班已由助教确认独立完成，可迁移，不要求重读全课',
        },
      ],
    },
    sessions: [
      { id: 'LS-1118', kind: 'regular', title: '周三拉坯正课', at: '2026-11-18T19:00:00+08:00' },
      { id: 'LS-1118-REPLAY', kind: 'demo_replay', title: '课间补看立壁示范', at: '2026-11-18T19:45:00+08:00' },
      { id: 'LS-1120-DEMO', kind: 'demo_replay', title: '课后中段动作补看示范', at: '2026-11-20T20:00:00+08:00' },
      { id: 'PW-1122', kind: 'personal_practice', title: '学员个人作业/开放练习', at: '2026-11-22T15:00:00+08:00' },
      {
        id: 'OPEN-1126',
        kind: 'open_studio',
        title: '今晚开放台·保留原件辅导',
        at: '2026-11-26T18:30:00+08:00',
        tableIds: ['T-W1', 'T-W2', 'T-W3'],
      },
    ],
    tables: [
      { id: 'T-W1', name: '拉坯1台', zone: '拉坯区', capacity: 1 },
      { id: 'T-W2', name: '拉坯2台', zone: '拉坯区', capacity: 1 },
      { id: 'T-W3', name: '拉坯3台', zone: '拉坯区', capacity: 2 },
    ],
    pieces: [
      piece('P-JIA-00', 'S-JIA', 'done', 'CLAY-P-JIA-00#v1'),
      piece('P-JIA-01', 'S-JIA', 'unfinished', 'CLAY-P-JIA-01#v1', { currentTableId: 'WHEEL-2' }),
      piece('P-JIA-02', 'S-JIA', 'practice', 'CLAY-P-JIA-02#v1'),
      piece('DEMO-CLOSE', null, 'demo', 'CLAY-DEMO-CLOSE#v1', { isDemo: true }),
      piece('P-YI-H1', 'S-YI', 'unfinished', 'CLAY-P-YI-H1#v1'),
      piece('P-YI-H2', 'S-YI', 'practice', 'CLAY-P-YI-H2#v1'),
      piece('P-BING-01', 'S-BING', 'unfinished', 'CLAY-P-BING-01#v1'),
      piece('P-DING-01', 'S-DING', 'practice', 'CLAY-P-DING-01#v1'),
      piece('DEMO-RAISE-09', null, 'demo', 'CLAY-DEMO-RAISE-09#v1', { isDemo: true }),
      piece('P-WU-OLD', 'S-WU', 'recycled', 'CLAY-P-WU-OLD#v1', {
        recycled: true,
        recycledAt: '2026-11-25T10:00:00+08:00',
        recycleNote: '未办理保留，已回收重揉',
      }),
    ],
    records: [
      {
        id: 'PR-JIA-MAIN',
        sessionId: 'LS-1118',
        studentId: 'S-JIA',
        pieceNo: 'P-JIA-01',
        pieceUid: 'CLAY-P-JIA-01#v1',
        taConfirmedBy: 'TA-ZHOU',
        events: [
          event('EV-JIA-CENTER', 0, '2026-11-18T19:05:00+08:00', 'center', 'hands_on'),
          event('EV-JIA-OPEN', 1, '2026-11-18T19:12:00+08:00', 'open', 'hands_on'),
          event('EV-JIA-WALL', 2, '2026-11-18T19:35:00+08:00', 'raise_wall', 'hands_on'),
        ],
      },
      {
        id: 'PR-JIA-CROSS-CLOSE',
        sessionId: 'PW-1122',
        studentId: 'S-JIA',
        pieceNo: 'P-JIA-02',
        pieceUid: 'CLAY-P-JIA-02#v1',
        taConfirmedBy: 'TA-ZHOU',
        events: [event('EV-JIA-CROSS-CLOSE', 1, '2026-11-22T15:20:00+08:00', 'close', 'hands_on')],
      },
      {
        id: 'PR-JIA-OBS-CLOSE',
        sessionId: 'LS-1120-DEMO',
        studentId: 'S-JIA',
        pieceNo: 'DEMO-CLOSE',
        pieceUid: 'CLAY-DEMO-CLOSE#v1',
        taConfirmedBy: 'TA-ZHOU',
        events: [event('EV-JIA-OBS-CLOSE', 1, '2026-11-20T20:10:00+08:00', 'close', 'observed')],
      },
      {
        id: 'PR-YI-H1',
        sessionId: 'LS-1118',
        studentId: 'S-YI',
        pieceNo: 'P-YI-H1',
        pieceUid: 'CLAY-P-YI-H1#v1',
        taConfirmedBy: 'TA-ZHOU',
        events: [event('EV-YI-HANDLE', 1, '2026-11-18T19:20:00+08:00', 'attach_handle', 'hands_on')],
      },
      {
        id: 'PR-YI-H2',
        sessionId: 'PW-1122',
        studentId: 'S-YI',
        pieceNo: 'P-YI-H2',
        pieceUid: 'CLAY-P-YI-H2#v1',
        taConfirmedBy: 'TA-ZHOU',
        events: [event('EV-YI-JOIN', 1, '2026-11-22T15:40:00+08:00', 'smooth_join', 'hands_on')],
      },
      {
        id: 'PR-BING-LATE',
        sessionId: 'LS-1118',
        studentId: 'S-BING',
        pieceNo: 'P-BING-01',
        pieceUid: 'CLAY-P-BING-01#v1',
        taConfirmedBy: 'TA-ZHOU',
        late: true,
        note: '迟到后只看到立壁、收口后半段',
        events: [
          event('EV-BING-WALL-OBS', 2, '2026-11-18T19:40:00+08:00', 'raise_wall', 'observed'),
          event('EV-BING-CLOSE-OBS', 3, '2026-11-18T20:05:00+08:00', 'close', 'observed'),
        ],
      },
      {
        id: 'PR-DING-FLOW',
        sessionId: 'LS-1118',
        studentId: 'S-DING',
        pieceNo: 'P-DING-01',
        pieceUid: 'CLAY-P-DING-01#v1',
        taConfirmedBy: 'TA-ZHOU',
        missingMiddle: 'raise_wall',
        events: [
          event('EV-DING-OPEN', 1, '2026-11-18T19:10:00+08:00', 'open', 'hands_on'),
          event('EV-DING-CLOSE', 3, '2026-11-18T20:00:00+08:00', 'close', 'hands_on'),
        ],
      },
      {
        id: 'PR-DING-DEMO-WALL',
        sessionId: 'LS-1118-REPLAY',
        studentId: 'S-DING',
        pieceNo: 'DEMO-RAISE-09',
        pieceUid: 'CLAY-DEMO-RAISE-09#v1',
        taConfirmedBy: 'TA-ZHOU',
        fillsPracticeRecordId: 'PR-DING-FLOW',
        events: [event('EV-DING-WALL-DEMO', 2, '2026-11-18T19:45:00+08:00', 'raise_wall', 'observed')],
      },
      {
        id: 'PR-WU-OLD',
        sessionId: 'LS-1118',
        studentId: 'S-WU',
        pieceNo: 'P-WU-OLD',
        pieceUid: 'CLAY-P-WU-OLD#v1',
        taConfirmedBy: 'TA-ZHOU',
        events: [event('EV-WU-OPEN', 1, '2026-11-18T19:15:00+08:00', 'open', 'hands_on')],
      },
    ],
    cases: [],
    attendances: [],
    receipts: [],
    certificates: [],
    auditLogs: [
      {
        at: '2026-11-26T10:30:00+08:00',
        actor: 'SYSTEM',
        action: 'seed',
        message: '已载入虚构陶艺夜校初始资料；班型仅选择保留原件辅导',
      },
    ],
    idempotency: {},
    counters: { event: 1000, record: 1000, attendance: 1000, receipt: 1000, case: 1000, decision: 1000, student: 1000 },
  };
}

function piece(no, studentId, status, uid, extra = {}) {
  return {
    no,
    uid,
    studentId,
    status,
    recycled: status === 'recycled',
    storageSlot: status === 'unfinished' ? `待保留架/${no}` : null,
    continueTableId: null,
    reservationForCaseId: null,
    ...extra,
  };
}

function event(id, seq, at, action, mode, extra = {}) {
  return {
    id,
    seq,
    at,
    action,
    mode,
    independence: mode === 'hands_on' ? 'independent' : 'not_applicable',
    confirmedBy: 'TA-ZHOU',
    ...extra,
  };
}

export function findStudent(db, studentId) {
  const student = db.students.find((s) => s.id === studentId || s.name === studentId);
  if (!student) throw new DomainError(404, 'STUDENT_NOT_FOUND', `找不到学员：${studentId}`);
  return student;
}

export function findPiece(db, pieceNo, pieceUid = null) {
  if (pieceUid) {
    const byUid = db.pieces.find((p) => p.uid === pieceUid);
    if (byUid) return byUid;
  }
  const matches = db.pieces.filter((p) => p.no === pieceNo);
  if (pieceUid) {
    return matches.find((p) => p.uid === pieceUid) || null;
  }
  return matches.find((p) => !p.recycled) || matches[0] || null;
}

export function allEvents(db, studentId) {
  const student = studentId ? findStudent(db, studentId) : null;
  const out = [];
  for (const record of db.records) {
    if (student && record.studentId !== student.id) continue;
    for (const raw of record.events || []) {
      const pieceNo = raw.pieceNo || record.pieceNo;
      let pieceUid = raw.pieceUid || record.pieceUid || null;
      const piece = findPiece(db, pieceNo, pieceUid);
      if (!pieceUid && piece) pieceUid = piece.uid;
      const effectiveMode =
        raw.mode === 'hands_on' && raw.independence === 'assisted' ? 'assisted' : raw.mode;
      out.push({
        ...raw,
        effectiveMode,
        recordId: record.id,
        sessionId: raw.sessionId || record.sessionId,
        pieceNo,
        pieceUid,
        _time: Date.parse(raw.at || record.at || '') || 0,
      });
    }
  }
  return out.sort(compareTimeline);
}

function compareTimeline(a, b) {
  if (a._time !== b._time) return a._time - b._time;
  return (a.seq ?? 0) - (b.seq ?? 0);
}

function modeAccepted(ev, acceptedModes) {
  return acceptedModes.includes(ev.effectiveMode);
}

function samePieceIdentity(a, b) {
  if (a.pieceUid && b.pieceUid) return a.pieceUid === b.pieceUid;
  return a.pieceNo === b.pieceNo;
}

function checkLink(db, link, from, to) {
  const reasons = [];
  if (compareTimeline(to, from) <= 0) reasons.push('sequence_not_after_anchor');

  if (!modeAccepted(from, link.acceptedModes)) {
    reasons.push(`anchor_mode_${from.effectiveMode}_not_accepted`);
  }
  if (!modeAccepted(to, link.acceptedModes)) {
    reasons.push(`target_mode_${to.effectiveMode}_not_accepted`);
  }

  if (link.samePiece && !samePieceIdentity(from, to)) {
    reasons.push('piece_mismatch');
  }

  if (link.sameContinuousChain && !link.samePiece && from.chainId !== to.chainId) {
    reasons.push('continuous_chain_mismatch');
  }

  const piece = findPiece(db, to.pieceNo, to.pieceUid) || findPiece(db, from.pieceNo, from.pieceUid);
  if (link.samePiece && piece?.recycled && piece.recycledAt) {
    const recycledAt = Date.parse(piece.recycledAt);
    const fromTime = from._time;
    const toTime = to._time;
    if (toTime >= recycledAt && fromTime < recycledAt) {
      reasons.push('piece_recycled_before_continuation');
    }
  }

  return reasons;
}

function evaluateGroup(db, events, group) {
  const actionLabel = Object.fromEntries(db.actions.map((a) => [a.code, a.label]));
  const allByAction = new Map(
    group.sequence.map((code) => [code, events.filter((e) => e.action === code)]),
  );
  const candidates = group.sequence.map((code) =>
    events.filter((e) => e.action === code && modeAccepted(e, group.acceptedModes)),
  );

  let paths = candidates[0].map((e) => [e]);
  for (let i = 1; i < group.sequence.length; i += 1) {
    const link = group.links[i - 1];
    const nextPaths = [];
    for (const to of candidates[i]) {
      for (const path of paths) {
        const from = path[path.length - 1];
        if (checkLink(db, link, from, to).length === 0) {
          nextPaths.push([...path, to]);
          break;
        }
      }
    }
    paths = nextPaths;
  }

  if (paths.length > 0) {
    const chosen = paths[0];
    return {
      code: group.code,
      name: group.name,
      stampTitle: group.stampTitle,
      stampType: group.stampType,
      satisfied: true,
      actionCodes: group.sequence,
      actionNames: group.sequence.map((c) => actionLabel[c] || c),
      chosenEvidence: chosen.map(publicEvent),
      outstandingSegments: [],
    };
  }

  const linkResults = group.links.map((link, i) => {
    const fromEvents = allByAction.get(link.from) || allByAction.get(group.sequence[i]) || [];
    const toEvents = allByAction.get(link.to) || allByAction.get(group.sequence[i + 1]) || [];
    let satisfyingPair = null;
    const rejectionExamples = [];

    for (const from of fromEvents) {
      for (const to of toEvents) {
        const reasons = checkLink(db, link, from, to);
        if (reasons.length === 0 && !satisfyingPair) {
          satisfyingPair = { from, to };
        } else if (rejectionExamples.length < 8) {
          rejectionExamples.push({
            fromEventId: from.id,
            toEventId: to.id,
            fromAction: link.from,
            toAction: link.to,
            pieceNo: to.pieceNo,
            mode: to.effectiveMode,
            reasons,
          });
        }
      }
    }

    return {
      link,
      index: i,
      satisfied: Boolean(satisfyingPair),
      satisfyingPair: satisfyingPair
        ? { from: publicEvent(satisfyingPair.from), to: publicEvent(satisfyingPair.to) }
        : null,
      rejectionExamples,
    };
  });

  const failed = linkResults.filter((r) => !r.satisfied);
  const segments = mergeFailedLinks(db, events, failed, linkResults, actionLabel);

  return {
    code: group.code,
    name: group.name,
    stampTitle: group.stampTitle,
    stampType: group.stampType,
    satisfied: false,
    actionCodes: group.sequence,
    actionNames: group.sequence.map((c) => actionLabel[c] || c),
    chosenEvidence: [],
    linkResults: linkResults.map((r) => ({
      id: r.link.id,
      from: r.link.from,
      to: r.link.to,
      satisfied: r.satisfied,
      satisfyingPair: r.satisfyingPair,
    })),
    outstandingSegments: segments,
  };
}

function mergeFailedLinks(db, events, failed, linkResults, actionLabel) {
  const segments = [];
  for (const result of failed) {
    const last = segments[segments.length - 1];
    if (last && last.lastLinkIndex === result.index - 1) {
      last.actionCodes.push(result.link.to);
      last.linkIds.push(result.link.id);
      last.links.push(result.link);
      last.lastLinkIndex = result.index;
    } else {
      segments.push({
        groupCode: result.link.groupCode,
        actionCodes: [result.link.from, result.link.to],
        actionNames: [result.link.from, result.link.to].map((c) => actionLabel[c] || c),
        linkIds: [result.link.id],
        links: [result.link],
        firstLinkIndex: result.index,
        lastLinkIndex: result.index,
        linkResults: [],
      });
    }
  }

  return segments.map((segment) => {
    const firstLink = segment.links[0];
    const firstIndex = segment.firstLinkIndex;
    const groupEvents = linkResults[firstIndex] ? null : null;
    const allRejections = linkResults
      .filter((r) => segment.linkIds.includes(r.link.id))
      .flatMap((r) => r.rejectionExamples);

    const anchorCandidates = events
      .filter((e) => e.action === firstLink.from && firstLink.acceptedModes.includes(e.effectiveMode))
      .sort(compareTimeline);
    const anchorEvent = anchorCandidates[anchorCandidates.length - 1] || null;
    const piece = anchorEvent ? findPiece(db, anchorEvent.pieceNo, anchorEvent.pieceUid) : null;
    const pieceRecycled = Boolean(piece?.recycled);
    const samePieceRequired = segment.links.every((l) => l.samePiece);
    const retainable =
      SELECTED_CLASS_TYPE === 'retained_original' &&
      samePieceRequired &&
      Boolean(anchorEvent?.pieceUid || anchorEvent?.pieceNo) &&
      !pieceRecycled &&
      !piece?.isDemo;

    const hasUsableAnchor = Boolean(anchorEvent);
    const requiredNewActions = hasUsableAnchor
      ? segment.actionCodes.slice(1)
      : segment.actionCodes;

    const reasonCodes = [...new Set(allRejections.flatMap((x) => x.reasons))];
    if (reasonCodes.length === 0) reasonCodes.push('no_qualifying_continuous_chain');
    if (pieceRecycled) reasonCodes.push('anchor_piece_recycled');

    let repairKind = 'redo_continuous_segment_in_person';
    if (retainable) repairKind = 'retain_original_and_complete_on_same_piece';
    else if (firstLink.supplementByAudit && firstLink.acceptedModes.includes('observed')) {
      repairKind = 'audit_supplement_allowed';
    }

    return {
      gapKey: `${segment.groupCode}:${segment.linkIds.join('+')}`,
      groupCode: segment.groupCode,
      actionCodes: segment.actionCodes,
      segment: segment.actionCodes,
      segmentNames: segment.actionCodes.map((c) => actionLabel[c] || c),
      linkIds: segment.linkIds,
      requiredNewActions,
      requiredNewActionNames: requiredNewActions.map((c) => actionLabel[c] || c),
      shortestTeachableSegment: segment.actionCodes,
      anchorEvent: anchorEvent ? publicEvent(anchorEvent) : null,
      pieceNo: anchorEvent?.pieceNo || null,
      pieceUid: anchorEvent?.pieceUid || null,
      pieceRecycled,
      retainable,
      repairKind,
      mustAttendInPerson: segment.links.some((l) => l.acceptedModes.includes('hands_on')),
      canBeFilledByObservation: segment.links.every((l) => l.acceptedModes.includes('observed')),
      reasonCodes,
      candidateRejections: allRejections,
    };
  });
}

export function publicEvent(e) {
  if (!e) return null;
  return {
    id: e.id,
    recordId: e.recordId,
    sessionId: e.sessionId,
    action: e.action,
    mode: e.mode,
    effectiveMode: e.effectiveMode,
    independence: e.independence,
    seq: e.seq,
    at: e.at,
    pieceNo: e.pieceNo,
    pieceUid: e.pieceUid,
    confirmedBy: e.confirmededBy || e.confirmedBy,
  };
}

export function historyFingerprint(db, studentId) {
  const records = db.records
    .filter((r) => !studentId || r.studentId === studentId)
    .map((r) => ({
      id: r.id,
      studentId: r.studentId,
      pieceNo: r.pieceNo,
      pieceUid: r.pieceUid,
      events: r.events.map((e) => [e.id, e.action, e.mode, e.independence, e.at, e.seq]),
    }));
  return createHash('sha256').update(JSON.stringify(records)).digest('hex').slice(0, 16);
}

export function evaluateStudent(db, studentId) {
  const student = findStudent(db, studentId);
  const events = allEvents(db, student.id);
  const groups = db.rulebook.groups.map((g) =>
    evaluateGroup(
      db,
      events,
      clone({
        ...g,
        links: g.links.map((l) => ({ ...l, groupCode: g.code })),
      }),
    ),
  );

  const transferableCredits = db.rulebook.transferableCredits.map((credit) => {
    const evidence = events.find(
      (e) => e.action === credit.action && credit.acceptedModes.includes(e.effectiveMode),
    );
    return { ...credit, satisfied: Boolean(evidence), evidence: evidence ? publicEvent(evidence) : null };
  });

  const outstanding = groups
    .filter((g) => !g.satisfied)
    .flatMap((g) => g.outstandingSegments.map((s) => ({ ...s, groupName: g.name })));

  const stamps = db.certificates.filter((c) => c.studentId === student.id);

  return {
    student: { id: student.id, name: student.name, note: student.note },
    rulebookVersion: db.rulebook.version,
    historyRevision: db.meta.historyRevision,
    historyFingerprint: historyFingerprint(db, student.id),
    eventCount: events.length,
    groups,
    completedGroups: groups.filter((g) => g.satisfied).map((g) => ({ code: g.code, name: g.name })),
    outstandingSegments: outstanding,
    transferableCredits,
    stamps,
  };
}

export function findDemoFillablePractice(db, actionCode = 'raise_wall') {
  const matches = [];
  for (const student of db.students) {
    const evaluation = evaluateStudent(db, student.id);
    const audit = evaluation.groups.find((g) => g.code === 'WORKFLOW_AUDIT');
    const practical = evaluation.groups.find((g) => g.code === 'BASIC_CYLINDER');
    const demoEvents = allEvents(db, student.id).filter(
      (e) => e.action === actionCode && e.mode === 'observed',
    );
    for (const demo of demoEvents) {
      const demoRecord = db.records.find((r) => r.id === demo.recordId);
      const session = db.sessions.find((s) => s.id === demo.sessionId);
      const isMakeupDemo =
        Boolean(demoRecord?.fillsPracticeRecordId) || session?.kind === 'demo_replay';
      if (!isMakeupDemo) continue;

      const filledRecordId = demoRecord?.fillsPracticeRecordId || null;
      matches.push({
        studentId: student.id,
        studentName: student.name,
        demoEventId: demo.id,
        demoRecordId: demo.recordId,
        demoSessionId: demo.sessionId,
        demoPieceNo: demo.pieceNo,
        fillsPracticeRecordId: filledRecordId,
        auditWorkflowComplete: audit?.satisfied || false,
        practicalCompletionComplete: practical?.satisfied || false,
        answer:
          audit?.satisfied && !practical?.satisfied && filledRecordId
            ? `补看示范可把 ${filledRecordId} 的“流程见闻补全档”补完整，但不能补发亲手拉坯结课章。`
            : audit?.satisfied && !practical?.satisfied
              ? `这次补看示范可补入 ${student.name} 的流程见闻档，但不能替代同件亲手结课动作。`
              : '该示范只能补入规则允许观摩的项目，不能替代同件亲手动作。',
      });
    }
  }
  return matches;
}

function nextId(db, key, prefix) {
  db.counters[key] = (db.counters[key] || 1000) + 1;
  return `${prefix}-${db.counters[key]}`;
}

export function createStudent(db, body) {
  const name = String(body.name || '').trim();
  if (!name) throw new DomainError(400, 'NAME_REQUIRED', '请提供学员姓名');
  const id = body.id ? String(body.id) : nextId(db, 'student', 'S');
  if (db.students.some((s) => s.id === id)) {
    throw new DomainError(409, 'STUDENT_EXISTS', `学员编号已存在：${id}`);
  }
  const student = { id, name, note: body.note || '验题人新建学员' };
  db.students.push(student);
  log(db, 'API', 'student.create', `新建学员 ${id} ${name}`);
  return student;
}

export function createRecord(db, body) {
  const student = findStudent(db, body.studentId);
  if (!body.sessionId) throw new DomainError(400, 'SESSION_REQUIRED', '练习记录必须有课次');
  if (!body.pieceNo) throw new DomainError(400, 'PIECE_REQUIRED', '练习记录必须有泥件号');
  if (!Array.isArray(body.events) || body.events.length === 0) {
    throw new DomainError(400, 'EVENTS_REQUIRED', '练习记录至少包含一个动作和助教确认的参与方式');
  }

  let piece = findPiece(db, body.pieceNo, body.pieceUid || null);
  if (!piece) {
    piece = {
      no: body.pieceNo,
      uid: body.pieceUid || `${body.pieceNo}#v1`,
      studentId: student.id,
      status: 'practice',
      recycled: false,
      storageSlot: null,
      continueTableId: null,
      reservationForCaseId: null,
    };
    db.pieces.push(piece);
  }

  const record = {
    id: body.id || nextId(db, 'record', 'PR'),
    sessionId: body.sessionId,
    studentId: student.id,
    pieceNo: body.pieceNo,
    pieceUid: body.pieceUid || piece.uid,
    taConfirmedBy: body.confirmedBy || body.taConfirmedBy || 'TA-ON-DUTY',
    note: body.note || '',
    events: body.events.map((e, i) => ({
      id: e.id || nextId(db, 'event', 'EV'),
      seq: e.seq ?? i + 1,
      at: e.at || body.at || new Date(Date.now() + i * 1000).toISOString(),
      action: e.action,
      mode: e.mode || 'hands_on',
      independence: e.independence || (e.mode === 'observed' ? 'not_applicable' : 'independent'),
      confirmedBy: e.confirmedBy || record_ta(body),
    })),
  };
  db.records.push(record);
  db.meta.historyRevision += 1;
  log(db, 'API', 'record.create', `新增 ${student.id} 的练习记录 ${record.id}`);
  return record;
}

function record_ta(body) {
  return body.confirmedBy || body.taConfirmedBy || 'TA-ON-DUTY';
}

function selectSegment(db, body) {
  const evaluation = evaluateStudent(db, body.studentId);
  let candidates = evaluation.outstandingSegments;
  if (body.groupCode) candidates = candidates.filter((s) => s.groupCode === body.groupCode);
  if (body.linkId) candidates = candidates.filter((s) => s.linkIds.includes(body.linkId));
  if (body.gapKey) candidates = candidates.filter((s) => s.gapKey === body.gapKey);
  if (Array.isArray(body.segment)) {
    const key = body.segment.join('>');
    candidates = candidates.filter((s) => s.actionCodes.join('>') === key);
  }

  if (candidates.length === 0) {
    const ev = evaluateStudent(db, body.studentId);
    throw new DomainError(409, 'NOTHING_TO_MAKE_UP', '按当前规则没有找到符合选择条件的未补缺口', {
      evaluation: ev,
    });
  }

  return (
    candidates.find((s) => s.retainable && (!body.groupCode || s.groupCode === body.groupCode)) ||
    candidates[0]
  );
}

export function createCase(db, body) {
  if (!body.studentId) throw new DomainError(400, 'STUDENT_REQUIRED', '请提供学员编号');
  findStudent(db, body.studentId);

  const classType = body.classType || SELECTED_CLASS_TYPE;
  if (classType !== SELECTED_CLASS_TYPE) {
    throw new DomainError(
      400,
      'CLASS_TYPE_NOT_OFFERED',
      '本服务仅选开“保留原件辅导”班型；短环重练班未开办',
      { offered: SELECTED_CLASS_TYPE, requested: classType },
    );
  }

  const segment = selectSegment(db, body);
  const gapKey = `${body.studentId}:${segment.gapKey}`;
  const existing = db.cases.find((c) => c.gapKey === gapKey && c.status !== 'cancelled');
  if (existing) return { case: existing, existing: true };

  const sessionId = body.sessionId || db.meta.nextOpenSessionId;
  const session = db.sessions.find((s) => s.id === sessionId);
  if (!session || session.kind !== 'open_studio') {
    throw new DomainError(400, 'OPEN_SESSION_NOT_FOUND', `找不到可办理保留原件辅导的开放台：${sessionId}`);
  }

  let reservation = null;
  if (segment.retainable) {
    const piece = findPiece(db, segment.pieceNo, segment.pieceUid);
    if (!piece || piece.recycled) {
      throw new DomainError(422, 'PIECE_RECYCLED', '锚点泥件已经回收重揉，不能按保留原件连续段办理', {
        pieceNo: segment.pieceNo,
      });
    }
    if (piece.reservationForCaseId) {
      const other = db.cases.find((c) => c.id === piece.reservationForCaseId);
      if (other && other.status !== 'cancelled') {
        throw new DomainError(409, 'PIECE_ALREADY_RESERVED', '这件泥件已为其他缺口保留', {
          caseId: other.id,
          forStudentId: other.studentId,
        });
      }
    }

    const tableId = body.tableId || assignTable(db, session);
    const table = db.tables.find((t) => t.id === tableId);
    if (!table || !session.tableIds.includes(tableId)) {
      throw new DomainError(400, 'TABLE_NOT_IN_SESSION', '指定台号不属于该开放台场次');
    }

    reservation = {
      forStudentId: body.studentId,
      pieceNo: piece.no,
      pieceUid: piece.uid,
      storageSlot: piece.storageSlot || `待保留架/${piece.no}`,
      sessionId,
      tableId,
      tableName: table.name,
      status: 'held',
      decidedAt: new Date().toISOString(),
    };

    piece.status = 'reserved';
    piece.reservationForCaseId = null; // set after id creation below
    piece.continueTableId = tableId;
  }

  const id =
    body.id ||
    `CASE-${body.studentId}__${segment.groupCode}__${segment.linkIds.join('_')}`.replace(
      /[^A-Za-z0-9_-]/g,
      '_',
    );
  const makeupCase = {
    id,
    studentId: body.studentId,
    classType,
    groupCode: segment.groupCode,
    gapKey,
    segment: segment.actionCodes,
    segmentNames: segment.segmentNames,
    linkIds: segment.linkIds,
    requiredNewActions: segment.requiredNewActions,
    shortestTeachableSegment: segment.shortestTeachableSegment,
    repairKind: segment.repairKind,
    status: reservation ? 'reserved' : 'action_required',
    reservation,
    attendanceId: null,
    receiptId: null,
    stampId: null,
    mentorDecision: null,
    confirmations: [],
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };

  if (reservation) {
    const piece = findPiece(db, reservation.pieceNo, reservation.pieceUid);
    piece.reservationForCaseId = makeupCase.id;
  }

  db.cases.push(makeupCase);
  log(db, 'STUDENT', 'makeup_case.create', `${body.studentId} 发起补修单 ${id}`);
  return { case: makeupCase, existing: false, diagnosis: segment };
}

function assignTable(db, session) {
  const counts = new Map(session.tableIds.map((id) => [id, 0]));
  for (const c of db.cases) {
    if (c.reservation?.sessionId === session.id && c.status !== 'cancelled') {
      counts.set(c.reservation.tableId, (counts.get(c.reservation.tableId) || 0) + 1);
    }
  }
  for (const tableId of session.tableIds) {
    const table = db.tables.find((t) => t.id === tableId);
    if ((counts.get(tableId) || 0) < table.capacity) return tableId;
  }
  throw new DomainError(409, 'NO_TABLE_CAPACITY', '下一次开放台所有拉坯台均已满');
}

export function checkIn(db, caseId, body) {
  const c = getCase(db, caseId);
  if (c.attendanceId) {
    return { attendance: db.attendances.find((a) => a.id === c.attendanceId), existing: true, case: c };
  }
  if (!c.reservation) {
    throw new DomainError(422, 'NO_RETENTION_RESERVATION', '该补修单没有保留原件/台位安排');
  }

  const sessionId = body.sessionId || c.reservation.sessionId;
  if (sessionId !== c.reservation.sessionId) {
    throw new DomainError(422, 'SESSION_MISMATCH', '请到补修单保留的下一次开放台真实到课', {
      expected: c.reservation.sessionId,
      received: sessionId,
    });
  }
  const tableId = body.tableId || c.reservation.tableId;
  if (tableId !== c.reservation.tableId) {
    throw new DomainError(422, 'TABLE_MISMATCH', '台号与保留安排不一致', {
      expected: c.reservation.tableId,
      received: tableId,
    });
  }

  const piece = findPiece(db, c.reservation.pieceNo, c.reservation.pieceUid);
  if (!piece || piece.recycled) {
    c.status = 'blocked';
    throw new DomainError(422, 'PIECE_RECYCLED', '保留泥件已被回收重揉，不能继续同件连续段');
  }

  const attendance = {
    id: nextId(db, 'attendance', 'ATT'),
    caseId: c.id,
    studentId: c.studentId,
    sessionId,
    tableId,
    confirmedBy: body.confirmedBy || body.mentorId || 'MENTOR-ON-DUTY',
    attended: body.attended !== false,
    at: body.at || new Date().toISOString(),
    realAttendanceReceipt: true,
  };
  db.attendances.push(attendance);
  c.attendanceId = attendance.id;
  c.status = 'attended';
  c.updatedAt = new Date().toISOString();
  piece.status = 'at_table';
  log(db, attendance.confirmedBy, 'attendance.check_in', `${c.studentId} 真实到课 ${sessionId} ${tableId}`);
  return { attendance, existing: false, case: c };
}

export function addReceipt(db, caseId, body) {
  const c = getCase(db, caseId);
  if (c.receiptId) {
    const receipt = db.receipts.find((r) => r.id === c.receiptId);
    return { receipt, existing: true, case: c };
  }
  if (!c.attendanceId) {
    throw new DomainError(409, 'ATTENDANCE_REQUIRED', '须先记录学员真实到课，才能交完成回执');
  }

  const actions = Array.isArray(body.actions)
    ? body.actions
    : body.action
      ? [{ action: body.action, mode: body.mode || 'hands_on' }]
      : null;
  if (!actions || actions.length === 0) {
    throw new DomainError(400, 'ACTIONS_REQUIRED', '回执必须包含真实完成的符号动作');
  }

  for (const a of actions) {
    const mode = a.mode || 'hands_on';
    if (mode !== 'hands_on' || a.independence === 'assisted') {
      log(db, body.confirmedBy || 'TA', 'receipt.rejected_observation', `补修单 ${c.id} 提交非亲手回执`);
      throw new DomainError(422, 'HANDS_ON_REQUIRED', '该结课动作组要求学员亲手独立完成；观摩/助教把手不能换结课章', {
        receivedMode: mode,
      });
    }
  }

  const expectedPieceNo = c.reservation?.pieceNo || body.pieceNo;
  if (!expectedPieceNo) {
    throw new DomainError(400, 'PIECE_REQUIRED', '完成回执必须写明泥件号');
  }
  const expectedPieceUid = c.reservation?.pieceUid || body.pieceUid || null;
  const receiptPiece = findPiece(db, expectedPieceNo, expectedPieceUid);
  if (receiptPiece?.recycled) {
    throw new DomainError(422, 'PIECE_RECYCLED', '泥件已回收重揉，不能认定补修连续段；须按导师通知重做新段');
  }

  if (c.reservation) {
    const pieceNo = body.pieceNo || c.reservation.pieceNo;
    const pieceUid = body.pieceUid || c.reservation.pieceUid;
    if (pieceNo !== c.reservation.pieceNo || (body.pieceUid && body.pieceUid !== c.reservation.pieceUid)) {
      throw new DomainError(422, 'PIECE_MISMATCH', '回执泥件与保留原件不一致，不能与原件上的前段拼接', {
        expected: { pieceNo: c.reservation.pieceNo, pieceUid: c.reservation.pieceUid },
        received: { pieceNo, pieceUid: body.pieceUid || null },
      });
    }
    const piece = findPiece(db, pieceNo, pieceUid);
    if (!piece || piece.recycled) {
      throw new DomainError(422, 'PIECE_RECYCLED', '泥件已回收重揉，不能认定连续段');
    }
  }

  const record = createRecord(db, {
    studentId: c.studentId,
    sessionId: body.sessionId || c.reservation?.sessionId || db.meta.nextOpenSessionId,
    pieceNo: expectedPieceNo,
    pieceUid: expectedPieceUid || undefined,
    confirmedBy: body.confirmedBy || 'TA-ON-DUTY',
    note: `补修单 ${c.id} 真实完成回执`,
    events: actions.map((a, i) => ({
      action: a.action,
      mode: 'hands_on',
      independence: a.independence || 'independent',
      seq: a.seq ?? i + 10,
      at: a.at || body.at || new Date(Date.now() + i * 1000).toISOString(),
      confirmedBy: body.confirmedBy || 'TA-ON-DUTY',
    })),
  });

  const receipt = {
    id: nextId(db, 'receipt', 'RCP'),
    caseId: c.id,
    studentId: c.studentId,
    recordId: record.id,
    eventIds: record.events.map((e) => e.id),
    actions: actions.map((a) => a.action),
    pieceNo: record.pieceNo,
    pieceUid: record.pieceUid,
    confirmedBy: body.confirmedBy || 'TA-ON-DUTY',
    at: new Date().toISOString(),
    realCompletionReceipt: true,
  };
  db.receipts.push(receipt);
  c.receiptId = receipt.id;
  c.status = 'receipted';
  c.updatedAt = new Date().toISOString();

  if (c.reservation) {
    const piece = findPiece(db, c.reservation.pieceNo, c.reservation.pieceUid);
    if (piece) piece.status = 'completed_pending_mentor';
  }
  log(db, receipt.confirmedBy, 'receipt.create', `补修单 ${c.id} 收到真实完成回执 ${receipt.id}`);
  return { receipt, record, existing: false, case: c };
}

export function processMentor(db, caseId, body = {}) {
  const c = getCase(db, caseId);
  const mentorId = body.mentorId || body.confirmedBy;
  if (!mentorId) throw new DomainError(400, 'MENTOR_REQUIRED', '请提供办理导师编号');

  const evaluation = evaluateStudent(db, c.studentId);
  const group = evaluation.groups.find((g) => g.code === c.groupCode);
  const targetSegment = evaluation.outstandingSegments.find((s) => s.gapKey === c.gapKey.split(':').slice(1).join(':'));
  const isComplete = group?.satisfied;

  if (c.status === 'certified' && c.stampId) {
    const stamp = db.certificates.find((x) => x.id === c.stampId);
    c.confirmations.push({
      mentorId,
      at: new Date().toISOString(),
      outcome: 'duplicate_no_new_stamp',
      idempotencyKey: body.idempotencyKey || null,
    });
    c.updatedAt = new Date().toISOString();
    log(db, mentorId, 'mentor.confirm_duplicate', `重复确认 ${c.id}，不新发印章`);
    return {
      duplicate: true,
      created: false,
      case: c,
      stamp,
      signedDecision: c.mentorDecision,
      evaluation,
      message: '该缺口已经签发，重送或第二名导师并发办理均只保留同一份结课章。',
    };
  }

  if (body.decision === 'certify' && !isComplete) {
    throw new DomainError(422, 'GAP_STILL_OUTSTANDING', '当前规则下动作组尚未由学员亲手完整完成，不能签发结课章', {
      evaluation,
      targetSegment,
    });
  }

  const decision = {
    id: nextId(db, 'decision', 'DEC'),
    caseId: c.id,
    mentorId,
    at: new Date().toISOString(),
    rulebookVersion: db.rulebook.version,
    decision: isComplete ? 'certified' : 'attend_next_class',
    completedGroups: evaluation.completedGroups,
    transferableCredits: evaluation.transferableCredits.filter((x) => x.satisfied),
    outstandingSegments: evaluation.outstandingSegments,
    stampId: null,
    statement: '',
  };

  let stamp = null;
  if (isComplete) {
    const stampId = `STAMP-${c.studentId}-${c.groupCode}`;
    stamp = db.certificates.find((x) => x.id === stampId);
    if (!stamp) {
      const groupDef = db.rulebook.groups.find((g) => g.code === c.groupCode);
      stamp = {
        id: stampId,
        studentId: c.studentId,
        groupCode: c.groupCode,
        title: groupDef?.stampTitle || '完整动作结课章',
        issuedAt: new Date().toISOString(),
        mentorId,
        caseId: c.id,
        rulebookVersion: db.rulebook.version,
        evidenceEventIds: group.chosenEvidence.map((e) => e.id),
      };
      db.certificates.push(stamp);
    }
    decision.stampId = stamp.id;
    decision.statement = `已签明 ${evaluation.student.name}（${c.studentId}）学成「${group.name}」，结课章 ${stamp.id}；无待补动作段。`;
    c.status = 'certified';
    c.stampId = stamp.id;
    if (c.reservation) {
      const piece = findPiece(db, c.reservation.pieceNo, c.reservation.pieceUid);
      if (piece) piece.status = 'completed';
    }
    log(db, mentorId, 'mentor.certify', `${c.studentId} 签发 ${stamp.id}`);
  } else {
    const next = targetSegment || evaluation.outstandingSegments[0];
    decision.statement = next
      ? `已核对历史与现行规则：${evaluation.student.name} 已完成 ${evaluation.completedGroups
          .map((g) => g.name)
          .join('、') || '若干可迁移项'}；仍须本人下一班实际补做「${next.segmentNames.join(' → ')}」，不能以跨件动作或观摩替代。`
      : '动作组尚未完成，请安排下一班实际补做。';
    c.status = next?.retainable ? 'needs_in_person_on_retained_piece' : 'needs_in_person';
    log(db, mentorId, 'mentor.next_class_notice', `${c.id} 签发下班补做通知`);
  }

  c.mentorDecision = decision;
  c.confirmations.push({
    mentorId,
    at: decision.at,
    outcome: decision.decision,
    decisionId: decision.id,
    idempotencyKey: body.idempotencyKey || null,
  });
  c.updatedAt = decision.at;

  return { duplicate: false, created: Boolean(stamp), case: c, stamp, signedDecision: decision, evaluation };
}

export function getCase(db, caseId) {
  const c = db.cases.find((x) => x.id === caseId);
  if (!c) throw new DomainError(404, 'CASE_NOT_FOUND', `找不到补修单：${caseId}`);
  return c;
}

export function listStamps(db, query = {}) {
  let stamps = db.certificates;
  if (query.studentId) stamps = stamps.filter((s) => s.studentId === query.studentId);
  return stamps;
}

export function updateEvent(db, eventId, body) {
  for (const record of db.records) {
    const event = record.events.find((e) => e.id === eventId);
    if (!event) continue;
    const before = clone(event);
    if (body.mode !== undefined) {
      event.mode = body.mode;
      event.independence =
        body.independence ||
        (body.mode === 'observed' ? 'not_applicable' : body.mode === 'assisted' ? 'assisted' : 'independent');
    }
    if (body.independence !== undefined) event.independence = body.independence;
    if (body.action !== undefined) event.action = body.action;
    if (body.seq !== undefined) event.seq = Number(body.seq);
    if (body.at !== undefined) event.at = body.at;
    if (body.confirmedBy !== undefined) event.confirmedBy = body.confirmedBy;
    db.meta.historyRevision += 1;
    log(db, body.changedBy || 'VERIFIER', 'event.update', `事件 ${eventId} 从 ${JSON.stringify(before)} 改为 ${JSON.stringify(event)}`);
    return { recordId: record.id, before, after: event };
  }
  throw new DomainError(404, 'EVENT_NOT_FOUND', `找不到动作事件：${eventId}`);
}

export function updateRule(db, linkId, body) {
  let target = null;
  let targetGroup = null;
  for (const group of db.rulebook.groups) {
    const link = group.links.find((l) => l.id === linkId);
    if (link) {
      target = link;
      targetGroup = group;
      break;
    }
  }
  if (!target) throw new DomainError(404, 'RULE_NOT_FOUND', `找不到衔接规定：${linkId}`);

  const before = clone(target);
  const booleanAliases = {
    samePiece: ['samePiece', 'same_piece', 'mustSamePiece', '必须同件', '同件'],
    sameContinuousChain: ['sameContinuousChain', 'same_continuous_chain', '同一连续练习', '连续练习'],
    supplementByAudit: ['supplementByAudit', 'allowObservedSupplement', 'allow_audit', '允许旁听'],
    supplementByPersonalAssignment: ['supplementByPersonalAssignment', 'allowPersonalAssignment', '允许个人作业'],
  };

  for (const [field, aliases] of Object.entries(booleanAliases)) {
    for (const key of aliases) {
      if (body[key] !== undefined) {
        target[field] = Boolean(body[key]);
        break;
      }
    }
  }
  if (body.acceptedModes || body.accepted_modes) {
    target.acceptedModes = body.acceptedModes || body.accepted_modes;
  }
  if (body.description) target.description = body.description;

  db.rulebook.version += 1;
  log(db, body.changedBy || 'RULE-ADMIN', 'rule.update', `${linkId} 从 ${JSON.stringify(before)} 改为 ${JSON.stringify(target)}`);
  return { groupCode: targetGroup.code, before, after: target, rulebookVersion: db.rulebook.version };
}

export function recyclePiece(db, pieceNo, body = {}) {
  const piece = findPiece(db, pieceNo, body.pieceUid || null);
  if (!piece) throw new DomainError(404, 'PIECE_NOT_FOUND', `找不到泥件：${pieceNo}`);
  piece.recycled = true;
  piece.status = 'recycled';
  piece.recycledAt = body.at || new Date().toISOString();
  piece.recycleNote = body.note || '已回收重揉';
  if (piece.reservationForCaseId) {
    const c = db.cases.find((x) => x.id === piece.reservationForCaseId);
    if (c && c.status !== 'certified') c.status = 'blocked';
  }
  log(db, body.actor || 'STUDIO', 'piece.recycle', `泥件 ${pieceNo} 已回收重揉，不能再用于连续段`);
  return piece;
}

export function log(db, actor, action, message) {
  db.auditLogs.push({ at: new Date().toISOString(), actor, action, message });
  if (db.auditLogs.length > 500) db.auditLogs.splice(0, db.auditLogs.length - 500);
}

export function runBuiltInScenario(db) {
  // A deterministic summary used by the UI's one-click verification panel.
  const jia = evaluateStudent(db, 'S-JIA');
  const yiBefore = evaluateStudent(db, 'S-YI');
  const demo = findDemoFillablePractice(db);
  return {
    selectedClassType: SELECTED_CLASS_TYPE,
    jiaGap: jia.outstandingSegments.find((s) => s.linkIds.includes('L-WALL-CLOSE')),
    yiCrossPieceSatisfiedBeforeRuleChange: Boolean(
      yiBefore.groups.find((g) => g.code === 'HANDLE_JOIN_CONTROL')?.satisfied,
    ),
    demoMatches: demo,
  };
}
