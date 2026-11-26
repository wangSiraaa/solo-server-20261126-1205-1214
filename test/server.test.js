import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { once } from 'node:events';
import { Store } from '../src/store.js';
import { createServer } from '../src/server.js';
import { seedScenario } from '../src/fixtures.js';

function tempDb() {
  const dir = mkdtempSync(join(tmpdir(), 'joint-server-'));
  return join(dir, 'election.json');
}

async function listen(server) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return `http://127.0.0.1:${server.address().port}`;
}

async function stop(server) {
  server.close();
  server.closeAllConnections?.();
  await once(server, 'close');
}

test('两个柜员同时发同一席：只有两张票消耗于唯一就任书；重启和重发回执可查回', async () => {
  const file = tempDb();
  const store = new Store(file);
  const roundId = 'desk-race';
  store.transact({}, () => seedScenario(store, 'transfer', { roundId }));

  const server = createServer({ store });
  const base = await listen(server);

  const issue = (key) =>
    fetch(`${base}/api/rounds/${roundId}/appointments`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
      body: JSON.stringify({ seatId: 'reading', personId: 'jia' }),
    }).then(async (response) => ({ key, status: response.status, body: await response.json() }));

  const results = await Promise.all([issue('clerk-1-key'), issue('clerk-2-key')]);
  const statuses = results.map((r) => r.status).sort();
  assert.deepEqual(statuses, [201, 409]);

  const winner = results.find((r) => r.status === 201);
  const loser = results.find((r) => r.status === 409);
  assert.deepEqual(winner.body.ballotIds, ['b1', 'b2']);
  assert.equal(loser.body.error.code, 'SEAT_ALREADY_FILLED');
  assert.equal(loser.body.error.details.existingAppointment.id, winner.body.id);

  const persisted = store.getRound(roundId);
  assert.equal(persisted.appointments.length, 1);
  assert.deepEqual(persisted.appointments[0].ballotIds, ['b1', 'b2']);

  await stop(server);

  // Service restart: a brand-new Store/server must load the atomic JSON write.
  const restartedStore = new Store(file);
  assert.equal(restartedStore.getRound(roundId).appointments.length, 1);
  const restartedServer = createServer({ store: restartedStore });
  const restartedBase = await listen(restartedServer);

  const retry = await fetch(`${restartedBase}/api/rounds/${roundId}/appointments`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': winner.key },
    body: JSON.stringify({ seatId: 'reading', personId: 'jia' }),
  });
  const retryBody = await retry.json();
  assert.equal(retry.status, 201);
  assert.equal(retry.headers.get('idempotent-replayed'), 'true');
  assert.equal(retryBody.id, winner.body.id);
  assert.deepEqual(retryBody.ballotIds, ['b1', 'b2']);

  const receiptResponse = await fetch(`${restartedBase}/api/receipts/${winner.body.receiptCode}`);
  assert.equal(receiptResponse.status, 200);
  const receipt = await receiptResponse.json();
  assert.equal(receipt.appointment.id, winner.body.id);
  assert.deepEqual(receipt.usedBallotIds, ['b1', 'b2']);
  assert.equal(receipt.audit.length, 2);

  const resend = await fetch(`${restartedBase}/api/receipts/${winner.body.receiptCode}/resend`, { method: 'POST' });
  assert.equal(resend.status, 200);
  const resentBody = await resend.json();
  assert.equal(resentBody.resent, true);
  assert.deepEqual(resentBody.usedBallotIds, ['b1', 'b2']);

  await stop(restartedServer);
});

test('并发重复 settle 不会产生两套就任书', async () => {
  const file = tempDb();
  const store = new Store(file);
  const roundId = 'settle-race';
  store.transact({}, () => seedScenario(store, 'transfer', { roundId }));
  const server = createServer({ store });
  const base = await listen(server);

  const responses = await Promise.all([
    fetch(`${base}/api/rounds/${roundId}/settle`, { method: 'POST' }).then((r) => r.status),
    fetch(`${base}/api/rounds/${roundId}/settle`, { method: 'POST' }).then((r) => r.status),
  ]);
  assert.deepEqual(responses.sort(), [200, 201]);
  assert.equal(store.getRound(roundId).settlement.appointments.length, 2);
  assert.equal(store.getRound(roundId).appointments.length, 2);

  await stop(server);
});
