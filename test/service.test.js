import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { NightSchoolService, ServiceError } from '../src/service.js';

async function makeService() {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'night-school-test-'));
  const dataFile = path.join(dir, 'state.json');
  const service = new NightSchoolService({ dataFile });
  await service.init();
  return {
    service,
    dataFile,
    async cleanup() {
      await rm(dir, { recursive: true, force: true });
    },
  };
}

async function pairClass(service, id = 'cls-pair') {
  return service.createClass({
    id,
    name: '三台夜班',
    stationCount: 3,
    pairGroups: [['S1', 'S2']],
  });
}

const pairAnswers = {
  guestName: '阿夜',
  contact: 'aye@example.test',
  attendanceMode: 'pair',
  visitedLab: false,
  generalConsent: true,
  partnerName: '小校',
  partnerContact: 'xiao@example.test',
  pairWorkPlan: '一人负责接线，一人负责安全检查，互换复核。',
  pairConsent: true,
};

const soloAnswers = {
  guestName: '散客',
  contact: 'solo@example.test',
  attendanceMode: 'solo',
  generalConsent: true,
};

async function confirmedPairDraft(service, classId, draftId = 'draft-pair') {
  const created = await service.createDraft({ id: draftId, classId, answers: pairAnswers });
  const draft = created.draft;
  const invitation = (await service.issueInvitation(draft.id)).invitation;
  const confirmation = await service.confirmInvitation(invitation.id, {
    token: invitation.token,
    consent: true,
    companionName: '小校',
  });
  return { draft: created.draft, invitation, receipt: confirmation.receipt };
}

test('complete pair form with correct confirmation books S1 and S2', async () => {
  const env = await makeService();
  try {
    await pairClass(env.service);
    const { draft } = await confirmedPairDraft(env.service, 'cls-pair', 'd1');

    const result = await env.service.submitDraft(draft.id, { idempotencyKey: 'idem-success' });
    assert.equal(result.replayed, false);
    assert.equal(result.order.kind, 'pair');
    assert.deepEqual(result.order.seats.map((seat) => seat.stationId), ['S1', 'S2']);
    assert.equal(result.order.seats[0].personName, '阿夜');
    assert.equal(result.order.seats[1].personName, '小校');
    assert.equal(result.order.enrollmentNumbers.length, 2);
    assert.equal(result.order.counter.enrollmentNumbers.length, 2);
    assert.equal(result.order.formVersion, 1);
    assert.equal(result.order.effectiveAnswers.pairConsent, true);

    const cls = await env.service.getClass('cls-pair');
    assert.deepEqual(cls.class.availableStationIds, ['S3']);
    assert.equal(cls.class.stations[0].occupancy.personName, '阿夜');
    assert.equal(cls.class.stations[1].occupancy.personName, '小校');
  } finally {
    await env.cleanup();
  }
});

test('receipt confirming another draft cannot reserve seats for this order', async () => {
  const env = await makeService();
  try {
    await pairClass(env.service, 'cls-wrong');
    const a = await env.service.createDraft({ id: 'draft-a', classId: 'cls-wrong', answers: pairAnswers });
    await env.service.createDraft({ id: 'draft-b', classId: 'cls-wrong', answers: pairAnswers });
    const invitation = (await env.service.issueInvitation(a.draft.id)).invitation;

    const wrong = await env.service.confirmInvitation(invitation.id, {
      token: invitation.token,
      draftId: 'draft-b',
      consent: true,
    });
    assert.equal(wrong.receipt.status, 'target_mismatch');
    assert.equal(wrong.receipt.usable, false);

    await env.service.updateDraft(a.draft.id, { companionReceiptId: wrong.receipt.id });

    await assert.rejects(
      env.service.submitDraft(a.draft.id),
      (error) => error instanceof ServiceError
        && error.code === 'COMPANION_RECEIPT_FOR_OTHER_DRAFT',
    );

    const cls = await env.service.getClass('cls-wrong');
    assert.deepEqual(cls.class.availableStationIds, ['S1', 'S2', 'S3']);
    const orders = await env.service.listOrders();
    assert.equal(orders.orders.length, 0);
  } finally {
    await env.cleanup();
  }
});

