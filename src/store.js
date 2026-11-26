import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import {
  DEFAULT_SEATS,
  DomainError,
  auditBallots,
  candidateMap,
  classifyChoice,
  computeSettlement,
  dutyRoster,
  newReceiptCode,
  supportForPair,
  validateBallot,
} from './election.js';

export class Store {
  constructor(file = process.env.DATA_FILE || resolve('data/election-db.json')) {
    this.file = resolve(file);
    this.state = this.#load();
  }

  #defaultState() {
    return {
      schemaVersion: 1,
      createdAt: new Date().toISOString(),
      rounds: {},
      idempotency: {},
    };
  }

  #load() {
    try {
      if (!existsSync(this.file)) return this.#defaultState();
      const parsed = JSON.parse(readFileSync(this.file, 'utf8'));
      if (!parsed.rounds) parsed.rounds = {};
      if (!parsed.idempotency) parsed.idempotency = {};
      return parsed;
    } catch (error) {
      throw new Error(`无法读取持久化数据 ${this.file}: ${error.message}`);
    }
  }

  save() {
    mkdirSync(dirname(this.file), { recursive: true });
    const tmp = `${this.file}.${process.pid}.${randomUUID()}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.state, null, 2), { encoding: 'utf8', mode: 0o600 });
    renameSync(tmp, this.file);
  }

  /**
   * Run one synchronous command and persist state + idempotency record in a
   * single atomic rename. Node's event loop cannot interleave the sync block,
   * so concurrent local HTTP desk commands cannot both win the same seat.
   */
  transact({ key = null, fingerprint = null } = {}, operation) {
    if (key && this.state.idempotency[key]) {
      const record = this.state.idempotency[key];
      if (fingerprint && record.fingerprint !== fingerprint) {
        throw new DomainError(422, 'IDEMPOTENCY_KEY_REUSED', '同一 Idempotency-Key 不能用于不同请求');
      }
      return { replay: true, status: record.response.status, body: record.response.body, recordedAt: record.createdAt };
    }

    const snapshot = structuredClone(this.state);
    let result;
    try {
      result = operation(this.state);
    } catch (error) {
      // Keep failed synchronous commands atomic in memory as well as on disk.
      this.state = snapshot;
      throw error;
    }
    const status = result?.status ?? 200;
    const body = result?.body ?? result;

    if (key) {
      this.state.idempotency[key] = {
        fingerprint,
        // Detach the stored response from later in-memory state mutations.
        response: { status, body: structuredClone(body) },
        createdAt: new Date().toISOString(),
      };
    }
    this.save();
    return { replay: false, status, body };
  }

  listRounds() {
    return Object.values(this.state.rounds).sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  }

  getRound(roundId) {
    const round = this.state.rounds[roundId];
    if (!round) throw new DomainError(404, 'ROUND_NOT_FOUND', `选举轮次 ${roundId} 不存在`);
    return round;
  }

  createRound(input = {}) {
    const id = input.id ?? `round_${randomUUID()}`;
    if (this.state.rounds[id]) throw new DomainError(409, 'ROUND_ID_EXISTS', `轮次 ${id} 已存在`);

    const seats = input.seats?.length
      ? input.seats.map((seat, index) => ({
          id: seat.id,
          name: seat.name ?? seat.id,
          quota: Number.isInteger(seat.quota) && seat.quota > 0 ? seat.quota : 2,
          order: index,
        }))
      : DEFAULT_SEATS.map((seat, index) => ({ ...seat, order: index }));

    const candidates = {};
    for (const candidate of input.candidates ?? []) {
      if (!candidate.id) throw new DomainError(400, 'CANDIDATE_ID_REQUIRED', '候选人必须有 id');
      candidates[candidate.id] = normalizeCandidate(candidate);
    }

    const round = {
      id,
      name: input.name ?? id,
      status: 'open',
      ruleVersion: 'joint-v1',
      seats,
      candidates,
      ballots: [],
      appointments: [],
      settlement: null,
      createdAt: new Date().toISOString(),
    };
    this.state.rounds[id] = round;
    return { status: 201, body: round };
  }

  putCandidate(roundId, input = {}) {
    const round = this.getRound(roundId);
    if (!input.id) throw new DomainError(400, 'CANDIDATE_ID_REQUIRED', '候选人必须有 id');
    const existing = round.candidates[input.id] ?? { id: input.id };
    round.candidates[input.id] = normalizeCandidate({ ...existing, ...input });
    return { status: 200, body: round.candidates[input.id] };
  }

  patchCommitment(roundId, candidateId, patch = {}) {
    const round = this.getRound(roundId);
    const candidate = round.candidates[candidateId];
    if (!candidate) throw new DomainError(404, 'CANDIDATE_NOT_FOUND', `人员 ${candidateId} 不存在`);

    const commitment = { ...(candidate.commitment ?? {}) };
    if (patch.willingSeatIds) commitment.willingSeatIds = [...patch.willingSeatIds];
    if (patch.preferredSeatId !== undefined) commitment.preferredSeatId = patch.preferredSeatId;
    if (patch.seatResponses) {
      commitment.seatResponses = { ...(commitment.seatResponses ?? {}), ...patch.seatResponses };
    }
    if (patch.refusedSeatIds) {
      commitment.seatResponses = { ...(commitment.seatResponses ?? {}) };
      for (const seatId of patch.refusedSeatIds) commitment.seatResponses[seatId] = 'refused';
    }
    if (patch.acceptedSeatIds) {
      commitment.seatResponses = { ...(commitment.seatResponses ?? {}) };
      for (const seatId of patch.acceptedSeatIds) commitment.seatResponses[seatId] = 'accepted';
    }
    candidate.commitment = commitment;
    return { status: 200, body: candidate };
  }

  addBallot(roundId, input = {}) {
    const round = this.getRound(roundId);
    const choices = validateBallot(round, input);
    const seq = Math.max(0, ...round.ballots.map((ballot) => ballot.seq ?? 0)) + 1;
    const ballot = {
      id: input.id ?? `ballot_${randomUUID()}`,
      voterId: input.voterId,
      voterName: input.voterName ?? null,
      seq,
      submittedAt: input.submittedAt ?? new Date().toISOString(),
      choices,
    };
    round.ballots.push(ballot);
    round.ballots.sort((a, b) => a.seq - b.seq);
    return { status: 201, body: ballot };
  }

  settleRound(roundId, options = {}) {
    const round = this.getRound(roundId);
    if (round.settlement) {
      return { status: 200, body: { settlement: round.settlement, replayed: true, alreadySettled: true } };
    }
    if (round.appointments?.some((appointment) => appointment.status === 'issued')) {
      throw new DomainError(
        409,
        'ROUND_HAS_DESK_APPOINTMENTS',
        '本轮已有柜员逐张发出的就任书；请继续使用 appointments 接口发证，不能再运行会另选一套 slate 的联合结算',
        { appointmentIds: round.appointments.map((appointment) => appointment.id) },
      );
    }

    const result = computeSettlement(round);
    const settlementId = options.settlementId ?? `stlm_${randomUUID()}`;
    const issuedAt = new Date().toISOString();

    const appointments = result.appointments.map((draft) => {
      const id = `apt_${randomUUID()}`;
      const receiptCode = newReceiptCode();
      return {
        ...draft,
        appointmentIndex: undefined,
        id,
        settlementId,
        status: 'issued',
        receiptCode,
        issuedAt,
        idempotencyKey: options.idempotencyKey ?? null,
      };
    });

    const indexToIds = new Map(
      appointments.map((appointment, index) => [index, { id: appointment.id, receiptCode: appointment.receiptCode }]),
    );
    const destinations = result.destinations.map((destination) => {
      if (destination.appointmentIndex === null || destination.appointmentIndex === undefined) return destination;
      const ids = indexToIds.get(destination.appointmentIndex);
      return {
        ...destination,
        appointmentId: ids.id,
        receiptCode: ids.receiptCode,
        appointmentIndex: undefined,
      };
    });

    round.appointments.push(...appointments);
    round.status = 'settled';
    round.settlement = {
      id: settlementId,
      roundId,
      status: 'issued',
      rule: result.rule,
      createdAt: issuedAt,
      appointments,
      destinations,
      steps: result.steps,
    };

    return {
      status: 201,
      body: { settlement: round.settlement, dutyRoster: dutyRoster(round) },
    };
  }

  issueAppointment(roundId, input = {}) {
    const round = this.getRound(roundId);
    if (round.settlement) {
      throw new DomainError(409, 'ROUND_ALREADY_SETTLED', '本轮已完成联选结算；请查询既有就任书或回执', {
        settlementId: round.settlement.id,
      });
    }

    const seat = round.seats.find((item) => item.id === input.seatId);
    if (!seat) throw new DomainError(404, 'SEAT_NOT_FOUND', `未知席位 ${input.seatId}`);

    const personId = input.personId ?? input.candidateId;
    if (!personId) throw new DomainError(400, 'PERSON_ID_REQUIRED', '发证必须指定 personId');
    const candidate = candidateMap(round).get(personId);
    if (!candidate) throw new DomainError(404, 'CANDIDATE_NOT_FOUND', `未知人员 ${personId}`);

    const existingSeat = round.appointments.find((item) => item.seatId === seat.id && item.status === 'issued');
    if (existingSeat) {
      throw new DomainError(409, 'SEAT_ALREADY_FILLED', `${seat.name}席已有真实就任书`, {
        existingAppointment: existingSeat,
      });
    }
    const existingPerson = round.appointments.find((item) => item.personId === personId && item.status === 'issued');
    if (existingPerson) {
      throw new DomainError(409, 'PERSON_ALREADY_SEATED', '同一人只能担任一席', {
        existingAppointment: existingPerson,
      });
    }

    const context = {
      seatedPersonIds: new Set(round.appointments.map((item) => item.personId)),
      filledSeatIds: new Set(round.appointments.map((item) => item.seatId)),
      personAppointmentIds: new Map(round.appointments.map((item) => [item.personId, item.id])),
      seatAppointmentIds: new Map(round.appointments.map((item) => [item.seatId, item.id])),
    };
    const classification = classifyChoice(round, { personId, seatId: seat.id }, context);
    if (!classification.available) {
      throw new DomainError(422, 'CANDIDATE_CANNOT_SERVE', '该人＋席组合当前不能就任', classification);
    }

    const support = supportForPair(round, personId, seat.id);
    if (support.length < seat.quota) {
      throw new DomainError(422, 'QUOTA_NOT_MET', `定额 ${seat.quota} 票，当前有效支持 ${support.length} 票；提名不等于就任`, {
        quota: seat.quota,
        supportCount: support.length,
        supportBallotIds: support.map((item) => item.ballot.id),
      });
    }

    const ruleSelected = support.slice(0, seat.quota);
    let selected = ruleSelected;
    if (Array.isArray(input.ballotIds) && input.ballotIds.length > 0) {
      const requested = new Set(input.ballotIds);
      const activeIds = new Set(support.map((item) => item.ballot.id));
      for (const ballotId of requested) {
        if (!activeIds.has(ballotId)) {
          throw new DomainError(422, 'BALLOT_NOT_ACTIVE_SUPPORT', `票 ${ballotId} 不是该人＋席当前可用的支持票`);
        }
      }
      if (requested.size !== seat.quota || input.ballotIds.length !== seat.quota) {
        throw new DomainError(422, 'EXACT_QUOTA_REQUIRED', `就任必须实际消耗恰好 ${seat.quota} 张票`);
      }
      const selectedIds = new Set(ruleSelected.map((item) => item.ballot.id));
      if ([...requested].some((id) => !selectedIds.has(id)) || requested.size !== selectedIds.size) {
        throw new DomainError(422, 'TICKET_ORDER_RULE_CONFLICT', '多于定额时必须使用公开取票规则选出的最早两张', {
          selectedBallotIds: ruleSelected.map((item) => item.ballot.id),
        });
      }
      selected = support.filter((item) => requested.has(item.ballot.id));
    }

    const appointment = {
      id: `apt_${randomUUID()}`,
      roundId,
      seatId: seat.id,
      personId,
      quota: seat.quota,
      ballotIds: selected.map((item) => item.ballot.id),
      ballotUsages: selected.map((item) => ({
        ballotId: item.ballot.id,
        choiceIndex: item.choiceIndex,
        trail: [
          ...item.trail,
          {
            choiceIndex: item.choiceIndex,
            personId,
            seatId: seat.id,
            result: 'used',
            reason: 'quota_selected_by_ticket_order',
          },
        ],
      })),
      selectionRule: 'joint-v1',
      status: 'issued',
      receiptCode: newReceiptCode(),
      issuedAt: new Date().toISOString(),
      idempotencyKey: input.idempotencyKey ?? null,
    };

    round.appointments.push(appointment);
    return { status: 201, body: appointment };
  }

  findAppointment(appointmentId) {
    for (const round of Object.values(this.state.rounds)) {
      const appointment = round.appointments?.find((item) => item.id === appointmentId);
      if (appointment) return { round, appointment };
    }
    throw new DomainError(404, 'APPOINTMENT_NOT_FOUND', `就任书 ${appointmentId} 不存在`);
  }

  findReceipt(code) {
    for (const round of Object.values(this.state.rounds)) {
      const appointment = round.appointments?.find((item) => item.receiptCode === code);
      if (appointment) return { round, appointment };
    }
    throw new DomainError(404, 'RECEIPT_NOT_FOUND', `回执 ${code} 不存在`);
  }
}

function normalizeCandidate(input) {
  if (!input.id) throw new DomainError(400, 'CANDIDATE_ID_REQUIRED', '候选人必须有 id');
  return {
    id: input.id,
    name: input.name ?? input.id,
    qualifiedSeatIds: [...new Set(input.qualifiedSeatIds ?? input.qualifications ?? [])],
    commitment: {
      willingSeatIds: input.commitment?.willingSeatIds ?? [],
      preferredSeatId: input.commitment?.preferredSeatId ?? null,
      seatResponses: input.commitment?.seatResponses ?? {},
      ...input.commitment,
    },
  };
}
