import test from 'node:test';
import assert from 'node:assert/strict';
import { DispatchService, createInitialState, DomainError } from '../src/domain.js';

function seedWorld() {
  const svc = new DispatchService(createInitialState());
  svc.createStation({ id: 'south', name: '社区南门', points: [{ id: 'pt_south', name: '路边上车点' }] });
  svc.createStation({
    id: 'center',
    name: '邻里中心站',
    points: [
      { id: 'pt_drop', name: '第一车下客点' },
      { id: 'pt_board', name: '第二车上客点' },
    ],
  });
  svc.createStation({ id: 'old', name: '槐树巷', points: [{ id: 'pt_old' }] });
  svc.createStation({ id: 'market', name: '周末市集', points: [{ id: 'pt_market' }] });
  svc.createWalkPath({ id: 'walk_center', fromPointId: 'pt_drop', toPointId: 'pt_board', minutes: 2 });

  svc.createCar({
    id: 'car_a',
    driver: { id: 'drv_wang', name: '王师傅' },
    capacity: 4,
    workWindow: { startMin: 0, endMin: 60 },
    stops: [
      { stationId: 'south', pointId: 'pt_south', etaMinutes: 8, plannedArrivalMin: 8, plannedDepartureMin: 9 },
      { stationId: 'center', pointId: 'pt_drop', etaMinutes: 10, plannedArrivalMin: 10, plannedDepartureMin: 12, canWaitMinutes: 3 },
      { stationId: 'old', pointId: 'pt_old', plannedArrivalMin: 16, plannedDepartureMin: 17 },
      { stationId: 'market', pointId: 'pt_market', plannedArrivalMin: 22, plannedDepartureMin: 23 },
    ],
  });
  svc.createCar({
    id: 'car_b',
    driver: { id: 'drv_li', name: '李师傅' },
    capacity: 4,
    workWindow: { startMin: 0, endMin: 60 },
    stops: [
      { stationId: 'center', pointId: 'pt_board', etaMinutes: 12, plannedArrivalMin: 12, plannedDepartureMin: 14, canWaitMinutes: 2 },
      { stationId: 'old', pointId: 'pt_old', plannedArrivalMin: 18, plannedDepartureMin: 19 },
      { stationId: 'market', pointId: 'pt_market', plannedArrivalMin: 23, plannedDepartureMin: 24 },
    ],
    onboardCommitments: [
      { passengerId: 'gu', ticketCode: 'RS_GU', alightStationId: 'old', latestAlightMin: 20 },
    ],
  });
  svc.createCar({
    id: 'car_c',
    driver: { id: 'drv_zhao', name: '赵师傅' },
    capacity: 3,
    workWindow: { startMin: 6, endMin: 40 },
    stops: [
      { stationId: 'south', pointId: 'pt_south', plannedArrivalMin: 12, plannedDepartureMin: 13 },
      { stationId: 'center', pointId: 'pt_board', plannedArrivalMin: 16, plannedDepartureMin: 18 },
      { stationId: 'old', pointId: 'pt_old', plannedArrivalMin: 22, plannedDepartureMin: 23 },
      { stationId: 'market', pointId: 'pt_market', plannedArrivalMin: 27, plannedDepartureMin: 28 },
    ],
  });
  return svc;
}

function createNeighborTicket(svc, code = 'RS_241') {
  return svc.createTicket({
    code,
    partyName: '两位邻居',
    passengers: [
      { id: 'lin', name: '林岚' },
      { id: 'zhou', name: '周屿' },
    ],
    legs: [
      { legNo: 1, carId: 'car_a', fromStationId: 'south', toStationId: 'center', promisedAlightByMin: 12 },
      { legNo: 2, carId: 'car_b', fromStationId: 'center', toStationId: 'market', promisedAlightByMin: 30 },
    ],
    companionRequirements: [{ passengerIds: ['lin', 'zhou'], legNos: [1, 2], note: '两位邻居全程同车' }],
    transfer: { mode: 'protected', stationId: 'center' },
    fallback: { eligible: true, stationId: 'center', pointId: 'pt_board' },
  });
}

function reportABaseline(svc) {
  svc.reportCarEvent('car_a', { type: 'arrived', stationId: 'south', atMinute: 8 });
  svc.reportCarEvent('car_a', { type: 'departed', stationId: 'south', atMinute: 9 });
  svc.reportCarEvent('car_a', { type: 'arrived', stationId: 'center', atMinute: 10 });
}

