#!/usr/bin/env node
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');
const PORT = process.env.VERIFY_PORT || '4517';
const BASE = process.env.BASE_URL || `http://127.0.0.1:${PORT}`;
const DATA = process.env.VERIFY_DATA || `/tmp/studio-verify-${Date.now()}.json`;
let child = null;

function assert(cond, message, extra) {
  if (!cond) {
    const err = new Error(message);
    if (extra !== undefined) err.extra = extra;
    throw err;
  }
  console.log(`  ✓ ${message}`);
}

async function req(method, p, body) {
  const res = await fetch(`${BASE}${p}`, {
    method,
    headers: { 'content-type': 'application/json', 'x-request-id': `verify-${Math.random().toString(36).slice(2)}` },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  let json = null;
  const text = await res.text();
  try { json = text ? JSON.parse(text) : null; } catch { json = { raw: text }; }
  return { status: res.status, body: json };
}

async function waitUp(timeout = 5000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      const r = await fetch(`${BASE}/api/health`);
      if (r.ok) return;
    } catch { /* retry */ }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`server did not start at ${BASE}`);
}

function startServer(keepData = false) {
  if (process.env.BASE_URL) return null;
  if (!keepData) fs.rmSync(DATA, { force: true });
  const c = spawn(process.execPath, ['server.js'], {
    cwd: root,
    env: { ...process.env, PORT, STUDIO_DATA: DATA },
    stdio: ['ignore', 'pipe', 'pipe']
  });
  c.stdout.on('data', (d) => process.env.VERBOSE && process.stdout.write(`[server] ${d}`));
  c.stderr.on('data', (d) => process.stderr.write(`[server-error] ${d}`));
  return c;
}

function stopServer(c) {
  if (!c) return Promise.resolve();
  return new Promise((resolve) => {
    c.once('exit', () => resolve());
    c.kill('SIGTERM');
    setTimeout(() => { try { c.kill('SIGKILL'); } catch { /* noop */ resolve(); } }, 1500);
  });
}

async function stitchTicket(ticketId, initial) {
  let t = initial?.body?.ticket || initial;
  let guard = 0;
  while (t.nextCell && guard++ < 100) {
    const r = await req('POST', `/api/tickets/${ticketId}/report`, { expectedVersion: t.version });
    if (r.status !== 200) throw new Error(`report failed: ${r.status} ${JSON.stringify(r.body)}`);
    t = r.body.ticket;
  }
  return t;
}

function straightSegment(id) {
  return { id, rows: 2, cols: 2, threads: { red: { id: `${id}-straight`, path: [[0, 0], [0, 1]] } } };
}

async function testThreeSegmentsAndReconnect() {
  console.log('\n1) 三段新样：换入、领取、报工、完整针路、签发版本、重连续针');
  await req('POST', '/api/reset');
  await req('POST', '/api/orders', { id: 'three', rows: 2, left: { red: 0 }, right: { red: 0 }, segments: [straightSegment('old')] });
  const replaced = await req('POST', '/api/orders/three/segments/replace', {
    baseVersion: 1,
    segments: [straightSegment('a'), straightSegment('b'), straightSegment('c')]
  });
  assert(replaced.status === 200, '可以请求换入三段新样', replaced.body);
  assert(replaced.body.completable === true, '三段直走样与左右上端点相容');

  const tickets = [];
  for (let i = 0; i < 3; i++) {
    const r = await req('POST', '/api/orders/three/claim', { segmentIndex: i, workerId: `hoop-${i}` });
    assert(r.status === 200, `第 ${i + 1} 段可以领取针单`, r.body);
    tickets.push(r.body.ticket);
  }
  for (const t0 of tickets) {
    const done = await stitchTicket(t0.id, t0);
    assert(done.status === 'completed', `${t0.segmentId} 已逐格报工到出口`);
  }

  const pathRes = await req('GET', '/api/orders/three/full-path');
  assert(pathRes.status === 200, '可以查询完整针路');
  assert(pathRes.body.fullPath.red.length === 6, '完整红线按三段顺序共六格', pathRes.body.fullPath.red);
  assert(pathRes.body.actualPath.length === 6, '实际落针路径也有六格');
  assert(pathRes.body.issuedVersions.every((v) => v.issuedVersion === 1), '各段都能查到签发版本 v1', pathRes.body.issuedVersions);

  // A fresh client only knows the ticket id; GET must reveal the persisted needle point.
  const reconnect = await req('GET', `/api/tickets/${tickets[1].id}`);
  assert(reconnect.status === 200 && reconnect.body.ticket.status === 'completed', '断开再连可读到工单状态');
  assert(reconnect.body.ticket.cursor === 2, '重连后仍停在实际落针/出口处');
}

