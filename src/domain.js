import crypto from 'node:crypto';

export class DomainError extends Error {
  constructor(status, code, message, details = {}) {
    super(message);
    this.name = 'DomainError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function assert(condition, status, code, message, details = {}) {
  if (!condition) throw new DomainError(status, code, message, details);
}

export function createInitialState() {
  return {
    version: 1,
    seq: 0,
    stations: {},
    walkPaths: {},
    cars: {},
    tickets: {},
    handoffs: {},
    dispatches: {},
    receipts: {},
    messages: [],
    allocations: [],
    idempotency: {},
  };
}

const MINUTE = 1;

export class DispatchService {
  constructor(state = createInitialState()) {
    this.state = state;
  }

  nextId(prefix) {
    this.state.seq += 1;
    return `${prefix}_${this.state.seq.toString(36)}${crypto.randomBytes(2).toString('hex')}`;
  }

  getById(collection, id, label) {
    const item = this.state[collection]?.[id];
    assert(item, 404, `${label.toUpperCase()}_NOT_FOUND`, `${label} ${id} 不存在`);
    return item;
  }

  addMessage(message) {
    const record = {
      id: this.nextId('msg'),
      at: new Date().toISOString(),
      ...message,
    };
    this.state.messages.push(record);
    if (this.state.messages.length > 500) this.state.messages.shift();
    return record;
  }

  // ---------- stations and walking paths ----------

  createStation(input = {}) {
    assert(input.name, 400, 'STATION_NAME_REQUIRED', '站名必填');
    const id = input.id || this.nextId('st');
    assert(!this.state.stations[id], 409, 'STATION_EXISTS', `车站 ${id} 已存在`);

    let points = input.points;
    if (!points || points.length === 0) points = [{ id: 'default', name: '默认站台' }];
    points = points.map((point, index) => {
      const normalized = typeof point === 'string' ? { id: point, name: point } : point;
      assert(normalized.id, 400, 'POINT_ID_REQUIRED', '站内点必须有 id');
      return { id: normalized.id, name: normalized.name || normalized.id, order: index };
    });
    const pointIds = new Set();
    for (const point of points) {
      assert(!pointIds.has(point.id), 409, 'POINT_ID_DUPLICATE', `站内点 ${point.id} 重复`);
      pointIds.add(point.id);
    }

    const station = {
      id,
      name: input.name,
      aliases: input.aliases || [],
      defaultTransferMinutes: input.defaultTransferMinutes ?? null,
      points,
      createdAtMinute: input.atMinute ?? null,
    };
    this.state.stations[id] = station;
    return station;
  }

  findPoint(pointId) {
    if (!pointId) return null;
    for (const station of Object.values(this.state.stations)) {
      const point = station.points.find((candidate) => candidate.id === pointId);
      if (point) return { station, point };
    }
    return null;
  }

  createWalkPath(input = {}) {
    assert(input.fromPointId && input.toPointId, 400, 'WALK_POINTS_REQUIRED', '必须指定下客点和上客点');
    assert(Number.isFinite(input.minutes) && input.minutes >= 0, 400, 'WALK_MINUTES_INVALID', '步行时间必须是非负数');
    const from = this.findPoint(input.fromPointId);
    const to = this.findPoint(input.toPointId);
    assert(from, 400, 'FROM_POINT_NOT_FOUND', `下客点 ${input.fromPointId} 不存在`);
    assert(to, 400, 'TO_POINT_NOT_FOUND', `上客点 ${input.toPointId} 不存在`);

    const id = input.id || this.nextId('walk');
    assert(!this.state.walkPaths[id], 409, 'WALK_PATH_EXISTS', `步行路径 ${id} 已存在`);
    const path = {
      id,
      fromPointId: input.fromPointId,
      toPointId: input.toPointId,
      minutes: input.minutes,
      accessible: input.accessible ?? true,
      note: input.note || '',
    };
    this.state.walkPaths[id] = path;
    return path;
  }

  updateWalkPath(id, patch = {}) {
    const path = this.getById('walkPaths', id, 'walk path');
    if (patch.minutes !== undefined) {
      assert(Number.isFinite(patch.minutes) && patch.minutes >= 0, 400, 'WALK_MINUTES_INVALID', '步行时间必须是非负数');
      path.minutes = patch.minutes;
    }
    if (patch.note !== undefined) path.note = patch.note;
    return path;
  }

  findWalkPath(fromPointId, toPointId) {
    if (!fromPointId || !toPointId) return null;
    if (fromPointId === toPointId) {
      return { id: 'same_point', minutes: 0, source: 'same_point', fromPointId, toPointId };
    }
    const explicit = Object.values(this.state.walkPaths).find(
      (path) => path.fromPointId === fromPointId && path.toPointId === toPointId,
    );
    if (explicit) return { ...explicit, source: 'declared_path' };

    const fromOwner = this.findPoint(fromPointId);
    const toOwner = this.findPoint(toPointId);
    if (fromOwner && toOwner && fromOwner.station.id === toOwner.station.id) {
      const minutes = fromOwner.station.defaultTransferMinutes;
      if (Number.isFinite(minutes)) {
        return {
          id: `station_default_${fromOwner.station.id}`,
          minutes,
          source: 'station_default',
          fromPointId,
          toPointId,
        };
      }
    }
    return null;
  }

  // ---------- cars and driver declarations ----------

  createCar(input = {}) {
    assert(input.id, 400, 'CAR_ID_REQUIRED', '车辆 id 必填');
    assert(!this.state.cars[input.id], 409, 'CAR_EXISTS', `车辆 ${input.id} 已存在`);
    assert(input.driver?.id && input.driver?.name, 400, 'DRIVER_REQUIRED', '司机 id 和姓名必填');
    assert(Number.isInteger(input.capacity) && input.capacity > 0, 400, 'CAPACITY_INVALID', '座位数必须是正整数');
    assert(input.workWindow?.startMin != null && input.workWindow?.endMin != null, 400, 'WORK_WINDOW_REQUIRED', '必须申报可工作时段');
    assert(input.workWindow.endMin >= input.workWindow.startMin, 400, 'WORK_WINDOW_INVALID', '收车时间不能早于出车时间');
    assert(Array.isArray(input.stops) && input.stops.length > 0, 400, 'STOPS_REQUIRED', '必须按站序申报行程');

    const stationIds = new Set();
    const stops = input.stops.map((rawStop, index) => {
      const station = this.getById('stations', rawStop.stationId, 'station');
      assert(!stationIds.has(station.id), 400, 'DUPLICATE_CAR_STATION', `车辆行程中车站 ${station.id} 重复`);
      stationIds.add(station.id);

      const pointId = rawStop.pointId || station.points[0].id;
      const point = station.points.find((candidate) => candidate.id === pointId);
      assert(point, 400, 'POINT_NOT_IN_STATION', `站台点 ${pointId} 不属于车站 ${station.id}`);
      assert(
        rawStop.plannedArrivalMin != null && rawStop.plannedDepartureMin != null,
        400,
        'STOP_TIME_REQUIRED',
        `第 ${index + 1} 站必须申报计划到达和离站分钟`,
      );
      assert(
        rawStop.plannedDepartureMin >= rawStop.plannedArrivalMin,
        400,
        'STOP_TIME_INVALID',
        `第 ${index + 1} 站离站早于到站`,
      );
      return {
        stationId: station.id,
        pointId,
        order: index,
        etaMinutes: rawStop.etaMinutes ?? rawStop.plannedArrivalMin,
        plannedArrivalMin: rawStop.plannedArrivalMin,
        plannedDepartureMin: rawStop.plannedDepartureMin,
        canWaitMinutes: rawStop.canWaitMinutes ?? 0,
        waitNote: rawStop.waitNote || '',
        protectedHoldUntilMin: null,
      };
    });

    const commitments = (input.onboardCommitments || []).map((item) => {
      const station = this.getById('stations', item.alightStationId, 'commitment station');
      assert(item.passengerId && Number.isFinite(item.latestAlightMin), 400, 'COMMITMENT_INVALID', '在车乘客承诺需要乘客、下车站和最迟时刻');
      return {
        passengerId: item.passengerId,
        ticketCode: item.ticketCode || null,
        passengerName: item.passengerName || '',
        alightStationId: station.id,
        latestAlightMin: item.latestAlightMin,
      };
    });

    const car = {
      id: input.id,
      driver: input.driver,
      capacity: input.capacity,
      workWindow: input.workWindow,
      stops,
      onboardCommitments: commitments,
      actual: {},
      holds: [],
      reroutes: [],
      createdAtMinute: input.atMinute ?? null,
    };
    this.state.cars[car.id] = car;
    return car;
  }

