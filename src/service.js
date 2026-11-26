import crypto from 'node:crypto';
import { MemoryStore, createInitialState } from './store.js';
import {
  MAX_SOURCES,
  summarizeCompatibility,
  sourceReachesPoint,
} from './topology.js';
import { butlerRecommendation } from './policy.js';

export class DomainError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'DomainError';
    this.code = code;
    this.details = details;
  }
}

function uuid(prefix) {
  return `${prefix}_${crypto.randomUUID()}`;
}

function time(value) {
  if (!value) return NaN;
  return new Date(value).getTime();
}

function withinWindow(at, window) {
  const instant = time(at);
  if (!Number.isFinite(instant) || !window) return false;
  const start = time(window.start);
  const end = time(window.end);
  if (Number.isFinite(start) && instant < start) return false;
  if (Number.isFinite(end) && instant > end) return false;
  return true;
}

function stableFingerprint(value) {
  return crypto
    .createHash('sha1')
    .update(JSON.stringify(value))
    .digest('hex')
    .slice(0, 16);
}

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

export class RepairService {
  constructor(options = {}) {
    this.store = options.store ?? new MemoryStore();
    this.clock = options.clock ?? (() => new Date().toISOString());
    this.state = this.store.readState() ?? createInitialState(this.clock());
  }

  save() {
    this.store.writeState(this.state);
  }

  reset(state = createInitialState(this.clock())) {
    this.state = state;
    this.save();
    return this.state;
  }

  audit(action, details = {}, at = this.clock()) {
    this.state.audit.push({
      id: uuid('AUD'),
      at,
      action,
      details,
    });
  }

  // ---------- 基础查询与校验 ----------

  getTicketOrThrow(ticketId) {
    const ticket = this.state.tickets.find((item) => item.id === ticketId);
    if (!ticket) {
      throw new DomainError('TICKET_NOT_FOUND', `找不到住户单 ${ticketId}`, {
        ticketId,
      });
    }
    return ticket;
  }

  getCaseOrThrow(caseId) {
    const workCase = this.state.cases.find((item) => item.id === caseId);
    if (!workCase) {
      throw new DomainError('CASE_NOT_FOUND', `找不到共案 ${caseId}`, {
        caseId,
      });
    }
    return workCase;
  }

  getInspectionOrThrow(evidenceId) {
    const inspection = this.state.inspections.find(
      (item) => item.id === evidenceId,
    );
    if (!inspection) {
      throw new DomainError(
        'INSPECTION_NOT_FOUND',
        `找不到检查/试水记录 ${evidenceId}`,
        { evidenceId },
      );
    }
    return inspection;
  }

  // ---------- 楼栋拓扑与住户单 ----------

  setTopology(topology) {
    if ((topology.sources ?? []).length > MAX_SOURCES) {
      throw new DomainError(
        'TOO_MANY_SOURCES',
        `候选源头至多 ${MAX_SOURCES} 处`,
      );
    }
    const sourceIds = new Set();
    for (const source of topology.sources ?? []) {
      if (!source.id) {
        throw new DomainError('VALIDATION', '源头必须包含 id');
      }
      if (sourceIds.has(source.id)) {
        throw new DomainError('VALIDATION', `源头 id 重复：${source.id}`);
      }
      sourceIds.add(source.id);
    }
    this.state.topology = {
      sources: topology.sources ?? [],
      segments: topology.segments ?? [],
      points: topology.points ?? [],
      edges: topology.edges ?? [],
    };
    this.audit('topology.configured', {
      sourceIds: this.state.topology.sources.map((source) => source.id),
    });
    this.save();
    return this.state.topology;
  }

  createTicket(input = {}) {
    if (this.state.tickets.length >= 6) {
      throw new DomainError(
        'TICKET_LIMIT',
        '小楼栋服务至多开立六张住户单',
        { limit: 6 },
      );
    }
    if (!input.residentName || !input.symptom || !input.location) {
      throw new DomainError(
        'VALIDATION',
        '住户单至少需要住户、现象和位置',
      );
    }

    const id = input.id ?? uuid('T');
    if (this.state.tickets.some((ticket) => ticket.id === id)) {
      throw new DomainError('VALIDATION', `住户单 id 已存在：${id}`);
    }

    const kind = input.contract?.kind ?? input.contractKind ?? 'standard';
    const contract = this.buildContract(kind, input.contract ?? {});
    const at = input.createdAt ?? this.clock();

    const ticket = {
      id,
      residentId: input.residentId ?? uuid('U'),
      residentName: input.residentName,
      unit: input.unit ?? input.location,
      location: input.location,
      adjacentTo: input.adjacentTo ?? [],
      pointIds: input.pointIds ?? [],
      report: {
        symptom: input.symptom,
        detail: input.detail ?? '',
        reportedAt: at,
      },
      entryPermission: {
        granted: input.entryGranted ?? true,
        windows: input.entryWindows ?? [],
      },
      revisitWindows: input.revisitWindows ?? [],
      contract,
      treatmentType: input.treatmentType ?? contract.defaultTreatmentType,
      treatmentCompletedAt: null,
      status: 'reported',
      openRecheckId: null,
      activeCaseId: null,
      closure: null,
      createdAt: at,
      updatedAt: at,
    };

    this.state.tickets.push(ticket);
    this.audit('ticket.created', { ticketId: id, kind }, at);
    this.save();
    return ticket;
  }

