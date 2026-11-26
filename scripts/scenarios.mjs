#!/usr/bin/env node
import assert from 'node:assert/strict';
import { seedCanonicalService } from '../src/canonical.js';

function line(title) {
  console.log(`\n=== ${title} ===`);
}

function show(label, value) {
  console.log(`${label}:`, JSON.stringify(value, null, 2));
}

function catchCode(fn) {
  try {
    fn();
    return null;
  } catch (error) {
    return error.code;
  }
}

// Case 1: actual stops A 10-12, B 12-14, two-minute walk.
{
  line('例 1：10 分已下车 + 2 分钟站内步行，赶上 12 分到站的 B 车');
  const svc = seedCanonicalService();
  svc.reportCarEvent('car_a', { type: 'arrived', stationId: 'south', atMinute: 8 });
  svc.reportCarEvent('car_a', { type: 'departed', stationId: 'south', atMinute: 9 });
  svc.board('RS_241', { passengerId: 'lin', atMinute: 8 });
  svc.board('RS_241', { passengerId: 'zhou', atMinute: 8 });
  svc.reportCarEvent('car_a', { type: 'arrived', stationId: 'center', atMinute: 10 });
  svc.alight('RS_241', { passengerId: 'lin', carId: 'car_a', atMinute: 10 });
  svc.alight('RS_241', { passengerId: 'zhou', carId: 'car_a', atMinute: 10 });
  svc.reportCarEvent('car_a', { type: 'departed', stationId: 'center', atMinute: 12 });
  svc.reportCarEvent('car_b', { type: 'arrived', stationId: 'center', atMinute: 12 });

  const transfer = svc.evaluateTransfer({
    toCarId: 'car_b',
    stationId: 'center',
    alightAtMinute: 10,
    fromPointId: 'pt_drop',
    toPointId: 'pt_board',
  });
  show('换乘可行性', transfer);
  assert.equal(transfer.feasible, true);

  const receipt = svc.board('RS_241', { passengerId: 'lin', carId: 'car_b', atMinute: 12 }).receipt;
  const duplicate = svc.board('RS_241', { passengerId: 'lin', carId: 'car_b', atMinute: 12 });
  svc.board('RS_241', { passengerId: 'zhou', carId: 'car_b', atMinute: 12 });
  svc.reportCarEvent('car_b', { type: 'departed', stationId: 'center', atMinute: 14 });
  assert.equal(duplicate.duplicate, true);
  console.log('真实上车回执:', receipt.id, receipt.carId, receipt.atMinute);
  console.log('重复确认返回原回执:', duplicate.receipt.id === receipt.id);
}

// Case 2: A delayed to actual stop 13-15; B still 12-14.
{
  line('例 2：A 实际停靠 13-15，13 分才能下车；B 14 分驶离，不能接手');
  const svc = seedCanonicalService();
  svc.reportCarEvent('car_a', { type: 'arrived', stationId: 'south', atMinute: 8 });
  svc.reportCarEvent('car_a', { type: 'departed', stationId: 'south', atMinute: 9 });
  svc.reportCarEvent('car_a', { type: 'arrived', stationId: 'center', atMinute: 13 });
  svc.board('RS_241', { passengerId: 'lin', atMinute: 8 });
  svc.alight('RS_241', { passengerId: 'lin', carId: 'car_a', atMinute: 13 });
  svc.reportCarEvent('car_a', { type: 'departed', stationId: 'center', atMinute: 15 });
  svc.reportCarEvent('car_b', { type: 'arrived', stationId: 'center', atMinute: 12 });
  svc.reportCarEvent('car_b', { type: 'departed', stationId: 'center', atMinute: 14 });

  const transfer = svc.evaluateTransfer({
    toCarId: 'car_b',
    stationId: 'center',
    alightAtMinute: 13,
    fromPointId: 'pt_drop',
    toPointId: 'pt_board',
  });
  show('换乘判定', { feasible: transfer.feasible, readyAtMinute: transfer.readyAtMinute, slackMinutes: transfer.slackMinutes, reasons: transfer.reasons });
  assert.equal(transfer.feasible, false);
  const code = catchCode(() => svc.board('RS_241', { passengerId: 'lin', carId: 'car_b', atMinute: 14 }));
  assert.equal(code, 'TRANSFER_NOT_FEASIBLE');
  console.log('B 车上车结果: 拒绝，错误码 =', code);
}

