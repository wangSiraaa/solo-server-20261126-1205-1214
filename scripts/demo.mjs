import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const serverFile = path.join(__dirname, '..', 'src', 'server.js');

function getFreePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.listen(0, '127.0.0.1', () => {
      const port = probe.address().port;
      probe.close((error) => (error ? reject(error) : resolve(port)));
    });
    probe.on('error', reject);
  });
}

async function waitHealth(base, timeoutMs = 10_000) {
  const started = Date.now();
  let lastError;
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(`${base}/api/health`);
      if (response.ok) return response.json();
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`server did not become healthy: ${lastError?.message ?? 'unknown'}`);
}

function startServer({ port, dataFile }) {
  const child = spawn(process.execPath, [serverFile], {
    cwd: path.join(__dirname, '..'),
    env: { ...process.env, PORT: String(port), HOST: '127.0.0.1', DATA_FILE: dataFile },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.on('data', (chunk) => process.stdout.write(`[server] ${chunk}`));
  child.stderr.on('data', (chunk) => process.stderr.write(`[server-error] ${chunk}`));
  return child;
}

function stopServer(child) {
  if (!child || child.exitCode !== null) return Promise.resolve();
  return new Promise((resolve) => {
    child.once('exit', () => resolve());
    child.kill('SIGTERM');
    setTimeout(() => {
      if (child.exitCode === null) child.kill('SIGKILL');
    }, 2000).unref();
  });
}

async function main() {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'night-school-demo-'));
  const dataFile = path.join(dir, 'demo-state.json');
  const port = await getFreePort();
  const base = `http://127.0.0.1:${port}`;
  let child = startServer({ port, dataFile });

  const request = async (method, url, body, headers = {}) => {
    const response = await fetch(`${base}${url}`, {
      method,
      headers: body === undefined ? headers : { 'content-type': 'application/json', ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      const error = new Error(data?.error?.message || `HTTP ${response.status}`);
      error.data = data;
      error.status = response.status;
      throw error;
    }
    return data;
  };

  const pairAnswers = {
    guestName: '阿夜',
    contact: 'aye@example.test',
    attendanceMode: 'pair',
    generalConsent: true,
    partnerName: '小校',
    partnerContact: 'xiao@example.test',
    pairWorkPlan: '一号机接线、二号机安全复核。',
    pairConsent: true,
  };

  try {
    await waitHealth(base);
    console.log('\n1) 开一班三个台位，仅 S1/S2 可结伴');
    await request('POST', '/api/classes', {
      id: 'demo-class',
      stationCount: 3,
      pairGroups: [['S1', 'S2']],
    });

    console.log('2) 两人同意并完整填写“结伴实作”，同伴用邀请确认');
    const draft = await request('POST', '/api/drafts', {
      id: 'demo-pair',
      classId: 'demo-class',
      answers: pairAnswers,
    });
    const invitation = await request('POST', `/api/drafts/${draft.draft.id}/invitations`, {});
    await request('POST', `/api/invitations/${invitation.invitation.id}/confirm`, {
      token: invitation.invitation.token,
      consent: true,
      companionName: '小校',
    });
    const order = await request('POST', `/api/drafts/demo-pair/submit`, {
      idempotencyKey: 'demo-idem-001',
    });
    console.log('   成功双人单：', order.order.id, order.order.enrollmentNumbers, order.order.seats.map((s) => s.stationId));
    if (JSON.stringify(order.order.seats.map((s) => s.stationId)) !== JSON.stringify(['S1', 'S2'])) {
      throw new Error('双人单应坐在 S1/S2');
    }

    console.log('3) 错回执场景：同伴回执确认另一张草稿，本单不能占台');
    await request('POST', '/api/classes', { id: 'demo-wrong', stationCount: 3, pairGroups: [['S1', 'S2']] });
    const a = await request('POST', '/api/drafts', { id: 'wrong-a', classId: 'demo-wrong', answers: pairAnswers });
    await request('POST', '/api/drafts', { id: 'wrong-b', classId: 'demo-wrong', answers: pairAnswers });
    const wrongInvite = await request('POST', `/api/drafts/${a.draft.id}/invitations`, {});
    const wrongReceipt = await request('POST', `/api/invitations/${wrongInvite.invitation.id}/confirm`, {
      token: wrongInvite.invitation.token,
      draftId: 'wrong-b',
      consent: true,
    });
    await request('PATCH', '/api/drafts/wrong-a', { companionReceiptId: wrongReceipt.receipt.id });
    let blocked = false;
    try {
      await request('POST', '/api/drafts/wrong-a/submit', {});
    } catch (error) {
      blocked = error.data?.error?.code === 'COMPANION_RECEIPT_FOR_OTHER_DRAFT';
    }
    if (!blocked) throw new Error('错误草稿回执不应允许占台');
    const wrongClass = await request('GET', '/api/classes/demo-wrong');
    if (wrongClass.class.availableStationIds.length !== 3) throw new Error('错回执场景不应占台');
    console.log('   已拒绝，三台仍全空');

    console.log('4) 正确确认但 S1 先被散客领走，S3 空也不能凑双');
    await request('POST', '/api/classes', { id: 'demo-block', stationCount: 3, pairGroups: [['S1', 'S2']] });
    const solo = await request('POST', '/api/drafts', {
      id: 'block-solo',
      classId: 'demo-block',
      answers: { guestName: '散客', contact: 'solo@example.test', attendanceMode: 'solo', generalConsent: true },
    });
    await request('POST', `/api/drafts/${solo.draft.id}/submit`, { stationId: 'S1' });
    const pair = await request('POST', '/api/drafts', { id: 'block-pair', classId: 'demo-block', answers: pairAnswers });
    const pairInvite = await request('POST', `/api/drafts/${pair.draft.id}/invitations`, {});
    await request('POST', `/api/invitations/${pairInvite.invitation.id}/confirm`, {
      token: pairInvite.invitation.token,
      consent: true,
    });
    let pairBlocked = false;
    try {
      await request('POST', '/api/drafts/block-pair/submit', {});
    } catch (error) {
      pairBlocked = error.data?.error?.code === 'PAIR_STATIONS_UNAVAILABLE';
    }
    if (!pairBlocked) throw new Error('S1 被占后不应分配双人台');
    const blockedClass = await request('GET', '/api/classes/demo-block');
    console.log('   已拒绝；剩余空台：', blockedClass.class.availableStationIds);

    console.log('5) 管理员发布拆分新表，未交旧草稿继续按 v1 续填');
    const oldDraft = await request('POST', '/api/drafts', {
      id: 'continue-old',
      classId: 'demo-class',
      formVersion: 1,
      answers: { guestName: '上午客人', visitedLab: true },
    });
    const published = await request('POST', '/api/admin/forms/publish-split', {});
    const continued = await request('PATCH', '/api/drafts/continue-old', {
      answers: { contact: 'evening@example.test' },
    });
    if (continued.draft.formVersion !== 1 || continued.draft.effectiveAnswers.visitedLab !== true) {
      throw new Error('旧草稿应固定 v1 且旧题答案仍可解释');
    }
    console.log(`   新表 v${published.form.version}；旧稿仍 v${continued.draft.formVersion}，visitedLab=true 有效`);

    console.log('6) 真实重启 HTTP 服务（同一数据文件）');
    await stopServer(child);
    child = startServer({ port, dataFile });
    await waitHealth(base);
    const repeated = await request('POST', '/api/drafts/demo-pair/submit', {
      idempotencyKey: 'demo-idem-001',
    });
    if (repeated.order.id !== order.order.id || repeated.replayed !== true) {
      throw new Error('重启后重复递交必须返回原单');
    }
    const counter = await request('GET', `/api/counter/orders/${order.order.id}`);
    console.log('   重启后重复递交返回原单：', repeated.order.id);
    console.log('   柜台可见表单版本 v' + counter.counter.formVersion.version,
      '入班号', counter.counter.enrollmentNumbers,
      '就坐', counter.counter.seatedPeople.map((p) => `${p.stationId}:${p.name}`));

    console.log('\n演示通过。手动网络入口：npm start 后打开 http://localhost:3000/');
  } finally {
    await stopServer(child);
    await rm(dir, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error('\n演示失败：', error.message);
  if (error.data) console.error(JSON.stringify(error.data, null, 2));
  process.exitCode = 1;
});