  buildContract(kind, overrides = {}) {
    const base = {
      kind,
      requiredRevisit: 'agreed_window_dry',
      revisitWindowId: null,
      requireRepairCoveringSymptom: true,
      excludeAllCompatibleSources: false,
      defaultTreatmentType: 'permanent',
    };

    if (kind === 'strict_source_exclusion') {
      base.excludeAllCompatibleSources = true;
    } else if (kind === 'temporary_dry_once') {
      base.requiredRevisit = 'once_dry_after_treatment';
      base.requireRepairCoveringSymptom = false;
      base.excludeAllCompatibleSources = false;
      base.defaultTreatmentType = 'temporary';
    }

    return { ...base, ...overrides };
  }

  // ---------- 检查、隔离试水与公区结论借用 ----------

  recordInspection(input = {}) {
    if (!input.segmentId && !input.checkpointId && !input.pressureTest) {
      throw new DomainError(
        'VALIDATION',
        '检查记录至少需要检查点/管段或加压试验',
      );
    }
    const at = input.at ?? this.clock();
    const id = input.id ?? uuid('INS');
    if (this.state.inspections.some((item) => item.id === id)) {
      throw new DomainError('VALIDATION', `检查记录 id 已存在：${id}`);
    }

    const scope = input.scope ?? 'private';
    const inspection = {
      id,
      kind: input.kind ?? 'survey_observation',
      ticketId: input.ticketId ?? null,
      caseId: input.caseId ?? null,
      surveyorId: input.surveyorId ?? 'surveyor-default',
      at,
      checkpointId: input.checkpointId ?? null,
      segmentId: input.segmentId ?? null,
      isolatedSegmentIds: input.isolatedSegmentIds ?? [],
      coveredPointIds:
        input.coveredPointIds ??
        (input.readings ?? []).map((reading) => reading.pointId),
      readings: input.readings ?? [],
      pressureTest: input.pressureTest ?? null,
      controlled: input.controlled ?? false,
      conclusion: input.conclusion ?? null,
      confirmed: input.confirmed ?? true,
      access: {
        scope,
        // 私人室内观察一律不得作为可复制的邻单证据。
        authorized: input.authorized ?? scope !== 'private',
      },
      transferable: scope === 'public' && (input.transferable ?? true),
      reusableForTicketIds: input.reusableForTicketIds ?? [],
      retracted: false,
      retractedAt: null,
      retractReason: null,
      createdAt: at,
    };

    this.state.inspections.push(inspection);
    if (inspection.caseId) {
      const workCase = this.state.cases.find(
        (item) => item.id === inspection.caseId,
      );
      if (workCase && !workCase.inspectionIds.includes(id)) {
        workCase.inspectionIds.push(id);
      }
    }
    this.audit('inspection.recorded', { evidenceId: id }, at);
    this.save();
    return inspection;
  }

  retractInspection(evidenceId, reason = '撤下证据', at = this.clock()) {
    const inspection = this.getInspectionOrThrow(evidenceId);
    inspection.retracted = true;
    inspection.retractedAt = at;
    inspection.retractReason = reason;
    this.audit('inspection.retracted', { evidenceId, reason }, at);
    this.save();
    this.dispatchClosureNotices({ at });
    return inspection;
  }

  /** 收集本户合同可使用的证据：本单、曾属共案、明确借入的公区结论。 */
  getTicketEvidence(ticketId) {
    const borrows = this.state.borrows.filter(
      (borrow) => borrow.targetTicketId === ticketId && !borrow.revoked,
    );
    const borrowedEvidenceIds = new Set(
      borrows.map((borrow) => borrow.evidenceId),
    );

    const caseIds = new Set(
      this.state.cases
        .filter(
          (workCase) =>
            workCase.memberTicketIds.includes(ticketId) ||
            (workCase.formerMemberTicketIds ?? []).includes(ticketId),
        )
        .map((workCase) => workCase.id),
    );

    return this.state.inspections.filter((inspection) => {
      if (inspection.retracted) return false;
      if (inspection.ticketId === ticketId) return true;
      if (inspection.caseId && caseIds.has(inspection.caseId)) return true;
      if (borrowedEvidenceIds.has(inspection.id)) return true;
      return false;
    });
  }

  borrowEvidence(input = {}) {
    const evidence = this.getInspectionOrThrow(input.evidenceId);
    const target = this.getTicketOrThrow(input.targetTicketId);
    const at = input.at ?? this.clock();

    if (evidence.retracted) {
      throw new DomainError(
        'EVIDENCE_RETRACTED',
        '该检查结论已撤下，不能借出',
      );
    }

    if (evidence.access.scope === 'private' || !evidence.transferable) {
      throw new DomainError(
        'PRIVATE_EVIDENCE_NOT_TRANSFERABLE',
        '私人室内、无对外许可的观察不得复制到相邻工单',
        { evidenceId: evidence.id, targetTicketId: target.id },
      );
    }

    if (!evidence.confirmed) {
      throw new DomainError(
        'EVIDENCE_NOT_CONFIRMED',
        '只有已确认的公区管段结论可以转借',
      );
    }

    const adjacent =
      target.adjacentTo.includes(evidence.ticketId) ||
      evidence.reusableForTicketIds.includes(target.id) ||
      this.segmentRelatesToTicket(evidence.segmentId, target);
    if (!adjacent && !input.force) {
      throw new DomainError(
        'EVIDENCE_NOT_REUSABLE',
        '该公区管段结论与相邻工单不相关，不能转借',
      );
    }

    const existing = this.state.borrows.find(
      (borrow) =>
        borrow.evidenceId === evidence.id &&
        borrow.targetTicketId === target.id &&
        !borrow.revoked,
    );
    if (existing) return existing;

    const borrow = {
      id: input.id ?? uuid('BRW'),
      evidenceId: evidence.id,
      sourceTicketId: evidence.ticketId,
      targetTicketId: target.id,
      targetCaseId: input.targetCaseId ?? target.activeCaseId ?? null,
      // 只转用公开结论，不复制私人读数明细。
      publicConclusion: evidence.conclusion,
      segmentId: evidence.segmentId,
      borrowedAt: at,
      actorId: input.actorId ?? 'butler-default',
      revoked: false,
    };
    this.state.borrows.push(borrow);
    this.audit(
      'evidence.borrowed',
      { evidenceId: evidence.id, targetTicketId: target.id },
      at,
    );
    this.save();
    this.dispatchClosureNotices({ at });
    return borrow;
  }

