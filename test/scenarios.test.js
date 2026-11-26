import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from '../src/store.js';
import { createHousehold, setApplication } from '../src/domain.js';
import { signContract, contractSettlement, reviseContract, signRevisedContract } from '../src/contracts.js';
import { settleContract } from '../src/accounting.js';
import { planContracts } from '../src/schedule.js';
import { stay, app, hh } from './helpers.js';

test('whole family away cannot be claimed as hosting the same night (across contracts)', () => {
  const store = createStore();
  // a is a whole family (single member) that will be away.
  createHousehold(store, { id: 'a', members: ['a1'] });
  hh(store, 'b');
  hh(store, 'c');
  const n = ['2026-12-04'];
  const n2 = ['2026-12-11', '2026-12-12'];
  const n3 = ['2026-12-18', '2026-12-19'];
  app(store, 'a', { nights: [...n, ...n2, ...n3], party: ['a1'] }); // whole family
  app(store, 'b', { nights: [...n, ...n2, ...n3] });
  app(store, 'c', { nights: [...n, ...n2, ...n3] });

  // Contract 1: a hosts b on n (a is home as host).
  signContract(store, {
    type: 'pair', households: ['a', 'b'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: n },
      { host: 'b', guest: 'a', plannedNights: n2 },
    ],
  });
  // Contract 2 tries to send whole-family a away to c on n, while a is still
  // claimed as host at home on n in contract 1 -> conflict.
  assert.throws(() => signContract(store, {
    type: 'pair', households: ['a', 'c'],
    edges: [
      { host: 'c', guest: 'a', plannedNights: n },
      { host: 'a', guest: 'c', plannedNights: n3 },
    ],
  }), /whole-family/);
});

test('outside-ring nights do not leak into a ring settlement', () => {
  const store = createStore();
  for (const id of ['a', 'b', 'c']) hh(store, id);
  const r1 = ['2026-12-04', '2026-12-05'];
  const r2 = ['2026-12-11', '2026-12-12'];
  const r3 = ['2026-12-18', '2026-12-19'];
  const outside = ['2026-12-25', '2026-12-26'];
  const all = [...r1, ...r2, ...r3, ...outside];
  for (const id of ['a', 'b', 'c']) app(store, id, { nights: all });

  const ring = signContract(store, {
    type: 'ring', households: ['a', 'b', 'c'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: r1 },
      { host: 'b', guest: 'c', plannedNights: r2 },
      { host: 'c', guest: 'a', plannedNights: r3 },
    ],
  });
  // An OUTSIDE pair contract a<->b on different nights than the ring.
  const outside2 = ['2027-01-01', '2027-01-02'];
  app(store, 'a', { nights: [...all, ...outside2] });
  app(store, 'b', { nights: [...all, ...outside2] });
  const outsideC = signContract(store, {
    type: 'pair', households: ['a', 'b'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: outside },
      { host: 'b', guest: 'a', plannedNights: outside2 },
    ],
  });
  // Fully confirm the outside contract; it must not affect the ring.
  stay(store, outsideC.id, 0, outside);
  stay(store, outsideC.id, 1, outside2);

  const s = contractSettlement(store, ring.id);
  assert.equal(s.totalConfirmedNights, 0); // ring untouched before its own stays
  assert.equal(s.fullyOffset, true);
});

test('declined household is excluded from a revised contract', () => {
  const store = createStore();
  for (const id of ['a', 'b', 'c']) hh(store, id);
  const w1 = ['2026-12-04', '2026-12-05'];
  const w2 = ['2026-12-11', '2026-12-12'];
  const w3 = ['2026-12-18', '2026-12-19'];
  const all = [...w1, ...w2, ...w3];
  for (const id of ['a', 'b', 'c']) app(store, id, { nights: all });
  const c = signContract(store, {
    type: 'ring', households: ['a', 'b', 'c'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: w1 },
      { host: 'b', guest: 'c', plannedNights: w2 },
      { host: 'c', guest: 'a', plannedNights: w3 },
    ],
  });
  stay(store, c.id, 0, ['2026-12-04']); // a hosted b one night

  // b declines any new contract -> must never be re-homed.
  assert.throws(() => signRevisedContract(store, {
    type: 'pair', households: ['b', 'c'], declines: ['b'],
    edges: [
      { host: 'b', guest: 'c', plannedNights: ['2027-01-01', '2027-01-02'] },
      { host: 'c', guest: 'b', plannedNights: ['2027-01-08', '2027-01-09'] },
    ],
  }), /declined/);

  const rev = reviseContract(store, c.id, { declines: ['b'] });
  for (const p of rev.proposals) {
    assert.notEqual(p.host, 'b');
    assert.notEqual(p.guest, 'b');
  }
});

test('reviewer scenario: a fresh 4-household pair plan is signable end to end', () => {
  const store = createStore();
  for (const id of ['w', 'x', 'y', 'z']) hh(store, id);
  const wknds = [['2026-12-04', '2026-12-05'], ['2026-12-11', '2026-12-12']];
  const all = wknds.flat();
  for (const id of ['w', 'x', 'y', 'z']) app(store, id, { nights: all, minConsecutiveNights: 2 });

  const plan = planContracts(store, { type: 'pair', households: ['w', 'x', 'y', 'z'] });
  assert.ok(plan.count >= 1);
  let signed = 0;
  for (const c of plan.contracts) {
    const s = signContract(store, {
      type: 'pair', households: c.households,
      edges: c.edges.map((e) => ({ host: e.host, guest: e.guest, plannedNights: e.nights })),
    });
    assert.equal(s.status, 'signed');
    signed += 1;
  }
  assert.equal(signed, plan.count);
});
