import test from 'node:test';
import assert from 'node:assert/strict';
import { Observatory, MAX_STATIONS_PER_ACTIVITY } from '../src/domain.js';
import {
  intersectUnions,
  subtractHoles,
  sumSeconds,
} from '../src/time.js';

function workedExample() {
  const obs = new Observatory();
  obs.registerStation({ id: 'S1', agreedOffset: 0 });
  obs.registerStation({ id: 'S2', agreedOffset: 0 });
  obs.createCommission({
    id: 'C3',
    phenomenonInterval: [4, 8],
    requiredCommonSeconds: 3,
    stationIds: ['S1', 'S2'],
    background: { before: 1, after: 1 },
  });
  obs.submitFrame('S1', { id: 'T1', mode: 'target', localInterval: [4, 8] });
  obs.submitFrame('S2', { id: 'T2', mode: 'target', localInterval: [5, 8] });
  obs.submitFrame('S1', { id: 'B1', mode: 'background', localInterval: [3, 4] });
  obs.submitFrame('S2', { id: 'B2', mode: 'background', localInterval: [4, 5] });
  return obs;
}

test('half-open intervals: dropped frames become holes and intersections do not sum gaps', () => {
  const pieces = subtractHoles({ start: 4, end: 8 }, [[5, 6]]);
  assert.deepEqual(pieces, [{ start: 4, end: 5 }, { start: 6, end: 8 }]);
  assert.equal(sumSeconds(pieces), 3);

  const overlap = intersectUnions([{ start: 0, end: 3 }], [{ start: 4, end: 7 }]);
  assert.deepEqual(overlap, []);
  assert.equal(sumSeconds([{ start: 0, end: 3 }, { start: 4, end: 7 }]), 6);
  assert.equal(sumSeconds(overlap), 0);
});

test('exact story: [4,8)/[5,8) gives three common seconds with backgrounds', () => {
  const obs = workedExample();
  const evaluation = obs.evaluateCommission('C3');
  assert.equal(evaluation.commonSeconds, 3);
  assert.deepEqual(evaluation.commonIntervals, [{ start: 5, end: 8 }]);
  assert.equal(evaluation.backgroundSupportedSeconds, 3);
  assert.equal(evaluation.qualified, true);
});

test('clock correction shifts only S2 to [6,9), reducing clipped overlap to two seconds', () => {
  const obs = workedExample();
  obs.setClock('S2', { agreedOffset: 1, reason: 'correct second sync' });
  const corrected = obs.evaluateCommission('C3');
  assert.deepEqual(corrected.stationEvidence[1].targetFrameEvidence[0].commonNominalInterval, { start: 6, end: 9 });
  assert.deepEqual(corrected.commonIntervals, [{ start: 6, end: 8 }]);
  assert.equal(corrected.commonSeconds, 2);
  assert.equal(corrected.qualified, false);

  obs.setClock('S2', { agreedOffset: 0 });
  assert.equal(obs.evaluateCommission('C3').commonSeconds, 3);
});

test('changing requirement to four recomputes and cannot reuse the three-second conclusion', () => {
  const obs = workedExample();
  assert.equal(obs.evaluateCommission('C3').qualified, true);
  obs.updateCommission('C3', { requiredCommonSeconds: 4 });
  const evaluation = obs.evaluateCommission('C3');
  assert.equal(evaluation.requiredCommonSeconds, 4);
  assert.equal(evaluation.commonSeconds, 3);
  assert.equal(evaluation.qualified, false);
  assert.ok(evaluation.reasons.includes('INSUFFICIENT_COMMON_SECONDS'));
});