test('solo walk-in taking S1 prevents pair even though S3 is free', async () => {
  const env = await makeService();
  try {
    await pairClass(env.service, 'cls-solo-first');
    const solo = await env.service.createDraft({
      id: 'draft-solo',
      classId: 'cls-solo-first',
      answers: soloAnswers,
    });
    const soloOrder = await env.service.submitDraft(solo.draft.id, { stationId: 'S1' });
    assert.equal(soloOrder.order.seats[0].stationId, 'S1');

    const pair = await confirmedPairDraft(env.service, 'cls-solo-first', 'draft-pair-blocked');
    await assert.rejects(
      env.service.submitDraft(pair.draft.id),
      (error) => error instanceof ServiceError && error.code === 'PAIR_STATIONS_UNAVAILABLE',
    );

    const cls = await env.service.getClass('cls-solo-first');
    assert.deepEqual(cls.class.availableStationIds, ['S2', 'S3']);
    assert.equal(cls.class.pairGroups[0].canBookPair, false);
    assert.equal((await env.service.listOrders()).orders.length, 1);
  } finally {
    await env.cleanup();
  }
});

test('hidden and stale conditional answers are not effective after changing trigger', async () => {
  const env = await makeService();
  try {
    await pairClass(env.service, 'cls-stale');
    const created = await env.service.createDraft({
      id: 'draft-stale',
      classId: 'cls-stale',
      answers: pairAnswers,
    });
    assert.equal(created.draft.effectiveAnswers.partnerName, '小校');

    const hidden = await env.service.updateDraft('draft-stale', { attendanceMode: 'solo' });
    assert.equal(hidden.draft.effectiveAnswers.partnerName, undefined);
    assert.ok(hidden.draft.validation.staleAnswers.some((x) => x.key === 'partnerName'));

    const reshown = await env.service.updateDraft('draft-stale', { attendanceMode: 'pair' });
    assert.equal(reshown.draft.effectiveAnswers.partnerName, undefined);
    const requiredKeys = reshown.draft.validation.errors.map((e) => e.key);
    assert.ok(requiredKeys.includes('partnerName'));
    assert.ok(requiredKeys.includes('partnerContact'));
    assert.ok(requiredKeys.includes('pairConsent'));

    await assert.rejects(
      env.service.submitDraft('draft-stale'),
      (error) => error.code === 'FORM_VALIDATION_FAILED',
    );

    const newlyWritten = await env.service.updateDraft('draft-stale', { partnerName: '新同伴' });
    assert.equal(newlyWritten.draft.effectiveAnswers.partnerName, '新同伴');
    assert.equal(newlyWritten.draft.effectiveAnswers.partnerContact, undefined);
  } finally {
    await env.cleanup();
  }
});

test('pinned old draft keeps v1 meaning after split publication; new drafts use v2', async () => {
  const env = await makeService();
  try {
    await pairClass(env.service, 'cls-form');
    const old = await env.service.createDraft({
      id: 'old-draft',
      classId: 'cls-form',
      formVersion: 1,
      answers: { guestName: '旧稿客人', visitedLab: true },
    });
    assert.equal(old.draft.formVersion, 1);
    assert.equal(old.draft.effectiveAnswers.visitedLab, true);

    const published = await env.service.publishSplitForm();
    assert.equal(published.form.version, 2);
    assert.ok(published.form.questions.some((q) => q.key === 'visitedOrientation'));
    assert.equal(published.form.questions.some((q) => q.key === 'visitedLab'), false);

    const continued = await env.service.updateDraft('old-draft', { contact: 'continue@example.test' });
    assert.equal(continued.draft.formVersion, 1);
    assert.equal(continued.draft.effectiveAnswers.visitedLab, true);
    assert.equal(continued.draft.effectiveAnswers.contact, 'continue@example.test');

    const next = await env.service.createDraft({ classId: 'cls-form' });
    assert.equal(next.draft.formVersion, 2);
    assert.equal(next.draft.form.questions.some((q) => q.key === 'visitedLab'), false);

    await assert.rejects(
      env.service.createDraft({
        classId: 'cls-form',
        answers: { visitedLab: true },
      }),
      (error) => error.code === 'UNKNOWN_QUESTION',
    );

    const splitAnswered = await env.service.createDraft({
      classId: 'cls-form',
      answers: { visitedOrientation: true, operatedStationBefore: false },
    });
    assert.equal(splitAnswered.draft.effectiveAnswers.visitedOrientation, true);
    assert.equal(splitAnswered.draft.effectiveAnswers.operatedStationBefore, false);
  } finally {
    await env.cleanup();
  }
});

