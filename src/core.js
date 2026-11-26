import { ApiError } from './errors.js';
import { EPS, clipInterval, mergeIntervals, subtractIntervals, meterCells } from './intervals.js';

export function nextId(db, key, prefix, pad = 3) {
  db.counters[key] = (db.counters[key] ?? 0) + 1;
  return `${prefix}${String(db.counters[key]).padStart(pad, '0')}`;
}

export function mustFind(rows, id, label) {
  const row = rows.find((x) => x.id === id);
  if (!row) throw new ApiError(404, `${label.toUpperCase()}_NOT_FOUND`, `${label} ${id} 不存在`);
  return row;
}

export function createCar(db, payload = {}) {
  const car = {
    id: payload.id ?? nextId(db, 'car', 'CAR'),
    name: payload.name ?? `清扫${db.cars.length + 1}号`,
    plate: payload.plate ?? null,
    activeTripId: null,
  };
  db.cars.push(car);
  db.audit.push({ at: payload.at ?? 0, type: 'car.created', carId: car.id });
  return car;
}

export function getMapView(db) {
  return db.links.map((link) => {
    const curb = db.curbs.find((c) => c.id === link.rightCurbId) ?? null;
    const rightMerchants = [];
    for (const merchant of db.merchants) {
      const intervals = (db.currentContracts[merchant.id] ?? [])
        .filter((f) => f.curbId === link.rightCurbId)
        .map((f) => ({ fromM: f.fromM, toM: f.toM, label: f.label ?? '' }));
      if (intervals.length) rightMerchants.push({ merchantId: merchant.id, merchantName: merchant.name, intervals });
    }
    return {
      linkId: link.id,
      direction: `${link.fromNodeId}->${link.toNodeId}`,
      fromNodeId: link.fromNodeId,
      toNodeId: link.toNodeId,
      lengthM: link.lengthM,
      source: { segmentId: link.originalSegmentId, fromM: link.sourceFromM, toM: link.sourceToM },
      rightCurb: curb,
      rightMerchants,
      superseded: link.superseded === true,
    };
  });
}

export function reassignFrontage(db, payload) {
  const at = payload.at ?? 0;
  assertInt(at, '改签时间 at');
  const from = mustFind(db.merchants, payload.fromMerchantId, '转出商户');
  const to = mustFind(db.merchants, payload.toMerchantId, '接收商户');
  const curbId = payload.curbId;
  mustFind(db.curbs, curbId, '路缘');
  const lo = Number(payload.fromM);
  const hi = Number(payload.toM);
  const removeInterval = [lo, hi];

  const fromFrontages = structuredClone(db.currentContracts[from.id] ?? []);
  const targetIndex = fromFrontages.findIndex((f) => f.curbId === curbId && Math.abs(f.fromM - lo) < EPS && Math.abs(f.toM - hi) < EPS);
  if (targetIndex >= 0) {
    fromFrontages.splice(targetIndex, 1);
  } else {
    const idx = fromFrontages.findIndex((f) => f.curbId === curbId && f.fromM <= lo + EPS && f.toM >= hi - EPS);
    if (idx < 0) throw new ApiError(404, 'FRONTAGE_NOT_FOUND', '转出商户没有这段门面');
    const f = fromFrontages[idx];
    fromFrontages.splice(idx, 1);
    if (lo - f.fromM > EPS) fromFrontages.push({ ...f, toM: lo });
    if (f.toM - hi > EPS) fromFrontages.push({ ...f, fromM: hi });
  }
  setContract(db, { merchantId: from.id, at, frontages: fromFrontages });

  const toFrontages = structuredClone(db.currentContracts[to.id] ?? []);
  toFrontages.push({ curbId, fromM: lo, toM: hi, label: payload.label ?? `由${from.name}分得` });
  setContract(db, { merchantId: to.id, at, frontages: toFrontages });
  return { from: from.id, to: to.id, interval: removeInterval, curbId };
}


export function assertInt(value, field) {
  if (!Number.isInteger(value)) throw new ApiError(400, 'INVALID_MINUTE', `${field} 必须是整数分钟`);
}

