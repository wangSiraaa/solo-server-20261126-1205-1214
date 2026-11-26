import crypto from 'node:crypto';

export const intOrNull = (value) => {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isInteger(n) ? n : null;
};

export const idOrNull = (value) => {
  if (value === null || value === undefined || value === '') return null;
  return String(value);
};

export const requireInt = (value, field) => {
  const n = intOrNull(value);
  if (n === null) {
    const err = new Error(`${field} must be an integer minute`);
    err.status = 400;
    err.code = 'INVALID_MINUTE';
    throw err;
  }
  return n;
};

export function overlaps(aStart, aEnd, bStart, bEnd) {
  return aStart < bEnd && bStart < aEnd;
}

export function intervalConflict(busy, resourceId, start, end, opts = {}) {
  for (const item of busy.get(resourceId) ?? []) {
    if (opts.ignoreAppointmentId && item.appointmentId === opts.ignoreAppointmentId) continue;
    if (overlaps(start, end, item.start, item.end)) return item;
  }
  return null;
}

export function shiftAllows(staff, start, end) {
  const shifts = staff?.schedule?.shifts ?? staff?.shifts ?? [];
  if (!Array.isArray(shifts) || shifts.length === 0) return true;
  return shifts.some((shift) => shift.start <= start && end <= shift.end);
}

function cloneBusyMap(source) {
  const target = new Map();
  for (const [id, intervals] of source.entries()) target.set(id, intervals.slice());
  return target;
}

function addBusy(map, id, interval) {
  if (id === null || id === undefined) return;
  if (!map.has(id)) map.set(id, []);
  map.get(id).push(interval);
}

export function unrollWindow(appointment) {
  const u = appointment?.unroll ?? {};
  const explicitEarliest = intOrNull(u.earliestStart);
  const explicitLatest = intOrNull(u.latestStart);
  if (explicitEarliest !== null && explicitLatest !== null) {
    return { earliestStart: explicitEarliest, latestStart: explicitLatest, kind: 'absolute' };
  }

  const base = intOrNull(appointment?.rollCompletedAt) ?? intOrNull(appointment?.plannedRollCompleteAt);
  const minWait = intOrNull(u.minWaitMinutes);
  const maxWait = intOrNull(u.maxWaitMinutes);
  if (base !== null && minWait !== null && maxWait !== null) {
    return {
      earliestStart: base + minWait,
      latestStart: base + maxWait,
      rollBase: base,
      kind: intOrNull(appointment?.rollCompletedAt) !== null ? 'actual-relative' : 'planned-relative',
    };
  }
  return null;
}

function episodeInterval(episode) {
  const start = intOrNull(episode.actualStart) ?? intOrNull(episode.reservedStart) ?? intOrNull(episode.from);
  const end = intOrNull(episode.actualEnd) ?? intOrNull(episode.to) ?? intOrNull(episode.plannedEnd) ?? intOrNull(episode.reservedEnd);
  if (start === null || end === null || end <= start) return null;
  return { start, end };
}

function shortServiceInterval(appointment) {
  const duration = intOrNull(appointment.durationMinutes) ?? intOrNull(appointment?.unroll?.durationMinutes);
  const scheduled = intOrNull(appointment.scheduledStartAt) ?? intOrNull(appointment.startAt);
  const started = intOrNull(appointment.startedAt);
  if (!duration || duration <= 0 || appointment.kind === 'perm') return null;

  if (started !== null) {
    const completed = intOrNull(appointment.completedAt);
    return {
      start: started,
      end: completed ?? started + duration,
      kind: 'service',
      appointmentId: appointment.id,
      source: 'actual-service',
    };
  }
  if (scheduled !== null) {
    return {
      start: scheduled,
      end: scheduled + duration,
      kind: 'service',
      appointmentId: appointment.id,
      source: 'planned-service',
    };
  }
  return null;
}