  getStop(carId, stationId) {
    const car = this.getById('cars', carId, 'car');
    const index = car.stops.findIndex((stop) => stop.stationId === stationId);
    assert(index >= 0, 404, 'CAR_DOES_NOT_SERVE_STATION', `${carId} 的行程不含车站 ${stationId}`);
    return { car, stop: car.stops[index], index };
  }

  reportCarEvent(carId, input = {}) {
    const car = this.getById('cars', carId, 'car');
    assert(input.stationId, 400, 'STATION_REQUIRED', '实际进出站消息必须包含 stationId');
    const { stop, index } = this.getStop(carId, input.stationId);
    const atMinute = input.atMinute;
    assert(Number.isFinite(atMinute), 400, 'MINUTE_REQUIRED', '事件必须包含整数分钟 atMinute');

    const report = (car.actual[input.stationId] ||= {
      stationId: input.stationId,
      pointId: stop.pointId,
      arrivedAtMinute: null,
      departedAtMinute: null,
      arrivalRequestId: null,
      departureRequestId: null,
      lateDeparture: false,
      holdBreach: false,
    });

    if (input.type === 'arrived' || input.event === 'arrived') {
      if (report.arrivedAtMinute != null) {
        if (report.arrivedAtMinute === atMinute) {
          return { duplicate: true, report };
        }
        throw new DomainError(409, 'ARRIVAL_ALREADY_RECORDED', '到站时刻已记录且不能覆盖', { report });
      }
      for (let i = 0; i < index; i += 1) {
        const prior = car.actual[car.stops[i].stationId];
        assert(
          prior?.departedAtMinute != null,
          409,
          'STATION_ORDER_VIOLATION',
          `必须先按站序完成前一站 ${car.stops[i].stationId} 的离站报告`,
        );
      }
      assert(atMinute >= 0, 400, 'TIME_INVALID', '分钟数不能为负');
      report.arrivedAtMinute = atMinute;
      report.arrivalRequestId = input.requestId || null;
      report.pointId = input.pointId || report.pointId;
      this.addMessage({
        type: 'driver.arrived',
        carId,
        driverId: car.driver.id,
        stationId: input.stationId,
        atMinute,
      });
      return { duplicate: false, report };
    }

    if (input.type === 'departed' || input.event === 'departed') {
      assert(report.arrivedAtMinute != null, 409, 'ARRIVAL_NOT_RECORDED', '离站前必须先报实际到站');
      if (report.departedAtMinute != null) {
        if (report.departedAtMinute === atMinute) {
          return { duplicate: true, report };
        }
        throw new DomainError(409, 'DEPARTURE_ALREADY_RECORDED', '离站时刻已记录且不能覆盖', { report });
      }
      assert(atMinute >= report.arrivedAtMinute, 400, 'TIME_INVALID', '离站不能早于到站');
      report.departedAtMinute = atMinute;
      report.departureRequestId = input.requestId || null;
      report.lateDeparture = atMinute > stop.plannedDepartureMin;
      report.holdBreach = stop.protectedHoldUntilMin != null && atMinute > stop.protectedHoldUntilMin;
      if (report.lateDeparture) {
        this.addMessage({
          type: 'driver.late_departure',
          severity: report.holdBreach ? 'warning' : 'notice',
          carId,
          driverId: car.driver.id,
          stationId: input.stationId,
          atMinute,
          plannedDepartureMin: stop.plannedDepartureMin,
          minutesLate: atMinute - stop.plannedDepartureMin,
          protectedHoldUntilMin: stop.protectedHoldUntilMin,
          holdBreach: report.holdBreach,
          reason: input.reason || input.lateReason || '司机报告离站晚点',
        });
      }
      return { duplicate: false, report };
    }

    throw new DomainError(400, 'UNKNOWN_CAR_EVENT', '车辆事件 type 必须是 arrived 或 departed');
  }

  // ---------- tickets / ride sheets ----------

  createTicket(input = {}) {
    assert(input.code, 400, 'TICKET_CODE_REQUIRED', '乘车单 code 必填');
    assert(!this.state.tickets[input.code], 409, 'TICKET_EXISTS', `乘车单 ${input.code} 已存在`);
    assert(Array.isArray(input.passengers) && input.passengers.length > 0, 400, 'PASSENGERS_REQUIRED', '至少登记一位乘客');
    assert(Array.isArray(input.legs) && input.legs.length >= 1, 400, 'LEGS_REQUIRED', '乘车单至少包含一段行程');
    assert(input.legs.length <= 2, 400, 'ONLY_ONE_TRANSFER', '本服务支持一程换一次车，最多两段');

    const passengerIds = new Set();
    const passengers = input.passengers.map((passenger) => {
      assert(passenger.id && passenger.name, 400, 'PASSENGER_INVALID', '乘客必须有 id 和姓名');
      assert(!passengerIds.has(passenger.id), 409, 'PASSENGER_DUPLICATE', `乘客 ${passenger.id} 重复`);
      passengerIds.add(passenger.id);
      return { id: passenger.id, name: passenger.name, phone: passenger.phone || null };
    });

    const legs = input.legs.map((rawLeg, index) => {
      const from = this.getById('stations', rawLeg.fromStationId, 'from station');
      const to = this.getById('stations', rawLeg.toStationId, 'to station');
      assert(from.id !== to.id, 400, 'LEG_STATIONS_SAME', '一段行程的上车站和下车站不能相同');
      let car = null;
      let carStopFrom = null;
      if (rawLeg.carId) {
        car = this.getById('cars', rawLeg.carId, 'leg car');
        carStopFrom = car.stops.find((stop) => stop.stationId === from.id);
        const carStopTo = car.stops.find((stop) => stop.stationId === to.id);
        assert(carStopFrom && carStopTo, 400, 'CAR_LEG_STATION_MISMATCH', `${car.id} 不同时经过 ${from.id} 和 ${to.id}`);
        assert(carStopFrom.order < carStopTo.order, 400, 'CAR_LEG_ORDER_INVALID', `${from.name} 必须在 ${to.name} 之前`);
      }
      return {
        legNo: index + 1,
        fromStationId: from.id,
        fromPointId: rawLeg.fromPointId || carStopFrom?.pointId || from.points[0].id,
        toStationId: to.id,
        toPointId: rawLeg.toPointId || (car ? car.stops.find((stop) => stop.stationId === to.id)?.pointId : null) || to.points[0].id,
        carId: rawLeg.carId || null,
        promisedAlightByMin: rawLeg.promisedAlightByMin ?? null,
        takeoverSigned: null,
        dispatchId: null,
        changes: [],
      };
    });

    const companionRequirements = (input.companionRequirements || []).map((rule, index) => ({
      id: rule.id || `companion_${index + 1}`,
      passengerIds: rule.passengerIds || passengers.map((p) => p.id),
      legNos: rule.legNos || [1],
      note: rule.note || '同伴要求同车走完指定路段',
    }));
    for (const rule of companionRequirements) {
      for (const pid of rule.passengerIds) assert(passengerIds.has(pid), 400, 'COMPANION_PASSENGER_INVALID', '结伴规则包含不在乘车单上的乘客');
      for (const legNo of rule.legNos) assert(legs[legNo - 1], 400, 'COMPANION_LEG_INVALID', '结伴规则引用了不存在的路段');
    }

    const passengerState = {};
    for (const passenger of passengers) {
      passengerState[passenger.id] = {
        status: 'awaiting_leg_1',
        currentLegNo: 1,
        carId: null,
        lastCarId: null,
        boardedAtMinute: null,
        alightedAtMinute: null,
        completedLegNos: [],
        receiptIds: [],
        confirmations: {},
      };
    }

    const ticket = {
      code: input.code,
      partyName: input.partyName || '',
      passengers,
      legs,
      companionRequirements,
      transfer: input.transfer || null,
      fallback: input.fallback || { eligible: false },
      priority: input.priority || 'standard',
      requestedAtMinute: input.requestedAtMinute ?? 0,
      passengerState,
      history: [],
      createdAt: new Date().toISOString(),
    };
    this.state.tickets[ticket.code] = ticket;
    ticket.history.push({ at: new Date().toISOString(), type: 'ticket.created' });
    return ticket;
  }

