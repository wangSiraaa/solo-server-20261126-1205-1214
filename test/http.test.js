import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { NightSchoolService } from '../src/service.js';
import { createServer } from '../src/server.js';

test('HTTP entry serves UI and complete JSON workflow', async (t) => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'night-school-http-'));
  const dataFile = path.join(dir, 'http.json');
  const service = new NightSchoolService({ dataFile });
  await service.init();
  const server = createServer(service);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  const base = `http://127.0.0.1:${address.port}`;

  t.after(async () => {
    await new Promise((resolve) => server.close(resolve));
    await rm(dir, { recursive: true, force: true });
  });

  const homeResponse = await fetch(`${base}/`);
  assert.equal(homeResponse.status, 200);
  const html = await homeResponse.text();
  assert.match(html, /夜校报名表服务/);
  assert.match(html, /原表原办/);

  const json = async (method, url, body, headers = {}) => {
    const response = await fetch(`${base}${url}`, {
      method,
      headers: body === undefined ? headers : { 'content-type': 'application/json', ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json();
    return { status: response.status, data, headers: response.headers };
  };

  const cls = await json('POST', '/api/classes', {
    id: 'http-class',
    stationCount: 3,
    pairGroups: [['S1', 'S2']],
  });
  assert.equal(cls.status, 201);

  const answers = {
    guestName: 'HTTP 阿夜',
    contact: 'http-aye@example.test',
    attendanceMode: 'pair',
    generalConsent: true,
    partnerName: 'HTTP 小校',
    partnerContact: 'http-xiao@example.test',
    pairWorkPlan: '网络接口演示',
    pairConsent: true,
  };
  const draft = await json('POST', '/api/drafts', { id: 'http-draft', classId: 'http-class', answers });
  assert.equal(draft.status, 201);

  const invite = await json('POST', `/api/drafts/${draft.data.draft.id}/invitations`, {});
  assert.equal(invite.status, 201);
  const confirm = await json('POST', `/api/invitations/${invite.data.invitation.id}/confirm`, {
    token: invite.data.invitation.token,
    consent: true,
    companionName: 'HTTP 小校',
  });
  assert.equal(confirm.status, 200);

  const idemKey = 'http-idempotency-key';
  const first = await json('POST', `/api/drafts/${draft.data.draft.id}/submit`, {}, {
    'idempotency-key': idemKey,
  });
  assert.equal(first.status, 201);
  assert.deepEqual(first.data.order.seats.map((s) => s.stationId), ['S1', 'S2']);

  const repeat = await json('POST', '/api/orders', {
    draftId: draft.data.draft.id,
    idempotencyKey: idemKey,
  });
  assert.equal(repeat.status, 200);
  assert.equal(repeat.data.replayed, true);
  assert.equal(repeat.data.order.id, first.data.order.id);

  const counter = await json('GET', `/api/counter/orders/${first.data.order.id}`);
  assert.equal(counter.status, 200);
  assert.equal(counter.data.counter.formVersion.version, 1);
  assert.equal(counter.data.counter.seatedPeople.length, 2);
  assert.equal(counter.data.counter.enrollmentNumbers.length, 2);

  const enrollment = await json(
    'GET',
    `/api/counter/enrollments/${encodeURIComponent(counter.data.enrollmentNumbers[0])}`,
  );
  assert.equal(enrollment.status, 200);
  assert.equal(enrollment.data.id, first.data.order.id);

  const published = await json('POST', '/api/admin/forms/publish-split', {});
  assert.equal(published.status, 200);
  assert.equal(published.data.form.version, 2);

  const oldDraft = await json('GET', `/api/drafts/${draft.data.draft.id}`);
  assert.equal(oldDraft.data.draft.formVersion, 1);
});