  segmentRelatesToTicket(segmentId, ticket) {
    if (!segmentId) return false;
    if ((ticket.pointIds ?? []).some((pointId) => {
      const point = this.state.topology.points.find((p) => p.id === pointId);
      return point?.segmentId === segmentId;
    })) {
      return true;
    }
    const segment = this.state.topology.segments.find(
      (item) => item.id === segmentId,
    );
    return Boolean(segment?.public && ticket.adjacentTo.length > 0);
  }

  getCompatibility(ticketId) {
    this.getTicketOrThrow(ticketId);
    const evidence = this.getTicketEvidence(ticketId);
    const summary = summarizeCompatibility(this.state.topology, evidence);
    const repairedSourceIds = this.getTicketRepairs(ticketId).flatMap(
      (repair) => repair.sourceIds ?? [],
    );
    return {
      ...summary,
      evidenceIds: evidence.map((item) => item.id),
      repairedSourceIds: unique(repairedSourceIds),
      unresolvedSourceIds: unique(
        summary.compatibleSourceIds.filter(
          (sourceId) => !unique(repairedSourceIds).includes(sourceId),
        ),
      ),
    };
  }

  // ---------- 共案治理 ----------

  createCase(input = {}) {
    const ticketIds = unique(input.ticketIds ?? []);
    if (ticketIds.length < 2) {
      throw new DomainError(
        'VALIDATION',
        '共案至少包含两张已获入户许可的住户单',
      );
    }
    const tickets = ticketIds.map((id) => this.getTicketOrThrow(id));
    for (const ticket of tickets) {
      if (!ticket.entryPermission.granted) {
        throw new DomainError(
          'ENTRY_PERMISSION_REQUIRED',
          `住户 ${ticket.id} 尚未给予入户许可，不能纳入共案`,
          { ticketId: ticket.id },
        );
      }
      if (ticket.activeCaseId) {
        throw new DomainError(
          'TICKET_ALREADY_IN_CASE',
          `住户单 ${ticket.id} 已在共案 ${ticket.activeCaseId}`,
          { ticketId: ticket.id, caseId: ticket.activeCaseId },
        );
      }
    }

    const id = input.id ?? uuid('CASE');
    if (this.state.cases.some((item) => item.id === id)) {
      throw new DomainError('VALIDATION', `共案 id 已存在：${id}`);
    }
    const at = input.at ?? this.clock();
    const workCase = {
      id,
      kind: 'joint_repair',
      butlerPolicy:
        input.butlerPolicy ??
        'permitted_overlap_first_borrow_public_conclusion_when_fresh',
      memberTicketIds: ticketIds,
      formerMemberTicketIds: [],
      promisedResultTicketIds: [...ticketIds],
      status: 'planning',
      inspectionIds: [],
      inspectionOrder: [],
      plan: null,
      dispatchedAt: null,
      splitHistory: [],
      createdAt: at,
    };
    this.state.cases.push(workCase);
    for (const ticket of tickets) ticket.activeCaseId = id;
    this.audit('case.created', { caseId: id, ticketIds }, at);
    this.save();
    return workCase;
  }

  planCase(input = {}) {
    const workCase = this.getCaseOrThrow(input.caseId);
    if (workCase.status === 'dispatched' || workCase.status === 'in_progress') {
      throw new DomainError(
        'CASE_ALREADY_DISPATCHED',
        '必须在发队前确定检查次序；已发队后不能重排',
      );
    }
    const inspectionOrder = input.inspectionOrder ?? input.orderedSteps ?? [];
    if (!Array.isArray(inspectionOrder) || inspectionOrder.length === 0) {
      throw new DomainError('VALIDATION', '检查次序不能为空');
    }
    workCase.inspectionOrder = inspectionOrder;
    workCase.plan = {
      inspectionOrder,
      note: input.note ?? '发队前确定的检查/试水次序',
      decidedAt: input.at ?? this.clock(),
    };
    workCase.status = 'planned';
    this.audit(
      'case.planned',
      { caseId: workCase.id, inspectionOrder },
      input.at,
    );
    this.save();
    return workCase;
  }

  dispatchCase(input = {}) {
    const workCase = this.getCaseOrThrow(input.caseId);
    if (!workCase.plan || workCase.inspectionOrder.length === 0) {
      throw new DomainError(
        'PLAN_REQUIRED',
        '发队前必须先决定检查次序',
        { caseId: workCase.id },
      );
    }
    const at = input.at ?? this.clock();
    workCase.status = 'dispatched';
    workCase.dispatchedAt = at;
    workCase.dispatchedTo = input.actorId ?? 'repair-crew-default';
    this.audit('case.dispatched', { caseId: workCase.id }, at);
    this.save();
    return workCase;
  }

