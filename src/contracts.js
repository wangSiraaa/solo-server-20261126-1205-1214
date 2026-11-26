// Contract lifecycle: sign -> report/confirm per edge -> cancel -> revise ->
// close (immutable statements) -> supplementary close (delta only).

import {
  badRequest, notFound, conflict, unprocessable, uid, clone, nowIso,
  sortNights, asArray,
} from './util.js';
import { checkEdge } from './schedule.js';
import { settleContract, settlementDelta, summarize, bookingView } from './accounting.js';

function appOrThrow(store, id) {
  const a = store.state.applications[id];
  if (!a) throw unprocessable(`household ${id} has no active application`);
  return a;
}

// Re-validate a proposed contract against the *current* applications. This is
// what blocks signing after dates shrink or a host revokes combination consent.
export function validateContractAgainstApplications(store, spec) {
  const problems = [];
  for (const e of spec.edges) {
    const hostApp = appOrThrow(store, e.host);
    const guestApps = (e.guestHouseholds ?? [e.guest]).map((g) => appOrThrow(store, g));
    const res = checkEdge(hostApp, guestApps, e.plannedNights ?? e.nights ?? []);
    if (!res.ok) problems.push({ host: e.host, guest: e.guest, reasons: res.reasons });
  }
  return problems;
}

// "If the whole family is out one day, the home cannot simultaneously claim to
// be receiving." Enforced across all active (non-closed) contracts so a
// household cannot be away as a whole family in one contract while hosting in
// another on the same night.
function checkRoleConflicts(store, normEdges, excludeContractId = null) {
  const roleByNight = new Map(); // householdId -> Map(night -> {host:bool, guestWhole:bool, why})
  const note = (h, n, role, why) => {
    if (!roleByNight.has(h)) roleByNight.set(h, new Map());
    const m = roleByNight.get(h);
    const cur = m.get(n) ?? { host: false, guestWhole: false, why: [] };
    if (role === 'host') cur.host = true;
    if (role === 'guestWhole') cur.guestWhole = true;
    cur.why.push(why);
    m.set(n, cur);
  };

  for (const e of normEdges) {
    for (const n of e.plannedNights) {
      note(e.host, n, 'host', 'new-contract host');
      for (const g of e.guestHouseholds) {
        if (store.state.applications[g]?.wholeFamily) note(g, n, 'guestWhole', 'new-contract whole-family away');
      }
    }
  }
  for (const c of Object.values(store.state.contracts)) {
    if (c.id === excludeContractId) continue;
    if (c.status === 'closed') continue;
    for (const e of c.edges) {
      for (const n of e.plannedNights) {
        if (e.cancelledNights?.includes(n)) continue;
        note(e.host, n, 'host', `contract ${c.id} host`);
        for (const g of e.guestHouseholds ?? [e.guest]) {
          if (store.state.applications[g]?.wholeFamily) note(g, n, 'guestWhole', `contract ${c.id} whole-family away`);
        }
      }
    }
  }

  const conflicts = [];
  for (const [h, nights] of roleByNight) {
    for (const [n, r] of nights) {
      if (r.host && r.guestWhole) {
        conflicts.push({ household: h, night: n, why: r.why });
      }
    }
  }
  return conflicts;
}