export function getLink(db, payload = {}) {
  if (payload.linkId) return mustFind(db.links, payload.linkId, '路段');
  const matches = db.links.filter(
    (l) => l.fromNodeId === payload.fromNodeId && l.toNodeId === payload.toNodeId,
  );
  if (matches.length === 1) return matches[0];
  if (matches.length > 1) {
    throw new ApiError(400, 'AMBIGUOUS_DIRECTION', '同一起终点存在多个小路段，请传 linkId');
  }
  throw new ApiError(404, 'LINK_NOT_FOUND', '找不到对应行驶方向的小路段');
}

export function strokeToCurb(link, downM, upM) {
  if (![downM, upM].every((n) => Number.isFinite(n))) {
    throw new ApiError(400, 'INVALID_POSITION', '开刷/收刷位置必须是数字（米）');
  }
  for (const p of [downM, upM]) {
    if (p < -EPS || p > link.lengthM + EPS) {
      throw new ApiError(400, 'POSITION_OUTSIDE_LINK', `刷头位置必须在 ${link.id} 的 0–${link.lengthM} 米内`);
    }
  }
  const lo = Math.min(downM, upM);
  const hi = Math.max(downM, upM);
  if (link.direction === 'ref') {
    return { curbId: link.rightCurbId, fromM: link.sourceFromM + lo, toM: link.sourceFromM + hi };
  }
  return { curbId: link.rightCurbId, fromM: link.sourceToM - hi, toM: link.sourceToM - lo };
}

export function setContract(db, { merchantId, at, frontages, allowOverlap = false }) {
  assertInt(at, '合同时间 at');
  mustFind(db.merchants, merchantId, '商户');
  if (!Array.isArray(frontages) || frontages.length === 0) {
    throw new ApiError(400, 'INVALID_FRONTAGES', '门面区间不能为空');
  }

  const normalized = frontages.map((f) => {
    const curb = mustFind(db.curbs, f.curbId, '路缘');
    const fromM = Number(f.fromM);
    const toM = Number(f.toM);
    if (!(fromM >= -EPS && toM <= curb.lengthM + EPS && toM - fromM > EPS)) {
      throw new ApiError(400, 'INVALID_FRONTAGE_INTERVAL', `门面区间无效：${f.curbId} [${f.fromM},${f.toM})`);
    }
    return { curbId: f.curbId, fromM, toM, label: f.label ?? '' };
  });

  const ownByCurb = new Map();
  for (const f of normalized) {
    const list = ownByCurb.get(f.curbId) ?? [];
    list.push([f.fromM, f.toM]);
    ownByCurb.set(f.curbId, list);
  }
  for (const [curbId, intervals] of ownByCurb) {
    const merged = mergeIntervals(intervals);
    if (merged.length < intervals.length) {
      throw new ApiError(409, 'OVERLAPPING_OWN_FRONTAGE', '同一商户的门面区间相互重叠，不能重复计活');
    }
    if (!allowOverlap) {
      for (const other of db.merchants.filter((m) => m.id !== merchantId)) {
        const others = (db.currentContracts[other.id] ?? [])
          .filter((f) => f.curbId === curbId)
          .map((f) => [f.fromM, f.toM]);
        for (const mine of merged) {
          const leftover = subtractIntervals([mine], others);
          const unchanged = leftover.length === 1 &&
            Math.abs(leftover[0][0] - mine[0]) < EPS &&
            Math.abs(leftover[0][1] - mine[1]) < EPS;
          if (!unchanged) {
            throw new ApiError(409, 'FRONTAGE_ALREADY_CONTRACTED', `${curbId} [${mine[0]},${mine[1]}) 与 ${other.name} 的当前门面重叠`, {
              merchantId: other.id,
            });
          }
        }
      }
    }
  }

  const version = { id: nextId(db, 'contractVersion', 'CV'), merchantId, at, frontages: structuredClone(normalized) };
  db.contractVersions.push(version);
  db.currentContracts[merchantId] = normalized;
  db.audit.push({ at, type: 'contract.updated', merchantId, versionId: version.id });
  return version;
}

