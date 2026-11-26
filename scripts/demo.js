'use strict';

/**
 * End-to-end demonstration over real HTTP.
 *
 * Casts a host and two players, then walks the scenario from the brief:
 *
 *   - 正文甲 10s, 乙 6s; a mandatory guide sits at the 甲→乙 seam (2s / 5s).
 *   - Both players play continuously to their own 13th second. Only the first
 *     player has heard any of 乙, so the discussion point cannot be granted.
 *   - Both finish 乙; the discussion becomes openable.
 *   - The host hands authority to a player; the OLD host (whose connection was
 *     dropped) sends one late open request — it is rejected as stale.
 *   - The NEW host queries who is missing what, then opens.
 *   - A player reconnects and reads back the truly effective permit + where to
 *     resume re-listening.
 *
 * Run: node scripts/demo.js     (add --keep to leave the server running)
 */

const { createServer } = require('../src/server');
const {
  logicalPrefixToIntervals,
  logicalRangeToIntervals,
} = require('../src/model');

function makeClient(base) {
  async function call(method, path, body) {
    const res = await fetch(base + path, {
      method,
      headers: body ? { 'content-type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const json = await res.json();
    return { status: res.status, body: json };
  }
  return {
    get: (p) => call('GET', p),
    post: (p, b) => call('POST', p, b),
    del: (p) => call('DELETE', p),
  };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const server = createServer();
  await new Promise((r) => server.listen(0, r));
  const port = server.address().port;
  const base = `http://localhost:${port}`;
  const api = makeClient(base);
  const keep = process.argv.includes('--keep');

  const line = (s = '') => console.log(s);
  const rule = (s) => { line(); line('─'.repeat(72)); line(s); line('─'.repeat(72)); };

  try {
    line(`co-listening service on ${base}`);

    // --- 0. Create the room ------------------------------------------------
    rule('0. create room: 甲 10s + 乙 6s, seam guide 2s (p1) / 5s (p2), sync rule');
    await api.post('/rooms', {
      roomId: 'bookclub',
      flow: [
        { segmentId: 'A', duration: 10 },
        { segmentId: 'B', duration: 6 },
      ],
      rule: 'sync',
      host: { id: 'host', name: '主持人' },
      members: [{ id: 'p1', name: '听众一' }, { id: 'p2', name: '听众二' }],
      assignmentsByMember: {
        p1: { 'seam:0': { guideId: 'g-p1', duration: 2, mustListen: true } },
        p2: { 'seam:0': { guideId: 'g-p2', duration: 5, mustListen: true } },
      },
    });

    // Open one live connection per participant.
    const conn = {};
    for (const id of ['host', 'p1', 'p2']) {
      const r = await api.post('/rooms/bookclub/connections', { memberId: id, connId: `${id}-c1` });
      conn[id] = `${id}-c1`;
      line(`   connected: ${id}`);
    }

    const layout = async (mid) => (await api.get(`/rooms/bookclub/members/${mid}/progress`)).body;

    // Host binds the discussion to 乙 (occurrence 1).
    await api.post('/rooms/bookclub/discussion/point', {
      actorMemberId: 'host', authorityEpoch: 1, connId: conn.host,
      segmentId: 'B', occurrence: 1,
    });

    // --- 1. Both play to their own 13th second -----------------------------
    rule('1. both players play continuously to 13s; only p1 has heard any of 乙');
    for (const mid of ['p1', 'p2']) {
      const prog = await layout(mid);
      // Reconstruct per-item intervals for "continuous to logical 13s".
      const items = logicalPrefixToIntervals(prog.items, 13);
      await api.post('/rooms/bookclub/reports', {
        memberId: mid, connId: conn[mid], manifestVersion: 1, items,
      });
    }
    for (const mid of ['p1', 'p2']) {
      const prog = await layout(mid);
      const bp = prog.bodyPosition;
      line(`   ${mid}: logical position ${prog.position}s, body at ${bp.segmentId} offset ${bp.offset}s`);
    }

    // Even though both "requested" to go on, the point is not grantable.
    const tryOpen = await api.post('/rooms/bookclub/discussion/open', {
      actorMemberId: 'host', authorityEpoch: 1, connId: conn.host,
    });
    line(`   host tries to open discussion -> HTTP ${tryOpen.status} ${tryOpen.body.error}`);
    line(`   waiting on: ${tryOpen.body.waitingOn.map((w) => `${w.memberId}(${w.reason})`).join(', ')}`);

    // --- 2. Both finish 乙 -------------------------------------------------
    rule('2. both finish 乙 (their full timelines); discussion becomes openable');
    for (const mid of ['p1', 'p2']) {
      const prog = await layout(mid);
      const items = logicalPrefixToIntervals(prog.items, prog.totalDuration);
      await api.post('/rooms/bookclub/reports', {
        memberId: mid, connId: conn[mid], manifestVersion: 1, items,
      });
    }
    const missing = await api.get('/rooms/bookclub/who-missing');
    line(`   who-missing.waitingOn = [${missing.body.waitingOn.join(', ')}]`);

    // --- 3. Authority handover + late request from the old host ------------
    rule('3. host hands authority to p1; the OLD host sends a late open request');
    const transfer = await api.post('/rooms/bookclub/authority', {
      actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, toMemberId: 'p1',
    });
    line(`   authority now epoch ${transfer.body.authorityEpoch}, host ${transfer.body.hostMemberId}`);

    // The old host's connection drops...
    await api.del(`/rooms/bookclub/connections/${conn.host}?memberId=host`);
    line('   old host connection dropped');
    await sleep(20);

    // ...and it wakes up to send one late open request with the stale epoch.
    const lateOpen = await api.post('/rooms/bookclub/discussion/open', {
      actorMemberId: 'host', authorityEpoch: 1, connId: conn.host,
    });
    line(`   old host late open -> HTTP ${lateOpen.status} ${lateOpen.body.error} (expected STALE_AUTHORITY)`);
    if (lateOpen.status < 400) throw new Error('stale authority was wrongly accepted!');

    // --- 4. New host queries and opens ------------------------------------
    rule('4. new host (p1) queries who is missing, then opens discussion');
    const ask = await api.get('/rooms/bookclub/who-missing');
    line(`   who-missing -> waitingOn=[${ask.body.waitingOn.join(', ')}], round=[${ask.body.round.join(', ')}]`);
    const opened = await api.post('/rooms/bookclub/discussion/open', {
      actorMemberId: 'p1', authorityEpoch: 2, connId: conn.p1,
    });
    line(`   open -> HTTP ${opened.status}, status=${opened.body.status}`);

    // The old authority cannot open a second time either.
    const again = await api.post('/rooms/bookclub/discussion/open', {
      actorMemberId: 'host', authorityEpoch: 1, connId: conn.host,
    });
    line(`   old authority opens again -> HTTP ${again.status} ${again.body.error}`);

    // --- 5. Permit + reconnect reads back the truth -----------------------
    rule('5. p1 re-authenticates: reads back the effective permit and resume point');
    // End the discussion, then p1 requests a continue permit.
    await api.post('/rooms/bookclub/discussion/end', {
      actorMemberId: 'p1', authorityEpoch: 2, connId: conn.p1,
    });
    await api.post('/rooms/bookclub/permits', {
      memberId: 'p1', connId: conn.p1, kind: 'continue',
    });

    // p1 drops and reconnects with the same connection id.
    await api.del(`/rooms/bookclub/connections/${conn.p1}?memberId=p1`);
    const snap = await api.post('/rooms/bookclub/connections', { memberId: 'p1', connId: conn.p1 });
    line(`   p1 reconnect: effectivePermits=${snap.body.effectivePermits.length}, resume position=${snap.body.relisten.position}s`);
    line(`   reconnect flag = ${snap.body.connection ? snap.body.connection.alive : 'n/a'}, reopenCount=${snap.body.connection && snap.body.connection.reopenCount}`);

    // --- 6. Audit trail ---------------------------------------------------
    rule('6. audit trail (last 10 events) — every permission decision is on record');
    const audit = await api.get('/rooms/bookclub/audit');
    for (const e of audit.body.events.slice(-10)) {
      line(`   #${e.seq} ${e.type}${e.memberId ? ' ' + e.memberId : ''}${e.reason ? ' (' + e.reason + ')' : ''}`);
    }

    line();
    line('done. (server stays up with --keep)');
  } finally {
    if (!keep) {
      await new Promise((r) => server.close(r));
      line('server stopped.');
    }
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
