// Night-level accounting. Everything here is DERIVED from stored bookings:
// a booking keeps each side's reported night *set*, so re-reporting, reloading
// or concurrent writes can never double count. Only nights both sides report
// are "confirmed actual" and count toward offset; the symmetric difference is
// held as disputed.

import { intersectNights, differenceNights, sortNights, unionNights } from './util.js';

export function bookingView(b) {
  const hostSet = sortNights(b.hostReported ?? []);
  const guestSet = sortNights(b.guestReported ?? []);
  const agreed = intersectNights(hostSet, guestSet);
  const bothConfirmed = Boolean(b.hostConfirmedAt && b.guestConfirmedAt);
  // Report = "I stayed / I hosted these nights". Confirm = "I accept this
  // same booking". Only mutually confirmed agreed nights count toward offset.
  const confirmed = bothConfirmed ? agreed : [];
  const onlyHost = differenceNights(hostSet, guestSet);
  const onlyGuest = differenceNights(guestSet, hostSet);
  return {
    id: b.id,
    contractId: b.contractId,
    edgeIndex: b.edgeIndex,
    host: b.host,
    guest: b.guest,
    hostReported: hostSet,
    guestReported: guestSet,
    hostConfirmed: Boolean(b.hostConfirmedAt),
    guestConfirmed: Boolean(b.guestConfirmedAt),
    bothConfirmed,
    agreedNights: agreed,
    confirmedNights: confirmed,
    pendingNights: bothConfirmed ? [] : agreed,
    disputedNights: sortNights(unionNights(onlyHost, onlyGuest)),
    disputeDetail: { onlyHost, onlyGuest },
    version: b.version,
    history: b.history ?? [],
  };
}

// Bookings relevant to a contract, keyed by edge.
export function contractBookings(bookings, contractId) {
  return Object.values(bookings).filter((b) => b.contractId === contractId);
}

// Core settlement of one signed contract. Only edges declared on the contract
// participate ("环外住宿不得挪用").
export function settleContract(contract, allBookings) {
  const books = contractBookings(allBookings, contract.id);
  const byEdge = new Map();
  for (const b of books) byEdge.set(b.edgeIndex, b);

  const edges = contract.edges.map((e, idx) => {
    const b = byEdge.get(idx);
    const view = b ? bookingView(b) : {
      confirmedNights: [], disputedNights: [], hostReported: [], guestReported: [],
    };
    return {
      edgeIndex: idx,
      host: e.host,
      guest: e.guest,
      plannedNights: sortNights(e.plannedNights ?? []),
      plannedCount: (e.plannedNights ?? []).length,
      cancelledNights: sortNights(e.cancelledNights ?? []),
      confirmedNights: view.confirmedNights,
      actualCount: view.confirmedNights.length,
      disputedNights: view.disputedNights,
      disputeDetail: view.disputeDetail,
      bookingId: b?.id ?? null,
    };
  });

  // "Equal portion" offset: the amount every edge in the signed ring/pair can
  // cancel at once. For a pair that is min(xAB, xBA); for a ring it is the min
  // across all edges. Residual = what each edge carries beyond the common part.
  const actuals = edges.map((e) => e.actualCount);
  const common = actuals.length ? Math.min(...actuals) : 0;
  for (const e of edges) {
    e.offset = common;
    e.residual = e.actualCount - common;
  }

  // Per-household net within the contract: hosted minus received (confirmed).
  const per = new Map();
  const touch = (h) => {
    if (!per.has(h)) per.set(h, { id: h, hosted: 0, received: 0, hostedNights: [], receivedNights: [] });
    return per.get(h);
  };
  for (const e of edges) {
    const host = touch(e.host);
    const guest = touch(e.guest);
    host.hosted += e.actualCount;
    host.hostedNights.push(...e.confirmedNights);
    guest.received += e.actualCount;
    guest.receivedNights.push(...e.confirmedNights);
  }
  const households = [...per.values()].map((h) => {
    const net = h.hosted - h.received;
    return {
      id: h.id,
      hosted: h.hosted,
      received: h.received,
      net,
      carrying: net > 0,        // advanced nights not yet reciprocated
      owing: net < 0,
      hostedNights: sortNights(h.hostedNights),
      receivedNights: sortNights(h.receivedNights),
    };
  });

  // Directional obligations under "equal-portion offset" (等量部分抵销):
  // each edge contributes the SAME common portion to a circular offset; the
  // residual beyond that common portion stays as a direct obligation on that
  // edge (no netting across the ring, and outside-ring nights never apply).
  //   pair  (a->b = x, b->a = y): common = min(x,y); residual on one edge -> owe
  //   ring: common = min over all edges; each edge's residual is a direct debt
  // `advancedBy` names the household that actually provided the extra nights.
  const debts = [];
  for (const e of edges) {
    if (e.residual > 0) {
      debts.push({
        from: e.guest,
        to: e.host,
        nights: e.residual,
        advancedBy: e.host,
        kind: contract.type === 'pair' ? 'pair' : 'ring',
        edgeIndex: e.edgeIndex,
        nights_detail: e.confirmedNights.slice(common), // traceable nights
      });
    }
  }

  const netZero = households.every((h) => h.net === 0);
  const allPlannedMet = edges.every((e) => e.actualCount >= e.plannedCount);
  const hasDispute = edges.some((e) => e.disputedNights.length > 0);

  return {
    contractId: contract.id,
    type: contract.type,
    status: contract.status,
    households: contract.households.slice(),
    edges,
    householdPositions: households,
    debts,
    commonOffset: common,
    fullyOffset: netZero,
    allPlannedMet,
    hasDispute,
    totalConfirmedNights: edges.reduce((s, e) => s + e.actualCount, 0),
    // Which household is "carrying" unmatched nights (traceability).
    carryingHouseholds: households.filter((h) => h.net > 0).map((h) => h.id),
    owingHouseholds: households.filter((h) => h.net < 0).map((h) => h.id),
    byId: Object.fromEntries(households.map((h) => [h.id, h])),
  };
}

