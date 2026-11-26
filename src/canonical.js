import { DispatchService, createInitialState } from './domain.js';

export const stations = [
  {
    id: 'south',
    name: '社区南门',
    points: [{ id: 'pt_south', name: '南门路边上车点' }],
  },
  {
    id: 'center',
    name: '邻里中心换乘站',
    defaultTransferMinutes: 2,
    points: [
      { id: 'pt_drop', name: '东侧下客点' },
      { id: 'pt_board', name: '西侧上客点' },
    ],
  },
  {
    id: 'old',
    name: '槐树巷',
    points: [{ id: 'pt_old', name: '巷口临时站' }],
  },
  {
    id: 'market',
    name: '周末市集',
    points: [{ id: 'pt_market', name: '市集正门' }],
  },
];

export const walkPath = {
  id: 'walk_center',
  fromPointId: 'pt_drop',
  toPointId: 'pt_board',
  minutes: 2,
  note: '东下客点到西上客点的站内步行时间',
};

export const cars = [
  {
    id: 'car_a',
    driver: { id: 'drv_wang', name: '王师傅' },
    capacity: 4,
    workWindow: { startMin: 0, endMin: 60 },
    stops: [
      { stationId: 'south', pointId: 'pt_south', etaMinutes: 8, plannedArrivalMin: 8, plannedDepartureMin: 9, canWaitMinutes: 1 },
      { stationId: 'center', pointId: 'pt_drop', etaMinutes: 10, plannedArrivalMin: 10, plannedDepartureMin: 12, canWaitMinutes: 3, waitNote: '换乘站可按窗多等' },
      { stationId: 'old', pointId: 'pt_old', plannedArrivalMin: 16, plannedDepartureMin: 17 },
      { stationId: 'market', pointId: 'pt_market', plannedArrivalMin: 22, plannedDepartureMin: 23 },
    ],
  },
  {
    id: 'car_b',
    driver: { id: 'drv_li', name: '李师傅' },
    capacity: 4,
    workWindow: { startMin: 0, endMin: 60 },
    stops: [
      { stationId: 'center', pointId: 'pt_board', etaMinutes: 12, plannedArrivalMin: 12, plannedDepartureMin: 14, canWaitMinutes: 2, waitNote: '最多为保接多等 2 分钟' },
      { stationId: 'old', pointId: 'pt_old', plannedArrivalMin: 18, plannedDepartureMin: 19 },
      { stationId: 'market', pointId: 'pt_market', plannedArrivalMin: 23, plannedDepartureMin: 24 },
    ],
    onboardCommitments: [
      { passengerId: 'gu', ticketCode: 'RS_GU242', passengerName: '顾阿姨', alightStationId: 'old', latestAlightMin: 20 },
    ],
  },
  {
    id: 'car_c',
    driver: { id: 'drv_zhao', name: '赵师傅' },
    capacity: 3,
    workWindow: { startMin: 6, endMin: 40 },
    stops: [
      { stationId: 'south', pointId: 'pt_south', plannedArrivalMin: 12, plannedDepartureMin: 13 },
      { stationId: 'center', pointId: 'pt_board', plannedArrivalMin: 16, plannedDepartureMin: 18, canWaitMinutes: 1 },
      { stationId: 'old', pointId: 'pt_old', plannedArrivalMin: 22, plannedDepartureMin: 23 },
      { stationId: 'market', pointId: 'pt_market', plannedArrivalMin: 27, plannedDepartureMin: 28 },
    ],
  },
];

export const neighborTicket = {
  code: 'RS_241',
  partyName: '两位邻居',
  priority: 'high',
  requestedAtMinute: 7,
  passengers: [
    { id: 'lin', name: '林岚' },
    { id: 'zhou', name: '周屿' },
  ],
  legs: [
    { legNo: 1, carId: 'car_a', fromStationId: 'south', toStationId: 'center', promisedAlightByMin: 12 },
    { legNo: 2, carId: 'car_b', fromStationId: 'center', toStationId: 'market', promisedAlightByMin: 30 },
  ],
  companionRequirements: [
    { id: 'together_all', passengerIds: ['lin', 'zhou'], legNos: [1, 2], note: '两位邻居要求同车走完换乘两段' },
  ],
  transfer: { mode: 'protected', stationId: 'center', walkPathId: 'walk_center' },
  fallback: { eligible: true, stationId: 'center', pointId: 'pt_board', reason: '换乘失败时在票面原上客站兜底' },
};

export const guTicket = {
  code: 'RS_GU242',
  partyName: '顾阿姨',
  priority: 'urgent',
  passengers: [{ id: 'gu', name: '顾阿姨' }],
  legs: [
    { legNo: 1, carId: 'car_b', fromStationId: 'center', toStationId: 'old', promisedAlightByMin: 20 },
  ],
  fallback: { eligible: true, stationId: 'center', pointId: 'pt_board' },
};

export const cExistingTicket = {
  code: 'RS_C001',
  partyName: 'C 车已有订单',
  passengers: [{ id: 'song', name: '宋乘客' }],
  legs: [
    { legNo: 1, carId: 'car_c', fromStationId: 'south', toStationId: 'market' },
  ],
};

export const soloTicket = {
  code: 'RS_303',
  partyName: '何邻居',
  priority: 'standard',
  passengers: [{ id: 'he', name: '何邻居' }],
  legs: [
    { legNo: 1, fromStationId: 'center', toStationId: 'market' },
  ],
  fallback: { eligible: true, stationId: 'center', pointId: 'pt_board' },
};

export function seedCanonicalService() {
  const service = new DispatchService(createInitialState());
  for (const station of stations) service.createStation(station);
  service.createWalkPath(walkPath);
  for (const car of cars) service.createCar(car);
  service.createTicket(neighborTicket);
  service.createTicket(guTicket);
  service.createTicket(cExistingTicket);
  service.createTicket(soloTicket);
  return service;
}