  requirePassenger(ticket, passengerId) {
    const passenger = ticket.passengers.find((candidate) => candidate.id === passengerId);
    assert(passenger, 404, 'PASSENGER_NOT_ON_TICKET', `乘车单 ${ticket.code} 没有乘客 ${passengerId}`);
    return passenger;
  }

  getReceipt(receiptId) {
    return this.getById('receipts', receiptId, 'receipt');
  }

  addReceipt(receipt) {
    const id = receipt.id || this.nextId('rcp');
    const record = { id, ...receipt };
    this.state.receipts[id] = record;
    return record;
  }

  evaluateTransfer(input = {}) {
    const stationId = input.stationId;
    const station = stationId ? this.state.stations[stationId] : null;
    if (stationId) assert(station, 404, 'STATION_NOT_FOUND', `车站 ${stationId} 不存在`);

    const alightAtMinute = input.alightAtMinute;
    assert(Number.isFinite(alightAtMinute), 400, 'ALIGHT_TIME_REQUIRED', '必须提供第一车实际下客分钟');
    const fromPointId = input.fromPointId;
    const toPointId = input.toPointId;
    assert(fromPointId && toPointId, 400, 'TRANSFER_POINTS_REQUIRED', '必须提供下客点和上客点');

    const walk = this.findWalkPath(fromPointId, toPointId);
    const fromPointOwner = this.findPoint(fromPointId);
    const toPointOwner = this.findPoint(toPointId);
    const sameStationName = !!fromPointOwner
      && !!toPointOwner
      && fromPointOwner.station.name === toPointOwner.station.name
      && fromPointOwner.station.id !== toPointOwner.station.id;
    const reasons = [];
    if (!walk) {
      reasons.push({
        code: 'WALK_PATH_MISSING',
        message: '两张行程表只有相同站名或未登记点，缺少可验证的下客点到上客点走行时间',
      });
    }
    const walkMinutes = walk?.minutes ?? null;
    const readyAtMinute = walk ? alightAtMinute + walk.minutes : null;

    let secondArrivalMin = input.secondArrivalMinute ?? null;
    let secondDepartureMin = input.secondDepartureMinute ?? null;
    let toCar = null;
    if (input.toCarId && stationId) {
      try {
        const result = this.getStop(input.toCarId, stationId);
        toCar = result.car;
        const actual = toCar.actual[stationId];
        secondArrivalMin = actual?.arrivedAtMinute ?? result.stop.plannedArrivalMin;
        secondDepartureMin = actual?.departedAtMinute ?? result.stop.plannedDepartureMin;
      } catch (error) {
        if (error instanceof DomainError && error.code === 'CAR_DOES_NOT_SERVE_STATION') {
          reasons.push({ code: 'SECOND_CAR_STATION_MISSING', message: '后车行程表没有经过该站' });
        } else throw error;
      }
    }

    if (readyAtMinute != null && secondDepartureMin != null && readyAtMinute > secondDepartureMin) {
      reasons.push({
        code: 'MISSED_DEPARTURE',
        message: `走行结束为第 ${readyAtMinute} 分，后车第 ${secondDepartureMin} 分已驶离`,
        readyAtMinute,
        secondDepartureMin,
      });
    }
    if (input.boardAtMinute != null && readyAtMinute != null && input.boardAtMinute < readyAtMinute) {
      reasons.push({ code: 'BOARD_BEFORE_READY', message: '乘客还未走到上客点，不能提前上车' });
    }
    if (input.boardAtMinute != null && secondDepartureMin != null && input.boardAtMinute > secondDepartureMin) {
      reasons.push({ code: 'CAR_DEPARTED', message: '上车确认晚于后车实际离站' });
    }

    return {
      feasible: reasons.length === 0,
      stationId: stationId || null,
      sameStationNameOnly: !walk && (sameStationName || !!input.sameStationName),
      alightAtMinute,
      fromPointId,
      toPointId,
      walkPathId: walk?.id || null,
      walkSource: walk?.source || null,
      walkMinutes,
      readyAtMinute,
      secondCarId: input.toCarId || null,
      secondArrivalMinute: secondArrivalMin,
      secondDepartureMinute: secondDepartureMin,
      slackMinutes: readyAtMinute != null && secondDepartureMin != null ? secondDepartureMin - readyAtMinute : null,
      reasons,
    };
  }

  // ---------- boarding / alighting ----------

  findPriorReceipt(passengerState, type, legNo, carId) {
    return passengerState.receiptIds
      .map((id) => this.state.receipts[id])
      .filter((receipt) => receipt.type === type && receipt.legNo === legNo && (!carId || receipt.carId === carId))
      .at(-1) || null;
  }

