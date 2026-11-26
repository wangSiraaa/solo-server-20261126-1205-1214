'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { CoListeningService } = require('../src/service');
const { logicalPrefixToIntervals, logicalRangeToIntervals } = require('../src/model');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function layoutOf(svc, roomId, memberId) {
  const room = svc._room(roomId);
  const member = svc._member(room, memberId);
  return svc._layout(member);
}

/** Report a continuous logical run [from,to) for a member on a version. */
function reportRange(svc, roomId, memberId, connId, version, fromLogical, toLogical, extra = {}) {
  const L = layoutOf(svc, roomId, memberId);
  const items = logicalRangeToIntervals(L.items, fromLogical, toLogical);
  return svc.report({ roomId, memberId, connId, manifestVersion: version, items, ...extra });
}

/** Report "played continuously from 0 up to N logical seconds". */
function reportPrefix(svc, roomId, memberId, connId, version, upto) {
  const L = layoutOf(svc, roomId, memberId);
  const items = logicalPrefixToIntervals(L.items, upto);
  return svc.report({ roomId, memberId, connId, manifestVersion: version, items });
}

function openConn(svc, roomId, members) {
  const conn = {};
  for (const m of members) {
    const id = `${m}-c1`;
    svc.connect({ roomId, memberId: m, connId: id });
    conn[m] = id;
  }
  return conn;
}

// Core episode from the brief: 正文甲 ten seconds, 乙 six seconds.
const EPISODE = [
  { segmentId: 'A', duration: 10 },
  { segmentId: 'B', duration: 6 },
];
// 甲→乙 seam carries a mandatory guide: 2s for p1, 5s for p2.
const ASSIGN = {
  p1: { 'seam:0': { guideId: 'gA-p1', duration: 2, mustListen: true } },
  p2: { 'seam:0': { guideId: 'gA-p2', duration: 5, mustListen: true } },
};

function makeRoom(rule = 'sync') {
  const svc = new CoListeningService();
  svc.createRoom({
    roomId: 'r',
    flow: EPISODE,
    rule,
    host: { id: 'host', name: 'Host' },
    members: [{ id: 'p1' }, { id: 'p2' }],
    assignmentsByMember: ASSIGN,
  });
  const conn = openConn(svc, 'r', ['host', 'p1', 'p2']);
  return { svc, conn };
}

// ---------------------------------------------------------------------------
// 1. The headline example: both at 13s, only p1 has heard 乙.
// ---------------------------------------------------------------------------

test('both at 13s: only p1 has heard part of B; discussion cannot open', () => {
  const { svc, conn } = makeRoom('sync');
  svc.setDiscussionPoint({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, segmentId: 'B', occurrence: 1 });

  // Both "pre-request" continuing to B.
  svc.requestPermit({ roomId: 'r', memberId: 'p1', connId: conn.p1, kind: 'continue' });
  svc.requestPermit({ roomId: 'r', memberId: 'p2', connId: conn.p2, kind: 'continue' });

  // Both play continuously from the start to their own 13th second.
  reportPrefix(svc, 'r', 'p1', conn.p1, 1, 13);
  reportPrefix(svc, 'r', 'p2', conn.p2, 1, 13);

  const p1pos = svc.progress({ roomId: 'r', memberId: 'p1' }).bodyPosition;
  const p2pos = svc.progress({ roomId: 'r', memberId: 'p2' }).bodyPosition;
  assert.equal(p1pos.segmentId, 'B');
  assert.equal(p1pos.offset, 1, 'p1 heard 1s of B');
  assert.equal(p2pos.segmentId, 'B');
  assert.equal(p2pos.offset, 0, 'p2 only reached the start of B');

  // They have NOT heard the same sentence.
  assert.notDeepEqual(
    { s: p1pos.segmentId, o: p1pos.offset },
    { s: p2pos.segmentId, o: p2pos.offset }
  );

  // Even though both requested early, the discussion point cannot be awarded.
  assert.throws(
    () => svc.openDiscussion({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host }),
    (e) => e.code === 'DISCUSSION_NOT_READY'
  );

  // The sync permits are still waiting, not granted.
  const permits = svc.permitsFor({ roomId: 'r' }).effective;
  assert.equal(permits.length, 0, 'no effective permit while round is not ready');

  const miss = svc.whoMissing({ roomId: 'r' });
  assert.deepEqual(miss.waitingOn.sort(), ['p1', 'p2']);
});

