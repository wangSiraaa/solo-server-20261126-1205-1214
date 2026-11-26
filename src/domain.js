import { createHash, randomUUID } from 'node:crypto';
import { HttpError, badRequest, conflict, notFound } from './errors.js';
import {
  intersectUnions,
  intervalSeconds,
  isBackgroundEligible,
  mapLocalInterval,
  mergeIntervals,
  parseBackgroundPolicy,
  subtractHoles,
  sumSeconds,
  toInterval,
} from './time.js';

export const MAX_STATIONS_PER_ACTIVITY = 4;

const REASON_LABELS = {
  CLOCK_NOT_CALIBRATED_WITH_TWO_SYNCS: '时钟缺少至少两次对时校准',
  INSUFFICIENT_COMMON_SECONDS: '多站共同可用秒数不足',
  BACKGROUND_GAP: '部分共同秒缺少落在规定前后距离内的背景',
  BACKGROUND_INSUFFICIENT_FOR_REQUIREMENT: '有背景支撑的共同秒数不足',
  MISSING_ACTUAL_FRAMES: '有站点缺少实到目标帧',
  MISSING_ELIGIBLE_BACKGROUND: '有站点缺少合规背景',
};

function intervalFromInput(body, fieldNames = []) {
  if (!body || typeof body !== 'object') return undefined;
  for (const name of fieldNames) {
    if (body[name] !== undefined && body[name] !== null) {
      return toInterval(body[name]);
    }
  }
  if (body.interval !== undefined) return toInterval(body.interval);
  if (body.intervalStart !== undefined && body.intervalEnd !== undefined) {
    return toInterval(body.intervalStart, body.intervalEnd);
  }
  if (body.start !== undefined && body.end !== undefined) {
    return toInterval(body.start, body.end);
  }
  if (body.startSecond !== undefined && body.endSecond !== undefined) {
    return toInterval(body.startSecond, body.endSecond);
  }
  return undefined;
}

function intOrUndefined(value, field) {
  if (value === undefined || value === null || value === '') return undefined;
  const n = Number(value);
  if (!Number.isInteger(n)) throw badRequest('INTEGER_REQUIRED', `${field} 必须是整数，收到 ${value}`);
  return n;
}

function numOrZero(value, field) {
  if (value === undefined || value === null) return 0;
  const n = Number(value);
  if (!Number.isFinite(n)) throw badRequest('NUMBER_REQUIRED', `${field} 必须是数字，收到 ${value}`);
  return n;
}