  board(ticketCode, input = {}) {
    const ticket = this.getById('tickets', ticketCode, 'ticket');
    const passengerId = input.passengerId;
    assert(passengerId, 400, 'PASSENGER_ID_REQUIRED', '必须提供 passengerId');
    this.requirePassenger(ticket, passengerId);
    const ps = ticket.passengerState[passengerId];
    const requestedLegNo = input.legNo || ps.currentLegNo || 1;
    const leg = ticket.legs[requestedLegNo - 1];
    assert(leg, 404, 'LEG_NOT_FOUND', `第 ${requestedLegNo} 段不存在`);
    const carId = input.carId || leg.carId;
    assert(carId, 400, 'CAR_NOT_ASSIGNED', '该段还没有可确认的车辆');
    const car = this.getById('cars', carId, 'car');

    const priorSameBoard = this.findPriorReceipt(ps, 'BOARD', requestedLegNo, carId);
    if (priorSameBoard) {
      return { duplicate: true, receipt: priorSameBoard, ticket, passengerState: ps };
    }
    if (ps.completedLegNos.includes(requestedLegNo)) {
      throw new DomainError(409, 'LEG_ALREADY_COMPLETED', '该段已有真实下车回执，不能换一辆车重复上车销单', {
        completedLegNo: requestedLegNo,
      });
    }

    if (ps.status === 'on_board' && ps.carId && ps.carId !== carId) {
      throw new DomainError(409, 'MUST_ALIGHT_FIRST', '乘客仍在另一辆车上，未实际下车不能在新车上车', {
        currentCarId: ps.carId,
      });
    }

    if (leg.carId && leg.carId !== carId) {
      throw new DomainError(409, 'LEG_CAR_MISMATCH', '乘车单该段已签给另一辆车', { signedCarId: leg.carId });
    }

    if (requestedLegNo > 1) {
      const priorLeg = ticket.legs[requestedLegNo - 2];
      const priorAlight = this.findPriorReceipt(ps, 'ALIGHT', requestedLegNo - 1, priorLeg.carId);
      if (!priorAlight) {
        throw new DomainError(409, 'PREVIOUS_LEG_NOT_DELIVERED', '上一程没有真实下车回执，不能签下一程上车', {
          previousLegNo: requestedLegNo - 1,
        });
      }
      const boardPointId = input.pointId || leg.fromPointId || this.getStop(carId, leg.fromStationId).stop.pointId;
      const evaluation = this.evaluateTransfer({
        stationId: priorLeg.toStationId,
        alightAtMinute: priorAlight.atMinute,
        fromPointId: priorAlight.pointId || priorLeg.toPointId,
        toPointId: boardPointId,
        toCarId: carId,
        boardAtMinute: input.atMinute,
      });
      if (!evaluation.feasible) {
        throw new DomainError(422, 'TRANSFER_NOT_FEASIBLE', '站内换乘时刻或走行时间不成立，后车不能实际接手', evaluation);
      }
    }

    const { stop: fromStop } = this.getStop(carId, leg.fromStationId);
    if (input.stationId) assert(input.stationId === leg.fromStationId, 400, 'BOARD_STATION_MISMATCH', '上车站与乘车单不一致');
    const atMinute = input.atMinute;
    assert(Number.isFinite(atMinute), 400, 'MINUTE_REQUIRED', '上车确认必须包含 atMinute');

    const actual = car.actual[leg.fromStationId];
    assert(actual?.arrivedAtMinute != null, 409, 'CAR_NOT_ACTUALLY_AT_STATION', '司机尚未报实际到站，不能确认上车');
    assert(atMinute >= actual.arrivedAtMinute, 409, 'BOARD_BEFORE_ARRIVAL', '上车时间早于车辆实际到站');
    if (actual.departedAtMinute != null) {
      assert(atMinute <= actual.departedAtMinute, 409, 'CAR_DEPARTED', '车辆已实际离站，不能补签上车', {
        departedAtMinute: actual.departedAtMinute,
      });
    }

    const onboardCount = Object.values(this.state.receipts).filter(
      (receipt) =>
        receipt.type === 'BOARD' &&
        receipt.carId === carId &&
        !Object.values(this.state.receipts).some(
          (other) => other.type === 'ALIGHT' && other.passengerId === receipt.passengerId && other.legNo === receipt.legNo && other.ticketCode === receipt.ticketCode,
        ),
    ).length;
    assert(onboardCount < car.capacity, 409, 'CAR_FULL', `车辆 ${carId} 已无空位`);

    for (const rule of ticket.companionRequirements) {
      if (!rule.legNos.includes(requestedLegNo)) continue;
      for (const otherPid of rule.passengerIds) {
        if (otherPid === passengerId) continue;
        const otherOnOtherCar = ticket.passengerState[otherPid]?.status === 'on_board' && ticket.passengerState[otherPid].carId !== carId;
        assert(!otherOnOtherCar, 409, 'COMPANION_SPLIT', '同伴要求同车走完该段，但另一位已上了另一辆车', {
          companionPassengerId: otherPid,
          otherCarId: ticket.passengerState[otherPid]?.carId,
        });
      }
    }

    if (!leg.carId) leg.carId = carId;
    const receipt = this.addReceipt({
      type: 'BOARD',
      ticketCode,
      passengerId,
      passengerName: ticket.passengers.find((p) => p.id === passengerId).name,
      legNo: requestedLegNo,
      carId,
      driverId: car.driver.id,
      stationId: leg.fromStationId,
      pointId: input.pointId || fromStop.pointId,
      atMinute,
      requestId: input.requestId || null,
    });
    ps.receiptIds.push(receipt.id);
    ps.status = 'on_board';
    ps.carId = carId;
    ps.boardedAtMinute = atMinute;
    ps.currentLegNo = requestedLegNo;
    ticket.history.push({ at: new Date().toISOString(), type: 'passenger.board', receiptId: receipt.id, passengerId, carId });
    return { duplicate: false, receipt, ticket, passengerState: ps };
  }

  alight(ticketCode, input = {}) {
    const ticket = this.getById('tickets', ticketCode, 'ticket');
    const passengerId = input.passengerId;
    assert(passengerId, 400, 'PASSENGER_ID_REQUIRED', '必须提供 passengerId');
    this.requirePassenger(ticket, passengerId);
    const ps = ticket.passengerState[passengerId];
    const legNo = input.legNo || ps.currentLegNo;
    const leg = ticket.legs[legNo - 1];
    assert(leg, 404, 'LEG_NOT_FOUND', `第 ${legNo} 段不存在`);
    const carId = input.carId || ps.carId || leg.carId;
    assert(carId, 400, 'CAR_REQUIRED', '下车确认必须说明车辆');

    const priorAlight = this.findPriorReceipt(ps, 'ALIGHT', legNo, carId);
    if (priorAlight) return { duplicate: true, receipt: priorAlight, ticket, passengerState: ps };

    assert(ps.status === 'on_board' && ps.carId === carId, 409, 'NOT_ON_BOARD', '该乘客当前不在这辆车上，不能签下车', {
      status: ps.status,
      currentCarId: ps.carId,
    });
    assert(ps.currentLegNo === legNo, 409, 'LEG_MISMATCH', '下车路段与当前乘车间段不一致');
    assert(!input.stationId || input.stationId === leg.toStationId, 400, 'ALIGHT_STATION_MISMATCH', '下车站与乘车单不一致');

    const car = this.getById('cars', carId, 'car');
    const actual = car.actual[leg.toStationId];
    const atMinute = input.atMinute;
    assert(Number.isFinite(atMinute), 400, 'MINUTE_REQUIRED', '下车确认必须包含 atMinute');
    assert(actual?.arrivedAtMinute != null, 409, 'CAR_NOT_ACTUALLY_AT_STATION', '司机尚未报该站实际到站');
    assert(atMinute >= actual.arrivedAtMinute, 409, 'ALIGHT_BEFORE_ARRIVAL', '下车时间早于实际到站');
    if (actual.departedAtMinute != null) {
      assert(atMinute <= actual.departedAtMinute, 409, 'CAR_DEPARTED', '车辆已离站，不能在该站补签下车');
    }

    const toStop = car.stops.find((stop) => stop.stationId === leg.toStationId);
    const receipt = this.addReceipt({
      type: 'ALIGHT',
      ticketCode,
      passengerId,
      passengerName: ticket.passengers.find((p) => p.id === passengerId).name,
      legNo,
      carId,
      driverId: car.driver.id,
      stationId: leg.toStationId,
      pointId: leg.toPointId || toStop?.pointId,
      atMinute,
      requestId: input.requestId || null,
    });
    ps.receiptIds.push(receipt.id);
    if (!ps.completedLegNos.includes(legNo)) ps.completedLegNos.push(legNo);
    ps.lastCarId = carId;
    ps.carId = null;
    ps.alightedAtMinute = atMinute;
    if (ticket.legs[legNo]) {
      ps.status = 'alighted_waiting_next';
      ps.currentLegNo = legNo + 1;
    } else {
      ps.status = 'completed';
      ps.currentLegNo = legNo;
    }
    ticket.history.push({ at: new Date().toISOString(), type: 'passenger.alight', receiptId: receipt.id, passengerId, carId });

    if (legNo === 1 && ticket.transfer?.mode === 'protected') {
      const handoff = Object.values(this.state.handoffs).find(
        (candidate) => candidate.ticketCode === ticketCode && candidate.fromCarId === carId && candidate.status === 'signed',
      );
      if (handoff) {
        handoff.firstDeliveredAtMinute = Math.max(handoff.firstDeliveredAtMinute ?? -Infinity, atMinute);
        handoff.actualFirstAlightMinute = atMinute;
      }
    }
    return { duplicate: false, receipt, ticket, passengerState: ps };
  }