export function createShift(db, { id, at, promisedEndAt, merchantIds }) {
  assertInt(at, '当班开始 at');
  assertInt(promisedEndAt, '承诺结束 promisedEndAt');
  if (promisedEndAt < at) throw new ApiError(400, 'BAD_PROMISE', '承诺结束时刻不能早于当班开始');

  const ids = merchantIds?.length ? merchantIds : db.merchants.map((m) => m.id);
  const shift = {
    id: id ?? nextId(db, 'shift', 'SHIFT'),
    startedAt: at,
    promisedEndAt,
    merchantIds: [...ids],
    orderIds: [],
    status: 'open',
  };
  db.shifts.push(shift);

  for (const merchantId of ids) {
    mustFind(db.merchants, merchantId, '商户');
    const frontages = db.currentContracts[merchantId];
    if (!frontages?.length) throw new ApiError(409, 'NO_CONTRACT', `${merchantId} 没有有效门面合同`);

    const order = {
      id: nextId(db, 'order', 'O'),
      shiftId: shift.id,
      merchantId,
      createdAt: at,
      promisedEndAt,
      status: 'open',
      closedAt: null,
      frontages: structuredClone(frontages),
    };
    db.orders.push(order);
    shift.orderIds.push(order.id);

    for (const f of frontages) {
      db.rights.push({
        id: nextId(db, 'right', 'R'),
        orderId: order.id,
        shiftId: shift.id,
        merchantId,
        curbId: f.curbId,
        fromM: f.fromM,
        toM: f.toM,
        label: f.label ?? '',
        dueAt: promisedEndAt,
        createdAt: at,
      });
    }
  }

  db.audit.push({ at, type: 'shift.created', shiftId: shift.id });
  return shift;
}

function tripAppliesToRight(trip, right) {
  if (trip.shiftId && trip.shiftId === right.shiftId) return true;
  return (trip.manifest ?? []).some((task) => task.rightId === right.id);
}

export function coveragePiecesForRight(db, right, asOf = null) {
  const out = [];
  for (const cov of db.coverages.filter((c) => c.curbId === right.curbId)) {
    if (Number.isInteger(asOf) && cov.receivedAt > asOf) continue;
    const trip = mustFind(db.trips, cov.tripId, '趟次');
    const manifestRight = (trip.manifest ?? []).some((t) => t.rightId === right.id);
    if (!tripAppliesToRight(trip, right) && !manifestRight) continue;
    const clipped = clipInterval(cov.fromM, cov.toM, right.fromM, right.toM);
    if (!clipped) continue;
    const report = db.reports.find((r) => r.id === cov.reportId);
    out.push({
      interval: clipped,
      coverageId: cov.id,
      reportId: cov.reportId,
      clientReportId: report?.clientReportId ?? null,
      tripId: cov.tripId,
      carId: cov.carId,
      sweptAt: cov.sweptAt,
      receivedAt: cov.receivedAt,
      timely: cov.sweptAt <= right.dueAt + EPS,
    });
  }
  return out;
}

export function assessRight(db, right, at = null) {
  const pieces = coveragePiecesForRight(db, right, at);
  const allCover = mergeIntervals(pieces.map((p) => p.interval));
  const timelyCover = mergeIntervals(pieces.filter((p) => p.timely).map((p) => p.interval));
  const target = [[right.fromM, right.toM]];
  const missing = subtractIntervals(target, allCover);
  const timelyMissing = subtractIntervals(target, timelyCover);
  return {
    rightId: right.id,
    curbId: right.curbId,
    fromM: right.fromM,
    toM: right.toM,
    label: right.label,
    pieces,
    coveredIntervals: allCover,
    timelyCoveredIntervals: timelyCover,
    missingIntervals: missing,
    timelyMissingIntervals: timelyMissing,
    physicallyComplete: missing.length === 0,
    timelyComplete: timelyMissing.length === 0,
    overdue: at === null ? null : at > right.dueAt && timelyMissing.length > 0,
    owed: at === null ? missing.length > 0 : at > right.dueAt && missing.length > 0,
  };
}

