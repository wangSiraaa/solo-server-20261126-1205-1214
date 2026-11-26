import test from 'node:test';
import assert from 'node:assert/strict';

import { RepairService } from '../src/service.js';
import { MemoryStore } from '../src/store.js';
import { createServer } from '../src/server.js';
import { seedScenario } from '../src/scenarios.js';

const service = new RepairService({ store: new MemoryStore() });
seedScenario(service, 'baseline', { includeBing: true });
const server = createServer({ service });

await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;

test.after(() => server.close());

async function api(path, options = {}) {
  const response = await fetch(`${base}${path}`, {
    method: options.method ?? 'GET',
    headers: { 'Content-Type': 'application/json' },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const payload = await response.json();
  return { status: response.status, payload };
}

test('HTTP: 健康检查、工作台和基准结案决策可查询', async () => {
  const health = await api('/api/health');
  assert.equal(health.status, 200);
  assert.equal(health.payload.ticketLimit, 6);

  const closure = await api('/api/tickets/T2/closure');
  assert.equal(closure.status, 200);
  assert.equal(closure.payload.eligible, true);
  assert.deepEqual(closure.payload.compatibility.combinations, [['A']]);

  const steps = await api('/api/next-steps?role=resident&actorId=U_YI');
  assert.equal(steps.status, 200);
  assert.equal(steps.payload.items[0].code, 'SIGN_CLOSURE_NOTICE');
});

test('HTTP: 重复发送结案通知不产生新通知，住户可签收', async () => {
  const first = await api('/api/tickets/T1/closure-notice', { method: 'POST' });
  const second = await api('/api/tickets/T1/closure-notice', { method: 'POST' });
  assert.equal(first.status, 201);
  assert.equal(second.status, 201);
  assert.equal(first.payload.id, second.payload.id);

  const sign = await api('/api/tickets/T1/sign', {
    method: 'POST',
    body: { actorId: 'U_JIA' },
  });
  assert.equal(sign.status, 201);
  assert.equal(sign.payload.kind, 'closure_receipt');

  const tickets = await api('/api/tickets/T1');
  assert.equal(tickets.payload.status, 'closed');
});

test('HTTP: 不满足乙的严格源头条件时返回 409，且私人证据借用返回 403', async () => {
  seedScenario(service, 'withoutBExclusion');

  const closure = await api('/api/tickets/T2/closure');
  assert.equal(closure.payload.eligible, false);

  const notice = await api('/api/tickets/T2/closure-notice', {
    method: 'POST',
  });
  assert.equal(notice.status, 409);
  assert.equal(notice.payload.error.code, 'CLOSURE_NOT_ALLOWED');

  await api('/api/tickets', {
    method: 'POST',
    body: {
      id: 'T4',
      residentId: 'U_DING',
      residentName: '丁',
      location: '邻户',
      symptom: '返潮',
      adjacentTo: ['T2'],
    },
  });
  await api('/api/inspections', {
    method: 'POST',
    body: {
      id: 'PRIVATE_X',
      ticketId: 'T2',
      segmentId: 'SEG_YI_BRANCH',
      scope: 'private',
      readings: [{ pointId: 'CP_YI', wet: true }],
    },
  });
  const borrow = await api('/api/evidence/borrow', {
    method: 'POST',
    body: { evidenceId: 'PRIVATE_X', targetTicketId: 'T4' },
  });
  assert.equal(borrow.status, 403);
  assert.equal(
    borrow.payload.error.code,
    'PRIVATE_EVIDENCE_NOT_TRANSFERABLE',
  );
});

test('HTTP: 住户提出复查后，工作台返回等待复查', async () => {
  seedScenario(service, 'baseline');
  const recheck = await api('/api/tickets/T2/recheck-requests', {
    method: 'POST',
    body: { residentId: 'U_YI', reason: '天花板仍有湿痕' },
  });
  assert.equal(recheck.status, 201);

  const step = await api('/api/tickets/T2/next-step?role=resident');
  assert.equal(step.payload.code, 'WAIT_RECHECK_VISIT');

  const work = await api('/api/tickets/T2/next-step?role=maintainer');
  assert.equal(work.payload.code, 'PERFORM_RECHECK');
});

test('HTTP: 静态页面可打开', async () => {
  const response = await fetch(`${base}/`);
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /报修联办/);
});
