export function createDb() {
  return {
    counters: {},
    nodes: [],
    segments: [],
    curbs: [],
    links: [],
    cars: [],
    merchants: [],
    contractVersions: [],
    currentContracts: {},
    shifts: [],
    orders: [],
    rights: [],
    complaints: [],
    trips: [],
    reports: [],
    coverages: [],
    audit: [],
    meta: { seeded: false },
  };
}

function curb(id, segmentId, side, lengthM) {
  return { id, segmentId, side, lengthM };
}

function wholeLink({ id, segmentId, fromNodeId, toNodeId, direction, rightCurbId, reverseLinkId, lengthM = 10 }) {
  return {
    id,
    segmentId,
    originalSegmentId: segmentId,
    fromNodeId,
    toNodeId,
    direction,
    rightCurbId,
    reverseLinkId,
    sourceFromM: 0,
    sourceToM: lengthM,
    lengthM,
    superseded: false,
  };
}

export function buildSeededDb() {
  const db = createDb();
  db.meta.seeded = true;
  db.meta.rehearsal = 'one-car-three-junctions-four-shops';

  db.nodes = [
    { id: 'I1', name: '巷口', x: 0, y: 0, uTurnAllowed: true },
    { id: 'I2', name: '拐角', x: 10, y: 0, uTurnAllowed: false },
    { id: 'I3', name: '北门路口', x: 0, y: 10, uTurnAllowed: true },
  ];

  db.segments = [
    { id: 'SEG_12', name: '巷口—拐角段', nodeAId: 'I1', nodeBId: 'I2', lengthM: 10, refLinkId: 'L12' },
    { id: 'SEG_23', name: '拐角—北门段', nodeAId: 'I2', nodeBId: 'I3', lengthM: 10, refLinkId: 'L23' },
    { id: 'SEG_31', name: '北门—巷口段', nodeAId: 'I3', nodeBId: 'I1', lengthM: 10, refLinkId: 'L31' },
  ];

  db.curbs = [
    curb('CURB_12_R', 'SEG_12', 'ref-right', 10),
    curb('CURB_12_L', 'SEG_12', 'ref-left', 10),
    curb('CURB_23_R', 'SEG_23', 'ref-right', 10),
    curb('CURB_23_L', 'SEG_23', 'ref-left', 10),
    curb('CURB_31_R', 'SEG_31', 'ref-right', 10),
    curb('CURB_31_L', 'SEG_31', 'ref-left', 10),
  ];

  db.links = [
    wholeLink({ id: 'L12', segmentId: 'SEG_12', fromNodeId: 'I1', toNodeId: 'I2', direction: 'ref', rightCurbId: 'CURB_12_R', reverseLinkId: 'L21' }),
    wholeLink({ id: 'L21', segmentId: 'SEG_12', fromNodeId: 'I2', toNodeId: 'I1', direction: 'reverse', rightCurbId: 'CURB_12_L', reverseLinkId: 'L12' }),
    wholeLink({ id: 'L23', segmentId: 'SEG_23', fromNodeId: 'I2', toNodeId: 'I3', direction: 'ref', rightCurbId: 'CURB_23_R', reverseLinkId: 'L32' }),
    wholeLink({ id: 'L32', segmentId: 'SEG_23', fromNodeId: 'I3', toNodeId: 'I2', direction: 'reverse', rightCurbId: 'CURB_23_L', reverseLinkId: 'L23' }),
    wholeLink({ id: 'L31', segmentId: 'SEG_31', fromNodeId: 'I3', toNodeId: 'I1', direction: 'ref', rightCurbId: 'CURB_31_R', reverseLinkId: 'L13' }),
    wholeLink({ id: 'L13', segmentId: 'SEG_31', fromNodeId: 'I1', toNodeId: 'I3', direction: 'reverse', rightCurbId: 'CURB_31_L', reverseLinkId: 'L31' }),
  ];

  db.cars = [{ id: 'CAR1', name: '清扫一号', plate: '街扫·001', activeTripId: null }];

  db.merchants = [
    { id: 'M_ALLEY', name: '巷口面馆' },
    { id: 'M_NEIGHBOR', name: '隔壁五金' },
    { id: 'M_CORNER', name: '拐角百货' },
    { id: 'M_NORTH', name: '北门菜站' },
  ];

  db.currentContracts = {
    M_ALLEY: [{ curbId: 'CURB_12_R', fromM: 0, toM: 2, label: '巷口门面' }],
    M_NEIGHBOR: [{ curbId: 'CURB_12_R', fromM: 6, toM: 8, label: '隔壁门面' }],
    M_CORNER: [
      { curbId: 'CURB_12_R', fromM: 8, toM: 10, label: '拐角第一面（临巷口—拐角段）' },
      { curbId: 'CURB_23_R', fromM: 0, toM: 2, label: '拐角第二面（临拐角—北门段）' },
    ],
    M_NORTH: [{ curbId: 'CURB_31_R', fromM: 0, toM: 2, label: '北门同一段门面' }],
  };

  db.contractVersions = Object.entries(db.currentContracts).map(([merchantId, frontages], i) => ({
    id: `CV_INIT_${i + 1}`,
    merchantId,
    at: 0,
    frontages: structuredClone(frontages),
  }));

  return db;
}

export function resetSeeded(db) {
  const fresh = buildSeededDb();
  for (const key of Object.keys(db)) delete db[key];
  Object.assign(db, fresh);
  return db;
}