export function assessOrder(db, orderId, at = null) {
  const order = mustFind(db.orders, orderId, '结单');
  const rights = db.rights.filter((r) => r.orderId === orderId);
  const items = rights.map((r) => assessRight(db, r, at));
  const missing = items.flatMap((i) => i.missingIntervals.map((interval) => ({ rightId: i.rightId, curbId: i.curbId, interval })));
  const timelyMissing = items.flatMap((i) => i.timelyMissingIntervals.map((interval) => ({ rightId: i.rightId, curbId: i.curbId, interval })));
  const complaints = db.complaints.filter((c) => c.orderId === orderId);
  return {
    orderId,
    at,
    promisedEndAt: order.promisedEndAt,
    status: order.status,
    physicallyComplete: missing.length === 0,
    timelyComplete: timelyMissing.length === 0,
    overdue: at === null ? null : at > order.promisedEndAt && timelyMissing.length > 0,
    owed: at === null ? missing.length > 0 : at > order.promisedEndAt && missing.length > 0,
    missing,
    timelyMissing,
    items,
    complaintIds: complaints.map((c) => c.id),
    openComplaintIds: complaints.filter((c) => c.status !== 'resolved').map((c) => c.id),
  };
}

export function closeOrder(db, { orderId, at, allowLate = true, clerk = '服务台' }) {
  assertInt(at, '关单时间 at');
  const order = mustFind(db.orders, orderId, '结单');
  for (const c of db.complaints.filter((c) => c.orderId === orderId && c.status !== 'resolved')) {
    refreshComplaint(db, c.id, at);
  }
  const assessment = assessOrder(db, orderId, at);
  if (!assessment.physicallyComplete) {
    throw new ApiError(409, 'STILL_OWED', '仍有未被正确方向实扫覆盖的路缘，不能关单', assessment.missing);
  }
  if (assessment.openComplaintIds.length) {
    throw new ApiError(409, 'COMPLAINT_OPEN', '投诉尚未凭实扫办妥，不能给商户关单', assessment.openComplaintIds);
  }
  if (!assessment.timelyComplete && !allowLate) {
    throw new ApiError(409, 'LATE_SWEEP_ONLY', '只有超期实扫，不能按期限内履约关单');
  }
  order.status = assessment.timelyComplete ? 'closed_on_time' : 'closed_late';
  order.closedAt = at;
  order.closedBy = clerk;
  db.audit.push({ at, type: 'order.closed', orderId, status: order.status, clerk });
  return { order, assessment };
}

export function amendOrderToContract(db, { orderId, at, frontages, promisedEndAt }) {
  assertInt(at, '合同变更时间 at');
  const order = mustFind(db.orders, orderId, '结单');
  const version = setContract(db, { merchantId: order.merchantId, at, frontages });
  const dueAt = promisedEndAt ?? order.promisedEndAt;
  if (promisedEndAt !== undefined) assertInt(promisedEndAt, '新承诺结束时刻');

  const addedRights = [];
  for (const f of version.frontages) {
    const existing = db.rights
      .filter((r) => r.orderId === order.id && r.curbId === f.curbId)
      .map((r) => [r.fromM, r.toM]);
    for (const [fromM, toM] of subtractIntervals([[f.fromM, f.toM]], existing)) {
      const right = {
        id: nextId(db, 'right', 'R'),
        orderId: order.id,
        shiftId: order.shiftId,
        merchantId: order.merchantId,
        curbId: f.curbId,
        fromM,
        toM,
        label: f.label ?? '当班中新增门面',
        dueAt,
        createdAt: at,
        amended: true,
      };
      db.rights.push(right);
      addedRights.push(right);
    }
  }

  order.frontages = structuredClone(version.frontages);
  order.amendments = order.amendments ?? [];
  order.amendments.push({ at, contractVersionId: version.id, addedRightIds: addedRights.map((r) => r.id), dueAt });
  if (promisedEndAt !== undefined) order.promisedEndAt = promisedEndAt;
  db.audit.push({ at, type: 'order.amended', orderId: order.id, addedRightIds: addedRights.map((r) => r.id) });
  return { order, addedRights };
}

