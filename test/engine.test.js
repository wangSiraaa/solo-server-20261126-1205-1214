'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { Engine, DomainError } = require('../src/engine');

// -----------------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------------
const ALL = ['lead', 'follow'];
const RHYTHMS = ['waltz', 'tango', 'salsa'];

function newEngine(opts) {
  return new Engine(opts);
}

/**
 * Open a fresh 3-round session in mode B with four versatile guests
 * 甲/001, 乙/002, 丙/003, 丁/004. Everyone accepts every rhythm.
 */
function baseScene() {
  const e = newEngine();
  e.createSession({ sessionId: 's', rounds: RHYTHMS, zones: ['z1', 'z2'] });
  e.registerGuest('s', { number: '001', name: '甲', roles: ALL, targetPartners: 2 });
  e.registerGuest('s', { number: '002', name: '乙', roles: ALL, targetPartners: 2 });
  e.registerGuest('s', { number: '003', name: '丙', roles: ALL, targetPartners: 1 });
  e.registerGuest('s', { number: '004', name: '丁', roles: ALL, targetPartners: 1 });
  e.setZone('s', { zone: 'z1', open: true });
  e.setZone('s', { zone: 'z2', open: true });
  return e;
}

/** Propose an invitation and have the invited guest confirm it. Returns inv. */
function invite(e, { round, from, to, fromRole, toRole, zone, source }) {
  const inv = e.proposeInvitation('s', { round, from, to, fromRole, toRole, zone, source });
  const res = e.confirmInvitation('s', { invitationId: inv.id, by: to });
  assert.equal(res.ok, true, `confirm of ${inv.id} should succeed`);
  return inv;
}

