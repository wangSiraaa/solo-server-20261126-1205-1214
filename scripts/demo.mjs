#!/usr/bin/env node
import { spawn } from 'node:child_process';
import nodeAssert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const port = process.env.DEMO_PORT ?? '4319';
const base = `http://127.0.0.1:${port}`;
const dataFile = process.env.DEMO_DATA_FILE ?? path.join(root, 'data', 'demo.json');

fs.rmSync(dataFile, { force: true });
fs.mkdirSync(path.dirname(dataFile), { recursive: true });

let child;

function startServer() {
  return new Promise((resolve, reject) => {
    const proc = spawn(process.execPath, [path.join(root, 'src', 'server.js')], {
      cwd: root,
      env: { ...process.env, PORT: port, HOST: '127.0.0.1', DATA_FILE: dataFile },
      stdio: ['ignore', 'pipe', 'inherit'],
    });
    proc.stdout.on('data', (chunk) => process.stdout.write(`[server] ${chunk}`));
    proc.on('error', reject);
    child = proc;
    resolve(proc);
  });
}

async function stopServer() {
  if (!child || child.killed) return;
  await new Promise((resolve) => {
    child.once('exit', resolve);
    child.kill('SIGTERM');
  });
  child = null;
}

async function waitForHealth(retries = 50) {
  for (let i = 0; i < retries; i += 1) {
    try {
      const res = await fetch(`${base}/health`);
      if (res.ok) return res.json();
    } catch {
      await new Promise((r) => setTimeout(r, 100));
    }
  }
  throw new Error('server did not become healthy');
}