  changeTicketStation(ticketCode, input = {}) {
    const ticket = this.getById('tickets', ticketCode, 'ticket');
    const legNo = input.legNo || ticket.passengerState[input.passengerId]?.currentLegNo || ticket.legs.length;
    const leg = ticket.legs[legNo - 1];
    assert(leg, 404, 'LEG_NOT_FOUND', `第 ${legNo} 段不存在`);
    assert(input.newToStationId, 400, 'NEW_STATION_REQUIRED', '必须给出新的下车站');
    const newStation = this.getById('stations', input.newToStationId, 'new station');
    assert(newStation.id !== leg.toStationId, 409, 'STATION_UNCHANGED', '新站与当前下车站相同');

    const passengerId = input.passengerId;
    if (passengerId) this.requirePassenger(ticket, passengerId);
    const ps = passengerId ? ticket.passengerState[passengerId] : null;
    const carId = ps?.carId || leg.carId;
    assert(carId, 409, 'CAR_NOT_ASSIGNED', '改站前该段还没有车辆');
    const car = this.getById('cars', carId, 'car');
    const fromIndex = car.stops.findIndex((stop) => stop.stationId === leg.fromStationId);
    const newIndex = car.stops.findIndex((stop) => stop.stationId === newStation.id);
    assert(newIndex >= 0, 422, 'CAR_DOES_NOT_SERVE_NEW_STATION', `车辆 ${carId} 改站后的行程不经过 ${newStation.id}`);
    assert(newIndex > fromIndex, 422, 'NEW_STATION_ORDER_INVALID', '新下车站必须位于上车站之后');

    const oldActual = car.actual[leg.toStationId];
    const atMinute = input.atMinute;
    assert(Number.isFinite(atMinute), 400, 'MINUTE_REQUIRED', '改站请求必须包含 atMinute');
    if (oldActual?.arrivedAtMinute != null) {
      assert(atMinute < oldActual.arrivedAtMinute, 409, 'STOP_ALREADY_REACHED', '原下车站已经实际到达，不能途中改站');
    }
    const newActual = car.actual[newStation.id];
    assert(!newActual?.arrivedAtMinute || atMinute < newActual.arrivedAtMinute, 409, 'NEW_STOP_ALREADY_REACHED', '新站已经到达，不能再改到该站');

    const oldStationId = leg.toStationId;
    const oldPointId = leg.toPointId;
    leg.changes.push({
      atMinute,
      oldStationId,
      oldPointId,
      newStationId: newStation.id,
      newPointId: input.newToPointId || car.stops[newIndex].pointId,
      reason: input.reason || '旅客途中申请改站',
      requestId: input.requestId || null,
    });
    leg.toStationId = newStation.id;
    leg.toPointId = input.newToPointId || car.stops[newIndex].pointId;
    ticket.history.push({ at: new Date().toISOString(), type: 'ticket.station_changed', legNo, oldStationId, newStationId: newStation.id });
    return { ticket, leg, change: leg.changes.at(-1) };
  }

  getLocation(ticketCode) {
    const ticket = this.getById('tickets', ticketCode, 'ticket');
    const passengers = ticket.passengers.map((passenger) => {
      const ps = ticket.passengerState[passenger.id];
      const owedLegs = ticket.legs
        .filter((leg) => !ps.completedLegNos.includes(leg.legNo))
        .map((leg) => {
          const action = ps.status === 'on_board' && ps.currentLegNo === leg.legNo ? 'alight' : 'board';
          return {
            legNo: leg.legNo,
            fromStationId: leg.fromStationId,
            toStationId: leg.toStationId,
            signedCarId: leg.carId,
            currentCarId: ps.carId,
            action,
            detail:
              action === 'alight'
                ? `仍在 ${ps.carId} 上，欠 ${this.stationName(leg.toStationId)} 下车回执`
                : `欠第 ${leg.legNo} 段上车：${this.stationName(leg.fromStationId)} → ${this.stationName(leg.toStationId)}`,
          };
        });
      return {
        ...passenger,
        status: ps.status,
        currentCarId: ps.carId,
        lastCarId: ps.lastCarId,
        currentLegNo: ps.currentLegNo,
        completedLegNos: ps.completedLegNos,
        owedLegs,
        receipts: ps.receiptIds.map((id) => this.state.receipts[id]),
      };
    });
    const currentCarIds = [...new Set(passengers.map((p) => p.currentCarId).filter(Boolean))];
    return {
      code: ticket.code,
      partyName: ticket.partyName,
      currentCarIds,
      allDelivered: passengers.every((p) => p.owedLegs.length === 0),
      passengers,
    };
  }

  stationName(id) {
    return this.state.stations[id]?.name || id;
  }

  // ---------- protected handoffs ----------

  assessWait(carId, stationId, requestedUntilMinute, excludeHandoffId = null) {
    const { car, stop, index } = this.getStop(carId, stationId);
    assert(Number.isFinite(requestedUntilMinute), 400, 'HOLD_UNTIL_REQUIRED', '必须给出留车到第几分钟');
    const baseDeparture = stop.plannedDepartureMin;
    const extraWaitMinutes = Math.max(0, requestedUntilMinute - baseDeparture);
    const reasons = [];
    const predictedAlights = [];

    if (extraWaitMinutes > (stop.canWaitMinutes ?? 0)) {
      reasons.push({
        code: 'WAIT_ALLOWANCE_EXCEEDED',
        message: `该站最多允许多等 ${stop.canWaitMinutes ?? 0} 分钟，申请 ${extraWaitMinutes} 分钟`,
        allowedExtraWaitMinutes: stop.canWaitMinutes ?? 0,
        requestedExtraWaitMinutes: extraWaitMinutes,
      });
    }
    if (requestedUntilMinute > car.workWindow.endMin) {
      reasons.push({ code: 'WORK_WINDOW_EXCEEDED', message: '留车超过司机申报的可工作时段' });
    }
    if (car.actual[stationId]?.departedAtMinute != null) {
      reasons.push({ code: 'CAR_ALREADY_DEPARTED', message: '车已经实际离站，不能再签留车窗口' });
    }

    for (let j = index + 1; j < car.stops.length; j += 1) {
      const later = car.stops[j];
      const predictedArrivalMinute = later.plannedArrivalMin + extraWaitMinutes;
      if (predictedArrivalMinute > car.workWindow.endMin) {
        reasons.push({ code: 'WORK_WINDOW_EXCEEDED', message: `等待会使车在第 ${later.stationId} 站超出工作时段` });
      }
    }

    const obligations = [];
    for (const commitment of car.onboardCommitments || []) {
      if (commitment.ticketCode) {
        const ticket = this.state.tickets[commitment.ticketCode];
        const ps = ticket?.passengerState?.[commitment.passengerId];
        const leg = ticket?.legs.find((item) => item.carId === carId && item.toStationId === commitment.alightStationId);
        if (ps && leg && ps.completedLegNos.includes(leg.legNo)) continue;
      }
      obligations.push({
        source: 'driver_declaration',
        passengerId: commitment.passengerId,
        ticketCode: commitment.ticketCode,
        alightStationId: commitment.alightStationId,
        latestAlightMin: commitment.latestAlightMin,
      });
    }

    for (const ticket of Object.values(this.state.tickets)) {
      for (const passenger of ticket.passengers) {
        const ps = ticket.passengerState[passenger.id];
        for (const leg of ticket.legs) {
          if (leg.carId !== carId || leg.toStationId == null) continue;
          if (ps.completedLegNos.includes(leg.legNo)) continue;
          // Wait protection is constrained by people already physically on this car.
          // Future ticketed reservations affect capacity, but not this dwell-time promise.
          if (!(ps.status === 'on_board' && ps.carId === carId)) continue;
          if (leg.promisedAlightByMin == null) continue;
          if (obligations.some((o) => o.source === 'ticket' && o.ticketCode === ticket.code && o.passengerId === passenger.id && o.legNo === leg.legNo)) continue;
          obligations.push({
            source: 'ticket',
            ticketCode: ticket.code,
            passengerId: passenger.id,
            legNo: leg.legNo,
            alightStationId: leg.toStationId,
            latestAlightMin: leg.promisedAlightByMin,
          });
        }
      }
    }

    for (const obligation of obligations) {
      const alightIndex = car.stops.findIndex((candidate) => candidate.stationId === obligation.alightStationId);
      if (alightIndex < 0 || alightIndex <= index) continue;
      const alightStop = car.stops[alightIndex];
      const predictedAlightMinute = alightStop.plannedArrivalMin + extraWaitMinutes;
      predictedAlights.push({ ...obligation, plannedAlightMinute: alightStop.plannedArrivalMin, predictedAlightMinute });
      if (predictedAlightMinute > obligation.latestAlightMin) {
        reasons.push({
          code: 'PROMISED_ALIGHT_MISSED',
          message: `留车会使 ${obligation.ticketCode || ''} ${obligation.passengerId} 预计第 ${predictedAlightMinute} 分下车，晚于承诺 ${obligation.latestAlightMin} 分`,
          obligation,
          predictedAlightMinute,
        });
      }
    }

    return {
      feasible: reasons.length === 0,
      carId,
      stationId,
      requestedUntilMinute,
      baseDepartureMinute: baseDeparture,
      extraWaitMinutes,
      predictedAlights,
      reasons,
      excludeHandoffId: excludeHandoffId || null,
    };
  }

