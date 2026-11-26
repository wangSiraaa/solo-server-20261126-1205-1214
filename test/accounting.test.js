import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from '../src/store.js';
import { hh, app, stay } from './helpers.js';
import { signContract, contractSettlement } from '../src/contracts.js';


test('balanced pair: both stay full two nights each => fully offset, nobody owes', () => {
  const store = createStore();
  hh(store, 'a'); hh(store, 'b');
  const all = ['2026-11-20', '2026-11-21', '2026-11-27', '2026-11-28'];
  app(store, 'a', { nights: all, minConsecutiveNights: 2 });
  app(store, 'b', { nights: all, minConsecutiveNights: 2 });

  const c = signContract(store, {
    type: 'pair', households: ['a', 'b'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: ['2026-11-27', '2026-11-28'] },
      { host: 'b', guest: 'a', plannedNights: ['2026-11-20', '2026-11-21'] },
    ],
  });

  stay(store, c.id, 0, ['2026-11-27', '2026-11-28']);
  stay(store, c.id, 1, ['2026-11-20', '2026-11-21']);

  const s = contractSettlement(store, c.id);
  assert.equal(s.fullyOffset, true);
  assert.equal(s.allPlannedMet, true);
  assert.deepEqual(s.carryingHouseholds, []);
  assert.deepEqual(s.debts, []);
  assert.equal(s.edges[0].actualCount, 2);
  assert.equal(s.edges[1].actualCount, 2);
});

test('one edge short by a night: previous "all offset" breaks, extra night traceable to host', () => {
  const store = createStore();
  hh(store, 'a'); hh(store, 'b');
  const all = ['2026-11-20', '2026-11-21', '2026-11-27', '2026-11-28'];
  app(store, 'a', { nights: all, minConsecutiveNights: 1 });
  app(store, 'b', { nights: all, minConsecutiveNights: 1 });

  const c = signContract(store, {
    type: 'pair', households: ['a', 'b'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: ['2026-11-27', '2026-11-28'] },
      { host: 'b', guest: 'a', plannedNights: ['2026-11-20', '2026-11-21'] },
    ],
  });
  // Everyone else full; edge 1 (b->a) only one night actually stayed.
  stay(store, c.id, 0, ['2026-11-27', '2026-11-28']);
  stay(store, c.id, 1, ['2026-11-20']); // one fewer

  const s = contractSettlement(store, c.id);
  assert.equal(s.fullyOffset, false);
  assert.equal(s.allPlannedMet, false);
  // a hosted 2, received 1 => +1 carrying. b hosted 1, received 2 => -1 owing.
  assert.deepEqual(s.carryingHouseholds, ['a']);
  assert.deepEqual(s.owingHouseholds, ['b']);
  assert.deepEqual(s.debts, [{
    from: 'b', to: 'a', nights: 1, advancedBy: 'a', kind: 'pair',
    edgeIndex: 0, nights_detail: ['2026-11-28'],
  }]);
  // Every confirmed night is traceable to its host edge.
  assert.deepEqual(s.edges[0].confirmedNights, ['2026-11-27', '2026-11-28']);
  assert.deepEqual(s.edges[1].confirmedNights, ['2026-11-20']);
  assert.equal(s.edges[1].actualCount, 1);
});

test('ring of three balanced: equal portion offset, fully balanced', () => {
  const store = createStore();
  for (const id of ['a', 'b', 'c']) hh(store, id);
  const w1 = ['2026-12-04', '2026-12-05'];
  const w2 = ['2026-12-11', '2026-12-12'];
  const w3 = ['2026-12-18', '2026-12-19'];
  const all = [...w1, ...w2, ...w3];
  app(store, 'a', { nights: all });
  app(store, 'b', { nights: all });
  app(store, 'c', { nights: all });

  const c = signContract(store, {
    type: 'ring', households: ['a', 'b', 'c'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: w1 },
      { host: 'b', guest: 'c', plannedNights: w2 },
      { host: 'c', guest: 'a', plannedNights: w3 },
    ],
  });
  stay(store, c.id, 0, w1);
  stay(store, c.id, 1, w2);
  stay(store, c.id, 2, w3);
  const s = contractSettlement(store, c.id);
  assert.equal(s.fullyOffset, true);
  assert.equal(s.commonOffset, 2);
  assert.deepEqual(s.debts, []);
  for (const h of s.householdPositions) assert.equal(h.net, 0);
});

test('ring unequal: equal portion cancels, residual traces who advanced', () => {
  const store = createStore();
  for (const id of ['a', 'b', 'c']) hh(store, id);
  const w1 = ['2026-12-04', '2026-12-05', '2026-12-06'];
  const w2 = ['2026-12-11', '2026-12-12'];
  const w3 = ['2026-12-18', '2026-12-19', '2026-12-20'];
  const all = [...w1, ...w2, ...w3, '2026-12-01', '2026-12-02'];
  for (const id of ['a', 'b', 'c']) app(store, id, { nights: all });
  const c = signContract(store, {
    type: 'ring', households: ['a', 'b', 'c'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: w1 }, // 3
      { host: 'b', guest: 'c', plannedNights: w2 }, // 2
      { host: 'c', guest: 'a', plannedNights: w3 }, // 3
    ],
  });
  stay(store, c.id, 0, w1);
  stay(store, c.id, 1, w2);
  stay(store, c.id, 2, w3);
  const s = contractSettlement(store, c.id);
  assert.equal(s.commonOffset, 2);
  assert.equal(s.fullyOffset, false);
  // edge 1 had 2 (residual 0), edges 0 and 2 have residual 1 each.
  const res = Object.fromEntries(s.edges.map((e) => [`${e.host}>${e.guest}`, e.residual]));
  assert.deepEqual(res, { 'a>b': 1, 'b>c': 0, 'c>a': 1 });
  // Totals still net to zero across the ring even though not "fully offset".
  const sum = s.householdPositions.reduce((acc, h) => acc + h.net, 0);
  assert.equal(sum, 0);
});
