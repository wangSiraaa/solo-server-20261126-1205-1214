import { resetSeeded } from './seed.js';
import {
  addSweepReport,
  assessOrder,
  closeOrder,
  createComplaint,
  createShift,
  createTrip,
  dispatchMakeup,
  complaintView,
  refreshComplaint,
  returnTrip,
  setContract,
} from './core.js';

function northRight(db, orderId) {
  return db.rights.find((r) => r.orderId === orderId && r.curbId === 'CURB_31_R');
}

export function runRehearsal(db, options = {}) {
  const arrivalOrder = options.arrivalOrder === 'makeup-first' ? 'makeup-first' : 'late-first';
  resetSeeded(db);
  const out = { arrivalOrder, steps: {}, assertions: {} };

  // 1) 拐角店两面：只收到第一面的正确方向实扫时，第二面仍欠。
  const cornerShift = createShift(db, { id: 'SHIFT_CORNER', at: 10, promisedEndAt: 30, merchantIds: ['M_CORNER'] });
  const cornerOrderId = cornerShift.orderIds[0];
  const cornerTrip = createTrip(db, {
    id: 'T_CORNER',
    carId: 'CAR1',
    shiftId: cornerShift.id,
    at: 11,
    routeLinkIds: ['L12', 'L23', 'L31'],
  });
  addSweepReport(db, {
    tripId: cornerTrip.id,
    clientReportId: 'RPT_CORNER_12',
    linkId: 'L12',
    brushDownM: 0,
    brushUpM: 10,
    sweptAt: 15,
    receivedAt: 15,
  });
  const cornerMid = assessOrder(db, cornerOrderId, 16);
  out.steps.cornerSecondFaceOwedAfterFirstFace = {
    answer: cornerMid.missing.length ? '是' : '否',
    missing: cornerMid.missing,
  };

  addSweepReport(db, {
    tripId: cornerTrip.id,
    clientReportId: 'RPT_CORNER_23',
    linkId: 'L23',
    brushDownM: 0,
    brushUpM: 2,
    sweptAt: 18,
    receivedAt: 18,
  });
  out.steps.cornerClosed = closeOrder(db, { orderId: cornerOrderId, at: 20 });
  returnTrip(db, { tripId: cornerTrip.id, at: 25 });

  // 合同可把拐角第二面从拐角百货分给隔壁五金；历史当班快照不变。
  setContract(db, {
    merchantId: 'M_CORNER',
    at: 30,
    frontages: [{ curbId: 'CURB_12_R', fromM: 8, toM: 10, label: '拐角第一面' }],
  });
  setContract(db, {
    merchantId: 'M_NEIGHBOR',
    at: 31,
    frontages: [
      { curbId: 'CURB_12_R', fromM: 6, toM: 8, label: '隔壁原门面' },
      { curbId: 'CURB_23_R', fromM: 0, toM: 2, label: '分得的拐角第二面' },
    ],
  });
  out.steps.cornerSecondFaceSplitToNeighbor = true;

  // 2) 北门两米：正确方向刷足恰好两米，可按时期单。
  const happyShift = createShift(db, { id: 'SHIFT_HAPPY', at: 40, promisedEndAt: 60, merchantIds: ['M_NORTH'] });
  const happyOrderId = happyShift.orderIds[0];
  const happyTrip = createTrip(db, { id: 'T_HAPPY', carId: 'CAR1', shiftId: happyShift.id, at: 41, routeLinkIds: ['L13', 'L31'] });
  addSweepReport(db, {
    tripId: happyTrip.id,
    clientReportId: 'RPT_HAPPY',
    linkId: 'L31',
    brushDownM: 0,
    brushUpM: 2,
    sweptAt: 50,
    receivedAt: 50,
  });
  out.steps.happyClose = closeOrder(db, { orderId: happyOrderId, at: 61 });
  returnTrip(db, { tripId: happyTrip.id, at: 65 });
  out.assertions.happyClosedOnTime = db.orders.find((o) => o.id === happyOrderId).status === 'closed_on_time';

  // 3) 合同只扩一米到三米：原趟只刷 [0,2)，不能结清；先立 C_EXT。
  setContract(db, { merchantId: 'M_NORTH', at: 70, frontages: [{ curbId: 'CURB_31_R', fromM: 0, toM: 3, label: '北门扩展门面' }] });
  const extShift = createShift(db, { id: 'SHIFT_EXT', at: 71, promisedEndAt: 90, merchantIds: ['M_NORTH'] });
  const extOrderId = extShift.orderIds[0];
  const extRight = northRight(db, extOrderId);
  const extTrip = createTrip(db, { id: 'T_EXT', carId: 'CAR1', shiftId: extShift.id, at: 72, routeLinkIds: ['L31'] });
  const cExt = createComplaint(db, {
    id: 'C_EXT',
    complaintNo: 'TS-EXT',
    orderId: extOrderId,
    at: 91,
    reason: '合同扩出的第三米无实扫依据',
    claims: [{ rightId: extRight.id, fromM: 2, toM: 3 }],
  });
  returnTrip(db, { tripId: extTrip.id, at: 95 });

  // 恢复两米合同；已生成的当班权利和投诉号不注销。
  setContract(db, { merchantId: 'M_NORTH', at: 96, frontages: [{ curbId: 'CURB_31_R', fromM: 0, toM: 2, label: '北门门面恢复' }] });

  // 4) 两米合同下，刷头在第二米前抬起：实际只有 [0,1)，第二米 [1,2) 欠扫。
  const liftShift = createShift(db, { id: 'SHIFT_LIFT', at: 100, promisedEndAt: 120, merchantIds: ['M_NORTH'] });
  const liftOrderId = liftShift.orderIds[0];
  const liftRight = northRight(db, liftOrderId);
  const liftTrip = createTrip(db, { id: 'T_LIFT', carId: 'CAR1', shiftId: liftShift.id, at: 101, routeLinkIds: ['L31'] });
  const cLift = createComplaint(db, {
    id: 'C_LIFT',
    complaintNo: 'TS-LIFT',
    orderId: liftOrderId,
    at: 121,
    reason: '店主看见刷头在第二米前抬起',
    claims: [{ rightId: liftRight.id, fromM: 1, toM: 2 }],
  });
  returnTrip(db, { tripId: liftTrip.id, at: 125 });

  // 服务台决定：两份相邻欠段放进同一趟补扫，仍派 CAR1；固定原趟不改单。
  const makeup = dispatchMakeup(db, {
    id: 'T_MAKEUP',
    carId: 'CAR1',
    complaintIds: [cExt.id, cLift.id],
    at: 126,
    routeLinkIds: ['L31'],
  });
  out.steps.makeupDispatch = makeup.trip;
  const postMakeup = () =>
    addSweepReport(db, {
      tripId: makeup.trip.id,
      clientReportId: 'RPT_MAKEUP',
      linkId: 'L31',
      brushDownM: 1,
      brushUpM: 3,
      sweptAt: 127,
      receivedAt: arrivalOrder === 'makeup-first' ? 130 : 132,
    });
  returnTrip(db, { tripId: makeup.trip.id, at: 129 });

  const postExtLate = () =>
    addSweepReport(db, {
      tripId: extTrip.id,
      clientReportId: 'RPT_EXT_LATE',
      linkId: 'L31',
      brushDownM: 0,
      brushUpM: 2,
      sweptAt: 80,
      receivedAt: arrivalOrder === 'late-first' ? 130 : 131,
    });
  const postLiftLate = () =>
    addSweepReport(db, {
      tripId: liftTrip.id,
      clientReportId: 'RPT_LIFT_LATE',
      linkId: 'L31',
      brushDownM: 0,
      brushUpM: 1,
      sweptAt: 110,
      receivedAt: arrivalOrder === 'late-first' ? 131 : 132,
    });

  if (arrivalOrder === 'late-first') {
    postExtLate();
    postLiftLate();
    postMakeup();
  } else {
    postMakeup();
    postExtLate();
    postLiftLate();
  }

  const extView = complaintView(db, cExt.id);
  const liftView = complaintView(db, cLift.id);
  out.steps.extComplaint = extView;
  out.steps.liftComplaint = liftView;

  out.steps.extClose = closeOrder(db, { orderId: extOrderId, at: 140 });
  out.steps.liftClose = closeOrder(db, { orderId: liftOrderId, at: 141 });

  out.assertions.twoComplaintRightsPersisted = db.complaints.length === 2;
  out.assertions.contractRestoreDidNotDeleteRights =
    db.rights.some((r) => r.id === extRight.id && r.toM === 3) &&
    db.complaints.some((c) => c.id === 'C_LIFT' && c.claims.some((cl) => cl.fromM === 1 && cl.toM === 2));
  out.assertions.bothComplaintsResolved = extView.computedStatus === 'resolved' && liftView.computedStatus === 'resolved';
  out.assertions.makeupFilledBothMeters =
    extView.claims[0].fills.some((f) => f.tripId === 'T_MAKEUP') &&
    liftView.claims[0].fills.some((f) => f.tripId === 'T_MAKEUP');
  out.assertions.lateOriginalsKeptAsEvidence =
    db.reports.some((r) => r.clientReportId === 'RPT_EXT_LATE' && r.receivedAfterReturn) &&
    db.reports.some((r) => r.clientReportId === 'RPT_LIFT_LATE' && r.receivedAfterReturn);
  out.assertions.ordersClosedLate =
    db.orders.find((o) => o.id === extOrderId).status === 'closed_late' &&
    db.orders.find((o) => o.id === liftOrderId).status === 'closed_late';
  out.ok = Object.values(out.assertions).every(Boolean);
  out.complaintNumbers = [
    { complaintNo: 'TS-EXT', id: cExt.id },
    { complaintNo: 'TS-LIFT', id: cLift.id },
  ];
  return out;
}

export function rehearsalSummary(result) {
  return {
    ok: result.ok,
    arrivalOrder: result.arrivalOrder,
    assertions: result.assertions,
    complaintNumbers: result.complaintNumbers,
    openingAnswer: result.steps.cornerSecondFaceOwedAfterFirstFace.answer,
    makeupTripId: result.steps.makeupDispatch.id,
  };
}
