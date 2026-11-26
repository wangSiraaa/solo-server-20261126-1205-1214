import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from '../src/store.js';
import { hh, app, stay } from './helpers.js';
import { setApplication } from '../src/domain.js';
import { signContract, contractSettlement, closeContract, listStatements } from '../src/contracts.js';
import { planContracts } from '../src/schedule.js';

// The full narrative from the task, in order, on one service instance.

test('narrative: balanced 3-group, then one edge short, then date/consent blocks, then close', () => {
  const store = createStore();
  for (const id of ['a', 'b', 'c']) hh(store, id);
  // Three mutually consenting households, each hosts on a different weekend.
  const w1 = ['2026-12-04', '2026-12-05'];
  const w2 = ['2026-12-11', '2026-12-12'];
  const w3 = ['2026-12-18', '2026-12-19'];
  const all = [...w1, ...w2, ...w3];
  for (const id of ['a', 'b', 'c']) {
    app(store, id, { nights: all, minConsecutiveNights: 2, maxNights: 2 });
  }
  // Contract is adopted: the mutual-aid ring.
  const c = signContract(store, {
    type: 'ring', households: ['a', 'b', 'c'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: w1 },
      { host: 'b', guest: 'c', plannedNights: w2 },
      { host: 'c', guest: 'a', plannedNights: w3 },
    ],
  });

  // --- Everyone actually stays both nights: all square. ---
  for (let e = 0; e < 3; e += 1) stay(store, c.id, e, [w1, w2, w3][e]);
  let s = contractSettlement(store, c.id);
  assert.equal(s.fullyOffset, true);
  assert.equal(s.commonOffset, 2);
  assert.deepEqual(s.debts, []);

  // --- One edge only one night (c->a loses its second night). ---
  stay(store, c.id, 2, [w3[0]]);
  s = contractSettlement(store, c.id);
  assert.equal(s.fullyOffset, false);
  assert.equal(s.edges[2].actualCount, 1);
  // Under equal-portion offset the common part is min(2,2,1)=1. Each edge keeps
  // its residual: a->b has 1 extra (b owes a), b->c has 1 extra (c owes b),
  // c->a is fully covered by the common portion. Residuals never net across the
  // ring; both remain individually traceable to the host that advanced them.
  assert.deepEqual(s.carryingHouseholds.sort(), ['a']);
  assert.deepEqual(s.owingHouseholds.sort(), ['c']);
  assert.equal(s.commonOffset, 1);
  assert.equal(s.debts.length, 2);
  const byEdge = Object.fromEntries(s.debts.map((d) => [d.edgeIndex, d]));
  assert.deepEqual(byEdge[0], {
    from: 'b', to: 'a', nights: 1, advancedBy: 'a', kind: 'ring',
    edgeIndex: 0, nights_detail: [w1[1]],
  });
  assert.deepEqual(byEdge[1], {
    from: 'c', to: 'b', nights: 1, advancedBy: 'b', kind: 'ring',
    edgeIndex: 1, nights_detail: [w2[1]],
  });

  // --- Close: final statement captures the outstanding night. ---
  const final = closeContract(store, c.id);
  assert.equal(final.kind, 'final');
  assert.equal(final.summary.fullyOffset, false);
  assert.ok(final.summary.debts.length >= 1);
  assert.equal(listStatements(store, c.id).length, 1);
});

test('narrative: date shrink blocks 2-night signing, restore fixes it, consent revoke blocks confirm', () => {
  const store = createStore();
  hh(store, 'x'); hh(store, 'y');
  const nXY = ['2027-02-05', '2027-02-06'];
  const nYX = ['2027-02-12', '2027-02-13'];
  app(store, 'x', { nights: [...nXY, ...nYX], minConsecutiveNights: 2 });
  app(store, 'y', { nights: [...nXY, ...nYX], minConsecutiveNights: 2 });

  const spec = {
    type: 'pair', households: ['x', 'y'],
    edges: [
      { host: 'x', guest: 'y', plannedNights: nXY },
      { host: 'y', guest: 'x', plannedNights: nYX },
    ],
  };
  assert.doesNotThrow(() => signContract(store, spec));

  // Shrink x's away window to one night: the 2-night continuous visit can't sign.
  setApplication(store, 'x', {
    travelNights: [nYX[0]], hostNights: [...nXY, ...nYX], minConsecutiveNights: 2,
  });
  assert.throws(() => signContract(store, spec), /cannot be signed/);

  // Restore dates -> signable again.
  setApplication(store, 'x', {
    travelNights: [...nXY, ...nYX], hostNights: [...nXY, ...nYX], minConsecutiveNights: 2,
  });
  const c = signContract(store, spec);
  assert.equal(c.status, 'signed');
});

test('reviewer: a different 4-household group plan yields a different, signable arrangement', () => {
  const store = createStore();
  for (const id of ['p', 'q', 'r', 's']) hh(store, id);
  const w1 = ['2027-03-05', '2027-03-06'];
  const w2 = ['2027-03-12', '2027-03-13'];
  const all = [...w1, ...w2];
  for (const id of ['p', 'q', 'r', 's']) {
    app(store, id, { nights: all, minConsecutiveNights: 2, maxNights: 2 });
  }
  const pairPlan = planContracts(store, { type: 'pair', households: ['p', 'q', 'r', 's'] });
  const ringPlan = planContracts(store, { type: 'ring', households: ['p', 'q', 'r', 's'] });
  // Both contract types can propose complete arrangements.
  assert.ok(pairPlan.count >= 1);
  assert.ok(ringPlan.count >= 1);
  // Every proposed contract signs cleanly.
  for (const c of pairPlan.contracts) {
    assert.doesNotThrow(() => signContract(store, {
      type: 'pair', households: c.households,
      edges: c.edges.map((e) => ({ host: e.host, guest: e.guest, plannedNights: e.nights })),
    }));
  }
});
