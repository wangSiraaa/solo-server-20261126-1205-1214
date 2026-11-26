'use strict';

const path = require('node:path');
const { JsonStore } = require('./json-store');
const { PlatformService } = require('./service');

function offsetIso(minutes) {
  return new Date(Date.now() + minutes * 60_000).toISOString();
}

function sumPoints(items, predicate = () => true) {
  return items.filter(predicate).reduce((sum, item) => sum + item.points, 0);
}

function tryCall(fn) {
  try {
    return { ok: true, value: fn() };
  } catch (error) {
    return {
      ok: false,
      error: { status: error.status, code: error.code, message: error.message, details: error.details },
    };
  }
}

function runCoreScenario(service, config) {
  const {
    id,
    enteredRoomIds,
    doorwayOnlyRoomIds = ['R-1701-L'],
    notSeenRoomIds = [],
    signedRoomIds,
    regime = 'FIRST_PROTECTION',
    participant = { type: 'TENANT', id: 'T-1001' },
    mandate,
  } = config;

  service.createSearchOrder({ id, tenantId: 'T-1001', regime });
  service.addStoreAuthorization({
    id: `AUTH-${id}`,
    searchOrderId: id,
    tenantId: 'T-1001',
    storeId: 'S-A',
    scopes: ['VIEWING', 'NEGOTIATION'],
    roomIds: ['R-1701-E', 'R-1701-W'],
  });

  if (mandate) {
    service.addCompanionMandate(id, {
      id: mandate.id,
      tenantId: 'T-1001',
      personId: mandate.personId,
      personName: mandate.personName,
      roomIds: mandate.roomIds,
      validFrom: offsetIso(-60),
      validTo: offsetIso(24 * 60),
    });
  }

  service.createAppointment({
    id: `APT-${id}`,
    searchOrderId: id,
    listingId: 'L-1701-WHOLE',
    storeId: 'S-A',
    agentId: 'AG-A1',
    scheduledAt: offsetIso(0),
    plannedSpaceIds: ['R-1701-E', 'R-1701-W', 'R-1701-L'],
  });

  service.createProtection({
    id: `PRT-${id}`,
    searchOrderId: id,
    listingId: 'L-1701-WHOLE',
    storeId: 'S-A',
    agentId: 'AG-A1',
    roomIds: ['R-1701-E', 'R-1701-W'],
    promisedStart: offsetIso(-60),
    promisedEnd: offsetIso(180),
  });

  const visit = service.submitVisit({
    id: `VST-${id}`,
    appointmentId: `APT-${id}`,
    agentId: 'AG-A1',
    participant,
    occurredAt: offsetIso(10),
    enteredRoomIds,
    doorwayOnlyRoomIds,
    notSeenRoomIds,
  });

  const confirmer = config.confirmer || 'T-1001';
  service.confirmVisit(visit.id, { actorId: confirmer, decision: 'CONFIRMED' });

  const negotiation = service.createNegotiation({
    id: `NEG-${id}`,
    searchOrderId: id,
    storeId: 'S-A',
    agentId: 'AG-A1',
    roomIds: signedRoomIds,
    kind: 'TERM_AGREED',
    terms: { monthlyRentPoints: 1701, note: '仅接通议价记录；本演示不另设计点' },
    occurredAt: offsetIso(25),
  });
  service.confirmNegotiation(negotiation.id, 'T-1001');

  const leaseDetail = service.signLease({
    id: `LSE-${id}`,
    searchOrderId: id,
    listingId: 'L-1701-WHOLE',
    signingStoreId: 'S-B',
    signingAgentId: 'AG-B1',
    tenantActorId: 'T-1001',
    signedRoomIds,
    wholeSet: signedRoomIds.length === 2,
    signedAt: offsetIso(40),
  });

  return leaseDetail;
}

