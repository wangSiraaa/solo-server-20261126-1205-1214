import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createServer } from '../src/server.js';

async function startedServer() {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'phonics-test-')), 'db.json');
  const { server, store } = createServer({ dataFile: file });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const base = `http://127.0.0.1:${port}`;

  async function api(method, route, body) {
    const res = await fetch(`${base}${route}`, {
      method,
      headers: body === undefined ? {} : { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await res.text();
    const json = text ? JSON.parse(text) : null;
    return { status: res.status, body: json };
  }

  async function close() {
    await new Promise((resolve) => server.close(resolve));
  }

  return { file, store, base, api, close };
}

async function seedPhonics(api, { studentId = 'ada', practiceDays = [1, 2, 3, 4, 5, 6, 7, 8] } = {}) {
  await api('POST', '/api/teacher/kps', { id: 'kp-a', name: 'short a' });
  await api('POST', '/api/teacher/kps', { id: 'kp-long-a', name: 'long a' });
  await api('POST', '/api/teacher/cards', { id: 'card-a1', glyph: 'a' });
  await api('POST', '/api/teacher/cards', { id: 'card-a2', glyph: 'a' });
  await api('POST', '/api/teacher/cards', { id: 'card-glyph-a', glyph: 'a' });
  await api('POST', '/api/teacher/cards', { id: 'card-extra', glyph: 'x' });
  await api('POST', '/api/teacher/units', { id: 'u1', name: 'Unit 1' });
  await api('POST', '/api/teacher/units', { id: 'u2', name: 'Unit 2' });
  await api('POST', '/api/teacher/assignments', {
    id: 'as-a1',
    unitId: 'u1',
    cardId: 'card-a1',
    kpId: 'kp-a',
    expected: 'æ',
    hints: ['listen: æ'],
  });
  await api('POST', '/api/teacher/assignments', {
    id: 'as-a2',
    unitId: 'u1',
    cardId: 'card-a2',
    kpId: 'kp-a',
    expected: 'æ',
    hints: ['listen: æ again'],
  });
  await api('POST', '/api/teacher/assignments', {
    id: 'as-glyph-u1',
    unitId: 'u1',
    cardId: 'card-glyph-a',
    kpId: 'kp-a',
    expected: 'æ',
    hints: ['short a'],
  });
  await api('POST', '/api/teacher/assignments', {
    id: 'as-glyph-u2',
    unitId: 'u2',
    cardId: 'card-glyph-a',
    kpId: 'kp-long-a',
    expected: 'eɪ',
    hints: ['long a'],
  });
  await api('POST', '/api/teacher/gates', {
    id: 'gate-app',
    name: 'Application level',
    opensOnDay: 8,
    requirements: [{ kpId: 'kp-a', independentRounds: 2 }],
  });
  await api('POST', '/api/students', { id: studentId, name: studentId, practiceDays });
  await api('POST', '/api/teacher/packs', {
    id: 'pack-d1',
    day: 1,
    unitId: 'u1',
    assignmentIds: ['as-a1'],
    worksheetId: 'ws-d1',
  });
  await api('POST', '/api/teacher/packs', {
    id: 'pack-d4',
    day: 4,
    unitId: 'u1',
    assignmentIds: ['as-a2'],
    worksheetId: 'ws-d4',
  });
}

function correctReceipt(overrides = {}) {
  return {
    studentId: 'ada',
    roundId: overrides.roundId,
    worksheetId: overrides.worksheetId,
    deviceId: 'device-home',
    copyId: 'copy-1',
    returnedAt: '2026-11-01T18:00:00Z',
    answers: [
      {
        assignmentId: 'as-a1',
        cardId: 'card-a1',
        firstGuess: 'æ',
        finalAnswer: 'æ',
        revealedHints: [],
      },
    ],
    ...overrides,
  };
}

test('same round reprinted or returned by two devices counts once, while day 1 and day 4 count twice', async () => {
  const t = await startedServer();
  try {
    await seedPhonics(t.api);

    const claim1 = await t.api('POST', '/api/rounds/claim', {
      studentId: 'ada',
      packId: 'pack-d1',
      deviceId: 'device-a',
    });
    assert.equal(claim1.status, 201);
    assert.equal(claim1.body.round.worksheetId, 'ws-d1');

    const receipt = correctReceipt({ roundId: claim1.body.round.id });
    const sub1 = await t.api('POST', '/api/receipts', receipt);
    assert.equal(sub1.status, 201);
    assert.equal(sub1.body.round.reconciliation.kpResults['kp-a'].state, 'independent');

    // Second device and old worksheet identity deliver the same facts later.
    const duplicate = await t.api('POST', '/api/receipts', {
      ...receipt,
      deviceId: 'device-b',
      copyId: 'copy-reprint',
      returnedAt: '2026-11-03T09:00:00Z',
      roundId: undefined,
      worksheetId: 'ws-d1',
    });
    assert.equal(duplicate.status, 200);
    assert.equal(duplicate.body.duplicate, true);

    let progress = await t.api('GET', '/api/students/ada/progress');
    assert.equal(progress.body.mastery[0].stage, 1);
    assert.deepEqual(progress.body.mastery[0].qualifyingDays, [1]);

    const claim4 = await t.api('POST', '/api/rounds/claim', {
      studentId: 'ada',
      packId: 'pack-d4',
      deviceId: 'device-a',
    });
    const sub4 = await t.api('POST', '/api/receipts', {
      studentId: 'ada',
      roundId: claim4.body.round.id,
      deviceId: 'device-a',
      returnedAt: 'day4-late',
      answers: [
        { assignmentId: 'as-a2', cardId: 'card-a2', firstGuess: 'æ', finalAnswer: 'æ', revealedHints: [] },
      ],
    });
    assert.equal(sub4.status, 201);

    const day4Gate = await t.api('GET', '/api/gates/gate-app/access?studentId=ada&day=4');
    assert.equal(day4Gate.body.status, 'scheduled_locked');
    assert.equal(day4Gate.body.requirements[0].earnedIndependentRounds, 2);

    const gate = await t.api('GET', '/api/gates/gate-app/access?studentId=ada&day=8');
    assert.equal(gate.body.open, true);
    assert.equal(gate.body.status, 'open');

    const opp = await t.api('GET', '/api/students/ada/opportunities?day=8');
    assert.deepEqual(opp.body.practiceDays, [1, 2, 3, 4, 5, 6, 7, 8]);
  } finally {
    await t.close();
  }
});

test('hint on another same-KP card in the same round blocks independence, but a later round can reset', async () => {
  const t = await startedServer();
  try {
    await seedPhonics(t.api);
    await t.api('POST', '/api/teacher/packs', {
      id: 'pack-cross',
      day: 3,
      assignmentIds: ['as-a1', 'as-a2'],
      worksheetId: 'ws-cross',
    });
    const claim = await t.api('POST', '/api/rounds/claim', { studentId: 'ada', packId: 'pack-cross' });

    const hintedFirst = await t.api('POST', '/api/receipts', {
      studentId: 'ada',
      roundId: claim.body.round.id,
      deviceId: 'd1',
      answers: [
        {
          assignmentId: 'as-a1',
          cardId: 'card-a1',
          firstGuess: '?',
          finalAnswer: 'æ',
          revealedHints: ['listen: æ'],
        },
        { assignmentId: 'as-a2', cardId: 'card-a2', firstGuess: 'æ', finalAnswer: 'æ', revealedHints: [] },
      ],
    });

    const result = hintedFirst.body.round.reconciliation.kpResults['kp-a'];
    assert.equal(result.state, 'hint_assisted');
    assert.equal(result.sufficientForOneRound, false);
    const secondAnswer = hintedFirst.body.submission.kpClaims['kp-a'].evidence[1];
    assert.equal(secondAnswer.hintBeforeAnswer, true);
    assert.equal(secondAnswer.independentCorrect, false);

    // A prior-round hint does not contaminate a newly issued day-4 round.
    const claim4 = await t.api('POST', '/api/rounds/claim', { studentId: 'ada', packId: 'pack-d4' });
    await t.api('POST', '/api/receipts', {
      studentId: 'ada',
      roundId: claim4.body.round.id,
      answers: [
        { assignmentId: 'as-a2', cardId: 'card-a2', firstGuess: 'æ', finalAnswer: 'æ', revealedHints: [] },
      ],
    });
    const progress = await t.api('GET', '/api/students/ada/progress');
    const mastery = progress.body.mastery.find((m) => m.kpId === 'kp-a');
    assert.equal(mastery.stage, 1);
    assert.deepEqual(mastery.qualifyingDays, [4]);
  } finally {
    await t.close();
  }
});

test('replacing day 4 with a hinted alternate-card answer leaves the gate locked', async () => {
  const t = await startedServer();
  try {
    await seedPhonics(t.api, { studentId: 'ivy' });

    const c1 = await t.api('POST', '/api/rounds/claim', { studentId: 'ivy', packId: 'pack-d1' });
    await t.api('POST', '/api/receipts', {
      ...correctReceipt({ studentId: 'ivy', roundId: c1.body.round.id }),
      answers: [
        { assignmentId: 'as-a1', cardId: 'card-a1', firstGuess: 'æ', finalAnswer: 'æ', revealedHints: [] },
      ],
    });

    const c4 = await t.api('POST', '/api/rounds/claim', { studentId: 'ivy', packId: 'pack-d4' });
    const hinted = await t.api('POST', '/api/receipts', {
      studentId: 'ivy',
      roundId: c4.body.round.id,
      deviceId: 'device-school',
      answers: [
        {
          assignmentId: 'as-a2',
          cardId: 'card-a2',
          firstGuess: 'uh',
          finalAnswer: 'æ',
          revealedHints: ['listen: æ again'],
        },
      ],
    });
    assert.equal(hinted.body.round.reconciliation.kpResults['kp-a'].state, 'hint_assisted');

    const gate = await t.api('GET', '/api/gates/gate-app/access?studentId=ivy&day=8');
    assert.equal(gate.body.open, false);
    assert.equal(gate.body.status, 'requirements_locked');
    assert.equal(gate.body.requirements[0].earnedIndependentRounds, 1);
    assert.equal(gate.body.requirements[0].stillNeeds, 1);

    const opp = await t.api('GET', '/api/students/ivy/opportunities?day=5');
    const req = opp.body.gates[0].requirements[0];
    assert.deepEqual(req.earnedDays, [1]);
    assert.ok(req.contributableDays.includes(5));
    assert.equal(req.nextPracticeDay, 5);
  } finally {
    await t.close();
  }
});

test('two distinct same-day rounds contribute at most one daily stage', async () => {
  const t = await startedServer();
  try {
    await seedPhonics(t.api);
    await t.api('POST', '/api/teacher/packs', {
      id: 'pack-day1-b',
      day: 1,
      assignmentIds: ['as-a2'],
      worksheetId: 'ws-day1-b',
    });

    for (const [packId, as] of [
      ['pack-d1', 'as-a1'],
      ['pack-day1-b', 'as-a2'],
    ]) {
      const claim = await t.api('POST', '/api/rounds/claim', { studentId: 'ada', packId });
      const receipt = correctReceipt({
        roundId: claim.body.round.id,
        answers: [{ assignmentId: as, cardId: as === 'as-a1' ? 'card-a1' : 'card-a2', finalAnswer: 'æ' }],
      });
      await t.api('POST', '/api/receipts', receipt);
    }

    const progress = await t.api('GET', '/api/students/ada/progress');
    const mastery = progress.body.mastery.find((m) => m.kpId === 'kp-a');
    assert.equal(mastery.stage, 1);
    assert.deepEqual(mastery.qualifyingDays, [1]);
    const capped = progress.body.evidence.filter((e) => e.reason === 'daily_max_one_round');
    assert.equal(capped.length, 1);
  } finally {
    await t.close();
  }
});

test('same glyph in two units is reconciled to the lesson-specific sound and knowledge point', async () => {
  const t = await startedServer();
  try {
    await seedPhonics(t.api);
    const pack2 = await t.api('POST', '/api/teacher/packs', {
      id: 'pack-u2',
      day: 2,
      unitId: 'u2',
      assignmentIds: ['as-glyph-u2'],
      worksheetId: 'ws-u2',
    });
    const claim = await t.api('POST', '/api/rounds/claim', { studentId: 'ada', packId: 'pack-u2' });

    const wrongSoundForLesson = await t.api('POST', '/api/receipts', {
      studentId: 'ada',
      roundId: claim.body.round.id,
      answers: [{ assignmentId: 'as-glyph-u2', cardId: 'card-glyph-a', firstGuess: 'æ', finalAnswer: 'æ' }],
    });
    assert.equal(wrongSoundForLesson.body.submission.kpClaims['kp-long-a'].state, 'incorrect');

    const rightSound = await t.api('POST', '/api/receipts', {
      studentId: 'ada',
      roundId: claim.body.round.id,
      deviceId: 'device-2',
      copyId: 'copy-right',
      answers: [{ assignmentId: 'as-glyph-u2', cardId: 'card-glyph-a', firstGuess: 'eɪ', finalAnswer: 'eɪ' }],
    });
    // Different outcome copies are an evidence conflict, not two reviews.
    assert.equal(rightSound.body.round.status, 'conflicted');
    assert.equal(rightSound.body.round.reconciliation.kpResults['kp-long-a'].state, 'conflicted');
  } finally {
    await t.close();
  }
});

test('closed pack rejects temporary extra cards from knowledge-point accumulation', async () => {
  const t = await startedServer();
  try {
    await seedPhonics(t.api);
    await t.api('POST', '/api/teacher/kps', { id: 'kp-extra' });
    await t.api('POST', '/api/teacher/assignments', {
      id: 'as-extra',
      unitId: 'u1',
      cardId: 'card-extra',
      kpId: 'kp-extra',
      expected: 'ks',
    });
    const claim = await t.api('POST', '/api/rounds/claim', { studentId: 'ada', packId: 'pack-d1' });
    const res = await t.api('POST', '/api/receipts', {
      studentId: 'ada',
      roundId: claim.body.round.id,
      answers: [
        { assignmentId: 'as-a1', cardId: 'card-a1', finalAnswer: 'æ' },
        { assignmentId: 'as-extra', cardId: 'card-extra', finalAnswer: 'ks' },
      ],
    });
    assert.equal(res.status, 201);
    assert.equal(res.body.submission.extraCardCount, 1);
    assert.equal(res.body.round.reconciliation.kpResults['kp-extra'], undefined);
    const progress = await t.api('GET', '/api/students/ada/progress');
    assert.equal(progress.body.mastery.some((m) => m.kpId === 'kp-extra'), false);
  } finally {
    await t.close();
  }
});

test('adaptive classroom blocks an unissued next round and targets the error with another card', async () => {
  const t = await startedServer();
  try {
    await seedPhonics(t.api);
    await t.api('POST', '/api/students', {
      id: 'leo',
      mode: 'adaptive',
      practiceDays: [1, 2, 5, 8],
    });

    const first = await t.api('POST', '/api/rounds/claim', { studentId: 'leo', mode: 'adaptive', day: 1, count: 1 });
    assert.equal(first.status, 201);
    assert.equal(first.body.round.items[0].kpId, 'kp-a');

    const earlyNext = await t.api('POST', '/api/rounds/claim', { studentId: 'leo', mode: 'adaptive', day: 1 });
    assert.equal(earlyNext.status, 409);
    assert.equal(earlyNext.body.error.code, 'previous_round_outstanding');

    await t.api('POST', '/api/receipts', {
      studentId: 'leo',
      roundId: first.body.round.id,
      answers: [
        {
          assignmentId: first.body.round.items[0].assignmentId,
          cardId: first.body.round.items[0].cardId,
          firstGuess: 'oh',
          finalAnswer: 'ɒ',
        },
      ],
    });

    const next = await t.api('POST', '/api/rounds/claim', { studentId: 'leo', mode: 'adaptive', day: 2, count: 1 });
    assert.equal(next.status, 201);
    assert.deepEqual(next.body.round.focusKpIds, ['kp-a']);
    assert.notEqual(next.body.round.items[0].cardId, first.body.round.items[0].cardId);

    const invented = await t.api('POST', '/api/receipts', {
      studentId: 'leo',
      roundId: 'r-made-up-offline',
      answers: [],
    });
    assert.equal(invented.status, 409);
    assert.equal(invented.body.error.code, 'unknown_round_or_worksheet');
  } finally {
    await t.close();
  }
});

test('conflicting device copies isolate that KP, create repair, and a new independent round restores progress', async () => {
  const t = await startedServer();
  try {
    await seedPhonics(t.api, { studentId: 'maya', practiceDays: [1, 4, 5, 8] });

    const d1 = await t.api('POST', '/api/rounds/claim', { studentId: 'maya', packId: 'pack-d1' });
    await t.api('POST', '/api/receipts', {
      studentId: 'maya',
      roundId: d1.body.round.id,
      deviceId: 'device-a',
      answers: [{ assignmentId: 'as-a1', cardId: 'card-a1', finalAnswer: 'æ' }],
    });

    const d4 = await t.api('POST', '/api/rounds/claim', { studentId: 'maya', packId: 'pack-d4' });
    await t.api('POST', '/api/receipts', {
      studentId: 'maya',
      roundId: d4.body.round.id,
      deviceId: 'device-a',
      copyId: 'a',
      answers: [{ assignmentId: 'as-a2', cardId: 'card-a2', firstGuess: 'æ', finalAnswer: 'æ', revealedHints: [] }],
    });
    const conflict = await t.api('POST', '/api/receipts', {
      studentId: 'maya',
      roundId: d4.body.round.id,
      deviceId: 'device-b',
      copyId: 'b',
      answers: [
        {
          assignmentId: 'as-a2',
          cardId: 'card-a2',
          firstGuess: 'uh',
          finalAnswer: 'æ',
          revealedHints: ['listen: æ again'],
        },
      ],
    });
    assert.equal(conflict.body.round.status, 'conflicted');
    assert.equal(conflict.body.round.reconciliation.kpResults['kp-a'].state, 'conflicted');

    const evidence = await t.api('GET', `/api/rounds/${d4.body.round.id}/evidence`);
    assert.equal(evidence.body.sufficiency['kp-a'].oneRoundCredit, false);
    assert.ok(evidence.body.reconciliation.conflicts.length >= 1);

    const opp = await t.api('GET', '/api/students/maya/opportunities?day=5');
    const order = opp.body.makeupOrders.find((o) => o.conflictRoundId === d4.body.round.id && o.status === 'open');
    assert.ok(order);

    const repairClaim = await t.api('POST', '/api/rounds/claim', {
      studentId: 'maya',
      makeupOrderId: order.id,
      day: 5,
      deviceId: 'device-a',
    });
    assert.equal(repairClaim.status, 201);
    assert.equal(repairClaim.body.round.purpose, 'makeup');

    const repairAnswer = repairClaim.body.round.items[0];
    const repairReceipt = await t.api('POST', '/api/receipts', {
      studentId: 'maya',
      roundId: repairClaim.body.round.id,
      deviceId: 'device-a',
      answers: [
        {
          assignmentId: repairAnswer.assignmentId,
          cardId: repairAnswer.cardId,
          firstGuess: repairAnswer.expected,
          finalAnswer: repairAnswer.expected,
          revealedHints: [],
        },
      ],
    });
    assert.equal(repairReceipt.body.round.status, 'confirmed');

    const gate = await t.api('GET', '/api/gates/gate-app/access?studentId=maya&day=8');
    assert.equal(gate.body.open, true);
    assert.deepEqual(gate.body.requirements[0].earnedDays, [1, 5]);
  } finally {
    await t.close();
  }
});

test('teacher can adjudicate one trustworthy device copy instead of repairing', async () => {
  const t = await startedServer();
  try {
    await seedPhonics(t.api, { studentId: 'judy', practiceDays: [4, 8] });
    const claim = await t.api('POST', '/api/rounds/claim', { studentId: 'judy', packId: 'pack-d4' });
    const trusted = await t.api('POST', '/api/receipts', {
      studentId: 'judy',
      roundId: claim.body.round.id,
      deviceId: 'device-a',
      answers: [independentAnswerForTest('as-a2', 'card-a2')],
    });
    await t.api('POST', '/api/receipts', {
      studentId: 'judy',
      roundId: claim.body.round.id,
      deviceId: 'device-b',
      answers: [
        {
          assignmentId: 'as-a2',
          cardId: 'card-a2',
          firstGuess: 'uh',
          finalAnswer: 'æ',
          revealedHints: ['listen: æ again'],
        },
      ],
    });

    const decided = await t.api(
      'POST',
      `/api/teacher/rounds/${claim.body.round.id}/adjudicate`,
      { submissionId: trusted.body.submission.id, note: 'classroom log is authoritative' },
    );
    assert.equal(decided.status, 200);
    assert.equal(decided.body.round.status, 'confirmed');
    assert.equal(decided.body.round.reconciliation.kpResults['kp-a'].state, 'independent');

    const opp = await t.api('GET', '/api/students/judy/opportunities?day=8');
    assert.equal(opp.body.makeupOrders[0].status, 'superseded_by_adjudication');
    assert.equal(opp.body.gates[0].requirements[0].earnedIndependentRounds, 1);
  } finally {
    await t.close();
  }
});

function independentAnswerForTest(assignmentId, cardId) {
  return { assignmentId, cardId, firstGuess: 'æ', finalAnswer: 'æ', revealedHints: [] };
}

test('baseline two rounds are not rejected after adding an unlearned gate requirement', async () => {
  const t = await startedServer();
  try {
    await seedPhonics(t.api);
    for (const [packId, as] of [
      ['pack-d1', 'as-a1'],
      ['pack-d4', 'as-a2'],
    ]) {
      const claim = await t.api('POST', '/api/rounds/claim', { studentId: 'ada', packId });
      await t.api('POST', '/api/receipts', {
        studentId: 'ada',
        roundId: claim.body.round.id,
        answers: [
          {
            assignmentId: as,
            cardId: as === 'as-a1' ? 'card-a1' : 'card-a2',
            firstGuess: 'æ',
            finalAnswer: 'æ',
          },
        ],
      });
    }

    let gate = await t.api('GET', '/api/gates/gate-app/access?studentId=ada&day=8');
    assert.equal(gate.body.open, true);

    await t.api('POST', '/api/teacher/kps', { id: 'kp-unlearned', name: 'new point' });
    const patch = await t.api('PATCH', '/api/teacher/gates/gate-app', {
      requirements: [
        { kpId: 'kp-a', independentRounds: 2 },
        { kpId: 'kp-unlearned', independentRounds: 2 },
      ],
    });
    assert.equal(patch.status, 200);

    gate = await t.api('GET', '/api/gates/gate-app/access?studentId=ada&day=8');
    assert.equal(gate.body.open, false);
    assert.equal(gate.body.status, 'requirements_locked');
    const added = gate.body.requirements.find((r) => r.kpId === 'kp-unlearned');
    assert.equal(added.earnedIndependentRounds, 0);
    assert.equal(added.stillNeeds, 2);
  } finally {
    await t.close();
  }
});

test('firstGuess short string alone is enough for server-side scoring', async () => {
  const t = await startedServer();
  try {
    await seedPhonics(t.api, { studentId: 'gus' });
    const claim = await t.api('POST', '/api/rounds/claim', { studentId: 'gus', packId: 'pack-d1' });
    const res = await t.api('POST', '/api/receipts', {
      studentId: 'gus',
      roundId: claim.body.round.id,
      answers: [{ cardId: 'card-a1', guess: 'æ', hints: [] }],
    });
    assert.equal(res.status, 201);
    assert.equal(res.body.round.reconciliation.kpResults['kp-a'].state, 'independent');
  } finally {
    await t.close();
  }
});

test('persisted round and worksheet survive server recreation', async () => {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'phonics-restart-')), 'db.json');
  let first = createServer({ dataFile: file });
  await new Promise((r) => first.server.listen(0, '127.0.0.1', r));
  const port = first.server.address().port;
  const api = (method, route, body) =>
    fetch(`http://127.0.0.1:${port}${route}`, {
      method,
      headers: { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then(async (res) => ({ status: res.status, body: await res.json() }));

  await seedPhonics(api);
  const claim = await api('POST', '/api/rounds/claim', { studentId: 'ada', packId: 'pack-d1' });
  await new Promise((r) => first.server.close(r));

  const second = createServer({ dataFile: file });
  await new Promise((r) => second.server.listen(0, '127.0.0.1', r));
  const restartedPort = second.server.address().port;
  try {
    const round = await fetch(`http://127.0.0.1:${restartedPort}/api/rounds/${claim.body.round.id}`).then((r) => r.json());
    assert.equal(round.worksheetId, 'ws-d1');
    const worksheet = await fetch(`http://127.0.0.1:${restartedPort}/api/worksheets/ws-d1`).then((r) => r.json());
    assert.equal(worksheet.id, 'pack-d1');
    assert.equal(worksheet.kpIds[0], 'kp-a');
  } finally {
    await new Promise((r) => second.server.close(r));
  }
});
