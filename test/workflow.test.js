import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from '../src/app.js';

async function startServer(scenario = 'pre-split') {
  const server = createServer({ dbPath: ':memory:', scenario });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;

  async function api(path, options = {}) {
    const headers = { 'content-type': 'application/json', ...(options.headers || {}) };
    const response = await fetch(`${base}${path}`, {
      method: options.method || 'GET',
      headers,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    const text = await response.text();
    let json = null;
    try { json = text ? JSON.parse(text) : null; } catch { json = text; }
    return { status: response.status, body: json };
  }

  return {
    base,
    api,
    close: () => new Promise((resolve, reject) => server.close((err) => err ? reject(err) : resolve())),
  };
}

test('拆义、旧版断线补交、新义版、歧义判定与复核完整流程', async (t) => {
  const ctx = await startServer('pre-split');
  t.after(() => ctx.close());
  const { api } = ctx;

  // 种子：旧 G、拆义前版本、学生甲已开卷。
  let r = await api('/api/senses');
  assert.equal(r.status, 200);
  assert.deepEqual(r.body.filter((s) => s.status === 'active').map((s) => s.id), ['G']);

  r = await api('/api/attempts/ATT_A');
  assert.equal(r.status, 200);
  assert.equal(r.body.editionId, 'EDITION_OLD');
  const oldQuestion = r.body.snapshot.questions[0];
  assert.equal(oldQuestion.options[0].number, 'A');
  assert.equal(oldQuestion.options[0].id, 'OPT_QOLD_G');
  assert.equal(oldQuestion.options[0].correct, undefined);

  r = await api('/api/attempts/ATT_OLD_DONE/review');
  assert.equal(r.status, 200);
  assert.equal(r.body.correctCount, 1);
  assert.equal(r.body.items[0].correct.senseId, 'G');

  // 编辑请求：G -> L（照明）+ S（提示）。
  r = await api('/api/senses/G/split', {
    method: 'POST',
    body: {
      editorId: 'ED1',
      reason: '旧义兼两类用法',
      senses: [
        { id: 'L', label: '照明', semanticKey: 'illumination', definition: '点灯照亮。' },
        { id: 'S', label: '提示', semanticKey: 'signal', definition: '用灯光示意。' },
      ],
    },
  });
  assert.equal(r.status, 201, JSON.stringify(r.body));
  assert.deepEqual(r.body.oldSense.splitInto, ['L', 'S']);

  // 两名编者核义：E1=L；E2=L,S。
  for (const editorId of ['ED1', 'ED2']) {
    r = await api('/api/examples/E1/verifications', {
      method: 'POST', body: { editorId, senseIds: ['L'] },
    });
    assert.equal(r.status, 201);
  }
  assert.equal(r.body.consensus, true);

  for (const editorId of ['ED1', 'ED2']) {
    r = await api('/api/examples/E2/verifications', {
      method: 'POST', body: { editorId, senseIds: ['L', 'S'] },
    });
    assert.equal(r.status, 201);
  }

  r = await api('/api/examples/E1');
  assert.deepEqual(r.body.certifiedSenseIds, ['L']);
  r = await api('/api/examples/E2');
  assert.deepEqual(r.body.certifiedSenseIds, ['L', 'S']);

  // 可查决定：E1+照明可继续；E2+唯一照明必须教师处理。
  r = await api('/api/decisions/report');
  assert.equal(r.status, 200);
  const findRow = (e, tag) => r.body.rows.find((x) => x.exampleId === e && x.tagId === tag);
  assert.equal(findRow('E1', 'TAG_ILLUM').eligible, true);
  assert.equal(findRow('E1', 'TAG_ILLUM').reasonCode, 'ELIGIBLE_UNIQUE');
  assert.equal(findRow('E2', 'TAG_ILLUM').eligible, false);
  assert.equal(findRow('E2', 'TAG_ILLUM').reasonCode, 'AMBIGUOUS_EXAMPLE_FOR_UNIQUE_ANSWER');
  assert.equal(findRow('E1', 'TAG_CUE').reasonCode, 'EXAMPLE_TAG_MISMATCH');

  // 教师用 E1 + 照明标签出新练习；新选项 A 是 S，B 才是 L。
  r = await api('/api/questions', {
    method: 'POST',
    body: {
      id: 'Q_NEW',
      teacherId: 'TEA1',
      exampleId: 'E1',
      usageSenseId: 'L',
      requiredTagId: 'TAG_ILLUM',
      options: [
        { id: 'OPT_QNEW_S', senseId: 'S', definition: '用灯光示意；提示。', correct: false },
        { id: 'OPT_QNEW_L', senseId: 'L', definition: '点灯照亮；照明。', correct: true },
        { id: 'OPT_QNEW_COVER', definition: '遮盖。', correct: false },
      ],
    },
  });
  assert.equal(r.status, 201, JSON.stringify(r.body));
  assert.equal(r.body.eligibility.eligible, true);

  r = await api('/api/editions', {
    method: 'POST',
    body: { editionId: 'EDITION_NEW', teacherId: 'TEA1', questionIds: ['Q_NEW'] },
  });
  assert.equal(r.status, 201, JSON.stringify(r.body));
  assert.equal(r.body.edition.glossary.L.status, 'active');
  assert.equal(r.body.edition.glossary.S.status, 'active');

  // E2 可以建草稿，但不能按“唯一照明答案”封题。
  r = await api('/api/questions', {
    method: 'POST',
    body: {
      id: 'Q_E2_BAD',
      exampleId: 'E2',
      usageSenseId: 'L',
      requiredTagId: 'TAG_ILLUM',
      options: [
        { id: 'OPT_E2_L', senseId: 'L', definition: '照明', correct: true },
        { id: 'OPT_E2_S', senseId: 'S', definition: '提示', correct: false },
      ],
    },
  });
  assert.equal(r.status, 201);
  assert.equal(r.body.eligibility.eligible, false);
  r = await api('/api/editions', {
    method: 'POST',
    body: { editionId: 'EDITION_E2_BAD', questionIds: ['Q_E2_BAD'] },
  });
  assert.equal(r.status, 422);
  assert.equal(r.body.error.code, 'EDITION_NOT_READY');

  // 学生乙按新义版开卷。
  r = await api('/api/editions/EDITION_NEW/attempts', {
    method: 'POST', body: { attemptId: 'ATT_B', studentId: 'STU_B' },
  });
  assert.equal(r.status, 201);
  const bQuestion = r.body.attempt.snapshot.questions[0];
  assert.equal(bQuestion.options[0].number, 'A');
  assert.equal(bQuestion.options[0].id, 'OPT_QNEW_S');
  assert.equal(bQuestion.options[1].number, 'B');
  assert.equal(bQuestion.options[1].id, 'OPT_QNEW_L');

  // 断线补交若走到新版路径，明确拒绝串版。
  const stalePayload = {
    attemptId: 'ATT_A',
    clientSubmissionId: 'offline-retry-1',
    answers: [{ questionId: 'Q_OLD', optionNumber: 'A' }],
  };
  r = await api('/api/editions/EDITION_NEW/submit', { method: 'POST', body: stalePayload });
  assert.equal(r.status, 409);
  assert.equal(r.body.error.code, 'EDITION_MISMATCH');

  // 回到 ATT_A：选项号 A 只按拆义前快照解析为 G，判对。
  r = await api('/api/attempts/ATT_A/submit', { method: 'POST', body: stalePayload });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.equal(r.body.review.editionId, 'EDITION_OLD');
  assert.equal(r.body.review.correctCount, 1);
  assert.equal(r.body.review.items[0].selected.optionId, 'OPT_QOLD_G');
  assert.match(r.body.review.items[0].selected.definition, /拆义前旧释义/);

  // 相同断线重试幂等返回同一复核。
  const firstReviewId = r.body.review.id;
  r = await api('/api/attempts/ATT_A/submit', { method: 'POST', body: stalePayload });
  assert.equal(r.status, 200);
  assert.equal(r.body.idempotentReplay, true);
  assert.equal(r.body.review.id, firstReviewId);

  // 不同答案不能覆盖已交卷。
  r = await api('/api/attempts/ATT_A/submit', {
    method: 'POST',
    body: { answers: [{ questionId: 'Q_OLD', optionNumber: 'B' }] },
  });
  assert.equal(r.status, 409);

  // 旧 optionId 拿到新版作答中不能套用。
  r = await api('/api/editions/EDITION_NEW/attempts', {
    method: 'POST', body: { attemptId: 'ATT_C', studentId: 'STU_C' },
  });
  assert.equal(r.status, 201);
  r = await api('/api/attempts/ATT_C/submit', {
    method: 'POST',
    body: { answers: [{ questionId: 'Q_NEW', optionId: 'OPT_QOLD_G' }] },
  });
  assert.equal(r.status, 400);
  assert.equal(r.body.error.code, 'OPTION_NOT_VISIBLE_IN_ATTEMPT');

  // 新版 A 确实是 S；乙选 B=L 才对。
  r = await api('/api/attempts/ATT_B/submit', {
    method: 'POST',
    body: { answers: [{ questionId: 'Q_NEW', optionNumber: 'B' }] },
  });
  assert.equal(r.status, 200);
  assert.equal(r.body.review.items[0].isCorrect, true);
  assert.equal(r.body.review.items[0].selected.optionId, 'OPT_QNEW_L');

  // 复核按学生实际见到的 G 释义；当前词典另行说明 G 已拆。
  r = await api('/api/attempts/ATT_A/review');
  assert.equal(r.status, 200);
  const item = r.body.items[0];
  assert.equal(item.correct.senseId, 'G');
  assert.match(item.correct.definition, /旧释义/);
  assert.equal(r.body.currentDictionary.senses.G.status, 'split');
  assert.deepEqual(r.body.currentDictionary.senses.G.splitInto, ['L', 'S']);

  // 改词头影响当前索引，但不改旧版快照，也不消除歧义。
  r = await api('/api/headwords/HW_DADENG', { method: 'PATCH', body: { text: '打灯儿' } });
  assert.equal(r.status, 200);
  r = await api('/api/index?q=打灯儿');
  assert.equal(r.body.count, 1);
  assert.equal(r.body.entries[0].headword, '打灯儿');
  r = await api('/api/editions/EDITION_OLD?view=teacher');
  assert.equal(r.body.headword.text, '打灯');
  r = await api('/api/decisions/report');
  assert.equal(findRowLocal(r.body, 'E2', 'TAG_ILLUM').eligible, false);
  await api('/api/headwords/HW_DADENG', { method: 'PATCH', body: { text: '打灯' } });

  // 只把 E1 核定用法改成 L、S：同一个唯一答案不再成立，也不能发新版。
  r = await api('/api/examples/E1/certified-usages', {
    method: 'PUT',
    body: { senseIds: ['L', 'S'], editorIds: ['ED1', 'ED2'] },
  });
  assert.equal(r.status, 200);
  r = await api('/api/questions/Q_NEW/evaluate', { method: 'POST' });
  assert.equal(r.body.eligible, false);
  assert.equal(r.body.reasonCode, 'AMBIGUOUS_EXAMPLE_FOR_UNIQUE_ANSWER');
  r = await api('/api/editions', {
    method: 'POST', body: { editionId: 'EDITION_NEW_BAD_AMBIG', questionIds: ['Q_NEW'] },
  });
  assert.equal(r.status, 422);

  // 恢复唯 L 后可继续；只把教师标签换成“提示”，该已核实句子不再适用。
  r = await api('/api/examples/E1/certified-usages', {
    method: 'PUT', body: { senseIds: ['L'], editorIds: ['ED1', 'ED2'] },
  });
  assert.equal(r.status, 200);
  r = await api('/api/questions/Q_NEW/evaluate', { method: 'POST' });
  assert.equal(r.body.eligible, true);

  r = await api('/api/questions/Q_NEW', {
    method: 'PATCH', body: { discriminationLabel: '提示' },
  });
  assert.equal(r.status, 200);
  assert.equal(r.body.eligibility.eligible, false);
  assert.equal(r.body.eligibility.reasonCode, 'EXAMPLE_TAG_MISMATCH');
  r = await api('/api/editions', {
    method: 'POST', body: { editionId: 'EDITION_CUE_BAD', questionIds: ['Q_NEW'] },
  });
  assert.equal(r.status, 422);

  // 恢复照明标签后可另发一版；旧版和已交卷复核不变。
  r = await api('/api/questions/Q_NEW', {
    method: 'PATCH', body: { requiredTagId: 'TAG_ILLUM' },
  });
  assert.equal(r.body.eligibility.eligible, true);
  r = await api('/api/editions', {
    method: 'POST', body: { editionId: 'EDITION_NEW_2', questionIds: ['Q_NEW'] },
  });
  assert.equal(r.status, 201);
  r = await api('/api/attempts/ATT_A/review');
  assert.equal(r.body.editionId, 'EDITION_OLD');
  assert.equal(r.body.correctCount, 1);
});

function findRowLocal(report, exampleId, tagId) {
  return report.rows.find((x) => x.exampleId === exampleId && x.tagId === tagId);
}

test('合并义项会迁移当前用法，但冻结版本与复核不变', async (t) => {
  const ctx = await startServer('post-split');
  t.after(() => ctx.close());
  const { api } = ctx;

  let r = await api('/api/editions/EDITION_NEW?view=teacher');
  assert.equal(r.status, 200);
  assert.deepEqual(r.body.answerKey.Q_NEW, ['OPT_QNEW_L']);

  r = await api('/api/editor/senses/merge', {
    method: 'POST',
    body: {
      editorId: 'ED1',
      sourceSenseIds: ['L', 'S'],
      targetSense: {
        id: 'M',
        label: '灯光用途合并义',
        definition: '用灯照亮或示意。',
      },
    },
  });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.equal(r.body.sense.id, 'M');

  r = await api('/api/senses/L');
  assert.equal(r.body.status, 'merged');
  assert.equal(r.body.mergedInto, 'M');
  r = await api('/api/examples/E1');
  assert.deepEqual(r.body.certifiedSenseIds, ['M']);
  r = await api('/api/examples/E2');
  assert.deepEqual(r.body.certifiedSenseIds, ['M']);

  // 已发行新版仍是 L/S 自足快照。
  r = await api('/api/editions/EDITION_NEW?view=teacher');
  assert.equal(r.body.glossary.L.status, 'active');
  assert.equal(r.body.glossary.S.status, 'active');
  assert.deepEqual(r.body.answerKey.Q_NEW, ['OPT_QNEW_L']);

  // 学生乙已经按新义版交卷/开卷，仍可按 L 选项复核。
  r = await api('/api/attempts/ATT_B/submit', {
    method: 'POST',
    body: { answers: [{ questionId: 'Q_NEW', optionId: 'OPT_QNEW_L' }] },
  });
  assert.equal(r.status, 200);
  assert.equal(r.body.review.items[0].isCorrect, true);
  r = await api('/api/attempts/ATT_B/review');
  assert.equal(r.body.items[0].correct.senseId, 'L');
  assert.equal(r.body.currentDictionary.senses.L.status, 'merged');
});