export function createTrip(db, payload) {
  const car = mustFind(db.cars, payload.carId, '车辆');
  assertInt(payload.startedAt ?? payload.at, '出车时间');
  const at = payload.startedAt ?? payload.at;
  if (car.activeTripId) {
    const active = db.trips.find((t) => t.id === car.activeTripId && !t.returnedAt);
    if (active) throw new ApiError(409, 'CAR_BUSY', `${car.name} 仍在执行 ${active.id}，不能再派`);
  }
  const routeLinkIds = payload.routeLinkIds ?? [];
  for (const id of routeLinkIds) mustFind(db.links, id, '路段');
  const mode = payload.mode ?? 'fixed_loop';
  const trip = {
    id: payload.id ?? nextId(db, 'trip', 'T'),
    kind: payload.kind ?? 'regular',
    mode,
    carId: car.id,
    shiftId: payload.shiftId ?? null,
    complaintIds: payload.complaintIds ?? [],
    startedAt: at,
    returnedAt: null,
    receivedAfterReturn: [],
    routeLinkIds,
    manifest: structuredClone(payload.manifest ?? []),
    frozen: mode === 'fixed_loop',
    currentNodeId: payload.startNodeId ?? null,
    lastReportedAt: null,
    status: 'active',
  };
  db.trips.push(trip);
  car.activeTripId = trip.id;
  db.audit.push({ at, type: 'trip.dispatched', tripId: trip.id, carId: car.id, mode: trip.mode });
  return trip;
}

export function returnTrip(db, { tripId, at }) {
  assertInt(at, '回场时间 at');
  const trip = mustFind(db.trips, tripId, '趟次');
  if (trip.returnedAt) return trip;
  if (at < trip.startedAt) throw new ApiError(400, 'BAD_RETURN_TIME', '回场时间早于出车时间');
  trip.returnedAt = at;
  trip.status = 'returned';
  const car = db.cars.find((c) => c.id === trip.carId);
  if (car?.activeTripId === trip.id) car.activeTripId = null;
  db.audit.push({ at, type: 'trip.returned', tripId });
  return trip;
}

export function addSweepReport(db, input) {
  const trip = mustFind(db.trips, input.tripId, '趟次');
  if (input.clientReportId) {
    const existing = db.reports.find((r) => r.clientReportId === input.clientReportId);
    if (existing) {
      return { report: existing, duplicate: true, coverage: db.coverages.find((c) => c.reportId === existing.id) };
    }
  }

  const link = getLink(db, input);
  const sweptAt = input.sweptAt ?? input.endedAt ?? input.at;
  assertInt(sweptAt, '实际清扫时间 sweptAt/at');
  if (input.startedAt !== undefined) assertInt(input.startedAt, '开刷时刻 startedAt');
  const receivedAt = input.receivedAt ?? sweptAt;
  assertInt(receivedAt, '服务台收到时间 receivedAt');
  if (trip.returnedAt !== null && sweptAt > trip.returnedAt) {
    throw new ApiError(400, 'SWEEP_AFTER_RETURN', '实扫位置时间晚于回场时间，不能归入本趟');
  }
  if (sweptAt < trip.startedAt) throw new ApiError(400, 'SWEEP_BEFORE_DEPARTURE', '实扫时间早于本趟出车时间');

  const interval = strokeToCurb(link, input.brushDownM, input.brushUpM);
  const report = {
    id: nextId(db, 'report', 'RPT'),
    clientReportId: input.clientReportId ?? null,
    tripId: trip.id,
    carId: trip.carId,
    linkId: link.id,
    fromNodeId: link.fromNodeId,
    toNodeId: link.toNodeId,
    travelDirection: `${link.fromNodeId}->${link.toNodeId}`,
    brushDownM: input.brushDownM,
    brushUpM: input.brushUpM,
    curbId: interval.curbId,
    curbFromM: interval.fromM,
    curbToM: interval.toM,
    startedAt: input.startedAt ?? sweptAt,
    sweptAt,
    receivedAt,
    receivedAfterReturn: trip.returnedAt !== null && receivedAt > trip.returnedAt,
  };
  db.reports.push(report);

  const coverage = {
    id: nextId(db, 'coverage', 'COV'),
    reportId: report.id,
    tripId: trip.id,
    carId: trip.carId,
    curbId: interval.curbId,
    fromM: interval.fromM,
    toM: interval.toM,
    sweptAt,
    receivedAt,
  };
  db.coverages.push(coverage);
  if (trip.mode === 'on_demand') {
    trip.currentNodeId = link.toNodeId;
    trip.lastReportedAt = receivedAt;
  }

  const affected = db.complaints.filter((c) => c.claims.some((cl) => cl.curbId === interval.curbId));
  for (const c of affected) refreshComplaint(db, c.id, receivedAt);

  db.audit.push({
    at: receivedAt,
    type: 'report.received',
    reportId: report.id,
    tripId: trip.id,
    lateAfterReturn: report.receivedAfterReturn,
  });
  return { report, coverage, duplicate: false, affectedComplaintIds: affected.map((c) => c.id) };
}