export function signContract(store, spec) {
  const { type, households, edges } = spec ?? {};
  if (!Array.isArray(households) || households.length < 2) throw badRequest('contract needs households');
  if (!Array.isArray(edges) || edges.length === 0) throw badRequest('contract needs edges');
  if (type === 'pair' && households.length !== 2) throw badRequest('pair contract needs exactly two households');
  if (type === 'ring' && (households.length < 3 || households.length > 4)) {
    throw badRequest('ring contract needs three or four households');
  }

  const normEdges = edges.map((e, idx) => {
    const planned = sortNights(asArray(e.plannedNights ?? e.nights));
    if (planned.length === 0) throw badRequest(`edge ${idx} has no planned nights`);
    if (!households.includes(e.host)) throw badRequest(`edge ${idx} host not in contract`);
    const guests = e.guestHouseholds ? asArray(e.guestHouseholds) : [e.guest];
    for (const g of guests) if (!households.includes(g)) throw badRequest(`edge ${idx} guest not in contract`);
    return {
      index: idx,
      host: e.host,
      guest: guests[0],
      guestHouseholds: guests,
      plannedNights: planned,
      cancelledNights: [],
    };
  });

  const problems = validateContractAgainstApplications(store, { edges: normEdges });
  if (problems.length) {
    throw unprocessable('contract cannot be signed against current applications', { problems });
  }

  const roleConflicts = checkRoleConflicts(store, normEdges);
  if (roleConflicts.length) {
    throw unprocessable('whole-family away but home claimed as hosting on the same night', {
      conflicts: roleConflicts,
    });
  }

  // For a pair, ensure both directions present.
  if (type === 'pair') {
    const [a, b] = households;
    const hasAB = normEdges.some((e) => e.host === a && e.guest === b);
    const hasBA = normEdges.some((e) => e.host === b && e.guest === a);
    if (!hasAB || !hasBA) throw unprocessable('a pair contract needs both directions');
  }
  if (type === 'ring') {
    // every household must host exactly one edge and guest exactly one (a cycle)
    for (const h of households) {
      if (normEdges.filter((e) => e.host === h).length !== 1) {
        throw unprocessable('ring needs exactly one outgoing edge per household');
      }
      if (normEdges.filter((e) => e.guest === h).length !== 1) {
        throw unprocessable('ring needs exactly one incoming edge per household');
      }
    }
  }

  const id = uid('ctr');
  const contract = {
    id,
    type,
    households: households.slice(),
    edges: normEdges,
    status: 'signed',
    signedAt: nowIso(),
    bookings: {}, // edgeIndex -> bookingId
    statements: [],
    draft: Boolean(spec.draft),
    revisedFrom: spec.revisedFrom ?? null,
  };

  // Create a booking per edge with empty report sets.
  for (const e of normEdges) {
    const bid = uid('bkg');
    store.state.bookings[bid] = {
      id: bid,
      contractId: id,
      edgeIndex: e.index,
      host: e.host,
      guest: e.guest,
      hostReported: [],
      guestReported: [],
      hostConfirmedAt: null,
      guestConfirmedAt: null,
      version: 0,
      history: [],
      createdAt: nowIso(),
    };
    contract.bookings[e.index] = bid;
  }

  store.state.contracts[id] = contract;
  store.logEvent('contract.signed', { id, type, households, edges: normEdges });
  store.save();
  return clone(contract);
}

export function getContract(store, id) {
  const c = store.state.contracts[id];
  if (!c) throw notFound(`contract ${id} not found`);
  return c;
}

function bookingForEdge(store, contract, edgeIndex) {
  const bid = contract.bookings[edgeIndex];
  const b = bid ? store.state.bookings[bid] : null;
  if (!b) throw notFound(`no booking for edge ${edgeIndex}`);
  return b;
}

// Report actual nights for one side. Reporting is a SET assignment (not an
// increment), so re-reporting the same weekend is idempotent.
export function reportNights(store, contractId, edgeIndex, side, nights, { confirm = false } = {}) {
  const contract = getContract(store, contractId);
  if (contract.status === 'closed') throw conflict('contract is closed');
  if (side !== 'host' && side !== 'guest') throw badRequest('side must be host or guest');
  const b = bookingForEdge(store, contract, edgeIndex);
  const field = side === 'host' ? 'hostReported' : 'guestReported';
  const set = sortNights(asArray(nights));
  b.history.push({ at: nowIso(), side, reported: set, confirm });
  b[field] = set;
  b.version += 1;
  if (confirm) {
    if (side === 'host') b.hostConfirmedAt = nowIso();
    else b.guestConfirmedAt = nowIso();
  } else {
    // A fresh report supersedes this side's prior confirmation.
    if (side === 'host') b.hostConfirmedAt = null;
    else b.guestConfirmedAt = null;
  }
  store.logEvent('booking.report', { contractId, edgeIndex, side, nights: set, confirm });
  store.save();
  return bookingView(b);
}