  /**
   * 现象后来证实不同源时拆案。
   * 不删除 revisits/notifications/signoffs，因此已回访/已通知户的结果保留。
   */
  splitCase(input = {}) {
    const workCase = this.getCaseOrThrow(input.caseId);
    const splitTicketIds = unique(input.ticketIds ?? []);
    if (splitTicketIds.length === 0) {
      throw new DomainError('VALIDATION', '拆案至少指定一张住户单');
    }
    for (const ticketId of splitTicketIds) {
      if (!workCase.memberTicketIds.includes(ticketId)) {
        throw new DomainError(
          'TICKET_NOT_IN_CASE',
          `住户单 ${ticketId} 不在共案 ${workCase.id} 中`,
        );
      }
    }

    const at = input.at ?? this.clock();
    const newCaseId = input.newCaseId ?? uuid('CASE_SPLIT');
    const preservedRevisitIds = this.state.revisits
      .filter((revisit) => splitTicketIds.includes(revisit.ticketId))
      .map((revisit) => revisit.revisitId);
    const preservedNotificationIds = this.state.notifications
      .filter((notification) => splitTicketIds.includes(notification.ticketId))
      .map((notification) => notification.id);

    workCase.memberTicketIds = workCase.memberTicketIds.filter(
      (id) => !splitTicketIds.includes(id),
    );
    workCase.formerMemberTicketIds = unique([
      ...(workCase.formerMemberTicketIds ?? []),
      ...splitTicketIds,
    ]);
    workCase.splitHistory.push({
      at,
      splitTicketIds,
      newCaseId,
      reason: input.reason ?? '证据显示不同源',
      preservedRevisitIds,
      preservedNotificationIds,
    });
    if (workCase.memberTicketIds.length === 0) workCase.status = 'split';

    const newCase = {
      id: newCaseId,
      kind: 'split_separate_commission',
      splitFromCaseId: workCase.id,
      butlerPolicy: 'separate_commission_borrow_only_public',
      memberTicketIds: splitTicketIds,
      formerMemberTicketIds: [],
      promisedResultTicketIds: [...splitTicketIds],
      status: 'planning',
      inspectionIds: [],
      inspectionOrder: [],
      plan: null,
      dispatchedAt: null,
      splitHistory: [],
      createdAt: at,
    };
    this.state.cases.push(newCase);

    for (const ticketId of splitTicketIds) {
      const ticket = this.getTicketOrThrow(ticketId);
      ticket.activeCaseId = newCaseId;
    }

    this.audit(
      'case.split',
      {
        originalCaseId: workCase.id,
        newCaseId,
        splitTicketIds,
        preservedRevisitIds,
        preservedNotificationIds,
      },
      at,
    );
    this.save();
    return {
      originalCase: workCase,
      newCase,
      preservedRevisitIds,
      preservedNotificationIds,
    };
  }

  recordJointRevisit(input = {}) {
    const workCase = this.getCaseOrThrow(input.caseId);
    const jointRevisitId =
      input.jointRevisitId ?? input.revisitId ?? uuid('JR');
    const at = input.at ?? this.clock();
    const results = input.results ?? [];

    const existing = this.state.revisits.filter(
      (revisit) => revisit.jointRevisitId === jointRevisitId,
    );
    if (existing.length > 0) {
      const notices = this.dispatchClosureNotices({ at });
      return {
        duplicate: true,
        jointRevisitId,
        revisits: existing,
        notices,
      };
    }

    for (const ticketId of workCase.memberTicketIds) {
      if (!results.some((result) => result.ticketId === ticketId)) {
        throw new DomainError(
          'JOINT_RESULT_MISSING',
          `共案承诺逐户取得回访结果，缺少 ${ticketId}`,
          { missingTicketId: ticketId },
        );
      }
    }

    const revisits = [];
    for (const result of results) {
      const recorded = this.recordRevisit({
        ...result,
        caseId: workCase.id,
        jointRevisitId,
        actorId: input.actorId ?? result.actorId ?? 'repair-crew-default',
        at: result.at ?? at,
      });
      revisits.push(recorded.revisit);
    }

    workCase.lastJointRevisitId = jointRevisitId;
    workCase.status = 'revisited';
    this.save();
    const notices = this.dispatchClosureNotices({ at });
    return { duplicate: false, jointRevisitId, revisits, notices };
  }

  // ---------- 模拟施工与维修登记 ----------

  simulateConstructionSteps(sourceIds = [], treatmentType = 'permanent') {
    const sourceText = sourceIds.length ? sourceIds.join('+') : '临时处置';
    return [
      { code: 'WORK_ORDER_ISSUED', simulated: true, label: '签发模拟施工单' },
      { code: 'VALVE_ISOLATED', simulated: true, label: '临时隔开相关管段' },
      { code: 'WORK_AREA_OPENED', simulated: true, label: '模拟打开检查面' },
      {
        code: 'SOURCE_TREATED',
        simulated: true,
        label: `模拟处理源头：${sourceText}`,
      },
      { code: 'PRESSURE_RESTORED', simulated: true, label: '模拟恢复管压' },
      { code: 'SURFACE_RESTORED', simulated: true, label: '模拟恢复饰面' },
      ...(treatmentType === 'temporary'
        ? [{ code: 'TEMPORARY_SEAL', simulated: true, label: '仅做临时封堵/导水' }]
        : []),
    ];
  }

