'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createServer } = require('../src/server');
const { logicalPrefixToIntervals } = require('../src/model');

function client(base) {
  const call = async (method, path, body) => {
    const res = await fetch(base + path, {
      method,
      headers: body ? { 'content-type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    return { status: res.status, body: await res.json() };
  };
  return {
    get: (p) => call('GET', p),
    post: (p, b) => call('POST', p, b),
    del: (p) => call('DELETE', p),
  };
}

async function withServer(fn) {
  const server = createServer();
  await new Promise((r) => server.listen(0, r));
  const base = `http://localhost:${server.address().port}`;
  try {
    await fn(client(base), base, server);
  } finally {
    await new Promise((r) => server.close(r));
  }
}

async function seed(api) {
  const r = await api.post('/rooms', {
    roomId: 'r',
    flow: [
      { segmentId: 'A', duration: 10 },
      { segmentId: 'B', duration: 6 },
    ],
    rule: 'sync',
    host: { id: 'host' },
    members: [{ id: 'p1' }, { id: 'p2' }],
    assignmentsByMember: {
      p1: { 'seam:0': { guideId: 'g1', duration: 2, mustListen: true } },
      p2: { 'seam:0': { guideId: 'g2', duration: 5, mustListen: true } },
    },
  });
  assert.equal(r.status, 201);
  for (const id of ['host', 'p1', 'p2']) {
    await api.post('/rooms/r/connections', { memberId: id, connId: `${id}-c1` });
  }
}

async function playTo(api, mid, connId, upto) {
  const prog = (await api.get(`/rooms/r/members/${mid}/progress`)).body;
  const items = logicalPrefixToIntervals(prog.items, upto);
  return api.post('/rooms/r/reports', { memberId: mid, connId, manifestVersion: prog.manifestVersion, items });
}

test('HTTP: health and room creation', async () => {
  await withServer(async (api) => {
    assert.equal((await api.get('/health')).status, 200);
    await seed(api);
    const room = await api.get('/rooms/r');
    assert.equal(room.status, 200);
    assert.equal(room.body.rule, 'sync');
    assert.deepEqual(room.body.members.map((m) => m.id).sort(), ['host', 'p1', 'p2']);
  });
});

test('HTTP: 13s example blocks discussion, then allows it after finishing B', async () => {
  await withServer(async (api) => {
    await seed(api);
    await api.post('/rooms/r/discussion/point', {
      actorMemberId: 'host', authorityEpoch: 1, connId: 'host-c1', segmentId: 'B', occurrence: 1,
    });
    await playTo(api, 'p1', 'p1-c1', 13);
    await playTo(api, 'p2', 'p2-c1', 13);

    const p1 = (await api.get('/rooms/r/members/p1/progress')).body;
    const p2 = (await api.get('/rooms/r/members/p2/progress')).body;
    assert.equal(p1.bodyPosition.segmentId, 'B');
    assert.equal(p1.bodyPosition.offset, 1);
    assert.equal(p2.bodyPosition.segmentId, 'B');
    assert.equal(p2.bodyPosition.offset, 0);

    const open1 = await api.post('/rooms/r/discussion/open', { actorMemberId: 'host', authorityEpoch: 1, connId: 'host-c1' });
    assert.equal(open1.status, 409);
    assert.equal(open1.body.error, 'DISCUSSION_NOT_READY');
    assert.deepEqual(open1.body.waitingOn.map((w) => w.memberId).sort(), ['p1', 'p2']);

    await playTo(api, 'p1', 'p1-c1', 18);
    await playTo(api, 'p2', 'p2-c1', 21);
    const open2 = await api.post('/rooms/r/discussion/open', { actorMemberId: 'host', authorityEpoch: 1, connId: 'host-c1' });
    assert.equal(open2.status, 200);
    assert.equal(open2.body.status, 'open');
  });
});

test('HTTP: authority transfer + late stale open rejected; rejoin snapshot truthful', async () => {
  await withServer(async (api) => {
    await seed(api);
    await api.post('/rooms/r/discussion/point', { actorMemberId: 'host', authorityEpoch: 1, connId: 'host-c1', segmentId: 'B', occurrence: 1 });
    await playTo(api, 'p1', 'p1-c1', 18);
    await playTo(api, 'p2', 'p2-c1', 21);

    await api.post('/rooms/r/authority', { actorMemberId: 'host', authorityEpoch: 1, connId: 'host-c1', toMemberId: 'p1' });
    await api.del('/rooms/r/connections/host-c1?memberId=host');

    const late = await api.post('/rooms/r/discussion/open', { actorMemberId: 'host', authorityEpoch: 1, connId: 'host-c1' });
    assert.equal(late.status, 409);
    assert.equal(late.body.error, 'STALE_AUTHORITY');

    const who = await api.get('/rooms/r/who-missing');
    assert.deepEqual(who.body.waitingOn, []);

    const opened = await api.post('/rooms/r/discussion/open', { actorMemberId: 'p1', authorityEpoch: 2, connId: 'p1-c1' });
    assert.equal(opened.status, 200);

    // p1 reconnects to the same connection id and reads effective state.
    await api.del('/rooms/r/connections/p1-c1?memberId=p1');
    const snap = await api.post('/rooms/r/connections', { memberId: 'p1', connId: 'p1-c1' });
    assert.equal(snap.status, 201);
    assert.equal(snap.body.connection.alive, true);
    assert.ok(snap.body.relisten.position > 0);
  });
});

test('HTTP: malformed request returns structured error', async () => {
  await withServer(async (api) => {
    await seed(api);
    const bad = await api.post('/rooms/r/reports', {
      memberId: 'p1', connId: 'p1-c1', manifestVersion: 1, items: [{ idx: 99, from: 0, to: 1 }],
    });
    assert.equal(bad.status, 400);
    assert.equal(bad.body.error, 'BAD_REQUEST');
  });
});
