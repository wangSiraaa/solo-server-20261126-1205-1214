'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { JsonStore } = require('../src/json-store');
const { seedState } = require('../src/seed');
const { PlatformService } = require('../src/service');
const {
  runCoreScenario,
  runOverlapScenario,
  sumPoints,
} = require('../src/scenarios');
const { allocateIntegerPoints } = require('../src/utils');

function freshService() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'platform-domain-'));
  const file = path.join(dir, 'platform.json');
  const store = new JsonStore(file, seedState);
  return { service: new PlatformService(store), file, store };
}

test('allocation always preserves integer pool total', () => {
  const result = allocateIntegerPoints(4, [
    { payeeStoreId: 'S-A', weight: 1 },
    { payeeStoreId: 'S-C', weight: 1 },
    { payeeStoreId: 'S-X', weight: 1 },
  ]);
  assert.equal(sumPoints(result), 4);
  assert.deepEqual(result.map((r) => r.points), [2, 1, 1]);
});

test('opening fact: East viewed but whole set signed pays only East room', () => {
  const { service } = freshService();
  const detail = runCoreScenario(service, {
    id: 'T-EAST-ONLY',
    enteredRoomIds: ['R-1701-E'],
    doorwayOnlyRoomIds: ['R-1701-L'],
    notSeenRoomIds: ['R-1701-W'],
    signedRoomIds: ['R-1701-E', 'R-1701-W'],
  });

  assert.equal(detail.commissionItems.length, 1);
  assert.equal(detail.commissionItems[0].roomId, 'R-1701-E');
  assert.equal(detail.commissionItems[0].points, 4);
  assert.equal(detail.commissionItems[0].payeeStoreId, 'S-A');
});

test('required three point scenarios: 8, East 4, West 4', () => {
  const first = freshService();
  const d1 = runCoreScenario(first.service, {
    id: 'T1',
    enteredRoomIds: ['R-1701-E', 'R-1701-W'],
    signedRoomIds: ['R-1701-E', 'R-1701-W'],
  });
  assert.equal(sumPoints(d1.commissionItems, (i) => i.payeeStoreId === 'S-A'), 8);
  assert.equal(sumPoints(d1.commissionItems, (i) => i.payeeStoreId === 'S-B'), 0);
  assert.deepEqual(
    d1.commissionItems.map((i) => [i.roomId, i.points]).sort(),
    [
      ['R-1701-E', 4],
      ['R-1701-W', 4],
    ],
  );

  const settlement = first.service.createSettlement({
    requestId: 'REQ-T1',
    storeId: 'S-A',
    itemIds: d1.commissionItems.map((i) => i.id),
  });
  assert.equal(settlement.settlement.totalPoints, 8);
  assert.equal(settlement.storeAccount.balancePoints, 8);

  const replay = first.service.createSettlement({
    requestId: 'REQ-T1',
    storeId: 'S-A',
    itemIds: d1.commissionItems.map((i) => i.id),
  });
  assert.equal(replay.replayed, true);
  assert.equal(replay.settlement.id, settlement.settlement.id);
  assert.equal(replay.storeAccount.balancePoints, 8);

  assert.throws(
    () =>
      first.service.createSettlement({
        requestId: 'REQ-T1-NEW',
        storeId: 'S-A',
        itemIds: d1.commissionItems.map((i) => i.id),
      }),
    (error) => error.code === 'COMMISSION_ITEM_ALREADY_WITHDRAWN' && error.status === 409,
  );

  const second = freshService();
  const d2 = runCoreScenario(second.service, {
    id: 'T2',
    enteredRoomIds: ['R-1701-E'],
    doorwayOnlyRoomIds: ['R-1701-L'],
    notSeenRoomIds: ['R-1701-W'],
    signedRoomIds: ['R-1701-E', 'R-1701-W'],
  });
  assert.equal(d2.commissionItems.length, 1);
  assert.equal(d2.commissionItems[0].roomId, 'R-1701-E');
  assert.equal(sumPoints(d2.commissionItems, (i) => i.payeeStoreId === 'S-A'), 4);

  const third = freshService();
  const d3 = runCoreScenario(third.service, {
    id: 'T3',
    enteredRoomIds: ['R-1701-E', 'R-1701-W'],
    signedRoomIds: ['R-1701-W'],
  });
  assert.equal(d3.commissionItems.length, 1);
  assert.equal(d3.commissionItems[0].roomId, 'R-1701-W');
  assert.equal(sumPoints(d3.commissionItems, (i) => i.payeeStoreId === 'S-A'), 4);
});

