/**
 * 向正在运行的本地服务写入一套固定、可在页面中查看的题目案卷。
 * 默认会先调用 /api/admin/reset 清空当前本地台账。
 *
 * 用法：
 *   npm start
 *   npm run showcase
 */
const BASE = process.env.BASE_URL ?? 'http://127.0.0.1:4173';

async function req(method, route, body) {
  const response = await fetch(`${BASE}${route}`, {
    method,
    headers: { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(data?.message ?? `${response.status} ${route}`);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

const post = (route, body) => req('POST', route, body);
const get = (route) => req('GET', route);

async function dispatchAccept(testerId, taskId, deviceId) {
  const invitation = await post('/api/invitations/dispatch', { testerId, taskId, deviceId });
  return post(`/api/invitations/${invitation.id}/accept`, {
    testerId,
    acceptTerms: true,
    deviceId,
  });
}

async function click(sessionId, actionId, extra = {}) {
  return post(`/api/sessions/${sessionId}/click`, { actionId, ...extra });
}

async function bluePath(sessionId) {
  await click(sessionId, 'go-library');
  await click(sessionId, 'collect:book-blue');
  await click(sessionId, 'uncollect:book-blue');
}

async function main() {
  await post('/api/admin/reset', {});

  const pkg = await post('/api/packages', {
    id: 'pkg-safe-showcase',
    title: '互不泄题演示包：蓝册往返 + 红册待借',
    taskIds: ['task-blue-roundtrip', 'task-red-loan-flag'],
  });

  // 1. 全新试用员：三点自助发现，审核并领取。
  const fresh = await dispatchAccept('T-001-fresh', 'task-blue-roundtrip', 'device-A');
  await bluePath(fresh.terminal.sessionId);
  const freshCompletion = await post(`/api/claims/${fresh.claim.claimId}/completions`, {
    completionId: 'cmp-T001-blue',
    deviceId: 'device-A',
  });
  await post(`/api/completions/${freshCompletion.completion.id}/review`, { decision: 'approve', reviewerName: '招募方' });
  await post(`/api/claims/${fresh.claim.claimId}/payout`, { deviceId: 'device-A' });
  // 多设备重传同一完成/领取：页面台账仍只有一笔。
  await post(`/api/claims/${fresh.claim.claimId}/completions`, { completionId: 'cmp-T001-blue', deviceId: 'device-B' });
  await post(`/api/claims/${fresh.claim.claimId}/completions`, { completionId: 'cmp-T001-blue', deviceId: 'device-C' });
  await post(`/api/claims/${fresh.claim.claimId}/payout`, { deviceId: 'device-B' });

  // 2. 同一新人已掌握收藏手势；绿册同手势变体不能再领首次发现奖。
  const green = await dispatchAccept('T-001-fresh', 'task-green-roundtrip-known-variant', 'device-A');
  await click(green.terminal.sessionId, 'go-library');
  await click(green.terminal.sessionId, 'collect:book-green');
  await click(green.terminal.sessionId, 'uncollect:book-green');
  const greenCompletion = await post(`/api/claims/${green.claim.claimId}/completions`, {
    completionId: 'cmp-T001-green-known',
  });
  await post(`/api/completions/${greenCompletion.completion.id}/review`, {
    decision: 'acknowledge-blocked',
    reviewerName: '招募方',
  });

  // 3. 错目标 + 相同外观结束页。
  const wrong = await dispatchAccept('T-002-retarget', 'task-blue-roundtrip', 'device-A');
  await click(wrong.terminal.sessionId, 'go-library');
  await click(wrong.terminal.sessionId, 'collect:book-blue');
  try {
    await click(wrong.terminal.sessionId, 'uncollect:book-blue', { targetId: 'book-green' });
  } catch {}
  await click(wrong.terminal.sessionId, 'shortcut-lookalike-finish');
  const wrongCompletion = await post(`/api/claims/${wrong.claim.claimId}/completions`, {
    completionId: 'cmp-T002-wrong-target',
  });
  await post(`/api/completions/${wrongCompletion.completion.id}/review`, { decision: 'reject', reviewerName: '招募方' });

  // 4. 旧单实际展示排除提示并保存回条。
  const hintedPrior = await dispatchAccept('T-003-hinted', 'task-blue-roundtrip', 'device-prior');
  await post(`/api/claims/${hintedPrior.claim.claimId}/hints/hint-blue-answer/view`, { deviceId: 'device-prior' });
  await bluePath(hintedPrior.terminal.sessionId);
  const priorCompletion = await post(`/api/claims/${hintedPrior.claim.claimId}/completions`, {
    completionId: 'cmp-T003-prior-hint',
  });
  await post(`/api/completions/${priorCompletion.completion.id}/review`, {
    decision: 'acknowledge-blocked',
    reviewerName: '招募方',
  });

  // 5. 同一身份新蓝册单：不临时提示，完整轨迹仍不付发现奖。
  const hintedBlue = await dispatchAccept('T-003-hinted', 'task-blue-roundtrip', 'device-new');
  await bluePath(hintedBlue.terminal.sessionId);
  const hintedBlueCompletion = await post(`/api/claims/${hintedBlue.claim.claimId}/completions`, {
    completionId: 'cmp-T003-new-blue-blocked',
  });
  await post(`/api/completions/${hintedBlueCompletion.completion.id}/review`, {
    decision: 'acknowledge-blocked',
    reviewerName: '招募方',
  });

  // 6. 同一人改做不泄题的红册任务，正常得 2 点。
  const red = await dispatchAccept('T-003-hinted', 'task-red-loan-flag', 'device-red');
  await click(red.terminal.sessionId, 'flag-loan:book-red');
  await click(red.terminal.sessionId, 'unflag-loan:book-red');
  const redCompletion = await post(`/api/claims/${red.claim.claimId}/completions`, {
    completionId: 'cmp-T003-red-payable',
  });
  await post(`/api/completions/${redCompletion.completion.id}/review`, { decision: 'approve', reviewerName: '招募方' });
  await post(`/api/claims/${red.claim.claimId}/payout`, { deviceId: 'device-red' });

  const audit = await get('/api/audit');
  console.log('演示案卷已写入', BASE);
  console.log({ packageId: pkg.id, counts: audit.counts });
  console.log('审核/复查:', `${BASE}/review.html`);
  console.log('T-001 报酬:', `${BASE}/pay.html?tester=T-001-fresh`);
  console.log('T-003 报酬:', `${BASE}/pay.html?tester=T-003-hinted`);
}

main().catch((error) => {
  console.error('演示案卷写入失败：', error.message);
  if (error.data) console.error(JSON.stringify(error.data, null, 2));
  process.exit(1);
});
