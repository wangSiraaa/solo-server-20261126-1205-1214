'use strict';

function seedState() {
  const now = '2026-11-26T09:00:00.000Z';

  return {
    schemaVersion: 1,
    seededAt: now,
    counters: {},

    accounts: [
      {
        id: 'ACCT-PLATFORM-CLEARING',
        type: 'PLATFORM_CLEARING',
        ownerId: 'PLATFORM',
        name: '平台佣金清算（本地模拟）',
        balancePoints: 100000,
      },
      { id: 'ACCT-T-1001', type: 'TENANT', ownerId: 'T-1001', name: '租客 林一', balancePoints: 0 },
      { id: 'ACCT-T-3003', type: 'TENANT', ownerId: 'T-3003', name: '租客 周三', balancePoints: 0 },
      { id: 'ACCT-S-A', type: 'STORE', ownerId: 'S-A', name: '甲店模拟账户', balancePoints: 0 },
      { id: 'ACCT-S-B', type: 'STORE', ownerId: 'S-B', name: '乙店模拟账户', balancePoints: 0 },
      { id: 'ACCT-S-C', type: 'STORE', ownerId: 'S-C', name: '丙店模拟账户', balancePoints: 0 },
    ],

    tenants: [
      { id: 'T-1001', name: '林一', phoneExt: '1001', accountId: 'ACCT-T-1001' },
      { id: 'T-3003', name: '周三', phoneExt: '3003', accountId: 'ACCT-T-3003' },
    ],

    companionProfiles: [
      { id: 'P-2002', name: '林二（同住人）', phoneExt: '2002' },
      { id: 'P-2003', name: '林三（同住人）', phoneExt: '2003' },
    ],

    stores: [
      {
        id: 'S-A',
        name: '甲店',
        businessModel: 'FIRST_PROTECTION_AND_RELAY',
        accountId: 'ACCT-S-A',
        clerkIds: ['CLERK-A'],
      },
      {
        id: 'S-B',
        name: '乙店',
        businessModel: 'SIGNING_ONLY',
        accountId: 'ACCT-S-B',
        clerkIds: ['CLERK-B'],
      },
      {
        id: 'S-C',
        name: '丙店',
        businessModel: 'RELAY',
        accountId: 'ACCT-S-C',
        clerkIds: ['CLERK-C'],
      },
    ],

    agents: [
      { id: 'AG-A1', storeId: 'S-A', name: '甲经纪' },
      { id: 'AG-A2', storeId: 'S-A', name: '甲店第二经纪' },
      { id: 'AG-B1', storeId: 'S-B', name: '乙经纪' },
      { id: 'AG-C1', storeId: 'S-C', name: '丙经纪' },
    ],

    clerks: [
      { id: 'CLERK-A', storeId: 'S-A', name: '甲店店员' },
      { id: 'CLERK-B', storeId: 'S-B', name: '乙店店员' },
      { id: 'CLERK-C', storeId: 'S-C', name: '丙店店员' },
    ],

    owners: [{ id: 'O-1701', name: '1701 业主' }, { id: 'O-1808', name: '1808 业主' }],

    // Physical spaces have stable IDs. Renaming a room changes name only.
    rooms: [
      {
        id: 'R-1701-E',
        unitId: 'U-1701',
        number: '1701-E',
        name: '1701 东屋',
        kind: 'BEDROOM',
        commissionable: true,
      },
      {
        id: 'R-1701-W',
        unitId: 'U-1701',
        number: '1701-W',
        name: '1701 西屋',
        kind: 'BEDROOM',
        commissionable: true,
      },
      {
        id: 'R-1701-L',
        unitId: 'U-1701',
        number: '1701-L',
        name: '1701 共用客厅',
        kind: 'LIVING_ROOM',
        commissionable: false,
      },

      // Three overlapping listings below all point at R-1808-M.
      {
        id: 'R-1808-M',
        unitId: 'U-1808',
        number: '1808-M',
        name: '1808 东屋',
        kind: 'BEDROOM',
        commissionable: true,
        aliases: ['1808 朝东主卧', '1808 阳光主卧'],
      },
      {
        id: 'R-1808-S',
        unitId: 'U-1808',
        number: '1808-S',
        name: '1808 西屋',
        kind: 'BEDROOM',
        commissionable: true,
      },
      {
        id: 'R-1808-L',
        unitId: 'U-1808',
        number: '1808-L',
        name: '1808 共用客厅',
        kind: 'LIVING_ROOM',
        commissionable: false,
      },
    ],

    listings: [
      {
        id: 'L-1701-WHOLE',
        ownerId: 'O-1701',
        title: '1701 相连东、西屋整套挂牌',
        kind: 'WHOLE',
        roomIds: ['R-1701-E', 'R-1701-W'],
        commonAreaIds: ['R-1701-L'],
        viewingPointsByRoom: { 'R-1701-E': 4, 'R-1701-W': 4 },
        negotiationPointsByRoom: {},
        active: true,
        createdAt: now,
      },
      {
        id: 'L-1701-E',
        ownerId: 'O-1701',
        title: '1701 东屋单独广告（同一物理房间）',
        kind: 'ROOM',
        roomIds: ['R-1701-E'],
        commonAreaIds: ['R-1701-L'],
        viewingPointsByRoom: { 'R-1701-E': 4 },
        negotiationPointsByRoom: {},
        active: true,
        createdAt: now,
      },
      {
        id: 'L-1701-W',
        ownerId: 'O-1701',
        title: '1701 西屋单独广告（同一物理房间）',
        kind: 'ROOM',
        roomIds: ['R-1701-W'],
        commonAreaIds: ['R-1701-L'],
        viewingPointsByRoom: { 'R-1701-W': 4 },
        negotiationPointsByRoom: {},
        active: true,
        createdAt: now,
      },

      // Three mutually overlapping 1808 listings. Every one touches R-1808-M.
      {
        id: 'L-1808-WHOLE',
        ownerId: 'O-1808',
        title: '重叠挂牌一：1808 东屋+西屋整套',
        kind: 'WHOLE',
        roomIds: ['R-1808-M', 'R-1808-S'],
        commonAreaIds: ['R-1808-L'],
        viewingPointsByRoom: { 'R-1808-M': 4, 'R-1808-S': 4 },
        negotiationPointsByRoom: { 'R-1808-M': 2, 'R-1808-S': 2 },
        active: true,
        createdAt: now,
      },
      {
        id: 'L-1808-E-SOLO',
        ownerId: 'O-1808',
        title: '重叠挂牌二：1808 东屋单间',
        kind: 'ROOM',
        roomIds: ['R-1808-M'],
        commonAreaIds: ['R-1808-L'],
        viewingPointsByRoom: { 'R-1808-M': 4 },
        negotiationPointsByRoom: { 'R-1808-M': 2 },
        active: true,
        createdAt: now,
      },
      {
        id: 'L-1808-RENAMED',
        ownerId: 'O-1808',
        title: '重叠挂牌三：改名“阳光主卧”，仍指向 R-1808-M',
        kind: 'ROOM',
        roomIds: ['R-1808-M'],
        commonAreaIds: ['R-1808-L'],
        viewingPointsByRoom: { 'R-1808-M': 4 },
        negotiationPointsByRoom: { 'R-1808-M': 2 },
        active: true,
        createdAt: now,
      },
    ],

    searchOrders: [],
    companionMandates: [],
    storeAuthorizations: [],
    appointments: [],
    protections: [],
    visits: [],
    contributions: [],
    negotiations: [],
    handoffs: [],
    splitAgreements: [],
    leases: [],
    commissionCases: [],
    commissionItems: [],
    settlements: [],
    ledgerEntries: [],
    idempotencyRecords: [],
  };
}

module.exports = { seedState };
