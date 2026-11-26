'use strict';

/**
 * Dance party back office — event-sourced domain engine.
 *
 * OPERATING MODE: "每轮关轮后再公布下一支" (round-by-round / dynamic, mode B).
 * A round's invitations are proposed and confirmed by phones; when the round
 * closes the engine decides which confirmed invitations actually became a
 * dance (both present + zone open + rhythm still accepted). The next round is
 * then planned from what actually happened and who agreed to dance again.
 *
 * NEW-PARTNER RULE: a guest's "distinct partners" are counted by the partner's
 * attendee number only — role is irrelevant. So if 乙 changes from follow to
 * lead and dances 甲 a second time, 甲 still has met only ONE partner (乙).
 * The role change is preserved in the meeting record as a distinguishable,
 * repeat (role-swapped) visit, but it never increments the partner count.
 *
 * All commands are validated against current state, turned into one or more
 * events, durably appended, then folded into state. Replaying the log rebuilds
 * exactly the same view, so `metPartners(number)` after a restart returns the
 * people the guest really met that night.
 */

const { EventStore } = require('./store');

const LEAD = 'lead';
const FOLLOW = 'follow';
const VALID_ROLES = new Set([LEAD, FOLLOW]);

// Invitation lifecycle:
//   proposed -> confirmed -> fulfilled
//                       \-> absent | zoneClosed | invalidRhythm | conflict
//   proposed -> declined | cancelled | expired | conflict
//   proposed/confirmed for a closed round that never confirmed -> expired
//   outcome.status is the terminal status set at round close.
const TERMINAL = new Set([
  'fulfilled',
  'absent',
  'zoneClosed',
  'invalidRhythm',
  'conflict',
  'declined',
  'cancelled',
  'expired',
]);

class DomainError extends Error {
  constructor(code, message, detail) {
    super(message || code);
    this.name = 'DomainError';
    this.code = code;
    this.detail = detail;
  }
}

function emptyState() {
  return { sessions: {} };
}

function isPresent(guest, round) {
  return !(guest.absentRounds && guest.absentRounds[round]);
}

/**
 * Does the guest accept a dance of `rhythm` in `round`?
 * A per-round consent override wins over the guest's registered rhythms.
 */
function acceptsRhythm(guest, round, rhythm) {
  const ov = guest.consent && guest.consent[round];
  if (ov && Object.prototype.hasOwnProperty.call(ov, rhythm)) return !!ov[rhythm];
  return (guest.rhythms || []).includes(rhythm);
}

function roleComboFor(a, b) {
  // Prefer a leading; fall back to b leading. Returns {aRole,bRole} or null.
  const combos = [
    { aRole: LEAD, bRole: FOLLOW },
    { aRole: FOLLOW, bRole: LEAD },
  ];
  for (const c of combos) {
    if (a.roles.includes(c.aRole) && b.roles.includes(c.bRole)) return c;
  }
  return null;
}

class Engine {
  constructor({ filePath } = {}) {
    this.store = filePath ? new EventStore(filePath) : null;
    this.__memSeq = 0;
    this.state = emptyState();
    if (this.store) {
      for (const ev of this.store.load()) this.#apply(ev);
    }
  }

