#!/usr/bin/env node
'use strict';

/**
 * A runnable demo of the round-by-round back office.
 *
 *   node scripts/demo.js
 *
 * It walks through a small 3-round party and prints, for guest 甲:
 *   - the distinguishing of a role-swapped re-dance (still ONE partner),
 *   - then a genuinely new partner (now TWO),
 *   - the quota / rest / reschedule advice,
 *   - what the order redemption sees (schedule export never redeems).
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { Engine } = require('../src/engine');

const ALL = ['lead', 'follow'];
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dance-demo-'));
const logPath = path.join(dir, 'events.jsonl');
const e = new Engine({ filePath: logPath });

function invite(round, from, to, fromRole, toRole, zone, source = 'request') {
  const inv = e.proposeInvitation('s', { round, from, to, fromRole, toRole, zone, source });
  const res = e.confirmInvitation('s', { invitationId: inv.id, by: to });
  if (!res.ok) throw new Error(`confirm failed: ${JSON.stringify(res)}`);
  return inv;
}

function line(title) {
  console.log('\n' + '='.repeat(60) + '\n' + title + '\n' + '='.repeat(60));
}

e.createSession({ sessionId: 's', rounds: ['waltz', 'tango', 'salsa'], zones: ['z1', 'z2'] });
e.registerGuest('s', { number: '001', name: '甲', roles: ALL, targetPartners: 2 });
e.registerGuest('s', { number: '002', name: '乙', roles: ALL, targetPartners: 2 });
e.registerGuest('s', { number: '003', name: '丙', roles: ALL, targetPartners: 1 });
e.registerGuest('s', { number: '004', name: '丁', roles: ALL, targetPartners: 1 });
e.setZone('s', { zone: 'z1', open: true });
e.setZone('s', { zone: 'z2', open: true });

line('Round 1: 甲 leads, 乙 follows');
invite(1, '001', '002', 'lead', 'follow', 'z1');
e.closeRound('s', { round: 1 });
console.log('甲 不同舞伴:', e.metPartners('s', '001'));

line('Round 2: 乙 swaps roles (now leads) and re-dances 甲');
invite(2, '002', '001', 'lead', 'follow', 'z1');
e.closeRound('s', { round: 2 });
console.log('甲 不同舞伴:', e.metPartners('s', '001'), '(仍是 1 人！)');
const rec = e.meetingRecord('s', '001');
console.log('甲的会面记录:');
for (const p of rec.partners) {
  for (const d of p.dances) {
    console.log(
      `  R${d.round} vs ${p.name} — 我 ${d.myRole}/${d.theirRole}` +
        (d.roleSwapped ? '  [换角色重跳]' : '') +
        (d.repeat ? '  [repeat]' : '')
    );
  }
}

line('Quota advice for 甲 after round 2');
console.log(e.quota('s', '001'));

line('Round 3: 丙 leads, 甲 follows — a genuinely NEW partner');
invite(3, '003', '001', 'lead', 'follow', 'z2');
e.closeRound('s', { round: 3 });
console.log('甲 不同舞伴:', e.metPartners('s', '001'), '(现在 2 人)');

line('Export schedule (READ-ONLY, must not redeem)');
const exported = e.exportSchedule('s');
console.log('rounds:', exported.rounds.map((r) => `R${r.round}${r.closed ? '(closed)' : ''}`).join(' '));
console.log('甲 redeemed after export?', e.meetingRecord('s', '001').redeemed);

line('Explicit redeem now that 甲 really met 2 distinct partners');
console.log(e.redeem('s', { number: '001' }));

line('Restart: replay the event log from disk, by attendee number');
const e2 = new Engine({ filePath: logPath });
console.log('甲 replayed met:', e2.metPartners('s', '001'));
console.log('乙 replayed met:', e2.metPartners('s', '002'));

console.log(`\n(event log: ${logPath})`);