// =============================================================================
// 1. The headline rule: a role swap is NOT a new partner
// =============================================================================
test('role swap: 乙 changes role and re-dances 甲, 甲 still has ONE distinct partner', () => {
  const e = baseScene();

  // Round 1: 甲 leads, 乙 follows.
  invite(e, { round: 1, from: '001', to: '002', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  e.closeRound('s', { round: 1 });
  assert.deepEqual(e.metPartners('s', '001'), ['002']);

  // 乙 "swaps roles" (now leads) and dances 甲 again in round 2.
  invite(e, { round: 2, from: '002', to: '001', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  e.closeRound('s', { round: 2 });

  // Still only 乙 — role is irrelevant to the partner count.
  assert.deepEqual(e.metPartners('s', '001'), ['002']);
  const rec = e.meetingRecord('s', '001');
  assert.equal(rec.distinctPartners, 1);

  // ...but the meeting record DOES distinguish the role-swapped repeat.
  const b = rec.partners.find((p) => p.number === '002');
  assert.equal(b.dances.length, 2);
  assert.equal(b.dances[0].myRole, 'lead');
  assert.equal(b.dances[1].myRole, 'follow');
  assert.equal(b.dances[1].repeat, true);
  assert.equal(b.dances[1].roleSwapped, true);
});

test('then arranging 丙 and really dancing reaches TWO distinct partners', () => {
  const e = baseScene();

  invite(e, { round: 1, from: '001', to: '002', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  e.closeRound('s', { round: 1 });
  invite(e, { round: 2, from: '002', to: '001', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  e.closeRound('s', { round: 2 });
  assert.equal(e.meetingRecord('s', '001').distinctPartners, 1);

  // Round 3: 丙 leads, 甲 follows — a genuinely new partner.
  invite(e, { round: 3, from: '003', to: '001', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  e.closeRound('s', { round: 3 });

  assert.deepEqual(e.metPartners('s', '001'), ['002', '003']);
  assert.equal(e.meetingRecord('s', '001').distinctPartners, 2);
});

// =============================================================================
// 2. Rhythm permission blocks the count (counterfactual)
// =============================================================================
test('rhythmBlock: 甲 rejects 丙’s rhythm in round 2 -> floor untouched, cannot settle at 2', () => {
  const e = baseScene();

  // R1: 甲+乙 dance.
  invite(e, { round: 1, from: '001', to: '002', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  e.closeRound('s', { round: 1 });

  // R2: arrange 甲+丙 (tango) and confirm, THEN 甲 withdraws permission for tango.
  invite(e, { round: 2, from: '003', to: '001', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  e.setConsent('s', { number: '001', round: 2, rhythm: 'tango', accept: false });

  // Keep the floor exactly as-is (both zones still open).
  const before = e.exportSchedule('s');
  assert.equal(before.zones.find((z) => z.id === 'z1').open, true);

  const result = e.closeRound('s', { round: 2 });
  const outcome = result.outcomes.find((o) => o.status === 'invalidRhythm');
  assert.ok(outcome, 'the 甲/丙 invitation must be invalidRhythm at close');
  assert.equal(result.dances.length, 0, 'no dance was counted');

  assert.equal(e.metPartners('s', '001').length, 1, '甲 must still have only ONE partner');
  assert.equal(e.meetingRecord('s', '001').blocked.some((b) => b.status === 'invalidRhythm'), true);
});

// =============================================================================
// 3. Zone closed blocks the count (counterfactual)
// =============================================================================
test('zoneBlock: restore permission but close the only usable zone -> still cannot settle at 2', () => {
  const e = baseScene();

  invite(e, { round: 1, from: '001', to: '002', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  e.closeRound('s', { round: 1 });

  // R2: permission is fine (default), but the 甲/丙 booking is in z1 only.
  invite(e, { round: 2, from: '003', to: '001', fromRole: 'lead', toRole: 'follow', zone: 'z1' });

  // Close the one zone that could host these two; leave everything else identical.
  e.setZone('s', { zone: 'z1', open: false });

  const result = e.closeRound('s', { round: 2 });
  const outcome = result.outcomes.find((o) => o.status === 'zoneClosed');
  assert.ok(outcome, 'the 甲/丙 invitation must be zoneClosed at close');
  assert.equal(result.dances.length, 0);
  assert.equal(e.metPartners('s', '001').length, 1, '甲 must still have only ONE partner');
});

test('zoneBlock (single-zone session): the one and only zone being closed blocks it', () => {
  const e = newEngine();
  e.createSession({ sessionId: 's', rounds: RHYTHMS, zones: ['main'] });
  e.registerGuest('s', { number: '001', name: '甲', roles: ALL, targetPartners: 2 });
  e.registerGuest('s', { number: '002', name: '乙', roles: ALL, targetPartners: 1 });
  e.registerGuest('s', { number: '003', name: '丙', roles: ALL, targetPartners: 1 });
  e.setZone('s', { zone: 'main', open: true });

  e.proposeInvitation('s', { round: 1, from: '001', to: '002', fromRole: 'lead', toRole: 'follow', zone: 'main' });
  e.confirmInvitation('s', { invitationId: 'inv_s_1', by: '002' });
  e.closeRound('s', { round: 1 });

  e.proposeInvitation('s', { round: 2, from: '003', to: '001', fromRole: 'lead', toRole: 'follow', zone: 'main' });
  e.confirmInvitation('s', { invitationId: 'inv_s_2', by: '001' });
  e.setZone('s', { zone: 'main', open: false });
  e.closeRound('s', { round: 2 });

  assert.equal(e.metPartners('s', '001').length, 1);
});

// =============================================================================
// 4. Two leads book the same guest in two zones -> effective side is visible
// =============================================================================
test('twoLeads: same follower booked in two zones resolves to one effective dance', () => {
  const e = newEngine();
  e.createSession({ sessionId: 's', rounds: RHYTHMS, zones: ['z1', 'z2'] });
  e.registerGuest('s', { number: 'L1', name: '领队一', roles: ['lead'], targetPartners: 1 });
  e.registerGuest('s', { number: 'L2', name: '领队二', roles: ['lead'], targetPartners: 1 });
  e.registerGuest('s', { number: 'F', name: '跟舞者', roles: ['follow'], targetPartners: 2 });
  e.setZone('s', { zone: 'z1', open: true });
  e.setZone('s', { zone: 'z2', open: true });

  // Both leads register F in different zones for the same round.
  const inv1 = e.proposeInvitation('s', { round: 1, from: 'L1', to: 'F', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  const inv2 = e.proposeInvitation('s', { round: 1, from: 'L2', to: 'F', fromRole: 'lead', toRole: 'follow', zone: 'z2' });

  // F accepts both (near-simultaneous phones). First accepted is the effective one.
  const r1 = e.confirmInvitation('s', { invitationId: inv1.id, by: 'F' });
  const r2 = e.confirmInvitation('s', { invitationId: inv2.id, by: 'F' });
  assert.equal(r1.ok, true);
  assert.equal(r2.ok, false);
  assert.equal(r2.code, 'CONFLICT');
  assert.equal(r2.conflictWith, inv1.id);

  // Before close: the effective assignment is unambiguous.
  const eff = e.effectiveAssignment('s', { number: 'F', round: 1 });
  assert.equal(eff.effective.kind, 'confirmed-invitation');
  assert.equal(eff.effective.invitationId, inv1.id);
  assert.equal(eff.effective.partner, 'L1');
  assert.ok(eff.conflicts.includes(inv2.id));

  // After close: exactly one dance, in z1, and F met only L1.
  const closed = e.closeRound('s', { round: 1 });
  assert.equal(closed.dances.length, 1);
  assert.equal(closed.dances[0].zone, 'z1');
  assert.deepEqual(e.metPartners('s', 'F'), ['L1']);
  assert.deepEqual(e.metPartners('s', 'L2'), []);
});

test('twoLeads: whoever confirms first wins regardless of lead register order', () => {
  const e = newEngine();
  e.createSession({ sessionId: 's', rounds: RHYTHMS, zones: ['z1', 'z2'] });
  e.registerGuest('s', { number: 'L1', name: '领队一', roles: ['lead'], targetPartners: 1 });
  e.registerGuest('s', { number: 'L2', name: '领队二', roles: ['lead'], targetPartners: 1 });
  e.registerGuest('s', { number: 'F', name: '跟舞者', roles: ['follow'], targetPartners: 2 });
  e.setZone('s', { zone: 'z1', open: true });
  e.setZone('s', { zone: 'z2', open: true });

  const inv1 = e.proposeInvitation('s', { round: 1, from: 'L1', to: 'F', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  const inv2 = e.proposeInvitation('s', { round: 1, from: 'L2', to: 'F', fromRole: 'lead', toRole: 'follow', zone: 'z2' });

  // This time F accepts L2 first.
  const r2 = e.confirmInvitation('s', { invitationId: inv2.id, by: 'F' });
  const r1 = e.confirmInvitation('s', { invitationId: inv1.id, by: 'F' });
  assert.equal(r2.ok, true);
  assert.equal(r1.ok, false);

  const eff = e.effectiveAssignment('s', { number: 'F', round: 1 });
  assert.equal(eff.effective.partner, 'L2');

  const closed = e.closeRound('s', { round: 1 });
  assert.equal(closed.dances.length, 1);
  assert.equal(closed.dances[0].zone, 'z2');
});

// =============================================================================
// 5. Full small party (<=6 guests, 3 rounds) via propose/confirm/open/close/makeup
// =============================================================================
test('happyPath: everyone shows up as agreed and hits their signed encounter count', () => {
  const e = newEngine();
  e.createSession({ sessionId: 's', rounds: RHYTHMS, zones: ['z1', 'z2', 'z3'] });
  // 6 guests; 甲 wants 2 distinct partners over 3 rounds.
  e.registerGuest('s', { number: '001', name: '甲', roles: ALL, targetPartners: 2 });
  e.registerGuest('s', { number: '002', name: '乙', roles: ALL, targetPartners: 2 });
  e.registerGuest('s', { number: '003', name: '丙', roles: ALL, targetPartners: 2 });
  e.registerGuest('s', { number: '004', name: '丁', roles: ALL, targetPartners: 1 });
  e.registerGuest('s', { number: '005', name: '戊', roles: ALL, targetPartners: 1 });
  e.registerGuest('s', { number: '006', name: '己', roles: ALL, targetPartners: 1 });
  for (const z of ['z1', 'z2', 'z3']) e.setZone('s', { zone: z, open: true });

  invite(e, { round: 1, from: '001', to: '002', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  invite(e, { round: 1, from: '003', to: '004', fromRole: 'lead', toRole: 'follow', zone: 'z2' });
  invite(e, { round: 1, from: '005', to: '006', fromRole: 'lead', toRole: 'follow', zone: 'z3' });
  e.closeRound('s', { round: 1 });

  invite(e, { round: 2, from: '001', to: '003', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  invite(e, { round: 2, from: '002', to: '005', fromRole: 'lead', toRole: 'follow', zone: 'z2' });
  invite(e, { round: 2, from: '004', to: '006', fromRole: 'lead', toRole: 'follow', zone: 'z3' });
  e.closeRound('s', { round: 2 });

  invite(e, { round: 3, from: '001', to: '004', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  invite(e, { round: 3, from: '002', to: '006', fromRole: 'lead', toRole: 'follow', zone: 'z2' });
  invite(e, { round: 3, from: '003', to: '005', fromRole: 'lead', toRole: 'follow', zone: 'z3' });
  e.closeRound('s', { round: 3 });

  // 甲 met 乙, 丙, 丁 => 3 distinct, target 2 -> redeemed on explicit action only.
  assert.deepEqual(e.metPartners('s', '001'), ['002', '003', '004']);
  const q = e.quota('s', '001');
  assert.equal(q.canFulfil, true);
  assert.equal(q.prompt, 'fulfilled');

  // Exporting the schedule must NOT redeem anything.
  const exported = e.exportSchedule('s');
  assert.equal(e.meetingRecord('s', '001').redeemed, false);
  assert.equal(exported.rounds.length, 3);

  // Explicit redeem succeeds now that the count is met.
  const order = e.redeem('s', { number: '001' });
  assert.equal(order.redeemed, true);
  assert.throws(() => e.redeem('s', { number: '001' }), /already redeemed/i);
});

// =============================================================================
// 6. No-show, make-up reassignment, and quota advice
// =============================================================================
test('noShow: a partner who leaves is distinguished and make-up finds a new partner', () => {
  const e = baseScene();
  // Give 丁 a real target so makeup matters.
  e.registerGuest('s', { number: '005', name: '戊', roles: ALL, targetPartners: 1 });

  // R1: 甲+乙 dance; 丁 rests (temporarily leaves).
  invite(e, { round: 1, from: '001', to: '002', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  invite(e, { round: 1, from: '004', to: '003', fromRole: 'lead', toRole: 'follow', zone: 'z2' });
  e.setRest('s', { number: '003', round: 1, away: true }); // 丙 won't arrive
  const r1 = e.closeRound('s', { round: 1 });

  const absent = r1.outcomes.find((o) => o.status === 'absent');
  assert.ok(absent, 'the 丁/丙 invitation must be absent at close');

  // 丁's record separates the no-show from actual encounters.
  const recD = e.meetingRecord('s', '004');
  assert.equal(recD.noShows.length, 1);
  assert.equal(recD.noShows[0].partner, '003');
  assert.equal(recD.distinctPartners, 0);

  // Make-up: 丁 still needs a partner; reassign them in round 2 (prefer a new one).
  const makeup = e.autoMakeup('s');
  assert.ok(makeup.created.length >= 1, 'a make-up invitation is created');
  const mk = makeup.created[0];
  assert.equal(mk.from === '004' || mk.to === '004', true);
  assert.equal(mk.round, 2);

  // Close round 2 with the make-up invitation already confirmed.
  const r2 = e.closeRound('s', { round: 2 });
  assert.equal(r2.dances.length, 1);
  assert.equal(e.metPartners('s', '004').length, 1);
});

test('quota: tells a guest to rest when they are marked away for the next round', () => {
  const e = baseScene();
  e.setRest('s', { number: '001', round: 1, away: true });
  const q = e.quota('s', '001');
  assert.equal(q.prompt, 'rest');
  assert.equal(q.delivered, 0);
});

test('quota: warns reschedule when the signed count cannot be delivered', () => {
  // Only one remaining round and no fresh candidates -> cannot fulfil a target of 3.
  const e = newEngine();
  e.createSession({ sessionId: 's', rounds: ['waltz'], zones: ['z1'] });
  e.registerGuest('s', { number: '001', name: '甲', roles: ALL, targetPartners: 3, rhythms: ['waltz'] });
  e.registerGuest('s', { number: '002', name: '乙', roles: ALL, targetPartners: 1, rhythms: ['waltz'] });
  e.setZone('s', { zone: 'z1', open: true });
  const q = e.quota('s', '001');
  assert.equal(q.canFulfil, false);
  assert.equal(q.prompt, 'reschedule');
});

// =============================================================================
// 7. Restart durability: replay by attendee number
// =============================================================================
test('restart: replaying the log reproduces exactly whom each guest really met', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dance-'));
  const file = path.join(dir, 'events.jsonl');

  const e1 = new Engine({ filePath: file });
  e1.createSession({ sessionId: 's', rounds: RHYTHMS, zones: ['z1', 'z2'] });
  e1.registerGuest('s', { number: '001', name: '甲', roles: ALL, targetPartners: 2 });
  e1.registerGuest('s', { number: '002', name: '乙', roles: ALL, targetPartners: 2 });
  e1.registerGuest('s', { number: '003', name: '丙', roles: ALL, targetPartners: 1 });
  e1.setZone('s', { zone: 'z1', open: true });
  e1.setZone('s', { zone: 'z2', open: true });

  invite(e1, { round: 1, from: '001', to: '002', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  e1.closeRound('s', { round: 1 });
  invite(e1, { round: 2, from: '002', to: '001', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  e1.closeRound('s', { round: 2 });
  invite(e1, { round: 3, from: '003', to: '001', fromRole: 'lead', toRole: 'follow', zone: 'z2' });
  e1.closeRound('s', { round: 3 });

  const before = {
    '001': e1.metPartners('s', '001'),
    '002': e1.metPartners('s', '002'),
    '003': e1.metPartners('s', '003'),
  };
  assert.deepEqual(before['001'], ['002', '003']);

  // Simulate a process restart.
  const e2 = new Engine({ filePath: file });
  for (const n of ['001', '002', '003']) {
    assert.deepEqual(e2.metPartners('s', n), before[n], `guest ${n} replay mismatch`);
    assert.equal(e2.meetingRecord('s', n).distinctPartners, before[n].length);
  }
  // The role-swapped repeat survives replay too.
  const rec = e2.meetingRecord('s', '001');
  const b = rec.partners.find((p) => p.number === '002');
  assert.equal(b.dances.length, 2);
  assert.equal(b.dances[1].roleSwapped, true);
});

// =============================================================================
// 8. Order / invariant guards
// =============================================================================
test('guards: cannot close out of order, cannot confirm a closed round, no self/role clash', () => {
  const e = baseScene();
  assert.throws(() => e.closeRound('s', { round: 2 }), /expected to close round 1/);
  assert.throws(
    () => e.proposeInvitation('s', { round: 1, from: '001', to: '001', fromRole: 'lead', toRole: 'follow', zone: 'z1' }),
    /cannot invite yourself/
  );
  assert.throws(
    () => e.proposeInvitation('s', { round: 1, from: '001', to: '002', fromRole: 'lead', toRole: 'lead', zone: 'z1' }),
    /one partner must lead/
  );
  // A guest who can do both roles may propose as follow and succeed when the
  // partner leads.
  const ok = e.proposeInvitation('s', { round: 1, from: '001', to: '002', fromRole: 'follow', toRole: 'lead', zone: 'z1' });
  assert.equal(ok.fromRole, 'follow');
  assert.equal(ok.toRole, 'lead');
});

test('guards: after a round closes you can no longer invite into it', () => {
  const e = baseScene();
  invite(e, { round: 1, from: '001', to: '002', fromRole: 'lead', toRole: 'follow', zone: 'z1' });
  e.closeRound('s', { round: 1 });
  assert.throws(
    () => e.proposeInvitation('s', { round: 1, from: '003', to: '004', fromRole: 'lead', toRole: 'follow', zone: 'z1' }),
    /already closed/
  );
});

test('guards: role capability is enforced for role-restricted guests', () => {
  const e = newEngine();
  e.createSession({ sessionId: 's', rounds: RHYTHMS, zones: ['z1'] });
  e.registerGuest('s', { number: 'FO', name: '只跟', roles: ['follow'], targetPartners: 1 });
  e.registerGuest('s', { number: 'LE', name: '只领', roles: ['lead'], targetPartners: 1 });
  e.setZone('s', { zone: 'z1', open: true });
  assert.throws(
    () => e.proposeInvitation('s', { round: 1, from: 'FO', to: 'LE', fromRole: 'lead', toRole: 'follow', zone: 'z1' }),
    /cannot lead/
  );
});

test('guards: a guest cannot be booked outside their purchased rounds', () => {
  const e = newEngine();
  e.createSession({ sessionId: 's', rounds: ['waltz', 'tango', 'salsa'], zones: ['z1'] });
  e.registerGuest('s', { number: '001', name: '甲', roles: ALL, targetPartners: 1, roundsPurchased: 2 });
  e.registerGuest('s', { number: '002', name: '乙', roles: ALL, targetPartners: 1 });
  e.setZone('s', { zone: 'z1', open: true });
  assert.throws(
    () => e.proposeInvitation('s', { round: 3, from: '001', to: '002', fromRole: 'lead', toRole: 'follow', zone: 'z1' }),
    /did not buy round 3/
  );
  // Within purchase it works.
  assert.equal(
    e.proposeInvitation('s', { round: 2, from: '001', to: '002', fromRole: 'lead', toRole: 'follow', zone: 'z1' }).round,
    2
  );
});

// =============================================================================
// 9. Program-book partners vs. actual encounters
// =============================================================================
test('program-book: a scheduled ("节目单") partner is listed separately until danced', () => {
  const e = baseScene();
  // A program-book invitation for round 1, confirmed but not yet closed.
  invite(e, { round: 1, from: '001', to: '002', fromRole: 'lead', toRole: 'follow', zone: 'z1', source: 'program' });

  const rec = e.meetingRecord('s', '001');
  assert.equal(rec.distinctPartners, 0, 'nobody actually met yet');
  assert.equal(rec.scheduled.length, 1);
  assert.equal(rec.scheduled[0].partner, '002');
  assert.equal(rec.scheduled[0].source, 'program');

  // Now it really happens.
  e.closeRound('s', { round: 1 });
  const rec2 = e.meetingRecord('s', '001');
  assert.equal(rec2.distinctPartners, 1);
  assert.equal(rec2.scheduled.length, 0, 'no longer merely scheduled');
  assert.equal(rec2.partners[0].dances[0].scheduled, true, 'but the dance notes it came from the book');
});

test('program-book: schedule export lists the plan and does not redeem orders', () => {
  const e = baseScene();
  invite(e, { round: 1, from: '001', to: '002', fromRole: 'lead', toRole: 'follow', zone: 'z1', source: 'program' });
  const exp = e.exportSchedule('s');
  assert.equal(exp.rounds[0].invitations.length, 1);
  assert.equal(exp.rounds[0].invitations[0].source, 'program');
  assert.equal(exp.rounds[0].closed, false);
  // no redemption as a side effect
  assert.equal(e.meetingRecord('s', '001').redeemed, false);
});