function settleWithDuplicateCheck(service, { settlementId, requestId, storeId, itemIds }) {
  const first = service.createSettlement({
    id: settlementId,
    requestId,
    storeId,
    itemIds,
  });

  const replay = service.createSettlement({ requestId, storeId, itemIds });

  const secondDifferentRequest = tryCall(() =>
    service.createSettlement({
      requestId: `${requestId}-DIFFERENT-ATTEMPT`,
      storeId,
      itemIds,
    }),
  );

  return {
    settlementId: first.settlement.id,
    totalPoints: first.settlement.totalPoints,
    storeBalanceAfter: first.storeAccount.balancePoints,
    replayReturnedSameSettlement: replay.settlement.id === first.settlement.id,
    replayMarked: replay.replayed === true,
    duplicateWithoutIdempotencyBlocked: !secondDifferentRequest.ok,
    duplicateError: secondDifferentRequest.error,
  };
}

function runOverlapScenario(service, { id, regime, settleDuplicate = false }) {
  const tenantId = 'T-3003';
  service.createSearchOrder({ id, tenantId, regime });

  // Agent A books overlap listing 2 and truly enters M.
  service.createAppointment({
    id: `APT-${id}-A`,
    searchOrderId: id,
    listingId: 'L-1808-E-SOLO',
    storeId: 'S-A',
    agentId: 'AG-A1',
    plannedSpaceIds: ['R-1808-M', 'R-1808-L'],
  });
  service.createProtection({
    id: `PRT-${id}-A-M`,
    searchOrderId: id,
    listingId: 'L-1808-E-SOLO',
    storeId: 'S-A',
    agentId: 'AG-A1',
    roomIds: ['R-1808-M'],
    promisedStart: offsetIso(-60),
    promisedEnd: offsetIso(240),
  });
  const visitA = service.submitVisit({
    id: `VST-${id}-A`,
    appointmentId: `APT-${id}-A`,
    participant: { type: 'TENANT', id: tenantId },
    enteredRoomIds: ['R-1808-M'],
    doorwayOnlyRoomIds: ['R-1808-L'],
    occurredAt: offsetIso(5),
  });
  service.confirmVisit(visitA.id, { actorId: tenantId });

  // Tenant and receiving Store C confirm exactly which room/work inherits.
  const handoff = service.createHandoff({
    id: `HND-${id}`,
    searchOrderId: id,
    fromStoreId: 'S-A',
    toStoreId: 'S-C',
    items: [
      { roomId: 'R-1808-M', workType: 'VIEWING', sourceId: visitA.id, note: '继承东屋已看事实，C 将复看' },
      { roomId: 'R-1808-S', workType: 'VIEWING', note: '由 C 新看西屋' },
      { roomId: 'R-1808-M', workType: 'NEGOTIATION' },
      { roomId: 'R-1808-S', workType: 'NEGOTIATION' },
    ],
  });
  service.confirmHandoffByTenant(handoff.id, tenantId);
  service.acceptHandoffByStore(handoff.id, { agentId: 'AG-C1' });

  // C uses overlap listing 3 for repeat M viewing.
  service.createAppointment({
    id: `APT-${id}-C-M`,
    searchOrderId: id,
    listingId: 'L-1808-RENAMED',
    storeId: 'S-C',
    agentId: 'AG-C1',
    plannedSpaceIds: ['R-1808-M', 'R-1808-L'],
  });

  // C uses overlap listing 1 for S. It plans only S + living room.
  service.createAppointment({
    id: `APT-${id}-C-S`,
    searchOrderId: id,
    listingId: 'L-1808-WHOLE',
    storeId: 'S-C',
    agentId: 'AG-C1',
    plannedSpaceIds: ['R-1808-S', 'R-1808-L'],
  });

  const mProtectionConflict = tryCall(() =>
    service.createProtection({
      id: `PRT-${id}-C-M`,
      searchOrderId: id,
      listingId: 'L-1808-RENAMED',
      storeId: 'S-C',
      agentId: 'AG-C1',
      roomIds: ['R-1808-M'],
      promisedStart: offsetIso(-30),
      promisedEnd: offsetIso(240),
    }),
  );

  service.createProtection({
    id: `PRT-${id}-C-S`,
    searchOrderId: id,
    listingId: 'L-1808-WHOLE',
    storeId: 'S-C',
    agentId: 'AG-C1',
    roomIds: ['R-1808-S'],
    promisedStart: offsetIso(-30),
    promisedEnd: offsetIso(240),
  });

  const visitCM = service.submitVisit({
    id: `VST-${id}-C-M`,
    appointmentId: `APT-${id}-C-M`,
    agentId: 'AG-C1',
    participant: { type: 'TENANT', id: tenantId },
    enteredRoomIds: ['R-1808-M'],
    doorwayOnlyRoomIds: ['R-1808-L'],
    occurredAt: offsetIso(50),
  });
  service.confirmVisit(visitCM.id, { actorId: tenantId });

  const visitCS = service.submitVisit({
    id: `VST-${id}-C-S`,
    appointmentId: `APT-${id}-C-S`,
    agentId: 'AG-C1',
    participant: { type: 'TENANT', id: tenantId },
    enteredRoomIds: ['R-1808-S'],
    doorwayOnlyRoomIds: ['R-1808-L'],
    occurredAt: offsetIso(70),
  });
  service.confirmVisit(visitCS.id, { actorId: tenantId });

  const negotiation = service.createNegotiation({
    id: `NEG-${id}-C`,
    searchOrderId: id,
    storeId: 'S-C',
    agentId: 'AG-C1',
    roomIds: ['R-1808-M', 'R-1808-S'],
    kind: 'TERM_AGREED',
    occurredAt: offsetIso(90),
  });
  service.confirmNegotiation(negotiation.id, tenantId);

  const leaseDetail = service.signLease({
    id: `LSE-${id}`,
    searchOrderId: id,
    listingId: 'L-1808-WHOLE',
    signingStoreId: 'S-B',
    signingAgentId: 'AG-B1',
    tenantActorId: tenantId,
    signedRoomIds: ['R-1808-M', 'R-1808-S'],
    wholeSet: true,
    signedAt: offsetIso(120),
  });

  const result = {
    orderId: id,
    regime,
    protectionConflictWhenCUsesThirdListingForSameRoom: mProtectionConflict.error,
    items: leaseDetail.commissionItems.map((item) => ({
      id: item.id,
      roomId: item.roomId,
      type: item.commissionType,
      storeId: item.payeeStoreId,
      points: item.points,
      basis: item.basis,
      status: item.status,
    })),
    totals: {
      A: sumPoints(leaseDetail.commissionItems, (i) => i.payeeStoreId === 'S-A'),
      B: sumPoints(leaseDetail.commissionItems, (i) => i.payeeStoreId === 'S-B'),
      C: sumPoints(leaseDetail.commissionItems, (i) => i.payeeStoreId === 'S-C'),
    },
  };

  if (settleDuplicate) {
    const aItem = leaseDetail.commissionItems.find(
      (item) => item.payeeStoreId === 'S-A' && item.commissionType === 'VIEWING',
    );
    result.duplicateSettlement = settleWithDuplicateCheck(service, {
      settlementId: `STL-${id}-A`,
      requestId: `REQ-${id}-A`,
      storeId: 'S-A',
      itemIds: [aItem.id],
    });
  }

  return result;
}

