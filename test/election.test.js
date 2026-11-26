import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Store } from '../src/store.js';
import { seedScenario } from '../src/fixtures.js';
import { auditBallots, dutyRoster } from '../src/election.js';

function newStore() {
  const dir = mkdtempSync(join(tmpdir(), 'joint-election-'));
  return new Store(join(dir, 'db.json'));
}

function seedAndSettle(name, options = {}) {
  const store = newStore();
  const roundId = options.roundId ?? 'r1';
  store.transact({}, () => seedScenario(store, name, { ...options, roundId }));
  const outcome = store.transact({}, () => store.settleRound(roundId));
  return { store, round: store.getRound(roundId), outcome: outcome.body };
}

test('原四票：甲阅览用 b1/b2，甲球场意愿不符后 b3/b4 转给乙球场', () => {
  const { round, outcome } = seedAndSettle('transfer');
  assert.equal(outcome.settlement.appointments.length, 2);

  const reading = round.appointments.find((a) => a.seatId === 'reading');
  const court = round.appointments.find((a) => a.seatId === 'court');
  assert.equal(reading.personId, 'jia');
  assert.deepEqual(reading.ballotIds, ['b1', 'b2']);
  assert.equal(court.personId, 'yi');
  assert.deepEqual(court.ballotIds, ['b3', 'b4']);

  const destinations = Object.fromEntries(outcome.settlement.destinations.map((d) => [d.ballotId, d]));
  assert.equal(destinations.b1.status, 'used');
  assert.equal(destinations.b2.seatId, 'reading');
  assert.equal(destinations.b3.status, 'used');
  assert.equal(destinations.b3.personId, 'yi');
  assert.deepEqual(
    destinations.b3.trail.map((x) => x.reason),
    ['unwilling', 'quota_selected_by_ticket_order'],
  );
  assert.deepEqual(
    destinations.b4.trail.map((x) => x.reason),
    ['unwilling', 'quota_selected_by_ticket_order'],
  );

  const roster = dutyRoster(round);
  assert.deepEqual(roster.map((s) => [s.seatId, s.status, s.officeholder?.id]), [
    ['reading', 'filled', 'jia'],
    ['court', 'filled', 'yi'],
  ]);
});

test('球场后项改为无资格丙：甲仍任阅览，两张球场票无可用后项，不产生乙', () => {
  const { round, outcome } = seedAndSettle('ineligible-backup');
  assert.deepEqual(
    round.appointments.map((a) => [a.seatId, a.personId, a.ballotIds]),
    [['reading', 'jia', ['b1', 'b2']]],
  );

  const destinations = Object.fromEntries(outcome.settlement.destinations.map((d) => [d.ballotId, d]));
  for (const id of ['b3', 'b4']) {
    assert.equal(destinations[id].status, 'unused_no_viable_choice');
    assert.deepEqual(
      destinations[id].trail.map((x) => x.reason),
      ['unwilling', 'ineligible'],
    );
  }
  assert.equal(auditBallots(round).filter((d) => d.personId === 'yi' && d.status === 'used').length, 0);

  const courtSeat = dutyRoster(round).find((s) => s.seatId === 'court');
  assert.equal(courtSeat.status, 'vacant');
  assert.equal(courtSeat.officeholder, null);
});

test('乙拒绝有效就任承诺：即使投了乙也不产生就任书或值班证', () => {
  const { round, outcome } = seedAndSettle('refused-backup');
  assert.deepEqual(
    round.appointments.map((a) => [a.seatId, a.personId]),
    [['reading', 'jia']],
  );
  assert.equal(round.appointments.some((a) => a.personId === 'yi'), false);

  const b3 = outcome.settlement.destinations.find((d) => d.ballotId === 'b3');
  assert.deepEqual(
    b3.trail.map((x) => x.reason),
    ['unwilling', 'refused'],
  );

  const roster = dutyRoster(round);
  assert.equal(roster.find((s) => s.seatId === 'court').status, 'vacant');
});

test('甲愿任两席但偏好阅览：只发一张书，球场票因甲已就任而不消耗，球席空着等待', () => {
  const { round, outcome } = seedAndSettle('both-willing', { preference: 'reading' });
  assert.deepEqual(
    round.appointments.map((a) => [a.seatId, a.personId, a.ballotIds]),
    [['reading', 'jia', ['b1', 'b2']]],
  );

  const b3 = outcome.settlement.destinations.find((d) => d.ballotId === 'b3');
  assert.equal(b3.status, 'unused_no_viable_choice');
  assert.deepEqual(
    b3.trail.map((x) => x.reason),
    ['person_already_seated'],
  );

  const roster = dutyRoster(round);
  assert.equal(roster.find((s) => s.seatId === 'reading').status, 'filled');
  assert.equal(roster.find((s) => s.seatId === 'court').status, 'vacant');
});

test('偏好球场时规则对称：甲任球场，阅览席空', () => {
  const { round } = seedAndSettle('both-willing', { preference: 'court' });
  assert.deepEqual(
    round.appointments.map((a) => [a.seatId, a.personId, a.ballotIds]),
    [['court', 'jia', ['b3', 'b4']]],
  );
  assert.equal(dutyRoster(round).find((s) => s.seatId === 'reading').status, 'vacant');
});

test('甲就偏好席后，另一席的甲项会把票转给后项乙；若乙达额则球席可同时补齐', () => {
  const { round } = seedAndSettle('both-willing', {
    roundId: 'r-backup',
    preference: 'reading',
    withBackup: true,
  });
  assert.deepEqual(
    round.appointments.map((a) => [a.seatId, a.personId, a.ballotIds]),
    [
      ['reading', 'jia', ['b1', 'b2']],
      ['court', 'yi', ['b3', 'b4']],
    ],
  );
  const audit = Object.fromEntries(auditBallots(round).map((d) => [d.ballotId, d]));
  assert.equal(audit.b3.status, 'used');
  assert.equal(audit.b3.personId, 'yi');
  assert.deepEqual(audit.b3.trail.map((x) => x.reason), [
    'person_already_seated',
    'quota_selected_by_ticket_order',
  ]);
});

test('超过定额时公开取最早两张，第三张不被消耗并继续后项', () => {
  const { round } = seedAndSettle('overquota');
  const reading = round.appointments.find((a) => a.seatId === 'reading');
  assert.deepEqual(reading.ballotIds, ['b1', 'b2']);

  const audit = Object.fromEntries(auditBallots(round).map((d) => [d.ballotId, d]));
  assert.equal(audit.b3.status, 'unused_no_viable_choice');
  assert.equal(audit.b3.trail[0].reason, 'quota_filled_by_earlier_tickets');
  assert.deepEqual(audit.b3.trail[0].selectedBallotIds, ['b1', 'b2']);
});