  createHandoff(input = {}) {
    assert(input.fromCarId && input.toCarId, 400, 'CARS_REQUIRED', '交接窗必须指定前后两车');
    assert(input.stationId, 400, 'STATION_REQUIRED', '交接窗必须指定车站');
    const from = this.getStop(input.fromCarId, input.stationId);
    const to = this.getStop(input.toCarId, input.stationId);
    assert(from.car.id !== to.car.id, 400, 'SAME_CAR_TRANSFER', '换乘必须是两辆车');
    assert(Number.isFinite(input.windowStartMin) && Number.isFinite(input.windowEndMin), 400, 'WINDOW_REQUIRED', '必须申报交接窗起止分钟');
    assert(input.windowEndMin >= input.windowStartMin, 400, 'WINDOW_INVALID', '交接窗结束不能早于开始');
    const holdUntilMin = input.holdUntilMin ?? input.windowEndMin;
    const waitAssessment = this.assessWait(input.toCarId, input.stationId, holdUntilMin);

    let transferEvaluation = null;
    if (input.ticketCode) {
      const ticket = this.getById('tickets', input.ticketCode, 'ticket');
      if (input.passengerId) this.requirePassenger(ticket, input.passengerId);
      const firstLeg = ticket.legs[0];
      const secondLeg = ticket.legs[1];
      if (secondLeg) {
        const alightAt = input.expectedAlightAtMinute ?? from.stop.plannedArrivalMin;
        transferEvaluation = this.evaluateTransfer({
          stationId: input.stationId,
          alightAtMinute: alightAt,
          fromPointId: input.fromPointId || firstLeg.toPointId || from.stop.pointId,
          toPointId: input.toPointId || secondLeg.fromPointId || to.stop.pointId,
          toCarId: input.toCarId,
        });
      }
    }

    const id = input.id || this.nextId('hf');
    assert(!this.state.handoffs[id], 409, 'HANDOFF_EXISTS', `交接窗 ${id} 已存在`);
    const handoff = {
      id,
      ticketCode: input.ticketCode || null,
      passengerIds: input.passengerIds || (input.passengerId ? [input.passengerId] : []),
      fromCarId: input.fromCarId,
      toCarId: input.toCarId,
      stationId: input.stationId,
      fromPointId: input.fromPointId || from.stop.pointId,
      toPointId: input.toPointId || to.stop.pointId,
      windowStartMin: input.windowStartMin,
      windowEndMin: input.windowEndMin,
      holdUntilMin,
      status: 'proposed',
      confirmations: [],
      waitAssessment,
      transferEvaluation,
      operatorSelected: input.operatorSelected ?? false,
      createdAt: new Date().toISOString(),
    };
    this.state.handoffs[id] = handoff;
    return handoff;
  }

  confirmHandoff(handoffId, input = {}) {
    const handoff = this.getById('handoffs', handoffId, 'handoff');
    const carId = input.carId;
    assert(carId, 400, 'CAR_ID_REQUIRED', '确认必须带 carId');
    let role;
    if (carId === handoff.fromCarId) role = 'from_driver';
    else if (carId === handoff.toCarId) role = 'to_driver';
    else throw new DomainError(403, 'NOT_PARTICIPATING_DRIVER', '只有交接窗上的两名司机可以确认');

    const car = this.state.cars[carId];
    if (input.driverId && input.driverId !== car.driver.id) {
      throw new DomainError(403, 'DRIVER_MISMATCH', '司机身份与车辆申报不一致');
    }

    const existing = handoff.confirmations.find((item) => item.role === role && item.carId === carId);
    if (existing) return { duplicate: true, handoff, confirmation: existing };

    if (role === 'to_driver') {
      const holdUntilMin = input.holdUntilMin ?? handoff.holdUntilMin;
      if (input.holdUntilMin != null && input.holdUntilMin !== handoff.holdUntilMin) {
        handoff.holdUntilMin = holdUntilMin;
        handoff.waitAssessment = this.assessWait(handoff.toCarId, handoff.stationId, holdUntilMin, handoff.id);
      }
      const assessment = this.assessWait(handoff.toCarId, handoff.stationId, holdUntilMin, handoff.id);
      handoff.waitAssessment = assessment;
      if (!assessment.feasible) {
        throw new DomainError(422, 'WAIT_NOT_FEASIBLE', '不能签下会突破等待余量或使原有乘客晚点的留车窗口', assessment);
      }
    }

    const confirmation = {
      role,
      carId,
      driverId: input.driverId || car.driver.id,
      atMinute: input.atMinute ?? null,
      requestId: input.requestId || null,
      confirmedAt: new Date().toISOString(),
    };
    handoff.confirmations.push(confirmation);

    const roles = new Set(handoff.confirmations.map((item) => item.role));
    if (roles.has('from_driver') && roles.has('to_driver')) {
      handoff.status = 'signed';
      handoff.signedAt = new Date().toISOString();
      const toStop = this.state.cars[handoff.toCarId].stops.find((stop) => stop.stationId === handoff.stationId);
      toStop.protectedHoldUntilMin = Math.max(toStop.protectedHoldUntilMin ?? -Infinity, handoff.holdUntilMin);
      this.state.cars[handoff.toCarId].holds.push({
        handoffId: handoff.id,
        stationId: handoff.stationId,
        untilMinute: handoff.holdUntilMin,
      });
      if (handoff.ticketCode) {
        const ticket = this.state.tickets[handoff.ticketCode];
        const secondLeg = ticket?.legs[1];
        if (secondLeg) {
          secondLeg.takeoverSigned = {
            handoffId: handoff.id,
            stationId: handoff.stationId,
            holdUntilMin: handoff.holdUntilMin,
            signedAt: handoff.signedAt,
          };
        }
      }
      this.addMessage({ type: 'handoff.signed', handoffId, ticketCode: handoff.ticketCode, stationId: handoff.stationId });
    } else {
      handoff.status = role === 'from_driver' ? 'from_confirmed' : 'to_confirmed';
    }
    return { duplicate: false, handoff, confirmation };
  }

  // ---------- on-demand offers and real-car fallback ----------