  registerRepair(input = {}) {
    const at = input.at ?? this.clock();
    const sourceIds = unique(input.sourceIds ?? []);
    if (sourceIds.length > 2) {
      throw new DomainError(
        'TOO_MANY_REPAIRED_SOURCES',
        '一次现场模型至多处理两处独立源头',
      );
    }
    for (const sourceId of sourceIds) {
      if (!this.state.topology.sources.some((source) => source.id === sourceId)) {
        throw new DomainError('SOURCE_NOT_FOUND', `未知源头 ${sourceId}`);
      }
    }

    const id = input.id ?? uuid('REP');
    if (this.state.repairs.some((repair) => repair.id === id)) {
      throw new DomainError('VALIDATION', `维修记录 id 已存在：${id}`);
    }

    const repair = {
      id,
      ticketId: input.ticketId ?? null,
      caseId: input.caseId ?? null,
      sourceIds,
      actual: input.actual ?? true,
      treatmentType: input.treatmentType ?? 'permanent',
      note: input.note ?? '',
      at,
      simulatedConstruction: true,
      steps: this.simulateConstructionSteps(
        sourceIds,
        input.treatmentType ?? 'permanent',
      ),
    };
    this.state.repairs.push(repair);

    for (const ticketId of this.repairLinkedTicketIds(repair)) {
      const ticket = this.getTicketOrThrow(ticketId);
      ticket.treatmentCompletedAt = at;
      ticket.updatedAt = at;
    }

    this.audit(
      'repair.registered',
      { repairId: id, sourceIds, caseId: repair.caseId },
      at,
    );
    this.save();
    this.dispatchClosureNotices({ at });
    return repair;
  }

  repairLinkedTicketIds(repair) {
    const ids = [];
    if (repair.ticketId) ids.push(repair.ticketId);
    if (repair.caseId) {
      const workCase = this.state.cases.find(
        (item) => item.id === repair.caseId,
      );
      if (workCase) {
        ids.push(...workCase.memberTicketIds);
        ids.push(...(workCase.formerMemberTicketIds ?? []));
      }
    }
    return unique(ids);
  }

  getTicketRepairs(ticketId) {
    const caseIds = new Set(
      this.state.cases
        .filter(
          (workCase) =>
            workCase.memberTicketIds.includes(ticketId) ||
            (workCase.formerMemberTicketIds ?? []).includes(ticketId),
        )
        .map((workCase) => workCase.id),
    );
    return this.state.repairs.filter(
      (repair) =>
        repair.ticketId === ticketId ||
        (repair.caseId && caseIds.has(repair.caseId)),
    );
  }

  // ---------- 回访、复查与合同结案条件 ----------

  recordRevisit(input = {}) {
    const ticket = this.getTicketOrThrow(input.ticketId);
    const at = input.at ?? this.clock();
    const revisitId = input.revisitId ?? uuid('RV_BIZ');

    const existing = this.state.revisits.find(
      (revisit) =>
        revisit.ticketId === ticket.id && revisit.revisitId === revisitId,
    );
    if (existing) {
      if (
        input.result &&
        existing.result !== input.result
      ) {
        throw new DomainError(
          'IDEMPOTENCY_CONFLICT',
          `同一回访 ${revisitId} 已记录为 ${existing.result}，不能改写成 ${input.result}`,
        );
      }
      const notices = this.dispatchClosureNotices({ at });
      return { revisit: existing, duplicate: true, notices };
    }

    if (!['dry', 'wet'].includes(input.result)) {
      throw new DomainError('VALIDATION', '回访结果必须是 dry 或 wet');
    }

    const revisit = {
      id: uuid('RV'),
      revisitId,
      ticketId: ticket.id,
      caseId: input.caseId ?? ticket.activeCaseId ?? null,
      jointRevisitId: input.jointRevisitId ?? null,
      at,
      result: input.result,
      actorId: input.actorId ?? 'repair-crew-default',
      kind: input.kind ?? 'ordinary',
      recheckId: input.recheckId ?? null,
      note: input.note ?? '',
    };
    this.state.revisits.push(revisit);
    ticket.updatedAt = at;

    if (input.recheckId && ticket.openRecheckId === input.recheckId) {
      const recheck = this.state.recheckRequests.find(
        (item) => item.id === input.recheckId,
      );
      if (recheck) {
        recheck.status = 'completed';
        recheck.completedAt = at;
        recheck.result = input.result;
      }
      ticket.openRecheckId = null;
    }

    this.audit(
      'revisit.recorded',
      { ticketId: ticket.id, revisitId, result: input.result },
      at,
    );
    this.save();
    const notices = this.dispatchClosureNotices({ at });
    return { revisit, duplicate: false, notices };
  }

  requestRecheck(input = {}) {
    const ticket = this.getTicketOrThrow(input.ticketId);
    if (input.residentId && input.residentId !== ticket.residentId) {
      throw new DomainError(
        'PERMISSION_DENIED',
        '只有该住户可以提出复查',
      );
    }
    const at = input.at ?? this.clock();
    if (ticket.openRecheckId) {
      return this.state.recheckRequests.find(
        (item) => item.id === ticket.openRecheckId,
      );
    }

    const nextCycle = (ticket.closureCycle ?? 1) + 1;
    const recheck = {
      id: input.id ?? uuid('RC'),
      ticketId: ticket.id,
      residentId: ticket.residentId,
      reason: input.reason ?? '住户对结案/返潮结果有异议',
      observedWet: input.observedWet ?? null,
      requestedAt: at,
      status: 'open',
      closureCycle: nextCycle,
    };
    this.state.recheckRequests.push(recheck);
    ticket.openRecheckId = recheck.id;
    ticket.closureCycle = nextCycle;
    ticket.status = 'recheck_requested';
    const previousNotice = this.state.notifications
      .filter((notification) => notification.ticketId === ticket.id)
      .at(-1);
    if (previousNotice) previousNotice.status = 'disputed';
    ticket.closure = ticket.closure
      ? { ...ticket.closure, disputedAt: at }
      : ticket.closure;
    this.audit('recheck.requested', { ticketId: ticket.id }, at);
    this.save();
    return recheck;
  }

