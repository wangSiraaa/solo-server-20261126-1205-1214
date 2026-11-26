'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { Engine } = require('../src/engine');
const { createServer } = require('../src/server');

async function start() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dance-srv-'));
  const engine = new Engine({ filePath: path.join(dir, 'events.jsonl') });
  const server = createServer({ engine });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  return { server, base, engine, dir };
}

async function call(base, method, urlPath, body) {
  const res = await fetch(base + urlPath, {
    method,
    headers: body ? { 'content-type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json();
  return { status: res.status, body: json };
}

test('server: full flow over HTTP including concurrent same-round booking', async () => {
  const { server, base } = await start();
  try {
    assert.equal((await call(base, 'POST', '/sessions', { sessionId: 's', rounds: ['waltz', 'tango', 'salsa'], zones: ['z1', 'z2'] })).status, 200);
    await call(base, 'POST', '/sessions/s/guests', { number: 'L1', name: '领队一', roles: ['lead'], targetPartners: 1 });
    await call(base, 'POST', '/sessions/s/guests', { number: 'L2', name: '领队二', roles: ['lead'], targetPartners: 1 });
    await call(base, 'POST', '/sessions/s/guests', { number: 'F', name: '跟舞者', roles: ['follow'], targetPartners: 2 });
    await call(base, 'POST', '/sessions/s/zones', { zone: 'z1', open: true });
    await call(base, 'POST', '/sessions/s/zones', { zone: 'z2', open: true });

    const i1 = (await call(base, 'POST', '/sessions/s/invitations', { round: 1, from: 'L1', to: 'F', fromRole: 'lead', toRole: 'follow', zone: 'z1' })).body;
    const i2 = (await call(base, 'POST', '/sessions/s/invitations', { round: 1, from: 'L2', to: 'F', fromRole: 'lead', toRole: 'follow', zone: 'z2' })).body;

    // Fire both confirmations concurrently, exactly as two phones might.
    const [c1, c2] = await Promise.all([
      call(base, 'POST', `/sessions/s/invitations/${i1.id}/confirm`, { by: 'F' }),
      call(base, 'POST', `/sessions/s/invitations/${i2.id}/confirm`, { by: 'F' }),
    ]);
    const oks = [c1, c2].filter((r) => r.body.ok === true);
    const conflicts = [c1, c2].filter((r) => r.body.code === 'CONFLICT');
    assert.equal(oks.length, 1, 'exactly one confirmation wins');
    assert.equal(conflicts.length, 1, 'exactly one is a conflict');

    const eff = (await call(base, 'GET', '/sessions/s/guests/F/effective?round=1')).body;
    assert.equal(eff.effective.kind, 'confirmed-invitation');

    const close = (await call(base, 'POST', '/sessions/s/rounds/1/close')).body;
    assert.equal(close.dances.length, 1);
    const met = (await call(base, 'GET', '/sessions/s/guests/F/met')).body;
    assert.deepEqual(met.met, [eff.effective.partner]);
  } finally {
    server.close();
  }
});

test('server: 404 and 400 mapped from domain errors', async () => {
  const { server, base } = await start();
  try {
    const nf = await call(base, 'GET', '/sessions/nope/guests/1/met');
    assert.equal(nf.status, 404);
    assert.equal(nf.body.error, 'NO_SESSION');
    const bad = await call(base, 'POST', '/sessions', { sessionId: 's', rounds: 0, zones: 1 });
    assert.equal(bad.status, 400);
  } finally {
    server.close();
  }
});

test('server: persistence survives engine reload from the same log file', async () => {
  const { server, base, dir } = await start();
  let file;
  try {
    await call(base, 'POST', '/sessions', { sessionId: 's', rounds: ['r1'], zones: ['z1'] });
    await call(base, 'POST', '/sessions/s/guests', { number: '001', name: '甲', roles: ['lead'], targetPartners: 1 });
    await call(base, 'POST', '/sessions/s/guests', { number: '002', name: '乙', roles: ['follow'], targetPartners: 1 });
    await call(base, 'POST', '/sessions/s/zones', { zone: 'z1', open: true });
    const inv = (await call(base, 'POST', '/sessions/s/invitations', { round: 1, from: '001', to: '002', fromRole: 'lead', toRole: 'follow', zone: 'z1' })).body;
    await call(base, 'POST', `/sessions/s/invitations/${inv.id}/confirm`, { by: '002' });
    await call(base, 'POST', '/sessions/s/rounds/1/close');
    file = path.join(dir, 'events.jsonl');
  } finally {
    server.close();
  }
  const reloaded = new Engine({ filePath: file });
  assert.deepEqual(reloaded.metPartners('s', '001'), ['002']);
});
