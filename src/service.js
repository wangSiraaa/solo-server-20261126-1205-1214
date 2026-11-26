import fs from 'node:fs';
import path from 'node:path';
import {
  buildBaseSchedule,
  currentStateHash,
  evaluateAppointment,
  findInsertion,
  intOrNull,
  intervalConflict,
  planRecovery,
  requireInt,
  shiftAllows,
  unrollWindow,
} from './engine.js';

class HttpError extends Error {
  constructor(status, code, message, details) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function defaultDb() {
  return {
    version: 1,
    createdAt: new Date().toISOString(),
    shop: {
      name: 'Integer Minute Salon',
      insertionPolicy: 'ATOMIC_CONFIRMATION_ONLY',
      latenessPolicy: 'EARLIEST_LATEST_START_DEADLINE_FIRST',
    },
    seq: { staff: 0, chair: 0, service: 0, appointment: 0, handoff: 0, payment: 0 },
    staff: [],
    chairs: [],
    services: [],
    appointments: [],
  };
}

function normalizeInterval(input, prefix = '') {
  const start = intOrNull(input.start ?? input.from);
  const end = intOrNull(input.end ?? input.to);
  if (start === null || end === null || end <= start) {
    throw new HttpError(400, 'INVALID_INTERVAL', `${prefix} interval requires integer start/end with end > start`);
  }
  return { start, end, reason: input.reason ?? null };
}

export class SalonService {
  constructor({ dataFile = path.join(process.cwd(), 'data', 'salon.json') } = {}) {
    this.dataFile = dataFile;
    this.db = this.load();
  }

  load() {
    try {
      const raw = fs.readFileSync(this.dataFile, 'utf8');
      const parsed = JSON.parse(raw);
      return { ...defaultDb(), ...parsed, seq: { ...defaultDb().seq, ...(parsed.seq ?? {}) } };
    } catch (error) {
      if (error.code === 'ENOENT') return defaultDb();
      throw error;
    }
  }