  findRevisitBasis(ticket) {
    const mine = this.state.revisits
      .filter((revisit) => revisit.ticketId === ticket.id)
      .sort((a, b) => time(a.at) - time(b.at));

    const contract = ticket.contract;
    let window = null;
    let qualifying = [];

    if (contract.requiredRevisit === 'agreed_window_dry') {
      window =
        ticket.revisitWindows.find(
          (item) => item.id === contract.revisitWindowId,
        ) ?? ticket.revisitWindows[0] ?? null;
      qualifying = mine.filter(
        (revisit) =>
          revisit.result === 'dry' && withinWindow(revisit.at, window),
      );
    } else if (contract.requiredRevisit === 'once_dry_after_treatment') {
      qualifying = mine.filter((revisit) => {
        if (revisit.result !== 'dry') return false;
        if (!ticket.treatmentCompletedAt) return true;
        return time(revisit.at) >= time(ticket.treatmentCompletedAt);
      });
    }

    const latestDry = qualifying.at(-1) ?? null;
    const laterWet = latestDry
      ? mine.some(
          (revisit) =>
            revisit.result === 'wet' &&
            time(revisit.at) > time(latestDry.at),
        )
      : false;
    const latestRevisit = mine.at(-1) ?? null;

    return {
      window,
      revisits: mine,
      latestDry,
      laterWet,
      latestRevisit,
      openRecheck: ticket.openRecheckId,
    };
  }

  evaluateClosure(ticketId) {
    const ticket = this.getTicketOrThrow(ticketId);
    const compatibility = this.getCompatibility(ticketId);
    const repairs = this.getTicketRepairs(ticketId);
    const revisitBasis = this.findRevisitBasis(ticket);
    const reasons = [];

    if (ticket.openRecheckId) {
      reasons.push({
        code: 'RECHECK_OPEN',
        message: '住户已提出复查，需先完成新一轮复查',
        recheckId: ticket.openRecheckId,
      });
    }

    if (ticket.contract.requiredRevisit !== 'none') {
      if (!revisitBasis.latestDry) {
        const hasWet = revisitBasis.revisits.some((r) => r.result === 'wet');
        const hasAny = revisitBasis.revisits.length > 0;
        let code = 'REVISIT_MISSING';
        let message = '本户尚未取得合同要求的干回访依据';
        if (
          ticket.contract.requiredRevisit === 'agreed_window_dry' &&
          hasAny &&
          !revisitBasis.window
        ) {
          code = 'REVISIT_WINDOW_MISSING';
          message = '合同未配置约定回访时段';
        } else if (
          ticket.contract.requiredRevisit === 'agreed_window_dry' &&
          hasAny
        ) {
          code = hasWet ? 'REVISIT_WET' : 'REVISIT_NOT_IN_AGREED_WINDOW';
          message = hasWet
            ? '回访见湿，现象尚无解除依据'
            : '已有回访但不在合同约定时段内且为干读数';
        }
        reasons.push({
          code,
          message,
          window: revisitBasis.window,
        });
      } else if (revisitBasis.laterWet) {
        reasons.push({
          code: 'REVISIT_WET_AFTER_DRY',
          message: '干回访后又出现湿读数，不能解除现象',
        });
      }
    }

    if (
      ticket.contract.excludeAllCompatibleSources &&
      compatibility.unresolvedSourceIds.length > 0
    ) {
      reasons.push({
        code: 'UNRESOLVED_SOURCE',
        message: '合同要求排除/处置每一个仍相容且未实际修过的源头',
        sourceIds: compatibility.unresolvedSourceIds,
        compatibleCombinations: compatibility.combinations,
      });
    }

    if (ticket.contract.requireRepairCoveringSymptom) {
      const repairedIds = unique(repairs.flatMap((r) => r.sourceIds ?? []));
      const coversSymptom = repairedIds.some((sourceId) =>
        (ticket.pointIds ?? []).some((pointId) =>
          sourceReachesPoint(
            this.state.topology,
            sourceId,
            pointId,
            [],
          ),
        ),
      );
      if (!coversSymptom) {
        reasons.push({
          code: 'REPAIR_NOT_COVER_SYMPTOM',
          message: '尚未登记实际修过且能沿连通关系覆盖本户现象点的源头',
          repairedSourceIds: repairedIds,
          ticketPointIds: ticket.pointIds,
        });
      }
    }

    const eligible = reasons.length === 0;
    const basisFingerprint = stableFingerprint({
      contract: ticket.contract,
      window: revisitBasis.window,
      dryRevisit: revisitBasis.latestDry
        ? {
            id: revisitBasis.latestDry.id,
            revisitId: revisitBasis.latestDry.revisitId,
            at: revisitBasis.latestDry.at,
            result: revisitBasis.latestDry.result,
          }
        : null,
      evidence: compatibility.evidenceIds,
      combinations: compatibility.combinations,
      repairs: repairs.map((repair) => repair.id),
      openRecheckId: ticket.openRecheckId,
      treatmentCompletedAt: ticket.treatmentCompletedAt,
    });

    return {
      ticketId: ticket.id,
      eligible,
      reasons,
      basisFingerprint,
      compatibility,
      repairs: repairs.map((repair) => ({
        id: repair.id,
        sourceIds: repair.sourceIds,
        treatmentType: repair.treatmentType,
        at: repair.at,
      })),
      revisit: revisitBasis.latestDry
        ? {
            revisitId: revisitBasis.latestDry.revisitId,
            at: revisitBasis.latestDry.at,
            result: revisitBasis.latestDry.result,
          }
        : null,
      window: revisitBasis.window,
    };
  }