test('10 分下车、两分钟站内步行、后车 12 分到 14 分开：换乘成立且重复确认不产生第二张回执', () => {
  const svc = seedWorld();
  createNeighborTicket(svc);
  reportABaseline(svc);

  const firstBoard = svc.board('RS_241', { passengerId: 'lin', atMinute: 8 });
  assert.equal(firstBoard.duplicate, false);
  const duplicateBoard = svc.board('RS_241', { passengerId: 'lin', atMinute: 8 });
  assert.equal(duplicateBoard.duplicate, true);
  assert.equal(duplicateBoard.receipt.id, firstBoard.receipt.id);

  svc.board('RS_241', { passengerId: 'zhou', atMinute: 8 });
  const linAlight = svc.alight('RS_241', { passengerId: 'lin', carId: 'car_a', atMinute: 10 });
  svc.alight('RS_241', { passengerId: 'zhou', carId: 'car_a', atMinute: 10 });
  assert.equal(linAlight.receipt.atMinute, 10);

  svc.reportCarEvent('car_b', { type: 'arrived', stationId: 'center', atMinute: 12 });
  const evaluation = svc.evaluateTransfer({
    toCarId: 'car_b',
    stationId: 'center',
    alightAtMinute: 10,
    fromPointId: 'pt_drop',
    toPointId: 'pt_board',
  });
  assert.deepEqual([evaluation.feasible, evaluation.readyAtMinute, evaluation.slackMinutes], [true, 12, 2]);

  const boardB = svc.board('RS_241', { passengerId: 'lin', carId: 'car_b', atMinute: 12 });
  const repeated = svc.board('RS_241', { passengerId: 'lin', carId: 'car_b', atMinute: 12 });
  assert.equal(boardB.receipt.type, 'BOARD');
  assert.equal(repeated.duplicate, true);
  assert.equal(repeated.receipt.id, boardB.receipt.id);
  svc.board('RS_241', { passengerId: 'zhou', carId: 'car_b', atMinute: 12 });

  const location = svc.getLocation('RS_241');
  assert.deepEqual(location.currentCarIds, ['car_b']);
  assert.equal(location.passengers[0].owedLegs[0].action, 'alight');
  assert.match(location.passengers[0].owedLegs[0].detail, /周末市集/);
});

test('第一车推迟到 13 至 15 分：13 分下车的人赶不上 14 分离站的 B 车', () => {
  const svc = seedWorld();
  createNeighborTicket(svc);
  svc.reportCarEvent('car_a', { type: 'arrived', stationId: 'south', atMinute: 8 });
  svc.reportCarEvent('car_a', { type: 'departed', stationId: 'south', atMinute: 9 });
  svc.reportCarEvent('car_a', { type: 'arrived', stationId: 'center', atMinute: 13 });
  svc.board('RS_241', { passengerId: 'lin', atMinute: 8 });
  svc.alight('RS_241', { passengerId: 'lin', carId: 'car_a', atMinute: 13 });

  svc.reportCarEvent('car_b', { type: 'arrived', stationId: 'center', atMinute: 12 });
  svc.reportCarEvent('car_b', { type: 'departed', stationId: 'center', atMinute: 14 });

  const evaluation = svc.evaluateTransfer({
    toCarId: 'car_b',
    stationId: 'center',
    alightAtMinute: 13,
    fromPointId: 'pt_drop',
    toPointId: 'pt_board',
  });
  assert.equal(evaluation.feasible, false);
  assert.ok(evaluation.reasons.some((r) => r.code === 'MISSED_DEPARTURE'));
  assert.equal(evaluation.readyAtMinute, 15);

  assert.throws(
    () => svc.board('RS_241', { passengerId: 'lin', carId: 'car_b', atMinute: 14 }),
    (error) => error instanceof DomainError && error.code === 'TRANSFER_NOT_FEASIBLE',
  );
  const location = svc.getLocation('RS_241');
  const lin = location.passengers.find((p) => p.id === 'lin');
  assert.equal(lin.currentCarId, null);
  assert.ok(!lin.receipts.some((r) => r.type === 'BOARD' && r.carId === 'car_b'));
});

