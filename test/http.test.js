import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from '../src/store.js';
import { createServer } from '../src/api.js';

// Minimal fetch-based client against an ephemeral server.
async function withServer(fn) {
  const store = createStore();
  const server = createServer(store);
  await new Promise((r) => server.listen(0, r));
  const { port } = server.address();
  const base = `http://127.0.0.1:${port}`;
  const call = async (method, path, body) => {
    const res = await fetch(base + path, {
      method,
      headers: { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const json = res.status === 204 ? null : await res.json();
    return { status: res.status, json };
  };
  try {
    return await fn({ call, store });
  } finally {
    await new Promise((r) => server.close(r));
  }
}

const nightsA = ['2026-12-04', '2026-12-05'];
const nightsB = ['2026-12-11', '2026-12-12'];
const nights = [...nightsA, ...nightsB];

test('HTTP: full lifecycle over the wire', async () => {
  await withServer(async ({ call }) => {
    for (const id of ['a', 'b']) {
      const r = await call('POST', '/households', {
        id, displayName: id.toUpperCase(), members: [`${id}-1`, `${id}-2`],
      });
      assert.equal(r.status, 201);
      const a = await call('PUT', `/households/${id}/application`, {
        travelNights: nights, hostNights: nights, minConsecutiveNights: 1,
      });
      assert.equal(a.status, 200);
    }

    const signed = await call('POST', '/contracts', {
      type: 'pair', households: ['a', 'b'],
      edges: [
        { host: 'a', guest: 'b', plannedNights: nightsA },
        { host: 'b', guest: 'a', plannedNights: nightsB },
      ],
    });
    assert.equal(signed.status, 201);
    const cid = signed.json.id;
    const edgeNights = [nightsA, nightsB];

    for (let edge = 0; edge < 2; edge += 1) {
      const n = edgeNights[edge];
      await call('POST', `/contracts/${cid}/edges/${edge}/report`, { side: 'host', nights: n, confirm: true });
      await call('POST', `/contracts/${cid}/edges/${edge}/report`, { side: 'guest', nights: n, confirm: true });
      await call('POST', `/contracts/${cid}/edges/${edge}/confirm`, { side: 'host' });
      await call('POST', `/contracts/${cid}/edges/${edge}/confirm`, { side: 'guest' });
    }

    const s = await call('GET', `/contracts/${cid}/settlement`);
    assert.equal(s.json.fullyOffset, true);

    const closed = await call('POST', `/contracts/${cid}/close`);
    assert.equal(closed.json.kind, 'final');
    const stmts = await call('GET', `/contracts/${cid}/statements`);
    assert.equal(stmts.json.length, 1);
    const stm = await call('GET', `/statements/${closed.json.id}`);
    assert.equal(stm.json.id, closed.json.id);
  });
});

test('HTTP: errors are structured JSON with status codes', async () => {
  await withServer(async ({ call }) => {
    const missing = await call('GET', '/households/nope');
    assert.equal(missing.status, 404);
    assert.equal(missing.json.error.code, 'NOT_FOUND');

    await call('POST', '/households', { id: 'x', members: ['m1'] });
    await call('PUT', '/households/x/application', { travelNights: nights, hostNights: nights });
    const bad = await call('POST', '/contracts', {
      type: 'pair', households: ['x', 'y'],
      edges: [{ host: 'x', guest: 'y', plannedNights: nights }],
    });
    assert.equal(bad.status, 422); // y has no application
  });
});