test('staggered three-second observations have zero joint seconds and are not additive', () => {
  const obs = new Observatory();
  obs.registerStation({ id: 'A' });
  obs.registerStation({ id: 'B' });
  obs.createCommission({
    id: 'STAG',
    phenomenonInterval: [30, 40],
    requiredCommonSeconds: 3,
    stationIds: ['A', 'B'],
    background: { before: 1, after: 1 },
  });
  obs.submitFrame('A', { mode: 'target', localInterval: [31, 34] });
  obs.submitFrame('B', { mode: 'target', localInterval: [36, 39] });
  obs.submitFrame('A', { mode: 'background', localInterval: [30, 31] });
  obs.submitFrame('B', { mode: 'background', localInterval: [39, 40] });

  const e = obs.evaluateCommission('STAG');
  assert.equal(e.individualTotalSeconds, 6);
  assert.equal(e.commonSeconds, 0);
  assert.equal(e.additiveSecondsRejected, true);
  assert.equal(e.qualified, false);

  const decision = obs.closeCommission('STAG');
  assert.equal(decision.result, 'pending_makeup');
  assert.equal(obs.getCommission('STAG').status, 'pending_makeup');
});

test('background distance is enforced in both directions', () => {
  const obs = new Observatory();
  obs.registerStation({ id: 'A' });
  obs.registerStation({ id: 'B' });
  obs.createCommission({
    id: 'BG',
    phenomenonInterval: [0, 5],
    requiredCommonSeconds: 2,
    stationIds: ['A', 'B'],
    background: { before: 1, after: 1 },
  });
  obs.submitFrame('A', { mode: 'target', localInterval: [0, 3] });
  obs.submitFrame('B', { mode: 'target', localInterval: [0, 3] });
  obs.submitFrame('A', { mode: 'background', localInterval: [-5, -4] }); // gap 4, too far
  obs.submitFrame('B', { mode: 'background', localInterval: [3, 4] });

  const e = obs.evaluateCommission('BG');
  assert.equal(e.commonSeconds, 3);
  assert.equal(e.backgroundSupportedSeconds, 0);
  assert.ok(e.reasons.includes('BACKGROUND_GAP'));
  assert.equal(e.qualified, false);
});

test('adjacent source frames with separate backgrounds are partitioned correctly', () => {
  const obs = new Observatory();
  obs.registerStation({ id: 'A' });
  obs.registerStation({ id: 'B' });
  obs.createCommission({
    id: 'SPLIT-BG',
    phenomenonInterval: [0, 6],
    requiredCommonSeconds: 6,
    stationIds: ['A', 'B'],
    background: { before: 1, after: 1 },
  });
  obs.submitFrame('A', { mode: 'target', localInterval: [0, 6] });
  obs.submitFrame('A', { mode: 'background', localInterval: [-1, 0] });
  obs.submitFrame('B', { id: 'B1', mode: 'target', localInterval: [0, 3] });
  obs.submitFrame('B', { id: 'B2', mode: 'target', localInterval: [3, 6] });
  obs.submitFrame('B', { mode: 'background', localInterval: [-1, 0] });
  obs.submitFrame('B', { mode: 'background', localInterval: [6, 7] });

  const evaluation = obs.evaluateCommission('SPLIT-BG');
  assert.equal(evaluation.commonSeconds, 6);
  assert.equal(evaluation.commonSegments.length, 2);
  assert.equal(evaluation.backgroundSupportedSeconds, 6);
  assert.equal(evaluation.qualified, true);
});

test('confirmed windows atomically reserve slots under competing commissions and are non-transferable', () => {
  const obs = new Observatory();
  obs.registerStation({ id: 'A' });
  obs.registerStation({ id: 'B' });
  for (const id of ['C1', 'C2']) {
    obs.createCommission({
      id,
      phenomenonInterval: [50, 54],
      requiredCommonSeconds: 3,
      stationIds: ['A', 'B'],
    });
  }
  obs.createWindow({ id: 'W', stationIds: ['A', 'B'], interval: [50, 54], status: 'proposed' });
  assert.throws(
    () => obs.dispatchFromWindow({ commissionId: 'C1', windowId: 'W' }),
    (err) => err.code === 'WINDOW_NOT_CONFIRMED',
  );
  obs.confirmWindow('W');

  const first = obs.dispatchFromWindow({ commissionId: 'C1', windowId: 'W' });
  assert.equal(first.status, 'held');
  assert.throws(
    () => obs.dispatchFromWindow({ commissionId: 'C2', windowId: 'W' }),
    (err) => err.code === 'SLOT_CONFLICT',
  );
  assert.throws(
    () => obs.transferDispatch(),
    (err) => err.code === 'NON_TRANSFERABLE_DISPATCH',
  );

  obs.cancelDispatch(first.id, { reason: 'station lost' });
  const replacement = obs.dispatchFromWindow({ commissionId: 'C2', windowId: 'W' });
  assert.equal(replacement.commissionId, 'C2');
});