function runAllScenarios(service) {
  if (service.state.demoSummary) {
    return { ...service.state.demoSummary, replayed: true };
  }
  if (service.state.searchOrders.some((order) => order.id === 'SO-1-BOTH-VIEW-BOTH-SIGN')) {
    return {
      replayed: true,
      alreadyBootstrapped: true,
      message: '演示单号已存在；如需从头重跑，请使用 RESET_DATA=1 或 npm run demo。',
      queryExamples: {
        tenant: 'GET /tenants/T-1001/trace/SO-1-BOTH-VIEW-BOTH-SIGN',
        storeA: 'GET /stores/S-A/trace/SO-1-BOTH-VIEW-BOTH-SIGN',
      },
    };
  }

  // Opening question: only East entered, but whole set signed -> only East 4.
  const eastOnly = runCoreScenario(service, {
    id: 'SO-0-EAST-ONLY',
    enteredRoomIds: ['R-1701-E'],
    doorwayOnlyRoomIds: ['R-1701-L'],
    notSeenRoomIds: ['R-1701-W'],
    signedRoomIds: ['R-1701-E', 'R-1701-W'],
  });

  // Required case 1: both rooms viewed and both signed -> A gets 8.
  const both = runCoreScenario(service, {
    id: 'SO-1-BOTH-VIEW-BOTH-SIGN',
    enteredRoomIds: ['R-1701-E', 'R-1701-W'],
    signedRoomIds: ['R-1701-E', 'R-1701-W'],
  });
  const bothSettlement = settleWithDuplicateCheck(service, {
    settlementId: 'STL-SO-1-A-8',
    requestId: 'REQ-SO-1-A-8',
    storeId: 'S-A',
    itemIds: both.commissionItems.map((item) => item.id),
  });

  // Required case 2: West receipt becomes doorway/living-room only -> East 4.
  const doorwayWest = runCoreScenario(service, {
    id: 'SO-2-WEST-DOORWAY-ONLY',
    enteredRoomIds: ['R-1701-E'],
    doorwayOnlyRoomIds: ['R-1701-L'],
    notSeenRoomIds: ['R-1701-W'],
    signedRoomIds: ['R-1701-E', 'R-1701-W'],
  });

  // Required case 3: full viewing, lease is West only -> West 4.
  const westLeaseOnly = runCoreScenario(service, {
    id: 'SO-3-FULL-VIEW-WEST-LEASE',
    enteredRoomIds: ['R-1701-E', 'R-1701-W'],
    signedRoomIds: ['R-1701-W'],
    wholeSet: false,
  });

  // Authorized companion can view on behalf for this order.
  const proxy = runCoreScenario(service, {
    id: 'SO-4-AUTHORIZED-PROXY',
    enteredRoomIds: ['R-1701-E', 'R-1701-W'],
    signedRoomIds: ['R-1701-E', 'R-1701-W'],
    participant: { type: 'COMPANION', id: 'P-2002' },
    confirmer: 'P-2002',
    mandate: { id: 'MDT-SO-4', personId: 'P-2002', personName: '林二（同住人）' },
  });

  const overlapFirst = runOverlapScenario(service, {
    id: 'SO-5-OVERLAP-FIRST',
    regime: 'FIRST_PROTECTION',
  });
  const overlapRelay = runOverlapScenario(service, {
    id: 'SO-6-OVERLAP-RELAY',
    regime: 'RELAY_CONTRIBUTION',
    settleDuplicate: true,
  });

  const compact = (detail) => ({
    orderId: detail.lease.searchOrderId,
    leaseId: detail.lease.id,
    signedRoomIds: detail.lease.signedRoomIds,
    items: detail.commissionItems.map((item) => ({
      roomId: item.roomId,
      type: item.commissionType,
      storeId: item.payeeStoreId,
      points: item.points,
      status: item.status,
    })),
    storeAPoints: sumPoints(detail.commissionItems, (i) => i.payeeStoreId === 'S-A'),
  });

  const summary = {
    generatedAt: new Date().toISOString(),
    rule: 'Only CONFIRMED entry into a commissionable independent room earns viewing credit. Doorway-only shared spaces never count. Commission cases are unique per lease+physical room; listings pointing at one room never duplicate the pool.',
    openingEastOnlyWholeLease: compact(eastOnly),
    required: {
      case1BothViewedBothSigned: { ...compact(both), extraction: bothSettlement },
      case2WestOnlySharedLivingRoom: compact(doorwayWest),
      case3WestOnlyLease: compact(westLeaseOnly),
    },
    authorizedProxy: compact(proxy),
    overlap: {
      firstProtection: overlapFirst,
      relayContribution: overlapRelay,
    },
    queryExamples: {
      tenant: 'GET /tenants/T-1001/trace/SO-1-BOTH-VIEW-BOTH-SIGN',
      storeA: 'GET /stores/S-A/trace/SO-1-BOTH-VIEW-BOTH-SIGN',
      byLeaseId: 'GET /trace/LSE-SO-1-BOTH-VIEW-BOTH-SIGN?as=store&actorId=S-B',
      settlement: 'GET /settlements/STL-SO-1-A-8',
    },
  };

  service.state.demoSummary = summary;
  service.commit();
  return summary;
}

if (require.main === module) {
  const dataFile = process.env.DATA_FILE
    ? path.resolve(process.env.DATA_FILE)
    : path.join(__dirname, '..', 'data', 'platform.json');
  const store = new JsonStore(dataFile, require('./seed').seedState);
  const service = new PlatformService(store);
  const summary = runAllScenarios(service);
  console.log(JSON.stringify(summary, null, 2));
}

module.exports = { runAllScenarios, runCoreScenario, runOverlapScenario, sumPoints };