function sectionIntervals(appointment) {
  const result = [];
  for (const section of appointment.sections ?? []) {
    const completedAt = intOrNull(section.completedAt);
    const plannedStart = intOrNull(section.plannedStartAt);
    const workMinutes = intOrNull(section.workMinutes) ?? 0;
    const stylistId = section.stylistId ?? section.plannedStylistId ?? appointment.principalStylistId;

    if (completedAt !== null && stylistId) {
      const startedAt = intOrNull(section.startedAt) ?? completedAt - workMinutes;
      if (completedAt > startedAt) {
        result.push({
          start: startedAt,
          end: completedAt,
          kind: 'section',
          appointmentId: appointment.id,
          sectionId: section.id,
          stylistId,
          source: 'actual-section',
        });
      }
    } else if (plannedStart !== null && workMinutes > 0 && stylistId) {
      result.push({
        start: plannedStart,
        end: plannedStart + workMinutes,
        kind: 'section',
        appointmentId: appointment.id,
        sectionId: section.id,
        stylistId,
        source: 'planned-section',
      });
    }
  }
  return result;
}

function unrollActualInterval(appointment) {
  const u = appointment.unroll ?? {};
  const startedAt = intOrNull(u.startedAt);
  const duration = intOrNull(u.durationMinutes);
  if (startedAt === null || !duration || duration <= 0) return null;
  const completedAt = intOrNull(u.completedAt);
  return {
    start: startedAt,
    end: completedAt ?? startedAt + duration,
    kind: 'unroll',
    appointmentId: appointment.id,
    stylistId: idOrNull(u.stylistId) ?? idOrNull(appointment.principalStylistId),
    source: completedAt !== null ? 'actual-unroll' : 'active-unroll',
  };
}

export function currentStateHash(appointment) {
  const sections = (appointment.sections ?? []).map((s) => ({
    id: s.id,
    completedAt: intOrNull(s.completedAt),
    stylistId: s.stylistId ?? null,
    workMinutes: intOrNull(s.workMinutes) ?? null,
  }));
  const rollCompletedAt = intOrNull(appointment.rollCompletedAt) ?? null;
  return crypto
    .createHash('sha256')
    .update(JSON.stringify({ sections, rollCompletedAt }))
    .digest('hex')
    .slice(0, 16);
}

export function appointmentToPlan(appointment, db = null) {
  const staffIntervals = [];
  const chairIntervals = [];
  const requirements = [];

  const serviceInterval = shortServiceInterval(appointment);
  if (serviceInterval && appointment.stylistId) {
    staffIntervals.push({ ...serviceInterval, stylistId: appointment.stylistId });
  }

  for (const interval of sectionIntervals(appointment)) {
    if (interval.stylistId) staffIntervals.push(interval);
  }

  const unrollInterval = unrollActualInterval(appointment);
  if (unrollInterval?.stylistId) staffIntervals.push(unrollInterval);

  for (const episode of appointment.chairEpisodes ?? []) {
    const time = episodeInterval(episode);
    if (time && episode.chairId) {
      chairIntervals.push({
        ...time,
        chairId: episode.chairId,
        appointmentId: appointment.id,
        kind: 'chair-episode',
        source: episode.actualStart !== undefined ? 'actual-chair' : 'reserved-chair',
      });
    }
  }

  const u = appointment.unroll ?? {};
  const window = unrollWindow(appointment);
  const unrollStarted = intOrNull(u.startedAt) !== null;
  const unrollCompleted = intOrNull(u.completedAt) !== null;
  if (window && !unrollStarted && !unrollCompleted && intOrNull(u.durationMinutes) > 0) {
    let stylistId = idOrNull(u.stylistId);
    let eligibleStylistIds = null;

    if (!stylistId && appointment.mode === 'principal') {
      stylistId = idOrNull(appointment.principalStylistId);
    }
    if (!stylistId) {
      if (Array.isArray(appointment.eligibleStylistIds) && appointment.eligibleStylistIds.length) {
        eligibleStylistIds = appointment.eligibleStylistIds.slice();
      } else if (db) {
        eligibleStylistIds = db.staff
          .filter((s) => s.active !== false && s.canUnroll === true)
          .map((s) => s.id);
      } else {
        eligibleStylistIds = [];
      }
    }

    let chairId = idOrNull(u.chairId) ?? idOrNull(appointment.chairId);
    let chairIds = null;
    if (u.chairChoiceAllowed === true || appointment.movementAllowed === true) {
      if (Array.isArray(u.allowedChairIds) && u.allowedChairIds.length) chairIds = u.allowedChairIds.slice();
      else if (db) chairIds = db.chairs.filter((c) => c.active !== false).map((c) => c.id);
      chairId = null;
    } else if (chairId === null && db) {
      chairIds = db.chairs.filter((c) => c.active !== false).map((c) => c.id);
    }

    requirements.push({
      id: `unroll:${appointment.id}`,
      appointmentId: appointment.id,
      kind: 'unroll',
      earliestStart: window.earliestStart,
      latestStart: window.latestStart,
      durationMinutes: u.durationMinutes,
      stylistId,
      eligibleStylistIds,
      chairId,
      chairIds,
      chairChoiceAllowed: u.chairChoiceAllowed === true || appointment.movementAllowed === true,
      sourceWindow: window,
    });
  }

  return { staffIntervals, chairIntervals, requirements };
}