test('only a companion mandated for this search order can view on behalf', () => {
  const { service } = freshService();
  service.createSearchOrder({ id: 'SO-MANDATE', tenantId: 'T-1001' });
  service.createAppointment({
    id: 'APT-MANDATE',
    searchOrderId: 'SO-MANDATE',
    listingId: 'L-1701-E',
    storeId: 'S-A',
    agentId: 'AG-A1',
  });

  assert.throws(
    () =>
      service.submitVisit({
        appointmentId: 'APT-MANDATE',
        participant: { type: 'COMPANION', id: 'P-9999' },
        enteredRoomIds: ['R-1701-E'],
      }),
    (error) => error.code === 'NO_VIEWING_MANDATE' && error.status === 403,
  );

  service.addCompanionMandate('SO-MANDATE', {
    tenantId: 'T-1001',
    personId: 'P-2002',
    personName: '林二',
  });

  const visit = service.submitVisit({
    id: 'VST-MANDATE',
    appointmentId: 'APT-MANDATE',
    participant: { type: 'COMPANION', id: 'P-2002' },
    enteredRoomIds: ['R-1701-E'],
    doorwayOnlyRoomIds: ['R-1701-L'],
  });
  assert.equal(visit.participant.type, 'AUTHORIZED_COMPANION');
  const confirmed = service.confirmVisit(visit.id, { actorId: 'P-2002' });
  assert.equal(confirmed.status, 'CONFIRMED');
});

test('two mandated companions can view separately and still establish both room credits', () => {
  const { service } = freshService();
  service.createSearchOrder({ id: 'SO-SPLIT-COMPANIONS', tenantId: 'T-1001' });
  service.createAppointment({
    id: 'APT-SPLIT-COMPANIONS',
    searchOrderId: 'SO-SPLIT-COMPANIONS',
    listingId: 'L-1701-WHOLE',
    storeId: 'S-A',
    agentId: 'AG-A1',
  });
  service.addCompanionMandate('SO-SPLIT-COMPANIONS', {
    tenantId: 'T-1001',
    personId: 'P-2002',
    roomIds: ['R-1701-E'],
  });
  service.addCompanionMandate('SO-SPLIT-COMPANIONS', {
    tenantId: 'T-1001',
    personId: 'P-2003',
    roomIds: ['R-1701-W'],
  });

  assert.throws(
    () =>
      service.submitVisit({
        appointmentId: 'APT-SPLIT-COMPANIONS',
        participant: { type: 'COMPANION', id: 'P-2002' },
        enteredRoomIds: ['R-1701-W'],
      }),
    (error) => error.code === 'ROOM_OUTSIDE_MANDATE',
  );

  const eastVisit = service.submitVisit({
    id: 'VST-COMP-E',
    appointmentId: 'APT-SPLIT-COMPANIONS',
    participant: { type: 'COMPANION', id: 'P-2002' },
    enteredRoomIds: ['R-1701-E'],
  });
  service.confirmVisit(eastVisit.id, { actorId: 'P-2002' });

  const westVisit = service.submitVisit({
    id: 'VST-COMP-W',
    appointmentId: 'APT-SPLIT-COMPANIONS',
    participant: { type: 'COMPANION', id: 'P-2003' },
    enteredRoomIds: ['R-1701-W'],
  });
  service.confirmVisit(westVisit.id, { actorId: 'T-1001' });

  const lease = service.signLease({
    searchOrderId: 'SO-SPLIT-COMPANIONS',
    listingId: 'L-1701-WHOLE',
    signingStoreId: 'S-B',
    signingAgentId: 'AG-B1',
    tenantActorId: 'T-1001',
    signedRoomIds: ['R-1701-E', 'R-1701-W'],
    wholeSet: true,
  });
  assert.equal(sumPoints(lease.commissionItems, (i) => i.payeeStoreId === 'S-A'), 8);
});

test('different listings pointing to one physical room cannot create duplicate protection', () => {
  const { service } = freshService();
  service.createSearchOrder({ id: 'SO-CONFLICT', tenantId: 'T-3003' });
  service.createAppointment({
    id: 'APT-CONFLICT-A',
    searchOrderId: 'SO-CONFLICT',
    listingId: 'L-1808-E-SOLO',
    storeId: 'S-A',
    agentId: 'AG-A1',
  });
  service.createProtection({
    searchOrderId: 'SO-CONFLICT',
    storeId: 'S-A',
    agentId: 'AG-A1',
    roomIds: ['R-1808-M'],
    promisedEnd: new Date(Date.now() + 3600_000).toISOString(),
  });

  service.createAppointment({
    id: 'APT-CONFLICT-C',
    searchOrderId: 'SO-CONFLICT',
    listingId: 'L-1808-RENAMED',
    storeId: 'S-C',
    agentId: 'AG-C1',
  });
  assert.throws(
    () =>
      service.createProtection({
        searchOrderId: 'SO-CONFLICT',
        storeId: 'S-C',
        agentId: 'AG-C1',
        listingId: 'L-1808-RENAMED',
        roomIds: ['R-1808-M'],
        promisedEnd: new Date(Date.now() + 3600_000).toISOString(),
      }),
    (error) => error.code === 'PROTECTION_CONFLICT' && error.status === 409,
  );
});

