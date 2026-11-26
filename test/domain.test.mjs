import test from 'node:test';
import assert from 'node:assert/strict';

import {
  PROTOTYPES,
  assessEligibility,
  availableActions,
  builtInTasks,
  evaluateCompletion,
  planPackage,
  transition,
} from '../lib/domain.mjs';

const blueTask = builtInTasks().find((task) => task.id === 'task-blue-roundtrip');
const prototype = PROTOTYPES[blueTask.prototypeId];

function runActions(actionInputs) {
  let state = structuredClone(blueTask.initialState);
  let screenId = blueTask.initialScreenId;
  const events = [];
  for (const input of actionInputs) {
    const result = transition(prototype, state, screenId, input);
    state = result.state;
    screenId = result.screenId;
    events.push({ ...result.event, seq: events.length + 1 });
  }
  return { state, screenId, events };
}

function verdictFor(run, eligibility = { independent: true, excluded: false }) {
  const claim = {
    id: 'claim-test',
    testerId: 'tester-test',
    prototypeId: prototype.id,
    prototypeVersion: prototype.version,
    agreementSnapshot: { unitPrice: 3, currency: 'points' },
  };
  const session = {
    id: 'session-test',
    claimId: claim.id,
    testerId: claim.testerId,
    state: run.state,
    screenId: run.screenId,
    events: run.events,
    rejectedEvents: [],
    provenance: 'self-service',
  };
  return evaluateCompletion({
    task: blueTask,
    prototype,
    claim,
    session,
    eligibility,
  });
}

test('蓝册收藏并取消收藏的有序真实轨迹可付 3 点', () => {
  const run = runActions([
    { actionId: 'go-library' },
    { actionId: 'collect:book-blue' },
    { actionId: 'uncollect:book-blue' },
  ]);
  assert.deepEqual(run.state.favorites, []);
  assert.equal(run.state.flags.blueRoundtrip, true);

  const verdict = verdictFor(run);
  assert.equal(verdict.behaviorPassed, true);
  assert.equal(verdict.discoveryPayable, true);
  assert.equal(verdict.payableAmount, 3);
});

test('第二个动作改绑绿册时，即使到相同外观结束页也不通过', () => {
  assert.throws(
    () =>
      runActions([
        { actionId: 'go-library' },
        { actionId: 'collect:book-blue' },
        { actionId: 'uncollect:book-blue', targetId: 'book-green' },
      ]),
    (error) => error.code === 'TARGET_BINDING_MISMATCH',
  );

  // 即便客户端另外走到外观相同的 lookalike 结束页，缺少蓝册取消事件仍失败。
  const run = runActions([
    { actionId: 'go-library' },
    { actionId: 'collect:book-blue' },
    { actionId: 'shortcut-lookalike-finish' },
  ]);
  assert.equal(run.screenId, 'finish-lookalike');
  const verdict = verdictFor(run);
  assert.equal(verdict.behaviorPassed, false);
  assert.equal(verdict.discoveryPayable, false);
  assert.ok(verdict.checks.some((check) => check.id === 'exact-ordered-verbs-and-targets' && !check.ok));
});

test('同名“确定”在不同画面有不同动作身份', () => {
  const noticeActions = availableActions(prototype, 'notice', { favorites: [], loanFlags: [], flags: {} });
  const finishActions = availableActions(prototype, 'finish', { favorites: [], loanFlags: [], flags: {} });
  assert.equal(noticeActions[0].label, '确定');
  assert.equal(finishActions[0].label, '确定');
  assert.notEqual(noticeActions[0].actionId, finishActions[0].actionId);
});

test('按试用员身份读回的旧提示曝光会排除发现奖', () => {
  const eligibility = assessEligibility(blueTask, {
    testerId: 'T-1',
    exposures: [
      {
        id: 'exp-1',
        testerId: 'T-1',
        hintId: 'hint-blue-answer',
        sourceReceiptNo: 'RC-OLD',
        reveals: ['binding:collect-blue', 'binding:uncollect-blue'],
        shownAt: '2026-11-26T00:00:00.000Z',
      },
    ],
    knownKeys: [],
  }, '2026-11-27T00:00:00.000Z');

  assert.equal(eligibility.independent, false);
  assert.equal(eligibility.reason, 'prior_exclusion_hint_actually_shown');
  assert.deepEqual(eligibility.sourceReceiptNos, ['RC-OLD']);

  const run = runActions([
    { actionId: 'go-library' },
    { actionId: 'collect:book-blue' },
    { actionId: 'uncollect:book-blue' },
  ]);
  const verdict = verdictFor(run, eligibility);
  assert.equal(verdict.behaviorPassed, true);
  assert.equal(verdict.discoveryPayable, false);
  assert.equal(verdict.payableAmount, 0);
});

test('发包策略识别共享手势，且互不泄题任务可同包', () => {
  const tasks = builtInTasks();
  const safe = planPackage(tasks, ['task-blue-roundtrip', 'task-red-loan-flag']);
  assert.equal(safe.safeForOneUnmoderatedPackage, true);

  const conflict = planPackage(tasks, ['task-blue-roundtrip', 'task-green-roundtrip-known-variant']);
  assert.equal(conflict.safeForOneUnmoderatedPackage, false);
  assert.ok(conflict.conflicts[0].sharedDiscoveryKeys.includes('gesture:favorite-toggle'));
});
