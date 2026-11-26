'use strict';

const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const PORT = Number(process.env.TEST_PORT || 3210);
const BASE = `http://127.0.0.1:${PORT}`;
const PHONE = '13800001111';
const TMP_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'hotline-acceptance-'));
const DATA_FILE = path.join(TMP_DIR, 'hotline.json');

let child = null;
let revision = 0;
let passed = 0;

function ok(message) {
  passed += 1;
  console.log(`  ✅ ${message}`);
}

async function request(method, route, body) {
  const res = await fetch(`${BASE}${route}`, {
    method,
    headers: { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let json = null;
  const text = await res.text();
  if (text) {
    try {
      json = JSON.parse(text);
    } catch {
      json = { raw: text };
    }
  }
  if (json && Number.isInteger(json.revision)) revision = json.revision;
  return { status: res.status, ok: res.ok, json };
}

async function post(route, body) {
  return request('POST', route, body);
}

async function get(route) {
  return request('GET', route);
}

async function act(action, body = {}, options = {}) {
  const payload = { ...body };
  if (payload.expectedRevision === undefined && options.optimistic !== false) {
    payload.expectedRevision = revision;
  }
  const response = await post(`/console/${action}`, payload);
  if (options.expectFailure) {
    assert.equal(response.ok, false, `${action} 应当失败，但成功了: ${JSON.stringify(response.json)}`);
    if (options.code) {
      assert.equal(
        response.json?.error?.code,
        options.code,
        `${action} 错误码应为 ${options.code}，实际 ${response.json?.error?.code}`,
      );
    }
    return response;
  }
  assert.equal(response.ok, true, `${action} 失败: ${response.status} ${JSON.stringify(response.json)}`);
  assert.equal(
    Number.isInteger(response.json.revision),
    true,
    `${action} 响应缺少 revision`,
  );
  revision = response.json.revision;
  return response.json;
}

function startServer() {
  return new Promise((resolve, reject) => {
    child = spawn(process.execPath, [path.join(ROOT, 'server.js')], {
      cwd: ROOT,
      env: {
        ...process.env,
        PORT: String(PORT),
        HOST: '127.0.0.1',
        HOTLINE_DATA: DATA_FILE,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let logs = '';
    child.stdout.on('data', (chunk) => {
      logs += chunk.toString();
    });
    child.stderr.on('data', (chunk) => {
      logs += chunk.toString();
    });
    child.on('exit', (code) => {
      if (code !== 0 && code !== null) reject(new Error(`server exited ${code}\n${logs}`));
    });

    const started = Date.now();
    const timer = setInterval(async () => {
      try {
        const res = await fetch(`${BASE}/health`);
        if (res.ok) {
          clearInterval(timer);
          resolve(logs);
        }
      } catch {
        if (Date.now() - started > 10000) {
          clearInterval(timer);
          reject(new Error(`server did not start\n${logs}`));
        }
      }
    }, 100);
  });
}

async function stopServer() {
  if (!child) return;
  const current = child;
  await new Promise((resolve) => {
    current.once('exit', resolve);
    current.kill('SIGTERM');
    setTimeout(() => {
      if (!current.killed) current.kill('SIGKILL');
      resolve();
    }, 3000).unref();
  });
  child = null;
}

async function main() {
  console.log(`\n[1/9] 启动服务，数据文件 ${DATA_FILE}`);
  await startServer();
  let health = await get('/health');
  assert.equal(health.json.ok, true);
  ok('健康检查通过');

  console.log('\n[2/9] 校验甲乙资格与乐观锁');
  const agentsRes = await get('/agents');
  const agentA = agentsRes.json.agents.find((a) => a.id === 'a');
  const agentB = agentsRes.json.agents.find((a) => a.id === 'b');
  assert.deepEqual(agentA.skills, ['date']);
  assert.deepEqual(agentB.skills.sort(), ['date', 'pickup']);
  ok('甲可答日期；乙可答日期和取件');

  await act('inbound', { callerNumber: PHONE, agentId: 'a' });
  await act('answer', { callerNumber: PHONE, agentId: 'a' });
  await act('ask', { callerNumber: PHONE, itemCode: 'date' });
  const dateAnswer = await act('speak', {
    callerNumber: PHONE,
    agentId: 'a',
    itemCode: 'date',
    text: '社区图书馆下周一上午九点开放。',
  });
  assert.equal(dateAnswer.formalAnswer, true);
  assert.equal(dateAnswer.item.status, 'answered');
  ok('甲当来电人面答复日期，日期项结清');

  await act('ask', { callerNumber: PHONE, itemCode: 'pickup' });

  const staleRevision = revision;
  const [hold1, hold2] = await Promise.all([
    post('/console/hold', { callerNumber: PHONE, expectedRevision: staleRevision }),
    post('/console/hold', { callerNumber: PHONE, expectedRevision: staleRevision }),
  ]);
  const statuses = [hold1, hold2];
  assert.equal(statuses.filter((r) => r.ok).length, 1, '相同旧画面的两个 hold 只能一个成功');
  assert.equal(
    statuses.filter((r) => r.json?.error?.code === 'stale_screen').length,
    1,
    '另一个必须收到 stale_screen',
  );
  const board = await get('/board');
  revision = board.json.board.revision;
  ok('两位坐席相同旧画面并发操作时，后提交者收到版本冲突');

  console.log('\n[3/9] 忙线/通话失败时仍由甲跟进');
  let presence = await request('POST', '/agents/b/presence', {
    presence: 'busy',
    expectedRevision: revision,
  });
  assert.equal(presence.ok, true);
  revision = presence.json.revision;
  const busyConsult = await act(
    'consult',
    { callerNumber: PHONE, fromAgentId: 'a', targetAgentId: 'b' },
    { expectFailure: true, code: 'target_agent_busy' },
  );
  assert.equal(busyConsult.json.error.details.consult.state, 'failed');
  ok('乙忙线时咨询支路记为 failed/busy，责任仍在甲');

  const retrieved = await act('retrieve', { callerNumber: PHONE, agentId: 'a' });
  assert.equal(retrieved.audio.callerHears.a, true);
  ok('甲把等待中的来电人取回，原接办人继续跟进');

  presence = await request('POST', '/agents/b/presence', {
    presence: 'available',
    expectedRevision: revision,
  });
  assert.equal(presence.ok, true);
  revision = presence.json.revision;

  console.log('\n[4/9] 私聊咨询：乙的取件答复不能结算');
  await act('hold', { callerNumber: PHONE, agentId: 'a' });
  const consult = await act('consult', {
    callerNumber: PHONE,
    fromAgentId: 'a',
    targetAgentId: 'b',
  });
  const consultId = consult.consult.id;
  const answeredConsult = await act('answer-consult', {
    callerNumber: PHONE,
    consultCallId: consultId,
    agentId: 'b',
  });
  assert.equal(answeredConsult.audio.callerHears?.a, undefined);
  assert.equal(answeredConsult.audio.callerHears?.b, undefined);
  assert.equal(answeredConsult.audio.holdMusic, true);
  ok('甲、乙在私聊支路；来电人只听等待音乐，听不到私聊');

  const privatePickup = await act('speak', {
    callerNumber: PHONE,
    agentId: 'b',
    itemCode: 'pickup',
    text: '请凭取件码到北门服务台领取。',
  });
  assert.equal(privatePickup.formalAnswer, false);
  assert.equal(privatePickup.utterance.audibleToCaller, false);
  assert.deepEqual(privatePickup.utterance.audienceAgentIds, ['a']);
  assert.ok(privatePickup.blockers.includes('utterance_not_audible_to_caller'));
  const callerDetail0 = await get(`/caller/${PHONE}`);
  assert.equal(callerDetail0.json.activeCase.items.pickup.status, 'open');
  ok('乙对着只连甲的咨询支路念取件方式，取件项仍开着');

  const closePrivate = await act(
    'close-item',
    {
      callerNumber: PHONE,
      itemCode: 'pickup',
      utteranceId: privatePickup.utterance.id,
      agentId: 'b',
    },
    { expectFailure: true, code: 'answer_cannot_close_item' },
  );
  assert.ok(closePrivate.json.error.details.blockers.includes('utterance_not_audible_to_caller'));
  ok('不能拿私聊原话补办“已答复来电人”');

  await act('assign', { callerNumber: PHONE, fromAgentId: 'a', toAgentId: 'b' });
  let detail = (await get(`/caller/${PHONE}`)).json;
  assert.equal(detail.activeCase.ticketHolderAgentId, 'b');
  assert.equal(detail.activeCase.returnableAgentId, 'a');
  ok('工单可先转给乙准备，但可回访责任人仍为甲，直到交接条件对齐');

  console.log('\n[5/9] 陪同桥接、资格撤销与正式办结边界');
  const bridged = await act('bridge', { callerNumber: PHONE, fromAgentId: 'a', toAgentId: 'b' });
  assert.equal(bridged.audio.callerHears.a, true);
  assert.equal(bridged.audio.callerHears.b, true);
  ok('桥接后来电人实际与甲乙在同一音频桥');

  let revoke = await request('DELETE', '/agents/b/skills/pickup', {
    expectedRevision: revision,
  });
  assert.equal(revoke.ok, true);
  assert.equal(revoke.json.audioPathChanged, false);
  revision = revoke.json.revision;
  const audioAfterRevoke = await get(`/calls/${detail.activeCase.primaryCallId}/audio`);
  assert.equal(audioAfterRevoke.json.audio.callerHears.b, true);
  ok('只撤掉乙的取件班次资格：通话/音频通路仍畅通');

  const badHandoff = await act(
    'complete-handoff',
    { callerNumber: PHONE, fromAgentId: 'a', toAgentId: 'b' },
    { expectFailure: true, code: 'handover_not_ready' },
  );
  assert.ok(badHandoff.json.error.details.missingQualifications.includes('pickup'));
  detail = (await get(`/caller/${PHONE}`)).json;
  assert.equal(detail.activeCase.returnableAgentId, 'a');
  assert.equal(detail.audio.callerHears.b, true);
  ok('资格不匹配时交班不能完成，责任仍在甲，通路不变');

  let grant = await request('PUT', '/agents/b/skills/pickup', {
    expectedRevision: revision,
  });
  assert.equal(grant.ok, true);
  revision = grant.json.revision;
  const handoff = await act('complete-handoff', {
    callerNumber: PHONE,
    fromAgentId: 'a',
    toAgentId: 'b',
  });
  assert.equal(handoff.case.returnableAgentId, 'b');
  assert.equal(handoff.handover.status, 'completed');
  ok('乙当前具备未答事项资格且与来电人真实连通，责任交接完成');

  console.log('\n[6/9] 重发挂机通知落到已换班会话');
  detail = (await get(`/caller/${PHONE}`)).json;
  const successfulConsultCall = detail.calls.find(
    (c) => c.kind === 'consult' && ['active', 'ringing'].includes(c.state),
  );
  assert.ok(successfulConsultCall, '应找到成功的咨询支路');
  const aAudioLeg = successfulConsultCall.legs.find((l) => l.participantId === 'a');
  const eventPayload = {
    eventId: 'evt-provider-a-hangup-001',
    type: 'leg.disconnected',
    providerLegId: aAudioLeg.providerLegId,
    occurredAt: new Date().toISOString(),
  };
  const firstEvent = await post('/telephony/events', eventPayload);
  assert.equal(firstEvent.status, 201);
  assert.equal(firstEvent.json.duplicate, false);
  assert.equal(firstEvent.json.currentReturnableAgentId, 'b');
  assert.equal(firstEvent.json.audio.callerHears?.a, undefined);
  assert.equal(firstEvent.json.audio.callerHears.b, true);
  ok('甲的挂机通知只断甲支路；乙—来电人仍可听见，当前接办人为乙');

  const cleanupA = await act('hangup', { callerNumber: PHONE, who: 'a' });
  assert.equal(cleanupA.audio.callerHears.b, true);
  ok('清理甲其余保持支路后，整通来电仍未结束');

  const redelivered = await post('/telephony/events', eventPayload);
  assert.equal(redelivered.status, 200);
  assert.equal(redelivered.json.duplicate, true);
  assert.equal(redelivered.json.currentReturnableAgentId, 'b');
  ok('立即重发同一挂机通知被幂等识别，不改变已经换班的责任关系');

  console.log('\n[7/9] 撤资格后同一句话不能补办；恢复后必须重新当来电人面答复');
  revoke = await request('DELETE', '/agents/b/skills/pickup', {
    expectedRevision: revision,
  });
  assert.equal(revoke.ok, true);
  revision = revoke.json.revision;

  const unqualifiedAudible = await act('speak', {
    callerNumber: PHONE,
    agentId: 'b',
    itemCode: 'pickup',
    text: '请凭取件码到北门服务台领取。',
  });
  assert.equal(unqualifiedAudible.utterance.audibleToCaller, true);
  assert.equal(unqualifiedAudible.formalAnswer, false);
  assert.ok(unqualifiedAudible.blockers.includes('agent_not_qualified_at_delivery'));
  detail = (await get(`/caller/${PHONE}`)).json;
  assert.equal(detail.activeCase.items.pickup.status, 'open');
  ok('乙资格已撤但通路仍在；即使来电人听见同一句，取件项也不办结');

  grant = await request('PUT', '/agents/b/skills/pickup', { expectedRevision: revision });
  assert.equal(grant.ok, true);
  revision = grant.json.revision;

  const acceptOldUnqualified = await act(
    'close-item',
    {
      callerNumber: PHONE,
      itemCode: 'pickup',
      utteranceId: unqualifiedAudible.utterance.id,
      agentId: 'b',
    },
    { expectFailure: true, code: 'answer_cannot_close_item' },
  );
  assert.ok(
    acceptOldUnqualified.json.error.details.blockers.includes('agent_not_qualified_at_delivery'),
  );
  ok('恢复资格后，资格撤销期间说出的旧话仍不能追溯办结');

  const repeatedPickup = await act('speak', {
    callerNumber: PHONE,
    agentId: 'b',
    itemCode: 'pickup',
    text: '请凭取件码到北门服务台，出示取件码后领取。',
  });
  assert.equal(repeatedPickup.formalAnswer, true);
  assert.equal(repeatedPickup.item.status, 'answered');
  assert.ok(repeatedPickup.item.audioProof.deliveredAudible);
  ok('乙在资格有效且来电人实际在场时重新答复，取件项才结清');

  const callerHangup = await act('hangup', { callerNumber: PHONE, who: 'caller' });
  assert.equal(callerHangup.settlement.rootEnded, true);
  assert.equal(callerHangup.case.status, 'resolved');
  assert.equal(callerHangup.case.items.date.status, 'answered');
  assert.equal(callerHangup.case.items.pickup.status, 'answered');
  ok('来电人挂机，两项均有实际送达证据，案件结案');

  console.log('\n[7b] 支路隔离、甲无取件资格与“未答有人接”');
  const PHONE_A_PICKUP = '13700003333';
  await act('inbound', { callerNumber: PHONE_A_PICKUP, agentId: 'a' });
  await act('answer', { callerNumber: PHONE_A_PICKUP, agentId: 'a' });
  await act('ask', { callerNumber: PHONE_A_PICKUP, itemCode: 'pickup' });
  const aPickup = await act('speak', {
    callerNumber: PHONE_A_PICKUP,
    agentId: 'a',
    itemCode: 'pickup',
    text: '取件好像在北门。',
  });
  assert.equal(aPickup.formalAnswer, false);
  assert.equal(aPickup.utterance.qualification.reason, 'missing_skill');
  ok('甲当面说取件也因缺少取件资格而不能办结');

  await act('hold', { callerNumber: PHONE_A_PICKUP, agentId: 'a' });
  const sideConsult = await act('consult', {
    callerNumber: PHONE_A_PICKUP,
    fromAgentId: 'a',
    targetAgentId: 'b',
  });
  await act('answer-consult', {
    callerNumber: PHONE_A_PICKUP,
    consultCallId: sideConsult.consult.id,
    agentId: 'b',
  });
  const endConsultOnly = await act('hangup', {
    callerNumber: PHONE_A_PICKUP,
    callId: sideConsult.consult.id,
  });
  assert.ok(endConsultOnly.hangup.affectedCallIds.includes(sideConsult.consult.id));
  const sideDetailDuring = (await get(`/caller/${PHONE_A_PICKUP}`)).json;
  const sideConsultCall = sideDetailDuring.calls.find((c) => c.id === sideConsult.consult.id);
  assert.equal(sideConsultCall.state, 'ended');
  assert.notEqual(sideDetailDuring.activeCase.primaryCallState || sideDetailDuring.calls.find((c) => c.kind === 'inbound').state, 'ended');
  const retrievedAfterConsultEnd = await act('retrieve', {
    callerNumber: PHONE_A_PICKUP,
    agentId: 'a',
  });
  assert.equal(retrievedAfterConsultEnd.audio.callerHears.a, true);
  ok('只结束咨询支路后，来电主支路仍在，甲可取回来电人');

  await act('hangup', { callerNumber: PHONE_A_PICKUP, who: 'caller' });
  const pendingDetail = (await get(`/caller/${PHONE_A_PICKUP}`)).json;
  assert.equal(pendingDetail.cases[0].status, 'pending_followup');
  assert.deepEqual(pendingDetail.responsibility.openItemCodes, ['pickup']);
  assert.equal(pendingDetail.responsibility.ownerQualifiedForOpenItems, false);
  assert.ok(pendingDetail.responsibility.eligibleAgentIds.includes('b'));
  assert.equal(pendingDetail.responsibility.fallbackQueue, 'community-hotline-desk');
  ok('未答取件不结案；工作台保留责任人、合格接棒人乙和值班兜底台');

  const PHONE_B_DATE = '13700004444';
  await act('inbound', { callerNumber: PHONE_B_DATE, agentId: 'b' });
  await act('answer', { callerNumber: PHONE_B_DATE, agentId: 'b' });
  await act('ask', { callerNumber: PHONE_B_DATE, itemCode: 'date' });
  const bDate = await act('speak', {
    callerNumber: PHONE_B_DATE,
    agentId: 'b',
    itemCode: 'date',
    text: '开放日期是下周一。',
  });
  assert.equal(bDate.formalAnswer, true);
  await act('hangup', { callerNumber: PHONE_B_DATE, who: 'caller' });
  ok('乙同样具备日期答复资格并可独立结案');

  console.log('\n[8/9] 重启进程后从来电号恢复完整责任交接并重放通知');
  const beforeRestart = (await get(`/caller/${PHONE}`)).json;
  assert.equal(beforeRestart.handovers.length, 1);
  assert.equal(beforeRestart.handovers[0].status, 'completed');
  assert.ok(beforeRestart.timeline.some((e) => e.type === 'handover.completed'));
  ok('重启前可按来电号查到交接链');

  await stopServer();
  assert.ok(fs.existsSync(DATA_FILE), 'JSON 持久化文件应已写入');
  await startServer();
  health = await get('/health');
  assert.equal(health.json.ok, true);
  ok('服务已用同一数据文件重启');

  const afterRestart = (await get(`/caller/${PHONE}`)).json;
  assert.equal(afterRestart.callerNumber, PHONE);
  assert.equal(afterRestart.activeCaseId, null, '已结案件不再是 active case');
  assert.equal(afterRestart.cases[0].returnableAgentId, 'b');
  assert.equal(afterRestart.cases[0].ticketHolderAgentId, 'b');
  assert.equal(afterRestart.cases[0].items.date.answeredByAgentId, 'a');
  assert.equal(afterRestart.cases[0].items.pickup.answeredByAgentId, 'b');
  assert.ok(afterRestart.handovers[0].phases.some((p) => p.phase === 'private_consult_connected'));
  assert.ok(afterRestart.handovers[0].phases.some((p) => p.phase === 'ticket_assigned_privately'));
  assert.ok(afterRestart.handovers[0].phases.some((p) => p.phase === 'caller_bridged'));
  assert.ok(afterRestart.handovers[0].phases.some((p) => p.phase === 'responsibility_transferred'));
  assert.ok(afterRestart.handovers[0].phases.some((p) => p.phase === 'original_agent_departed'));
  ok('重启后从来电号查回：私聊、工单转移、桥接、责任转移、甲离场全链');

  const replayedAfterRestart = await post('/telephony/events', eventPayload);
  assert.equal(replayedAfterRestart.status, 200);
  assert.equal(replayedAfterRestart.json.duplicate, true);
  assert.equal(replayedAfterRestart.json.currentReturnableAgentId, 'b');
  ok('重启后重发同一挂机通知仍由持久化幂等表拦截，责任人为乙');

  console.log('\n[9/9] CLI 冒烟');
  const { execFileSync } = require('node:child_process');
  const cliOut = execFileSync(process.execPath, [path.join(ROOT, 'cli.js'), 'board', '--base', BASE], {
    encoding: 'utf8',
    env: process.env,
  });
  assert.match(cliOut, /community-hotline|revision|agents/);
  ok('CLI 可从另一终端调用 /board');

  await stopServer();
  console.log(`\n🎉 验收通过：${passed} 个断言场景全部成功。`);
  console.log(`持久化目录保留在: ${TMP_DIR}`);
}

main().catch(async (err) => {
  console.error('\n❌ 验收失败:', err);
  try {
    await stopServer();
  } catch {}
  process.exit(1);
});