// Difference between two settlement snapshots (for supplementary statements).
export function settlementDelta(prev, next) {
  if (!prev) return { baseline: true, nextSummary: summarize(next) };
  const edgeDelta = next.edges.map((e) => {
    const p = prev.edges.find((pe) => pe.edgeIndex === e.edgeIndex && pe.host === e.host && pe.guest === e.guest);
    const before = p?.actualCount ?? 0;
    return {
      edgeIndex: e.edgeIndex, host: e.host, guest: e.guest,
      before, after: e.actualCount, delta: e.actualCount - before,
      newNights: differenceNights(e.confirmedNights, p?.confirmedNights ?? []),
      removedNights: differenceNights(p?.confirmedNights ?? [], e.confirmedNights),
    };
  });
  const posDelta = next.householdPositions.map((h) => {
    const p = prev.householdPositions.find((ph) => ph.id === h.id);
    return {
      id: h.id,
      netBefore: p?.net ?? 0, netAfter: h.net, netDelta: h.net - (p?.net ?? 0),
      hostedDelta: h.hosted - (p?.hosted ?? 0),
      receivedDelta: h.received - (p?.received ?? 0),
    };
  });
  return {
    edgeDelta,
    positionDelta: posDelta,
    fullyOffsetBefore: prev.fullyOffset,
    fullyOffsetAfter: next.fullyOffset,
    debtsBefore: prev.debts,
    debtsAfter: next.debts,
  };
}

export function summarize(settlement) {
  return {
    contractId: settlement.contractId,
    type: settlement.type,
    fullyOffset: settlement.fullyOffset,
    commonOffset: settlement.commonOffset,
    edges: settlement.edges.map((e) => ({
      host: e.host, guest: e.guest, planned: e.plannedCount, actual: e.actualCount,
      offset: e.offset, residual: e.residual, disputed: e.disputedNights.length,
    })),
    households: settlement.householdPositions.map((h) => ({ id: h.id, hosted: h.hosted, received: h.received, net: h.net })),
    debts: settlement.debts,
  };
}
