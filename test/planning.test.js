import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from '../src/store.js';
import { hh, app } from './helpers.js';
import { planContracts } from '../src/schedule.js';
import { signContract } from '../src/contracts.js';

test('pair planning: picks max number of complete mutual visits within dates', () => {
  const store = createStore();
  for (const id of ['a', 'b', 'c', 'd']) hh(store, id);
  const w1 = ['2026-12-04', '2026-12-05'];
  const w2 = ['2026-12-11', '2026-12-12'];
  const all = [...w1, ...w2];
  for (const id of ['a', 'b', 'c', 'd']) app(store, id, { nights: all, minConsecutiveNights: 2 });

  const plan = planContracts(store, { type: 'pair', households: ['a', 'b', 'c', 'd'] });
  // With 4 households and two weekends we can pack two disjoint pair-visits.
  assert.equal(plan.count, 2);
  for (const c of plan.contracts) {
    assert.equal(c.type, 'pair');
    // each household in at most one packed contract
  }
  const used = plan.contracts.flatMap((c) => c.households);
  assert.equal(new Set(used).size, used.length, 'no household double-used');
  // Every packed pair has both directions and is signable.
  for (const c of plan.contracts) {
    assert.doesNotThrow(() => signContract(store, {
      type: 'pair', households: c.households,
      edges: c.edges.map((e) => ({ host: e.host, guest: e.guest, plannedNights: e.nights })),
    }));
  }
});

test('ring planning: complete ring only when all three select feasible dates', () => {
  const store = createStore();
  for (const id of ['a', 'b', 'c']) hh(store, id);
  const w1 = ['2026-12-04', '2026-12-05'];
  const w2 = ['2026-12-11', '2026-12-12'];
  const w3 = ['2026-12-18', '2026-12-19'];
  app(store, 'a', { nights: [...w1, ...w2, ...w3], minConsecutiveNights: 2 });
  app(store, 'b', { nights: [...w1, ...w2, ...w3], minConsecutiveNights: 2 });
  app(store, 'c', { nights: [...w1, ...w2, ...w3], minConsecutiveNights: 2 });

  const plan = planContracts(store, { type: 'ring', households: ['a', 'b', 'c'] });
  assert.ok(plan.count >= 1, 'at least one complete ring');
  const ring = plan.contracts.find((c) => c.households.length === 3);
  assert.ok(ring, 'a 3-ring emerged');
  // Exactly one outgoing and one incoming edge per household.
  for (const h of ring.households) {
    assert.equal(ring.edges.filter((e) => e.host === h).length, 1);
    assert.equal(ring.edges.filter((e) => e.guest === h).length, 1);
  }
});

test('ring not formed when one household lacks a date', () => {
  const store = createStore();
  for (const id of ['a', 'b', 'c']) hh(store, id);
  app(store, 'a', { nights: ['2026-12-04', '2026-12-05'] });
  app(store, 'b', { nights: ['2026-12-04', '2026-12-05'] });
  app(store, 'c', { nights: ['2027-03-05', '2027-03-06'] }); // valid but no overlap
  const plan = planContracts(store, { type: 'ring', households: ['a', 'b', 'c'] });
  assert.equal(plan.count, 0);
});
