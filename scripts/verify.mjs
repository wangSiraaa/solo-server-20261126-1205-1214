import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const PORT = Number(process.env.VERIFY_PORT ?? 4319);
const BASE = `http://127.0.0.1:${PORT}`;
const DATA_FILE = path.join(os.tmpdir(), `trial-verify-${Date.now()}-${Math.random().toString(16).slice(2)}.json`);

let child;

function log(step, message) {
  console.log(`${step} ${message}`);
}

async function startServer() {
  child = spawn(process.execPath, [path.join(ROOT, 'server.mjs')], {
    cwd: ROOT,
    env: { ...process.env, PORT: String(PORT), HOST: '127.0.0.1', DATA_FILE },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.on('data', (chunk) => process.stdout.write(`[server] ${chunk}`));
  child.stderr.on('data', (chunk) => process.stderr.write(`[server-error] ${chunk}`));

  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${BASE}/api/health`);
      if (response.ok) return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error('server did not start');
}

async function stopServer() {
  if (!child) return;
  child.kill('SIGTERM');
  await new Promise((resolve) => {
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      resolve();
    }, 2500);
    child.once('exit', () => {
      clearTimeout(timer);
      resolve();
    });
  });
  child = null;
}

async function request(method, route, body, headers = {}) {
  const response = await fetch(`${BASE}${route}`, {
    method,
    headers: { 'content-type': 'application/json', ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  return { status: response.status, data, headers: response.headers };
}
const get = (route) => request('GET', route);
const post = (route, body, headers) => request('POST', route, body, headers);

async function dispatchAndAccept(testerId, taskId, deviceId = 'dev-main') {
  const dispatched = await post('/api/invitations/dispatch', { testerId, taskId, deviceId });
  assert.equal(dispatched.status, 201, dispatched.data?.message);
  const accepted = await post(`/api/invitations/${dispatched.data.id}/accept`, {
    testerId,
    acceptTerms: true,
    deviceId,
  });
  assert.equal(accepted.status, 201, accepted.data?.message);
  return accepted.data;
}

async function click(sessionId, actionId, extra = {}) {
  return post(`/api/sessions/${sessionId}/click`, { actionId, ...extra });
}

async function blueHappyPath(sessionId) {
  let result = await click(sessionId, 'go-library', { deviceId: 'dev-main' });
  assert.equal(result.status, 200, result.data?.message);
  result = await click(sessionId, 'collect:book-blue', { deviceId: 'dev-main' });
  assert.equal(result.status, 200, result.data?.message);
  result = await click(sessionId, 'uncollect:book-blue', { deviceId: 'dev-main' });
  assert.equal(result.status, 200, result.data?.message);
  return result.data.terminal;
}

async function approve(completionId) {
  const review = await post(`/api/completions/${completionId}/review`, {
    decision: 'approve',
    reviewerName: '自动审核',
  });
  assert.equal(review.status, 200, review.data?.message);
  return review.data;
}

async function main() {
  await rm(DATA_FILE, { force: true });
  log('▶', `使用临时数据文件 ${DATA_FILE}`);
  await startServer();

  try {
    const suffix = Math.random().toString(36).slice(2, 8);

    log('1/7', '发包策略：蓝册+红册安全，蓝册+绿册共享手势冲突');
    const safePlan = await post('/api/packages/plan', {
      taskIds: ['task-blue-roundtrip', 'task-red-loan-flag'],
    });
    assert.equal(safePlan.data.safeForOneUnmoderatedPackage, true);
    const conflictPlan = await post('/api/packages/plan', {
      taskIds: ['task-blue-roundtrip', 'task-green-roundtrip-known-variant'],
    });
    assert.equal(conflictPlan.data.safeForOneUnmoderatedPackage, false);
    assert.match(conflictPlan.data.conflicts[0].sharedDiscoveryKeys.join(','), /gesture:favorite-toggle/);

    log('2/7', '新人独立完成蓝册两步，审核三点并多设备并发重传');
    const independentTester = `T-independent-${suffix}`;
    const independent = await dispatchAndAccept(independentTester, 'task-blue-roundtrip', 'dev-A');
    const independentClaimId = independent.claim.claimId;
    const independentSessionId = independent.terminal.sessionId;
    await blueHappyPath(independentSessionId);

    const completionId = `cmp-blue-independent-${suffix}`;
    const firstCompletion = await post(`/api/claims/${independentClaimId}/completions`, {
      completionId,
      deviceId: 'dev-A',
    });
    assert.equal(firstCompletion.status, 201);
    assert.equal(firstCompletion.data.verdict.behaviorPassed, true);
    assert.equal(firstCompletion.data.verdict.discoveryPayable, true);
    assert.equal(firstCompletion.data.verdict.payableAmount, 3);

    await approve(completionId);

    const retransmissions = await Promise.all(
      ['dev-B', 'dev-C', 'dev-D'].map((deviceId) =>
        post(`/api/claims/${independentClaimId}/completions`, { completionId, deviceId }),
      ),
    );
    for (const response of retransmissions) {
      assert.equal(response.status, 200);
      assert.equal(response.data.deduplicated, true);
      assert.equal(response.data.settlement.amount, 3);
    }

    const duplicatePayouts = await Promise.all(
      ['dev-A', 'dev-B', 'dev-C'].map((deviceId) =>
        post(`/api/claims/${independentClaimId}/payout`, { deviceId }),
      ),
    );
    for (const response of duplicatePayouts) {
      assert.ok([200, 201].includes(response.status), response.data?.message);
      assert.ok(response.data.payout.id);
      assert.equal(response.data.payout.amount, 3);
      if (response.status === 200) assert.equal(response.data.deduplicated, true);
    }
    const independentPayments = await get(`/api/payments?testerId=${independentTester}`);
    assert.equal(independentPayments.data.totalPaid, 3);
    assert.equal(independentPayments.data.payouts.length, 1);

    const knownVariant = await dispatchAndAccept(
      independentTester,
      'task-green-roundtrip-known-variant',
      'dev-green-variant',
    );
    assert.equal(knownVariant.claim.eligibilityAtAcceptance.excluded, true);
    assert.equal(
      knownVariant.claim.eligibilityAtAcceptance.reason,
      'discovery_already_known_from_actual_prior_work',
    );
    assert.ok(knownVariant.claim.eligibilityAtAcceptance.knownMatches.includes('gesture:favorite-toggle'));
    await click(knownVariant.terminal.sessionId, 'go-library');
    await click(knownVariant.terminal.sessionId, 'collect:book-green');
    await click(knownVariant.terminal.sessionId, 'uncollect:book-green');
    const knownVariantCompletion = await post(`/api/claims/${knownVariant.claim.claimId}/completions`, {
      completionId: `cmp-green-known-${suffix}`,
      deviceId: 'dev-green-variant',
    });
    assert.equal(knownVariantCompletion.data.verdict.behaviorPassed, true);
    assert.equal(knownVariantCompletion.data.verdict.discoveryPayable, false);
    const knownVariantApprove = await post(
      `/api/completions/${knownVariantCompletion.data.completion.id}/review`,
      { decision: 'approve' },
    );
    assert.equal(knownVariantApprove.status, 409);

    log('3/7', '另开邀约：第二个动作改绑绿册，相同外观结束页也不通过');
    const wrongTester = `T-wrong-target-${suffix}`;
    const wrong = await dispatchAndAccept(wrongTester, 'task-blue-roundtrip', 'dev-A');
    await click(wrong.terminal.sessionId, 'go-library');
    await click(wrong.terminal.sessionId, 'collect:book-blue');
    const retarget = await click(wrong.terminal.sessionId, 'uncollect:book-blue', {
      targetId: 'book-green',
    });
    assert.equal(retarget.status, 409);
    assert.equal(retarget.data.error, 'TARGET_BINDING_MISMATCH');

    const shortcut = await click(wrong.terminal.sessionId, 'shortcut-lookalike-finish');
    assert.equal(shortcut.status, 200);
    assert.equal(shortcut.data.terminal.currentScreenId, 'finish-lookalike');
    const config = await get('/api/config');
    assert.equal(config.data.prototype.screens.finish.appearanceKey, 'plain-finish');
    assert.equal(config.data.prototype.screens['finish-lookalike'].appearanceKey, 'plain-finish');

    const wrongCompletion = await post(`/api/claims/${wrong.claim.claimId}/completions`, {
      completionId: `cmp-wrong-${suffix}`,
      deviceId: 'dev-A',
    });
    assert.equal(wrongCompletion.data.verdict.behaviorPassed, false);
    assert.equal(wrongCompletion.data.verdict.discoveryPayable, false);
    assert.equal(wrongCompletion.data.verdict.payableAmount, 0);
    const targetCheck = wrongCompletion.data.verdict.checks.find((c) => c.id === 'exact-ordered-verbs-and-targets');
    assert.equal(targetCheck.ok, false);

    const forbiddenApprove = await post(`/api/completions/${wrongCompletion.data.completion.id}/review`, {
      decision: 'approve',
    });
    assert.equal(forbiddenApprove.status, 409);
    assert.equal(forbiddenApprove.data.error, 'NOT_SETTLABLE_UNDER_AGREED_RATE');
    await post(`/api/completions/${wrongCompletion.data.completion.id}/review`, { decision: 'reject' });
    const wrongPayments = await get(`/api/payments?testerId=${wrongTester}`);
    assert.equal(wrongPayments.data.totalPaid, 0);

    const hostDemo = await post('/api/demonstrations', {
      taskId: 'task-blue-roundtrip',
      directlyTaught: false,
      actions: [
        { actionId: 'go-library' },
        { actionId: 'collect:book-blue' },
        { actionId: 'uncollect:book-blue' },
      ],
    });
    assert.equal(hostDemo.status, 201);
    const replayTester = `T-host-replay-${suffix}`;
    const replayClaim = await dispatchAndAccept(replayTester, 'task-blue-roundtrip', 'dev-replay');
    const replayedSession = await post(
      `/api/claims/${replayClaim.claim.claimId}/replay-demonstration`,
      { demonstrationId: hostDemo.data.id },
    );
    assert.equal(replayedSession.status, 201, replayedSession.data?.message);
    const replayCompletion = await post(`/api/claims/${replayClaim.claim.claimId}/completions`, {
      completionId: `cmp-host-replay-${suffix}`,
      sessionId: replayedSession.data.sessionId,
      deviceId: 'dev-replay',
    });
    assert.equal(replayCompletion.data.verdict.behaviorPassed, false);
    assert.equal(replayCompletion.data.verdict.discoveryPayable, false);
    const provenanceCheck = replayCompletion.data.verdict.checks.find((c) => c.id === 'no-host-proxy-events');
    assert.equal(provenanceCheck.ok, false);

    log('4/7', '先造一单实际展示过蓝册排除提示并完成/关闭，保存领取回条');
    const hintedTester = `T-hinted-${suffix}`;
    const prior = await dispatchAndAccept(hintedTester, 'task-blue-roundtrip', 'dev-prior');
    const hintView = await post(
      `/api/claims/${prior.claim.claimId}/hints/hint-blue-answer/view`,
      { deviceId: 'dev-prior' },
    );
    assert.equal(hintView.status, 200);
    assert.equal(hintView.data.exposure.actuallyShown, true);
    const receiptsBefore = await get(`/api/testers/${hintedTester}/receipts`);
    assert.equal(receiptsBefore.data.receipts.length, 1);
    const priorReceiptNo = receiptsBefore.data.receipts[0].receiptNo;

    await blueHappyPath(prior.terminal.sessionId);
    const priorCompletion = await post(`/api/claims/${prior.claim.claimId}/completions`, {
      completionId: `cmp-prior-hinted-${suffix}`,
    });
    assert.equal(priorCompletion.data.verdict.behaviorPassed, true);
    assert.equal(priorCompletion.data.verdict.discoveryPayable, false);
    await post(`/api/completions/${priorCompletion.data.completion.id}/review`, {
      decision: 'acknowledge-blocked',
    });

    log('5/7', '同一身份新蓝册单：读回旧回条，本次不提示，完整轨迹仍不付发现奖');
    const newBlue = await dispatchAndAccept(hintedTester, 'task-blue-roundtrip', 'dev-new');
    assert.equal(newBlue.claim.eligibilityAtAcceptance.excluded, true);
    assert.equal(newBlue.claim.eligibilityAtAcceptance.reason, 'prior_exclusion_hint_actually_shown');
    assert.ok(newBlue.claim.eligibilityAtAcceptance.sourceReceiptNos.includes(priorReceiptNo));

    const newReceipt = await get(`/api/claims/${newBlue.claim.claimId}`);
    assert.equal(newReceipt.data.hintExposures.length, 0, '本次无主持会话不应临时展示提示');

    await blueHappyPath(newBlue.terminal.sessionId);
    const hintedValidCompletion = await post(`/api/claims/${newBlue.claim.claimId}/completions`, {
      completionId: `cmp-restored-blue-${suffix}`,
      deviceId: 'dev-new',
    });
    assert.equal(hintedValidCompletion.data.verdict.behaviorPassed, true);
    assert.equal(hintedValidCompletion.data.verdict.discoveryPayable, false);
    assert.equal(
      hintedValidCompletion.data.verdict.eligibility.reason,
      'prior_exclusion_hint_actually_shown',
    );
    assert.ok(
      hintedValidCompletion.data.verdict.eligibility.sourceReceiptNos.includes(priorReceiptNo),
    );
    const blockedApprove = await post(
      `/api/completions/${hintedValidCompletion.data.completion.id}/review`,
      { decision: 'approve' },
    );
    assert.equal(blockedApprove.status, 409);
    const blockedPayout = await post(`/api/claims/${newBlue.claim.claimId}/payout`, {});
    assert.equal(blockedPayout.status, 409);
    assert.equal(blockedPayout.data.error, 'SETTLEMENT_NOT_APPROVED');

    log('6/7', '同一人改做不被蓝册提示泄题的红册待借任务，仍可正常得 2 点');
    const red = await dispatchAndAccept(hintedTester, 'task-red-loan-flag', 'dev-red');
    let redClick = await click(red.terminal.sessionId, 'flag-loan:book-red');
    assert.equal(redClick.status, 200, redClick.data?.message);
    redClick = await click(red.terminal.sessionId, 'unflag-loan:book-red');
    assert.equal(redClick.status, 200, redClick.data?.message);
    const redCompletion = await post(`/api/claims/${red.claim.claimId}/completions`, {
      completionId: `cmp-red-${suffix}`,
      deviceId: 'dev-red',
    });
    assert.equal(redCompletion.data.verdict.behaviorPassed, true);
    assert.equal(redCompletion.data.verdict.discoveryPayable, true);
    assert.equal(redCompletion.data.verdict.payableAmount, 2);
    await approve(redCompletion.data.completion.id);
    const redPayout = await post(`/api/claims/${red.claim.claimId}/payout`, { deviceId: 'dev-red' });
    assert.equal(redPayout.status, 201);
    assert.equal(redPayout.data.payout.amount, 2);

    const hintedPayments = await get(`/api/payments?testerId=${hintedTester}`);
    assert.equal(hintedPayments.data.totalPaid, 2, '被提示泄题的蓝册单为 0，只有红册任务支付 2 点');
    assert.equal(hintedPayments.data.payouts.length, 1);

    log('7/7', '停服并重启：从 JSON 复查，旧完成重传仍不多发工资');
    await stopServer();
    await startServer();

    const audit = await get('/api/audit');
    assert.ok(audit.data.counts.claims >= 5);
    assert.ok(audit.data.counts.payouts >= 2);
    const reviewQueue = await get('/api/review/queue');
    assert.ok(reviewQueue.data.length >= 5);

    const afterRestartRetransmit = await post(`/api/claims/${independentClaimId}/completions`, {
      completionId,
      deviceId: 'dev-after-restart',
    });
    assert.equal(afterRestartRetransmit.status, 200);
    assert.equal(afterRestartRetransmit.data.deduplicated, true);
    assert.equal(afterRestartRetransmit.data.settlement.status, 'paid');

    const afterRestartPayoutRetry = await post(`/api/claims/${independentClaimId}/payout`, {
      deviceId: 'dev-after-restart-2',
    });
    assert.equal(afterRestartPayoutRetry.status, 200);
    assert.equal(afterRestartPayoutRetry.data.deduplicated, true);

    const independentAfter = await get(`/api/payments?testerId=${independentTester}`);
    assert.equal(independentAfter.data.totalPaid, 3);
    assert.equal(independentAfter.data.payouts.length, 1);

    const receiptsAfterRestart = await get(`/api/testers/${hintedTester}/receipts`);
    const blueReceipts = receiptsAfterRestart.data.receipts.filter((r) => r.taskId === 'task-blue-roundtrip');
    assert.ok(blueReceipts.length >= 2);
    assert.ok(receiptsAfterRestart.data.history.exposures.length >= 1);

    console.log('\n✅ 全部验证通过：独立发现付 3、错目标/假结束页拒绝、旧提示回条排除、不泄题任务付 2、多设备与重启均不重复发薪。');
    console.log(`   临时数据保留在 ${DATA_FILE}，可供检查；正式本地数据默认写入 /workspace/data/local-db.json`);
  } finally {
    await stopServer();
  }
}

main().catch(async (error) => {
  console.error('\n❌ 验证失败：', error);
  await stopServer();
  process.exitCode = 1;
});