async function testRedBandAndChanges() {
  console.log('\n2) 两行红带：徽记迫使换行；同一改样在两个签发现场结果不同');
  const scenario = await req('POST', '/api/scenarios/red-band', {});
  assert(scenario.status === 200 && scenario.body.ok, '内置红带现场已建立');
  const s = scenario.body;

  const a = await req('PATCH', '/api/orders/red-order-a/design', { target: 'tail', straightOnly: true });
  assert(a.status === 200, '未签发尾段可以被替换/改约束并保存为主编草稿');
  assert(a.body.completable === false, '首段已实际刺成换行后，尾段只能直走时不能宣布可完', a.body.violations);

  const straightChange = await req('POST', '/api/orders/red-site-straight/change-requests', s.sameChange);
  assert(straightChange.status === 200 && straightChange.body.changeRequest.accepted, '首段已签直走的现场能交这笔尾段直走申请');

  const switchChange = await req('POST', '/api/orders/red-site-switch/change-requests', s.sameChange);
  assert(switchChange.status === 409 && switchChange.body.ok === false, '首段已签换行的同一改样不能交');
  assert(switchChange.body.error === 'change_request_rejected', '拒绝原因是改后无法达成订单');

  const straightView = await req('GET', '/api/orders/red-site-straight');
  const switchView = await req('GET', '/api/orders/red-site-switch');
  assert(straightView.body.designVersion === 2, '前者已产生新的签发设计版本');
  assert(switchView.body.designVersion === 1, '被拒绝的后者没有擅自应用改样');

  const restored = await req('PATCH', '/api/orders/red-site-straight/design', {
    segmentIndex: 1,
    segment: s.straightSite.segmentInputs[1]
  });
  assert(restored.status === 200 && restored.body.completable === true, '还原原始尾段后，两种换行走法恢复且全带可完');
  assert(Object.keys(restored.body.segmentPolicies[1] || {}).length === 0, '整段还原会清掉 straightOnly 限制');
}

async function testConcurrency() {
  console.log('\n3) 两台终端同版本互斥动作；已确认针不能当未刺交回');
  await req('POST', '/api/reset');
  await req('POST', '/api/orders', {
    id: 'race', rows: 2, left: { red: 0 }, right: { red: 0 },
    segments: [{ id: 'long', rows: 2, cols: 3, threads: { red: { path: [[0, 0], [0, 1], [0, 2]] } } }]
  });
  const issued = await req('POST', '/api/orders/race/claim', { segmentIndex: 0, workerId: 'race-worker' });
  const ticketId = issued.body.ticket.id;
  const baseVersion = issued.body.ticket.version;
  const nextCell = issued.body.ticket.nextCell;

  const [stitchRes, returnRes] = await Promise.all([
    req('POST', `/api/tickets/${ticketId}/report`, { expectedVersion: baseVersion }),
    req('POST', `/api/tickets/${ticketId}/return`, { expectedVersion: baseVersion, cell: nextCell })
  ]);
  const successes = [stitchRes, returnRes].filter((r) => r.status === 200);
  const conflicts = [stitchRes, returnRes].filter((r) => r.status === 409);
  assert(successes.length === 1 && conflicts.length === 1, '服务实际受理顺序下恰好一个动作生效', {
    stitch: stitchRes.status, return: returnRes.status
  });
  const final = await req('GET', `/api/tickets/${ticketId}`);
  assert(final.body.ticket.version === baseVersion + 1, '版本只前进一次');
  if (stitchRes.status === 200) {
    assert(final.body.ticket.status === 'in_progress', '若确认先生效，则格位已刺下');
    const tryReturnStitch = await req('POST', `/api/tickets/${ticketId}/return`, {
      expectedVersion: final.body.ticket.version,
      cell: stitchRes.body.ticket.stitched.at(-1)
    });
    assert(tryReturnStitch.status === 409 && tryReturnStitch.body.error === 'already_stitched', '已确认的针不能当未刺交回');
  } else {
    assert(final.body.ticket.status === 'returned', '若交回先生效，则针单进入改样状态');
    const staleStitch = await req('POST', `/api/tickets/${ticketId}/report`, { expectedVersion: baseVersion });
    assert(staleStitch.status === 409, '旧版本确认不能覆盖已受理的交回');
  }

  // Deterministic stitch-first branch.
  await req('POST', '/api/reset');
  await req('POST', '/api/orders', { id: 'race2', rows: 2, left: { red: 0 }, right: { red: 0 }, segments: [straightSegment('a')] });
  const t2 = (await req('POST', '/api/orders/race2/claim', { segmentIndex: 0 })).body.ticket;
  const st = await req('POST', `/api/tickets/${t2.id}/report`, { expectedVersion: t2.version });
  const oldReturn = await req('POST', `/api/tickets/${t2.id}/return`, { expectedVersion: t2.version, cell: t2.nextCell });
  assert(oldReturn.status === 409, '相同旧版本的交回在确认之后失效');
  const returnStitched = await req('POST', `/api/tickets/${t2.id}/return`, {
    expectedVersion: st.body.ticket.version,
    cell: st.body.ticket.stitched.at(-1)
  });
  assert(returnStitched.status === 409 && returnStitched.body.error === 'already_stitched', '当前版本也不能把已刺针交回');

  // Deterministic return-first branch on a fresh ticket.
  await req('POST', '/api/orders', { id: 'race3', rows: 2, left: { red: 0 }, right: { red: 0 }, segments: [straightSegment('a')] });
  const t3 = (await req('POST', '/api/orders/race3/claim', { segmentIndex: 0 })).body.ticket;
  const ret = await req('POST', `/api/tickets/${t3.id}/return`, { expectedVersion: t3.version, cell: t3.nextCell });
  assert(ret.status === 200, '未刺下一格连同余单可以先交回');
  const staleReport = await req('POST', `/api/tickets/${t3.id}/report`, { expectedVersion: t3.version });
  assert(staleReport.status === 409, '交回后旧报工版本不能再生效');
}