function stableStringify(value) {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}:${stableStringify(value[k])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

export class Observatory {
  constructor({ seed = false } = {}) {
    this.reset({ seed });
  }

  reset({ seed = false } = {}) {
    this.logicalClock = 0;
    this.events = [];
    this.stations = new Map();
    this.frames = new Map();
    this.commissions = new Map();
    this.windows = new Map();
    this.dispatches = new Map();
    this.arrivals = new Map();
    this.reports = new Map();
    this.caseDecisions = new Map();
    this.incidents = new Map();
    this.idempotencyKeys = new Map();
    this.counter = 0;
    if (seed) this.seedDemo();
    return this.publicState();
  }

  nextId(prefix) {
    this.counter += 1;
    return `${prefix}-${this.counter.toString(36).toUpperCase()}-${randomUUID().slice(0, 8)}`;
  }

  // Lamport clock. A remote logical clock is accepted on every inbound command.
  tick(remoteClock, type, actor, payload = {}) {
    const remote = Number(remoteClock);
    if (Number.isFinite(remote)) this.logicalClock = Math.max(this.logicalClock, Math.floor(remote));
    this.logicalClock += 1;
    const event = {
      seq: this.events.length + 1,
      logicalClock: this.logicalClock,
      wallAt: new Date().toISOString(),
      type,
      actor: actor || null,
      payload,
    };
    this.events.push(event);
    return event;
  }

  publicState() {
    return {
      logicalClock: this.logicalClock,
      stationCount: this.stations.size,
      commissionCount: this.commissions.size,
      reportCount: this.reports.size,
      eventCount: this.events.length,
    };
  }

  // ---------- Stations and clocks ----------

  registerStation(body = {}, remoteClock) {
    const id = body.id || body.stationId || this.nextId('ST');
    if (this.stations.has(id)) throw conflict('STATION_EXISTS', `站点 ${id} 已存在`);

    const offsetExplicit = body.agreedOffset !== undefined || body.offset !== undefined;
    const offset = intOrUndefined(body.agreedOffset ?? body.offset ?? 0, 'agreedOffset') ?? 0;
    const station = {
      id,
      name: body.name || id,
      status: body.status || 'online',
      clock: {
        formula: 'commonSecond = localSecond + agreedOffset',
        agreedOffset: offset,
        syncs: [],
        corrections: [],
        calibrated: false,
      },
      createdAtLogical: this.logicalClock + 1,
    };

    const syncs = Array.isArray(body.syncs) ? body.syncs : body.clock?.syncs;
    if (Array.isArray(syncs) && syncs.length > 0) {
      for (const sync of syncs) this.addSyncToStation(station, sync, false);
    } else if (body.conventionAnchors !== false) {
      // The integer convention itself supplies two audit anchors for the
      // simulated clock. Explicit POST /sync records replace/augment this.
      this.addSyncToStation(station, { localSecond: 0, commonSecond: offset, kind: 'convention-anchor' }, false);
      this.addSyncToStation(station, { localSecond: 10, commonSecond: 10 + offset, kind: 'convention-anchor' }, false);
    }
    if (!offsetExplicit && Array.isArray(syncs) && syncs.length >= 2) {
      const impliedOffsets = station.clock.syncs.map((sync) => sync.impliedOffset);
      if (impliedOffsets.every((value) => Number.isInteger(value) && value === impliedOffsets[0])) {
        station.clock.agreedOffset = impliedOffsets[0];
      }
    }
    station.clock.calibrated = station.clock.syncs.length >= 2;

    this.stations.set(id, station);
    this.tick(remoteClock, 'station.registered', id, { id, name: station.name, offset });
    return this.getStation(id);
  }

  ensureStation(stationId, body = {}, remoteClock) {
    if (!stationId) throw badRequest('STATION_REQUIRED', '必须提供 stationId');
    if (!this.stations.has(stationId)) {
      this.registerStation({ id: stationId, ...body }, remoteClock);
    }
    return this.stations.get(stationId);
  }

  addSyncToStation(station, body = {}, recordEvent = true, remoteClock) {
    const localSecond = Number(body.localSecond ?? body.local ?? body.localTime);
    const commonSecond = Number(body.commonSecond ?? body.common ?? body.commonTime);
    if (!Number.isFinite(localSecond) || !Number.isFinite(commonSecond)) {
      throw badRequest('INVALID_SYNC', '对时记录需要 localSecond 和 commonSecond');
    }
    const sync = {
      id: body.id || this.nextId('SYNC'),
      localSecond,
      commonSecond,
      impliedOffset: commonSecond - localSecond,
      kind: body.kind || 'field-sync',
      logicalClock: this.logicalClock + (recordEvent ? 1 : 0),
      at: new Date().toISOString(),
    };
    station.clock.syncs.push(sync);
    station.clock.calibrated = station.clock.syncs.length >= 2;
    if (recordEvent) this.tick(remoteClock, 'clock.synced', station.id, { sync });
    return sync;
  }

  addSync(stationId, body, remoteClock) {
    const station = this.ensureStation(stationId, {}, remoteClock);
    const sync = this.addSyncToStation(station, body, false);

    let correction = null;
    if (body.agreedOffset !== undefined || body.offset !== undefined) {
      const toOffset = intOrUndefined(body.agreedOffset ?? body.offset, 'agreedOffset');
      correction = {
        fromOffset: station.clock.agreedOffset,
        toOffset,
        reason: body.reason || 'sync-supplied integer correction',
        logicalClock: this.logicalClock + 1,
      };
      station.clock.corrections.push(correction);
      station.clock.agreedOffset = toOffset;
    } else if (body.adoptImpliedOffset === true || body.adopt === true) {
      if (!Number.isInteger(sync.impliedOffset)) {
        throw badRequest('NON_INTEGER_OFFSET', '约定要求整数偏差；该对时推算出的偏差不是整数');
      }
      correction = {
        fromOffset: station.clock.agreedOffset,
        toOffset: sync.impliedOffset,
        reason: 'adopt implied integer offset from corrected sync',
        logicalClock: this.logicalClock + 1,
      };
      station.clock.corrections.push(correction);
      station.clock.agreedOffset = sync.impliedOffset;
    }

    this.tick(remoteClock, 'clock.synced', stationId, { sync, correction });
    return { station: this.getStation(stationId), sync, correction, logicalClock: this.logicalClock };
  }

  updateStation(stationId, body = {}, remoteClock) {
    const station = this.stations.get(stationId);
    if (!station) throw notFound('STATION_NOT_FOUND', `找不到站点 ${stationId}`);
    if (body.name !== undefined) station.name = body.name;
    if (body.status !== undefined) station.status = body.status;
    if (body.agreedOffset !== undefined || body.offset !== undefined || Array.isArray(body.syncs)) {
      return this.setClock(stationId, body, remoteClock);
    }
    this.tick(remoteClock, 'station.updated', stationId, { name: station.name, status: station.status });
    return this.getStation(stationId);
  }

  setClock(stationId, body = {}, remoteClock) {
    const station = this.ensureStation(stationId, {}, remoteClock);
    const oldOffset = station.clock.agreedOffset;

    if (Array.isArray(body.syncs)) {
      station.clock.syncs = [];
      for (const sync of body.syncs) this.addSyncToStation(station, sync, false);
    }
    if (body.agreedOffset !== undefined || body.offset !== undefined) {
      const offset = intOrUndefined(body.agreedOffset ?? body.offset, 'agreedOffset');
      station.clock.agreedOffset = offset;
    }
    station.clock.calibrated = station.clock.syncs.length >= 2;
    station.clock.corrections.push({
      fromOffset: oldOffset,
      toOffset: station.clock.agreedOffset,
      reason: body.reason || 'manual-clock-correction',
      logicalClock: this.logicalClock + 1,
      at: new Date().toISOString(),
    });
    this.tick(remoteClock, 'clock.corrected', stationId, {
      fromOffset: oldOffset,
      toOffset: station.clock.agreedOffset,
    });
    return this.getStation(stationId);
  }

  setStationStatus(stationId, status, body = {}, remoteClock) {
    const station = this.ensureStation(stationId, {}, remoteClock);
    station.status = status;
    if (status === 'offline' || status === 'lost') {
      const incident = {
        id: this.nextId('INC'),
        stationId,
        type: 'contact-lost',
        reason: body.reason || 'station did not check in',
        sinceLogicalClock: this.logicalClock + 1,
        activeDispatchIds: [],
        sealed: false,
      };
      for (const d of this.dispatches.values()) {
        if (d.stationIds.includes(stationId) && ['held', 'active', 'at_risk'].includes(d.status)) {
          d.status = 'at_risk';
          d.riskReason = 'STATION_CONTACT_LOST';
          incident.activeDispatchIds.push(d.id);
        }
      }
      this.incidents.set(incident.id, incident);
      this.tick(remoteClock, 'station.lost', stationId, { incident });
      return { station: this.getStation(stationId), incident };
    }
    this.tick(remoteClock, 'station.online', stationId, { reason: body.reason || 'heartbeat' });
    return { station: this.getStation(stationId), incident: null };
  }

  getStation(id) {
    const s = this.stations.get(id);
    if (!s) throw notFound('STATION_NOT_FOUND', `找不到站点 ${id}`);
    return JSON.parse(JSON.stringify(s));
  }

  listStations() {
    return [...this.stations.values()].map((s) => this.getStation(s.id));
  }

  // ---------- Actual frame / arrival records ----------

  submitFrame(stationId, body = {}, remoteClock, idempotencyKey) {
    const station = this.ensureStation(stationId, body.station || {}, remoteClock);
    if (idempotencyKey && this.idempotencyKeys.has(idempotencyKey)) {
      const prior = this.idempotencyKeys.get(idempotencyKey);
      this.tick(remoteClock, 'frame.idempotent_retry', stationId, { idempotencyKey, frameId: prior.id });
      return { ...prior, duplicate: true, idempotentReplay: true, logicalClock: this.logicalClock };
    }

    const localInterval = intervalFromInput(body, ['localInterval', 'nominalInterval', 'shootingInterval']);
    if (!localInterval) throw badRequest('INTERVAL_REQUIRED', '交帧需要本地拍摄区间，例如 localInterval:[4,8]');

    let droppedIntervals = [];
    const droppedInput = body.droppedIntervals ?? body.droppedFrames ?? body.holes ?? [];
    if (!Array.isArray(droppedInput)) throw badRequest('INVALID_DROPPED', 'droppedIntervals 必须是区间数组');
    droppedIntervals = droppedInput.map((x) => toInterval(x));

    const mode = body.mode || body.role || body.frameType || 'target';
    if (!['target', 'background'].includes(mode)) {
      throw badRequest('INVALID_MODE', 'mode 只能是 target 或 background');
    }
    const quality = body.quality || 'qualified';
    const frameId = body.id || body.frameId || this.nextId('FRAME');

    if (this.frames.has(frameId)) {
      const existing = this.frames.get(frameId);
      if (existing.stationId !== stationId) {
        throw conflict('FRAME_ID_CONFLICT', `帧号 ${frameId} 已被其他站点使用`);
      }
      existing.deliveries += 1;
      existing.lastLogicalClock = this.logicalClock + 1;
      this.tick(remoteClock, 'frame.duplicate_delivery', stationId, {
        frameId,
        deliveries: existing.deliveries,
      });
      const receipt = this.frameReceipt(existing, true);
      if (idempotencyKey) this.idempotencyKeys.set(idempotencyKey, receipt);
      return receipt;
    }

    if (body.commissionId && !this.commissions.has(body.commissionId)) {
      throw notFound('COMMISSION_NOT_FOUND', `交帧关联的委托 ${body.commissionId} 不存在`);
    }

    const frame = {
      id: frameId,
      stationId,
      commissionId: body.commissionId || null,
      mode,
      quality,
      localInterval,
      droppedIntervals,
      note: body.note || '',
      deliveries: 1,
      firstLogicalClock: this.logicalClock + 1,
      lastLogicalClock: this.logicalClock + 1,
      createdAt: new Date().toISOString(),
    };
    this.frames.set(frameId, frame);

    if (body.commissionId) {
      this.recordArrival(
        {
          commissionId: body.commissionId,
          stationId,
          source: 'frame-received',
          frameId,
          atCommonSecond: body.arrivalAtCommonSecond,
        },
        remoteClock,
      );
    }

    this.tick(remoteClock, 'frame.received', stationId, {
      frameId,
      mode,
      localInterval,
      droppedCount: droppedIntervals.length,
    });
    const receipt = this.frameReceipt(frame, false);
    if (idempotencyKey) this.idempotencyKeys.set(idempotencyKey, receipt);
    return receipt;
  }

  frameReceipt(frame, duplicate) {
    const station = this.stations.get(frame.stationId);
    const mappedNominal = mapLocalInterval(frame.localInterval, station.clock.agreedOffset);
    const mappedDropped = frame.droppedIntervals.map((iv) => mapLocalInterval(iv, station.clock.agreedOffset));
    const usableIntervals = frame.quality === 'qualified'
      ? subtractHoles(mappedNominal, mappedDropped)
      : [];
    return {
      id: frame.id,
      frameId: frame.id,
      stationId: frame.stationId,
      commissionId: frame.commissionId,
      mode: frame.mode,
      quality: frame.quality,
      duplicate: Boolean(duplicate),
      deliveries: frame.deliveries,
      localInterval: frame.localInterval,
      droppedIntervals: frame.droppedIntervals,
      commonInterval: mappedNominal,
      mappedDroppedIntervals: mappedDropped,
      usableIntervals,
      usableSeconds: sumSeconds(usableIntervals),
      firstLogicalClock: frame.firstLogicalClock,
      logicalClock: this.logicalClock,
    };
  }

  listFrames(query = {}) {
    let items = [...this.frames.values()];
    if (query.stationId) items = items.filter((f) => f.stationId === query.stationId);
    if (query.commissionId) {
      items = items.filter((f) => !f.commissionId || f.commissionId === query.commissionId);
    }
    if (query.mode) items = items.filter((f) => f.mode === query.mode);
    return items.map((f) => this.frameReceipt(f, false));
  }

  recordArrival(body = {}, remoteClock) {
    const commissionId = body.commissionId;
    const stationId = body.stationId;
    if (!commissionId || !this.commissions.has(commissionId)) {
      throw notFound('COMMISSION_NOT_FOUND', `实到记录需要存在的 commissionId，收到 ${commissionId}`);
    }
    const station = this.ensureStation(stationId, {}, remoteClock);
    const id = body.id || this.nextId('ARR');
    const arrival = {
      id,
      commissionId,
      stationId,
      source: body.source || 'check-in',
      frameId: body.frameId || null,
      atCommonSecond: body.atCommonSecond ?? body.atSecond ?? null,
      logicalClock: this.logicalClock + 1,
      wallAt: new Date().toISOString(),
    };
    this.arrivals.set(id, arrival);
    if (station.status === 'offline' || station.status === 'lost') station.status = 'online';
    this.tick(remoteClock, 'arrival.recorded', stationId, { commissionId, arrivalId: id });
    return { ...arrival, logicalClock: this.logicalClock };
  }

  listArrivals(commissionId) {
    return [...this.arrivals.values()].filter((a) => !commissionId || a.commissionId === commissionId);
  }

  listIncidents(query = {}) {
    return [...this.incidents.values()]
      .filter((incident) => !query.stationId || incident.stationId === query.stationId)
      .map((incident) => JSON.parse(JSON.stringify(incident)));
  }

  // ---------- Commissions ----------

  createCommission(body = {}, remoteClock) {
    const id = body.id || body.commissionId || this.nextId('C');
    if (this.commissions.has(id)) throw conflict('COMMISSION_EXISTS', `委托号 ${id} 已存在`);

    const phenomenonInterval = intervalFromInput(body, [
      'phenomenonInterval',
      'eventInterval',
      'targetInterval',
      'celestialInterval',
      'windowInterval',
    ]);
    if (!phenomenonInterval) {
      throw badRequest('INTERVAL_REQUIRED', '委托需要天象区间 phenomenonInterval:[start,end)');
    }

    let stationIds = body.stationIds || body.stations || body.combination || body.stationCombination;
    if (typeof stationIds === 'string') stationIds = stationIds.split(',').map((s) => s.trim());
    if (!Array.isArray(stationIds)) throw badRequest('STATIONS_REQUIRED', '委托需要 stationIds 站点组合');
    stationIds = [...new Set(stationIds.map(String))];
    if (stationIds.length < 2) throw badRequest('NEED_TWO_STATIONS', '联测委托至少需要两站');
    if (stationIds.length > MAX_STATIONS_PER_ACTIVITY) {
      throw badRequest('TOO_MANY_STATIONS', `一个活动最多 ${MAX_STATIONS_PER_ACTIVITY} 站`);
    }
    for (const sid of stationIds) this.ensureStation(sid, {}, remoteClock);

    const requiredCommonSeconds = intOrUndefined(
      body.requiredCommonSeconds ?? body.requiredSeconds ?? body.minCommonSeconds ?? body.needSeconds ?? 3,
      'requiredCommonSeconds',
    );
    if (requiredCommonSeconds <= 0) throw badRequest('INVALID_REQUIREMENT', '共同秒数必须为正整数');

    const commission = {
      id,
      commissionNumber: body.commissionNumber || id,
      title: body.title || id,
      phenomenonInterval,
      requiredCommonSeconds,
      stationIds,
      backgroundPolicy: parseBackgroundPolicy(body.backgroundPolicy ?? body.background ?? {}),
      requireClockCalibration: body.requireClockCalibration !== false,
      adhoc: Boolean(body.adhoc || body.temporary),
      status: 'open',
      requirementsVersion: 1,
      amendments: [],
      originalReportId: null,
      latestReportId: null,
      createdAtLogical: this.logicalClock + 1,
      createdAt: new Date().toISOString(),
    };
    this.commissions.set(id, commission);
    this.tick(remoteClock, 'commission.created', null, {
      id,
      stationIds,
      phenomenonInterval,
      requiredCommonSeconds,
    });
    return this.getCommission(id);
  }

  getCommission(id) {
    const c = this.commissions.get(id);
    if (!c) throw notFound('COMMISSION_NOT_FOUND', `找不到委托 ${id}`);
    return JSON.parse(JSON.stringify(c));
  }

  listCommissions() {
    return [...this.commissions.values()].map((c) => this.getCommission(c.id));
  }

  updateCommission(id, body = {}, remoteClock) {
    const c = this.commissions.get(id);
    if (!c) throw notFound('COMMISSION_NOT_FOUND', `找不到委托 ${id}`);

    const changes = {};
    if (body.requiredCommonSeconds !== undefined || body.requiredSeconds !== undefined) {
      changes.requiredCommonSeconds = intOrUndefined(
        body.requiredCommonSeconds ?? body.requiredSeconds,
        'requiredCommonSeconds',
      );
    }
    const interval = intervalFromInput(body, ['phenomenonInterval', 'eventInterval']);
    if (interval) changes.phenomenonInterval = interval;
    if (body.backgroundPolicy !== undefined || body.background !== undefined) {
      changes.backgroundPolicy = parseBackgroundPolicy(body.backgroundPolicy ?? body.background);
    }
    if (body.title !== undefined) changes.title = body.title;

    const wasSealed = Boolean(c.latestReportId);
    if (wasSealed && Object.keys(changes).length > 0) {
      c.amendments.push({
        version: c.requirementsVersion,
        changedAtLogical: this.logicalClock + 1,
        previous: {
          requiredCommonSeconds: c.requiredCommonSeconds,
          phenomenonInterval: c.phenomenonInterval,
          backgroundPolicy: c.backgroundPolicy,
        },
        changes,
        note: '原封交报告保留；新条件必须重新计算，不能沿用旧结论',
      });
      c.requirementsVersion += 1;
      c.status = 'amended_open';
    }
    Object.assign(c, changes);
    this.tick(remoteClock, 'commission.updated', null, { id, changes, wasSealed });
    return this.getCommission(id);
  }

  // ---------- Evidence evaluation ----------

  stationEvidence(commission, stationId) {
    const station = this.stations.get(stationId);
    if (!station) throw notFound('STATION_NOT_FOUND', `找不到站点 ${stationId}`);

    const targetFrames = [...this.frames.values()].filter((f) => {
      if (f.stationId !== stationId || f.mode !== 'target' || f.quality !== 'qualified') return false;
      if (f.commissionId && f.commissionId !== commission.id) return false;
      return true;
    });
    const backgroundFrames = [...this.frames.values()].filter((f) => {
      if (f.stationId !== stationId || f.mode !== 'background' || f.quality !== 'qualified') return false;
      if (f.commissionId && f.commissionId !== commission.id) return false;
      return true;
    });

    const targetPieces = [];
    const backgroundCheckPieces = [];
    const targetFrameEvidence = [];
    for (const frame of targetFrames) {
      const nominal = mapLocalInterval(frame.localInterval, station.clock.agreedOffset);
      const holes = frame.droppedIntervals.map((iv) => mapLocalInterval(iv, station.clock.agreedOffset));
      const usable = subtractHoles(nominal, holes)
        .map((iv) => ({ ...iv, frameId: frame.id }));
      const commissionOverlapIntervals = intersectUnions(
        [commission.phenomenonInterval],
        usable,
      ).map((iv) => ({ ...iv, frameId: frame.id }));

      // Ignore target frames that do not cover any part of this phenomenon.
      if (commissionOverlapIntervals.length === 0) continue;

      targetPieces.push(...commissionOverlapIntervals);
      backgroundCheckPieces.push(
        ...usable.filter((piece) =>
          intersectUnions([piece], [commission.phenomenonInterval]).length > 0),
      );
      targetFrameEvidence.push({
        frameId: frame.id,
        localInterval: frame.localInterval,
        commonNominalInterval: nominal,
        mappedDroppedIntervals: holes,
        usableIntervals: usable,
        commissionOverlapIntervals,
        deliveries: frame.deliveries,
      });
    }

    const mergedTargetIntervals = mergeIntervals(targetPieces);

    const backgrounds = backgroundFrames.map((frame) => {
      const nominal = mapLocalInterval(frame.localInterval, station.clock.agreedOffset);
      const holes = frame.droppedIntervals.map((iv) => mapLocalInterval(iv, station.clock.agreedOffset));
      const usable = subtractHoles(nominal, holes);
      return {
        frameId: frame.id,
        localInterval: frame.localInterval,
        commonInterval: nominal,
        usableIntervals: usable,
        deliveries: frame.deliveries,
      };
    });

    const backgroundChecks = [];
    for (const piece of backgroundCheckPieces) {
      let best = null;
      for (const bg of backgrounds) {
        for (const bgInterval of bg.usableIntervals) {
          const verdict = isBackgroundEligible(bgInterval, piece, commission.backgroundPolicy);
          if (!best || (verdict.eligible && !best.verdict.eligible)
            || (verdict.eligible && verdict.relation.distance < best.verdict.relation.distance)) {
            best = { background: bg, interval: bgInterval, verdict };
          }
        }
      }
      backgroundChecks.push({
        exposureInterval: { start: piece.start, end: piece.end },
        frameId: piece.frameId,
        eligible: Boolean(best?.verdict.eligible),
        reason: best?.verdict.reason || 'NO_BACKGROUND',
        distance: best?.verdict.relation.distance ?? null,
        side: best?.verdict.relation.side ?? null,
        selectedBackgroundFrameId: best?.background.frameId || null,
        backgroundInterval: best?.interval || null,
      });
    }

    const eligibleChecks = backgroundChecks.filter((x) => x.eligible);
    const backgroundSupportedIntervals = mergeIntervals(
      eligibleChecks.map((x) => ({ start: x.exposureInterval.start, end: x.exposureInterval.end })),
    );

    return {
      stationId,
      stationName: station.name,
      status: station.status,
      clock: JSON.parse(JSON.stringify(station.clock)),
      clockCalibrated: station.clock.syncs.length >= 2,
      targetFrameEvidence,
      targetIntervals: mergedTargetIntervals,
      backgrounds,
      backgroundChecks,
      backgroundSupportedIntervals,
      actualArrivals: this.listArrivals(commission.id).filter((a) => a.stationId === stationId),
    };
  }

  evaluateCommission(id, remoteClock) {
    const c = this.commissions.get(id);
    if (!c) throw notFound('COMMISSION_NOT_FOUND', `找不到委托 ${id}`);

    const stationEvidence = c.stationIds.map((sid) => this.stationEvidence(c, sid));
    const unions = stationEvidence.map((s) => s.targetIntervals);
    let commonIntervals = intersectUnions(...unions);
    commonIntervals = commonIntervals
      .map((iv) => ({
        start: Math.max(iv.start, c.phenomenonInterval.start),
        end: Math.min(iv.end, c.phenomenonInterval.end),
      }))
      .filter((iv) => iv.end > iv.start);

    // Split at source exposure-piece boundaries so two adjacent frames with
    // different backgrounds are not treated as one background-check unit.
    const sourceBoundaries = new Set();
    for (const station of stationEvidence) {
      for (const check of station.backgroundChecks) {
        sourceBoundaries.add(check.exposureInterval.start);
        sourceBoundaries.add(check.exposureInterval.end);
      }
    }
    const partitionedIntervals = commonIntervals.flatMap((iv) => {
      const cuts = [
        iv.start,
        iv.end,
        ...[...sourceBoundaries].filter((point) => point > iv.start && point < iv.end),
      ].sort((a, b) => a - b);
      const pieces = [];
      for (let i = 0; i < cuts.length - 1; i += 1) {
        if (cuts[i + 1] > cuts[i]) pieces.push({ start: cuts[i], end: cuts[i + 1] });
      }
      return pieces;
    });

    const commonSegments = partitionedIntervals.map((iv) => {
      const checks = stationEvidence.map((station) => {
        const bgCheck = station.backgroundChecks.find((check) =>
          check.exposureInterval.start <= iv.start
          && check.exposureInterval.end >= iv.end);
        return {
          stationId: station.stationId,
          backgroundEligible: Boolean(bgCheck?.eligible),
          reason: bgCheck?.reason || 'NO_BACKGROUND_FOR_SEGMENT',
          distance: bgCheck?.distance ?? null,
          side: bgCheck?.side ?? null,
          backgroundFrameId: bgCheck?.selectedBackgroundFrameId || null,
          backgroundInterval: bgCheck?.backgroundInterval || null,
          exposureFrameId: bgCheck?.frameId || null,
        };
      });
      const backgroundSupported = checks.every((x) => x.backgroundEligible);
      return {
        interval: iv,
        seconds: intervalSeconds(iv),
        backgroundSupported,
        stationChecks: checks,
      };
    });

    const backgroundSupportedSeconds = sumSeconds(
      commonSegments.filter((s) => s.backgroundSupported).map((s) => s.interval),
    );
    const commonSeconds = sumSeconds(commonIntervals);
    const allClocksCalibrated = stationEvidence.every((s) => s.clockCalibrated);
    const clockRequirementMet = !c.requireClockCalibration || allClocksCalibrated;

    const missingStations = stationEvidence
      .filter((s) => s.targetIntervals.length === 0)
      .map((s) => s.stationId);
    const stationsWithoutBackground = stationEvidence
      .filter((s) => !s.backgroundChecks.some((x) => x.eligible))
      .map((s) => s.stationId);

    const reasons = [];
    if (!clockRequirementMet) reasons.push('CLOCK_NOT_CALIBRATED_WITH_TWO_SYNCS');
    if (commonSeconds < c.requiredCommonSeconds) reasons.push('INSUFFICIENT_COMMON_SECONDS');
    // Only call it a background gap when some otherwise common second is not
    // background-supported. If common duration itself is short, that shortage
    // is reported separately rather than mislabelled as a background failure.
    if (backgroundSupportedSeconds < commonSeconds) reasons.push('BACKGROUND_GAP');
    if (commonSeconds >= c.requiredCommonSeconds
      && backgroundSupportedSeconds < c.requiredCommonSeconds) reasons.push('BACKGROUND_INSUFFICIENT_FOR_REQUIREMENT');
    if (missingStations.length) reasons.push('MISSING_ACTUAL_FRAMES');
    if (stationsWithoutBackground.length) reasons.push('MISSING_ELIGIBLE_BACKGROUND');

    const qualified = reasons.length === 0;
    const reasonMessages = reasons.map((code) => REASON_LABELS[code] || code);
    const individualTotalSeconds = stationEvidence.reduce(
      (sum, s) => sum + sumSeconds(s.targetIntervals), 0,
    );

    const evaluation = {
      commissionId: c.id,
      requirementsVersion: c.requirementsVersion,
      evaluatedAtLogical: this.logicalClock + (remoteClock === undefined ? 0 : 1),
      phenomenonInterval: c.phenomenonInterval,
      requiredCommonSeconds: c.requiredCommonSeconds,
      commonIntervals,
      commonSegments,
      commonSeconds,
      backgroundSupportedSeconds,
      individualTotalSeconds,
      additiveSecondsRejected: commonSeconds === 0 && individualTotalSeconds > 0,
      allClocksCalibrated,
      clockRequirementMet,
      missingStations,
      stationsWithoutBackground,
      qualified,
      reasons,
      reasonMessages,
      stationEvidence,
      note: '每次都从当前实到帧和当前对时重新计算；计划表和派班回执不计入共同秒数。',
    };

    if (remoteClock !== undefined) {
      this.tick(remoteClock, 'commission.evaluated', null, {
        id,
        qualified,
        commonSeconds,
        required: c.requiredCommonSeconds,
      });
      evaluation.evaluatedAtLogical = this.logicalClock;
    }
    return evaluation;
  }

  // ---------- Windows, dispatch, relay ----------

  createWindow(body = {}, remoteClock) {
    const id = body.id || body.windowId || this.nextId('WIN');
    if (this.windows.has(id)) throw conflict('WINDOW_EXISTS', `联测窗口 ${id} 已存在`);
    const interval = intervalFromInput(body, ['interval', 'windowInterval', 'commonInterval']);
    if (!interval) throw badRequest('INTERVAL_REQUIRED', '窗口需要 interval:[start,end)');
    let stationIds = body.stationIds || body.pair || body.stations;
    if (typeof stationIds === 'string') stationIds = stationIds.split(',').map((s) => s.trim());
    if (!Array.isArray(stationIds) || stationIds.length !== 2) {
      throw badRequest('PAIR_REQUIRED', '预先划定的联测窗口必须属于指定站对（两站）');
    }
    stationIds = [...new Set(stationIds.map(String))].sort();
    for (const sid of stationIds) this.ensureStation(sid, {}, remoteClock);

    const window = {
      id,
      stationIds,
      interval,
      status: body.status === 'confirmed' ? 'confirmed' : 'proposed',
      note: body.note || '',
      createdAtLogical: this.logicalClock + 1,
    };
    this.windows.set(id, window);
    this.tick(remoteClock, 'window.created', null, { id, stationIds, interval, status: window.status });
    return JSON.parse(JSON.stringify(window));
  }

  confirmWindow(id, remoteClock) {
    const w = this.windows.get(id);
    if (!w) throw notFound('WINDOW_NOT_FOUND', `找不到窗口 ${id}`);
    w.status = 'confirmed';
    w.confirmedAtLogical = this.logicalClock + 1;
    this.tick(remoteClock, 'window.confirmed', null, { id });
    return JSON.parse(JSON.stringify(w));
  }

  listWindows(query = {}) {
    let items = [...this.windows.values()];
    if (query.stationId) items = items.filter((w) => w.stationIds.includes(query.stationId));
    if (query.confirmed === 'true' || query.confirmed === true) {
      items = items.filter((w) => w.status === 'confirmed');
    }
    return JSON.parse(JSON.stringify(items));
  }

  overlappingDispatch(stationIds, interval, excludeId = null) {
    for (const d of this.dispatches.values()) {
      if (excludeId && d.id === excludeId) continue;
      if (!['held', 'active', 'at_risk'].includes(d.status)) continue;
      if (!d.reservationInterval) continue;
      if (d.stationIds.some((sid) => stationIds.includes(sid))) {
        const overlap = intersectUnions([d.reservationInterval], [interval]);
        if (overlap.length) return d;
      }
    }
    return null;
  }

  dispatchFromWindow(body = {}, remoteClock, idempotencyKey) {
    if (idempotencyKey && this.idempotencyKeys.has(idempotencyKey)) {
      return { ...this.idempotencyKeys.get(idempotencyKey), duplicate: true };
    }
    const commissionId = body.commissionId;
    const windowId = body.windowId;
    const c = this.commissions.get(commissionId);
    if (!c) throw notFound('COMMISSION_NOT_FOUND', `找不到委托 ${commissionId}`);
    const w = this.windows.get(windowId);
    if (!w) throw notFound('WINDOW_NOT_FOUND', `找不到联测窗口 ${windowId}`);
    if (w.status !== 'confirmed') {
      throw conflict('WINDOW_NOT_CONFIRMED', `值守者只能凭已确认窗口派班；${windowId} 状态为 ${w.status}`);
    }
    if (!w.stationIds.every((sid) => c.stationIds.includes(sid))) {
      throw badRequest('WINDOW_PAIR_NOT_IN_COMMISSION', '窗口站对不属于该委托的站点组合');
    }
    const interval = body.interval ? toInterval(body.interval) : w.interval;
    const outside = interval.start < w.interval.start || interval.end > w.interval.end;
    if (outside) throw badRequest('OUTSIDE_WINDOW', '发出的时段不能超出已确认联测窗口');

    const conflictDispatch = this.overlappingDispatch(w.stationIds, interval);
    if (conflictDispatch) {
      throw conflict('SLOT_CONFLICT', '该站时段已派给另一委托，不能重复占用或随意转让', {
        existingDispatchId: conflictDispatch.id,
        commissionId: conflictDispatch.commissionId,
      });
    }

    const receipt = {
      id: body.id || this.nextId('D'),
      mode: 'confirmed-joint-window',
      commissionId,
      windowId,
      stationIds: [...w.stationIds],
      interval,
      reservationInterval: interval,
      nonTransferable: true,
      transferRule: '时段固定给本委托；如需改派，必须先取消并留下审计记录，不能直接转给临时委托',
      status: 'held',
      receipts: w.stationIds.map((sid) => ({
        stationId: sid,
        receiptId: this.nextId('RCPT'),
        acknowledged: true,
        atLogicalClock: this.logicalClock + 1,
      })),
      logicalClock: this.logicalClock + 1,
      createdAt: new Date().toISOString(),
    };
    this.dispatches.set(receipt.id, receipt);
    this.tick(remoteClock, 'dispatch.window_issued', null, {
      dispatchId: receipt.id,
      commissionId,
      windowId,
      stationIds: receipt.stationIds,
    });
    receipt.logicalClock = this.logicalClock;
    if (idempotencyKey) this.idempotencyKeys.set(idempotencyKey, receipt);
    return JSON.parse(JSON.stringify(receipt));
  }

  cancelDispatch(id, body = {}, remoteClock) {
    const d = this.dispatches.get(id);
    if (!d) throw notFound('DISPATCH_NOT_FOUND', `找不到派班回执 ${id}`);
    d.status = 'cancelled';
    d.cancellationReason = body.reason || 'cancelled by host';
    d.cancelledAtLogical = this.logicalClock + 1;
    this.tick(remoteClock, 'dispatch.cancelled', null, { id, reason: d.cancellationReason });
    return JSON.parse(JSON.stringify(d));
  }

  transferDispatch() {
    throw conflict(
      'NON_TRANSFERABLE_DISPATCH',
      '已发出的站上时段不能随意转给临时委托；请取消原派班后按已确认窗口/接力规则重新派班',
    );
  }

  planRelay(body = {}) {
    const forecast = body.forecast || {};
    const sentAt = Number(forecast.sentAt ?? body.forecastSentAt);
    if (!Number.isFinite(sentAt)) throw badRequest('FORECAST_SENT_AT_REQUIRED', '需要前站在线预报发出时间 forecast.sentAt');
    const predictedInterval = intervalFromInput(forecast, ['predictedInterval', 'interval'])
      ?? intervalFromInput(body, ['predictedInterval', 'targetInterval']);
    if (!predictedInterval) throw badRequest('FORECAST_INTERVAL_REQUIRED', '在线预报需要 predictedInterval');

    const notificationDelay = numOrZero(
      body.notificationDelaySeconds ?? body.notifyDelaySeconds ?? body.notificationDelay,
      'notificationDelay',
    );
    const receiptInput = body.receipt || body.receiptAck || {};
    const explicitOrderReceivedAt = body.orderReceivedAt
      ?? receiptInput.receivedAt
      ?? receiptInput.orderReceivedAt;
    const deliveryAt = explicitOrderReceivedAt !== undefined && explicitOrderReceivedAt !== null
      ? Number(explicitOrderReceivedAt)
      : sentAt + notificationDelay;
    if (!Number.isFinite(deliveryAt)) throw badRequest('INVALID_RECEIPT', '接收回执时间必须是数字');

    const receiptDelayInput = body.receiptDelaySeconds ?? body.receiptDelay ?? receiptInput.delaySeconds;
    const receiptDelay = receiptDelayInput === undefined ? 0 : numOrZero(receiptDelayInput, 'receiptDelay');
    const explicitCommanderReceiptAt = body.receiptReceivedAt
      ?? body.ackReceivedAt
      ?? receiptInput.commanderReceivedAt
      ?? receiptInput.ackReceivedAt;
    const commanderReceiptAt = explicitCommanderReceiptAt !== undefined && explicitCommanderReceiptAt !== null
      ? Number(explicitCommanderReceiptAt)
      : deliveryAt + receiptDelay;
    if (!Number.isFinite(commanderReceiptAt)) throw badRequest('INVALID_RECEIPT', '接收回执到达时间必须是数字');

    const candidatesInput = body.candidates || (body.stationId ? [{
      stationId: body.stationId,
      slewSeconds: body.slewSeconds ?? body.turnTimeSeconds ?? body.turnSeconds ?? 0,
      availableInterval: body.availableInterval,
    }] : []);
    if (!candidatesInput.length) throw badRequest('CANDIDATES_REQUIRED', '需要至少一个候选站');

    const candidates = candidatesInput.map((candidate) => {
      const stationId = candidate.stationId || candidate.id;
      if (!stationId) throw badRequest('CANDIDATE_ID_REQUIRED', '候选站需要 stationId');
      const slew = numOrZero(candidate.slewSeconds ?? candidate.turnTimeSeconds ?? body.slewSeconds ?? body.turnTimeSeconds ?? 0, 'slewSeconds');
      const readyAt = deliveryAt + slew;
      const available = candidate.availableInterval ? toInterval(candidate.availableInterval) : { start: -Infinity, end: Infinity };
      const lower = Math.max(predictedInterval.start, available.start, readyAt);
      const upper = Math.min(predictedInterval.end, available.end);
      const catchableInterval = lower < upper ? { start: lower, end: upper } : null;
      return {
        stationId,
        orderReceivedAt: deliveryAt,
        receiptCreatedAt: deliveryAt,
        commanderReceiptAt,
        slewSeconds: slew,
        readyAt,
        availableInterval: Number.isFinite(available.start) ? available : null,
        catchableInterval,
        catchableSeconds: catchableInterval ? intervalSeconds(catchableInterval) : 0,
        tooLate: !catchableInterval,
      };
    });

    return {
      mode: 'relay-command',
      decisionBasis: 'only the online forecast supplied before the observation; no post-pass full trajectory is read',
      postHocTrajectoryUsed: false,
      timeline: {
        forecastSentAt: sentAt,
        notificationDelaySeconds: notificationDelay,
        orderReceivedAt: deliveryAt,
        receipt: body.receipt || (explicitCommanderReceiptAt !== undefined ? { receivedAt: explicitCommanderReceiptAt } : null),
        receiptDelaySeconds: receiptDelay,
        commanderReceiptAt,
      },
      predictedInterval,
      candidates,
    };
  }

  dispatchRelay(body = {}, remoteClock, idempotencyKey) {
    if (idempotencyKey && this.idempotencyKeys.has(idempotencyKey)) {
      return { ...this.idempotencyKeys.get(idempotencyKey), duplicate: true };
    }
    const plan = this.planRelay(body);
    const requestedStation = body.stationId || body.selectStationId;
    let chosen;
    if (requestedStation) {
      chosen = plan.candidates.find((c) => c.stationId === requestedStation);
      if (!chosen) throw notFound('CANDIDATE_NOT_FOUND', `候选站 ${requestedStation} 不在输入列表中`);
    } else {
      chosen = [...plan.candidates].sort((a, b) => b.catchableSeconds - a.catchableSeconds)[0];
    }
    if (!chosen?.catchableInterval) {
      throw conflict('NO_TIMELY_CANDIDATE', '按通知延迟、回执和转向时间，没有候选站还能及时赶到任何预报段', plan);
    }

    const commissionId = body.commissionId;
    if (commissionId && !this.commissions.has(commissionId)) {
      throw notFound('COMMISSION_NOT_FOUND', `找不到委托 ${commissionId}`);
    }
    for (const sid of [chosen.stationId]) this.ensureStation(sid, {}, remoteClock);

    const conflictDispatch = commissionId
      ? this.overlappingDispatch([chosen.stationId], chosen.catchableInterval)
      : null;
    if (conflictDispatch) {
      throw conflict('SLOT_CONFLICT', '候选站的可赶时段已被另一派班占用', {
        existingDispatchId: conflictDispatch.id,
      });
    }

    const receipt = {
      id: body.id || this.nextId('D'),
      mode: 'relay-command',
      commissionId: commissionId || null,
      stationIds: [chosen.stationId],
      interval: chosen.catchableInterval,
      reservationInterval: chosen.catchableInterval,
      nonTransferable: true,
      status: 'held',
      forecast: body.forecast,
      plan,
      receipts: [{
        stationId: chosen.stationId,
        receiptId: this.nextId('RCPT'),
        orderReceivedAt: chosen.orderReceivedAt,
        commanderReceiptAt: chosen.commanderReceiptAt,
        readyAt: chosen.readyAt,
      }],
      logicalClock: this.logicalClock + 1,
      createdAt: new Date().toISOString(),
    };
    this.dispatches.set(receipt.id, receipt);
    this.tick(remoteClock, 'dispatch.relay_issued', chosen.stationId, {
      dispatchId: receipt.id,
      commissionId,
      catchableInterval: chosen.catchableInterval,
    });
    receipt.logicalClock = this.logicalClock;
    if (idempotencyKey) this.idempotencyKeys.set(idempotencyKey, receipt);
    return JSON.parse(JSON.stringify(receipt));
  }

  listDispatches(commissionId) {
    return [...this.dispatches.values()]
      .filter((d) => !commissionId || d.commissionId === commissionId)
      .map((d) => JSON.parse(JSON.stringify(d)));
  }

  // ---------- Host closing and sealed reports ----------

  makeupPlan(commissionId) {
    const evaluation = this.evaluateCommission(commissionId);
    const c = this.commissions.get(commissionId);
    const neededStations = new Set([...evaluation.missingStations, ...evaluation.stationsWithoutBackground]);
    const allConfirmedWindows = this.listWindows({ confirmed: true });
    const candidateConfirmedWindows = allConfirmedWindows
      .filter((w) => intersectUnions([w.interval], [c.phenomenonInterval]).length > 0)
      .filter((w) => w.stationIds.some((sid) => neededStations.has(sid))
        || w.stationIds.some((sid) => c.stationIds.includes(sid)))
      .map((w) => {
        const replacementStations = w.stationIds.filter((sid) => !c.stationIds.includes(sid));
        return {
          ...w,
          fulfilsOriginalCombination: w.stationIds.every((sid) => c.stationIds.includes(sid)),
          replacementStations,
          action: replacementStations.length
            ? `可派 ${replacementStations.join(',')} 补看，但需先把委托站点组合修订/另开为包含该站的新委托`
            : '可按原站点组合安排补看',
        };
      });

    return {
      commissionId,
      neededStations: [...neededStations],
      actualCommonSeconds: evaluation.commonSeconds,
      requiredCommonSeconds: c.requiredCommonSeconds,
      candidateConfirmedWindows,
      otherConfirmedWindows: allConfirmedWindows.filter((w) =>
        !candidateConfirmedWindows.some((candidate) => candidate.id === w.id)),
      rule: '这些窗口只能证明还能派谁补看；未收到实到帧前不得记为共视成功。',
      relayAlternative: 'POST /api/relay/plan with an online forecast to calculate a catchable segment.',
    };
  }

  closeCommission(id, body = {}, remoteClock) {
    const c = this.commissions.get(id);
    if (!c) throw notFound('COMMISSION_NOT_FOUND', `找不到委托 ${id}`);
    const evaluation = this.evaluateCommission(id, remoteClock);
    const dispatches = this.listDispatches(id);
    const arrivals = this.listArrivals(id);

    if (!evaluation.qualified) {
      c.status = 'pending_makeup';
      const makeupPlan = this.makeupPlan(id);
      const decision = {
        id: this.nextId('CASE'),
        commissionId: id,
        result: 'pending_makeup',
        closedAtLogical: this.logicalClock,
        host: body.host || 'duty-host',
        evaluation,
        makeupPlan,
        arrivals,
        dispatchReceipts: dispatches,
        note: '依实到记录判定共同秒数或背景不足；委托保留为待补，计划表只用于安排补看。',
      };
      this.caseDecisions.set(decision.id, decision);
      this.tick(remoteClock, 'commission.pending_makeup', body.host || 'duty-host', {
        id,
        reasons: evaluation.reasons,
      });
      decision.closedAtLogical = this.logicalClock;
      return JSON.parse(JSON.stringify(decision));
    }

    const version = [...this.reports.values()].filter((r) => r.commissionId === id).length + 1;
    const report = {
      id: body.reportId || `RPT-${id}-V${version}`,
      commissionId: id,
      version,
      sealed: true,
      original: version === 1,
      host: body.host || 'duty-host',
      sealedAtLogical: this.logicalClock + 1,
      requirementsSnapshot: {
        requirementsVersion: c.requirementsVersion,
        phenomenonInterval: c.phenomenonInterval,
        requiredCommonSeconds: c.requiredCommonSeconds,
        stationIds: c.stationIds,
        backgroundPolicy: c.backgroundPolicy,
      },
      evaluation,
      dispatchReceipts: dispatches,
      arrivals,
      incidentIds: [...this.incidents.values()].filter((i) => c.stationIds.includes(i.stationId)).map((i) => i.id),
      guarantee: '封交后报告正文不可变；重复交帧或后续对时修正只进入新事件/新版报告，不覆盖本原版。',
    };
    report.checksum = createHash('sha256').update(stableStringify({
      id: report.id,
      commissionId: report.commissionId,
      version: report.version,
      sealedAtLogical: report.sealedAtLogical,
      requirementsSnapshot: report.requirementsSnapshot,
      commonSegments: report.evaluation.commonSegments,
    })).digest('hex');

    if (this.reports.has(report.id)) throw conflict('REPORT_EXISTS', `报告号 ${report.id} 已封交且不可覆盖`);
    this.reports.set(report.id, report);
    c.status = 'sealed';
    c.latestReportId = report.id;
    if (!c.originalReportId) c.originalReportId = report.id;
    this.tick(remoteClock, 'report.sealed', body.host || 'duty-host', {
      reportId: report.id,
      commissionId: id,
      version,
      commonSeconds: evaluation.commonSeconds,
    });
    report.sealedAtLogical = this.logicalClock;
    return JSON.parse(JSON.stringify({ result: 'measured', commissionId: id, report }));
  }

  getReport(id) {
    const r = this.reports.get(id);
    if (!r) throw notFound('REPORT_NOT_FOUND', `找不到封交报告 ${id}`);
    return JSON.parse(JSON.stringify(r));
  }

  listReports(commissionId) {
    return [...this.reports.values()]
      .filter((r) => !commissionId || r.commissionId === commissionId)
      .map((r) => ({
        id: r.id,
        commissionId: r.commissionId,
        version: r.version,
        original: r.original,
        sealed: r.sealed,
        sealedAtLogical: r.sealedAtLogical,
        checksum: r.checksum,
        commonSeconds: r.evaluation.commonSeconds,
      }));
  }

  // Reader entry: commission number -> segments, backgrounds, receipts.
  traceCommission(id) {
    const c = this.getCommission(id);
    const evaluation = this.evaluateCommission(id);
    const reports = this.listReports(id);
    const fullReports = reports.map((r) => this.getReport(r.id));
    const dispatchReceipts = this.listDispatches(id);
    const arrivals = this.listArrivals(id);
    const makeupPlan = c.status === 'pending_makeup' || !evaluation.qualified ? this.makeupPlan(id) : null;
    const caseDecisions = [...this.caseDecisions.values()]
      .filter((decision) => decision.commissionId === id)
      .map((decision) => JSON.parse(JSON.stringify(decision)));
    const incidents = this.listIncidents().filter((incident) => c.stationIds.includes(incident.stationId));
    return {
      commission: c,
      readerLinks: {
        json: `/api/commissions/${encodeURIComponent(id)}/trace`,
        html: `/reader.html?commissionId=${encodeURIComponent(id)}`,
        friendlyPath: `/reader/${encodeURIComponent(id)}`,
      },
      commonSegments: evaluation.commonSegments,
      commonIntervals: evaluation.commonIntervals,
      commonSeconds: evaluation.commonSeconds,
      requiredCommonSeconds: evaluation.requiredCommonSeconds,
      backgrounds: evaluation.stationEvidence.map((s) => ({
        stationId: s.stationId,
        backgrounds: s.backgrounds,
        checks: s.backgroundChecks,
      })),
      stationEvidence: evaluation.stationEvidence,
      dispatchReceipts,
      arrivals,
      incidents,
      reports: fullReports,
      caseDecisions,
      makeupPlan,
      logicalClock: this.logicalClock,
    };
  }

  // ---------- Demo seed ----------

  seedDemo() {
    this.registerStation({ id: 'S1', name: '东河站', agreedOffset: 0 });
    this.registerStation({ id: 'S2', name: '西岭站', agreedOffset: 0 });
    this.registerStation({ id: 'S3', name: '南塬站', agreedOffset: 0, status: 'online' });
    this.registerStation({ id: 'S4', name: '北台站', agreedOffset: 0 });

    // Exact [4,8), 3-second worked example, already closed from actual frames.
    this.createCommission({
      id: 'C-DONE',
      title: '双站三秒实测样例（已封交）',
      phenomenonInterval: [4, 8],
      requiredCommonSeconds: 3,
      stationIds: ['S1', 'S2'],
      background: { before: 1, after: 1 },
    });
    this.createWindow({
      id: 'W-4-8-S1S2',
      stationIds: ['S1', 'S2'],
      interval: [4, 8],
      status: 'confirmed',
    });
    this.dispatchFromWindow({
      id: 'D-DONE',
      commissionId: 'C-DONE',
      windowId: 'W-4-8-S1S2',
    });
    this.submitFrame('S1', {
      id: 'F-DONE-S1-T', commissionId: 'C-DONE', mode: 'target', localInterval: [4, 8],
    });
    this.submitFrame('S2', {
      id: 'F-DONE-S2-T', commissionId: 'C-DONE', mode: 'target', localInterval: [5, 8],
    });
    this.submitFrame('S1', {
      id: 'BG-DONE-S1', commissionId: 'C-DONE', mode: 'background', localInterval: [3, 4],
    });
    this.submitFrame('S2', {
      id: 'BG-DONE-S2', commissionId: 'C-DONE', mode: 'background', localInterval: [4, 5],
    });
    this.closeCommission('C-DONE', { host: '主持人-林' });

    // Open laboratory for the clock-correction / four-second requirement story.
    this.createCommission({
      id: 'C-LAB',
      title: '对时修正与四秒重算实验',
      phenomenonInterval: [4, 8],
      requiredCommonSeconds: 3,
      stationIds: ['S1', 'S2'],
      background: { before: 1, after: 1 },
    });
    this.submitFrame('S1', { id: 'F-LAB-S1', mode: 'target', localInterval: [4, 8] });
    this.submitFrame('S2', { id: 'F-LAB-S2', mode: 'target', localInterval: [5, 8] });
    this.submitFrame('S1', { id: 'BG-LAB-S1', mode: 'background', localInterval: [3, 4] });
    this.submitFrame('S2', { id: 'BG-LAB-S2', mode: 'background', localInterval: [4, 5] });

    // Insufficient actual arrival: S3 is lost and has no target frame.
    this.createCommission({
      id: 'C-MAKEUP',
      title: '一站失联、实到不足待补',
      phenomenonInterval: [20, 24],
      requiredCommonSeconds: 3,
      stationIds: ['S1', 'S3'],
      background: { before: 1, after: 1 },
    });
    this.createWindow({
      id: 'W-20-24-S1S2',
      stationIds: ['S1', 'S2'],
      interval: [20, 24],
      status: 'confirmed',
    });
    this.submitFrame('S1', {
      id: 'F-MAKEUP-S1', commissionId: 'C-MAKEUP', mode: 'target', localInterval: [20, 24],
    });
    this.submitFrame('S1', {
      id: 'BG-MAKEUP-S1', commissionId: 'C-MAKEUP', mode: 'background', localInterval: [19, 20],
    });
    this.setStationStatus('S3', 'lost', { reason: 'seed: 南向链路无回执' });
    this.closeCommission('C-MAKEUP', { host: '主持人-林' });

    // Two three-second exposures that do not overlap and must not be summed.
    this.createCommission({
      id: 'C-STAGGER',
      title: '两份三秒彼此错开，不能相加',
      phenomenonInterval: [30, 40],
      requiredCommonSeconds: 3,
      stationIds: ['S1', 'S2'],
      background: { before: 1, after: 1 },
    });
    this.createWindow({
      id: 'W-30-39-S1S2',
      stationIds: ['S1', 'S2'],
      interval: [30, 39],
      status: 'confirmed',
    });
    this.submitFrame('S1', { id: 'F-STAG-S1', mode: 'target', localInterval: [31, 34] });
    this.submitFrame('S2', { id: 'F-STAG-S2', mode: 'target', localInterval: [36, 39] });
    this.submitFrame('S1', { id: 'BG-STAG-S1', mode: 'background', localInterval: [30, 31] });
    this.submitFrame('S2', { id: 'BG-STAG-S2', mode: 'background', localInterval: [39, 40] });
    this.closeCommission('C-STAGGER', { host: '主持人-周' });

    // Four-station logical-clock activity with a repeated S4 frame.
    this.createCommission({
      id: 'C-FOUR',
      title: '四站逻辑钟与重复交帧',
      phenomenonInterval: [40, 44],
      requiredCommonSeconds: 2,
      stationIds: ['S1', 'S2', 'S3', 'S4'],
      background: { before: 1, after: 1 },
    });
    this.createWindow({
      id: 'W-41-43-PAIR',
      stationIds: ['S3', 'S4'],
      interval: [41, 43],
      status: 'confirmed',
    });
    for (const sid of ['S1', 'S2', 'S3', 'S4']) {
      this.submitFrame(sid, {
        id: `F-FOUR-${sid}`,
        commissionId: 'C-FOUR',
        mode: 'target',
        localInterval: [41, 43],
      });
      this.submitFrame(sid, {
        id: `BG-FOUR-${sid}`,
        commissionId: 'C-FOUR',
        mode: 'background',
        localInterval: [40, 41],
      });
    }
    // S4 repeats the exact same frame after the report is sealed.
    this.closeCommission('C-FOUR', { host: '主持人-周' });
    this.submitFrame('S4', { id: 'F-FOUR-S4', commissionId: 'C-FOUR', mode: 'target', localInterval: [41, 43] });
    this.submitFrame('S4', { id: 'F-FOUR-S4', commissionId: 'C-FOUR', mode: 'target', localInterval: [41, 43] });

    // Two competing commissions and one confirmed window for race tests/demo.
    this.createWindow({
      id: 'W-RACE-50-54',
      stationIds: ['S1', 'S2'],
      interval: [50, 54],
      status: 'confirmed',
    });
    this.createCommission({
      id: 'C-RACE-A',
      title: '竞争委托 A',
      phenomenonInterval: [50, 54],
      requiredCommonSeconds: 3,
      stationIds: ['S1', 'S2'],
      background: { before: 1, after: 1 },
    });
    this.createCommission({
      id: 'C-RACE-B',
      title: '竞争委托 B',
      phenomenonInterval: [50, 54],
      requiredCommonSeconds: 3,
      stationIds: ['S1', 'S2'],
      background: { before: 1, after: 1 },
      adhoc: true,
    });

    // Relay worked example, stored as an auditable receipt. It uses only the
    // online forecast supplied at second 100, not the later observed trajectory.
    this.createCommission({
      id: 'C-RELAY',
      title: '接力指挥台在线预报派班示例',
      phenomenonInterval: [106, 112],
      requiredCommonSeconds: 3,
      stationIds: ['S1', 'S2'],
      background: { before: 2, after: 2 },
    });
    this.dispatchRelay({
      id: 'D-RELAY-DEMO',
      commissionId: 'C-RELAY',
      stationId: 'S2',
      forecast: { sentAt: 100, predictedInterval: [106, 112], knownUntil: 100 },
      notificationDelaySeconds: 1,
      receiptDelaySeconds: 1,
      slewSeconds: 2,
      availableInterval: [100, 120],
    });

    // Lose S3 again after the four-station report is sealed. The sealed report
    // and repeated-frame receipts remain readable; new work cannot count on S3.
    this.setStationStatus('S3', 'lost', { reason: 'seed: 四站封交后链路再次中断' });
  }
}
