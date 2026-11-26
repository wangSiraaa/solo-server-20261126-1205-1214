import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from '../src/store.js';
import { hh, app, stay } from './helpers.js';
import {
  signContract, contractSettlement, closeContract, reopenContract,
  listStatements, getStatement, reportNights, confirmBooking,
} from '../src/contracts.js';

function pair(store, { nA = 2, nB = 2 } = {}) {
  hh(store, 'a'); hh(store, 'b');
  const wAB = ['2026-11-27', '2026-11-28', '2026-11-29'];
  const wBA = ['2026-12-04', '2026-12-05', '2026-12-06'];
  app(store, 'a', { nights: [...wAB, ...wBA] });
  app(store, 'b', { nights: [...wAB, ...wBA] });
  return signContract(store, {
    type: 'pair', households: ['a', 'b'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: wAB.slice(0, nA) },
      { host: 'b', guest: 'a', plannedNights: wBA.slice(0, nB) },
    ],
  });
}

test('close issues an immutable final statement; reopened close emits delta-only supplement', () => {
  const store = createStore();
  const c = pair(store);
  stay(store, c.id, 0, ['2026-11-27', '2026-11-28']);
  stay(store, c.id, 1, ['2026-12-04', '2026-12-05']);

  const final = closeContract(store, c.id);
  assert.equal(final.kind, 'final');
  assert.equal(final.sequence, 1);
  assert.equal(final.summary.fullyOffset, true);
  assert.equal(final.delta.baseline, true);

  // Old statement remains queryable and byte-identical.
  const again = getStatement(store, final.id);
  assert.deepEqual(again, final);

  // Reopen, one more night gets confirmed for edge 0 (a hosts b extra night).
  reopenContract(store, c.id);
  stay(store, c.id, 0, ['2026-11-27', '2026-11-28', '2026-11-29']);
  const supp = closeContract(store, c.id);
  assert.equal(supp.kind, 'supplementary');
  assert.equal(supp.sequence, 2);

  // Delta reflects only the extra night: +1 on edge 0, nothing else.
  const d0 = supp.delta.edgeDelta.find((e) => e.edgeIndex === 0);
  const d1 = supp.delta.edgeDelta.find((e) => e.edgeIndex === 1);
  assert.equal(d0.before, 2);
  assert.equal(d0.after, 3);
  assert.equal(d0.delta, 1);
  assert.equal(d1.delta, 0);

  // And "previously fully offset" now false.
  assert.equal(supp.delta.fullyOffsetBefore, true);
  assert.equal(supp.delta.fullyOffsetAfter, false);

  const stmts = listStatements(store, c.id);
  assert.equal(stmts.length, 2);
  assert.equal(stmts[0].kind, 'final');
  assert.equal(stmts[1].kind, 'supplementary');
  // The final statement still says fully offset.
  assert.equal(stmts[0].summary.fullyOffset, true);
});

test('supplementary close cannot count nights twice', () => {
  const store = createStore();
  const c = pair(store);
  stay(store, c.id, 0, ['2026-11-27', '2026-11-28']);
  stay(store, c.id, 1, ['2026-12-04', '2026-12-05']);
  const final = closeContract(store, c.id);
  assert.equal(final.settlement.totalConfirmedNights, 4);

  reopenContract(store, c.id);
  // Re-report the SAME nights (not new) and close again.
  stay(store, c.id, 0, ['2026-11-27', '2026-11-28']);
  stay(store, c.id, 1, ['2026-12-04', '2026-12-05']);
  const supp = closeContract(store, c.id);
  assert.equal(supp.settlement.totalConfirmedNights, 4); // not 8
  for (const e of supp.delta.edgeDelta) assert.equal(e.delta, 0);
});
