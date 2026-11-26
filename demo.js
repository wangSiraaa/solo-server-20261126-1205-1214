// Demonstration of the adopted club contract: the mutual-aid ring ("互助环").
// Three households, each can host on a *different* weekend, all agree to the
// cycle A hosts B -> B hosts C -> C hosts A. We print the complete signed
// group and exactly which nights do / do not get cross-household offset.
//
// Constraint honoured: at most four households, at most six nights.

import { createStore } from './src/store.js';
import { createHousehold, setApplication } from './src/domain.js';
import { planContracts, checkEdge } from './src/schedule.js';
import { signContract, reportNights, confirmBooking, contractSettlement } from './src/contracts.js';
import { settleContract } from './src/accounting.js';

const store = createStore();

function mk(id) {
  return createHousehold(store, {
    id,
    displayName: `Household ${id.toUpperCase()}`,
    members: [`${id}-parent`, `${id}-child`],
    // parent+child are inseparable: they travel together or not at all.
    inseparableGroups: [[`${id}-parent`, `${id}-child`]],
  });
}

const A = 'a', B = 'b', C = 'c';
[A, B, C].forEach(mk);

// Each household is available on all six nights (max 3 weekends), but we give
// each a *different* hosting weekend as its preference.
const WE = {
  dec04: ['2026-12-04', '2026-12-05'],
  dec11: ['2026-12-11', '2026-12-12'],
  dec18: ['2026-12-18', '2026-12-19'],
};
const ALL = [...WE.dec04, ...WE.dec11, ...WE.dec18]; // 6 nights total

// Mutual consent: each household explicitly accepts every other household as a
// travelling companion (the ring only ever sends one guest household per edge).
const othersFor = (id) => [A, B, C].filter((x) => x !== id).map((x) => [x]);
for (const id of [A, B, C]) {
  setApplication(store, id, {
    party: [`${id}-parent`, `${id}-child`],
    travelNights: ALL,
    hostNights: ALL,
    minConsecutiveNights: 2,
    maxNights: 2,
    acceptedCompanions: othersFor(id),
    preferredHosts: [A, B, C].filter((x) => x !== id),
  });
}

console.log('=== Adopted contract: mutual-aid ring ===\n');
console.log('Households:', [A, B, C].join(', '), '| nights:', ALL.length, '(<= 6)\n');

// Plan complete rings, then pick a concrete cycle: a->b (dec04), b->c (dec11),
// c->a (dec18) so every household hosts a *different* weekend.
const plan = planContracts(store, { type: 'ring', households: [A, B, C] });
console.log('Complete rings found by planner:', plan.count);

const cycle = {
  type: 'ring',
  households: [A, B, C],
  edges: [
    { host: A, guest: B, plannedNights: WE.dec04 },
    { host: B, guest: C, plannedNights: WE.dec11 },
    { host: C, guest: A, plannedNights: WE.dec18 },
  ],
};
for (const e of cycle.edges) {
  const ok = checkEdge(store.state.applications[e.host], [store.state.applications[e.guest]], e.plannedNights);
  if (!ok.ok) throw new Error(`edge ${e.host}->${e.guest} not valid: ${ok.reasons.join('; ')}`);
}
const contract = signContract(store, cycle);

console.log('\n=== Signed complete group ===');
console.log('contract:', contract.id, '| type:', contract.type, '| status:', contract.status);
for (const e of contract.edges) {
  console.log(`  ${e.host} hosts ${e.guest} on ${e.plannedNights.join(', ')}`);
}

// Everyone actually stays the full two nights at every edge.
const allNights = Object.values(WE);
contract.edges.forEach((e, i) => {
  const n = allNights[i];
  reportNights(store, contract.id, i, 'host', n, { confirm: true });
  reportNights(store, contract.id, i, 'guest', n, { confirm: true });
  confirmBooking(store, contract.id, i, 'host');
  confirmBooking(store, contract.id, i, 'guest');
});

const s = contractSettlement(store, contract.id);
console.log('\n=== Settlement ===');
console.log('total confirmed nights:', s.totalConfirmedNights);
console.log('equal-portion offset (common):', s.commonOffset, 'nights per edge');
console.log('fully offset:', s.fullyOffset);
for (const e of s.edges) {
  console.log(`  edge ${e.host}->${e.guest}: actual=${e.actualCount}, offset=${e.offset}, residual=${e.residual}`);
}
for (const h of s.householdPositions) {
  console.log(`  ${h.id}: hosted=${h.hosted}, received=${h.received}, net=${h.net}`);
}

// ---- show a night that does NOT get cross-household offset ---------------
// Scenario: C can't host its dec18 night (cancels one night). Its first night
// still happens, so edge c->a is 1 night while a->b and b->c are 2 nights.
console.log('\n=== Night that gets NO cross-household offset (residual) ===');
const store2 = createStore();
[A, B, C].forEach((id) => {
  createHousehold(store2, { id, members: [`${id}-parent`, `${id}-child`] });
});
for (const id of [A, B, C]) {
  setApplication(store2, id, {
    party: [`${id}-parent`, `${id}-child`],
    travelNights: ALL, hostNights: ALL, minConsecutiveNights: 1, maxNights: 2,
    acceptedCompanions: othersFor(id),
  });
}
const c2 = signContract(store2, {
  type: 'ring', households: [A, B, C],
  edges: [
    { host: A, guest: B, plannedNights: WE.dec04 },
    { host: B, guest: C, plannedNights: WE.dec11 },
    { host: C, guest: A, plannedNights: WE.dec18 },
  ],
});
// a->b: 2, b->c: 2, c->a: 1
[[0, WE.dec04], [1, WE.dec11], [2, ['2026-12-18']]].forEach(([i, n]) => {
  reportNights(store2, c2.id, i, 'host', n, { confirm: true });
  reportNights(store2, c2.id, i, 'guest', n, { confirm: true });
  confirmBooking(store2, c2.id, i, 'host');
  confirmBooking(store2, c2.id, i, 'guest');
});
const s2 = settleContract(c2, store2.state.bookings);
console.log('common offset =', s2.commonOffset, '(the equal portion all edges share)');
for (const e of s2.edges) {
  const note = e.residual > 0
    ? `  <-- ${e.residual} night NOT offset across the ring; stays traceable to host ${e.host}`
    : '';
  console.log(`  ${e.host}->${e.guest}: actual=${e.actualCount}, offset=${e.offset}, residual=${e.residual}${note}`);
}
console.log('directional debts (residuals):', JSON.stringify(s2.debts));
console.log('\nNote: nights outside any signed ring ("环外住宿") never enter these sums.');
