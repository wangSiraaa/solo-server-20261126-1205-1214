import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from '../server.js';
import { JsonStore } from '../src/store.js';
import {
  seedDb,
  evaluateStudent,
  findDemoFillablePractice,
  createCase,
} from '../src/model.js';

async function startedServer() {
  const store = new JsonStore(':memory:');
  const server = createServer({ store });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const base = `http://127.0.0.1:${port}`;
  return { server, base, store };
}

async function json(base, method, path, body) {
  const response = await fetch(`${base}${path}`, {
    method,
    headers: { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  return { status: response.status, data };
}

test('seed identifies the one audit practice a later demo can complete', () => {
  const db = seedDb();
  const found = findDemoFillablePractice(db);
  assert.equal(found.length, 1);
  assert.equal(found[0].fillsPracticeRecordId, 'PR-DING-FLOW');
  assert.equal(found[0].auditWorkflowComplete, true);
  assert.equal(found[0].practicalCompletionComplete, false);

  const jia = evaluateStudent(db, 'S-JIA');
  const wallClose = jia.outstandingSegments.find((s) => s.linkIds.includes('L-WALL-CLOSE'));
  assert.equal(wallClose.retainable, true);
  assert.equal(wallClose.pieceNo, 'P-JIA-01');
  assert.ok(wallClose.reasonCodes.includes('piece_mismatch'));
  assert.ok(wallClose.reasonCodes.includes('target_mode_observed_not_accepted'));
  assert.equal(jia.transferableCredits[0].satisfied, true);
});

test('Jia: retained original ticket, attendance, receipt, one stamp under duplicate/concurrent mentors', async (t) => {
  const { server, base } = await startedServer();
  t.after(() => server.close());

  let res = await json(base, 'POST', '/api/admin/reset');
  assert.equal(res.status, 200);

  res = await json(base, 'POST', '/api/cases', {
    studentId: 'S-JIA',
    groupCode: 'BASIC_CYLINDER',
    linkId: 'L-WALL-CLOSE',
  });
  assert.equal(res.status, 201);
  const caseId = res.data.case.id;
  assert.equal(res.data.case.reservation.forStudentId, 'S-JIA');
  assert.equal(res.data.case.reservation.pieceNo, 'P-JIA-01');
  assert.equal(res.data.case.reservation.sessionId, 'OPEN-1126');
  assert.ok(res.data.case.reservation.tableId);

  // Duplicate initiation returns the same ticket.
  const duplicateCreate = await json(base, 'POST', '/api/cases', {
    studentId: 'S-JIA',
    groupCode: 'BASIC_CYLINDER',
    linkId: 'L-WALL-CLOSE',
  });
  assert.equal(duplicateCreate.data.existing, true);
  assert.equal(duplicateCreate.data.case.id, caseId);

  // Receipt before real attendance is rejected.
  const earlyReceipt = await json(base, 'POST', `/api/cases/${caseId}/receipt`, { action: 'close' });
  assert.equal(earlyReceipt.status, 409);
  assert.equal(earlyReceipt.data.error.code, 'ATTENDANCE_REQUIRED');

  res = await json(base, 'POST', `/api/cases/${caseId}/attendance`, { confirmedBy: 'MENTOR-LIN' });
  assert.equal(res.status, 201);

  // Observation cannot replace hands-on completion.
  const observed = await json(base, 'POST', `/api/cases/${caseId}/receipt`, {
    action: 'close',
    mode: 'observed',
  });
  assert.equal(observed.status, 422);
  assert.equal(observed.data.error.code, 'HANDS_ON_REQUIRED');

  // Another piece, even hands-on, cannot splice onto P-JIA-01 wall.
  const wrongPiece = await json(base, 'POST', `/api/cases/${caseId}/receipt`, {
    action: 'close',
    mode: 'hands_on',
    pieceNo: 'P-JIA-02',
  });
  assert.equal(wrongPiece.status, 422);
  assert.equal(wrongPiece.data.error.code, 'PIECE_MISMATCH');

  res = await json(base, 'POST', `/api/cases/${caseId}/receipt`, {
    action: 'close',
    mode: 'hands_on',
    pieceNo: 'P-JIA-01',
    confirmedBy: 'TA-ZHOU',
  });
  assert.equal(res.status, 201);

  const [lin, he, resent] = await Promise.all([
    json(base, 'POST', `/api/cases/${caseId}/confirm`, { mentorId: 'MENTOR-LIN' }),
    json(base, 'POST', `/api/cases/${caseId}/confirm`, { mentorId: 'MENTOR-HE' }),
    json(base, 'POST', `/api/cases/${caseId}/confirm`, { mentorId: 'MENTOR-LIN' }),
  ]);

  assert.equal(lin.status, 200);
  assert.equal(he.status, 200);
  assert.equal(resent.status, 200);
  const stampId = lin.data.stamp.id;
  assert.equal(he.data.stamp.id, stampId);
  assert.equal(resent.data.stamp.id, stampId);
  assert.equal([lin, he].filter((r) => r.data.created).length, 1);
  assert.equal(he.data.duplicate, true);
  assert.equal(resent.data.duplicate, true);

  const stamps = await json(base, 'GET', '/api/stamps?studentId=S-JIA');
  const basic = stamps.data.filter((s) => s.groupCode === 'BASIC_CYLINDER');
  assert.equal(basic.length, 1);
  assert.equal(basic[0].id, stampId);
  assert.match(lin.data.signedDecision.statement, /学成/);
});

test('same-piece close still fails if its confirmed participation is changed to observation', async (t) => {
  const { server, base } = await startedServer();
  t.after(() => server.close());
  await json(base, 'POST', '/api/admin/reset');

  const created = await json(base, 'POST', '/api/cases', {
    studentId: 'S-JIA',
    groupCode: 'BASIC_CYLINDER',
    linkId: 'L-WALL-CLOSE',
  });
  const caseId = created.data.case.id;
  await json(base, 'POST', `/api/cases/${caseId}/attendance`, { confirmedBy: 'MENTOR-LIN' });
  const receipt = await json(base, 'POST', `/api/cases/${caseId}/receipt`, {
    action: 'close',
    pieceNo: 'P-JIA-01',
  });
  const eventId = receipt.data.receipt.eventIds[0];

  const changed = await json(base, 'PATCH', `/api/events/${eventId}`, { mode: 'observed' });
  assert.equal(changed.status, 200);

  const evaluation = await json(base, 'GET', '/api/students/S-JIA/evaluation');
  const group = evaluation.data.groups.find((g) => g.code === 'BASIC_CYLINDER');
  assert.equal(group.satisfied, false);

  const confirm = await json(base, 'POST', `/api/cases/${caseId}/confirm`, {
    mentorId: 'MENTOR-LIN',
    decision: 'certify',
  });
  assert.equal(confirm.status, 422);
  assert.equal(confirm.data.error.code, 'GAP_STILL_OUTSTANDING');
  const stamps = await json(base, 'GET', '/api/stamps?studentId=S-JIA');
  assert.equal(stamps.data.length, 0);
});

test('control history is unchanged but changing cross-piece rule to same-piece changes handling', async (t) => {
  const { server, base, store } = await startedServer();
  t.after(() => server.close());

  const before = await json(base, 'GET', '/api/students/S-YI/evaluation');
  assert.equal(before.data.groups.find((g) => g.code === 'HANDLE_JOIN_CONTROL').satisfied, true);
  const fingerprint = before.data.historyFingerprint;

  const rule = await json(base, 'PATCH', '/api/rules/links/L-HANDLE-JOIN', { samePiece: true });
  assert.equal(rule.status, 200);

  const after = await json(base, 'GET', '/api/students/S-YI/evaluation');
  const group = after.data.groups.find((g) => g.code === 'HANDLE_JOIN_CONTROL');
  assert.equal(group.satisfied, false);
  assert.equal(after.data.historyFingerprint, fingerprint);
  assert.ok(group.outstandingSegments[0].reasonCodes.includes('piece_mismatch'));

  const created = await json(base, 'POST', '/api/cases', {
    studentId: 'S-YI',
    groupCode: 'HANDLE_JOIN_CONTROL',
    linkId: 'L-HANDLE-JOIN',
  });
  assert.equal(created.status, 201);
  assert.equal(created.data.case.reservation.pieceNo, 'P-YI-H1');

  const beforePractice = await json(base, 'POST', `/api/cases/${created.data.case.id}/confirm`, {
    mentorId: 'MENTOR-LIN',
  });
  assert.equal(beforePractice.status, 200);
  assert.equal(beforePractice.data.stamp, null);
  assert.equal(beforePractice.data.signedDecision.decision, 'attend_next_class');

  await json(base, 'POST', `/api/cases/${created.data.case.id}/attendance`, { mentorId: 'MENTOR-LIN' });
  await json(base, 'POST', `/api/cases/${created.data.case.id}/receipt`, {
    action: 'smooth_join',
    pieceNo: 'P-YI-H1',
    confirmedBy: 'TA-ZHOU',
  });
  const done = await json(base, 'POST', `/api/cases/${created.data.case.id}/confirm`, {
    mentorId: 'MENTOR-LIN',
  });
  assert.equal(done.data.stamp.id, 'STAMP-S-YI-HANDLE_JOIN_CONTROL');
  assert.equal(store.db.certificates.filter((s) => s.studentId === 'S-YI').length, 1);
});

test('recycled piece is not retainable and cannot anchor a retained continuation', () => {
  const db = seedDb();
  const evaluation = evaluateStudent(db, 'S-WU');
  const gap = evaluation.outstandingSegments.find((s) => s.pieceNo === 'P-WU-OLD');
  assert.equal(gap.pieceRecycled, true);
  assert.equal(gap.retainable, false);

  const created = createCase(db, { studentId: 'S-WU', groupCode: 'BASIC_CYLINDER' });
  assert.equal(created.case.reservation, null);
  assert.notEqual(created.case.status, 'reserved');
});

test('a verifier can add a different student and process a fresh same-piece gap', async (t) => {
  const { server, base } = await startedServer();
  t.after(() => server.close());

  const student = await json(base, 'POST', '/api/students', { name: '己' });
  assert.equal(student.status, 201);
  const id = student.data.id;

  for (const [action, seq] of [['open', 1], ['raise_wall', 2]]) {
    const record = await json(base, 'POST', '/api/records', {
      studentId: id,
      sessionId: 'LS-1118',
      pieceNo: 'P-NEW-1',
      confirmedBy: 'TA-ZHOU',
      events: [{ action, seq, at: `2026-11-18T19:${seq === 1 ? '10' : '30'}:00+08:00`, mode: 'hands_on' }],
    });
    assert.equal(record.status, 201);
  }

  const evaluation = await json(base, 'GET', `/api/students/${id}/evaluation`);
  const gap = evaluation.data.outstandingSegments.find((s) => s.linkIds.includes('L-WALL-CLOSE'));
  assert.equal(gap.retainable, true);

  const created = await json(base, 'POST', '/api/cases', { studentId: id, linkId: 'L-WALL-CLOSE' });
  assert.equal(created.status, 201);
  const caseId = created.data.case.id;
  await json(base, 'POST', `/api/cases/${caseId}/attendance`, { confirmedBy: 'MENTOR-LIN' });
  await json(base, 'POST', `/api/cases/${caseId}/receipt`, { action: 'close', pieceNo: 'P-NEW-1' });
  const done = await json(base, 'POST', `/api/cases/${caseId}/confirm`, { mentorId: 'MENTOR-LIN' });
  assert.equal(done.status, 200);
  assert.ok(done.data.stamp.id.includes(id));
});