test('relay calculation uses notification delay, receipt delay and slew time only', () => {
  const obs = new Observatory();
  const plan = obs.planRelay({
    forecast: { sentAt: 10, predictedInterval: [14, 20] },
    notificationDelaySeconds: 1,
    receiptDelaySeconds: 1,
    stationId: 'R',
    slewSeconds: 2,
    availableInterval: [10, 30],
  });
  assert.equal(plan.timeline.orderReceivedAt, 11);
  assert.equal(plan.timeline.commanderReceiptAt, 12);
  assert.equal(plan.candidates[0].readyAt, 13);
  assert.deepEqual(plan.candidates[0].catchableInterval, { start: 14, end: 20 });

  const late = obs.planRelay({
    forecast: { sentAt: 10, predictedInterval: [14, 20] },
    notificationDelaySeconds: 1,
    stationId: 'R2',
    slewSeconds: 10,
  });
  assert.equal(late.candidates[0].tooLate, true);
  assert.throws(
    () => obs.dispatchRelay({
      forecast: { sentAt: 10, predictedInterval: [14, 20] },
      notificationDelaySeconds: 1,
      stationId: 'R3',
      slewSeconds: 10,
    }),
    (err) => err.code === 'NO_TIMELY_CANDIDATE',
  );
});

test('duplicate frame delivery leaves sealed original report readable and unchanged', () => {
  const obs = workedExample();
  const closed = obs.closeCommission('C3', { host: 'host' });
  const reportId = closed.report.id;
  const before = obs.getReport(reportId);

  const duplicate = obs.submitFrame('S2', {
    id: 'T2',
    mode: 'target',
    localInterval: [5, 8],
  }, 500);
  assert.equal(duplicate.duplicate, true);
  assert.equal(duplicate.deliveries, 2);
  const again = obs.submitFrame('S2', { id: 'T2', mode: 'target', localInterval: [5, 8] });
  assert.equal(again.duplicate, true);
  assert.equal(again.deliveries, 3);

  const after = obs.getReport(reportId);
  assert.equal(after.checksum, before.checksum);
  assert.equal(after.sealed, true);
  assert.equal(after.evaluation.commonSeconds, 3);
  assert.ok(obs.logicalClock >= 501);
});

test(`an activity accepts at most ${MAX_STATIONS_PER_ACTIVITY} stations`, () => {
  const obs = new Observatory();
  assert.throws(
    () => obs.createCommission({
      id: 'FIVE',
      phenomenonInterval: [0, 10],
      requiredCommonSeconds: 2,
      stationIds: ['S1', 'S2', 'S3', 'S4', 'S5'],
    }),
    /最多 4 站/,
  );
});

test('seed contains one sealed dual-station measurement and one pending makeup commission', () => {
  const obs = new Observatory({ seed: true });
  assert.equal(obs.getCommission('C-DONE').status, 'sealed');
  assert.equal(obs.getCommission('C-MAKEUP').status, 'pending_makeup');
  assert.equal(obs.getReport('RPT-C-DONE-V1').sealed, true);
  const trace = obs.traceCommission('C-DONE');
  assert.ok(trace.commonSeconds >= 3);
  assert.ok(trace.dispatchReceipts.length >= 1);
  assert.ok(trace.reports.length >= 1);
});
