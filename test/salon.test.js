import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { SalonService, HttpError } from '../src/service.js';

function tempService() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'salon-test-'));
  return new SalonService({ dataFile: path.join(dir, 'salon.json') });
}

function seedExample(service) {
  service.addStaff({ id: 'lin', name: '林师傅', canUnroll: true });
  service.addChair({ id: 'chair1', name: '烫卷椅一' });
  service.addChair({ id: 'chair2', name: '剪刘海椅' });

  const booked = service.bookAppointment({
    id: 'perm1',
    customerName: '烫卷客',
    signature: '烫卷客',
    mode: 'principal',
    principalStylistId: 'lin',
    sections: [{ id: 'last-zone', name: '最后一区' }],
    plannedRollCompleteAt: 0,
    unroll: { durationMinutes: 2, minWaitMinutes: 3, maxWaitMinutes: 4 },
  });
  assert.equal(booked.feasibility.feasible, true);
  service.checkIn('perm1', { at: 0, chairId: 'chair1' });
  service.completeSection('perm1', 'last-zone', { at: 0, stylistId: 'lin' });
  return booked.appointment;
}

test('accepts the three-minute bang in the original gap', () => {
  const service = tempService();
  seedExample(service);

  const quote = service.quoteInsertion({
    targetAppointmentId: 'perm1',
    stylistId: 'lin',
    durationMinutes: 3,
    availableFrom: 0,
    availableUntil: 3,
    chairId: 'chair2',
  });

  assert.equal(quote.feasible, true);
  assert.equal(quote.start, 0);
  assert.equal(quote.end, 3);
  assert.equal(quote.chairId, 'chair2');
  assert.equal(quote.assignments.find((a) => a.appointmentId === 'perm1')?.start, 3);

  const confirmed = service.confirmInsertion({
    customerName: '刘海客',
    stylistId: 'lin',
    durationMinutes: 3,
    availableFrom: 0,
    availableUntil: 3,
    chairId: 'chair2',
    targetAppointmentId: 'perm1',
  });
  assert.equal(confirmed.appointment.status, 'booked');
  assert.equal(confirmed.quote.promise.canPromise, true);

  // A two-minute job at minute 3 overlaps every possible unroll start [3,4].
  const check = service.quoteInsertion({ stylistId: 'lin', durationMinutes: 2, startAt: 3, chairId: 'chair2' });
  assert.equal(check.feasible, false);
});

test('two-minute-early roll completion removes the original insertion promise', () => {
  const service = tempService();
  seedExample(service);
  service.completeSection('perm1', 'last-zone', { at: -2, stylistId: 'lin' });

  const perm = service.findAppointment('perm1');
  assert.deepEqual(perm.unrollWindowSnapshot, {
    earliestStart: 1,
    latestStart: 2,
    rollBase: -2,
    kind: 'actual-relative',
  });

  const quote = service.quoteInsertion({
    stylistId: 'lin',
    durationMinutes: 3,
    availableFrom: 0,
    availableUntil: 3,
    chairId: 'chair2',
  });
  assert.equal(quote.feasible, false);
  assert.match(quote.code, /CONFLICT|FEASIBLE|COMBINATION|CLOSED/);
});

test('a chair that becomes free only at minute 2 blocks the original insertion', () => {
  const service = tempService();
  seedExample(service);

  // Restore the original process receipt after the early-roll mutation.
  service.completeSection('perm1', 'last-zone', { at: 0, stylistId: 'lin' });
  service.updateChair('chair2', { unavailable: [{ start: 0, end: 2, reason: 'occupied until minute 2' }] });

  const quote = service.quoteInsertion({
    stylistId: 'lin',
    durationMinutes: 3,
    availableFrom: 0,
    availableUntil: 3,
    chairId: 'chair2',
  });
  assert.equal(quote.feasible, false);
  assert.equal(quote.code, 'FIXED_CONFLICT');
  assert.equal(quote.fixedConflicts[0].code, 'CHAIR_BUSY');
});