  // ---------------------------------------------------------------------------
  // Command dispatch
  // ---------------------------------------------------------------------------
  #emit(event) {
    let record;
    if (this.store) {
      record = this.store.append(event);
    } else {
      record = { ...event, seq: ++this.__memSeq, ts: new Date().toISOString() };
    }
    this.#apply(record);
    return record;
  }

  getState() {
    return this.state;
  }

  // ---------------------------------------------------------------------------
  // Session / guest setup
  // ---------------------------------------------------------------------------
  createSession({ sessionId, rounds, zones }) {
    if (!sessionId) throw new DomainError('BAD_INPUT', 'sessionId required');
    if (this.state.sessions[sessionId]) {
      throw new DomainError('SESSION_EXISTS', `session ${sessionId} exists`);
    }
    const roundList = normaliseRounds(rounds);
    if (roundList.length < 1) throw new DomainError('BAD_INPUT', 'need >=1 round');
    const zoneList = normaliseZones(zones);
    if (zoneList.length < 1) throw new DomainError('BAD_INPUT', 'need >=1 zone');
    const ev = this.#emit({
      type: 'SESSION_CREATED',
      sessionId,
      rounds: roundList,
      zones: zoneList,
    });
    return this.state.sessions[sessionId];
  }

  registerGuest(sessionId, { number, name, roles, rhythms, targetPartners, roundsPurchased }) {
    const s = this.#session(sessionId);
    if (number === undefined || number === null || number === '') {
      throw new DomainError('BAD_INPUT', 'guest number required');
    }
    if (s.guests[number]) throw new DomainError('GUEST_EXISTS', `guest ${number} exists`);
    const roleList = normaliseRoles(roles);
    const rhythmList = Array.isArray(rhythms) ? rhythms.slice() : s.rounds.map((r) => r.rhythm);
    const target = targetPartners === undefined ? 0 : toInt(targetPartners, 'targetPartners');
    if (target < 0) throw new DomainError('BAD_INPUT', 'targetPartners must be >= 0');
    const purchased =
      roundsPurchased === undefined ? s.rounds.length : toInt(roundsPurchased, 'roundsPurchased');
    if (purchased < 1 || purchased > s.rounds.length) {
      throw new DomainError('BAD_INPUT', `roundsPurchased must be 1..${s.rounds.length}`);
    }
    this.#emit({
      type: 'GUEST_REGISTERED',
      sessionId,
      number,
      name: name || String(number),
      roles: roleList,
      rhythms: rhythmList,
      targetPartners: target,
      roundsPurchased: purchased,
    });
    return s.guests[number];
  }

  /** Mark a guest as resting / away (away=true) or back (away=false) for a round. */
  setRest(sessionId, { number, round, away }) {
    const s = this.#session(sessionId);
    this.#guest(s, number);
    this.#roundIndex(s, round);
    this.#emit({
      type: 'GUEST_REST_SET',
      sessionId,
      number,
      round,
      away: !!away,
    });
    return { number, round, away: !!away };
  }

  setZone(sessionId, { zone, open }) {
    const s = this.#session(sessionId);
    if (!s.zones[zone]) throw new DomainError('NO_ZONE', `zone ${zone} not found`);
    this.#emit({ type: 'ZONE_SET', sessionId, zone, open: !!open });
    return s.zones[zone];
  }

  /** Set (or clear) a guest's per-round rhythm permission override. */
  setConsent(sessionId, { number, round, rhythm, accept }) {
    const s = this.#session(sessionId);
    this.#guest(s, number);
    this.#roundIndex(s, round);
    if (!rhythm) throw new DomainError('BAD_INPUT', 'rhythm required');
    this.#emit({
      type: 'RHYTHM_CONSENT_SET',
      sessionId,
      number,
      round,
      rhythm,
      accept: !!accept,
    });
    return { number, round, rhythm, accept: !!accept };
  }

  // ---------------------------------------------------------------------------
  // Invitations
  // ---------------------------------------------------------------------------
  proposeInvitation(
    sessionId,
    { round, from, to, fromRole, toRole, zone, source }
  ) {
    const s = this.#session(sessionId);
    this.#roundIndex(s, round);
    if (this.#isClosed(s, round)) {
      throw new DomainError('ROUND_CLOSED', `round ${round} already closed`);
    }
    const a = this.#guest(s, from);
    const b = this.#guest(s, to);
    if (from === to) throw new DomainError('SELF_INVITE', 'cannot invite yourself');
    if (!s.zones[zone]) throw new DomainError('NO_ZONE', `zone ${zone} not found`);
    if (!VALID_ROLES.has(fromRole) || !VALID_ROLES.has(toRole)) {
      throw new DomainError('BAD_ROLE', 'roles must be lead/follow');
    }
    // A guest only bought encounters within their purchased rounds.
    if (round > a.roundsPurchased) {
      throw new DomainError('OUT_OF_PURCHASE', `${from} did not buy round ${round}`);
    }
    if (round > b.roundsPurchased) {
      throw new DomainError('OUT_OF_PURCHASE', `${to} did not buy round ${round}`);
    }
    if (fromRole === toRole) {
      throw new DomainError('ROLE_CLASH', 'one partner must lead and the other follow');
    }
    if (!a.roles.includes(fromRole)) {
      throw new DomainError('ROLE_CAPABILITY', `${from} cannot ${fromRole}`);
    }
    if (!b.roles.includes(toRole)) {
      throw new DomainError('ROLE_CAPABILITY', `${to} cannot ${toRole}`);
    }
    const id = this.#nextInvitationId(s);
    const rhythm = s.rounds[round - 1].rhythm;
    this.#emit({
      type: 'INVITATION_PROPOSED',
      invitationId: id,
      sessionId,
      round,
      from,
      to,
      fromRole,
      toRole,
      zone,
      rhythm,
      source: source || 'request',
    });
    return s.invitations[id];
  }

  /**
   * The invited guest agrees on their phone. The proposer's consent is implicit.
   * If either party is already confirmed in another invitation for the same
   * round, this confirmation loses and a CONFLICT is recorded (the earlier
   * confirmation is the "truly effective side").
   */
  confirmInvitation(sessionId, { invitationId, by }) {
    const s = this.#session(sessionId);
    const inv = s.invitations[invitationId];
    if (!inv) throw new DomainError('NO_INVITATION', `invitation ${invitationId} not found`);
    if (inv.status !== 'proposed') {
      throw new DomainError('NOT_CONFIRMABLE', `invitation is ${inv.status}`);
    }
    if (this.#isClosed(s, inv.round)) {
      throw new DomainError('ROUND_CLOSED', `round ${inv.round} closed`);
    }
    if (by !== inv.to) {
      throw new DomainError('WRONG_CONFIRMER', `only the invited guest (${inv.to}) may confirm`);
    }
    const clash = this.#confirmedClash(s, inv);
    if (clash) {
      this.#emit({
        type: 'INVITATION_CONFLICT',
        invitationId,
        sessionId,
        conflictWith: clash.invitationId,
        reason: `${clash.party} already confirmed in ${clash.invitationId}`,
      });
      return { ok: false, code: 'CONFLICT', conflictWith: clash.invitationId, invitation: s.invitations[invitationId] };
    }
    this.#emit({ type: 'INVITATION_CONFIRMED', invitationId, sessionId, by });
    return { ok: true, invitation: s.invitations[invitationId] };
  }

  declineInvitation(sessionId, { invitationId, by }) {
    const s = this.#session(sessionId);
    const inv = s.invitations[invitationId];
    if (!inv) throw new DomainError('NO_INVITATION', `invitation ${invitationId} not found`);
    if (inv.status !== 'proposed') {
      throw new DomainError('NOT_DECLINABLE', `invitation is ${inv.status}`);
    }
    if (by !== inv.to && by !== inv.from) {
      throw new DomainError('WRONG_DECLINER', 'not a party to this invitation');
    }
    this.#emit({ type: 'INVITATION_DECLINED', invitationId, sessionId, by });
    return s.invitations[invitationId];
  }

  cancelInvitation(sessionId, { invitationId, reason }) {
    const s = this.#session(sessionId);
    const inv = s.invitations[invitationId];
    if (!inv) throw new DomainError('NO_INVITATION', `invitation ${invitationId} not found`);
    if (TERMINAL.has(inv.status)) {
      throw new DomainError('NOT_CANCELLABLE', `invitation is ${inv.status}`);
    }
    this.#emit({
      type: 'INVITATION_CANCELLED',
      invitationId,
      sessionId,
      reason: reason || 'cancelled',
    });
    return s.invitations[invitationId];
  }

  // ---------------------------------------------------------------------------
  // Round close — the heart of mode B
  // ---------------------------------------------------------------------------
  closeRound(sessionId, { round }) {
    const s = this.#session(sessionId);
    this.#roundIndex(s, round);
    const expected = s.closedRounds.length + 1;
    if (round !== expected) {
      throw new DomainError('ROUND_ORDER', `expected to close round ${expected}, got ${round}`);
    }
    const confirmed = Object.values(s.invitations)
      .filter((i) => i.round === round && i.status === 'confirmed')
      .sort((x, y) => x.confirmedSeq - y.confirmedSeq);

    const usedGuests = new Set();
    const usedZones = new Set();
    const outcomes = [];
    const dances = [];

    for (const inv of confirmed) {
      let status = 'fulfilled';
      let reason = null;
      let absentBy = null;

      // one effective dance per guest per round
      if (usedGuests.has(inv.from) || usedGuests.has(inv.to)) {
        status = 'conflict';
        reason = 'guest already dancing this round';
      } else if (usedZones.has(inv.zone)) {
        status = 'conflict';
        reason = 'zone already hosting a dance this round';
      } else if (!s.zones[inv.zone].open) {
        status = 'zoneClosed';
        reason = `zone ${inv.zone} was not open`;
      } else if (!isPresent(s.guests[inv.from], round) || !isPresent(s.guests[inv.to], round)) {
        status = 'absent';
        absentBy = !isPresent(s.guests[inv.from], round) ? inv.from : inv.to;
        reason = `${absentBy} did not arrive`;
      } else if (
        !acceptsRhythm(s.guests[inv.from], round, inv.rhythm) ||
        !acceptsRhythm(s.guests[inv.to], round, inv.rhythm)
      ) {
        status = 'invalidRhythm';
        reason = `${!acceptsRhythm(s.guests[inv.from], round, inv.rhythm) ? inv.from : inv.to} does not accept ${inv.rhythm}`;
      }

      if (status === 'fulfilled') {
        usedGuests.add(inv.from);
        usedGuests.add(inv.to);
        usedZones.add(inv.zone);
        const danceId = `dance_${s.id}_${Object.keys(s.dances).length + dances.length + 1}`;
        dances.push({
          id: danceId,
          round,
          zone: inv.zone,
          a: inv.from,
          b: inv.to,
          aRole: inv.fromRole,
          bRole: inv.toRole,
          invitationId: inv.id,
          source: inv.source,
        });
        outcomes.push({ invitationId: inv.id, status, danceId });
      } else {
        outcomes.push({ invitationId: inv.id, status, reason, absentBy });
      }
    }

    // Invitations that never got confirmed before the round closed are expired.
    for (const inv of Object.values(s.invitations)) {
      if (inv.round === round && inv.status === 'proposed') {
        outcomes.push({ invitationId: inv.id, status: 'expired', reason: 'not confirmed before close' });
      }
    }

    this.#emit({ type: 'ROUND_CLOSED', sessionId, round, outcomes, dances });

    return {
      round,
      outcomes: outcomes.map((o) => ({ ...o })),
      dances,
      advice: Object.keys(s.guests).map((num) => ({ number: num, ...this.quota(sessionId, num) })),
    };
  }

  // ---------------------------------------------------------------------------
  // Queries
  // ---------------------------------------------------------------------------
  /**
   * Full meeting record for a guest, distinguishing:
   *   - actual encounters (by attendee number, role-blind)
   *   - repeat / role-swapped re-dances with the same partner
   *   - program-book ("节目单") partners that are still only scheduled
   *   - invitations where the partner never arrived (中途未到)
   */
  meetingRecord(sessionId, number) {
    const s = this.#session(sessionId);
    const guest = this.#guest(s, number);

    const danceList = Object.values(s.dances)
      .filter((d) => d.a === number || d.b === number)
      .sort((x, y) => x.round - y.round || x.seq - y.seq || cmp(x.id, y.id));

    const partners = new Map(); // partner number -> record
    for (const d of danceList) {
      const partner = d.a === number ? d.b : d.a;
      const myRole = d.a === number ? d.aRole : d.bRole;
      const theirRole = d.a === number ? d.bRole : d.aRole;
      if (!partners.has(partner)) {
        partners.set(partner, {
          number: partner,
          name: s.guests[partner].name,
          dances: [],
          distinct: true,
        });
      }
      const rec = partners.get(partner);
      const prev = rec.dances[rec.dances.length - 1];
      const roleSwapped = !!prev && prev.myRole !== myRole;
      rec.dances.push({
        round: d.round,
        zone: d.zone,
        myRole,
        theirRole,
        roleSwapped,
        repeat: !!prev,
        scheduled: d.source === 'program',
      });
    }

    const invites = Object.values(s.invitations)
      .filter((i) => i.from === number || i.to === number)
      .sort((x, y) => x.round - y.round || x.seq - y.seq);

    const scheduled = invites
      .filter((i) => i.source === 'program' && (i.status === 'proposed' || i.status === 'confirmed'))
      .map((i) => describeInvite(i, number));

    const pending = invites
      .filter((i) => i.status === 'proposed' && i.source !== 'program')
      .map((i) => describeInvite(i, number));

    const noShows = invites
      .filter((i) => i.status === 'absent')
      .map((i) => {
        const partner = i.from === number ? i.to : i.from;
        return {
          round: i.round,
          partner,
          partnerName: s.guests[partner] ? s.guests[partner].name : partner,
          absentBy: i.outcome ? i.outcome.absentBy : null,
          why: 'partner did not arrive (中途未到)',
        };
      });

    const blocked = invites
      .filter((i) => i.status === 'zoneClosed' || i.status === 'invalidRhythm' || i.status === 'conflict')
      .map((i) => ({ round: i.round, status: i.status, ...describeInvite(i, number) }));

    const partnerList = [...partners.values()].sort((x, y) => cmp(x.number, y.number));

    return {
      number,
      name: guest.name,
      roles: guest.roles.slice(),
      targetPartners: guest.targetPartners,
      roundsPurchased: guest.roundsPurchased,
      distinctPartners: partnerList.length,
      partners: partnerList,
      scheduled,
      pending,
      noShows,
      blocked,
      redeemed: !!(s.orders[number] && s.orders[number].redeemed),
    };
  }

  /** Distinct partner attendee numbers actually danced with, sorted. */
  metPartners(sessionId, number) {
    const s = this.#session(sessionId);
    this.#guest(s, number);
    const set = new Set();
    for (const d of Object.values(s.dances)) {
      if (d.a === number) set.add(d.b);
      else if (d.b === number) set.add(d.a);
    }
    return [...set].sort(cmp);
  }

  /**
   * How many more distinct partners can still be delivered for a guest, and
   * what should we tell them (rest now / come back / reschedule).
   */
  quota(sessionId, number) {
    const s = this.#session(sessionId);
    const guest = this.#guest(s, number);
    const met = this.metPartners(sessionId, number);
    const metSet = new Set(met);
    const delivered = met.length;
    const remaining = Math.max(0, guest.targetPartners - delivered);

    const nextRound = s.closedRounds.length + 1;
    const openRounds = [];
    for (let r = nextRound; r <= guest.roundsPurchased && r <= s.rounds.length; r++) {
      if (isPresent(guest, r)) openRounds.push(r);
    }

    // Candidate partners per remaining round: never met, present in the round,
    // role-compatible, both accept that round's rhythm, within their purchase,
    // and at least one zone open somewhere.
    const openZones = Object.values(s.zones).filter((z) => z.open).length;
    const adj = new Map(); // round -> array of candidate partner numbers
    const candidateSet = new Set();
    for (const r of openRounds) {
      if (openZones === 0) {
        adj.set(r, []);
        continue;
      }
      const rhythm = s.rounds[r - 1].rhythm;
      if (!acceptsRhythm(guest, r, rhythm)) {
        adj.set(r, []);
        continue;
      }
      const list = [];
      for (const otherNum of Object.keys(s.guests)) {
        if (otherNum === number || metSet.has(otherNum)) continue;
        const other = s.guests[otherNum];
        if (r > other.roundsPurchased) continue;
        if (!isPresent(other, r)) continue;
        if (!acceptsRhythm(other, r, rhythm)) continue;
        if (!roleComboFor(guest, other)) continue;
        list.push(otherNum);
        candidateSet.add(otherNum);
      }
      adj.set(r, list);
    }

    // The true number of *additional distinct* partners is at most one per
    // remaining round and is bounded by a round<->candidate maximum matching.
    const matchingBound = maxBipartiteMatching(openRounds, adj);
    const maxAdditional = Math.min(remaining, matchingBound);
    const projected = delivered + maxAdditional;
    const canFulfil = projected >= guest.targetPartners;

    let prompt = 'onTrack';
    let message = '';
    if (delivered >= guest.targetPartners) {
      prompt = 'fulfilled';
      message = `已跳足 ${guest.targetPartners} 位不同舞伴，无需再排。`;
    } else if (remaining === 0) {
      prompt = 'fulfilled';
      message = '名额已兑现。';
    } else if (nextRound <= s.rounds.length && !isPresent(guest, nextRound)) {
      prompt = 'rest';
      message = `你第 ${nextRound} 轮登记休息，先歇一轮；回来后仍差 ${remaining} 位。`;
    } else if (!canFulfil) {
      prompt = 'reschedule';
      message = `按当前到场与区域，最多还能遇到 ${maxAdditional} 位，差 ${remaining} 位无法全部兑现，建议征询改约/加轮。`;
    } else {
      prompt = 'onTrack';
      message = `还差 ${remaining} 位，尚有 ${openRounds.length} 个可跳轮次、${candidateSet.size} 位候选舞伴。`;
    }

    return {
      delivered,
      target: guest.targetPartners,
      remaining,
      maxAdditional,
      projected,
      canFulfil,
      openRounds,
      candidateCount: candidateSet.size,
      candidates: [...candidateSet].sort(cmp),
      prompt,
      message,
    };
  }

  /** Which invitation is truly in effect for this guest in this round. */
  effectiveAssignment(sessionId, { number, round }) {
    const s = this.#session(sessionId);
    this.#guest(s, number);
    const dance = Object.values(s.dances).find(
      (d) => d.round === round && (d.a === number || d.b === number)
    );
    if (dance) {
      const partner = dance.a === number ? dance.b : dance.a;
      return { effective: { kind: 'dance', danceId: dance.id, partner, zone: dance.zone }, conflicts: [] };
    }
    const forMe = Object.values(s.invitations).filter(
      (i) => i.round === round && (i.from === number || i.to === number)
    );
    const live = forMe
      .filter((i) => i.status === 'confirmed' || i.status === 'proposed')
      .sort((x, y) => statusRank(x) - statusRank(y) || x.seq - y.seq);
    const conflictIds = forMe.filter((i) => i.status === 'conflict').map((i) => i.id);
    const confirmed = live.find((i) => i.status === 'confirmed');
    if (confirmed) {
      const partner = confirmed.from === number ? confirmed.to : confirmed.from;
      return {
        effective: { kind: 'confirmed-invitation', invitationId: confirmed.id, partner, zone: confirmed.zone },
        conflicts: [
          ...conflictIds,
          ...live.filter((i) => i.status === 'proposed' && i !== confirmed).map((i) => i.id),
        ],
      };
    }
    return { effective: null, conflicts: [...conflictIds, ...live.map((i) => i.id)] };
  }

  /** Export the schedule. READ-ONLY — never redeems an order. */
  exportSchedule(sessionId) {
    const s = this.#session(sessionId);
    const rounds = s.rounds.map((r) => ({
      round: r.index,
      rhythm: r.rhythm,
      closed: this.#isClosed(s, r.index),
      invitations: Object.values(s.invitations)
        .filter((i) => i.round === r.index)
        .sort((a, b) => a.seq - b.seq)
        .map((i) => ({
          id: i.id,
          from: i.from,
          to: i.to,
          fromRole: i.fromRole,
          toRole: i.toRole,
          zone: i.zone,
          source: i.source,
          status: i.status,
        })),
      dances: Object.values(s.dances)
        .filter((d) => d.round === r.index)
        .map((d) => ({ id: d.id, a: d.a, b: d.b, aRole: d.aRole, bRole: d.bRole, zone: d.zone })),
    }));
    return {
      sessionId,
      zones: Object.values(s.zones).map((z) => ({ id: z.id, open: z.open })),
      rounds,
      // deliberately no order mutation here
    };
  }

  /**
   * Redeem a guest's purchase. This is an explicit, separate action and only
   * succeeds once the signed number of distinct partners has really been met.
   * Exporting the schedule never triggers it.
   */
  redeem(sessionId, { number }) {
    const s = this.#session(sessionId);
    const guest = this.#guest(s, number);
    const delivered = this.metPartners(sessionId, number).length;
    if (delivered < guest.targetPartners) {
      throw new DomainError(
        'NOT_FULFILLED',
        `only met ${delivered}/${guest.targetPartners} distinct partners`
      );
    }
    if (s.orders[number] && s.orders[number].redeemed) {
      throw new DomainError('ALREADY_REDEEMED', 'order already redeemed');
    }
    this.#emit({ type: 'ORDER_REDEEMED', sessionId, number });
    return s.orders[number];
  }

  // ---------------------------------------------------------------------------
  // Convenience: dynamic planning (mode B helpers)
  // ---------------------------------------------------------------------------
  /**
   * Plan one round from who is present and what zones are open: greedily pair
   * guests, preferring partners they have not met yet, respecting roles and
   * rhythm permissions. Returns the invitations it created (already confirmed
   * by the invited phones). Does not close the round.
   */
  autoPlanRound(sessionId, { round, source = 'request' } = {}) {
    const s = this.#session(sessionId);
    this.#roundIndex(s, round);
    if (this.#isClosed(s, round)) throw new DomainError('ROUND_CLOSED', `round ${round} closed`);

    const present = Object.keys(s.guests).filter(
      (n) => isPresent(s.guests[n], round) && !this.#hasLiveInvite(s, n, round)
    );
    const freeZones = Object.values(s.zones).filter((z) => z.open).map((z) => z.id);
    const met = new Map();
    for (const n of present) met.set(n, new Set(this.metPartners(sessionId, n)));

    // Build candidate pairs, preferring unmet partners and guests who still need.
    const pairs = [];
    for (let i = 0; i < present.length; i++) {
      for (let j = i + 1; j < present.length; j++) {
        const a = s.guests[present[i]];
        const b = s.guests[present[j]];
        if (!roleComboFor(a, b)) continue;
        if (!acceptsRhythm(a, round, s.rounds[round - 1].rhythm)) continue;
        if (!acceptsRhythm(b, round, s.rounds[round - 1].rhythm)) continue;
        const metAlready = met.get(a.number).has(b.number);
        pairs.push({ a: a.number, b: b.number, metAlready });
      }
    }
    pairs.sort((x, y) => Number(x.metAlready) - Number(y.metAlready) || cmp(x.a, y.a) || cmp(x.b, y.b));

    const taken = new Set();
    const created = [];
    for (const p of pairs) {
      if (taken.has(p.a) || taken.has(p.b)) continue;
      if (freeZones.length === 0) break;
      const a = s.guests[p.a];
      const b = s.guests[p.b];
      const combo = roleComboFor(a, b);
      if (!combo) continue;
      const zone = freeZones.shift();
      const inv = this.proposeInvitation(sessionId, {
        round,
        from: p.a,
        to: p.b,
        fromRole: combo.aRole,
        toRole: combo.bRole,
        zone,
        source,
      });
      const res = this.confirmInvitation(sessionId, { invitationId: inv.id, by: p.b });
      if (res.ok) {
        created.push(inv.id);
        taken.add(p.a);
        taken.add(p.b);
      } else {
        freeZones.unshift(zone);
      }
    }
    return { round, created, openZones: freeZones.length };
  }

  /**
   * Make-up dance (补舞): for every no-show invitation, find the party who did
   * arrive and pair them with a partner in a LATER open round. We try, in order:
   *   1. reschedule with the same partner (改约) if they can make a later round;
   *   2. pair with any other guest who still needs partners.
   * Existing confirmed partners/zones in other rounds are never disturbed.
   */
  autoMakeup(sessionId) {
    const s = this.#session(sessionId);
    const absent = Object.values(s.invitations)
      .filter((i) => i.status === 'absent')
      .sort((a, b) => a.round - b.round || a.seq - b.seq);
    const created = [];
    const nextRoundStart = s.closedRounds.length + 1;

    const laterRounds = (fromRound) => {
      const out = [];
      for (let r = fromRound + 1; r <= s.rounds.length; r++) {
        if (r >= nextRoundStart && !this.#isClosed(s, r)) out.push(r);
      }
      return out;
    };

    for (const inv of absent) {
      const metA = new Set(this.metPartners(sessionId, inv.from));
      const metB = new Set(this.metPartners(sessionId, inv.to));
      const needsA = s.guests[inv.from].targetPartners > metA.size;
      const needsB = s.guests[inv.to].targetPartners > metB.size;

      // The stranded guest is the one who arrived (is present in inv.round).
      const stranded = isPresent(s.guests[inv.from], inv.round) ? inv.from : inv.to;
      const strandedNeeds =
        stranded === inv.from ? needsA : needsB;
      if (!strandedNeeds) continue;

      let scheduled = false;

      // Prefer a brand-new partner first: that is what increases the guest's
      // distinct-partner count. Only fall back to re-dancing the same partner.
      for (const r of laterRounds(inv.round)) {
        const met = new Set(this.metPartners(sessionId, stranded));
        const candidates = Object.keys(s.guests)
          .filter((n) => n !== stranded && !met.has(n))
          .sort(cmp);
        for (const cand of candidates) {
          const slot = this.#tryMakeupPair(s, stranded, cand, r);
          if (slot) {
            created.push(slot);
            scheduled = true;
            break;
          }
        }
        if (scheduled) break;
      }

      // 2) Otherwise try to reschedule with the same partner (改约).
      if (!scheduled) {
        const other = stranded === inv.from ? inv.to : inv.from;
        for (const r of laterRounds(inv.round)) {
          const slot = this.#tryMakeupPair(s, stranded, other, r);
          if (slot) {
            created.push(slot);
            scheduled = true;
            break;
          }
        }
      }
    }
    return { created };
  }

  #tryMakeupPair(s, from, to, r) {
    const g1 = s.guests[from];
    const g2 = s.guests[to];
    if (!g2) return null;
    if (r > g1.roundsPurchased || r > g2.roundsPurchased) return null;
    if (!isPresent(g1, r) || !isPresent(g2, r)) return null;
    if (this.#hasLiveInvite(s, from, r) || this.#hasLiveInvite(s, to, r)) return null;
    const zone = Object.values(s.zones).find((z) => z.open && !this.#zoneBusy(s, z.id, r));
    if (!zone) return null;
    const rhythm = s.rounds[r - 1].rhythm;
    if (!acceptsRhythm(g1, r, rhythm) || !acceptsRhythm(g2, r, rhythm)) return null;
    const combo = roleComboFor(g1, g2);
    if (!combo) return null;
    const newInv = this.proposeInvitation(s.id, {
      round: r,
      from,
      to,
      fromRole: combo.aRole,
      toRole: combo.bRole,
      zone: zone.id,
      source: 'makeup',
    });
    const res = this.confirmInvitation(s.id, { invitationId: newInv.id, by: to });
    if (res.ok) return { invitationId: newInv.id, round: r, from, to };
    return null;
  }

  // ---------------------------------------------------------------------------
  // Internal helpers
  // ---------------------------------------------------------------------------
  #session(sessionId) {
    const s = this.state.sessions[sessionId];
    if (!s) throw new DomainError('NO_SESSION', `session ${sessionId} not found`);
    return s;
  }

  #guest(s, number) {
    const g = s.guests[number];
    if (!g) throw new DomainError('NO_GUEST', `guest ${number} not found`);
    return g;
  }

  #roundIndex(s, round) {
    if (!Number.isInteger(round) || round < 1 || round > s.rounds.length) {
      throw new DomainError('BAD_ROUND', `round must be 1..${s.rounds.length}`);
    }
    return round;
  }

  #isClosed(s, round) {
    return s.closedRounds.includes(round);
  }

  #hasLiveInvite(s, number, round) {
    return Object.values(s.invitations).some(
      (i) =>
        i.round === round &&
        (i.from === number || i.to === number) &&
        (i.status === 'confirmed' || i.status === 'proposed')
    );
  }

  #zoneBusy(s, zone, round) {
    return Object.values(s.invitations).some(
      (i) => i.round === round && i.zone === zone && i.status === 'confirmed'
    );
  }

  #confirmedClash(s, inv) {
    for (const party of [inv.from, inv.to]) {
      const clash = Object.values(s.invitations).find(
        (o) =>
          o.id !== inv.id &&
          o.round === inv.round &&
          o.status === 'confirmed' &&
          (o.from === party || o.to === party)
      );
      if (clash) return { party, invitationId: clash.id };
    }
    return null;
  }

  #nextInvitationId(s) {
    let n = Object.values(s.invitations).length + 1;
    while (s.invitations[`inv_${s.id}_${n}`]) n++;
    return `inv_${s.id}_${n}`;
  }

  // ---------------------------------------------------------------------------
  // Reducer — pure fold of events into state
  // ---------------------------------------------------------------------------
  #apply(ev) {
    const S = this.state.sessions;
    switch (ev.type) {
      case 'SESSION_CREATED': {
        const zones = {};
        for (const z of ev.zones) zones[z] = { id: z, open: false };
        S[ev.sessionId] = {
          id: ev.sessionId,
          rounds: ev.rounds.map((r) => ({ ...r })),
          zones,
          guests: {},
          invitations: {},
          dances: {},
          closedRounds: [],
          orders: {},
        };
        break;
      }
      case 'GUEST_REGISTERED': {
        const s = S[ev.sessionId];
        s.guests[ev.number] = {
          number: ev.number,
          name: ev.name,
          roles: ev.roles.slice(),
          rhythms: ev.rhythms.slice(),
          targetPartners: ev.targetPartners,
          roundsPurchased: ev.roundsPurchased,
          absentRounds: {},
          consent: {},
        };
        break;
      }
      case 'GUEST_REST_SET': {
        const g = S[ev.sessionId].guests[ev.number];
        if (ev.away) g.absentRounds[ev.round] = true;
        else delete g.absentRounds[ev.round];
        break;
      }
      case 'ZONE_SET': {
        S[ev.sessionId].zones[ev.zone].open = ev.open;
        break;
      }
      case 'RHYTHM_CONSENT_SET': {
        const g = S[ev.sessionId].guests[ev.number];
        g.consent[ev.round] = g.consent[ev.round] || {};
        g.consent[ev.round][ev.rhythm] = ev.accept;
        break;
      }
      case 'INVITATION_PROPOSED': {
        const s = S[ev.sessionId];
        s.invitations[ev.invitationId] = {
          id: ev.invitationId,
          sessionId: ev.sessionId,
          round: ev.round,
          from: ev.from,
          to: ev.to,
          fromRole: ev.fromRole,
          toRole: ev.toRole,
          zone: ev.zone,
          rhythm: ev.rhythm,
          source: ev.source,
          status: 'proposed',
          seq: ev.seq,
          confirmedSeq: null,
        };
        break;
      }
      case 'INVITATION_CONFIRMED': {
        const inv = S[ev.sessionId].invitations[ev.invitationId];
        inv.status = 'confirmed';
        inv.confirmedSeq = ev.seq;
        break;
      }
      case 'INVITATION_DECLINED': {
        S[ev.sessionId].invitations[ev.invitationId].status = 'declined';
        break;
      }
      case 'INVITATION_CANCELLED': {
        S[ev.sessionId].invitations[ev.invitationId].status = 'cancelled';
        break;
      }
      case 'INVITATION_CONFLICT': {
        const inv = S[ev.sessionId].invitations[ev.invitationId];
        inv.status = 'conflict';
        inv.conflictWith = ev.conflictWith;
        inv.conflictReason = ev.reason;
        break;
      }
      case 'ROUND_CLOSED': {
        const s = S[ev.sessionId];
        for (const o of ev.outcomes) {
          const inv = s.invitations[o.invitationId];
          if (!inv) continue;
          inv.status = o.status;
          inv.outcome = {
            status: o.status,
            reason: o.reason || null,
            absentBy: o.absentBy || null,
            danceId: o.danceId || null,
          };
        }
        for (const d of ev.dances) {
          s.dances[d.id] = { ...d, seq: ev.seq };
        }
        s.closedRounds.push(ev.round);
        break;
      }
      case 'ORDER_REDEEMED': {
        S[ev.sessionId].orders[ev.number] = {
          number: ev.number,
          redeemed: true,
          at: ev.ts,
          seq: ev.seq,
        };
        break;
      }
      default:
        throw new Error(`unknown event type ${ev.type}`);
    }
  }
}