// ---------------------------------------------------------------------------
// 2. After both finish B the discussion opens.
// ---------------------------------------------------------------------------

test('after both fully hear B, discussion opens', () => {
  const { svc, conn } = makeRoom('sync');
  svc.setDiscussionPoint({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, segmentId: 'B', occurrence: 1 });
  reportPrefix(svc, 'r', 'p1', conn.p1, 1, 18); // whole p1 timeline
  reportPrefix(svc, 'r', 'p2', conn.p2, 1, 13);
  assert.throws(() => svc.openDiscussion({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host }), (e) => e.code === 'DISCUSSION_NOT_READY');
  reportPrefix(svc, 'r', 'p2', conn.p2, 1, 21); // whole p2 timeline
  const state = svc.openDiscussion({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host });
  assert.equal(state.status, 'open');
});

// ---------------------------------------------------------------------------
// 3. Mandatory guide gaps block readiness; dragging does not count as played.
// ---------------------------------------------------------------------------

test('a drag skipping 3s of a mandatory guide leaves a gap the room points out', () => {
  const { svc, conn } = makeRoom('sync');
  svc.setDiscussionPoint({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, segmentId: 'B', occurrence: 1 });

  // Both hear everything.
  reportPrefix(svc, 'r', 'p1', conn.p1, 1, 18);
  reportPrefix(svc, 'r', 'p2', conn.p2, 1, 21);
  // Discussion is now openable.
  assert.equal(svc.whoMissing({ roomId: 'r' }).waitingOn.length, 0);

  // Replace ONLY p2's report: a drag skipped 3s of their 5s mandatory guide.
  // p2 heard A fully, B fully, but of the guide only [0,1) and [4,5).
  const items = [
    { idx: 0, from: 0, to: 10 }, // A
    { idx: 1, from: 0, to: 1 }, // guide part 1
    { idx: 1, from: 4, to: 5 }, // guide part 2 (3s skipped by drag)
    { idx: 2, from: 0, to: 6 }, // B
  ];
  svc.report({ roomId: 'r', memberId: 'p2', connId: conn.p2, manifestVersion: 1, items, reset: true });

  const miss = svc.whoMissing({ roomId: 'r' });
  assert.deepEqual(miss.waitingOn, ['p2']);
  const p2 = miss.members.find((m) => m.memberId === 'p2');
  assert.equal(p2.ready.reason, 'MANDATORY_GUIDE_MISSING');
  assert.equal(p2.ready.missingGuides[0].guideId, 'gA-p2');
  // The gap is exactly 3 seconds.
  const gap = p2.ready.missingGuides[0].missingIntervals[0];
  assert.equal(gap.to - gap.from, 3);

  // Discussion must now be blocked.
  assert.throws(() => svc.openDiscussion({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host }), (e) => e.code === 'DISCUSSION_NOT_READY');
});

// ---------------------------------------------------------------------------
// 4. Old-version reports are accepted but never reused as new-version seconds.
// ---------------------------------------------------------------------------

