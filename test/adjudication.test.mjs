import test from 'node:test';
import assert from 'node:assert/strict';
import { applyHand, buildSettlement, createGame, submitMarking } from '../src/game.mjs';
import {
  defaultLeague,
  evaluateAdjudication,
  openAdjudication,
  setRefereeDuty,
  submitRefereeVote,
} from '../src/adjudication.mjs';

const OLD_GROUP = 'white:1,1|1,2';
const t0 = Date.parse('2026-11-26T10:00:00.000Z');
const iso = (offsetSeconds) => new Date(t0 + offsetSeconds * 1000).toISOString();

function makeDisputedTerminalGame(id = 'adj-unit') {
  const game = createGame({ id, mode: 'formal', now: iso(0) });
  const moves = [
    ['black', 2, 1], ['white', 1, 1], ['black', 1, 3], ['white', 1, 2],
    ['black', 2, 3], ['white', 3, 2], ['black', 5, 1], ['white', 5, 5],
  ];
  let second = 1;
  for (const [color, x, y] of moves) applyHand(game, { color, x, y, now: iso(second++) });
  applyHand(game, { color: 'black', type: 'pass', now: iso(second++) });
  applyHand(game, { color: 'white', type: 'pass', now: iso(second++) });
  submitMarking(game, { as: 'black', dead: [{ x: 1, y: 1 }], now: iso(second++) });
  submitMarking(game, { as: 'white', dead: [], now: iso(second++) });
  return game;
}

test('两名裁判意见分裂时期满按原样留盘并注明 split', () => {
  const game = makeDisputedTerminalGame('split-unit');
  const league = defaultLeague(iso(0));
  openAdjudication(game, league, { deadlineSeconds: 60, panelSize: 3, now: iso(10) });
  const caseId = game.adjudication.cases[0].id;

  const remove = submitRefereeVote(game, league, caseId, {
    refereeId: 'ref-zhang', decision: 'remove', now: iso(11),
  });
  const keep = submitRefereeVote(game, league, caseId, {
    refereeId: 'ref-li', decision: 'keep', now: iso(12),
  });
  assert.equal(remove.statusCode, 200);
  assert.equal(keep.statusCode, 200);

  evaluateAdjudication(game, league, { now: iso(71) });
  const kase = game.adjudication.cases[0];
  assert.equal(kase.resolution.state, 'left');
  assert.equal(kase.resolution.split, true);
  assert.equal(kase.resolution.effectiveVoteIds.length, 2);
  assert.match(kase.resolution.note, /意见分裂/);

  const result = buildSettlement(game, { now: iso(72) });
  assert.equal(result.deadGroups.length, 0);
  assert.equal(result.score.stones.white, 4, '争议串仍在计分副本盘上');
  assert.equal(result.score.total.black, result.score.total.white);
});

test('两名所选裁判一 remove 一 keep 且均已投票时，不必等期限即按分裂留盘', () => {
  const game = makeDisputedTerminalGame('split-panel-two');
  const league = defaultLeague(iso(0));
  openAdjudication(game, league, { deadlineSeconds: 3600, panelSize: 2, now: iso(10) });
  const caseId = game.adjudication.cases[0].id;
  submitRefereeVote(game, league, caseId, { refereeId: 'ref-zhang', decision: 'remove', now: iso(11) });
  const keep = submitRefereeVote(game, league, caseId, { refereeId: 'ref-li', decision: 'keep', now: iso(12) });
  assert.equal(keep.evaluation.resolution.state, 'left');
  assert.equal(keep.evaluation.resolution.split, true);
  assert.equal(keep.evaluation.resolution.decidedBeforeDeadline, true);
});

test('值班变动：投票时在岗且未回避的意见，不因投票后离岗而失效；投票时离岗则不生效', () => {
  const game = makeDisputedTerminalGame('duty-unit');
  const league = defaultLeague(iso(0));
  openAdjudication(game, league, { deadlineSeconds: 60, now: iso(10) });
  const caseId = game.adjudication.cases[0].id;

  const zhang = submitRefereeVote(game, league, caseId, {
    refereeId: 'ref-zhang', decision: 'remove', now: iso(11),
  });
  setRefereeDuty(league, 'ref-zhang', false, { now: iso(12), reason: '投票后退班' });
  setRefereeDuty(league, 'ref-li', false, { now: iso(10.5), reason: '投票前离岗' });

  const liOffDuty = submitRefereeVote(game, league, caseId, {
    refereeId: 'ref-li', decision: 'remove', now: iso(13),
  });
  assert.equal(liOffDuty.statusCode, 409);
  assert.equal(liOffDuty.body.error.code, 'REFEREE_OFF_DUTY');

  evaluateAdjudication(game, league, { now: iso(71) });
  const zhangVote = game.adjudication.cases[0].votes.find((v) => v.id === zhang.vote.id);
  assert.equal(zhangVote.effective, true);
  assert.ok(game.adjudication.cases[0].resolution.effectiveVoteIds.includes(zhang.vote.id));
});

test('过期意见标记 late，不进入两名 remove 生效配对', () => {
  const game = makeDisputedTerminalGame('late-unit');
  const league = defaultLeague(iso(0));
  openAdjudication(game, league, { deadlineSeconds: 10, panelSize: 3, now: iso(0) });
  const caseId = game.adjudication.cases[0].id;

  const zhang = submitRefereeVote(game, league, caseId, { refereeId: 'ref-zhang', decision: 'remove', now: iso(1) });
  setRefereeDuty(league, 'ref-li', false, { now: iso(1.5), reason: '李裁临时离岗' });
  setRefereeDuty(league, 'ref-wang', true, { now: iso(1.5), reason: '王裁接班' });

  const wangSeated = submitRefereeVote(game, league, caseId, { refereeId: 'ref-wang', decision: 'remove', now: iso(2) });
  assert.equal(wangSeated.statusCode, 200);
  assert.equal(wangSeated.evaluation.resolution.state, 'removed');
  assert.deepEqual(wangSeated.evaluation.resolution.effectiveVoteIds, [zhang.vote.id, wangSeated.vote.id]);

  const late = submitRefereeVote(game, league, caseId, {
    refereeId: 'ref-qian', decision: 'remove', now: iso(11), oldOpinionId: 'old-1',
  });
  assert.equal(late.statusCode, 409);
  assert.equal(late.body.error.code, 'OPINION_EXPIRED');
  assert.equal(late.vote.status, 'late');
  assert.equal(late.vote.clientOpinionId, 'old-1');
  assert.equal(game.adjudication.cases[0].resolution.effectiveVoteIds.length, 2);
  assert.equal(OLD_GROUP, 'white:1,1|1,2');
});
