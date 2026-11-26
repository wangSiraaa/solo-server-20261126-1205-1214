import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { buildSeededDb } from '../src/seed.js';
import { runRehearsal } from '../src/rehearsal.js';
import { JsonStore } from '../src/store.js';
import {
  addSweepReport,
  amendOrderToContract,
  assessOrder,
  closeOrder,
  complaintView,
  createCar,
  createComplaint,
  createShift,
  createTrip,
  dispatchMakeup,
  resegment,
} from '../src/core.js';
import { planNextLink, shortestPath } from '../src/planning.js';

test('北门两米：正确方向刷足边界即可结单；反向行驶不计', () => {
  const db = buildSeededDb();
  const shift = createShift(db, { at: 1, promisedEndAt: 30, merchantIds: ['M_NORTH'] });
  const orderId = shift.orderIds[0];
  const trip = createTrip(db, { carId: 'CAR1', shiftId: shift.id, at: 2, routeLinkIds: ['L31'] });

  addSweepReport(db, {
    tripId: trip.id,
    linkId: 'L13', // 反向；右侧是 CURB_31_L，不是北门合同所在 CURB_31_R
    brushDownM: 0,
    brushUpM: 2,
    sweptAt: 10,
    receivedAt: 10,
  });
  assert.equal(assessOrder(db, orderId, 10).physicallyComplete, false); // 服务台此时尚未收到 11 分才送达的记录
  assert.deepEqual(assessOrder(db, orderId).missing[0].interval, [0, 2]);

  addSweepReport(db, {
    tripId: trip.id,
    linkId: 'L31',
    brushDownM: 0,
    brushUpM: 2, // 恰好两米
    sweptAt: 11,
    receivedAt: 11,
  });
  const closed = closeOrder(db, { orderId, at: 31 });
  assert.equal(closed.order.status, 'closed_on_time');
});

test('当班中只把合同扩一米，原趟不能结清；恢复合同不删除已增权利', () => {
  const db = buildSeededDb();
  const shift = createShift(db, { at: 1, promisedEndAt: 30, merchantIds: ['M_NORTH'] });
  const orderId = shift.orderIds[0];
  const trip = createTrip(db, { carId: 'CAR1', shiftId: shift.id, at: 2 });
  addSweepReport(db, { tripId: trip.id, linkId: 'L31', brushDownM: 0, brushUpM: 2, sweptAt: 10, receivedAt: 10 });

  amendOrderToContract(db, {
    orderId,
    at: 15,
    frontages: [{ curbId: 'CURB_31_R', fromM: 0, toM: 3 }],
  });
  let assessment = assessOrder(db, orderId, 31);
  assert.equal(assessment.physicallyComplete, false);
  assert.deepEqual(assessment.missing[0].interval, [2, 3]);
  assert.throws(() => closeOrder(db, { orderId, at: 31 }), (err) => err.code === 'STILL_OWED');

  amendOrderToContract(db, {
    orderId,
    at: 32,
    frontages: [{ curbId: 'CURB_31_R', fromM: 0, toM: 2 }],
  });
  assessment = assessOrder(db, orderId, 33);
  // 已追加的第三米权利不被合同恢复注销。
  assert.deepEqual(assessment.missing[0].interval, [2, 3]);
});

test('重新划段后仍锚定原路段米坐标，同一米不会产生两份权利', () => {
  const db = buildSeededDb();
  resegment(db, { segmentId: 'SEG_31', cuts: [1], at: 1 });
  const shift = createShift(db, { at: 2, promisedEndAt: 20, merchantIds: ['M_NORTH'] });
  const orderId = shift.orderIds[0];
  const trip = createTrip(db, { carId: 'CAR1', shiftId: shift.id, at: 3 });

  assert.equal(db.rights.filter((r) => r.orderId === orderId).length, 1);
  assert.equal(db.rights.find((r) => r.orderId === orderId).toM, 2);

  addSweepReport(db, { tripId: trip.id, linkId: 'L31_S1', brushDownM: 0, brushUpM: 1, sweptAt: 4, receivedAt: 4 });
  addSweepReport(db, { tripId: trip.id, linkId: 'L31_S2', brushDownM: 0, brushUpM: 1, sweptAt: 5, receivedAt: 5 });

  const assessment = assessOrder(db, orderId, 21);
  assert.equal(assessment.physicallyComplete, true);
  assert.deepEqual(assessment.missing, []);
});