const ACTIVE_STATUSES = new Set(['booked', 'checked_in', 'processing', 'rolled', 'ready_for_unroll', 'in_unroll']);

export function buildBaseSchedule(db, opts = {}) {
  const staffBusy = new Map();
  const chairBusy = new Map();
  const requirements = [];

  for (const staff of db.staff ?? []) {
    const off = [
      ...(staff.unavailable ?? []),
      ...(staff.schedule?.timeOff ?? []),
      ...(staff.timeOff ?? []),
    ];
    for (const interval of off) {
      const start = intOrNull(interval.start);
      const end = intOrNull(interval.end);
      if (start !== null && end !== null && end > start) {
        addBusy(staffBusy, staff.id, {
          start,
          end,
          kind: 'time-off',
          source: interval.reason ?? 'time-off',
          appointmentId: interval.appointmentId ?? null,
        });
      }
    }
  }

  for (const chair of db.chairs ?? []) {
    const closed = [...(chair.unavailable ?? []), ...(chair.closures ?? [])];
    for (const interval of closed) {
      const start = intOrNull(interval.start) ?? intOrNull(interval.from);
      const end = intOrNull(interval.end) ?? intOrNull(interval.to) ?? intOrNull(interval.unavailableUntil);
      // Support {unavailableUntil: 2} with an implicit long-ago start.
      const normalizedStart = start ?? Number.MIN_SAFE_INTEGER;
      if (end !== null && end > normalizedStart) {
        addBusy(chairBusy, chair.id, {
          start: normalizedStart,
          end,
          kind: 'closure',
          source: interval.reason ?? 'closure',
          appointmentId: null,
        });
      }
    }
  }

  for (const appointment of db.appointments ?? []) {
    if (opts.excludeAppointmentId && appointment.id === opts.excludeAppointmentId) continue;
    if (!ACTIVE_STATUSES.has(appointment.status) && appointment.status !== 'completed') {
      // completed/unrolled but not checked out can still occupy chair; include completed too.
      if (!['completed', 'unrolled'].includes(appointment.status)) continue;
    }

    const plan = appointmentToPlan(appointment, db);
    for (const interval of plan.staffIntervals) addBusy(staffBusy, interval.stylistId, interval);
    for (const interval of plan.chairIntervals) addBusy(chairBusy, interval.chairId, interval);
    requirements.push(...plan.requirements);
  }

  return { staffBusy, chairBusy, requirements };
}