// -----------------------------------------------------------------------------
// Small helpers
// -----------------------------------------------------------------------------
function normaliseRounds(rounds) {
  if (rounds === undefined || rounds === null) throw new DomainError('BAD_INPUT', 'rounds required');
  let list;
  if (typeof rounds === 'number') {
    list = Array.from({ length: rounds }, (_, i) => ({ rhythm: 'any' }));
  } else if (Array.isArray(rounds)) {
    list = rounds.map((r, i) => ({
      index: i + 1,
      rhythm: typeof r === 'string' ? r : r && r.rhythm ? r.rhythm : 'any',
    }));
  } else {
    throw new DomainError('BAD_INPUT', 'rounds must be a number or array');
  }
  return list.map((r, i) => ({ index: i + 1, rhythm: r.rhythm }));
}

function normaliseZones(zones) {
  if (zones === undefined || zones === null) throw new DomainError('BAD_INPUT', 'zones required');
  let list;
  if (typeof zones === 'number') list = Array.from({ length: zones }, (_, i) => `z${i + 1}`);
  else if (Array.isArray(zones)) list = zones.map((z) => (typeof z === 'string' ? z : z.id));
  else throw new DomainError('BAD_INPUT', 'zones must be a number or array');
  const seen = new Set();
  for (const z of list) {
    if (!z) throw new DomainError('BAD_INPUT', 'zone id required');
    if (seen.has(z)) throw new DomainError('BAD_INPUT', `duplicate zone ${z}`);
    seen.add(z);
  }
  return list;
}

