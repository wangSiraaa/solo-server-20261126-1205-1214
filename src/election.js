import { randomUUID } from 'node:crypto';

export const RULE_VERSION = 'joint-v1';

export const DEFAULT_SEATS = [
  { id: 'reading', name: '阅览室', quota: 2 },
  { id: 'court', name: '球场', quota: 2 },
];

export const PUBLIC_RULE = {
  ruleVersion: RULE_VERSION,
  mode: 'prior-joint-commitment',
  quota: 2,
  ticketOrder: '按交票流水号（seq）从小到大；同一票号再按 ballotId 字典序',
  overQuota:
    '同一“人＋席”有效支持超过两票时，只启用最早的两张；其余票不消耗，并继续扫描后项。',
  personRule: '同一人即使有两席资格也只能持有一张就任书；优先兑现其书面偏好席。',
  jointRule:
    '结算在一个持久化事务中生成相容的就任书；未满额、拒绝承诺、资格不符或已由他人就任的席位置空。',
  tieBreak:
    '同人双席均达额时先看本人承诺的偏好席；仍冲突时按第二张定额票的流水号、票号序列、席位顺序、人员 ID 决定。',
};

export class DomainError extends Error {
  constructor(status, code, message, details = {}) {
    super(message);
    this.name = 'DomainError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function byTicketOrder(a, b) {
  const sa = Number.isFinite(a.seq) ? a.seq : Number.MAX_SAFE_INTEGER;
  const sb = Number.isFinite(b.seq) ? b.seq : Number.MAX_SAFE_INTEGER;
  if (sa !== sb) return sa - sb;
  return String(a.id).localeCompare(String(b.id));
}

export function seatMap(round) {
  return new Map((round.seats ?? DEFAULT_SEATS).map((seat, index) => [seat.id, { ...seat, index }]));
}

export function candidateMap(round) {
  if (Array.isArray(round.candidates)) {
    return new Map(round.candidates.map((candidate) => [candidate.id, candidate]));
  }
  return new Map(Object.entries(round.candidates ?? {}));
}

export function responseFor(candidate, seatId) {
  const commitment = candidate?.commitment ?? {};
  const explicit = commitment.seatResponses?.[seatId] ?? commitment.responses?.[seatId];
  if (explicit) return explicit;
  if (commitment.refusedSeatIds?.includes(seatId)) return 'refused';
  if (commitment.acceptedSeatIds?.includes(seatId)) return 'accepted';
  if (commitment.willingSeatIds?.includes(seatId)) return 'willing';
  return 'uncommitted';
}

const POSITIVE_RESPONSES = new Set(['willing', 'committed', 'accepted']);

export function classifyChoice(round, choice, context = {}) {
  const seats = seatMap(round);
  const candidates = candidateMap(round);
  const seat = seats.get(choice?.seatId);
  const candidate = candidates.get(choice?.personId ?? choice?.candidateId);
  const seatedPersonIds = context.seatedPersonIds ?? new Set();
  const filledSeatIds = context.filledSeatIds ?? new Set();

  if (!seat) return { available: false, reason: 'unknown_seat', seatId: choice?.seatId };
  if (!candidate) {
    return { available: false, reason: 'unknown_candidate', personId: choice?.personId ?? choice?.candidateId };
  }
  if (!candidate.qualifiedSeatIds?.includes(seat.id)) {
    return { available: false, reason: 'ineligible', personId: candidate.id, seatId: seat.id };
  }

  const response = responseFor(candidate, seat.id);
  if (response === 'refused' || response === 'declined') {
    return { available: false, reason: 'refused', response, personId: candidate.id, seatId: seat.id };
  }
  if (response === 'expired') {
    return { available: false, reason: 'commitment_expired', response, personId: candidate.id, seatId: seat.id };
  }
  if (response === 'withdrawn') {
    return { available: false, reason: 'withdrawn', response, personId: candidate.id, seatId: seat.id };
  }
  if (!POSITIVE_RESPONSES.has(response)) {
    return { available: false, reason: 'unwilling', response, personId: candidate.id, seatId: seat.id };
  }

  if (seatedPersonIds.has(candidate.id)) {
    return {
      available: false,
      reason: 'person_already_seated',
      personId: candidate.id,
      seatId: seat.id,
      existingAppointmentId: context.personAppointmentIds?.get?.(candidate.id),
    };
  }
  if (filledSeatIds.has(seat.id)) {
    return {
      available: false,
      reason: 'seat_filled',
      personId: candidate.id,
      seatId: seat.id,
      existingAppointmentId: context.seatAppointmentIds?.get?.(seat.id),
    };
  }

  return { available: true, reason: 'active', candidate, seat, response };
}

function choicePersonId(choice) {
  return choice.personId ?? choice.candidateId;
}

function makeContext(appointments) {
  const seatedPersonIds = new Set();
  const filledSeatIds = new Set();
  const personAppointmentIds = new Map();
  const seatAppointmentIds = new Map();
  for (const appointment of appointments ?? []) {
    if (appointment.status && appointment.status !== 'issued') continue;
    seatedPersonIds.add(appointment.personId);
    filledSeatIds.add(appointment.seatId);
    personAppointmentIds.set(appointment.personId, appointment.id);
    seatAppointmentIds.set(appointment.seatId, appointment.id);
  }
  return { seatedPersonIds, filledSeatIds, personAppointmentIds, seatAppointmentIds };
}

/**
 * Scan one ballot from its current pointer. Returns the first currently
 * supportable choice and mutates pointer/trail for permanently skipped items.
 */
function advanceBallot(round, ballot, pointerState, trails, context, extraSkipHandler = null) {
  let pointer = pointerState.get(ballot.id) ?? 0;
  const choices = ballot.choices ?? [];

  while (pointer < choices.length) {
    const choice = choices[pointer];
    const classification = classifyChoice(round, choice, context);
    if (classification.available) {
      pointerState.set(ballot.id, pointer);
      return { choiceIndex: pointer, choice, classification };
    }

    const trailEntry = {
      choiceIndex: pointer,
      personId: choicePersonId(choice),
      seatId: choice.seatId,
      result: 'skipped',
      reason: classification.reason,
    };
    if (classification.existingAppointmentId) {
      trailEntry.existingAppointmentId = classification.existingAppointmentId;
    }
    trails.get(ballot.id).push(trailEntry);
    extraSkipHandler?.(ballot.id, pointer, classification);
    pointer += 1;
  }

  pointerState.set(ballot.id, pointer);
  return null;
}

function eventPriority(events, round) {
  const candidates = candidateMap(round);
  const seatOrder = new Map((round.seats ?? DEFAULT_SEATS).map((seat, index) => [seat.id, index]));

  for (const event of events) {
    const samePerson = events.filter((other) => other.personId === event.personId);
    const preferredSeatId = candidates.get(event.personId)?.commitment?.preferredSeatId;
    event.personConflictRank =
      samePerson.length > 1 && preferredSeatId === event.seatId
        ? 0
        : samePerson.length > 1
          ? 1
          : 0;
    event.closingSeq = event.selected[event.selected.length - 1]?.seq ?? Number.MAX_SAFE_INTEGER;
    event.seqTuple = event.selected.map((ticket) => ticket.seq ?? Number.MAX_SAFE_INTEGER);
    event.seatOrder = seatOrder.get(event.seatId) ?? Number.MAX_SAFE_INTEGER;
  }

  events.sort((a, b) => {
    if (a.personConflictRank !== b.personConflictRank) return a.personConflictRank - b.personConflictRank;
    if (a.closingSeq !== b.closingSeq) return a.closingSeq - b.closingSeq;
    for (let i = 0; i < Math.max(a.seqTuple.length, b.seqTuple.length); i += 1) {
      const diff = (a.seqTuple[i] ?? Infinity) - (b.seqTuple[i] ?? Infinity);
      if (diff !== 0) return diff;
    }
    if (a.seatOrder !== b.seatOrder) return a.seatOrder - b.seatOrder;
    if (a.seatId !== b.seatId) return a.seatId.localeCompare(b.seatId);
    return a.personId.localeCompare(b.personId);
  });
  return events[0];
}

/**
 * Deterministic prior-joint settlement.
 *
 * The returned appointments are drafts: the persistence layer assigns stable
 * ids/receipt codes. Ballots are marked used only by a returned appointment.
 */
export function computeSettlement(round) {
  const seats = seatMap(round);
  const ballots = [...(round.ballots ?? [])].sort(byTicketOrder);
  const quotaOf = (seatId) => seats.get(seatId)?.quota ?? 2;

  const pointerState = new Map(ballots.map((ballot) => [ballot.id, 0]));
  const trails = new Map(ballots.map((ballot) => [ballot.id, []]));
  const usedBallotIds = new Set();
  const usedAppointmentIndexByBallot = new Map();
  const appointments = [];
  const steps = [];
  const context = {
    seatedPersonIds: new Set(),
    filledSeatIds: new Set(),
    personAppointmentIds: new Map(),
    seatAppointmentIds: new Map(),
  };

  let guard = 0;
  const maxIterations = ballots.reduce((sum, ballot) => sum + (ballot.choices?.length ?? 0), 0) + ballots.length + 5;

  while (guard++ < maxIterations) {
    const groups = new Map();

    for (const ballot of ballots) {
      if (usedBallotIds.has(ballot.id)) continue;
      const active = advanceBallot(round, ballot, pointerState, trails, context);
      if (!active) continue;

      const key = `${choicePersonId(active.choice)}::${active.choice.seatId}`;
      if (!groups.has(key)) {
        groups.set(key, {
          key,
          personId: choicePersonId(active.choice),
          seatId: active.choice.seatId,
          supportBallots: [],
        });
      }
      groups.get(key).supportBallots.push(ballot);
    }

    const events = [...groups.values()]
      .map((group) => {
        const quota = quotaOf(group.seatId);
        const supportBallots = group.supportBallots.sort(byTicketOrder);
        return {
          ...group,
          quota,
          supportBallots,
          selected: supportBallots.slice(0, quota),
        };
      })
      .filter((event) => event.supportBallots.length >= event.quota);

    if (events.length === 0) break;

    const event = eventPriority(events, round);
    const appointmentIndex = appointments.length;
    const ballotUsages = [];

    for (const ballot of event.selected) {
      const choiceIndex = pointerState.get(ballot.id);
      const choice = ballot.choices[choiceIndex];
      trails.get(ballot.id).push({
        choiceIndex,
        personId: event.personId,
        seatId: event.seatId,
        result: 'used',
        reason: 'quota_selected_by_ticket_order',
      });
      usedBallotIds.add(ballot.id);
      usedAppointmentIndexByBallot.set(ballot.id, appointmentIndex);
      ballotUsages.push({
        ballotId: ballot.id,
        choiceIndex,
        trail: trails.get(ballot.id).map((entry) => ({ ...entry })),
      });
    }

    for (const ballot of event.supportBallots.slice(event.quota)) {
      const choiceIndex = pointerState.get(ballot.id);
      trails.get(ballot.id).push({
        choiceIndex,
        personId: event.personId,
        seatId: event.seatId,
        result: 'skipped',
        reason: 'quota_filled_by_earlier_tickets',
        selectedBallotIds: event.selected.map((ticket) => ticket.id),
      });
      pointerState.set(ballot.id, choiceIndex + 1);
    }

    appointments.push({
      appointmentIndex,
      seatId: event.seatId,
      personId: event.personId,
      quota: event.quota,
      ballotIds: event.selected.map((ticket) => ticket.id),
      ballotUsages,
      selectionRule: RULE_VERSION,
    });
    context.seatedPersonIds.add(event.personId);
    context.filledSeatIds.add(event.seatId);
    context.personAppointmentIds.set(event.personId, appointmentIndex);
    context.seatAppointmentIds.set(event.seatId, appointmentIndex);

    steps.push({
      appointmentIndex,
      personId: event.personId,
      seatId: event.seatId,
      quota: event.quota,
      supportCount: event.supportBallots.length,
      selectedBallotIds: event.selected.map((ticket) => ticket.id),
      overflowBallotIds: event.supportBallots.slice(event.quota).map((ticket) => ticket.id),
      personConflictRank: event.personConflictRank,
      closingSeq: event.closingSeq,
    });
  }

  if (guard >= maxIterations) throw new Error('Settlement did not converge');

  const destinations = ballots.map((ballot) => {
    const trail = trails.get(ballot.id);
    if (usedBallotIds.has(ballot.id)) {
      const appointmentIndex = usedAppointmentIndexByBallot.get(ballot.id);
      const usedEntry = [...trail].reverse().find((entry) => entry.result === 'used');
      const appointment = appointments[appointmentIndex];
      return {
        ballotId: ballot.id,
        voterId: ballot.voterId,
        seq: ballot.seq,
        status: 'used',
        personId: appointment.personId,
        seatId: appointment.seatId,
        choiceIndex: usedEntry.choiceIndex,
        appointmentIndex,
        trail,
      };
    }

    const pointer = pointerState.get(ballot.id) ?? 0;
    const activeChoice = pointer < (ballot.choices?.length ?? 0) ? ballot.choices[pointer] : null;
    return {
      ballotId: ballot.id,
      voterId: ballot.voterId,
      seq: ballot.seq,
      status: activeChoice ? 'unused_support_below_quota' : 'unused_no_viable_choice',
      activeChoiceIndex: activeChoice ? pointer : null,
      personId: activeChoice ? choicePersonId(activeChoice) : null,
      seatId: activeChoice?.seatId ?? null,
      trail,
    };
  });

  return { rule: PUBLIC_RULE, appointments, destinations, steps };
}

/**
 * Audit against persisted issued letters. This is read-only and can be called
 * after restart; appointment.ballotIds are the source of truth for consumed.
 */
export function auditBallots(round) {
  const ballots = [...(round.ballots ?? [])].sort(byTicketOrder);
  const appointments = round.appointments ?? [];
  const context = makeContext(appointments);
  const usedBallotToAppointment = new Map();
  for (const appointment of appointments) {
    for (const ballotId of appointment.ballotIds ?? []) {
      usedBallotToAppointment.set(ballotId, appointment);
    }
  }

  return ballots.map((ballot) => {
    const usedAppointment = usedBallotToAppointment.get(ballot.id);
    if (usedAppointment) {
      const storedUsage = usedAppointment.ballotUsages?.find((usage) => usage.ballotId === ballot.id);
      return {
        ballotId: ballot.id,
        voterId: ballot.voterId,
        seq: ballot.seq,
        status: 'used',
        personId: usedAppointment.personId,
        seatId: usedAppointment.seatId,
        choiceIndex: storedUsage?.choiceIndex ?? null,
        appointmentId: usedAppointment.id,
        receiptCode: usedAppointment.receiptCode,
        trail: storedUsage?.trail ?? [{ result: 'used', reason: 'persisted_appointment' }],
      };
    }

    const trail = [];
    const choices = ballot.choices ?? [];
    let active = null;

    for (let index = 0; index < choices.length; index += 1) {
      const choice = choices[index];
      const baseClassification = classifyChoice(round, choice, context);

      // An unused ballot pointing at the actual officeholder pair lost the
      // deterministic over-quota ticket competition.
      const pairAppointment = appointments.find(
        (appointment) =>
          appointment.status === 'issued' &&
          appointment.personId === choicePersonId(choice) &&
          appointment.seatId === choice.seatId,
      );

      if (pairAppointment && !(pairAppointment.ballotIds ?? []).includes(ballot.id)) {
        trail.push({
          choiceIndex: index,
          personId: choicePersonId(choice),
          seatId: choice.seatId,
          result: 'skipped',
          reason: 'quota_filled_by_earlier_tickets',
          appointmentId: pairAppointment.id,
          selectedBallotIds: pairAppointment.ballotIds,
        });
        continue;
      }

      if (baseClassification.available) {
        active = { index, choice, classification: baseClassification };
        break;
      }

      trail.push({
        choiceIndex: index,
        personId: choicePersonId(choice),
        seatId: choice.seatId,
        result: 'skipped',
        reason: baseClassification.reason,
        existingAppointmentId: baseClassification.existingAppointmentId,
      });
    }

    return {
      ballotId: ballot.id,
      voterId: ballot.voterId,
      seq: ballot.seq,
      status: active ? 'unused_support_below_quota' : 'unused_no_viable_choice',
      activeChoiceIndex: active?.index ?? null,
      personId: active ? choicePersonId(active.choice) : null,
      seatId: active?.choice?.seatId ?? null,
      trail,
    };
  });
}

/**
 * Find currently supportable ballots for a manual desk issuance. The ticket
 * rule is enforced here too: callers cannot choose arbitrary friends.
 */
export function supportForPair(round, personId, seatId) {
  const appointments = round.appointments ?? [];
  const context = makeContext(appointments);
  const usedBallotIds = new Set(appointments.flatMap((appointment) => appointment.ballotIds ?? []));
  const support = [];
  const previews = [];

  for (const ballot of [...(round.ballots ?? [])].sort(byTicketOrder)) {
    if (usedBallotIds.has(ballot.id)) continue;
    const trail = [];
    let active = null;

    for (let index = 0; index < (ballot.choices ?? []).length; index += 1) {
      const choice = ballot.choices[index];
      const classification = classifyChoice(round, choice, context);
      if (classification.available) {
        active = { index, choice };
        break;
      }
      trail.push({
        choiceIndex: index,
        personId: choicePersonId(choice),
        seatId: choice.seatId,
        result: 'skipped',
        reason: classification.reason,
      });
    }

    if (active && choicePersonId(active.choice) === personId && active.choice.seatId === seatId) {
      support.push({ ballot, choiceIndex: active.index, trail });
    }
    previews.push({ ballotId: ballot.id, active, trail });
  }

  support.sort((a, b) => byTicketOrder(a.ballot, b.ballot));
  return support;
}

export function dutyRoster(round) {
  const seats = round.seats ?? DEFAULT_SEATS;
  const candidates = candidateMap(round);
  const appointments = round.appointments ?? [];
  const audit = auditBallots(round);

  return seats.map((seat) => {
    const appointment = appointments.find((item) => item.seatId === seat.id && item.status === 'issued');
    if (!appointment) {
      const supporters = audit.filter((item) => item.seatId === seat.id && item.status === 'unused_support_below_quota');
      return {
        seatId: seat.id,
        seatName: seat.name,
        quota: seat.quota,
        status: 'vacant',
        officeholder: null,
        appointmentId: null,
        receiptCode: null,
        visibleNomineeSupport: supporters.map((item) => ({
          personId: item.personId,
          ballotId: item.ballotId,
          note: '仅为支持/提名，未实际用票就任，席位仍空',
        })),
      };
    }

    const candidate = candidates.get(appointment.personId);
    return {
      seatId: seat.id,
      seatName: seat.name,
      quota: seat.quota,
      status: 'filled',
      officeholder: { id: appointment.personId, name: candidate?.name ?? appointment.personId },
      appointmentId: appointment.id,
      settlementId: appointment.settlementId ?? null,
      receiptCode: appointment.receiptCode,
      ballotIds: appointment.ballotIds,
      issuedAt: appointment.issuedAt,
    };
  });
}

export function validateBallot(round, input) {
  if (!input.voterId) throw new DomainError(400, 'VOTER_ID_REQUIRED', '每张票必须有 voterId');
  if ((input.choices ?? []).length === 0) {
    throw new DomainError(400, 'CHOICES_REQUIRED', '排序票至少要写一个“人＋席”组合；允许只列部分组合');
  }

  const seats = seatMap(round);
  const candidates = candidateMap(round);
  const seen = new Set();
  const choices = input.choices.map((rawChoice, index) => {
    const personId = rawChoice.personId ?? rawChoice.candidateId;
    const seatId = rawChoice.seatId;
    if (!personId || !seatId) {
      throw new DomainError(400, 'INVALID_CHOICE', `第 ${index + 1} 项必须同时包含 personId/candidateId 和 seatId`);
    }
    if (!seats.has(seatId)) throw new DomainError(400, 'UNKNOWN_SEAT', `未知席位 ${seatId}`);
    if (!candidates.has(personId)) throw new DomainError(400, 'UNKNOWN_CANDIDATE', `未知人员 ${personId}`);
    const key = `${personId}::${seatId}`;
    if (seen.has(key)) throw new DomainError(400, 'DUPLICATE_CHOICE', `排序票中重复组合 ${key}`);
    seen.add(key);
    return { personId, seatId };
  });

  const duplicateVoter = (round.ballots ?? []).some((ballot) => ballot.voterId === input.voterId);
  if (duplicateVoter) {
    throw new DomainError(409, 'BALLOT_ALREADY_SUBMITTED', `成员 ${input.voterId} 已交过一张排序票`);
  }
  if (input.id && (round.ballots ?? []).some((ballot) => ballot.id === input.id)) {
    throw new DomainError(409, 'BALLOT_ID_EXISTS', `票号 ${input.id} 已存在`);
  }
  return choices;
}

export function newReceiptCode() {
  return `RCPT-${randomUUID().replace(/-/g, '').slice(0, 12).toUpperCase()}`;
}