function checkFixedIntervalsAgainstBase(base, plan, db, candidateId) {
  const conflicts = [];
  const staffById = new Map((db.staff ?? []).map((s) => [s.id, s]));
  const chairById = new Map((db.chairs ?? []).map((c) => [c.id, c]));

  for (const interval of plan.staffIntervals) {
    const staff = staffById.get(interval.stylistId);
    if (!staff) {
      conflicts.push({ code: 'UNKNOWN_STYLIST', stylistId: interval.stylistId, interval });
      continue;
    }
    if (staff.active === false) {
      conflicts.push({ code: 'STYLIST_INACTIVE', stylistId: staff.id, interval });
    }
    if (!shiftAllows(staff, interval.start, interval.end)) {
      conflicts.push({ code: 'OUTSIDE_SHIFT', stylistId: staff.id, interval });
    }
    const hit = intervalConflict(base.staffBusy, interval.stylistId, interval.start, interval.end);
    if (hit) conflicts.push({ code: 'STYLIST_BUSY', stylistId: interval.stylistId, interval, conflict: hit });
  }

  for (const interval of plan.chairIntervals) {
    const chair = chairById.get(interval.chairId);
    if (!chair) {
      conflicts.push({ code: 'UNKNOWN_CHAIR', chairId: interval.chairId, interval });
      continue;
    }
    if (chair.active === false) {
      conflicts.push({ code: 'CHAIR_INACTIVE', chairId: chair.id, interval });
    }
    const hit = intervalConflict(base.chairBusy, interval.chairId, interval.start, interval.end, {
      ignoreAppointmentId: candidateId,
    });
    if (hit) conflicts.push({ code: 'CHAIR_BUSY', chairId: interval.chairId, interval, conflict: hit });
  }

  return conflicts;
}

function solveRequirements(requirements, staffBusy, chairBusy, db, opts = {}) {
  const ordered = requirements
    .map((req) => ({ ...req }))
    .sort((a, b) => a.latestStart - b.latestStart || a.earliestStart - b.earliestStart || a.appointmentId.localeCompare(b.appointmentId));

  const assignments = [];
  let failure = null;

  function choicesFor(req) {
    const staff = db.staff ?? [];
    let stylistIds = req.stylistId ? [req.stylistId] : (req.eligibleStylistIds ?? staff.filter((s) => s.active !== false && s.canUnroll === true).map((s) => s.id));
    stylistIds = stylistIds.filter((id, index) => stylistIds.indexOf(id) === index);

    let chairIds;
    if (req.chairId) chairIds = [req.chairId];
    else if (req.chairIds?.length) chairIds = req.chairIds;
    else chairIds = (db.chairs ?? []).filter((c) => c.active !== false).map((c) => c.id);

    const starts = [];
    const floor = intOrNull(opts.now) ?? -Infinity;
    const earliest = Math.max(req.earliestStart, floor);
    for (let start = earliest; start <= req.latestStart; start += 1) starts.push(start);
    return { stylistIds, chairIds, starts };
  }

  function visit(index) {
    if (index === ordered.length) return true;
    const req = ordered[index];
    const { stylistIds, chairIds, starts } = choicesFor(req);

    if (starts.length === 0) {
      failure = { ok: false, code: 'WINDOW_ALREADY_CLOSED', requirementId: req.id, appointmentId: req.appointmentId };
      return false;
    }
    if (stylistIds.length === 0) {
      failure = { ok: false, code: 'NO_ELIGIBLE_STYLIST', requirementId: req.id, appointmentId: req.appointmentId };
      return false;
    }

    for (const stylistId of stylistIds) {
      const staff = (db.staff ?? []).find((s) => s.id === stylistId);
      if (!staff || staff.active === false) continue;

      for (const start of starts) {
        const end = start + req.durationMinutes;
        if (!shiftAllows(staff, start, end)) continue;
        if (intervalConflict(staffBusy, stylistId, start, end)) continue;

        for (const chairId of chairIds) {
          const chair = (db.chairs ?? []).find((c) => c.id === chairId);
          if (!chair || chair.active === false) continue;
          // The appointment's own continuous chair episode is not a competing booking.
          if (intervalConflict(chairBusy, chairId, start, end, { ignoreAppointmentId: req.appointmentId })) continue;

          const staffInterval = {
            start,
            end,
            kind: req.kind,
            appointmentId: req.appointmentId,
            requirementId: req.id,
            source: 'solved-requirement',
          };
          const chairInterval = { ...staffInterval, chairId };
          addBusy(staffBusy, stylistId, staffInterval);
          addBusy(chairBusy, chairId, chairInterval);
          assignments.push({ requirementId: req.id, appointmentId: req.appointmentId, stylistId, chairId, start, end });

          if (visit(index + 1)) return true;

          assignments.pop();
          staffBusy.get(stylistId).pop();
          chairBusy.get(chairId).pop();
          failure = { ok: false, code: 'NO_RESOURCE_COMBINATION', requirementId: req.id, appointmentId: req.appointmentId };
        }
      }
    }

    if (!failure) failure = { ok: false, code: 'NO_FEASIBLE_ASSIGNMENT', requirementId: req.id, appointmentId: req.appointmentId };
    return false;
  }

  const ok = visit(0);
  return { ok, assignments: ok ? assignments : [], failure: ok ? null : failure };
}

