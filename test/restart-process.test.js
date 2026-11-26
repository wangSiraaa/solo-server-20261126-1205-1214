'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { spawn } = require('node:child_process');

const root = path.join(__dirname, '..');

function getFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

async function waitForHealth(base, child, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`server exited early with code ${child.exitCode}`);
    }
    try {
      const res = await fetch(`${base}/health`);
      if (res.ok) return;
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  throw new Error(`server did not become healthy: ${lastError?.message}`);
}

function startServer(dataFile, port) {
  const child = spawn(process.execPath, [path.join(root, 'src', 'server.js')], {
    env: {
      ...process.env,
      DATA_FILE: dataFile,
      PORT: String(port),
      HOST: '127.0.0.1',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.on('data', (chunk) => process.stdout.write(`[server:${port}] ${chunk}`));
  child.stderr.on('data', (chunk) => process.stderr.write(`[server-error:${port}] ${chunk}`));
  const base = `http://127.0.0.1:${port}`;
  return waitForHealth(base, child).then(() => ({ child, base }));
}

function stopServer(child) {
  if (!child || child.exitCode !== null) return Promise.resolve();
  return new Promise((resolve) => {
    child.once('exit', resolve);
    child.kill('SIGTERM');
  });
}

test('a real process restart preserves attendance, leases, settlements and idempotency', async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'platform-restart-'));
  const dataFile = path.join(dir, 'restart.json');
  const port = await getFreePort();
  let running = await startServer(dataFile, port);
  let child = running.child;
  const base = running.base;

  t.after(async () => stopServer(child));

  let res = await fetch(`${base}/demo/run`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({}),
  });
  assert.equal(res.status, 201);
  const demo = await res.json();
  assert.equal(demo.required.case1BothViewedBothSigned.extraction.totalPoints, 8);

  res = await fetch(`${base}/tenants/T-1001/trace/SO-1-BOTH-VIEW-BOTH-SIGN`);
  assert.equal(res.status, 200);
  const beforeRestart = await res.json();
  assert.equal(beforeRestart.evidence.withdrawnPointTotal, 8);

  await stopServer(child);
  child = null;

  // Relaunch the exact same server process against the same JSON file.
  running = await startServer(dataFile, port);
  child = running.child;

  res = await fetch(`${base}/stores/S-A/trace/SO-1-BOTH-VIEW-BOTH-SIGN`);
  assert.equal(res.status, 200);
  const afterRestart = await res.json();
  assert.equal(afterRestart.visits[0].clientConfirmation.decision, 'CONFIRMED');
  assert.equal(afterRestart.leases[0].id, 'LSE-SO-1-BOTH-VIEW-BOTH-SIGN');
  // 8 from this core order plus 2 from the overlap relay duplicate demo.
  assert.equal(afterRestart.myMockAccount.balancePoints, 10);
  assert.equal(afterRestart.mySettlements.find((s) => s.id === 'STL-SO-1-A-8').totalPoints, 8);

  const itemIds = afterRestart.myCommissionItems
    .filter((i) => i.settlementId === 'STL-SO-1-A-8')
    .map((i) => i.id)
    .sort();
  assert.equal(itemIds.length, 2);

  // The persisted idempotency record still prevents a second payout.
  res = await fetch(`${base}/settlements`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'idempotency-key': 'REQ-SO-1-A-8' },
    body: JSON.stringify({ storeId: 'S-A', itemIds }),
  });
  assert.equal(res.status, 200);
  const replay = await res.json();
  assert.equal(replay.replayed, true);
  assert.equal(replay.storeAccount.balancePoints, 10);

  res = await fetch(`${base}/settlements`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ storeId: 'S-A', itemIds, requestId: 'AFTER-RESTART-NEW-KEY' }),
  });
  assert.equal(res.status, 409);
  assert.equal((await res.json()).error.code, 'COMMISSION_ITEM_ALREADY_WITHDRAWN');

  await stopServer(child);
  child = null;
});