test('两种到达顺序都保留两份投诉权利并得到相同最终覆盖', () => {
  for (const arrivalOrder of ['late-first', 'makeup-first']) {
    const db = buildSeededDb();
    const result = runRehearsal(db, { arrivalOrder });
    assert.equal(result.ok, true, JSON.stringify(result.assertions));
    assert.equal(db.complaints.length, 2);
    assert.equal(db.complaints.every((c) => c.status === 'resolved'), true);

    const ext = complaintView(db, 'C_EXT');
    const lift = complaintView(db, 'C_LIFT');
    assert.deepEqual(ext.claims[0].missingIntervals, []);
    assert.deepEqual(lift.claims[0].missingIntervals, []);
    assert.equal(ext.claims[0].meters[0].filledBy.some((f) => f.tripId === 'T_MAKEUP'), true);
    assert.equal(lift.claims[0].meters[0].filledBy.some((f) => f.tripId === 'T_MAKEUP'), true);
  }
});

test('重启后凭投诉号仍可读取米级实扫来源', () => {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'coop-')), 'restart.json');
  const store = new JsonStore(file);
  store.transact((db) => runRehearsal(db, { arrivalOrder: 'late-first' }));

  const restarted = new JsonStore(file);
  const view = complaintView(restarted.db, 'TS-LIFT');
  assert.equal(view.computedStatus, 'resolved');
  assert.equal(view.claims[0].meters[0].filledBy[0].tripId, 'T_MAKEUP');
  assert.ok(fs.existsSync(file));
});

test('固定整圈车未回场时，新增欠段可交给另一台车', () => {
  const db = buildSeededDb();
  createCar(db, { id: 'CAR2', name: '应急二号' });
  const shift = createShift(db, { at: 1, promisedEndAt: 10, merchantIds: ['M_NORTH'] });
  const complaint = createComplaint(db, { orderId: shift.orderIds[0], at: 11 });
  const active = createTrip(db, { carId: 'CAR1', shiftId: shift.id, at: 2, routeLinkIds: ['L12', 'L23', 'L31'] });

  assert.throws(
    () => dispatchMakeup(db, { complaintIds: [complaint.id], carId: 'CAR1', at: 12 }),
    (err) => err.code === 'FIXED_LOOP_FROZEN',
  );
  const makeup = dispatchMakeup(db, { complaintIds: [complaint.id], carId: 'CAR2', at: 12 });
  assert.equal(makeup.trip.carId, 'CAR2');
  assert.equal(active.frozen, true);
});

test('逐段要路会给出右扫段，并在封路时绕行', () => {
  const db = buildSeededDb();
  const shift = createShift(db, { at: 1, promisedEndAt: 10, merchantIds: ['M_NORTH'] });
  const complaint = createComplaint(db, { orderId: shift.orderIds[0], at: 11 });
  const trip = createTrip(db, { mode: 'on_demand', carId: 'CAR1', at: 12, startNodeId: 'I1', complaintIds: [complaint.id] });

  const atSource = planNextLink(db, trip.id, { currentNodeId: 'I3' });
  assert.equal(atSource.instruction, 'sweep');
  assert.equal(atSource.nextLinkId, 'L31');
  assert.deepEqual([atSource.brush.brushDownM, atSource.brush.brushUpM], [0, 2]);

  const detour = planNextLink(db, trip.id, { currentNodeId: 'I1', blockedLinkIds: ['L13'] });
  assert.equal(detour.instruction, 'deadhead');
  assert.equal(detour.nextLinkId, 'L12');
  assert.deepEqual(detour.approachPathLinkIds, ['L12', 'L23']);
});

test('逐段规划遵守禁止掉头和封路约束', () => {
  const db = buildSeededDb();

  // I2 不允许掉头：从 L12 到达 I2，不能立即沿 L21 返回，只能绕 L23/L31。
  const around = shortestPath(db, 'I2', 'I1', { incomingLinkId: 'L12' });
  assert.deepEqual(around.linkIds, ['L23', 'L31']);

  // 封掉 L23 后，I2 处既不能直行也不能合法掉头。
  const blocked = shortestPath(db, 'I2', 'I1', { incomingLinkId: 'L12', blockedLinkIds: ['L23'] });
  assert.equal(blocked, null);

  // I3 允许掉头：沿 L23 到 I3 后可立即换 L32。
  const uTurn = shortestPath(db, 'I3', 'I2', { incomingLinkId: 'L23' });
  assert.deepEqual(uTurn.linkIds, ['L32']);
});