export function evaluateAppointment(db, appointment, opts = {}) {
  const excludeAppointmentId = opts.excludeAppointmentId ?? appointment.id;
  const base = buildBaseSchedule(db, { excludeAppointmentId });
  const plan = appointmentToPlan(appointment, db);
  const fixedConflicts = checkFixedIntervalsAgainstBase(base, plan, db, excludeAppointmentId);

  if (fixedConflicts.length) {
    return { feasible: false, code: 'FIXED_CONFLICT', fixedConflicts, solver: null, assignments: [] };
  }

  const staffBusy = cloneBusyMap(base.staffBusy);
  const chairBusy = cloneBusyMap(base.chairBusy);
  for (const interval of plan.staffIntervals) addBusy(staffBusy, interval.stylistId, interval);
  for (const interval of plan.chairIntervals) addBusy(chairBusy, interval.chairId, interval);

  const requirements = [...base.requirements, ...plan.requirements];
  const solver = solveRequirements(requirements, staffBusy, chairBusy, db, opts);
  return {
    feasible: solver.ok,
    code: solver.ok ? 'FEASIBLE' : solver.failure?.code ?? 'INFEASIBLE',
    fixedConflicts,
    solver,
    assignments: solver.assignments,
    plan,
  };
}

function makeInsertCandidate(input, start, end, chairId) {
  const duration = input.durationMinutes;
  return {
    id: input.appointmentId ?? 'insertion-quote',
    code: 'QUOTE',
    kind: input.kind ?? 'short',
    mode: input.mode ?? 'principal',
    customerName: input.customerName ?? '插入客',
    stylistId: input.stylistId,
    principalStylistId: input.stylistId,
    status: 'booked',
    scheduledStartAt: start,
    durationMinutes: duration,
    chairId,
    chairEpisodes: chairId
      ? [{ chairId, reservedStart: start, reservedEnd: end, plannedEnd: end, reason: 'insertion-quote' }]
      : [],
    sections: [],
    unroll: null,
  };
}

