import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createServer } from '../src/server.js';
import { stations, walkPath, cars, neighborTicket } from '../src/canonical.js';

async function listenEphemeral(dataFile) {
  const server = createServer({ dataFile });
  await server.init();
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  return { server, base: `http://127.0.0.1:${port}` };
}

async function api(base, method, route, body, requestId) {
  const response = await fetch(`${base}${route}`, {
    method,
    headers: {
      'content-type': 'application/json',
      ...(requestId ? { 'idempotency-key': requestId } : {}),
    },
    body: body ? JSON.stringify({ ...body, ...(requestId ? { requestId } : {}) }) : undefined,
  });
  const json = await response.json();
  assert.ok(response.ok, `${method} ${route}: ${response.status} ${JSON.stringify(json)}`);
  return json;
}

test('HTTP API：重复请求幂等，关闭重启后乘车单位置仍可查询', async (t) => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'carpool-api-'));
  const dataFile = path.join(dir, 'state.json');
  let runtime = await listenEphemeral(dataFile);

  try {
    for (const station of stations) await api(runtime.base, 'POST', '/stations', station, `api-${station.id}`);
    await api(runtime.base, 'POST', '/walk-paths', walkPath, 'api-walk');
    for (const car of cars) await api(runtime.base, 'POST', '/cars', car, `api-${car.id}`);
    await api(runtime.base, 'POST', '/tickets', neighborTicket, 'api-ticket');

    await api(runtime.base, 'POST', '/cars/car_a/events', { type: 'arrived', stationId: 'south', atMinute: 8 }, 'api-a-arr-s');
    const first = await api(runtime.base, 'POST', '/tickets/RS_241/board', { passengerId: 'lin', atMinute: 8 }, 'api-board-lin');
    const replay = await api(runtime.base, 'POST', '/tickets/RS_241/board', { passengerId: 'lin', atMinute: 8 }, 'api-board-lin');
    assert.equal(replay.idempotentReplay, true);
    assert.equal(replay.receipt.id, first.receipt.id);

    await new Promise((resolve) => runtime.server.close(resolve));
    runtime = await listenEphemeral(dataFile);
    const location = await (await fetch(`${runtime.base}/tickets/RS_241/location`)).json();
    assert.equal(location.passengers[0].id, 'lin');
    assert.equal(location.passengers[0].status, 'on_board');
    assert.equal(location.passengers[0].currentCarId, 'car_a');
    assert.deepEqual(location.passengers[0].owedLegs.map((leg) => leg.legNo), [1, 2]);
  } finally {
    await new Promise((resolve) => runtime.server.close(resolve));
    rmSync(dir, { recursive: true, force: true });
  }
});