  // ---------- 结案通知与签收 ----------

  dispatchClosureNotices(options = {}) {
    const at = options.at ?? this.clock();
    const onlyTicketId = options.ticketId ?? null;
    const issued = [];
    const blocked = [];

    for (const ticket of this.state.tickets) {
      if (onlyTicketId && ticket.id !== onlyTicketId) continue;
      const decision = this.evaluateClosure(ticket.id);
      if (!decision.eligible) {
        blocked.push({ ticketId: ticket.id, reasons: decision.reasons });
        continue;
      }

      // 同一结案周期内，重发同一回访、重复扫描或隔天重启都只返回原通知。
      // 只有住户提出复查并开启新周期后，才可能产生新的结案通知。
      const cycle = ticket.closureCycle ?? 1;
      const dedupeKey = `closure_notice:${ticket.id}:cycle:${cycle}`;
      const existing = this.state.notifications.find(
        (notification) => notification.dedupeKey === dedupeKey,
      );
      if (existing) {
        issued.push(existing);
        continue;
      }

      const notice = {
        id: uuid('NTF'),
        dedupeKey,
        type: 'closure_notice',
        channel: 'in_service',
        ticketId: ticket.id,
        residentId: ticket.residentId,
        title: `${ticket.id} 结案通知`,
        body: `本户“${ticket.report.symptom}”已在合同要求的回访时刻取得解除依据，可签收结案。`,
        closureDecision: {
          basisFingerprint: decision.basisFingerprint,
          revisit: decision.revisit,
          excludedSourceIds: decision.compatibility.excludedSourceIds,
          repairedSourceIds: decision.compatibility.repairedSourceIds,
        },
        issuedAt: at,
        status: 'delivered',
        signedAt: null,
      };
      this.state.notifications.push(notice);
      ticket.closure = {
        noticeId: notice.id,
        dedupeKey,
        basisFingerprint: decision.basisFingerprint,
        issuedAt: at,
        signedAt: null,
      };
      if (ticket.status !== 'closed') ticket.status = 'closure_notified';
      this.audit(
        'closure_notice.issued',
        { ticketId: ticket.id, notificationId: notice.id },
        at,
      );
      issued.push(notice);
    }

    this.save();
    return { issued, blocked };
  }

  issueClosureNotice(ticketId, options = {}) {
    const decision = this.evaluateClosure(ticketId);
    if (!decision.eligible) {
      throw new DomainError(
        'CLOSURE_NOT_ALLOWED',
        '尚未满足该住户合同的结案条件，不能发送结案通知',
        decision,
      );
    }
    const result = this.dispatchClosureNotices({
      ...options,
      ticketId,
    });
    const notice = result.issued.find((item) => item.ticketId === ticketId);
    if (!notice) {
      throw new DomainError('CLOSURE_NOT_ALLOWED', '结案通知未生成', decision);
    }
    return notice;
  }

  sign(input = {}) {
    const at = input.at ?? this.clock();
    const kind = input.kind ?? 'closure_receipt';

    if (input.role === 'maintainer' || kind === 'work_ack') {
      const signoff = {
        id: uuid('SIG'),
        ticketId: input.ticketId ?? null,
        notificationId: input.notificationId ?? null,
        actorId: input.actorId ?? 'repair-crew-default',
        role: 'maintainer',
        kind,
        at,
        note: input.note ?? '',
      };
      this.state.signoffs.push(signoff);
      this.audit('work.signed', { signoffId: signoff.id }, at);
      this.save();
      return signoff;
    }

    const ticket = this.getTicketOrThrow(input.ticketId);
    if (input.actorId && input.actorId !== ticket.residentId) {
      throw new DomainError('PERMISSION_DENIED', '只能由本户住户签收');
    }

    const notice = input.notificationId
      ? this.state.notifications.find((n) => n.id === input.notificationId)
      : this.state.notifications
          .filter((n) => n.ticketId === ticket.id)
          .at(-1);
    if (!notice || notice.type !== 'closure_notice') {
      throw new DomainError(
        'NOTICE_NOT_FOUND',
        '尚无可签收的结案通知',
      );
    }

    const currentDecision = this.evaluateClosure(ticket.id);
    if (!currentDecision.eligible) {
      throw new DomainError(
        'CLOSURE_NOT_ALLOWED',
        '结案依据已不满足合同条件，不能签收结案',
        currentDecision,
      );
    }

    const existing = this.state.signoffs.find(
      (signoff) =>
        signoff.notificationId === notice.id &&
        signoff.kind === 'closure_receipt' &&
        signoff.actorId === ticket.residentId,
    );
    if (existing) return existing;

    const signoff = {
      id: uuid('SIG'),
      ticketId: ticket.id,
      notificationId: notice.id,
      actorId: ticket.residentId,
      role: 'resident',
      kind: 'closure_receipt',
      at,
      note: input.note ?? '住户实际签收',
    };
    this.state.signoffs.push(signoff);
    notice.signedAt = at;
    notice.status = 'signed';
    ticket.status = 'closed';
    ticket.closedAt = at;
    if (ticket.closure) ticket.closure.signedAt = at;
    this.audit(
      'closure.signed',
      { ticketId: ticket.id, notificationId: notice.id },
      at,
    );
    this.save();
    return signoff;
  }