test('after editing a guide, old reports are accepted but not treated as new-version progress', () => {
  const { svc, conn } = makeRoom('sync');
  // p1 listens to their v1 layout: A(0-10), guide 2s(10-12), B(12-18).
  reportPrefix(svc, 'r', 'p1', conn.p1, 1, 13); // heard A + full guide + 1s of B

  const before = svc.progress({ roomId: 'r', memberId: 'p1' });
  assert.equal(before.position, 13);
  assert.equal(before.bodyPosition.segmentId, 'B');
  assert.equal(before.bodyPosition.offset, 1);

  // Host replaces p1's guide with a 4s mandatory guide => version 2.
  svc.editManifest({
    roomId: 'r',
    actorMemberId: 'host',
    authorityEpoch: 1,
    connId: conn.host,
    memberId: 'p1',
    assignments: { 'seam:0': { guideId: 'gA-p1-new', duration: 4, mustListen: true } },
  });

  // An old (v1) report arriving now is still accepted...
  const res = reportPrefix(svc, 'r', 'p1', conn.p1, 1, 13);
  assert.equal(res.accepted, true);
  assert.equal(res.stale, true);
  assert.equal(res.manifestVersion, 1);
  assert.equal(res.currentVersion, 2);

  // ...but it must NOT make 13 the position in the new timeline.
  const after = svc.progress({ roomId: 'r', memberId: 'p1' });
  assert.equal(after.position, 10, 'position stops before the not-yet-heard new guide');
  assert.notEqual(after.position, 13, 'old seconds are not reused as new progress');
  // Body identity persists: B#1 still at offset 1.
  assert.equal(after.bodyPosition.segmentId, 'B');
  assert.equal(after.bodyPosition.offset, 1);
  // The new (4s) guide has 0 coverage and is mandatory => it is missing.
  const guide = after.missing.find((m) => m.kind === 'guide');
  assert.ok(guide, 'new guide shows as missing');
  assert.equal(guide.mustListen, true);
});

// ---------------------------------------------------------------------------
// 5. Repeated segment in one episode; discussion binds to one occurrence.
// ---------------------------------------------------------------------------

test('a repeated segment: hearing occurrence 1 does not satisfy a point bound to occurrence 2', () => {
  const svc = new CoListeningService();
  svc.createRoom({
    roomId: 'rr',
    flow: [
      { segmentId: 'A', duration: 10 },
      { segmentId: 'B', duration: 6 },
      { segmentId: 'A', duration: 10 }, // A appears twice
    ],
    rule: 'sync',
    host: { id: 'host' },
    members: [{ id: 'p1' }],
    assignmentsByMember: { p1: {} },
  });
  const conn = openConn(svc, 'rr', ['host', 'p1']);
  // Bind discussion to the SECOND occurrence of A.
  svc.setDiscussionPoint({ roomId: 'rr', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, segmentId: 'A', occurrence: 2 });

  const L = layoutOf(svc, 'rr', 'p1');
  const A1 = L.items.find((i) => i.kind === 'main' && i.segmentId === 'A' && i.occurrence === 1);
  const A2 = L.items.find((i) => i.kind === 'main' && i.segmentId === 'A' && i.occurrence === 2);
  assert.ok(A1 && A2 && A1.idx !== A2.idx);

  // Play only the first occurrence of A (and everything before it).
  svc.report({ roomId: 'rr', memberId: 'p1', connId: conn.p1, manifestVersion: 1, items: [{ idx: A1.idx, from: 0, to: 10 }] });

  const miss = svc.whoMissing({ roomId: 'rr' });
  assert.equal(miss.waitingOn.includes('p1'), true, 'occurrence-1 playback does not satisfy occurrence-2 point');
  assert.equal(miss.members[0].ready.occurrence, 2);

  // Now play the second occurrence.
  svc.report({ roomId: 'rr', memberId: 'p1', connId: conn.p1, manifestVersion: 1, items: [{ idx: A2.idx, from: 0, to: 10 }] });
  assert.deepEqual(svc.whoMissing({ roomId: 'rr' }).waitingOn, []);
});

// ---------------------------------------------------------------------------
// 6. Per-member guide length changes readiness threshold.
// ---------------------------------------------------------------------------

test('middle-of-guide at 13s means different body offsets per member', () => {
  const { svc, conn } = makeRoom('sync');
  reportPrefix(svc, 'r', 'p1', conn.p1, 1, 13);
  reportPrefix(svc, 'r', 'p2', conn.p2, 1, 13);
  const p1 = svc.progress({ roomId: 'r', memberId: 'p1' });
  const p2 = svc.progress({ roomId: 'r', memberId: 'p2' });
  // p1's guide is 2s so at 13s they are 1s into B; p2's guide is 5s so at 13s they are still 3s before B.
  assert.deepEqual(
    { seg: p1.bodyPosition.segmentId, off: p1.bodyPosition.offset },
    { seg: 'B', off: 1 }
  );
  assert.deepEqual(
    { seg: p2.bodyPosition.segmentId, off: p2.bodyPosition.offset },
    { seg: 'B', off: 0 }
  );
});

