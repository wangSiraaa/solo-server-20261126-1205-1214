import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from '../src/store.js';
import { hh, app, stay } from './helpers.js';
import { setApplication } from '../src/domain.js';
import {
  signContract, reportNights, confirmBooking, contractSettlement,
  getDispute, cancelNight, reviseContract,
} from '../src/contracts.js';

// Two different weekends: b visits a first, a reciprocates later.
const WAB = ['2026-12-04', '2026-12-05'];
const WBA = ['2026-12-11', '2026-12-12'];

test('date shrink: two consecutive away nights can no longer be signed', () => {
  const store = createStore();
  hh(store, 'a'); hh(store, 'b');
  app(store, 'a', { nights: [...WAB, ...WBA], minConsecutiveNights: 2 });
  app(store, 'b', { nights: [...WAB, ...WBA], minConsecutiveNights: 2 });

  const spec = {
    type: 'pair', households: ['a', 'b'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: WAB },
      { host: 'b', guest: 'a', plannedNights: WBA },
    ],
  };
  // Sanity: signable as-is.
  assert.doesNotThrow(() => signContract(store, spec));

  // Guest 'a' shortens its away window to a single night -> its 2-night visit
  // to b (WBA) can no longer be signed.
  setApplication(store, 'a', {
    travelNights: [WBA[0]], hostNights: [...WAB, ...WBA], minConsecutiveNights: 2,
  });
  assert.throws(() => signContract(store, spec), /cannot be signed/);
});

test('consent revoke: host removing combination consent blocks confirming original guest', () => {
  const store = createStore();
  // Partial parties (not whole family) so the same-night role rule does not
  // interfere; here we test consent only.
  hh(store, 'a'); hh(store, 'b');
  const nights = ['2026-12-04'];
  const later = ['2026-12-11'];
  app(store, 'a', { nights: [...nights, ...later], acceptedCompanions: [['b']], party: ['a-a'] });
  app(store, 'b', { nights: [...nights, ...later] });

  const c = signContract(store, {
    type: 'pair', households: ['a', 'b'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: nights },
      { host: 'b', guest: 'a', plannedNights: later },
    ],
  });

  // b reports it stayed at a's place.
  reportNights(store, c.id, 0, 'guest', nights, { confirm: false });

  // a revokes consent for combination [b].
  setApplication(store, 'a', { nights: [...nights, ...later], acceptedCompanions: [], party: ['a-a'] });

  // a can no longer confirm this booking.
  assert.throws(() => confirmBooking(store, c.id, 0, 'host'), /no longer supports/);
});

test('re-reporting the same weekend is idempotent (no double count)', () => {
  const store = createStore();
  hh(store, 'a'); hh(store, 'b');
  app(store, 'a', { nights: [...WAB, ...WBA] });
  app(store, 'b', { nights: [...WAB, ...WBA] });
  const c = signContract(store, {
    type: 'pair', households: ['a', 'b'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: WAB },
      { host: 'b', guest: 'a', plannedNights: WBA },
    ],
  });
  // Both households re-report the same weekends multiple times.
  for (let i = 0; i < 3; i += 1) {
    stay(store, c.id, 0, WAB);
    stay(store, c.id, 1, WBA);
  }
  const s = contractSettlement(store, c.id);
  assert.equal(s.edges[0].actualCount, 2);
  assert.equal(s.edges[1].actualCount, 2);
  assert.equal(s.totalConfirmedNights, 4);
  assert.equal(s.fullyOffset, true);
});

test('dispute: differing remembered nights are held, agreed nights count', () => {
  const store = createStore();
  hh(store, 'a'); hh(store, 'b');
  app(store, 'a', { nights: [...WAB, ...WBA] });
  app(store, 'b', { nights: [...WAB, ...WBA] });
  const c = signContract(store, {
    type: 'pair', households: ['a', 'b'],
    edges: [
      { host: 'a', guest: 'b', plannedNights: WAB },
      { host: 'b', guest: 'a', plannedNights: WBA },
    ],
  });
  // host a says b stayed both; guest b says it only stayed one.
  reportNights(store, c.id, 0, 'host', ['2026-12-04', '2026-12-05'], { confirm: true });
  reportNights(store, c.id, 0, 'guest', ['2026-12-04'], { confirm: true });
  confirmBooking(store, c.id, 0, 'host');
  confirmBooking(store, c.id, 0, 'guest');

  const d = getDispute(store, c.id, 0);
  assert.deepEqual(d.confirmedNights, ['2026-12-04']);
  assert.deepEqual(d.disputedNights, ['2026-12-05']);
  assert.deepEqual(d.disputeDetail.onlyHost, ['2026-12-05']);
  assert.deepEqual(d.disputeDetail.onlyGuest, []);

  // Host corrects its report to match; night resolves into confirmed.
  reportNights(store, c.id, 0, 'host', ['2026-12-04'], { confirm: true });
  confirmBooking(store, c.id, 0, 'host');
  const d2 = getDispute(store, c.id, 0);
  assert.deepEqual(d2.confirmedNights, ['2026-12-04']);
  assert.deepEqual(d2.disputedNights, []);
});

test('cancel a not-yet-stayed night releases it for rearrangement; stayed nights kept', () => {
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
  // a hosted b for both nights (stayed).
  stay(store, c.id, 0, w1);
  // c cancels the second night it was to host a.
  cancelNight(store, c.id, 2, w3[1]);
  // Cannot cancel a night already stayed.
  assert.throws(() => cancelNight(store, c.id, 0, w1[0]), /already confirmed/);

  const rev = reviseContract(store, c.id, { declines: ['b'] });
  // b declined -> never proposed as host or guest in new arrangement.
  for (const p of rev.proposals) {
    assert.notEqual(p.host, 'b');
    assert.notEqual(p.guest, 'b');
  }
  // kept stay preserved
  const kept = rev.keptConfirmedNights.find((k) => k.host === 'a');
  assert.deepEqual(kept.confirmedNights, w1);
});
