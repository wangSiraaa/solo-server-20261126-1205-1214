import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { SalonService } from '../src/service.js';
import { createServer } from '../src/server.js';

test('HTTP counter flow survives a real service restart', async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'salon-http-restart-'));
  const dataFile = path.join(dir, 'salon.json');
  let service = new SalonService({ dataFile });
  let server = createServer(service);
  t.after(() => {
    server.closeAllConnections?.();
    return new Promise((resolve) => server.close(resolve));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  let base = `http://127.0.0.1:${port}`;

  const api = async (method, route, body) => {
    const response = await fetch(`${base}${route}`, {
      method,
      headers: body === undefined ? {} : { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return { status: response.status, json: await response.json() };
  };

  await api('POST', '/api/staff', { id: 'lin', name: '林师傅', canUnroll: true });
  await api('POST', '/api/chairs', { id: 'c1', name: '一椅' });
  await api('POST', '/api/chairs', { id: 'c2', name: '二椅' });
  const booked = await api('POST', '/api/appointments', {
    id: 'p1',
    customerName: '烫卷客',
    signature: '烫卷客',
    mode: 'principal',
    principalStylistId: 'lin',
    sections: [{ id: 'z1' }],
    plannedRollCompleteAt: 0,
    unroll: { durationMinutes: 2, minWaitMinutes: 3, maxWaitMinutes: 4 },
  });
  assert.equal(booked.status, 201);
  assert.equal(booked.json.feasibility.feasible, true);
  assert.equal((await api('POST', '/api/appointments/p1/check-in', { at: 0, chairId: 'c1' })).status, 200);
  assert.equal((await api('POST', '/api/appointments/p1/sections/z1/complete', { at: 0, stylistId: 'lin' })).status, 200);

  const quote = await api('POST', '/api/quotes/insert', {
    stylistId: 'lin',
    durationMinutes: 3,
    availableFrom: 0,
    availableUntil: 3,
    chairId: 'c2',
  });
  assert.equal(quote.status, 200);
  assert.equal(quote.json.feasible, true);

  server.closeAllConnections?.();
  await new Promise((resolve) => server.close(resolve));

  service = new SalonService({ dataFile });
  server = createServer(service);
  t.after(() => {
    server.closeAllConnections?.();
    return new Promise((resolve) => server.close(resolve));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${server.address().port}`;

  const receipt = await api('GET', '/api/appointments/p1/receipt');
  assert.equal(receipt.status, 200);
  assert.equal(receipt.json.segments.find((s) => s.segment === 'roll_complete').state, 'realized');
  const afterRestart = await api('POST', '/api/quotes/insert', {
    stylistId: 'lin',
    durationMinutes: 3,
    availableFrom: 0,
    availableUntil: 3,
    chairId: 'c2',
  });
  assert.equal(afterRestart.json.feasible, true);
});