// ---------------------------------------------------------------------------
// 7. Sync rule: fast listener stops at the point; permit waits; defer to next round.
// ---------------------------------------------------------------------------

test('sync rule: a continue permit is granted only when the whole round is ready', () => {
  const { svc, conn } = makeRoom('sync');
  svc.setDiscussionPoint({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, segmentId: 'B', occurrence: 1 });

  const permit = svc.requestPermit({ roomId: 'r', memberId: 'p1', connId: conn.p1, kind: 'continue' });
  assert.equal(permit.status, 'waiting');

  reportPrefix(svc, 'r', 'p1', conn.p1, 1, 18); // p1 fully ready
  let effective = svc.permitsFor({ roomId: 'r', memberId: 'p1' }).effective;
  assert.equal(effective.length, 0, 'still waiting on p2');

  reportPrefix(svc, 'r', 'p2', conn.p2, 1, 21); // p2 catches up
  effective = svc.permitsFor({ roomId: 'r', memberId: 'p1' }).effective;
  assert.equal(effective.length, 1);
  assert.equal(effective[0].status, 'granted');
  assert.equal(effective[0].reason, 'ALL_ROUND_MEMBERS_READY');
});

test('sync rule: host defers an away member to next round and tells them how to catch up', () => {
  const { svc, conn } = makeRoom('sync');
  svc.setDiscussionPoint({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, segmentId: 'B', occurrence: 1 });
  reportPrefix(svc, 'r', 'p1', conn.p1, 1, 18); // p1 ready
  reportPrefix(svc, 'r', 'p2', conn.p2, 1, 13); // p2 stuck before B

  // p2 leaves.
  svc.leave({ roomId: 'r', memberId: 'p2' });

  const deferred = svc.deferMember({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, memberId: 'p2' });
  assert.ok(deferred.round.nextRound.includes('p2'));
  assert.ok(!deferred.round.round.includes('p2'));
  assert.equal(deferred.catchUp.memberId, 'p2');
  assert.equal(deferred.catchUp.bodyPosition.segmentId, 'B');
  // Catch-up tells them where to resume and what is still missing.
  assert.ok(deferred.catchUp.resumeAt >= 0);
  assert.ok(Array.isArray(deferred.catchUp.missing));

  // Now the round (just p1) is ready => discussion can open.
  const state = svc.openDiscussion({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host });
  assert.equal(state.status, 'open');
  assert.deepEqual(state.round, ['p1']);

  // p2 rejoins and asks the server how to catch up.
  svc.join({ roomId: 'r', memberId: 'p2' });
  svc.connect({ roomId: 'r', memberId: 'p2', connId: 'p2-c2' });
  const snap = svc.snapshot({ roomId: 'r', memberId: 'p2', connId: 'p2-c2' });
  assert.ok(snap.catchUp, 'deferred member is told how to catch up on reconnect');
  assert.equal(snap.catchUp.bodyPosition.segmentId, 'B');
});

// ---------------------------------------------------------------------------
// 8. Early rule: prelisten ahead, return to the point, then restore.
// ---------------------------------------------------------------------------