test('还原 10 分下车但站内步行改为 5 分钟：原 B 车仍不能销单，C 车另派后有真实上车回执', () => {
  const svc = seedWorld();
  createNeighborTicket(svc);
  const walk = Object.values(svc.state.walkPaths)[0];
  svc.updateWalkPath(walk.id, { minutes: 5 });
  reportABaseline(svc);
  svc.board('RS_241', { passengerId: 'lin', atMinute: 8 });
  svc.alight('RS_241', { passengerId: 'lin', carId: 'car_a', atMinute: 10 });
  svc.reportCarEvent('car_b', { type: 'arrived', stationId: 'center', atMinute: 12 });

  const evaluation = svc.evaluateTransfer({
    toCarId: 'car_b',
    stationId: 'center',
    alightAtMinute: 10,
    fromPointId: 'pt_drop',
    toPointId: 'pt_board',
  });
  assert.equal(evaluation.readyAtMinute, 15);
  assert.equal(evaluation.feasible, false);

  assert.throws(
    () => svc.board('RS_241', { passengerId: 'lin', carId: 'car_b', atMinute: 12 }),
    (error) => error instanceof DomainError && error.code === 'TRANSFER_NOT_FEASIBLE',
  );

  const beforeDelivery = (() => {
    const fresh = seedWorld();
    createNeighborTicket(fresh, 'RS_EARLY');
    assert.throws(
      () => fresh.createOnDemandDispatch('RS_EARLY', {}),
      (error) => error.code === 'FIRST_LEG_NOT_DELIVERED',
    );
  })();
  assert.equal(beforeDelivery, undefined);

  const dispatch = svc.createOnDemandDispatch('RS_241', { passengerIds: ['lin'] });
  assert.equal(dispatch.status, 'OFFERED');
  assert.deepEqual(dispatch.candidates.map((c) => c.carId), ['car_c']);
  const accepted = svc.acceptDispatch(dispatch.id, { carId: 'car_c', driverId: 'drv_zhao' });
  assert.equal(accepted.duplicate, false);
  assert.equal(svc.acceptDispatch(dispatch.id, { carId: 'car_c' }).duplicate, true);

  svc.reportCarEvent('car_c', { type: 'arrived', stationId: 'south', atMinute: 12 });
  svc.reportCarEvent('car_c', { type: 'departed', stationId: 'south', atMinute: 13 });
  svc.reportCarEvent('car_c', { type: 'arrived', stationId: 'center', atMinute: 16 });
  const receipt = svc.board('RS_241', { passengerId: 'lin', carId: 'car_c', atMinute: 16 }).receipt;
  assert.equal(receipt.type, 'BOARD');
  assert.equal(receipt.carId, 'car_c');
  assert.ok(svc.state.receipts[receipt.id]);
  const location = svc.getLocation('RS_241');
  assert.equal(location.passengers.find((p) => p.id === 'lin').currentCarId, 'car_c');
  assert.ok(!location.passengers.find((p) => p.id === 'lin').receipts.some((r) => r.type === 'BOARD' && r.carId === 'car_b'));
});

test('没有实际下第一辆车的人，不能在第二辆成功上车', () => {
  const svc = seedWorld();
  createNeighborTicket(svc);
  reportABaseline(svc);
  svc.board('RS_241', { passengerId: 'lin', atMinute: 8 });
  svc.reportCarEvent('car_b', { type: 'arrived', stationId: 'center', atMinute: 12 });

  assert.throws(
    () => svc.board('RS_241', { passengerId: 'lin', carId: 'car_b', legNo: 2, atMinute: 12 }),
    (error) => error.code === 'MUST_ALIGHT_FIRST',
  );
  assert.throws(
    () => svc.board('RS_241', { passengerId: 'lin', carId: 'car_b', legNo: 1, atMinute: 12 }),
    (error) => error.code === 'MUST_ALIGHT_FIRST',
  );
});

test('同站名但没有可验证站内走行路径，不能判定换乘可行', () => {
  const svc = new DispatchService(createInitialState());
  svc.createStation({ id: 'a_s', name: '团结站', points: [{ id: 'a_drop' }] });
  svc.createStation({ id: 'b_s', name: '团结站', points: [{ id: 'b_board' }] });
  const result = svc.evaluateTransfer({
    stationId: 'a_s',
    alightAtMinute: 10,
    fromPointId: 'a_drop',
    toPointId: 'b_board',
    secondArrivalMinute: 12,
    secondDepartureMinute: 14,
  });
  assert.equal(result.feasible, false);
  assert.equal(result.sameStationNameOnly, true);
  assert.ok(result.reasons.some((r) => r.code === 'WALK_PATH_MISSING'));
});

