#!/usr/bin/env node
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import os from 'node:os';
import path from 'node:path';
import { rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { stations, walkPath, cars, neighborTicket, guTicket, cExistingTicket, soloTicket } from '../src/canonical.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const serverPath = path.join(__dirname, '..', 'src', 'server.js');
const port = Number(process.env.DEMO_PORT || 3399);
const base = `http://127.0.0.1:${port}`;
const dataFile = path.join(os.tmpdir(), 'carpool-restart-demo.json');

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitForHealth() {
  for (let i = 0; i < 80; i += 1) {
    try {
      const response = await fetch(`${base}/health`);
      if (response.ok) return;
    } catch {
      // server is starting
    }
    await sleep(100);
  }
  throw new Error('server did not become healthy');
}

function startServer() {
  const child = spawn(process.execPath, [serverPath], {
    env: { ...process.env, PORT: String(port), DATA_FILE: dataFile },
    stdio: ['ignore', 'pipe', 'inherit'],
  });
  child.stdout.on('data', (chunk) => console.log(`[server] ${chunk.toString().trim()}`));
  return child;
}

async function stopServer(child) {
  if (!child.killed) child.kill('SIGTERM');
  await once(child, 'exit');
}

async function api(method, route, payload, requestId) {
  const response = await fetch(`${base}${route}`, {
    method,
    headers: {
      'content-type': 'application/json',
      ...(requestId ? { 'idempotency-key': requestId } : {}),
    },
    body: payload === undefined ? undefined : JSON.stringify(requestId ? { ...payload, requestId } : payload),
  });
  const text = await response.text();
  const json = text ? JSON.parse(text) : {};
  if (!response.ok) throw new Error(`${method} ${route} failed ${response.status}: ${JSON.stringify(json)}`);
  return json;
}

async function seed() {
  for (const station of stations) await api('POST', '/stations', station, `seed-${station.id}`);
  await api('POST', '/walk-paths', walkPath, 'seed-walk');
  for (const car of cars) await api('POST', '/cars', car, `seed-${car.id}`);
  await api('POST', '/tickets', neighborTicket, 'seed-rs241');
  await api('POST', '/tickets', guTicket, 'seed-rsgu');
  await api('POST', '/tickets', cExistingTicket, 'seed-rsc001');
  await api('POST', '/tickets', soloTicket, 'seed-rs303');
}

async function runWorkflow() {
  console.log('\n-- 经营决定：C 车已有 1 单，剩余 2 个兜底座位留给两位邻居 --');
  const allocation = await api('POST', '/operator/allocate-fallback', {
    carId: 'car_c',
    asOfMinute: 15,
    requests: [
      { ticketCode: 'RS_303', passengerIds: ['he'], legNo: 1 },
      { ticketCode: 'RS_241', passengerIds: ['lin', 'zhou'], legNo: 2, priority: 'high' },
    ],
  }, 'alloc-c-seats');
  assert.equal(allocation.remainingSeats, 0);
  assert.deepEqual(allocation.selected.map((item) => item.ticketCode), ['RS_241']);
  assert.deepEqual(allocation.deferred.map((item) => item.ticketCode), ['RS_303']);
  console.log('签单组 =', allocation.selected.map((x) => x.ticketCode).join(','), '; 延后 =', allocation.deferred.map((x) => `${x.ticketCode}(${x.decision})`).join(','));

  console.log('\n-- 两名司机共同确认定点保接窗 HF_DEMO（B 车留到第 15 分）--');
  const handoff = await api('POST', '/handoffs', {
    id: 'HF_DEMO',
    ticketCode: 'RS_241',
    fromCarId: 'car_a',
    toCarId: 'car_b',
    stationId: 'center',
    windowStartMin: 12,
    windowEndMin: 15,
    holdUntilMin: 15,
  }, 'create-hf-demo');
  assert.equal(handoff.waitAssessment.feasible, true);
  await api('POST', '/handoffs/HF_DEMO/confirm', { carId: 'car_a', driverId: 'drv_wang', atMinute: 9 }, 'hf-a-confirm');
  const b1 = await api('POST', '/handoffs/HF_DEMO/confirm', { carId: 'car_b', driverId: 'drv_li', atMinute: 10 });
  const b2 = await api('POST', '/handoffs/HF_DEMO/confirm', { carId: 'car_b', driverId: 'drv_li', atMinute: 10 });
  assert.equal(b1.handoff.status, 'signed');
  assert.equal(b2.duplicate, true);
  console.log('handoff status =', b1.handoff.status, '; 司机 B 重复确认 duplicate =', b2.duplicate);

  console.log('\n-- 司机报实际进出站，旅客用乘车单确认上/下车 --');
  await api('POST', '/cars/car_a/events', { type: 'arrived', stationId: 'south', atMinute: 8 }, 'a-arr-s');
  const boardA1 = await api('POST', '/tickets/RS_241/board', { passengerId: 'lin', atMinute: 8 }, 'lin-board-a');
  const boardA2 = await api('POST', '/tickets/RS_241/board', { passengerId: 'lin', atMinute: 8 }, 'lin-board-a');
  assert.equal(boardA1.idempotentReplay === undefined || boardA1.idempotentReplay === false, true);
  assert.equal(boardA2.idempotentReplay, true);
  assert.equal(boardA2.duplicate, true);
  await api('POST', '/tickets/RS_241/board', { passengerId: 'zhou', atMinute: 8 }, 'zhou-board-a');
  console.log('林岚 A 车上车回执 =', boardA1.receipt.id, '; 重复提交回放同一回执 =', boardA2.receipt.id === boardA1.receipt.id);

  await api('POST', '/cars/car_a/events', { type: 'departed', stationId: 'south', atMinute: 9 }, 'a-dep-s');
  await api('POST', '/cars/car_a/events', { type: 'arrived', stationId: 'center', atMinute: 10 }, 'a-arr-c');
  const alightLin1 = await api('POST', '/tickets/RS_241/alight', { passengerId: 'lin', carId: 'car_a', atMinute: 10 }, 'lin-alight-a');
  const alightLin2 = await api('POST', '/tickets/RS_241/alight', { passengerId: 'lin', carId: 'car_a', atMinute: 10 }, 'lin-alight-a');
  await api('POST', '/tickets/RS_241/alight', { passengerId: 'zhou', carId: 'car_a', atMinute: 10 }, 'zhou-alight-a');
  assert.equal(alightLin2.idempotentReplay, true);
  console.log('林岚 A 车下车回执 =', alightLin1.receipt.id, '; 重复下车确认被幂等处理 =', alightLin2.duplicate);

  await api('POST', '/cars/car_b/events', { type: 'arrived', stationId: 'center', atMinute: 12 }, 'b-arr-c');
  await api('POST', '/tickets/RS_GU242/board', { passengerId: 'gu', carId: 'car_b', atMinute: 12 }, 'gu-board-b');
  const boardB = await api('POST', '/tickets/RS_241/board', { passengerId: 'lin', carId: 'car_b', atMinute: 12 }, 'lin-board-b');
  await api('POST', '/tickets/RS_241/board', { passengerId: 'zhou', carId: 'car_b', atMinute: 12 }, 'zhou-board-b');
  console.log('两位邻居已凭 RS_241 上 B 车；林岚 B 车回执 =', boardB.receipt.id);

  console.log('\n-- 一笔迟到但符合保接窗的离站消息 --');
  const departure = await api('POST', '/cars/car_b/events', {
    type: 'departed',
    stationId: 'center',
    atMinute: 15,
    reason: '等两位邻居从东下客点走到西上客点',
  }, 'b-dep-c');
  assert.equal(departure.report.lateDeparture, true);
  assert.equal(departure.report.holdBreach, false);
  console.log('B 车实际离站 = 15 分；相对计划 14 分迟到 1 分；holdBreach = false');

  console.log('\n-- 顾阿姨途中把下车站由槐树巷改为周末市集 --');
  const changed = await api('POST', '/tickets/RS_GU242/change-station', {
    passengerId: 'gu',
    legNo: 1,
    newToStationId: 'market',
    atMinute: 17,
    reason: '槐树巷临时管制，旅客确认改到市集正门',
  }, 'gu-change-station');
  assert.equal(changed.leg.toStationId, 'market');
  await api('POST', '/cars/car_b/events', { type: 'arrived', stationId: 'old', atMinute: 19 }, 'b-arr-old');
  await api('POST', '/cars/car_b/events', { type: 'departed', stationId: 'old', atMinute: 19 }, 'b-dep-old');
  console.log('RS_GU242 第 1 段下车站已改为 =', changed.leg.toStationId, '; 槐树巷通过但未销下车回执');
}

async function verifyAfterRestart() {
  const rs241 = await api('GET', '/tickets/RS_241/location');
  const rsGu = await api('GET', '/tickets/RS_GU242/location');
  const hf = await api('GET', '/handoffs/HF_DEMO');
  const messages = await api('GET', '/messages');
  const late = messages.find((m) => m.type === 'driver.late_departure' && m.carId === 'car_b');

  assert.equal(hf.status, 'signed');
  assert.deepEqual(rs241.currentCarIds, ['car_b']);
  assert.equal(rs241.passengers.length, 2);
  for (const passenger of rs241.passengers) {
    assert.equal(passenger.currentCarId, 'car_b');
    assert.equal(passenger.owedLegs.length, 1);
    assert.equal(passenger.owedLegs[0].action, 'alight');
    assert.equal(passenger.owedLegs[0].toStationId, 'market');
  }
  assert.equal(rsGu.passengers[0].currentCarId, 'car_b');
  assert.equal(rsGu.passengers[0].owedLegs[0].toStationId, 'market');
  assert.ok(late);
  assert.equal(late.minutesLate, 1);

  console.log('\n-- 服务重启后持久化查询 --');
  console.log('RS_241 当前车辆 =', rs241.currentCarIds.join(','));
  for (const p of rs241.passengers) {
    console.log(`  ${p.name}(${p.id}): status=${p.status}, completedLegs=[${p.completedLegNos}], owed=${p.owedLegs[0].detail}`);
  }
  const gu = rsGu.passengers[0];
  console.log(`RS_GU242 ${gu.name}(${gu.id}): currentCar=${gu.currentCarId}, owedTo=${gu.owedLegs[0].toStationId}`);
  console.log('HF_DEMO =', hf.status, '; 迟到消息 =', `${late.carId} at ${late.atMinute} +${late.minutesLate}m`);
  console.log('\n演示通过：重启后仍能按乘车单查到人在哪辆车、哪一段还欠下车回执。');
}

rmSync(dataFile, { force: true });
let child = startServer();
try {
  await waitForHealth();
  await seed();
  await runWorkflow();
  await stopServer(child);

  console.log('\n-- 重新启动同一数据文件，验证恢复 --');
  child = startServer();
  await waitForHealth();
  await verifyAfterRestart();
} finally {
  if (child) await stopServer(child).catch(() => {});
}