test('changing companion target supersedes old invitation and requires reconfirmation', async () => {
  const env = await makeService();
  try {
    await pairClass(env.service, 'cls-change');
    await env.service.createDraft({ id: 'draft-change', classId: 'cls-change', answers: pairAnswers });
    const firstInvite = (await env.service.issueInvitation('draft-change')).invitation;

    const changed = await env.service.updateDraft('draft-change', { partnerContact: 'new-partner@example.test' });
    assert.equal(changed.draft.targetRevision, 2);
    const oldInvite = await env.service.getInvitation(firstInvite.id);
    assert.equal(oldInvite.invitation.status, 'superseded');

    const outdated = await env.service.confirmInvitation(firstInvite.id, {
      token: firstInvite.token,
      consent: true,
    });
    assert.equal(outdated.receipt.status, 'target_changed');

    const newInvite = (await env.service.issueInvitation('draft-change')).invitation;
    assert.notEqual(newInvite.id, firstInvite.id);
    const ok = await env.service.confirmInvitation(newInvite.id, {
      token: newInvite.token,
      consent: true,
      companionName: '小校',
    });
    assert.equal(ok.receipt.usable, true);

    const result = await env.service.submitDraft('draft-change');
    assert.equal(result.order.seats.length, 2);
  } finally {
    await env.cleanup();
  }
});

test('expired draft can be explained until expiry but can no longer be continued or submitted', async () => {
  const env = await makeService();
  try {
    await pairClass(env.service, 'cls-expiry');
    const created = await env.service.createDraft({
      classId: 'cls-expiry',
      ttlMs: -1000,
      answers: { guestName: '过期客人', visitedLab: true },
    });
    assert.equal(created.draft.expired, true);
    assert.equal(created.draft.effectiveAnswers.visitedLab, true);

    await assert.rejects(
      env.service.updateDraft(created.draft.id, { contact: 'late@example.test' }),
      (error) => error.code === 'DRAFT_EXPIRED',
    );
    await assert.rejects(
      env.service.submitDraft(created.draft.id),
      (error) => error.code === 'DRAFT_EXPIRED',
    );
  } finally {
    await env.cleanup();
  }
});

test('restart and repeated submission return only original two seats', async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'night-school-restart-'));
  const dataFile = path.join(dir, 'restart.json');
  try {
    const first = new NightSchoolService({ dataFile });
    await first.init();
    await first.createClass({ id: 'cls-restart', stationCount: 3, pairGroups: [['S1', 'S2']] });
    const pair = await confirmedPairDraft(first, 'cls-restart', 'draft-restart');
    const firstOrder = await first.submitDraft(pair.draft.id, { idempotencyKey: 'restart-key' });
    assert.equal(firstOrder.replayed, false);
    const originalOrderId = firstOrder.order.id;

    const restarted = new NightSchoolService({ dataFile });
    await restarted.init();
    const repeatSameKey = await restarted.submitDraft('draft-restart', { idempotencyKey: 'restart-key' });
    assert.equal(repeatSameKey.replayed, true);
    assert.equal(repeatSameKey.order.id, originalOrderId);

    const repeatWithoutKey = await restarted.submitDraft('draft-restart', {});
    assert.equal(repeatWithoutKey.replayed, true);
    assert.equal(repeatWithoutKey.order.id, originalOrderId);

    const orders = await restarted.listOrders();
    assert.equal(orders.orders.length, 1);
    assert.deepEqual(orders.orders[0].seats.map((s) => s.stationId), ['S1', 'S2']);

    const counter = await restarted.getOrder(originalOrderId);
    assert.equal(counter.counter.enrollmentNumbers.length, 2);
    const byEnrollment = await restarted.findByEnrollment(counter.enrollmentNumbers[1]);
    assert.equal(byEnrollment.id, originalOrderId);

    const cls = await restarted.getClass('cls-restart');
    assert.equal(cls.class.stations.filter((s) => s.occupancy).length, 2);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