test('early rule: prelisten ahead, must return to the discussion body, then restore prelisten progress', () => {
  // Rebuild room with a longer flow so there is something past B to prelisten.
  const svc2 = new CoListeningService();
  svc2.createRoom({
    roomId: 'e',
    flow: [
      { segmentId: 'A', duration: 10 },
      { segmentId: 'B', duration: 6 },
      { segmentId: 'C', duration: 8 },
    ],
    rule: 'early',
    host: { id: 'host' },
    members: [{ id: 'p1' }, { id: 'p2' }],
    assignmentsByMember: ASSIGN,
  });
  const c = openConn(svc2, 'e', ['host', 'p1', 'p2']);
  svc2.setDiscussionPoint({ roomId: 'e', actorMemberId: 'host', authorityEpoch: 1, connId: c.host, segmentId: 'B', occurrence: 1 });

  // p1 (fast) prelistens ahead: requests prelisten permit (granted immediately).
  const permit = svc2.requestPermit({ roomId: 'e', memberId: 'p1', connId: c.p1, kind: 'prelisten' });
  assert.equal(permit.status, 'granted');

  // p1 plays their entire timeline (A, guide, B, C): they ran ahead past B.
  reportPrefix(svc2, 'e', 'p1', c.p1, 1, 24); // p1 layout duration = 10+2+6+8 = 26? see below
  // p2 (slow) only reaches B fully, not C.
  const L2 = layoutOf(svc2, 'e', 'p2'); // p2: A(10)+guide5(5)+B(6)+C(8)=29
  // Play up through B for p2.
  const bEnd = L2.items.find((i) => i.segmentId === 'B').logicalEnd;
  reportPrefix(svc2, 'e', 'p2', c.p2, 1, bEnd);
  assert.equal(svc2.whoMissing({ roomId: 'e' }).waitingOn.length, 0, 'both heard B');

  const state = svc2.openDiscussion({ roomId: 'e', actorMemberId: 'host', authorityEpoch: 1, connId: c.host });
  assert.equal(state.status, 'open');
  // Find p1's directive from the discussion view.
  const p1Directive = svc2.discussionState({ roomId: 'e', memberId: 'p1' }).yourDirective;
  assert.equal(p1Directive.ranAhead, true);
  assert.equal(p1Directive.returnToSegment.segmentId, 'B');
  // Must be able to return to the B body.
  assert.ok(typeof p1Directive.returnTo === 'number');
  // Prelisten progress preserved.
  assert.ok(p1Directive.resumeAfterDiscussion > p1Directive.returnTo);

  const ended = svc2.endDiscussion({ roomId: 'e', actorMemberId: 'host', authorityEpoch: 1, connId: c.host });
  assert.equal(ended.restores.p1.prelistened, true);
  assert.equal(ended.restores.p1.resumeAt, p1Directive.resumeAfterDiscussion);
  // p2 did not run ahead, resumes at the discussion body.
  assert.equal(ended.restores.p2.prelistened, false);
});

test('sync rule denies prelisten', () => {
  const { svc, conn } = makeRoom('sync');
  const permit = svc.requestPermit({ roomId: 'r', memberId: 'p1', connId: conn.p1, kind: 'prelisten' });
  assert.equal(permit.status, 'denied');
  assert.equal(permit.reason, 'RULE_SYNC_NO_PRELISTEN');
});

// ---------------------------------------------------------------------------
// 9. Newly added third member must be listened to before the round opens.
// ---------------------------------------------------------------------------

test('restore logs, then adding a third member makes the round wait for them', () => {
  const { svc, conn } = makeRoom('sync');
  svc.setDiscussionPoint({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, segmentId: 'B', occurrence: 1 });

  // Both finish 乙 -> openable.
  reportPrefix(svc, 'r', 'p1', conn.p1, 1, 18);
  reportPrefix(svc, 'r', 'p2', conn.p2, 1, 21);
  assert.deepEqual(svc.whoMissing({ roomId: 'r' }).waitingOn, []);

  // ...then p2's report is replaced by a drag that skipped 3s of the guide.
  svc.report({
    roomId: 'r', memberId: 'p2', connId: conn.p2, manifestVersion: 1, reset: true,
    items: [
      { idx: 0, from: 0, to: 10 },
      { idx: 1, from: 0, to: 1 },
      { idx: 1, from: 4, to: 5 },
      { idx: 2, from: 0, to: 6 },
    ],
  });
  assert.deepEqual(svc.whoMissing({ roomId: 'r' }).waitingOn, ['p2']);

  // Restore the playback records (p2 replays and reports fully again).
  reportPrefix(svc, 'r', 'p2', conn.p2, 1, 21);
  assert.deepEqual(svc.whoMissing({ roomId: 'r' }).waitingOn, [], 'gap restored');

  // Now a third member joins (not listened), and the host widens the scope.
  svc.join({ roomId: 'r', memberId: 'p3', assignments: ASSIGN.p1 });
  svc.connect({ roomId: 'r', memberId: 'p3', connId: 'p3-c1' });

  let miss = svc.whoMissing({ roomId: 'r' });
  assert.ok(!miss.round.includes('p3'), 'new member not auto-added');
  svc.setRound({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, memberIds: ['p1', 'p2', 'p3'] });

  miss = svc.whoMissing({ roomId: 'r' });
  assert.deepEqual(miss.waitingOn, ['p3']);
  assert.throws(() => svc.openDiscussion({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host }), (e) => e.code === 'DISCUSSION_NOT_READY');

  // Once p3 listens, discussion can open.
  reportPrefix(svc, 'r', 'p3', 'p3-c1', 1, 18);
  assert.deepEqual(svc.whoMissing({ roomId: 'r' }).waitingOn, []);
  assert.equal(svc.openDiscussion({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host }).status, 'open');
});