test('persists realized segments and gives the same insertion answer after restart', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'salon-restart-'));
  const file = path.join(dir, 'salon.json');
  let service = new SalonService({ dataFile: file });
  seedExample(service);
  service.confirmInsertion({
    customerName: '刘海客',
    stylistId: 'lin',
    durationMinutes: 3,
    availableFrom: 0,
    availableUntil: 3,
    chairId: 'chair2',
  });
  service.completeSection('perm1', 'last-zone', { at: 0, stylistId: 'lin' });

  service = new SalonService({ dataFile: file });
  const receipt = service.getReceipt('perm1');
  const states = Object.fromEntries(receipt.segments.map((s) => [s.segment, s.state]));
  assert.equal(states['section:last-zone'], 'realized');
  assert.equal(states.roll_complete, 'realized');
  assert.equal(states.unroll_window, 'protected');

  const quote = service.quoteInsertion({
    stylistId: 'lin',
    durationMinutes: 2,
    availableFrom: 3,
    availableUntil: 5,
  });
  // At 3 Lin must unroll the perm; a two-minute job cannot be inserted over it.
  assert.equal(quote.feasible, false);
  assert.ok(fs.existsSync(file));
});

test('one qualified unroller is the minimal case; added stylist supports substitute recovery', () => {
  const service = tempService();
  seedExample(service);

  // Lin is suddenly occupied/off-shift from minute 3 through 5.
  service.updateStaffSchedule('lin', { timeOff: [{ start: 3, end: 5, reason: 'temporary absence' }] });
  let recovery = service.recover({ at: 3 });
  assert.equal(recovery.feasible, false);
  assert.equal(recovery.rescueOrder[0].status, 'sacrifice_or_rebook');
  assert.equal(recovery.rescueOrder[0].substituteOptions.length, 0);

  service.addStaff({ id: 'zhou', name: '周师傅', canUnroll: true });
  recovery = service.recover({ at: 3 });
  assert.equal(recovery.rescueOrder[0].status, 'needs_substitute_acceptance');
  assert.equal(recovery.rescueOrder[0].substituteOptions[0].stylistId, 'zhou');

  assert.throws(() => service.startUnroll('perm1', { at: 3, stylistId: 'zhou' }), (err) => {
    assert.equal(err instanceof HttpError, true);
    assert.equal(err.code, 'HANDOFF_NOT_ACCEPTED');
    return true;
  });

  assert.throws(() => service.acceptHandoff('perm1', {
    at: 3,
    stylistId: 'zhou',
    confirmState: true,
    acceptCompletedSections: true,
    chairId: 'chair1',
  }), (err) => err.code === 'SUBSTITUTE_CONSENT_REQUIRED');

  const handoff = service.acceptHandoff('perm1', {
    at: 3,
    stylistId: 'zhou',
    confirmState: true,
    acceptCompletedSections: true,
    chairId: 'chair1',
    customerConsent: true,
    consentSignature: '烫卷客',
  });
  assert.equal(handoff.feasibility.feasible, true);

  service.startUnroll('perm1', { at: 3, stylistId: 'zhou' });
  service.completeUnroll('perm1', { at: 5, stylistId: 'zhou' });
  const receipt = service.checkout('perm1', { at: 6, amount: 680, method: 'card' });
  assert.equal(receipt.status, 'checked_out');
  assert.equal(receipt.segments.find((s) => s.segment === 'checkout').state, 'realized');
});