export function createComplaint(db, payload) {
  const order = mustFind(db.orders, payload.orderId, '结单');
  const at = payload.at ?? order.promisedEndAt + 1;
  assertInt(at, '投诉时间 at');

  let claimsInput = payload.claims;
  if (!claimsInput?.length) {
    const assessment = assessOrder(db, order.id, at);
    claimsInput = assessment.missing.map((m) => ({ rightId: m.rightId, fromM: m.interval[0], toM: m.interval[1] }));
  }

  const claims = [];
  for (const input of claimsInput) {
    const right = mustFind(db.rights, input.rightId, '客户权利');
    if (right.orderId !== order.id) throw new ApiError(400, 'RIGHT_ORDER_MISMATCH', '权利不属于该单');
    const fromM = Number(input.fromM);
    const toM = Number(input.toM);
    if (fromM < right.fromM - EPS || toM > right.toM + EPS || toM - fromM <= EPS) {
      throw new ApiError(400, 'CLAIM_OUTSIDE_CONTRACT', '投诉区间超出当班合同门面');
    }
    claims.push({
      id: nextId(db, 'claim', 'CL'),
      rightId: right.id,
      curbId: right.curbId,
      fromM,
      toM,
      reason: input.reason ?? payload.reason ?? '欠扫',
    });
  }
  if (!claims.length) throw new ApiError(409, 'NOTHING_OWED', '没有可立案的欠扫米段');

  const complaint = {
    id: payload.id ?? nextId(db, 'complaint', 'C'),
    complaintNo: payload.complaintNo ?? payload.id ?? nextId(db, 'complaintNo', 'TS'),
    orderId: order.id,
    shiftId: order.shiftId,
    merchantId: order.merchantId,
    createdAt: at,
    promisedEndAt: order.promisedEndAt,
    claims,
    status: 'open',
    resolvedAt: null,
    resolveReportIds: [],
    events: [{ at, type: 'created', claimIds: claims.map((c) => c.id) }],
  };
  db.complaints.push(complaint);
  db.audit.push({ at, type: 'complaint.created', complaintId: complaint.id, orderId: order.id });
  return complaint;
}

function claimFills(db, complaint, claim, asOf = null) {
  const right = mustFind(db.rights, claim.rightId, '客户权利');
  const fills = [];
  for (const cov of db.coverages.filter((c) => c.curbId === claim.curbId)) {
    if (Number.isInteger(asOf) && cov.receivedAt > asOf) continue;
    const trip = mustFind(db.trips, cov.tripId, '趟次');
    const applies =
      (trip.shiftId && trip.shiftId === complaint.shiftId) ||
      (trip.manifest ?? []).some((t) => t.claimId === claim.id || t.rightId === right.id);
    if (!applies) continue;
    const clipped = clipInterval(cov.fromM, cov.toM, claim.fromM, claim.toM);
    if (!clipped) continue;
    fills.push({
      interval: clipped,
      coverageId: cov.id,
      reportId: cov.reportId,
      tripId: cov.tripId,
      carId: cov.carId,
      sweptAt: cov.sweptAt,
      receivedAt: cov.receivedAt,
      timely: cov.sweptAt <= complaint.promisedEndAt + EPS,
    });
  }
  return fills.sort((a, b) => a.sweptAt - b.sweptAt || a.reportId.localeCompare(b.reportId));
}