// ---------------------------------------------------------------------------
// 10. Authority transfer: stale epoch rejected; reconnected snapshot is truthful.
// ---------------------------------------------------------------------------

test('authority transfer invalidates the old epoch and the old host cannot open', () => {
  const { svc, conn } = makeRoom('sync');
  svc.setDiscussionPoint({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, segmentId: 'B', occurrence: 1 });
  reportPrefix(svc, 'r', 'p1', conn.p1, 1, 18);
  reportPrefix(svc, 'r', 'p2', conn.p2, 1, 21);

  const t = svc.transferAuthority({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, toMemberId: 'p1' });
  assert.equal(t.authorityEpoch, 2);
  assert.equal(t.hostMemberId, 'p1');

  // Old host, whose connection dropped, sends a late open request with epoch 1.
  svc.disconnect({ roomId: 'r', memberId: 'host', connId: conn.host });
  assert.throws(
    () => svc.openDiscussion({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host }),
    (e) => e.code === 'STALE_AUTHORITY'
  );

  // New host queries who is missing and opens properly.
  const miss = svc.whoMissing({ roomId: 'r' });
  assert.deepEqual(miss.waitingOn, []);
  const state = svc.openDiscussion({ roomId: 'r', actorMemberId: 'p1', authorityEpoch: 2, connId: conn.p1 });
  assert.equal(state.status, 'open');

  // Old epoch still cannot open again (nor end).
  assert.throws(() => svc.endDiscussion({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host }), (e) => e.code === 'STALE_AUTHORITY');
  // New host cannot re-open an already-open discussion.
  assert.throws(() => svc.openDiscussion({ roomId: 'r', actorMemberId: 'p1', authorityEpoch: 2, connId: conn.p1 }), (e) => e.code === 'DISCUSSION_ALREADY_OPEN');
});

test('reconnect reads back the truly effective permit, not the raw request', () => {
  const { svc, conn } = makeRoom('sync');
  svc.setDiscussionPoint({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, segmentId: 'B', occurrence: 1 });
  reportPrefix(svc, 'r', 'p1', conn.p1, 1, 18);
  reportPrefix(svc, 'r', 'p2', conn.p2, 1, 21);
  svc.requestPermit({ roomId: 'r', memberId: 'p1', connId: conn.p1, kind: 'continue' });

  // p1 drops and reconnects with the same connection id.
  svc.disconnect({ roomId: 'r', memberId: 'p1', connId: conn.p1 });
  const snap = svc.connect({ roomId: 'r', memberId: 'p1', connId: conn.p1 });
  assert.equal(snap.reconnected, true);
  assert.equal(snap.effectivePermits.length, 1);
  assert.equal(snap.effectivePermits[0].status, 'granted');
  assert.equal(snap.relisten.position, 18);

  // Now p2's coverage is invalidated: p2's report is replaced by one missing B.
  svc.report({ roomId: 'r', memberId: 'p2', connId: conn.p2, manifestVersion: 1, items: [{ idx: 0, from: 0, to: 10 }], reset: true });
  // p1 reconnects again: the previously "granted" permit is no longer effective.
  const snap2 = svc.snapshot({ roomId: 'r', memberId: 'p1', connId: conn.p1 });
  assert.equal(snap2.effectivePermits.length, 0);
  const raw = svc.permitsFor({ roomId: 'r', memberId: 'p1' }).permits;
  assert.equal(raw[0].status, 'revoked', 'raw record keeps the decision, for audit');
  assert.equal(raw[0].reason, 'ROUND_NOT_READY');
  const revoked = svc.audit({ roomId: 'r' }).events.find((e) => e.type === 'permit_revoked');
  assert.ok(revoked, 'revocation is audited');
});