test('定点保接窗须两名司机确认；等待会危及顾阿姨承诺下站时刻时不能签', () => {
  const svc = seedWorld();
  createNeighborTicket(svc);
  svc.createTicket({
    code: 'RS_GU',
    passengers: [{ id: 'gu', name: '顾阿姨' }],
    legs: [{ carId: 'car_b', fromStationId: 'center', toStationId: 'old', promisedAlightByMin: 20 }],
  });
  svc.reportCarEvent('car_b', { type: 'arrived', stationId: 'center', atMinute: 12 });
  svc.board('RS_GU', { passengerId: 'gu', carId: 'car_b', atMinute: 12 });

  const feasibleWait = svc.assessWait('car_b', 'center', 15);
  assert.equal(feasibleWait.feasible, true);
  assert.equal(feasibleWait.extraWaitMinutes, 1);
  assert.equal(feasibleWait.predictedAlights[0].predictedAlightMinute, 19);

  const badWait = svc.assessWait('car_b', 'center', 18);
  assert.equal(badWait.feasible, false);
  assert.ok(badWait.reasons.some((r) => r.code === 'WAIT_ALLOWANCE_EXCEEDED'));
  assert.ok(badWait.reasons.some((r) => r.code === 'PROMISED_ALIGHT_MISSED'));

  const handoff = svc.createHandoff({
    id: 'HF_TEST',
    ticketCode: 'RS_241',
    fromCarId: 'car_a',
    toCarId: 'car_b',
    stationId: 'center',
    windowStartMin: 12,
    windowEndMin: 15,
    holdUntilMin: 15,
  });
  assert.equal(handoff.status, 'proposed');
  svc.confirmHandoff('HF_TEST', { carId: 'car_a', driverId: 'drv_wang', atMinute: 9 });
  assert.equal(svc.state.handoffs.HF_TEST.status, 'from_confirmed');
  const signed = svc.confirmHandoff('HF_TEST', { carId: 'car_b', driverId: 'drv_li', atMinute: 10 });
  assert.equal(signed.handoff.status, 'signed');
  const duplicate = svc.confirmHandoff('HF_TEST', { carId: 'car_b', driverId: 'drv_li' });
  assert.equal(duplicate.duplicate, true);
  assert.equal(svc.state.tickets.RS_241.legs[1].takeoverSigned.handoffId, 'HF_TEST');
  assert.equal(svc.state.cars.car_b.stops[0].protectedHoldUntilMin, 15);

  const impossible = svc.createHandoff({
    id: 'HF_BAD',
    fromCarId: 'car_a',
    toCarId: 'car_b',
    stationId: 'center',
    windowStartMin: 12,
    windowEndMin: 18,
    holdUntilMin: 18,
  });
  assert.throws(
    () => svc.confirmHandoff('HF_BAD', { carId: 'car_b' }),
    (error) => error.status === 422 && error.code === 'WAIT_NOT_FEASIBLE',
  );
  assert.equal(impossible.status, 'proposed');
});

test('随到接驳必须见第一程真实送达；兜底只派有行程和工作时段的真车', () => {
  const svc = seedWorld();
  createNeighborTicket(svc, 'RS_FB');
  reportABaseline(svc);
  svc.board('RS_FB', { passengerId: 'lin', atMinute: 8 });
  svc.alight('RS_FB', { passengerId: 'lin', carId: 'car_a', atMinute: 10 });

  const dispatch = svc.createFallbackDispatch('RS_FB', { passengerIds: ['lin'], readyAtMinute: 15 });
  assert.equal(dispatch.kind, 'fallback');
  assert.ok(dispatch.candidates.some((c) => c.carId === 'car_c'));
  svc.acceptDispatch(dispatch.id, { carId: 'car_c' });
  assert.equal(svc.state.dispatches[dispatch.id].status, 'ACCEPTED');

  // A virtual anywhere-anytime car is never synthesized.
  const onlyAB = seedWorld();
  delete onlyAB.state.cars.car_c;
  onlyAB.updateWalkPath('walk_center', { minutes: 5 });
  createNeighborTicket(onlyAB, 'RS_NO_BACKUP');
  reportABaseline(onlyAB);
  onlyAB.board('RS_NO_BACKUP', { passengerId: 'lin', atMinute: 8 });
  onlyAB.alight('RS_NO_BACKUP', { passengerId: 'lin', carId: 'car_a', atMinute: 10 });
  const noCar = onlyAB.createFallbackDispatch('RS_NO_BACKUP', { passengerIds: ['lin'], readyAtMinute: 15 });
  assert.equal(noCar.status, 'NO_CANDIDATE');
  assert.deepEqual(Object.keys(onlyAB.state.cars).sort(), ['car_a', 'car_b']);

  svc.state.cars.car_c.workWindow.endMin = 20;
  const outsideWindow = svc.findPickupCars({
    stationId: 'center',
    readyAtMinute: 15,
    destinationStationId: 'market',
    partySize: 1,
    carId: 'car_c',
  });
  assert.equal(outsideWindow.candidates.length, 0);
  assert.ok(outsideWindow.rejected.some((item) => item.reason === 'OUTSIDE_WORK_WINDOW'));
});