export function complaintView(db, complaintOrId, asOf = null) {
  const complaint = typeof complaintOrId === 'string'
    ? (db.complaints.find((c) => c.id === complaintOrId || c.complaintNo === complaintOrId)
      ?? mustFind(db.complaints, complaintOrId, '投诉'))
    : complaintOrId;
  const merchant = db.merchants.find((m) => m.id === complaint.merchantId) ?? null;
  const claims = complaint.claims.map((claim) => {
    const fills = claimFills(db, complaint, claim, asOf);
    const covered = mergeIntervals(fills.map((f) => f.interval));
    const missing = subtractIntervals([[claim.fromM, claim.toM]], covered);
    const cells = meterCells(claim.fromM, claim.toM).map((cell) => {
      const filledBy = fills.filter((f) => clipInterval(f.interval[0], f.interval[1], cell.fromM, cell.toM));
      return { ...cell, status: filledBy.length ? 'filled' : 'missing', filledBy };
    });
    return {
      ...claim,
      coveredIntervals: covered,
      missingIntervals: missing,
      complete: missing.length === 0,
      fills,
      meters: cells,
    };
  });
  const complete = claims.every((c) => c.complete);
  return {
    id: complaint.id,
    complaintNo: complaint.complaintNo,
    orderId: complaint.orderId,
    shiftId: complaint.shiftId,
    merchant,
    createdAt: complaint.createdAt,
    promisedEndAt: complaint.promisedEndAt,
    storedStatus: complaint.status,
    computedStatus: complete ? 'resolved' : 'open',
    resolvedAt: complaint.resolvedAt,
    resolveReportIds: complaint.resolveReportIds,
    claims,
    events: complaint.events,
  };
}

export function refreshComplaint(db, complaintId, at = null) {
  const complaint = mustFind(db.complaints, complaintId, '投诉');
  const view = complaintView(db, complaint, at);
  if (view.computedStatus === 'resolved' && complaint.status !== 'resolved') {
    complaint.status = 'resolved';
    complaint.resolvedAt = at;
    complaint.resolveReportIds = [...new Set(view.claims.flatMap((c) => c.fills.map((f) => f.reportId)))];
    complaint.events.push({ at, type: 'resolved', reportIds: complaint.resolveReportIds });
    db.audit.push({ at, type: 'complaint.resolved', complaintId });
  }
  return complaintView(db, complaint);
}

export function completeComplaint(db, { complaintId, at }) {
  const view = refreshComplaint(db, complaintId, at);
  if (view.computedStatus !== 'resolved') {
    throw new ApiError(409, 'EVIDENCE_INCOMPLETE', '不能凭人工标记办妥：仍有米段缺少正确方向实扫', view.claims.flatMap((c) => c.missingIntervals));
  }
  return view;
}

export function evaluateMakeup(db, { complaintIds = [], at }) {
  const complaints = complaintIds.map((id) => mustFind(db.complaints, id, '投诉'));
  const outstanding = [];
  for (const c of complaints) {
    const view = complaintView(db, c);
    for (const claim of view.claims) {
      for (const interval of claim.missingIntervals) {
        outstanding.push({ complaintId: c.id, claimId: claim.id, rightId: claim.rightId, curbId: claim.curbId, interval });
      }
    }
  }
  const availableCars = db.cars.filter((car) => {
    if (!car.activeTripId) return true;
    const active = db.trips.find((t) => t.id === car.activeTripId && !t.returnedAt);
    return !active;
  });
  return {
    canAddToActiveFixedLoop: false,
    reason: '固定整圈路线离场后已冻结路缘与先后；新增欠段只能另派新趟或另一台车',
    outstanding,
    availableCarIds: availableCars.map((c) => c.id),
    canUseAnotherCar: availableCars.length > 0,
    at,
  };
}

function localIntervalForLink(link, fromM, toM) {
  // Convert original curb-reference interval to local directed-link positions.
  const a = Math.max(fromM, link.sourceFromM);
  const b = Math.min(toM, link.sourceToM);
  if (b <= a + EPS) return null;
  if (link.direction === 'ref') return { downM: a - link.sourceFromM, upM: b - link.sourceFromM };
  return { downM: link.sourceToM - b, upM: link.sourceToM - a };
}