// ---------------------------------------------------------------------------
// 11. Reports are validated against connection and manifest.
// ---------------------------------------------------------------------------

test('reports require a live connection and valid item indices', () => {
  const { svc, conn } = makeRoom('sync');
  assert.throws(
    () => svc.report({ roomId: 'r', memberId: 'p1', connId: 'never-opened', manifestVersion: 1, items: [] }),
    (e) => e.code === 'UNKNOWN_CONNECTION'
  );
  assert.throws(
    () => svc.report({ roomId: 'r', memberId: 'p1', connId: conn.p1, manifestVersion: 1, items: [{ idx: 99, from: 0, to: 1 }] }),
    (e) => e.code === 'BAD_REQUEST'
  );
  // After a disconnect the connection is closed and cannot report.
  svc.disconnect({ roomId: 'r', memberId: 'p1', connId: conn.p1 });
  assert.throws(
    () => svc.report({ roomId: 'r', memberId: 'p1', connId: conn.p1, manifestVersion: 1, items: [] }),
    (e) => e.code === 'CONNECTION_CLOSED'
  );
});

// ---------------------------------------------------------------------------
// 12. Audit log records every permission decision.
// ---------------------------------------------------------------------------

test('every permit decision is auditable', () => {
  const { svc, conn } = makeRoom('sync');
  svc.setDiscussionPoint({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, segmentId: 'B', occurrence: 1 });
  svc.requestPermit({ roomId: 'r', memberId: 'p1', connId: conn.p1, kind: 'continue' });
  reportPrefix(svc, 'r', 'p1', conn.p1, 1, 18);
  reportPrefix(svc, 'r', 'p2', conn.p2, 1, 21);
  const log = svc.audit({ roomId: 'r' });
  const types = log.events.map((e) => e.type);
  assert.ok(types.includes('permit_requested'));
  assert.ok(types.includes('permit_granted'));
  const granted = log.events.find((e) => e.type === 'permit_granted');
  assert.equal(granted.memberId, 'p1');
});

// ---------------------------------------------------------------------------
// 13. Leave and rejoin preserve progress and manifest version.
// ---------------------------------------------------------------------------

test('leave and rejoin preserve progress, reports and manifest version', () => {
  const { svc, conn } = makeRoom('sync');
  reportPrefix(svc, 'r', 'p1', conn.p1, 1, 13);
  const before = svc.progress({ roomId: 'r', memberId: 'p1' });

  svc.leave({ roomId: 'r', memberId: 'p1' });
  // Rejoining without new assignments keeps the same version + progress.
  const rejoined = svc.join({ roomId: 'r', memberId: 'p1' });
  assert.equal(rejoined.rejoined, true);
  assert.equal(rejoined.member.manifestVersion, 1);
  const after = svc.progress({ roomId: 'r', memberId: 'p1' });
  assert.equal(after.position, before.position);
  assert.equal(after.bodyPosition.segmentId, before.bodyPosition.segmentId);
  assert.equal(after.bodyPosition.offset, before.bodyPosition.offset);

  // Rejoining with new assignments bumps version but body identity persists.
  const rejoined2 = svc.join({
    roomId: 'r', memberId: 'p1',
    assignments: { 'seam:0': { guideId: 'g-new', duration: 3, mustListen: true } },
  });
  assert.equal(rejoined2.member.manifestVersion, 2);
  const after2 = svc.progress({ roomId: 'r', memberId: 'p1' });
  assert.equal(after2.bodyPosition.offset, before.bodyPosition.offset);
});