  findPickupCars(input = {}) {
    const stationId = input.stationId;
    assert(stationId, 400, 'STATION_REQUIRED', '找车必须给车站');
    this.getById('stations', stationId, 'station');
    const destinationStationId = input.destinationStationId;
    if (destinationStationId) this.getById('stations', destinationStationId, 'destination station');
    const partySize = input.partySize ?? 1;
    const readyAtMinute = input.readyAtMinute;
    const alightedAtMinute = input.alightedAtMinute;
    const fromPointId = input.fromPointId;
    const excludeCarId = input.excludeCarId || null;
    const onlyCarId = input.carId || null;

    const candidates = [];
    const rejected = [];
    for (const car of Object.values(this.state.cars)) {
      if (onlyCarId && car.id !== onlyCarId) continue;
      if (excludeCarId && car.id === excludeCarId) continue;
      const stopIndex = car.stops.findIndex((stop) => stop.stationId === stationId);
      if (stopIndex < 0) {
        rejected.push({ carId: car.id, reason: 'STATION_NOT_ON_ITINERARY' });
        continue;
      }
      const stop = car.stops[stopIndex];
      const destIndex = destinationStationId ? car.stops.findIndex((s) => s.stationId === destinationStationId) : stopIndex + 1;
      if (destinationStationId && (destIndex < 0 || destIndex <= stopIndex)) {
        rejected.push({ carId: car.id, reason: 'DESTINATION_AFTER_PICKUP_MISSING' });
        continue;
      }

      const actual = car.actual[stationId];
      const arrival = actual?.arrivedAtMinute ?? stop.plannedArrivalMin;
      const departure = actual?.departedAtMinute ?? stop.plannedDepartureMin;
      let earliestBoard = readyAtMinute;
      let walk = null;
      if (!Number.isFinite(earliestBoard)) {
        if (!Number.isFinite(alightedAtMinute) || !fromPointId) {
          rejected.push({ carId: car.id, reason: 'READY_TIME_OR_ALIGHT_RECEIPT_REQUIRED' });
          continue;
        }
        walk = this.findWalkPath(fromPointId, stop.pointId);
        if (!walk) {
          rejected.push({ carId: car.id, reason: 'WALK_PATH_MISSING' });
          continue;
        }
        earliestBoard = alightedAtMinute + walk.minutes;
      }
      earliestBoard = Math.max(earliestBoard, arrival);
      if (earliestBoard > departure) {
        rejected.push({ carId: car.id, reason: 'MISSED_DEPARTURE', earliestBoardMinute: earliestBoard, departureMinute: departure });
        continue;
      }
      if (earliestBoard < car.workWindow.startMin || (destinationStationId ? car.stops[destIndex].plannedArrivalMin : arrival) > car.workWindow.endMin) {
        rejected.push({ carId: car.id, reason: 'OUTSIDE_WORK_WINDOW' });
        continue;
      }

      const reservedSeats = this.reservedSeats(car.id);
      const availableSeats = Math.max(0, car.capacity - reservedSeats);
      if (availableSeats < partySize) {
        rejected.push({ carId: car.id, reason: 'INSUFFICIENT_SEATS', availableSeats, partySize });
        continue;
      }

      candidates.push({
        carId: car.id,
        driverId: car.driver.id,
        pickupPointId: stop.pointId,
        arrivalMinute: arrival,
        departureMinute: departure,
        earliestBoardMinute: earliestBoard,
        walkMinutes: walk?.minutes ?? null,
        destinationArrivalMinute: destinationStationId ? car.stops[destIndex].plannedArrivalMin : null,
        availableSeats,
        score: earliestBoard * 100 + (destinationStationId ? car.stops[destIndex].plannedArrivalMin : 0),
      });
    }
    candidates.sort((a, b) => a.score - b.score || a.carId.localeCompare(b.carId));
    return { candidates, rejected };
  }

  reservedSeats(carId) {
    const seats = new Set();
    for (const ticket of Object.values(this.state.tickets)) {
      for (const passenger of ticket.passengers) {
        const ps = ticket.passengerState[passenger.id];
        if (ps.status === 'on_board' && ps.carId === carId) {
          seats.add(`${ticket.code}:${passenger.id}`);
          continue;
        }
        for (const leg of ticket.legs) {
          if (leg.carId !== carId) continue;
          if (ps.completedLegNos.includes(leg.legNo)) continue;
          seats.add(`${ticket.code}:${passenger.id}`);
        }
      }
    }
    return seats.size;
  }

  verifyDeliveredLeg(ticket, passengerIds, legNo) {
    const delivered = [];
    for (const passengerId of passengerIds) {
      this.requirePassenger(ticket, passengerId);
      const ps = ticket.passengerState[passengerId];
      const receipt = this.findPriorReceipt(ps, 'ALIGHT', legNo, ticket.legs[legNo - 1].carId);
      assert(receipt, 409, 'FIRST_LEG_NOT_DELIVERED', '第一段没有真实下车回执，不能先发随到接驳约', { passengerId });
      delivered.push({ passengerId, receipt });
    }
    return delivered;
  }

  createOnDemandDispatch(ticketCode, input = {}) {
    const ticket = this.getById('tickets', ticketCode, 'ticket');
    const legNo = input.afterLegNo || 1;
    const leg = ticket.legs[legNo - 1];
    const nextLeg = ticket.legs[legNo];
    assert(leg && nextLeg, 400, 'NO_NEXT_LEG', '该乘车单没有可接驳的下一程');
    const passengerIds = input.passengerIds?.length ? input.passengerIds : ticket.passengers.map((p) => p.id);
    const delivered = this.verifyDeliveredLeg(ticket, passengerIds, legNo);
    const alightedAtMinute = Math.max(...delivered.map((item) => item.receipt.atMinute));
    const fromPointId = input.fromPointId || delivered.at(-1).receipt.pointId || leg.toPointId;
    const result = this.findPickupCars({
      stationId: leg.toStationId,
      fromPointId,
      alightedAtMinute,
      destinationStationId: nextLeg.toStationId,
      partySize: passengerIds.length,
      excludeCarId: leg.carId,
    });

    const id = input.id || this.nextId('dsp');
    const dispatch = {
      id,
      kind: 'on_demand',
      ticketCode,
      passengerIds,
      afterLegNo: legNo,
      pickupStationId: leg.toStationId,
      destinationStationId: nextLeg.toStationId,
      alightedAtMinute,
      fromPointId,
      status: result.candidates.length ? 'OFFERED' : 'NO_CANDIDATE',
      candidates: result.candidates,
      rejected: result.rejected,
      offers: result.candidates.map((candidate) => ({ carId: candidate.carId, driverId: candidate.driverId, status: 'OFFERED' })),
      selectedCarId: null,
      createdAt: new Date().toISOString(),
    };
    this.state.dispatches[id] = dispatch;
    if (!result.candidates.length && ticket.fallback?.eligible) {
      dispatch.fallbackRequired = true;
    }
    this.addMessage({ type: 'dispatch.created', dispatchId: id, ticketCode, candidates: result.candidates.map((c) => c.carId) });
    return dispatch;
  }