// Mutually confirm the same booking (both sides must have reported the same
// nights — otherwise only the intersection is held as pending, not counted).
export function confirmBooking(store, contractId, edgeIndex, side) {
  const contract = getContract(store, contractId);
  if (contract.status === 'closed') throw conflict('contract is closed');
  if (side !== 'host' && side !== 'guest') throw badRequest('side must be host or guest');

  // Re-validate consent at confirm time: a host who revoked the travelling
  // combination can no longer confirm the original guest.
  const edge = contract.edges[edgeIndex];
  if (!edge) throw notFound(`edge ${edgeIndex} not found`);
  const hostApp = store.state.applications[edge.host];
  const guestApps = edge.guestHouseholds.map((g) => store.state.applications[g]);
  if (hostApp && guestApps.every(Boolean)) {
    const res = checkEdge(hostApp, guestApps, sortNights(edge.plannedNights));
    if (!res.ok) {
      throw unprocessable('cannot confirm: current application no longer supports this booking', {
        reasons: res.reasons,
      });
    }
  }

  const b = bookingForEdge(store, contract, edgeIndex);
  if (side === 'host') b.hostConfirmedAt = b.hostConfirmedAt ?? nowIso();
  else b.guestConfirmedAt = b.guestConfirmedAt ?? nowIso();
  b.version += 1;
  b.history.push({ at: nowIso(), side, confirmed: true });
  store.logEvent('booking.confirm', { contractId, edgeIndex, side });
  store.save();
  return bookingView(b);
}

// Correct an existing report: same surface as report (set semantics) but
// explicitly recorded as a correction, and clears that side's confirmation.
export function correctReport(store, contractId, edgeIndex, side, nights) {
  return reportNights(store, contractId, edgeIndex, side, nights, { confirm: false });
}

// Dispute view for one booking: which nights each side claims, what is agreed,
// and what is held in dispute.
export function getDispute(store, contractId, edgeIndex) {
  const contract = getContract(store, contractId);
  const b = bookingForEdge(store, contract, edgeIndex);
  return { ...bookingView(b), edge: contract.edges[edgeIndex] };
}

// A household cancels a night it was to host. Already-stayed (confirmed) nights
// cannot be cancelled. The cancelled night is released for rearrangement.
export function cancelNight(store, contractId, edgeIndex, night) {
  const contract = getContract(store, contractId);
  if (contract.status === 'closed') throw conflict('contract is closed');
  const edge = contract.edges[edgeIndex];
  if (!edge) throw notFound(`edge ${edgeIndex} not found`);
  const b = bookingForEdge(store, contract, edgeIndex);
  const view = bookingView(b);
  if (view.confirmedNights.includes(night)) {
    throw conflict(`night ${night} already confirmed as stayed and cannot be cancelled`, { night });
  }
  if (!edge.plannedNights.includes(night)) {
    throw badRequest(`night ${night} is not part of edge ${edgeIndex}`);
  }
  edge.cancelledNights = sortNights([...edge.cancelledNights, night]);
  b.history.push({ at: nowIso(), cancelled: night });
  b.version += 1;
  store.logEvent('contract.cancelNight', { contractId, edgeIndex, night });
  store.save();
  return clone(edge);
}

// Close a contract: snapshot settlement. First close => final statement.
// Subsequent close => supplementary statement that only reflects the *delta*
// since the last statement. Old statements remain queryable forever.
export function closeContract(store, contractId) {
  const contract = getContract(store, contractId);
  const settlement = settleContract(contract, store.state.bookings);

  // Recompute the previous settlement from the stored snapshot so a
  // supplementary statement reflects exactly the delta since last close.
  const previous = contract.lastSettlementSnapshot
    ? settleFromSnapshot(contract.lastSettlementSnapshot, contract, store)
    : null;

  const isSupplementary = contract.statements.length > 0;
  const delta = settlementDelta(previous, settlement);
  const stmt = {
    id: uid('stm'),
    contractId,
    sequence: contract.statements.length + 1,
    kind: isSupplementary ? 'supplementary' : 'final',
    issuedAt: nowIso(),
    summary: summarize(settlement),
    delta: isSupplementary ? delta : { baseline: true, nextSummary: summarize(settlement) },
    settlement, // full immutable copy
  };
  store.state.statements[stmt.id] = stmt;
  contract.statements.push(stmt.id);
  contract.status = 'closed';
  contract.closedAt = nowIso();
  // Snapshot the confirmed-night baseline so the delta is exact.
  contract.lastSettlementSnapshot = settlement.edges.map((e) => ({
    edgeIndex: e.edgeIndex, host: e.host, guest: e.guest,
    confirmedNights: e.confirmedNights,
  }));
  contract.lastSettlement = settlement;
  store.logEvent('contract.close', { contractId, statementId: stmt.id, kind: stmt.kind });
  store.save();
  return clone(stmt);
}