test('renaming a room keeps one stable identity while a second listing can point to it', () => {
  const { service } = freshService();
  const beforeRooms = service.state.rooms.length;
  const room = service.createRoom({
    id: 'R-X1',
    number: 'X-1',
    name: '旧东屋',
    kind: 'BEDROOM',
  });
  service.renameRoom(room.id, '改名为阳光主卧');
  service.createListing({
    id: 'L-X1-A',
    title: 'X1 first ad',
    roomIds: [room.id],
    viewingPointsByRoom: { [room.id]: 4 },
  });
  service.createListing({
    id: 'L-X1-B',
    title: 'X1 renamed duplicate ad',
    roomIds: [room.id],
    viewingPointsByRoom: { [room.id]: 4 },
  });

  assert.equal(service.state.rooms.length, beforeRooms + 1);
  assert.equal(service.requireRoom(room.id).name, '改名为阳光主卧');
  assert.equal(service.state.listings.filter((l) => l.roomIds.includes(room.id)).length, 2);
});

test('overlap policy: first protection gives A M and C S; relay splits M 2/2', () => {
  const first = freshService();
  const r1 = runOverlapScenario(first.service, {
    id: 'OVL-FIRST-TEST',
    regime: 'FIRST_PROTECTION',
  });
  assert.equal(r1.protectionConflictWhenCUsesThirdListingForSameRoom.code, 'PROTECTION_CONFLICT');
  assert.equal(r1.totals.A, 4); // M viewing only
  assert.equal(r1.totals.B, 0);
  assert.ok(r1.totals.C >= 4); // S viewing plus separately listed negotiation points

  const mViewing = r1.items.find((i) => i.roomId === 'R-1808-M' && i.type === 'VIEWING');
  assert.equal(mViewing.storeId, 'S-A');
  assert.equal(mViewing.points, 4);
  const sViewing = r1.items.find((i) => i.roomId === 'R-1808-S' && i.type === 'VIEWING');
  assert.equal(sViewing.storeId, 'S-C');
  assert.equal(sViewing.points, 4);

  const relay = freshService();
  const r2 = runOverlapScenario(relay.service, {
    id: 'OVL-RELAY-TEST',
    regime: 'RELAY_CONTRIBUTION',
    settleDuplicate: true,
  });
  const mViewingItems = r2.items.filter((i) => i.roomId === 'R-1808-M' && i.type === 'VIEWING');
  assert.deepEqual(
    mViewingItems.map((i) => [i.storeId, i.points]).sort(),
    [
      ['S-A', 2],
      ['S-C', 2],
    ],
  );
  assert.equal(r2.duplicateSettlement.totalPoints, 2);
  assert.equal(r2.duplicateSettlement.replayReturnedSameSettlement, true);
  assert.equal(r2.duplicateSettlement.duplicateWithoutIdempotencyBlocked, true);
});

test('unrelated store cannot query or settle money for an order', () => {
  const { service } = freshService();
  const d = runCoreScenario(service, {
    id: 'T-AUTHZ',
    enteredRoomIds: ['R-1701-E'],
    signedRoomIds: ['R-1701-E'],
  });
  assert.throws(
    () => service.traceForStore('T-AUTHZ', { storeId: 'S-C' }),
    (e) => e.code === 'STORE_NOT_CONNECTED_TO_ORDER' && e.status === 403,
  );
  assert.throws(
    () =>
      service.createSettlement({
        storeId: 'S-C',
        itemIds: d.commissionItems.map((i) => i.id),
      }),
    (e) => e.code === 'ITEM_BELONGS_TO_OTHER_STORE' && e.status === 403,
  );
});

test('JSON file can be reopened and trace/payment evidence survives restart', () => {
  const { file } = freshService();
  let store = new JsonStore(file, seedState);
  let service = new PlatformService(store);
  const d = runCoreScenario(service, {
    id: 'RESTART-DOMAIN',
    enteredRoomIds: ['R-1701-E', 'R-1701-W'],
    signedRoomIds: ['R-1701-E', 'R-1701-W'],
  });
  service.createSettlement({
    requestId: 'REQ-RESTART',
    storeId: 'S-A',
    itemIds: d.commissionItems.map((i) => i.id),
  });

  // Simulate process restart: new store/service, same file.
  store = new JsonStore(file, seedState);
  service = new PlatformService(store);
  const tenantTrace = service.traceForTenant('RESTART-DOMAIN', 'T-1001');
  assert.equal(tenantTrace.visits.length, 1);
  assert.equal(tenantTrace.leases.length, 1);
  assert.equal(tenantTrace.settlements.length, 1);
  assert.equal(tenantTrace.evidence.withdrawnPointTotal, 8);

  const storeTrace = service.traceForStore('RESTART-DOMAIN', { clerkId: 'CLERK-A' });
  assert.equal(storeTrace.myMockAccount.balancePoints, 8);
  assert.equal(storeTrace.mySettlements[0].totalPoints, 8);
});