export function findInsertion(db, input) {
  const duration = requirePositiveInt(input.durationMinutes, 'durationMinutes');
  const stylistId = input.stylistId;
  const stylist = (db.staff ?? []).find((s) => s.id === stylistId);
  if (!stylist) {
    return { feasible: false, code: 'UNKNOWN_STYLIST', message: `Unknown stylist ${stylistId}` };
  }
  if (stylist.active === false) return { feasible: false, code: 'STYLIST_INACTIVE' };

  let firstStart = intOrNull(input.startAt);
  const availableFrom = intOrNull(input.earliestStart) ?? intOrNull(input.availableFrom) ?? intOrNull(input.at);
  const availableUntil = intOrNull(input.latestFinish) ?? intOrNull(input.availableUntil) ?? intOrNull(input.latestEnd);

  let earliest;
  let latestFinish;
  if (firstStart !== null) {
    earliest = firstStart;
    latestFinish = intOrNull(availableUntil) ?? firstStart + duration;
  } else {
    earliest = availableFrom ?? 0;
    if (availableUntil === null) {
      return { feasible: false, code: 'MISSING_AVAILABILITY', message: 'latestFinish/availableUntil is required' };
    }
    latestFinish = availableUntil;
  }
  const latestStart = latestFinish - duration;
  if (earliest > latestStart) {
    return {
      feasible: false,
      code: 'SERVICE_DOES_NOT_FIT_AVAILABILITY',
      earliestStart: earliest,
      latestStart,
      durationMinutes: duration,
    };
  }

  const requestedChair = idOrNull(input.chairId);
  const chairChoices = requestedChair
    ? [requestedChair]
    : (db.chairs ?? []).filter((c) => c.active !== false).map((c) => c.id);

  const feasibleStarts = [];
  const feasiblePlans = [];
  const diagnostics = [];
  const maxCandidates = input.limit ?? 10000;
  let examined = 0;

  for (let start = earliest; start <= latestStart && examined < maxCandidates; start += 1) {
    examined += 1;
    const end = start + duration;
    for (const chairId of chairChoices) {
      const candidate = makeInsertCandidate(input, start, end, chairId);
      const report = evaluateAppointment(db, candidate);
      if (report.feasible) {
        feasibleStarts.push(start);
        feasiblePlans.push({ start, end, chairId, assignments: report.assignments });
        if (input.includeAll !== true) {
          return {
            feasible: true,
            code: 'FEASIBLE',
            start,
            end,
            chairId,
            feasibleStarts: [start],
            latestFeasibleStart: start,
            assignments: report.assignments,
            promise: promisePolicy(start),
          };
        }
      } else if (diagnostics.length < 12) {
        diagnostics.push({ start, end, chairId, code: report.code, fixedConflicts: report.fixedConflicts, failure: report.solver?.failure ?? null });
      }
    }
  }

  if (feasibleStarts.length) {
    const best = feasiblePlans[0];
    return {
      feasible: true,
      code: 'FEASIBLE',
      ...best,
      feasibleStarts,
      latestFeasibleStart: feasibleStarts[feasibleStarts.length - 1],
      allPlans: feasiblePlans,
      promise: promisePolicy(feasibleStarts[feasibleStarts.length - 1]),
    };
  }

  return {
    feasible: false,
    code: diagnostics[0]?.code ?? 'NO_FEASIBLE_INSERTION',
    earliestStart: earliest,
    physicalLatestStart: latestStart,
    durationMinutes: duration,
    diagnostics,
    fixedConflicts: diagnostics[0]?.fixedConflicts ?? [],
    failure: diagnostics[0]?.failure ?? null,
    promise: { canPromise: false, policy: 'ATOMIC_CONFIRMATION_ONLY' },
  };
}

function requirePositiveInt(value, field) {
  const n = intOrNull(value);
  if (n === null || n <= 0) {
    const err = new Error(`${field} must be a positive integer minute`);
    err.status = 400;
    err.code = 'INVALID_DURATION';
    throw err;
  }
  return n;
}

function promisePolicy(latestFeasibleStart) {
  return {
    canPromise: true,
    latestFeasibleStart,
    quoteHeld: false,
    policy: 'CONFIRM_ATOMICALLY_BEFORE_LAST_FEASIBLE_START',
    explanation: 'Quotes are not held. The add-on is promised only when confirmation atomically writes its fixed slot and rechecks every protected window.',
  };
}