// ---------------------------------------------------------------------------
// 14. Changing the discussion point invalidates permits bound to the old one.
// ---------------------------------------------------------------------------

test('a permit bound to the old discussion point becomes stale when the point changes', () => {
  const svc = new CoListeningService();
  svc.createRoom({
    roomId: 'q',
    flow: [
      { segmentId: 'A', duration: 10 },
      { segmentId: 'B', duration: 6 },
      { segmentId: 'C', duration: 6 },
    ],
    rule: 'sync',
    host: { id: 'host' },
    members: [{ id: 'p1' }],
    assignmentsByMember: { p1: {} },
  });
  const conn = openConn(svc, 'q', ['host', 'p1']);
  svc.setDiscussionPoint({ roomId: 'q', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, segmentId: 'B', occurrence: 1 });
  reportPrefix(svc, 'q', 'p1', conn.p1, 1, 16); // through B
  svc.requestPermit({ roomId: 'q', memberId: 'p1', connId: conn.p1, kind: 'continue' });
  assert.equal(svc.permitsFor({ roomId: 'q', memberId: 'p1' }).effective.length, 1);

  // Move the discussion point to C: the old permit no longer applies.
  svc.setDiscussionPoint({ roomId: 'q', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, segmentId: 'C', occurrence: 1 });
  const p = svc.permitsFor({ roomId: 'q', memberId: 'p1' });
  assert.equal(p.effective.length, 0);
  assert.equal(p.stale.length, 1);
  assert.equal(p.stale[0].staleReason, 'POINT_CHANGED');
});

// ---------------------------------------------------------------------------
// 15. Early rule: the slow member's own position is not disturbed by a fast peer.
// ---------------------------------------------------------------------------

test('early rule: a fast peer prelistening does not move the slow member', () => {
  const svc = new CoListeningService();
  svc.createRoom({
    roomId: 'e2',
    flow: [
      { segmentId: 'A', duration: 10 },
      { segmentId: 'B', duration: 6 },
      { segmentId: 'C', duration: 8 },
    ],
    rule: 'early',
    host: { id: 'host' },
    members: [{ id: 'fast' }, { id: 'slow' }],
    assignmentsByMember: { fast: {}, slow: {} },
  });
  const conn = openConn(svc, 'e2', ['host', 'fast', 'slow']);
  // fast requests prelisten (granted immediately), plays through C.
  const perm = svc.requestPermit({ roomId: 'e2', memberId: 'fast', connId: conn.fast, kind: 'prelisten' });
  assert.equal(perm.status, 'granted');
  reportPrefix(svc, 'e2', 'fast', conn.fast, 1, 18); // into C
  reportPrefix(svc, 'e2', 'slow', conn.slow, 1, 10); // only through A
  const fast = svc.progress({ roomId: 'e2', memberId: 'fast' });
  const slow = svc.progress({ roomId: 'e2', memberId: 'slow' });
  assert.equal(fast.bodyPosition.segmentId, 'C');
  assert.equal(slow.bodyPosition.segmentId, 'B');
  assert.equal(slow.bodyPosition.offset, 0);
});

// ---------------------------------------------------------------------------
// 16. Defer everyone out of the round: the emptied round can proceed at once.
// ---------------------------------------------------------------------------

test('an emptied round (all deferred) can open the discussion at once', () => {
  const { svc, conn } = makeRoom('sync');
  svc.setDiscussionPoint({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, segmentId: 'B', occurrence: 1 });
  svc.deferMember({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, memberId: 'p1' });
  svc.deferMember({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host, memberId: 'p2' });
  const state = svc.openDiscussion({ roomId: 'r', actorMemberId: 'host', authorityEpoch: 1, connId: conn.host });
  assert.equal(state.status, 'open');
  assert.deepEqual(state.round, []);
  assert.deepEqual(state.nextRound.sort(), ['p1', 'p2']);
});
