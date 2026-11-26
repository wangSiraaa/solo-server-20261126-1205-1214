'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { JsonStore } = require('../src/json-store');
const { seedState } = require('../src/seed');
const { PlatformService } = require('../src/service');
const { createApp } = require('../src/app');

function startTestServer() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'platform-http-'));
  const file = path.join(dir, 'http.json');
  const store = new JsonStore(file, seedState);
  const service = new PlatformService(store);
  const server = createApp(service);

  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      resolve({
        file,
        service,
        server,
        base: `http://127.0.0.1:${port}`,
        close: () => new Promise((done) => server.close(done)),
      });
    });
  });
}

test('HTTP API exposes health, demo scenarios, role traces and idempotent extraction', async (t) => {
  const ctx = await startTestServer();
  t.after(async () => ctx.close());

  let res = await fetch(`${ctx.base}/health`);
  assert.equal(res.status, 200);
  const health = await res.json();
  assert.equal(health.ok, true);

  res = await fetch(`${ctx.base}/demo/run`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({}),
  });
  assert.equal(res.status, 201);
  const demo = await res.json();
  assert.equal(demo.required.case1BothViewedBothSigned.storeAPoints, 8);
  assert.equal(demo.required.case2WestOnlySharedLivingRoom.storeAPoints, 4);
  assert.equal(demo.required.case3WestOnlyLease.storeAPoints, 4);

  res = await fetch(`${ctx.base}/tenants/T-1001/trace/SO-1-BOTH-VIEW-BOTH-SIGN`);
  assert.equal(res.status, 200);
  const tenantTrace = await res.json();
  assert.equal(tenantTrace.searchOrder.id, 'SO-1-BOTH-VIEW-BOTH-SIGN');
  assert.equal(tenantTrace.visits[0].status, 'CONFIRMED');
  assert.equal(tenantTrace.leases[0].wholeSet, true);
  assert.equal(tenantTrace.evidence.withdrawnPointTotal, 8);

  res = await fetch(`${ctx.base}/stores/S-A/trace/SO-1-BOTH-VIEW-BOTH-SIGN`);
  assert.equal(res.status, 200);
  const storeTrace = await res.json();
  assert.equal(storeTrace.viewer.storeId, 'S-A');
  // Demo also withdraws A's 2-point relay overlap item, so the global mock
  // account balance is 8 + 2; this order's own settlement remains exactly 8.
  assert.equal(storeTrace.myMockAccount.balancePoints, 10);
  assert.equal(storeTrace.mySettlements.find((s) => s.id === 'STL-SO-1-A-8').totalPoints, 8);

  // Same idempotency key over HTTP replays the original extraction.
  const itemIds = storeTrace.myCommissionItems.map((i) => i.id).sort();
  res = await fetch(`${ctx.base}/settlements`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'idempotency-key': 'REQ-SO-1-A-8' },
    body: JSON.stringify({ storeId: 'S-A', itemIds }),
  });
  assert.equal(res.status, 200);
  const replay = await res.json();
  assert.equal(replay.replayed, true);
  assert.equal(replay.settlement.totalPoints, 8);

  // A new request trying the same already-paid items is rejected.
  res = await fetch(`${ctx.base}/settlements`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ storeId: 'S-A', itemIds, requestId: 'BRAND-NEW-KEY' }),
  });
  assert.equal(res.status, 409);
  const errorBody = await res.json();
  assert.equal(errorBody.error.code, 'COMMISSION_ITEM_ALREADY_WITHDRAWN');

  // Wrong tenant cannot read another person's order.
  res = await fetch(`${ctx.base}/tenants/T-3003/trace/SO-1-BOTH-VIEW-BOTH-SIGN`);
  assert.equal(res.status, 403);

  // Unified trace endpoint works from a lease number as well.
  res = await fetch(
    `${ctx.base}/trace/LSE-SO-3-FULL-VIEW-WEST-LEASE?as=store&actorId=S-A`,
  );
  assert.equal(res.status, 200);
  const byLease = await res.json();
  assert.equal(byLease.searchOrder.id, 'SO-3-FULL-VIEW-WEST-LEASE');
  assert.deepEqual(byLease.leases[0].signedRoomIds, ['R-1701-W']);
});