async function testLateReturnDoesNotRollBackNextTicket() {
  console.log('\n5) 迟到交回不能让已签发给下一位的活倒退');
  await req('POST', '/api/reset');
  await req('POST', '/api/orders', {
    id: 'late', rows: 2, left: { red: 0 }, right: { red: 0 },
    segments: [straightSegment('a'), straightSegment('b')]
  });
  const a = (await req('POST', '/api/orders/late/claim', { segmentIndex: 0, workerId: 'a' })).body.ticket;
  const b = (await req('POST', '/api/orders/late/claim', { segmentIndex: 1, workerId: 'b' })).body.ticket;
  const doneA = await stitchTicket(a.id, a);
  assert(doneA.status === 'completed', '前段已完整交回出口');

  const lateOld = await req('POST', `/api/tickets/${a.id}/return`, {
    expectedVersion: 1,
    cell: { row: 0, col: 0 }
  });
  assert(lateOld.status === 409, '携带旧签发版本的迟到交回被拒绝');
  const lateCurrent = await req('POST', `/api/tickets/${a.id}/return`, { expectedVersion: doneA.version });
  assert(lateCurrent.status === 409, '已到出口、没有未刺余单时不能再交回');

  const bAfter = await req('GET', `/api/tickets/${b.id}`);
  assert(bAfter.body.ticket.status === 'issued', '下一位的针单仍为已签发');
  assert(bAfter.body.ticket.version === 1, '下一位针单版本没有倒退');
  assert(bAfter.body.ticket.workerId === 'b', '下一位领绷人不变');
}

async function testFlipTransform() {
  console.log('\n4) 上下翻面时入口、出口和实际针迹一起翻');
  await req('POST', '/api/reset');
  await req('POST', '/api/orders', {
    id: 'flip', rows: 2, left: { red: 1 }, right: { red: 1 },
    segments: [{ id: 'f', rows: 2, cols: 2, allowFlip: true, threads: { red: { path: [[0, 0], [0, 1]] } } }]
  });
  const issued = await req('POST', '/api/orders/flip/claim', { segmentIndex: 0, flip: 'vertical' });
  assert(issued.status === 200, '允许翻面的样段可以签发翻面针单');
  const d = issued.body.ticket.diagram;
  assert(d.localOrderedCells.every((p) => p.row === 1), '实际针迹已翻到下行', d.localOrderedCells);
  assert(d.localEntry.row === 1 && d.localExit.row === 1, '入口和出口也一起翻到下行');
}

async function testRestartPersistence() {
  if (process.env.BASE_URL) {
    console.log('\n5) 已指定 BASE_URL，跳过脚本内重启持久化测试');
    return;
  }
  console.log('\n6) 服务重启/断开再连后仍能续到实际落针处');
  let c = child;
  await stopServer(c);
  c = startServer(true);
  child = c;
  await waitUp();
  const view = await req('GET', '/api/orders/late');
  assert(view.status === 200 && view.body.actualPath.length === 2, '重启后前段报工与完整针路仍在磁盘状态中');
  const b = view.body.issuedVersions[1];
  const middle = await req('GET', `/api/tickets/${b.ticketId}`);
  assert(middle.body.ticket.version === 1 && middle.body.ticket.status === 'issued', '重启后已签发的下一段版本和状态不变');
}

async function main() {
  child = startServer();
  await waitUp();
  try {
    await testThreeSegmentsAndReconnect();
    await testRedBandAndChanges();
    await testConcurrency();
    await testFlipTransform();
    await testLateReturnDoesNotRollBackNextTicket();
    await testRestartPersistence();
    console.log('\n全部验题脚本通过 ✅');
  } catch (err) {
    console.error('\n验题失败 ❌', err.message);
    if (err.extra) console.error(JSON.stringify(err.extra, null, 2));
    process.exitCode = 1;
  } finally {
    await stopServer(child);
  }
}

main();
