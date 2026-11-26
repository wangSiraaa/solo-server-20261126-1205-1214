import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from '../src/server.js';

async function startedServer(seed = false) {
  const { server, observatory } = createServer({ seed });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const base = `http://127.0.0.1:${port}`;
  return {
    base,
    observatory,
    close: () => new Promise((resolve) => server.close(resolve)),
    api: async (path, options = {}) => {
      const res = await fetch(`${base}${path}`, {
        method: options.method || 'GET',
        headers: { 'content-type': 'application/json', ...(options.headers || {}) },
        body: options.body ? JSON.stringify(options.body) : undefined,
      });
      const text = await res.text();
      const json = text ? JSON.parse(text) : null;
      return { status: res.status, json };
    },
  };
}

test('HTTP health, reader trace and exact clock scenario', async () => {
  const ctx = await startedServer(true);
  try {
    const health = await ctx.api('/health');
    assert.equal(health.status, 200);
    assert.equal(health.json.ok, true);

    const trace = await ctx.api('/api/commissions/C-DONE/trace');
    assert.equal(trace.status, 200);
    assert.equal(trace.json.commonSeconds, 3);
    assert.match(trace.json.readerLinks.html, /reader\.html/);
    assert.ok(trace.json.dispatchReceipts[0].receipts.length === 2);
    assert.ok(trace.json.reports[0].sealed);

    let evaluation = await ctx.api('/api/commissions/C-LAB/evaluate', { method: 'POST', body: {} });
    assert.equal(evaluation.json.commonSeconds, 3);
    assert.equal(evaluation.json.qualified, true);

    await ctx.api('/api/stations/S2/clock', { method: 'PUT', body: { agreedOffset: 1 } });
    evaluation = await ctx.api('/api/commissions/C-LAB/evaluate', { method: 'POST', body: {} });
    assert.deepEqual(evaluation.json.commonIntervals, [{ start: 6, end: 8 }]);
    assert.equal(evaluation.json.commonSeconds, 2);

    await ctx.api('/api/stations/S2/clock', { method: 'PUT', body: { agreedOffset: 0 } });
    await ctx.api('/api/commissions/C-LAB', { method: 'PATCH', body: { requiredCommonSeconds: 4 } });
    evaluation = await ctx.api('/api/commissions/C-LAB/evaluate', { method: 'POST', body: {} });
    assert.equal(evaluation.json.commonSeconds, 3);
    assert.equal(evaluation.json.qualified, false);
  } finally {
    await ctx.close();
  }
});

test('HTTP competing dispatch: exactly one claim wins and loser gets 409', async () => {
  const ctx = await startedServer(false);
  try {
    for (const id of ['A', 'B']) {
      await ctx.api('/api/stations', { method: 'POST', body: { id } });
    }
    for (const id of ['CA', 'CB']) {
      const created = await ctx.api('/api/commissions', {
        method: 'POST',
        body: { id, phenomenonInterval: [1, 4], requiredCommonSeconds: 2, stationIds: ['A', 'B'] },
      });
      assert.equal(created.status, 201);
    }
    await ctx.api('/api/windows', {
      method: 'POST',
      body: { id: 'W1', stationIds: ['A', 'B'], interval: [1, 4], status: 'confirmed' },
    });

    const results = await Promise.all([
      ctx.api('/api/dispatch/window', { method: 'POST', body: { commissionId: 'CA', windowId: 'W1' } }),
      ctx.api('/api/dispatch/window', { method: 'POST', body: { commissionId: 'CB', windowId: 'W1' } }),
    ]);
    const statuses = results.map((r) => r.status).sort();
    assert.deepEqual(statuses, [201, 409]);
    const conflict = results.find((r) => r.status === 409);
    assert.equal(conflict.json.error.code, 'SLOT_CONFLICT');
  } finally {
    await ctx.close();
  }
});

test('reader HTML entry is served', async () => {
  const ctx = await startedServer(true);
  try {
    const res = await fetch(`${ctx.base}/reader.html?commissionId=C-DONE`);
    assert.equal(res.status, 200);
    const html = await res.text();
    assert.match(html, /读片员/);
  } finally {
    await ctx.close();
  }
});
