import { test } from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';
import { createStore } from '../src/store.js';
import { hh, app, stay } from './helpers.js';
import { signContract, contractSettlement, reportNights, confirmBooking } from '../src/contracts.js';

test('restart: debts survive reload; re-reporting after restart does not double count', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ehs-'));
  const file = path.join(dir, 'state.json');

  const store = createStore({ file });
  hh(store, 'a'); hh(store, 'b');
  const all = ['2026-11-20', '2026-11-21', '2026-11-27', '2026-11-28'];
  app(store, 'a', { nights: all });
  app(store, 'b', { nights: all });
  const c = signContract(store, {
    type: 'pair', households: ['a', 'b'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: ['2026-11-27', '2026-11-28'] },
      { host: 'b', guest: 'a', plannedNights: ['2026-11-20', '2026-11-21'] },
    ],
  });
  stay(store, c.id, 0, ['2026-11-27', '2026-11-28']);
  stay(store, c.id, 1, ['2026-11-20']); // one short

  // Two households re-report the SAME weekend on the live service.
  stay(store, c.id, 0, ['2026-11-27', '2026-11-28']);
  stay(store, c.id, 1, ['2026-11-20', '2026-11-21']); // now b stayed both too
  store.flush();
  const before = contractSettlement(store, c.id);

  // "Disconnect service": fresh process, reload state from disk.
  const store2 = createStore({ file });
  const after = contractSettlement(store2, c.id);
  assert.deepEqual(after.debts, before.debts);
  assert.equal(after.totalConfirmedNights, 4);
  assert.equal(after.fullyOffset, true);
  // No re-counting from history.
  assert.equal(after.edges[0].actualCount, 2);
  assert.equal(after.edges[1].actualCount, 2);

  // Re-report again after restart; still 4.
  stay(store2, c.id, 0, ['2026-11-27', '2026-11-28']);
  stay(store2, c.id, 1, ['2026-11-20', '2026-11-21']);
  const after2 = contractSettlement(store2, c.id);
  assert.equal(after2.totalConfirmedNights, 4);

  fs.rmSync(dir, { recursive: true, force: true });
});

test('regroup: before/after obligations queryable across a restart', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ehs-'));
  const file = path.join(dir, 'state.json');
  const store = createStore({ file });
  for (const id of ['a', 'b', 'c']) hh(store, id);
  const w1 = ['2026-12-04', '2026-12-05'];
  const w2 = ['2026-12-11', '2026-12-12'];
  const w3 = ['2026-12-18', '2026-12-19'];
  for (const id of ['a', 'b', 'c']) app(store, id, { nights: [...w1, ...w2, ...w3] });
  const c = signContract(store, {
    type: 'ring', households: ['a', 'b', 'c'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: w1 },
      { host: 'b', guest: 'c', plannedNights: w2 },
      { host: 'c', guest: 'a', plannedNights: w3 },
    ],
  });
  stay(store, c.id, 0, w1);
  stay(store, c.id, 1, w2);   // c gets 2 at b
  // c's hosting of a not yet stayed -> c owes / b carries etc.
  store.flush();
  const before = contractSettlement(store, c.id);
  // a hosted b (2), received nothing yet -> carrying. b hosted c (2) and was
  // hosted by a (2) -> balanced. c received from b, hosted nobody -> owing.
  assert.deepEqual(before.carryingHouseholds.sort(), ['a']);
  assert.deepEqual(before.owingHouseholds.sort(), ['c']);

  const store2 = createStore({ file });
  const after = contractSettlement(store2, c.id);
  assert.deepEqual(after.debts, before.debts);
  assert.deepEqual(after.carryingHouseholds.sort(), ['a']);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('dispute pending confirmation does not count; after mutual confirm it does', () => {
  const store = createStore();
  hh(store, 'a'); hh(store, 'b');
  const nights = ['2026-12-04'];
  const later = ['2026-12-11', '2026-12-12'];
  app(store, 'a', { nights: [...nights, ...later] });
  app(store, 'b', { nights: [...nights, ...later] });
  const c = signContract(store, {
    type: 'pair', households: ['a', 'b'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: nights },
      { host: 'b', guest: 'a', plannedNights: later },
    ],
  });
  reportNights(store, c.id, 0, 'host', nights, { confirm: false });
  reportNights(store, c.id, 0, 'guest', nights, { confirm: false });
  // both reported the same nights but neither confirmed -> not counted
  assert.equal(contractSettlement(store, c.id).edges[0].actualCount, 0);
  confirmBooking(store, c.id, 0, 'host');
  // only one side confirmed -> still not counted
  assert.equal(contractSettlement(store, c.id).edges[0].actualCount, 0);
  confirmBooking(store, c.id, 0, 'guest');
  // both sides confirmed the same booking -> counted
  assert.equal(contractSettlement(store, c.id).edges[0].actualCount, 1);
});