export function planRecovery(db, input = {}) {
  const at = intOrNull(input.at) ?? 0;
  const base = buildBaseSchedule(db);
  const staffBusy = cloneBusyMap(base.staffBusy);
  const chairBusy = cloneBusyMap(base.chairBusy);

  const appointmentsById = new Map((db.appointments ?? []).map((a) => [a.id, a]));
  let requirements = base.requirements.map((req) => ({
    ...req,
    earliestStart: Math.max(req.earliestStart, at),
    recoveryOriginalEarliest: req.earliestStart,
  }));

  if (input.appointmentId) requirements = requirements.filter((r) => r.appointmentId === input.appointmentId);
  if (input.stylistId) {
    requirements = requirements.filter((r) => r.stylistId === input.stylistId || (!r.stylistId && (r.eligibleStylistIds ?? []).includes(input.stylistId)));
  }

  const priority = (req) => {
    const appt = appointmentsById.get(req.appointmentId) ?? {};
    const modeRank = appt.mode === 'principal' ? 0 : 1;
    const booked = intOrNull(appt.bookedAtMinute) ?? Number.MAX_SAFE_INTEGER;
    return [req.latestStart, modeRank, booked, appt.id];
  };
  requirements.sort((a, b) => {
    const pa = priority(a);
    const pb = priority(b);
    for (let i = 0; i < 3; i += 1) if (pa[i] !== pb[i]) return pa[i] - pb[i];
    return String(pa[3]).localeCompare(String(pb[3]));
  });

  const rescueOrder = [];
  const kept = [];
  let finalAssignments = [];

  for (const req of requirements) {
    const appt = appointmentsById.get(req.appointmentId);
    const alreadyClosed = req.earliestStart > req.latestStart;
    if (alreadyClosed) {
      rescueOrder.push({
        appointmentId: req.appointmentId,
        code: appt?.code,
        customerName: appt?.customerName,
        mode: appt?.mode,
        latestStart: req.latestStart,
        status: 'window_closed',
        reason: 'Latest contractual start has passed.',
      });
      continue;
    }

    const trialBusyStaff = cloneBusyMap(staffBusy);
    const trialBusyChair = cloneBusyMap(chairBusy);
    // Fixed intervals are already in base maps. Kept requirement intervals must be replayed by solving kept+req.
    const trial = solveRequirements([...kept, req], trialBusyStaff, trialBusyChair, db, { now: at });
    if (trial.ok) {
      kept.push(req);
      finalAssignments = trial.assignments;
      rescueOrder.push({
        appointmentId: req.appointmentId,
        code: appt?.code,
        customerName: appt?.customerName,
        mode: appt?.mode,
        latestStart: req.latestStart,
        status: 'save',
        priorityReason: 'Earliest irrecoverable latest-start deadline; ties prefer principal-protected and earlier booking.',
        plan: trial.assignments.find((a) => a.appointmentId === req.appointmentId) ?? null,
      });
    } else {
      const substituteOptions = findSubstituteOptions(db, req, kept, at);
      rescueOrder.push({
        appointmentId: req.appointmentId,
        code: appt?.code,
        customerName: appt?.customerName,
        mode: appt?.mode,
        latestStart: req.latestStart,
        status: substituteOptions.length ? 'needs_substitute_acceptance' : 'sacrifice_or_rebook',
        reason: trial.failure,
        substituteOptions,
      });
    }
  }

  let global = { ok: true, assignments: finalAssignments };
  if (kept.length) {
    global = solveRequirements(kept, cloneBusyMap(staffBusy), cloneBusyMap(chairBusy), db, { now: at });
  }

  return {
    at,
    feasible: rescueOrder.every((r) => r.status === 'save'),
    rescueOrder,
    assignments: global.assignments ?? [],
    policy: {
      order: 'earliest-latest-start-deadline-first',
      tieBreakers: ['principal-protected guest', 'earlier booking sequence'],
      rule: 'Never break a confirmed feasible promise to save a later deadline; a substitute is bookable only after personal state acceptance and chair confirmation.',
    },
  };
}

function findSubstituteOptions(db, req, kept, at) {
  const currentStylist = req.stylistId;
  const others = (db.staff ?? []).filter((s) => s.active !== false && s.canUnroll === true && s.id !== currentStylist);
  const options = [];
  for (const staff of others) {
    const altReq = { ...req, stylistId: staff.id, eligibleStylistIds: [staff.id] };
    const base = buildBaseSchedule(db);
    const result = solveRequirements([...kept, altReq], cloneBusyMap(base.staffBusy), cloneBusyMap(base.chairBusy), db, { now: at });
    if (result.ok) {
      options.push({ stylistId: staff.id, stylistName: staff.name, assignment: result.assignments.find((a) => a.appointmentId === req.appointmentId) });
    }
  }
  return options;
}