function normaliseRoles(roles) {
  let list;
  if (roles === undefined || roles === null) list = [LEAD, FOLLOW];
  else if (typeof roles === 'string') list = roles.split(/[,\s]+/).filter(Boolean);
  else list = roles.slice();
  if (list.length === 0) throw new DomainError('BAD_ROLE', 'guest needs at least one role');
  for (const r of list) {
    if (!VALID_ROLES.has(r)) throw new DomainError('BAD_ROLE', `bad role ${r}`);
  }
  return [...new Set(list)];
}

function describeInvite(i, me) {
  const partner = i.from === me ? i.to : i.from;
  const myRole = i.from === me ? i.fromRole : i.toRole;
  return {
    id: i.id,
    round: i.round,
    partner,
    myRole,
    partnerRole: i.from === me ? i.toRole : i.fromRole,
    zone: i.zone,
    rhythm: i.rhythm,
    status: i.status,
    source: i.source,
  };
}

function statusRank(i) {
  return i.status === 'confirmed' ? 0 : 1;
}

/**
 * Maximum bipartite matching size between rounds and candidate partner numbers.
 * adj: Map<round, number[]>. Each round can host at most one new partner, and a
 * partner can be met only once, so this is the tight upper bound on how many
 * additional distinct partners a guest can still meet.
 */
function maxBipartiteMatching(rounds, adj) {
  const matchToRound = new Map(); // partner -> round
  const tryAssign = (r, seen) => {
    for (const partner of adj.get(r) || []) {
      if (seen.has(partner)) continue;
      seen.add(partner);
      const prev = matchToRound.get(partner);
      if (prev === undefined || tryAssign(prev, seen)) {
        matchToRound.set(partner, r);
        return true;
      }
    }
    return false;
  };
  let size = 0;
  for (const r of rounds) {
    if (tryAssign(r, new Set())) size++;
  }
  return size;
}

function cmp(a, b) {
  const sa = String(a);
  const sb = String(b);
  return sa < sb ? -1 : sa > sb ? 1 : 0;
}

function toInt(v, name) {
  const n = typeof v === 'number' ? v : Number(v);
  if (!Number.isInteger(n)) throw new DomainError('BAD_INPUT', `${name} must be an integer`);
  return n;
}

module.exports = { Engine, DomainError };