  persist() {
    fs.mkdirSync(path.dirname(this.dataFile), { recursive: true });
    const tmp = `${this.dataFile}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(this.db, null, 2));
    fs.renameSync(tmp, this.dataFile);
  }

  reset() {
    this.db = defaultDb();
    this.persist();
    return this.db;
  }

  getState() {
    return this.db;
  }

  nextId(kind, prefix) {
    this.db.seq[kind] = (this.db.seq[kind] ?? 0) + 1;
    return `${prefix}${this.db.seq[kind]}`;
  }

  findAppointment(id) {
    const appt = this.db.appointments.find((a) => a.id === id || a.code === id);
    if (!appt) throw new HttpError(404, 'APPOINTMENT_NOT_FOUND', `Appointment ${id} was not found`);
    return appt;
  }

  findStaff(id) {
    const staff = this.db.staff.find((s) => s.id === id);
    if (!staff) throw new HttpError(404, 'STAFF_NOT_FOUND', `Stylist ${id} was not found`);
    return staff;
  }

  findChair(id) {
    const chair = this.db.chairs.find((c) => c.id === id);
    if (!chair) throw new HttpError(404, 'CHAIR_NOT_FOUND', `Chair ${id} was not found`);
    return chair;
  }

  addStaff(input = {}) {
    if (!String(input.name ?? '').trim()) throw new HttpError(400, 'NAME_REQUIRED', 'Stylist name is required');
    const id = input.id ? String(input.id) : this.nextId('staff', 'st_');
    if (this.db.staff.some((s) => s.id === id)) throw new HttpError(409, 'STAFF_EXISTS', `Stylist ${id} already exists`);

    const canUnroll = input.canUnroll === true
      || (Array.isArray(input.roles) && input.roles.includes('unroll'))
      || (Array.isArray(input.capabilities) && input.capabilities.includes('unroll'));

    const staff = {
      id,
      name: String(input.name).trim(),
      active: input.active !== false,
      canUnroll,
      capabilities: Array.isArray(input.capabilities) ? input.capabilities : [],
      acceptsOperations: Array.isArray(input.acceptsOperations) ? input.acceptsOperations : [],
      schedule: {
        shifts: (input.shifts ?? input.schedule?.shifts ?? []).map((x) => normalizeInterval(x, 'shift')),
        timeOff: (input.timeOff ?? input.schedule?.timeOff ?? []).map((x) => normalizeInterval(x, 'time-off')),
      },
      unavailable: (input.unavailable ?? []).map((x) => normalizeInterval(x, 'unavailable')),
    };
    this.db.staff.push(staff);
    this.persist();
    return staff;
  }

  updateStaffSchedule(id, input = {}) {
    const staff = this.findStaff(id);
    if (input.shifts) staff.schedule.shifts = input.shifts.map((x) => normalizeInterval(x, 'shift'));
    if (input.timeOff) staff.schedule.timeOff = input.timeOff.map((x) => normalizeInterval(x, 'time-off'));
    if (input.unavailable) staff.unavailable = input.unavailable.map((x) => normalizeInterval(x, 'unavailable'));
    if (input.active !== undefined) staff.active = input.active !== false;
    if (input.canUnroll !== undefined) staff.canUnroll = input.canUnroll !== false;
    this.persist();
    return staff;
  }

  addChair(input = {}) {
    if (!String(input.name ?? '').trim()) throw new HttpError(400, 'NAME_REQUIRED', 'Chair name is required');
    const id = input.id ? String(input.id) : this.nextId('chair', 'ch_');
    if (this.db.chairs.some((c) => c.id === id)) throw new HttpError(409, 'CHAIR_EXISTS', `Chair ${id} already exists`);

    const chair = {
      id,
      name: String(input.name).trim(),
      active: input.active !== false,
      movableCustomerAllowed: input.movableCustomerAllowed !== false,
      unavailable: (input.unavailable ?? []).map((x) => normalizeInterval(x, 'closure')),
      closures: (input.closures ?? []).map((x) => normalizeInterval(x, 'closure')),
    };
    this.db.chairs.push(chair);
    this.persist();
    return chair;
  }

  updateChair(id, input = {}) {
    const chair = this.findChair(id);
    if (input.name !== undefined) chair.name = String(input.name).trim();
    if (input.active !== undefined) chair.active = input.active !== false;
    if (input.unavailable) chair.unavailable = input.unavailable.map((x) => normalizeInterval(x, 'closure'));
    if (input.closures) chair.closures = input.closures.map((x) => normalizeInterval(x, 'closure'));
    if (input.addClosure) chair.unavailable.push(normalizeInterval(input.addClosure, 'closure'));
    this.persist();
    return chair;
  }

  addService(input = {}) {
    if (!String(input.name ?? '').trim()) throw new HttpError(400, 'NAME_REQUIRED', 'Service name is required');
    const id = input.id ? String(input.id) : this.nextId('service', 'svc_');
    const service = {
      id,
      name: String(input.name).trim(),
      kind: input.kind ?? 'general',
      durationMinutes: intOrNull(input.durationMinutes),
      sectionWorkMinutes: intOrNull(input.sectionWorkMinutes),
      unrollDurationMinutes: intOrNull(input.unrollDurationMinutes),
      minWaitMinutes: intOrNull(input.minWaitMinutes),
      maxWaitMinutes: intOrNull(input.maxWaitMinutes),
      requiredCapability: input.requiredCapability ?? null,
      price: input.price ?? null,
    };
    this.db.services.push(service);
    this.persist();
    return service;
  }

  normalizeSections(inputSections, service) {
    let raw = inputSections;
    if (!raw && service?.sectionCount) {
      raw = Array.from({ length: service.sectionCount }, (_, i) => ({ id: `sec_${i + 1}`, name: `区 ${i + 1}` }));
    }
    if (!Array.isArray(raw)) return [];
    return raw.map((section, index) => {
      if (typeof section === 'string') section = { name: section };
      const id = section.id ? String(section.id) : `sec_${index + 1}`;
      return {
        id,
        name: section.name ?? `第 ${index + 1} 区`,
        workMinutes: intOrNull(section.workMinutes) ?? intOrNull(service?.sectionWorkMinutes),
        plannedStartAt: intOrNull(section.plannedStartAt),
        plannedStylistId: section.plannedStylistId ?? null,
        requiredForMove: section.requiredForMove !== false,
        startedAt: intOrNull(section.startedAt),
        completedAt: intOrNull(section.completedAt),
        stylistId: section.stylistId ?? null,
        note: section.note ?? null,
      };
    });
  }

  bookAppointment(input = {}) {
    const customerName = String(input.customerName ?? '').trim();
    if (!customerName) throw new HttpError(400, 'CUSTOMER_NAME_REQUIRED', 'customerName is required');

    const mode = input.mode === 'process' ? 'process' : 'principal';
    let principalStylistId = input.principalStylistId ?? input.stylistId ?? null;
    if (mode === 'principal') {
      if (!principalStylistId) throw new HttpError(400, 'PRINCIPAL_REQUIRED', 'principalStylistId is required for principal schedule');
      const principal = this.findStaff(principalStylistId);
      if (principal.canUnroll !== true) {
        throw new HttpError(422, 'PRINCIPAL_NOT_UNROLL_QUALIFIED', 'The principal must be recorded as able to perform the protected unroll');
      }
      const signature = String(input.signature ?? input.signatureName ?? customerName).trim();
      if (!signature) throw new HttpError(400, 'SIGNATURE_REQUIRED', 'The customer must sign one name for principal booking');
      input.__signature = signature;
    } else if (principalStylistId) {
      this.findStaff(principalStylistId);
    }

    const service = input.serviceId ? this.db.services.find((s) => s.id === input.serviceId) : null;
    if (input.serviceId && !service) throw new HttpError(404, 'SERVICE_NOT_FOUND', `Service ${input.serviceId} was not found`);

    const kind = input.kind ?? service?.kind ?? (input.sections || input.unroll ? 'perm' : 'short');
    this.db.seq.appointment = (this.db.seq.appointment ?? 0) + 1;
    const sequence = this.db.seq.appointment;
    const id = input.id ? String(input.id) : `ap_${sequence}`;
    if (this.db.appointments.some((a) => a.id === id)) throw new HttpError(409, 'APPOINTMENT_EXISTS', `${id} already exists`);
    const code = input.code ?? `A-${String(sequence).padStart(4, '0')}`;

    const durationMinutes = intOrNull(input.durationMinutes) ?? intOrNull(service?.durationMinutes);
    const scheduledStartAt = intOrNull(input.scheduledStartAt) ?? intOrNull(input.startAt);

    let chairId = input.chairId ?? null;
    let chairEpisodes = [];

    const appointment = {
      id,
      code,
      kind,
      mode,
      customerName,
      signature: mode === 'principal' ? {
        name: input.__signature,
        at: intOrNull(input.signedAt) ?? intOrNull(input.bookedAtMinute) ?? null,
        isoAt: new Date().toISOString(),
      } : (input.signature ? { name: String(input.signature), isoAt: new Date().toISOString() } : null),
      principalStylistId: principalStylistId ? String(principalStylistId) : null,
      stylistId: input.stylistId ? String(input.stylistId) : (kind === 'short' ? String(principalStylistId ?? '') || null : null),
      status: 'booked',
      serviceId: service?.id ?? null,
      title: input.title ?? service?.name ?? (kind === 'perm' ? '卷发全套' : '短活'),
      scheduledStartAt,
      durationMinutes,
      customerAvailable: input.availableFrom !== undefined || input.availableUntil !== undefined ? {
        earliestStart: intOrNull(input.availableFrom ?? input.earliestStart),
        latestFinish: intOrNull(input.availableUntil ?? input.latestFinish),
      } : (input.customerAvailable ?? null),
      chairId: chairId ? String(chairId) : null,
      chairEpisodes,
      sections: this.normalizeSections(input.sections, service),
      plannedRollCompleteAt: intOrNull(input.plannedRollCompleteAt),
      rollCompletedAt: intOrNull(input.rollCompletedAt),
      movementAllowed: input.movementAllowed === true,
      mobility: null,
      unroll: this.normalizeUnroll(input.unroll, service),
      handoffs: [],
      payments: [],
      amount: input.amount ?? service?.price ?? null,
      bookingSequence: sequence,
      bookedAtMinute: intOrNull(input.bookedAtMinute) ?? intOrNull(input.at),
      insertionForAppointmentId: input.insertionForAppointmentId ?? null,
      insertionPromise: input.insertionPromise ?? null,
      history: [{ at: intOrNull(input.bookedAtMinute) ?? null, type: 'booked', detail: `Booked in ${mode} mode` }],
      createdAt: new Date().toISOString(),
    };

    if (appointment.sections.length && appointment.plannedRollCompleteAt === null) {
      const plannedEnds = appointment.sections
        .map((s) => (s.plannedStartAt !== null && s.workMinutes !== null ? s.plannedStartAt + s.workMinutes : null))
        .filter((n) => n !== null);
      if (plannedEnds.length) appointment.plannedRollCompleteAt = Math.max(...plannedEnds);
    }

    if (kind === 'short') {
      if (!appointment.stylistId) throw new HttpError(400, 'STYLIST_REQUIRED', 'Short appointment requires stylistId');
      this.findStaff(appointment.stylistId);
      if (!durationMinutes || durationMinutes <= 0) throw new HttpError(400, 'DURATION_REQUIRED', 'Short appointment requires positive durationMinutes');
      if (scheduledStartAt === null) throw new HttpError(400, 'START_REQUIRED', 'Confirmed appointment requires scheduledStartAt');

      if (!chairId) {
        const quote = findInsertion(this.db, {
          stylistId: appointment.stylistId,
          durationMinutes,
          startAt: scheduledStartAt,
          availableUntil: input.availableUntil,
        });
        if (!quote.feasible) {
          throw new HttpError(409, 'APPOINTMENT_NOT_FEASIBLE', 'No chair/schedule can honor this booking', quote);
        }
        chairId = quote.chairId;
        appointment.chairId = chairId;
      } else {
        this.findChair(chairId);
      }
      chairEpisodes.push({
        chairId,
        reservedStart: scheduledStartAt,
        reservedEnd: scheduledStartAt + durationMinutes,
        plannedEnd: scheduledStartAt + durationMinutes,
        reason: 'booking',
      });
      appointment.chairEpisodes = chairEpisodes;
    } else if (chairId && input.reserveChair === true && scheduledStartAt !== null) {
      this.findChair(chairId);
      const window = unrollWindow(appointment);
      const plannedEnd = input.expectedChairReleaseAt ?? (window ? window.latestStart + appointment.unroll.durationMinutes : null);
      if (plannedEnd !== null) {
        appointment.chairEpisodes.push({
          chairId,
          reservedStart: input.chairReservedFrom ?? scheduledStartAt,
          reservedEnd: plannedEnd,
          plannedEnd,
          reason: 'booking-reservation',
        });
      }
    }

    if (kind === 'perm' && !unrollWindow(appointment)) {
      throw new HttpError(400, 'UNROLL_WINDOW_REQUIRED', 'Provide plannedRollCompleteAt plus wait bounds, or unroll.earliestStart/latestStart');
    }

    const feasibility = evaluateAppointment(this.db, appointment);
    if (!feasibility.feasible) {
      throw new HttpError(409, 'APPOINTMENT_NOT_FEASIBLE', 'Booking cannot be honored without breaking a constraint', feasibility);
    }

    this.db.appointments.push(appointment);
    this.persist();
    return { appointment, feasibility };
  }

  normalizeUnroll(inputUnroll, service) {
    const input = inputUnroll ?? {};
    return {
      durationMinutes: intOrNull(input.durationMinutes) ?? intOrNull(service?.unrollDurationMinutes),
      minWaitMinutes: intOrNull(input.minWaitMinutes) ?? intOrNull(service?.minWaitMinutes),
      maxWaitMinutes: intOrNull(input.maxWaitMinutes) ?? intOrNull(service?.maxWaitMinutes),
      earliestStart: intOrNull(input.earliestStart),
      latestStart: intOrNull(input.latestStart),
      stylistId: input.stylistId ? String(input.stylistId) : null,
      chairId: input.chairId ? String(input.chairId) : null,
      allowedChairIds: Array.isArray(input.allowedChairIds) ? input.allowedChairIds.map(String) : null,
      chairChoiceAllowed: input.chairChoiceAllowed === true,
      requiredCapability: input.requiredCapability ?? service?.requiredCapability ?? null,
      acceptance: null,
      startedAt: intOrNull(input.startedAt),
      completedAt: intOrNull(input.completedAt),
    };
  }

  quoteInsertion(input = {}) {
    if (!input.stylistId) throw new HttpError(400, 'STYLIST_REQUIRED', 'stylistId is required');
    this.findStaff(input.stylistId);
    return findInsertion(this.db, input);
  }

  confirmInsertion(input = {}) {
    if (!input.customerName) throw new HttpError(400, 'CUSTOMER_NAME_REQUIRED', 'customerName is required');
    const quote = this.quoteInsertion(input);
    if (!quote.feasible) {
      throw new HttpError(409, 'INSERTION_NOT_FEASIBLE', 'Insertion cannot be promised while preserving all commitments', quote);
    }

    const result = this.bookAppointment({
      ...input,
      id: input.newAppointmentId ?? input.appointmentId,
      mode: 'principal',
      kind: input.kind ?? 'bang-trim',
      stylistId: input.stylistId,
      principalStylistId: input.stylistId,
      startAt: quote.start,
      scheduledStartAt: quote.start,
      chairId: quote.chairId,
      availableFrom: input.earliestStart ?? input.availableFrom,
      availableUntil: input.latestFinish ?? input.availableUntil,
      insertionForAppointmentId: input.targetAppointmentId ?? null,
      insertionPromise: quote.promise,
      signature: input.signature ?? input.customerName,
    });
    return { ...result, quote };
  }

  checkIn(id, input = {}) {
    const at = requireInt(input.at, 'at');
    const appt = this.findAppointment(id);
    if (!['booked'].includes(appt.status)) throw new HttpError(409, 'ALREADY_CHECKED_IN', `Status is ${appt.status}`);

    const chairId = input.chairId ?? appt.chairId;
    if (!chairId) throw new HttpError(400, 'CHAIR_REQUIRED', 'chairId is required at check-in');
    this.findChair(chairId);

    const window = unrollWindow(appt);
    let plannedEnd = intOrNull(input.expectedChairReleaseAt)
      ?? (window && appt.unroll?.durationMinutes ? window.latestStart + appt.unroll.durationMinutes : null)
      ?? (appt.kind === 'short' && appt.scheduledStartAt !== null ? appt.scheduledStartAt + appt.durationMinutes : null);
    if (plannedEnd === null) {
      throw new HttpError(400, 'CHAIR_RELEASE_REQUIRED', 'Provide expectedChairReleaseAt because no unroll/end window is available');
    }

    const { chairBusy } = buildBaseSchedule(this.db, { excludeAppointmentId: appt.id });
    const conflict = intervalConflict(chairBusy, chairId, at, plannedEnd);
    if (conflict) throw new HttpError(409, 'CHAIR_BUSY', 'Chair is not available for check-in', conflict);

    const existing = appt.chairEpisodes.find((e) => e.actualStart === undefined || e.actualStart === null);
    if (existing && existing.chairId === chairId) {
      existing.actualStart = at;
      existing.plannedEnd = plannedEnd;
    } else {
      if (existing) existing.actualEnd = at; // Release a different pre-reserved chair at arrival.
      appt.chairEpisodes.push({ chairId, actualStart: at, reservedStart: at, plannedEnd, reason: 'check-in' });
    }
    appt.chairId = chairId;
    appt.checkedInAt = at;
    appt.status = 'checked_in';
    appt.history.push({ at, type: 'check_in', detail: `Checked into ${chairId}` });
    this.persist();
    return appt;
  }

  completeSection(id, sectionId, input = {}) {
    const at = requireInt(input.at, 'at');
    const appt = this.findAppointment(id);
    if (!['checked_in', 'processing', 'rolled', 'ready_for_unroll'].includes(appt.status)) {
      throw new HttpError(409, 'CHECK_IN_REQUIRED', `Cannot complete section in status ${appt.status}`);
    }
    const section = (appt.sections ?? []).find((s) => s.id === sectionId);
    if (!section) throw new HttpError(404, 'SECTION_NOT_FOUND', `Section ${sectionId} was not found`);

    const stylistId = input.stylistId ?? section.stylistId ?? section.plannedStylistId ?? appt.principalStylistId;
    if (!stylistId) throw new HttpError(400, 'STYLIST_REQUIRED', 'Section completion requires stylistId');
    this.findStaff(stylistId);

    if (input.workMinutes !== undefined) section.workMinutes = intOrNull(input.workMinutes);
    if (input.startedAt !== undefined) section.startedAt = intOrNull(input.startedAt);
    section.stylistId = String(stylistId);
    section.completedAt = at;
    if (input.note !== undefined) section.note = input.note;
    if (input.operationAccepted !== undefined) section.operationAccepted = input.operationAccepted !== false;

    if (input.customerMovable !== undefined) {
      appt.movementAllowed = input.customerMovable === true;
      appt.mobility = {
        allowed: input.customerMovable === true,
        assessedBy: String(stylistId),
        at,
        reason: input.mobilityReason ?? (input.customerMovable ? 'Stylist confirms rollers/sections are stable enough to move' : 'Customer is not movable'),
        stateHash: currentStateHash(appt),
      };
    }

    const allDone = appt.sections.length > 0 && appt.sections.every((s) => intOrNull(s.completedAt) !== null);
    if (allDone) {
      appt.rollCompletedAt = Math.max(...appt.sections.map((s) => s.completedAt));
      appt.status = 'ready_for_unroll';
    } else {
      appt.status = 'processing';
    }
    appt.processStateHash = currentStateHash(appt);
    appt.unrollWindowSnapshot = unrollWindow(appt);
    appt.windowCheck = evaluateAppointment(this.db, appt);
    appt.history.push({ at, type: 'section_completed', detail: `${sectionId} completed by ${stylistId}` });
    this.persist();
    return appt;
  }

  assessMobility(id, input = {}) {
    const at = requireInt(input.at, 'at');
    const appt = this.findAppointment(id);
    const stylistId = input.stylistId;
    if (!stylistId) throw new HttpError(400, 'STYLIST_REQUIRED', 'stylistId is required');
    this.findStaff(stylistId);
    appt.movementAllowed = input.allowed !== false;
    appt.mobility = {
      allowed: appt.movementAllowed,
      assessedBy: stylistId,
      at,
      reason: input.reason ?? null,
      stateHash: currentStateHash(appt),
    };
    appt.history.push({ at, type: 'mobility_assessed', detail: appt.movementAllowed ? 'Movement allowed' : 'Movement not allowed' });
    this.persist();
    return appt;
  }

  movableChairs(id, query = {}) {
    const appt = this.findAppointment(id);
    const at = requireInt(query.at, 'at');
    if (!appt.movementAllowed || appt.mobility?.stateHash !== currentStateHash(appt)) {
      return { movable: false, chairs: [], reason: 'Customer is not currently cleared to move for this exact section state' };
    }
    const duration = intOrNull(query.durationMinutes) ?? intOrNull(appt.unroll?.durationMinutes) ?? 1;
    const { chairBusy } = this.scheduleBase(appt.id);
    const chairs = this.db.chairs.filter((chair) => {
      if (chair.active === false || chair.id === appt.chairId) return false;
      return !intervalConflict(chairBusy, chair.id, at, at + duration);
    }).map((c) => ({ id: c.id, name: c.name }));
    return { movable: true, chairs };
  }

  scheduleBase(excludeAppointmentId) {
    return buildBaseSchedule(this.db, { excludeAppointmentId });
  }

  moveChair(id, input = {}) {
    const at = requireInt(input.at, 'at');
    const toChairId = input.toChairId ?? input.chairId;
    if (!toChairId) throw new HttpError(400, 'CHAIR_REQUIRED', 'toChairId is required');
    this.findChair(toChairId);
    const appt = this.findAppointment(id);

    if (input.stylistId) this.findStaff(input.stylistId);
    if (input.confirmMovable !== true) {
      throw new HttpError(409, 'MOBILITY_CONFIRMATION_REQUIRED', 'Staff must confirm the customer may move');
    }
    if (!appt.movementAllowed || appt.mobility?.stateHash !== currentStateHash(appt)) {
      throw new HttpError(409, 'NOT_MOVABLE', 'The completed section state does not have valid movement clearance');
    }

    const episode = [...appt.chairEpisodes].reverse().find((e) => e.actualEnd === null || e.actualEnd === undefined);
    if (!episode) throw new HttpError(409, 'NO_ACTIVE_CHAIR_EPISODE', 'Customer has no active chair occupancy');
    const plannedEnd = episode.plannedEnd ?? episode.reservedEnd;
    if (plannedEnd === null) throw new HttpError(409, 'UNKNOWN_CHAIR_RELEASE', 'Cannot move without a planned release time');

    const { chairBusy } = this.scheduleBase(appt.id);
    const conflict = intervalConflict(chairBusy, toChairId, at, plannedEnd);
    if (conflict) throw new HttpError(409, 'TARGET_CHAIR_BUSY', 'Target chair is not available for the remaining stay', conflict);

    episode.actualEnd = at;
    const fromChairId = episode.chairId;
    appt.chairEpisodes.push({
      chairId: toChairId,
      actualStart: at,
      reservedStart: at,
      plannedEnd,
      reason: `move from ${fromChairId}`,
    });
    appt.chairId = toChairId;
    appt.history.push({ at, type: 'chair_moved', detail: `${fromChairId} -> ${toChairId}` });
    this.persist();
    return appt;
  }

  acceptHandoff(id, input = {}) {
    const at = requireInt(input.at, 'at');
    const appt = this.findAppointment(id);
    const stylistId = input.stylistId ?? input.toStylistId;
    if (!stylistId) throw new HttpError(400, 'STYLIST_REQUIRED', 'stylistId is required');
    const stylist = this.findStaff(stylistId);
    if (stylist.canUnroll === false) throw new HttpError(403, 'NOT_UNROLL_QUALIFIED', 'This stylist cannot perform unroll');
    if (input.confirmState !== true) {
      throw new HttpError(400, 'STATE_CONFIRMATION_REQUIRED', 'The receiving stylist must confirm the current section state');
    }

    const hash = currentStateHash(appt);
    const completedIds = new Set((appt.sections ?? []).filter((s) => s.completedAt !== null).map((s) => s.id));
    let acceptedSectionIds;
    if (input.acceptedSectionIds === '*' || input.acceptedSectionIds === 'all' || input.acceptCompletedSections === true) {
      acceptedSectionIds = [...completedIds];
    } else if (Array.isArray(input.acceptedSectionIds)) {
      acceptedSectionIds = input.acceptedSectionIds.map(String);
      for (const sectionId of acceptedSectionIds) {
        if (!appt.sections.some((s) => s.id === sectionId)) throw new HttpError(400, 'SECTION_NOT_FOUND', `Unknown section ${sectionId}`);
      }
    } else {
      throw new HttpError(400, 'ACCEPTED_SECTIONS_REQUIRED', 'Provide acceptedSectionIds or acceptCompletedSections:true');
    }

    if (input.forUnroll !== false) {
      const missing = [...completedIds].filter((sectionId) => !acceptedSectionIds.includes(sectionId));
      if (missing.length) {
        throw new HttpError(409, 'NOT_ALL_COMPLETED_SECTIONS_ACCEPTED', 'Unroll handoff must accept every completed section', { missing });
      }
    }

    const chairId = input.chairId ?? appt.unroll?.chairId ?? appt.chairId;
    if (!chairId) throw new HttpError(400, 'CHAIR_REQUIRED', 'Receiving stylist must identify a chair they can reserve');
    this.findChair(chairId);

    if (appt.mode === 'principal' && stylistId !== appt.principalStylistId) {
      if (input.customerConsent !== true) {
        throw new HttpError(409, 'SUBSTITUTE_CONSENT_REQUIRED', 'Principal schedule protects return to the principal; substitution needs customer consent');
      }
      if (!String(input.consentSignature ?? '').trim()) {
        throw new HttpError(400, 'CONSENT_SIGNATURE_REQUIRED', 'Record the customer signature agreeing to substitute');
      }
    }

    const previousAssignment = { stylistId: appt.unroll.stylistId, chairId: appt.unroll.chairId, acceptance: appt.unroll.acceptance };
    appt.unroll.stylistId = String(stylistId);
    appt.unroll.chairId = String(chairId);
    const feasibility = evaluateAppointment(this.db, appt);
    if (!feasibility.feasible) {
      Object.assign(appt.unroll, previousAssignment);
      throw new HttpError(409, 'HANDOFF_RESOURCE_NOT_AVAILABLE', 'Stylist cannot accept while preserving the unroll window/chair', feasibility);
    }

    const acceptance = {
      id: this.nextId('handoff', 'ho_'),
      at,
      stylistId: String(stylistId),
      acceptedSectionIds,
      stateHash: hash,
      chairId: String(chairId),
      customerConsent: input.customerConsent === true,
      consentSignature: input.consentSignature ? String(input.consentSignature) : null,
      note: input.note ?? null,
      assignment: feasibility.assignments.find((a) => a.appointmentId === appt.id) ?? null,
    };
    appt.unroll.acceptance = acceptance;
    appt.handoffs.push(acceptance);
    appt.processStateHash = hash;
    appt.history.push({ at, type: 'handoff_accepted', detail: `${stylistId} accepted state ${hash} for ${chairId}` });
    this.persist();
    return { appointment: appt, feasibility };
  }

  startUnroll(id, input = {}) {
    const at = requireInt(input.at, 'at');
    const appt = this.findAppointment(id);
    if (!appt.sections.length || !appt.sections.every((s) => s.completedAt !== null)) {
      throw new HttpError(409, 'SECTIONS_NOT_COMPLETE', 'All sections must be complete before unroll');
    }
    const window = unrollWindow(appt);
    if (!window) throw new HttpError(409, 'NO_UNROLL_WINDOW', 'Ticket has no agreed unroll window');
    if (at < window.earliestStart || at > window.latestStart) {
      throw new HttpError(409, 'OUTSIDE_UNROLL_WINDOW', `Start ${at} is outside [${window.earliestStart}, ${window.latestStart}]`, { window });
    }

    const stylistId = input.stylistId ?? appt.unroll?.stylistId ?? (appt.mode === 'principal' ? appt.principalStylistId : null);
    if (!stylistId) throw new HttpError(400, 'STYLIST_REQUIRED', 'Process ticket requires an accepted receiving stylist');
    this.findStaff(stylistId);

    if (appt.mode === 'process' || appt.unroll?.acceptance || stylistId !== appt.principalStylistId) {
      const acceptance = appt.unroll?.acceptance;
      if (!acceptance || acceptance.stylistId !== stylistId) {
        throw new HttpError(409, 'HANDOFF_NOT_ACCEPTED', 'This stylist must personally accept the section state before unroll');
      }
      if (acceptance.stateHash !== currentStateHash(appt)) {
        throw new HttpError(409, 'HANDOFF_STATE_STALE', 'Section state changed after acceptance; re-confirmation is required');
      }
    }

    const chairId = input.chairId ?? appt.unroll?.chairId ?? appt.chairId;
    if (!chairId) throw new HttpError(400, 'CHAIR_REQUIRED', 'chairId is required');
    if (chairId !== appt.chairId) {
      throw new HttpError(409, 'CHAIR_MOVE_REQUIRED', 'Move the customer to the reserved chair through the staff move endpoint first');
    }

    const duration = appt.unroll.durationMinutes;
    const { staffBusy, chairBusy } = this.scheduleBase(appt.id);
    const staff = this.findStaff(stylistId);
    if (staff.canUnroll !== true) throw new HttpError(403, 'NOT_UNROLL_QUALIFIED', 'This stylist is not recorded as unroll-qualified');
    if (!shiftAllows(staff, at, at + duration)) throw new HttpError(409, 'OUTSIDE_SHIFT', 'Stylist is off shift');
    const staffConflict = intervalConflict(staffBusy, stylistId, at, at + duration);
    if (staffConflict) throw new HttpError(409, 'STYLIST_BUSY', 'Stylist is busy for the unroll duration', staffConflict);
    const chairConflict = intervalConflict(chairBusy, chairId, at, at + duration);
    if (chairConflict) throw new HttpError(409, 'CHAIR_BUSY', 'Chair is busy for unroll', chairConflict);

    appt.unroll.stylistId = String(stylistId);
    appt.unroll.chairId = String(chairId);
    appt.unroll.startedAt = at;
    appt.status = 'in_unroll';
    appt.history.push({ at, type: 'unroll_started', detail: `Unroll started by ${stylistId}` });
    this.persist();
    return appt;
  }

  completeUnroll(id, input = {}) {
    const at = requireInt(input.at, 'at');
    const appt = this.findAppointment(id);
    if (appt.unroll?.startedAt === null || appt.unroll?.startedAt === undefined) {
      throw new HttpError(409, 'UNROLL_NOT_STARTED', 'Unroll has not started');
    }
    if (at < appt.unroll.startedAt) throw new HttpError(400, 'INVALID_END', 'Completion cannot precede start');
    if (input.stylistId && input.stylistId !== appt.unroll.stylistId) {
      throw new HttpError(403, 'WRONG_STYLIST', 'Only the stylist who started/accepted unroll should complete this operation');
    }
    appt.unroll.completedAt = at;
    appt.status = 'unrolled';
    appt.history.push({ at, type: 'unroll_completed', detail: `Unroll completed at ${at}` });
    this.persist();
    return appt;
  }

  checkout(id, input = {}) {
    const at = requireInt(input.at, 'at');
    const appt = this.findAppointment(id);
    if (appt.unroll?.completedAt === null || appt.unroll?.completedAt === undefined) {
      throw new HttpError(409, 'UNROLL_NOT_COMPLETE', 'Cannot checkout before unroll is complete');
    }
    const amount = intOrNull(input.amount) ?? intOrNull(appt.amount) ?? 0;
    const payment = {
      id: this.nextId('payment', 'pay_'),
      at,
      amount,
      method: input.method ?? 'cash',
      note: input.note ?? null,
    };
    appt.payments.push(payment);
    appt.paidAmount = (appt.paidAmount ?? 0) + amount;
    appt.status = 'checked_out';
    appt.checkedOutAt = at;

    const episode = [...appt.chairEpisodes].reverse().find((e) => e.actualEnd === null || e.actualEnd === undefined);
    if (episode) episode.actualEnd = at;
    appt.history.push({ at, type: 'checked_out', detail: `Paid ${amount} by ${payment.method}` });
    this.persist();
    return this.getReceipt(appt.id);
  }

  cancel(id, input = {}) {
    const appt = this.findAppointment(id);
    appt.status = 'cancelled';
    appt.cancelledAt = intOrNull(input.at) ?? null;
    appt.history.push({ at: appt.cancelledAt, type: 'cancelled', detail: input.reason ?? null });
    this.persist();
    return appt;
  }

  recover(input = {}) {
    return planRecovery(this.db, input);
  }

  reportLateness(id, input = {}) {
    const appt = this.findAppointment(id);
    const at = requireInt(input.at, 'at');
    appt.lateness = {
      reportedAt: at,
      delayMinutes: intOrNull(input.delayMinutes),
      reason: input.reason ?? null,
    };
    appt.history.push({ at, type: 'lateness_reported', detail: input.reason ?? `Late ${input.delayMinutes ?? ''} min` });
    this.persist();
    return { appointment: appt, recovery: planRecovery(this.db, { ...input, at }) };
  }

  getReceipt(id) {
    const appt = this.findAppointment(id);
    const window = unrollWindow(appt);
    const realizedSections = (appt.sections ?? []).map((section) => ({
      sectionId: section.id,
      name: section.name,
      state: section.completedAt !== null && section.completedAt !== undefined ? 'realized' : 'pending',
      stylistId: section.stylistId ?? null,
      completedAt: section.completedAt ?? null,
      workMinutes: section.workMinutes ?? null,
    }));

    const segments = [
      {
        segment: 'booking',
        state: 'realized',
        at: appt.createdAt,
        detail: `${appt.mode} booking ${appt.code}; signature ${appt.signature?.name ?? 'none'}`,
      },
      {
        segment: 'check_in',
        state: appt.checkedInAt !== undefined ? 'realized' : 'pending',
        at: appt.checkedInAt ?? null,
        chairId: appt.chairId,
      },
      ...realizedSections.map((s) => ({ segment: `section:${s.sectionId}`, ...s })),
      {
        segment: 'roll_complete',
        state: appt.rollCompletedAt !== null && appt.rollCompletedAt !== undefined ? 'realized' : (appt.plannedRollCompleteAt !== null ? 'planned' : 'pending'),
        at: appt.rollCompletedAt ?? appt.plannedRollCompleteAt ?? null,
        actual: appt.rollCompletedAt ?? null,
        planned: appt.plannedRollCompleteAt ?? null,
      },
      {
        segment: 'unroll_window',
        state: window ? (intOrNull(appt.unroll?.startedAt) !== null ? 'realized' : 'protected') : 'missing',
        ...window,
      },
      {
        segment: 'handoff',
        state: appt.unroll?.acceptance ? 'realized' : (appt.mode === 'process' ? 'pending_acceptance' : 'not_required_unless_substitute'),
        acceptance: appt.unroll?.acceptance ?? null,
      },
      {
        segment: 'unroll',
        state: intOrNull(appt.unroll?.completedAt) !== null ? 'realized' : intOrNull(appt.unroll?.startedAt) !== null ? 'in_progress' : 'protected',
        stylistId: appt.unroll?.stylistId ?? appt.principalStylistId,
        startedAt: appt.unroll?.startedAt ?? null,
        completedAt: appt.unroll?.completedAt ?? null,
        durationMinutes: appt.unroll?.durationMinutes ?? null,
      },
      {
        segment: 'checkout',
        state: appt.checkedOutAt !== undefined ? 'realized' : 'pending',
        at: appt.checkedOutAt ?? null,
        payments: appt.payments ?? [],
      },
    ];

    return {
      id: appt.id,
      code: appt.code,
      customerName: appt.customerName,
      mode: appt.mode,
      status: appt.status,
      processStateHash: appt.processStateHash ?? currentStateHash(appt),
      chairEpisodes: appt.chairEpisodes,
      segments,
      appointment: appt,
    };
  }
}

export { HttpError };
