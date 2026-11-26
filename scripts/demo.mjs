import assert from 'node:assert/strict';
import { createServer } from '../src/app.js';

const server = createServer({ dbPath: ':memory:', scenario: 'pre-split' });
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;

async function req(method, path, body) {
  const res = await fetch(`${base}${path}`, {
    method,
    headers: { 'content-type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json();
  return { status: res.status, json };
}

function show(title, value) {
  console.log(`\n## ${title}`);
  console.log(typeof value === 'string' ? value : JSON.stringify(value, null, 2));
}

try {
  console.log('方言社核心剧本：按版封题课堂');

  let r = await req('POST', '/api/senses/G/split', {
    editorId: 'ED1',
    senses: [
      { id: 'L', label: '照明', semanticKey: 'illumination', definition: '点灯照亮。' },
      { id: 'S', label: '提示', semanticKey: 'signal', definition: '用灯光示意。' },
    ],
  });
  assert.equal(r.status, 201);
  show('1. 编辑拆义 G -> L/S', r.json.oldSense.splitInto);

  for (const editorId of ['ED1', 'ED2']) {
    await req('POST', '/api/examples/E1/verifications', { editorId, senseIds: ['L'] });
    await req('POST', '/api/examples/E2/verifications', { editorId, senseIds: ['L', 'S'] });
  }
  show('2. 两编者核义', {
    E1: (await req('GET', '/api/examples/E1')).json.certifiedSenseIds,
    E2: (await req('GET', '/api/examples/E2')).json.certifiedSenseIds,
  });

  const report = (await req('GET', '/api/decisions/report')).json;
  show('3. 可查判定', report.rows.map((x) => ({
    candidate: x.candidateId,
    eligible: x.eligible,
    code: x.reasonCode,
  })));

  r = await req('POST', '/api/questions', {
    id: 'Q_NEW',
    teacherId: 'TEA1',
    exampleId: 'E1',
    usageSenseId: 'L',
    requiredTagId: 'TAG_ILLUM',
    options: [
      { id: 'OPT_QNEW_S', senseId: 'S', definition: '灯光示意；提示。', correct: false },
      { id: 'OPT_QNEW_L', senseId: 'L', definition: '点灯照亮；照明。', correct: true },
      { id: 'OPT_QNEW_X', definition: '遮盖。', correct: false },
    ],
  });
  assert.equal(r.json.eligibility.eligible, true);
  await req('POST', '/api/editions', { editionId: 'EDITION_NEW', questionIds: ['Q_NEW'] });
  await req('POST', '/api/editions/EDITION_NEW/attempts', { attemptId: 'ATT_B', studentId: 'STU_B' });
  show('4. 教师封新版 EDITION_NEW，学生乙开卷', '新版 A=S，B=L');

  const stale = await req('POST', '/api/editions/EDITION_NEW/submit', {
    attemptId: 'ATT_A',
    clientSubmissionId: 'offline-1',
    answers: [{ questionId: 'Q_OLD', optionNumber: 'A' }],
  });
  assert.equal(stale.status, 409);
  show('5. 旧 ATT_A 试图把旧 A 套到新版路径', stale.json.error.code);

  const oldSubmit = await req('POST', '/api/attempts/ATT_A/submit', {
    clientSubmissionId: 'offline-1',
    answers: [{ questionId: 'Q_OLD', optionNumber: 'A' }],
  });
  assert.equal(oldSubmit.status, 200);
  assert.equal(oldSubmit.json.review.correctCount, 1);
  show('6. 回到 ATT_A 快照补交：旧 A=G，仍判对', {
    gradedAgainst: oldSubmit.json.review.editionId,
    selected: oldSubmit.json.review.items[0].selected,
  });

  await req('PUT', '/api/examples/E1/certified-usages', { senseIds: ['L', 'S'], editorIds: ['ED1', 'ED2'] });
  const ambiguous = await req('POST', '/api/questions/Q_NEW/evaluate');
  assert.equal(ambiguous.json.reasonCode, 'AMBIGUOUS_EXAMPLE_FOR_UNIQUE_ANSWER');
  show('7. 把 E1 改成兼 L/S 后', ambiguous.json.reason);

  await req('PUT', '/api/examples/E1/certified-usages', { senseIds: ['L'], editorIds: ['ED1', 'ED2'] });
  await req('PATCH', '/api/questions/Q_NEW', { requiredTagId: 'TAG_CUE' });
  const cue = await req('POST', '/api/questions/Q_NEW/evaluate');
  assert.equal(cue.json.reasonCode, 'EXAMPLE_TAG_MISMATCH');
  show('8. 恢复唯 L，但标签换成“提示”', cue.json.reason);

  const review = (await req('GET', '/api/attempts/ATT_A/review')).json;
  show('9. 学生甲复核仍见实际选项和 G 释义', {
    editionId: review.editionId,
    selectedDefinition: review.items[0].selected.definition,
    currentGStatus: review.currentDictionary.senses.G.status,
    splitInto: review.currentDictionary.senses.G.splitInto,
  });

  console.log('\n演示通过。完整 HTTP 流程见 README.md，自动断言见 npm test。');
} finally {
  await new Promise((resolve) => server.close(resolve));
}