function settleFromSnapshot(snapshotEdges, contract, store) {
  // Rebuild a minimal booking map from the snapshot so settlementDelta sees the
  // exact previously-confirmed nights (without re-reading live reports).
  const fakeBookings = {};
  for (const s of snapshotEdges) {
    const id = `snap:${s.edgeIndex}`;
    fakeBookings[id] = {
      id, contractId: contract.id, edgeIndex: s.edgeIndex, host: s.host, guest: s.guest,
      hostReported: s.confirmedNights, guestReported: s.confirmedNights,
      hostConfirmedAt: 'snap', guestConfirmedAt: 'snap',
    };
  }
  const base = { ...contract, edges: contract.edges.map((e) => ({ ...e })) };
  return settleContract(base, fakeBookings);
}

// Reopen a closed contract so it can be corrected and closed again (yielding a
// supplementary statement). Confirmed/actual nights are preserved.
export function reopenContract(store, contractId) {
  const contract = getContract(store, contractId);
  if (contract.status !== 'closed') throw conflict('contract is not closed');
  contract.status = 'reopened';
  store.logEvent('contract.reopen', { contractId });
  store.save();
  return clone(contract);
}

export function listStatements(store, contractId) {
  const contract = getContract(store, contractId);
  return contract.statements.map((sid) => clone(store.state.statements[sid]));
}

export function getStatement(store, statementId) {
  const s = store.state.statements[statementId];
  if (!s) throw notFound(`statement ${statementId} not found`);
  return clone(s);
}

// Settlement view for a live contract.
export function contractSettlement(store, contractId) {
  const contract = getContract(store, contractId);
  return settleContract(contract, store.state.bookings);
}

// ---- revision / rearrangement -------------------------------------------
// Keep nights already stayed, release the rest, and propose follow-up
// contracts only among households that still consent. A household that has
// declined a new contract is never auto-placed into a new home.
export function reviseContract(store, contractId, { declines = [], splits = null } = {}) {
  const contract = getContract(store, contractId);
  const settlement = settleContract(contract, store.state.bookings);

  const declined = new Set(declines);
  for (const h of declined) {
    if (!contract.households.includes(h)) throw badRequest(`household ${h} is not in contract`);
    if (!store.state.households[h]) throw notFound(`household ${h} not found`);
  }

  // Unperformed obligation per edge = planned nights not confirmed-stayed and
  // not already cancelled.
  const unperformed = [];
  for (const e of settlement.edges) {
    const edge = contract.edges[e.edgeIndex];
    const stay = new Set(e.confirmedNights);
    const cancelled = new Set(edge.cancelledNights);
    const openNights = sortNights(
      edge.plannedNights.filter((n) => !stay.has(n) && !cancelled.has(n)),
    );
    if (openNights.length) {
      unperformed.push({ host: e.host, guest: e.guest, openNights });
    }
  }

  // Candidates for rearrangement: same households minus decliners, using only
  // still-open nights. We produce directional proposals; caller can sign.
  const eligible = contract.households.filter((h) => !declined.has(h));
  const proposals = [];
  for (const u of unperformed) {
    if (declined.has(u.host) || declined.has(u.guest)) continue;
    if (!eligible.includes(u.host) || !eligible.includes(u.guest)) continue;
    proposals.push({
      host: u.host,
      guest: u.guest,
      nights: u.openNights,
      reason: 'unperformed',
    });
  }

  return {
    contractId,
    keptConfirmedNights: settlement.edges.map((e) => ({
      host: e.host, guest: e.guest, confirmedNights: e.confirmedNights,
    })),
    unperformed,
    declined: [...declined],
    eligible,
    splitAllowed: splits == null ? true : splits,
    proposals, // never includes declined households as host or guest
    note: 'proposals only include consenting households; declined ones are excluded from new homes',
  };
}

// Convenience: build a revised (draft) contract from an explicit edge spec,
// excluding any declined household automatically.
export function signRevisedContract(store, spec) {
  const declined = new Set(spec.declines ?? []);
  for (const h of spec.households ?? []) {
    if (declined.has(h)) {
      throw unprocessable(`household ${h} declined the new contract and cannot be placed in a new home`);
    }
  }
  return signContract(store, { ...spec, revisedFrom: spec.revisedFrom ?? null });
}