test('movement clearance and chair moves release the old chair', () => {
  const service = tempService();
  seedExample(service);
  service.assessMobility('perm1', { at: 0, stylistId: 'lin', allowed: true, reason: 'rollers stable' });

  const movable = service.movableChairs('perm1', { at: 0, durationMinutes: 2 });
  assert.equal(movable.movable, true);
  assert.deepEqual(movable.chairs.map((c) => c.id), ['chair2']);

  service.moveChair('perm1', { at: 0, toChairId: 'chair2', stylistId: 'lin', confirmMovable: true });
  assert.equal(service.findAppointment('perm1').chairId, 'chair2');

  const requestedBusyChair = service.quoteInsertion({
    stylistId: 'lin', durationMinutes: 3, startAt: 0, availableUntil: 3, chairId: 'chair2',
  });
  assert.equal(requestedBusyChair.feasible, false);

  const anyChair = service.quoteInsertion({
    stylistId: 'lin', durationMinutes: 3, startAt: 0, availableUntil: 3,
  });
  assert.equal(anyChair.feasible, true);
  assert.equal(anyChair.chairId, 'chair1');
});

test('recovery prioritizes the salvageable ticket with the earliest latest start', () => {
  const service = tempService();
  service.addStaff({ id: 'lin', name: '林师傅', canUnroll: true });
  service.addChair({ id: 'c1', name: '椅1' });
  service.bookAppointment({
    id: 'late-deadline', customerName: '晚窗口', mode: 'principal', principalStylistId: 'lin',
    sections: [{ id: 'z' }], plannedRollCompleteAt: 7,
    unroll: { durationMinutes: 2, minWaitMinutes: 2, maxWaitMinutes: 3 }, // window 9..10
  });
  service.bookAppointment({
    id: 'early-deadline', customerName: '早窗口', mode: 'principal', principalStylistId: 'lin',
    sections: [{ id: 'z' }], plannedRollCompleteAt: 0,
    unroll: { durationMinutes: 2, minWaitMinutes: 3, maxWaitMinutes: 4 }, // window 3..4
  });

  const recovery = service.recover({ at: 0 });
  assert.deepEqual(recovery.rescueOrder.map((x) => x.appointmentId), ['early-deadline', 'late-deadline']);
  assert.equal(recovery.feasible, true);
});

test('process schedule requires personal handoff acceptance and stale state reconfirmation', () => {
  const service = tempService();
  service.addStaff({ id: 'lin', name: '林师傅', canUnroll: true });
  service.addStaff({ id: 'zhou', name: '周师傅', canUnroll: true });
  service.addChair({ id: 'c1', name: '椅1' });
  service.bookAppointment({
    id: 'proc1',
    customerName: '工序客',
    mode: 'process',
    sections: [{ id: 'a' }, { id: 'b' }],
    plannedRollCompleteAt: 10,
    unroll: { durationMinutes: 2, minWaitMinutes: 3, maxWaitMinutes: 4 },
  });
  service.checkIn('proc1', { at: 0, chairId: 'c1' });
  service.completeSection('proc1', 'a', { at: 9, stylistId: 'lin' });
  service.completeSection('proc1', 'b', { at: 10, stylistId: 'lin' });

  assert.throws(() => service.startUnroll('proc1', { at: 13, stylistId: 'zhou' }), (err) => err.code === 'HANDOFF_NOT_ACCEPTED');
  service.acceptHandoff('proc1', {
    at: 12,
    stylistId: 'zhou',
    confirmState: true,
    acceptCompletedSections: true,
    chairId: 'c1',
  });

  // A late correction changes the section receipt/hash.
  service.completeSection('proc1', 'a', { at: 8, stylistId: 'lin', workMinutes: 4 });
  assert.throws(() => service.startUnroll('proc1', { at: 13, stylistId: 'zhou' }), (err) => err.code === 'HANDOFF_STATE_STALE');
  service.acceptHandoff('proc1', {
    at: 12,
    stylistId: 'zhou',
    confirmState: true,
    acceptCompletedSections: true,
    chairId: 'c1',
  });
  service.startUnroll('proc1', { at: 13, stylistId: 'zhou' });
  assert.equal(service.findAppointment('proc1').status, 'in_unroll');
});