  // ---------- 下一步工作台 ----------

  getTicketNextStep(ticketId, role = 'resident') {
    const ticket = this.getTicketOrThrow(ticketId);
    const decision = this.evaluateClosure(ticketId);
    const notice = this.state.notifications
      .filter((n) => n.ticketId === ticket.id)
      .at(-1);

    if (role === 'resident') {
      if (ticket.openRecheckId) {
        return {
          code: 'WAIT_RECHECK_VISIT',
          text: '复查申请已受理，等待维修员再次入户查看干湿结果。',
          ticketId,
        };
      }
      if (notice && !notice.signedAt) {
        return {
          code: 'SIGN_CLOSURE_NOTICE',
          text: '已有结案通知，请实际签收；如仍有湿痕可提出复查。',
          ticketId,
          notificationId: notice.id,
          actions: [
            {
              method: 'POST',
              path: `/api/tickets/${ticketId}/sign`,
              body: { kind: 'closure_receipt', notificationId: notice.id },
            },
            {
              method: 'POST',
              path: `/api/tickets/${ticketId}/recheck-requests`,
              body: { reason: '仍有返潮/滴水' },
            },
          ],
        };
      }
      if (ticket.status === 'closed') {
        return {
          code: 'CLOSED_CAN_REQUEST_RECHECK',
          text: '本案已签收结案；若现象反复，可提出复查。',
          ticketId,
          actions: [
            {
              method: 'POST',
              path: `/api/tickets/${ticketId}/recheck-requests`,
              body: { reason: '结案后现象反复' },
            },
          ],
        };
      }
      if (!decision.eligible) {
        return {
          code: 'WAIT_CONTRACTUAL_VISIT_OR_EVIDENCE',
          text: '请等待物业按合同约定时段回访或完成源头排查；也可查看未满足条件。',
          ticketId,
          blockers: decision.reasons,
        };
      }
      return {
        code: 'WAIT_CLOSURE_NOTICE',
        text: '已满足结案依据，物业扫描后会发出不重复的结案通知。',
        ticketId,
        action: { method: 'POST', path: '/api/closures/run' },
      };
    }

    // maintainer / butler
    if (ticket.openRecheckId) {
      return {
        code: 'PERFORM_RECHECK',
        text: '该户提出复查，需优先入户记录新一轮干/湿结果。',
        ticketId,
        action: { method: 'POST', path: '/api/revisits' },
      };
    }
    if (this.state.inspections.filter((i) => i.ticketId === ticketId).length === 0 && !ticket.activeCaseId) {
      return {
        code: 'RECORD_SURVEY',
        text: '按住户许可时间入户，记录检查点、管段和隔离试水观察。',
        ticketId,
        action: { method: 'POST', path: '/api/inspections' },
      };
    }
    if (decision.compatibility.unresolvedSourceIds.length > 0) {
      return {
        code: 'EXCLUDE_OR_REPAIR_SOURCES',
        text: `仍有未处置相容源头：${decision.compatibility.unresolvedSourceIds.join('、')}；安排模拟加压试水或维修。`,
        ticketId,
        unresolvedSourceIds: decision.compatibility.unresolvedSourceIds,
        action: { method: 'POST', path: '/api/inspections' },
      };
    }
    if (
      ticket.contract.requireRepairCoveringSymptom &&
      decision.reasons.some((r) => r.code === 'REPAIR_NOT_COVER_SYMPTOM')
    ) {
      return {
        code: 'REGISTER_REPAIR',
        text: '登记实际修过的源头；施工工序仅作模拟。',
        ticketId,
        action: { method: 'POST', path: '/api/repairs' },
      };
    }
    if (decision.reasons.some((r) => r.code.startsWith('REVISIT'))) {
      return {
        code: 'RECORD_CONTRACTUAL_REVISIT',
        text: '按该户合同约定的回访时刻入户，并登记干/湿结果。',
        ticketId,
        action: { method: 'POST', path: '/api/revisits' },
      };
    }
    return {
      code: 'RUN_CLOSURE_SCAN',
      text: '结案依据已齐备，运行结案扫描；同一依据不会重复通知。',
      ticketId,
      action: { method: 'POST', path: '/api/closures/run' },
    };
  }

  getButlerRecommendation() {
    return butlerRecommendation(this.state);
  }

  getWorkbench(role = 'resident', actorId = null) {
    if (role === 'resident') {
      const tickets = this.state.tickets.filter(
        (ticket) => !actorId || ticket.residentId === actorId,
      );
      return {
        role,
        items: tickets.map((ticket) =>
          this.getTicketNextStep(ticket.id, 'resident'),
        ),
      };
    }

    const items = [];
    for (const workCase of this.state.cases) {
      if (workCase.status === 'planning' && !workCase.plan) {
        items.push({
          code: 'PLAN_CASE_ORDER',
          caseId: workCase.id,
          text: `共案 ${workCase.id} 发队前需决定检查次序。`,
          action: { method: 'POST', path: `/api/cases/${workCase.id}/plan` },
        });
      } else if (workCase.status === 'planned') {
        items.push({
          code: 'DISPATCH_CASE',
          caseId: workCase.id,
          text: `共案 ${workCase.id} 检查次序已定，可以发队。`,
          action: {
            method: 'POST',
            path: `/api/cases/${workCase.id}/dispatch`,
          },
        });
      }
    }
    for (const ticket of this.state.tickets) {
      items.push(this.getTicketNextStep(ticket.id, 'maintainer'));
    }
    return { role, actorId, items };
  }
}