test('额度分配：真 C 车只剩两个兜底座时，优先把同伴组签上，另一单延后', () => {
  const svc = seedWorld();
  createNeighborTicket(svc);
  svc.createTicket({
    code: 'RS_C_EXISTING',
    passengers: [{ id: 'song', name: '宋师傅的熟客' }],
    legs: [{ carId: 'car_c', fromStationId: 'center', toStationId: 'market' }],
  });
  svc.createTicket({
    code: 'RS_SOLO',
    priority: 'standard',
    passengers: [{ id: 'he', name: '何邻居' }],
    legs: [{ fromStationId: 'center', toStationId: 'market' }],
    fallback: { eligible: true, stationId: 'center' },
  });

  assert.equal(svc.reservedSeats('car_c'), 1);
  const allocation = svc.allocateFallback({
    carId: 'car_c',
    asOfMinute: 15,
    requests: [
      { ticketCode: 'RS_SOLO', passengerIds: ['he'], legNo: 1 },
      { ticketCode: 'RS_241', passengerIds: ['lin', 'zhou'], legNo: 2, priority: 'high' },
    ],
  });

  assert.equal(allocation.remainingSeats, 0);
  assert.deepEqual(allocation.selected.map((item) => item.ticketCode), ['RS_241']);
  assert.equal(allocation.selected[0].allocatedCarId, 'car_c');
  assert.deepEqual(allocation.deferred.map((item) => item.ticketCode), ['RS_SOLO']);
  assert.equal(allocation.deferred[0].decision, 'fallback_seat_budget_exhausted');
});

test('途中改站后按新站查询欠段；迟到离站生成可追溯消息', () => {
  const svc = seedWorld();
  svc.createTicket({
    code: 'RS_GU2',
    passengers: [{ id: 'gu', name: '顾阿姨' }],
    legs: [{ carId: 'car_b', fromStationId: 'center', toStationId: 'old', promisedAlightByMin: 20 }],
  });
  svc.reportCarEvent('car_b', { type: 'arrived', stationId: 'center', atMinute: 12 });
  svc.board('RS_GU2', { passengerId: 'gu', atMinute: 12 });
  svc.reportCarEvent('car_b', { type: 'departed', stationId: 'center', atMinute: 15, reason: '保接邻居，多等一分钟' });

  const changed = svc.changeTicketStation('RS_GU2', {
    passengerId: 'gu',
    legNo: 1,
    newToStationId: 'market',
    atMinute: 17,
    reason: '槐树巷临时管制，旅客同意改到市集站',
  });
  assert.equal(changed.leg.toStationId, 'market');

  svc.reportCarEvent('car_b', { type: 'arrived', stationId: 'old', atMinute: 19 });
  const location = svc.getLocation('RS_GU2');
  assert.equal(location.passengers[0].currentCarId, 'car_b');
  assert.equal(location.passengers[0].owedLegs[0].toStationId, 'market');
  assert.equal(location.passengers[0].owedLegs[0].action, 'alight');

  const lateMessage = svc.state.messages.find((m) => m.type === 'driver.late_departure' && m.carId === 'car_b');
  assert.ok(lateMessage);
  assert.equal(lateMessage.minutesLate, 1);
  assert.equal(lateMessage.reason, '保接邻居，多等一分钟');
});