  createFallbackDispatch(ticketCode, input = {}) {
    const ticket = this.getById('tickets', ticketCode, 'ticket');
    assert(ticket.fallback?.eligible, 403, 'FALLBACK_NOT_ENTITLED', '该乘车单未约定原站兜底');
    const passengerIds = input.passengerIds?.length ? input.passengerIds : ticket.passengers.map((p) => p.id);
    const firstPid = passengerIds[0];
    const ps = ticket.passengerState[firstPid];
    const legNo = input.legNo || ps.currentLegNo || 1;
    const leg = ticket.legs[legNo - 1];
    assert(leg, 404, 'LEG_NOT_FOUND', '兜底路段不存在');

    const pickupStationId = ticket.fallback.stationId || leg.fromStationId;
    assert(pickupStationId === leg.fromStationId || input.allowAlternateStation, 422, 'FALLBACK_NOT_AT_ORIGINAL_STATION', '兜底车必须在票面约定的原上客站接单');
    const pickupPointId = ticket.fallback.pointId || leg.fromPointId;
    let readyAtMinute = input.readyAtMinute ?? input.atMinute ?? ps.alightedAtMinute ?? 0;

    if (legNo > 1) {
      const priorLeg = ticket.legs[legNo - 2];
      const delivered = this.verifyDeliveredLeg(ticket, passengerIds, legNo - 1);
      const lastAlightAt = Math.max(...delivered.map((item) => item.receipt.atMinute));
      const lastPointId = delivered.at(-1).receipt.pointId || priorLeg.toPointId;
      const walk = this.findWalkPath(lastPointId, pickupPointId);
      assert(walk, 422, 'FALLBACK_POINT_UNREACHABLE', '乘客当前位置到约定兜底上客点没有可验证走行路径');
      readyAtMinute = lastAlightAt + walk.minutes;
    }

    const result = this.findPickupCars({
      stationId: pickupStationId,
      fromPointId: pickupPointId,
      readyAtMinute,
      destinationStationId: leg.toStationId,
      partySize: passengerIds.length,
      carId: input.carId || ticket.fallback.carId || null,
      excludeCarId: input.excludeFailedCar ? leg.carId : null,
    });
    if (input.carId && !result.candidates.length) {
      throw new DomainError(422, 'FALLBACK_CAR_UNAVAILABLE', '指定备用车有自己的行程或工作时段，当前不能兜底', result.rejected);
    }

    const id = input.id || this.nextId('fb');
    const dispatch = {
      id,
      kind: 'fallback',
      ticketCode,
      passengerIds,
      legNo,
      pickupStationId,
      pickupPointId,
      destinationStationId: leg.toStationId,
      readyAtMinute,
      status: result.candidates.length ? 'OFFERED' : 'NO_CANDIDATE',
      candidates: result.candidates,
      rejected: result.rejected,
      offers: result.candidates.map((candidate) => ({ carId: candidate.carId, driverId: candidate.driverId, status: 'OFFERED' })),
      selectedCarId: null,
      realCarOnly: true,
      createdAt: new Date().toISOString(),
    };
    this.state.dispatches[id] = dispatch;
    this.addMessage({ type: 'fallback.created', dispatchId: id, ticketCode, candidates: result.candidates.map((c) => c.carId) });
    return dispatch;
  }

  acceptDispatch(dispatchId, input = {}) {
    const dispatch = this.getById('dispatches', dispatchId, 'dispatch');
    const carId = input.carId;
    assert(carId, 400, 'CAR_ID_REQUIRED', '接约必须给 carId');
    const offer = dispatch.offers.find((item) => item.carId === carId);
    assert(offer, 404, 'OFFER_NOT_FOUND', '没有向这辆车发约');
    const car = this.getById('cars', carId, 'car');
    if (input.driverId && input.driverId !== car.driver.id) throw new DomainError(403, 'DRIVER_MISMATCH', '司机身份不一致');

    if (dispatch.status === 'ACCEPTED' && dispatch.selectedCarId === carId) {
      return { duplicate: true, dispatch };
    }
    assert(dispatch.status === 'OFFERED', 409, 'DISPATCH_NOT_OFFERABLE', `接约单状态为 ${dispatch.status}，不能重复选择`);

    const candidate = dispatch.candidates.find((item) => item.carId === carId);
    assert(candidate, 409, 'CANDIDATE_INFEASIBLE', '该车已不再满足时刻或座位条件');
    const reserved = this.reservedSeats(carId);
    assert(car.capacity - reserved >= dispatch.passengerIds.length, 409, 'SEATS_NO_LONGER_AVAILABLE', '座位已被其他签单占用');

    offer.status = 'ACCEPTED';
    offer.acceptedAtMinute = input.atMinute ?? null;
    dispatch.offers.filter((item) => item !== offer).forEach((item) => { item.status = 'EXPIRED'; });
    dispatch.status = 'ACCEPTED';
    dispatch.selectedCarId = carId;
    dispatch.acceptedAt = new Date().toISOString();

    const ticket = this.state.tickets[dispatch.ticketCode];
    const legNo = dispatch.legNo || dispatch.afterLegNo + 1;
    const leg = ticket.legs[legNo - 1];
    assert(leg, 500, 'DISPATCH_LEG_MISSING', '接约对应的路段不存在');
    for (const passengerId of dispatch.passengerIds) {
      const ps = ticket.passengerState[passengerId];
      if (ps.status === 'on_board' && ps.carId !== carId) {
        throw new DomainError(409, 'PASSENGER_STILL_ON_BOARD', '乘客仍在前一辆车上，不能把新车写成已接手');
      }
      leg.carId = carId;
      leg.dispatchId = dispatch.id;
    }
    this.addMessage({ type: 'dispatch.accepted', dispatchId, ticketCode: ticket.code, carId });
    return { duplicate: false, dispatch };
  }

  // ---------- explicit allocation decision ----------

  allocateFallback(input = {}) {
    const carId = input.carId || null;
    const seatBudget = input.seatBudget ?? Number.POSITIVE_INFINITY;
    const requests = (input.requests || []).map((request) => {
      const ticket = this.getById('tickets', request.ticketCode, 'ticket');
      const passengerIds = request.passengerIds?.length ? request.passengerIds : ticket.passengers.map((p) => p.id);
      const ps = ticket.passengerState[passengerIds[0]];
      const legNo = request.legNo || ps.currentLegNo || 1;
      const leg = ticket.legs[legNo - 1];
      const pickupStationId = ticket.fallback?.stationId || leg.fromStationId;
      const search = this.findPickupCars({
        stationId: pickupStationId,
        readyAtMinute: input.asOfMinute ?? request.readyAtMinute ?? ps.alightedAtMinute ?? 0,
        destinationStationId: leg.toStationId,
        partySize: passengerIds.length,
        carId,
      });
      const priorityRank = { urgent: 0, high: 0, standard: 1, flexible: 2 }[request.priority || ticket.priority] ?? 1;
      return {
        ticketCode: ticket.code,
        passengerIds,
        seats: passengerIds.length,
        priorityRank,
        requestedAtMinute: ticket.requestedAtMinute,
        entitled: !!ticket.fallback?.eligible,
        companionRule: ticket.companionRequirements.some((rule) => rule.passengerIds.length > 1),
        feasibleCars: search.candidates.map((c) => c.carId),
        rejected: search.rejected,
      };
    });

    requests.sort((a, b) =>
      Number(b.entitled) - Number(a.entitled)
      || a.priorityRank - b.priorityRank
      || a.requestedAtMinute - b.requestedAtMinute
      || a.ticketCode.localeCompare(b.ticketCode),
    );

    let remainingSeats = seatBudget;
    if (carId) {
      const car = this.getById('cars', carId, 'car');
      remainingSeats = Math.min(remainingSeats, Math.max(0, car.capacity - this.reservedSeats(carId)));
    }
    const selected = [];
    const deferred = [];
    for (const request of requests) {
      if (!request.feasibleCars.length) {
        deferred.push({ ...request, decision: 'no_real_car_feasible' });
      } else if (request.seats <= remainingSeats) {
        selected.push({ ...request, decision: 'signed', allocatedCarId: carId || request.feasibleCars[0] });
        remainingSeats -= request.seats;
      } else {
        deferred.push({ ...request, decision: 'fallback_seat_budget_exhausted' });
      }
    }

    const allocatedSeats = selected.reduce((sum, item) => sum + item.seats, 0);
    const allocation = {
      id: this.nextId('alloc'),
      kind: 'fallback_seats',
      product: input.product || 'on_demand_with_original_station_fallback',
      carId,
      seatBudget: Number.isFinite(seatBudget) ? seatBudget : null,
      spentSeats: allocatedSeats,
      remainingSeats,
      selected,
      deferred,
      decidedAt: new Date().toISOString(),
    };
    this.state.allocations.push(allocation);
    return allocation;
  }
}