async function api(method, route, body) {
  const res = await fetch(`${base}${route}`, {
    method,
    headers: body === undefined ? {} : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  const json = text ? JSON.parse(text) : null;
  if (!res.ok) {
    throw new Error(`${method} ${route} failed: ${res.status} ${JSON.stringify(json)}`);
  }
  return json;
}

function section(title) {
  console.log(`\n=== ${title} ===`);
}

function show(label, value) {
  console.log(`\n-- ${label}`);
  console.log(JSON.stringify(value, null, 2));
}

function assert(condition, message) {
  if (!condition) throw new Error(`Assertion failed: ${message}`);
  console.log(`   ✓ ${message}`);
}

function independentAnswer(assignmentId, cardId, sound = 'æ') {
  return { assignmentId, cardId, firstGuess: sound, finalAnswer: sound, revealedHints: [] };
}

async function claimPack(studentId, packId, deviceId) {
  return api('POST', '/api/rounds/claim', { studentId, packId, deviceId });
}

async function submitIndependent(studentId, roundId, item, deviceId = 'device-1', extra = {}) {
  return api('POST', '/api/receipts', {
    studentId,
    roundId,
    deviceId,
    returnedAt: extra.returnedAt ?? new Date().toISOString(),
    answers: [independentAnswer(item.assignmentId, item.cardId, item.expected)],
    ...extra,
  });
}

async function main() {
  section('Start persistent service');
  await startServer();
  const health = await waitForHealth();
  show('health', health);

  section('Teacher assigns phonics points, lesson-specific sounds, calendar and gate');
  await api('POST', '/api/teacher/kps', { id: 'kp-a', name: 'short a /æ/' });
  await api('POST', '/api/teacher/kps', { id: 'kp-long-a', name: 'long a /eɪ/' });
  await api('POST', '/api/teacher/cards', { id: 'card-a1', glyph: 'a' });
  await api('POST', '/api/teacher/cards', { id: 'card-a2', glyph: 'a' });
  await api('POST', '/api/teacher/cards', { id: 'card-glyph-a', glyph: 'a' });
  await api('POST', '/api/teacher/units', { id: 'u1', name: 'Unit 1' });
  await api('POST', '/api/teacher/units', { id: 'u2', name: 'Unit 2' });
  await api('POST', '/api/teacher/assignments', {
    id: 'as-a1', unitId: 'u1', cardId: 'card-a1', kpId: 'kp-a', expected: 'æ', hints: ['hear /æ/'],
  });
  await api('POST', '/api/teacher/assignments', {
    id: 'as-a2', unitId: 'u1', cardId: 'card-a2', kpId: 'kp-a', expected: 'æ', hints: ['hear /æ/ again'],
  });
  await api('POST', '/api/teacher/assignments', {
    id: 'as-glyph-u2', unitId: 'u2', cardId: 'card-glyph-a', kpId: 'kp-long-a', expected: 'eɪ', hints: ['hear /eɪ/'],
  });
  await api('POST', '/api/teacher/gates', {
    id: 'gate-app',
    name: 'Day-8 application level',
    opensOnDay: 8,
    requirements: [{ kpId: 'kp-a', independentRounds: 2 }],
  });
  await api('POST', '/api/students', { id: 'alice', name: 'Alice', practiceDays: [1, 2, 3, 4, 5, 6, 7, 8] });
  await api('POST', '/api/students', { id: 'ivy', name: 'Ivy', practiceDays: [1, 4, 5, 6, 7, 8] });
  await api('POST', '/api/students', { id: 'maya', name: 'Maya', practiceDays: [1, 4, 5, 8] });
  await api('POST', '/api/students', { id: 'leo', name: 'Leo', practiceDays: [1, 4, 5, 8] });

  await api('POST', '/api/teacher/packs', {
    id: 'pack-d1', day: 1, unitId: 'u1', assignmentIds: ['as-a1'], worksheetId: 'ws-d1',
  });
  await api('POST', '/api/teacher/packs', {
    id: 'pack-d4', day: 4, unitId: 'u1', assignmentIds: ['as-a2'], worksheetId: 'ws-d4',
  });
  await api('POST', '/api/teacher/packs', {
    id: 'pack-d5', day: 5, unitId: 'u1', assignmentIds: ['as-a1'], worksheetId: 'ws-d5',
  });
  console.log('   ✓ same glyph "a" is /æ/ in Unit 1 and /eɪ/ in Unit 2; receipts are scored by lesson assignment.');

  section('Baseline: Alice independently succeeds on days 1 and 4');
  const alice1Claim = await claimPack('alice', 'pack-d1', 'device-home');
  await submitIndependent('alice', alice1Claim.round.id, alice1Claim.round.items[0]);
  const alice4Claim = await claimPack('alice', 'pack-d4', 'device-home');
  await submitIndependent('alice', alice4Claim.round.id, alice4Claim.round.items[0]);
  const aliceDay4Gate = await api('GET', '/api/gates/gate-app/access?studentId=alice&day=4');
  assert(aliceDay4Gate.status === 'scheduled_locked', 'even two rounds do not open before day 8');
  assert(aliceDay4Gate.requirements[0].earnedIndependentRounds === 2, 'both rounds are already visible on day 4');
  let aliceGate = await api('GET', '/api/gates/gate-app/access?studentId=alice&day=8');
  show('Alice day-8 gate', aliceGate);
  assert(aliceGate.open === true, 'two distinct independent rounds open the day-8 gate');
  nodeAssert.deepEqual(aliceGate.requirements[0].earnedDays, [1, 4]);

  section('Conflict and repair: Maya has one normal round plus two disagreeing device copies');
  const maya1Claim = await claimPack('maya', 'pack-d1', 'device-a');
  await submitIndependent('maya', maya1Claim.round.id, maya1Claim.round.items[0], 'device-a');
  const maya4Claim = await claimPack('maya', 'pack-d4', 'device-a');
  await api('POST', '/api/receipts', {
    studentId: 'maya',
    roundId: maya4Claim.round.id,
    deviceId: 'device-a',
    copyId: 'classroom-copy',
    answers: [independentAnswer('as-a2', 'card-a2')],
  });
  const conflict = await api('POST', '/api/receipts', {
    studentId: 'maya',
    roundId: maya4Claim.round.id,
    deviceId: 'device-b',
    copyId: 'home-copy',
    answers: [{
      assignmentId: 'as-a2', cardId: 'card-a2', firstGuess: 'uh', finalAnswer: 'æ',
      revealedHints: ['hear /æ/ again'],
    }],
  });
  assert(conflict.round.status === 'conflicted', 'contradictory copies make only that KP conflicted');
  const evidence = await api('GET', `/api/rounds/${maya4Claim.round.id}/evidence`);
  show('Developer evidence explanation for conflicted round', {
    status: evidence.status,
    deviceIds: evidence.deviceIds,
    sufficiency: evidence.sufficiency,
    conflicts: evidence.reconciliation.conflicts,
  });
  assert(evidence.sufficiency['kp-a'].oneRoundCredit === false, 'conflicted independence evidence is not credited');

  const mayaOpp = await api('GET', '/api/students/maya/opportunities?day=5');
  const repairOrder = mayaOpp.makeupOrders.find((o) => o.conflictRoundId === maya4Claim.round.id && o.status === 'open');
  assert(Boolean(repairOrder), 'a targeted makeup order is created');
  const repairClaim = await api('POST', '/api/rounds/claim', {
    studentId: 'maya', makeupOrderId: repairOrder.id, day: 5, deviceId: 'device-a',
  });
  assert(repairClaim.round.items[0].cardId === 'card-a1', 'repair switches to another real card for the same KP');
  await submitIndependent('maya', repairClaim.round.id, repairClaim.round.items[0], 'device-a');
  const mayaGate = await api('GET', '/api/gates/gate-app/access?studentId=maya&day=8');
  show('Maya after targeted repair', mayaGate);
  assert(mayaGate.open === true, 'day 1 plus repaired day 5 are two rounds; normal prior evidence is retained');

  section('Prepare Leo: same canonical round exists on two devices before restart');
  const leo1Claim = await claimPack('leo', 'pack-d1', 'device-new');
  await submitIndependent('leo', leo1Claim.round.id, leo1Claim.round.items[0], 'device-new');
  const leo4OldClaim = await claimPack('leo', 'pack-d4', 'device-old');
  const leo4NewClaim = await claimPack('leo', 'pack-d4', 'device-new');
  assert(leo4OldClaim.round.id === leo4NewClaim.round.id, 'both devices resolve to the same round');
  await submitIndependent('leo', leo4NewClaim.round.id, leo4NewClaim.round.items[0], 'device-new', {
    copyId: 'new-device-copy',
  });

  section('Restart service; JSON persistence keeps rounds, gate and worksheets');
  await stopServer();
  await startServer();
  await waitForHealth();
  console.log('   ✓ service restarted from', dataFile);

  section('Old offline device sends a new copy of the same round after restart');
  const oldDuplicate = await api('POST', '/api/receipts', {
    studentId: 'leo',
    // Deliberately omit roundId to prove worksheet identity also resolves it.
    worksheetId: 'ws-d4',
    deviceId: 'device-old',
    copyId: 'old-device-late-copy',
    returnedAt: '2026-11-26T07:55:00Z',
    answers: [independentAnswer('as-a2', 'card-a2')],
  });
  show('Late duplicate reconciliation', {
    duplicate: oldDuplicate.duplicate,
    countedAs: oldDuplicate.countedAs,
    roundId: oldDuplicate.round.id,
    worksheetId: oldDuplicate.round.worksheetId,
    deviceIds: oldDuplicate.round.deviceIds,
    submissionCount: oldDuplicate.round.submissionCount,
  });
  assert(oldDuplicate.duplicate === true, 'late identical copy is recognized');
  assert(oldDuplicate.round.submissionCount === 1, 'same round remains one evidence round');

  const leoGate = await api('GET', '/api/gates/gate-app/access?studentId=leo&day=8');
  assert(leoGate.open === true, 'Leo still has exactly two contributing days: 1 and 4');
  nodeAssert.deepEqual(leoGate.requirements[0].earnedDays, [1, 4]);

  const nextClaim = await claimPack('leo', 'pack-d5', 'device-new');
  const worksheet = await api('GET', `/api/worksheets/${nextClaim.round.worksheetId}`);
  show('Next real worksheet after restart', {
    roundId: nextClaim.round.id,
    worksheetId: worksheet.worksheetId,
    day: worksheet.day,
    kpIds: worksheet.kpIds,
    firstItem: worksheet.items[0],
  });
  assert(worksheet.worksheetId === 'ws-d5', 'next claim lines up with the persisted real worksheet');
  assert(worksheet.kpIds.includes('kp-a'), 'worksheet snapshot lines up with gate knowledge point');

  section('Tainted variant: Ivy views the hint before answering the alternate day-4 card');
  const ivy1Claim = await claimPack('ivy', 'pack-d1', 'device-school');
  await submitIndependent('ivy', ivy1Claim.round.id, ivy1Claim.round.items[0], 'device-school');
  const ivy4Claim = await claimPack('ivy', 'pack-d4', 'device-school');
  const tainted = await api('POST', '/api/receipts', {
    studentId: 'ivy',
    roundId: ivy4Claim.round.id,
    deviceId: 'device-school',
    answers: [{
      assignmentId: 'as-a2',
      cardId: 'card-a2',
      firstGuess: 'uh',
      finalAnswer: 'æ',
      revealedHints: ['hear /æ/ again'],
    }],
  });
  assert(
    tainted.round.reconciliation.kpResults['kp-a'].state === 'hint_assisted',
    'hint-before-answer loses independence for that round',
  );
  const ivyGate = await api('GET', '/api/gates/gate-app/access?studentId=ivy&day=8');
  const ivyOpp = await api('GET', '/api/students/ivy/opportunities?day=5');
  show('Ivy gate and practicable days after tainted day 4', {
    gate: ivyGate,
    practiceDays: ivyOpp.practiceDays,
    kpARequirement: ivyOpp.gates[0].requirements[0],
  });
  assert(ivyGate.open === false, 'only day 1 contributes, so day 8 does not unlock');
  assert(ivyGate.requirements[0].stillNeeds === 1, 'one more distinct independent round is needed');
  assert(ivyOpp.gates[0].requirements[0].nextPracticeDay === 5, 'day 5 is offered as the next contributable day');

  section('Back to baseline, then teacher adds one unlearned gate requirement');
  const baselineAlice = await api('GET', '/api/gates/gate-app/access?studentId=alice&day=8');
  assert(baselineAlice.open === true, 'baseline Alice opens normally before the gate change');
  await api('POST', '/api/teacher/kps', { id: 'kp-new', name: 'not yet learned' });
  await api('PATCH', '/api/teacher/gates/gate-app', {
    requirements: [
      { kpId: 'kp-a', independentRounds: 2 },
      { kpId: 'kp-new', independentRounds: 2 },
    ],
  });
  const changedGateAlice = await api('GET', '/api/gates/gate-app/access?studentId=alice&day=8');
  show('Alice after teacher adds unlearned point', changedGateAlice);
  assert(changedGateAlice.open === false, 'two old rounds cannot satisfy the newly added point');
  assert(
    changedGateAlice.requirements.find((r) => r.kpId === 'kp-new').earnedIndependentRounds === 0,
    'unlearned added point has no evidence',
  );

  section('Demo complete');
  console.log('\nAll narrative assertions passed.');
  console.log(`Persistent demo database: ${dataFile}`);
  console.log('Start the API with: npm start');
}

main()
  .catch((err) => {
    console.error('\nDEMO FAILED:', err);
    process.exitCode = 1;
  })
  .finally(async () => {
    await stopServer();
  });