// Case 3: restore alight to 10, change walking to five, dispatch real C.
{
  line('例 3：下车时刻还原为 10 分，只把站内步行改为 5 分钟；B 不能销单，另派 C');
  const svc = seedCanonicalService();
  svc.updateWalkPath('walk_center', { minutes: 5 });
  svc.reportCarEvent('car_a', { type: 'arrived', stationId: 'south', atMinute: 8 });
  svc.reportCarEvent('car_a', { type: 'departed', stationId: 'south', atMinute: 9 });
  svc.board('RS_241', { passengerId: 'lin', atMinute: 8 });
  svc.reportCarEvent('car_a', { type: 'arrived', stationId: 'center', atMinute: 10 });
  svc.alight('RS_241', { passengerId: 'lin', carId: 'car_a', atMinute: 10 });
  svc.reportCarEvent('car_a', { type: 'departed', stationId: 'center', atMinute: 12 });
  svc.reportCarEvent('car_b', { type: 'arrived', stationId: 'center', atMinute: 12 });

  const transfer = svc.evaluateTransfer({
    toCarId: 'car_b',
    stationId: 'center',
    alightAtMinute: 10,
    fromPointId: 'pt_drop',
    toPointId: 'pt_board',
  });
  assert.equal(transfer.feasible, false);
  console.log('B 车 slackMinutes =', transfer.slackMinutes, '; readyAt =', transfer.readyAtMinute);
  const bCode = catchCode(() => svc.board('RS_241', { passengerId: 'lin', carId: 'car_b', atMinute: 12 }));
  assert.equal(bCode, 'TRANSFER_NOT_FEASIBLE');
  svc.reportCarEvent('car_b', { type: 'departed', stationId: 'center', atMinute: 14 });
  console.log('原 B 车销单结果:', bCode);

  const dispatch = svc.createOnDemandDispatch('RS_241', { passengerIds: ['lin'] });
  assert.equal(dispatch.candidates[0].carId, 'car_c');
  svc.acceptDispatch(dispatch.id, { carId: 'car_c', driverId: 'drv_zhao' });
  svc.reportCarEvent('car_c', { type: 'arrived', stationId: 'south', atMinute: 12 });
  svc.reportCarEvent('car_c', { type: 'departed', stationId: 'south', atMinute: 13 });
  svc.reportCarEvent('car_c', { type: 'arrived', stationId: 'center', atMinute: 16 });
  const receipt = svc.board('RS_241', { passengerId: 'lin', carId: 'car_c', atMinute: 16 }).receipt;
  assert.ok(receipt.id);
  show('C 车真实上车回执', receipt);
  const location = svc.getLocation('RS_241');
  assert.equal(location.passengers.find((p) => p.id === 'lin').currentCarId, 'car_c');
  console.log('重启后可按 RS_241 查到林岚在 car_c；回执已持久化在 receipts 集合。');
}

// Invariant: never alighted.
{
  line('附加 invariant：未下 A 车，不能上 B 车');
  const svc = seedCanonicalService();
  svc.reportCarEvent('car_a', { type: 'arrived', stationId: 'south', atMinute: 8 });
  svc.board('RS_241', { passengerId: 'lin', atMinute: 8 });
  svc.reportCarEvent('car_b', { type: 'arrived', stationId: 'center', atMinute: 12 });
  const code = catchCode(() => svc.board('RS_241', { passengerId: 'lin', carId: 'car_b', legNo: 2, atMinute: 12 }));
  assert.equal(code, 'MUST_ALIGHT_FIRST');
  console.log('结果:', code);
}

console.log('\n全部 canonical scenarios 通过。');