export function dispatchMakeup(db, payload) {
  assertInt(payload.at, '派单时间 at');
  const evaluation = evaluateMakeup(db, payload);
  if (!evaluation.outstanding.length) throw new ApiError(409, 'NO_OUTSTANDING_METERS', '所选投诉已由实扫补齐，无需再派补扫');

  let carId = payload.carId;
  if (!carId) {
    carId = evaluation.availableCarIds[0];
    if (!carId) throw new ApiError(409, 'NO_AVAILABLE_CAR', '没有已回场/未出车的车辆；可等原趟回场后另派新趟');
  } else {
    mustFind(db.cars, carId, '车辆');
    const car = db.cars.find((c) => c.id === carId);
    const active = db.trips.find((t) => t.id === car.activeTripId && !t.returnedAt);
    if (active) {
      const other = db.cars.find((c) => !db.trips.some((t) => t.id === c.activeTripId && !t.returnedAt));
      throw new ApiError(409, 'FIXED_LOOP_FROZEN', '不能把新欠段塞进已离场的固定整圈路线', {
        activeTripId: active.id,
        alternativeCarId: other?.id ?? null,
      });
    }
  }

  const manifest = [];
  for (const item of evaluation.outstanding) {
    const links = db.links.filter((l) => l.rightCurbId === item.curbId && !l.superseded);
    let placed = false;
    for (const link of links) {
      const local = localIntervalForLink(link, item.interval[0], item.interval[1]);
      if (!local) continue;
      manifest.push({
        complaintId: item.complaintId,
        claimId: item.claimId,
        rightId: item.rightId,
        curbId: item.curbId,
        linkId: link.id,
        fromM: item.interval[0],
        toM: item.interval[1],
        brushDownM: local.downM,
        brushUpM: local.upM,
      });
      placed = true;
    }
    if (!placed) throw new ApiError(409, 'NO_DIRECTIONAL_LINK', `找不到可正确右扫 ${item.curbId} ${item.interval} 的方向路段`);
  }

  const routeLinkIds = payload.routeLinkIds?.length
    ? payload.routeLinkIds
    : [...new Set(manifest.map((m) => m.linkId))];

  const trip = createTrip(db, {
    id: payload.id,
    carId,
    at: payload.at,
    kind: 'makeup',
    mode: 'fixed_loop',
    complaintIds: payload.complaintIds,
    routeLinkIds,
    manifest,
  });
  return { trip, evaluation };
}

export function resegment(db, { segmentId, cuts = [], at = 0 }) {
  const segment = mustFind(db.segments, segmentId, '原路段');
  if (segment.resegmentedAt !== undefined) throw new ApiError(409, 'ALREADY_RESEGMENTED', '本演示路段已重新划段');
  assertInt(at, '划段时间 at');
  const points = [...new Set(cuts.map(Number))].sort((a, b) => a - b);
  if (points.some((p) => !(p > 0 && p < segment.lengthM))) throw new ApiError(400, 'BAD_CUT', '切点必须在原路段内部');
  const bounds = [0, ...points, segment.lengthM];
  const oldLinks = db.links.filter((l) => l.segmentId === segmentId);
  const refOld = oldLinks.find((l) => l.direction === 'ref');
  const revOld = oldLinks.find((l) => l.direction === 'reverse');
  const newLinks = [];
  const virtualNodes = [];

  for (let i = 1; i < bounds.length - 1; i += 1) {
    const id = `${segmentId}_P${i}`;
    const node = { id, name: `${segment.name}${bounds[i]}米分界`, x: null, y: null, uTurnAllowed: false, virtual: true };
    db.nodes.push(node);
    virtualNodes.push(node);
  }

  for (let i = 0; i < bounds.length - 1; i += 1) {
    const a = bounds[i];
    const b = bounds[i + 1];
    const fromNodeId = i === 0 ? segment.nodeAId : virtualNodes[i - 1].id;
    const toNodeId = i === bounds.length - 2 ? segment.nodeBId : virtualNodes[i].id;
    const refId = `${refOld.id}_S${i + 1}`;
    const revId = `${revOld.id}_S${i + 1}`;
    const ref = {
      ...structuredClone(refOld),
      id: refId,
      fromNodeId,
      toNodeId,
      sourceFromM: a,
      sourceToM: b,
      lengthM: b - a,
      reverseLinkId: revId,
      superseded: false,
    };
    const rev = {
      ...structuredClone(revOld),
      id: revId,
      fromNodeId: toNodeId,
      toNodeId: fromNodeId,
      sourceFromM: a,
      sourceToM: b,
      lengthM: b - a,
      reverseLinkId: refId,
      superseded: false,
    };
    db.links.push(ref, rev);
    newLinks.push(ref, rev);
  }

  for (const link of oldLinks) {
    link.superseded = true;
    link.supersededAt = at;
  }
  segment.resegmentedAt = at;
  segment.cuts = points;
  db.audit.push({ at, type: 'segment.resegmented', segmentId, newLinkIds: newLinks.map((l) => l.id) });
  return newLinks;
}
