import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { ClubApiError, ClubClient } from '../clients/club-client.mjs';
import { boardSignature } from '../src/go.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');
const port = Number(process.env.DEMO_PORT || 4317);
const baseUrl = `http://127.0.0.1:${port}`;
const dataFile = process.env.DEMO_DATA || path.join(os.tmpdir(), `club-e2e-${process.pid}.json`);
const OLD_GROUP = 'white:1,1|1,2';
const MERGED_GROUP = 'white:1,1|1,2|2,2|3,2';

try { fs.rmSync(dataFile, { force: true }); } catch { /* ignore */ }

const black = new ClubClient(baseUrl, { prefix: 'black-player' });
const white = new ClubClient(baseUrl, { prefix: 'white-player' });
const club = new ClubClient(baseUrl, { prefix: 'club-client' });

let serverProcess;
let passed = 0;

function check(condition, message) {
  if (!condition) throw new Error(`断言失败：${message}`);
  passed += 1;
  console.log(`  ✅ ${message}`);
}

function section(title) {
  console.log(`\n=== ${title} ===`);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitForHealth() {
  const probe = new ClubClient(baseUrl, { prefix: 'health' });
  for (let i = 0; i < 80; i += 1) {
    try {
      await probe.health();
      return;
    } catch {
      await sleep(100);
    }
  }
  throw new Error('服务启动后健康检查一直失败');
}

function startServer() {
  serverProcess = spawn(process.execPath, [path.join(root, 'server.js')], {
    env: { ...process.env, PORT: String(port), HOST: '127.0.0.1', DATA_FILE: dataFile },
    stdio: ['ignore', 'inherit', 'inherit'],
  });
  return waitForHealth();
}

function stopServer() {
  return new Promise((resolve, reject) => {
    if (!serverProcess) return resolve();
    serverProcess.once('exit', () => resolve());
    serverProcess.kill('SIGTERM');
    const timer = setTimeout(() => {
      serverProcess.kill('SIGKILL');
      reject(new Error('服务未能在演示时限内关闭'));
    }, 3000);
    timer.unref();
  });
}

async function expectErrorCode(code, fn) {
  try {
    const value = await fn();
    throw new Error(`预期错误 ${code}，但请求成功：${JSON.stringify(value).slice(0, 200)}`);
  } catch (error) {
    if (error instanceof ClubApiError && error.code === code) {
      check(true, `服务按预期拒绝：${code} —— ${error.message}`);
      return error;
    }
    throw error;
  }
}

async function playCoreGame(gameId, finalMove = 'control') {
  await black.place(gameId, 'black', 2, 1, `${gameId}-m1-b21`);
  await white.place(gameId, 'white', 1, 1, `${gameId}-m2-w11`);
  await black.place(gameId, 'black', 1, 3, `${gameId}-m3-b13`);
  await white.place(gameId, 'white', 1, 2, `${gameId}-m4-w12`);
  await black.place(gameId, 'black', 2, 3, `${gameId}-m5-b23`);
  await white.place(gameId, 'white', 3, 2, `${gameId}-m6-w32`);
  await black.place(gameId, 'black', 5, 1, `${gameId}-m7-b51`);
  if (finalMove === 'merge') {
    await white.place(gameId, 'white', 2, 2, `${gameId}-m8-w22-merge`);
  } else {
    await white.place(gameId, 'white', 5, 5, `${gameId}-m8-w55-control`);
  }
}

async function main() {
  section('启动 HTTP 棋社服务');
  await startServer();
  check(fs.existsSync(dataFile), '服务使用 JSON 原子存档文件');

  section('1. 五路起势盘：黑下 (2,1) 只提左侧白子');
  await black.createGame({
    id: 'demo-capture',
    mode: 'practice',
    size: 5,
    players: { black: '黑方-测试', white: '白方-起势' },
    setup: [
      { x: 1, y: 1, color: 'white' },
      { x: 3, y: 1, color: 'white' },
      { x: 1, y: 2, color: 'black' },
    ],
  });
  const captureMove = await black.place('demo-capture', 'black', 2, 1, 'capture-b-2-1');
  check(captureMove.move.captured.length === 1, '只提掉一串、共 1 子');
  check(captureMove.move.captured[0].x === 1 && captureMove.move.captured[0].y === 1, '被提的是左侧白子 (1,1)');
  const captureGame = await black.getGame('demo-capture');
  check(captureGame.game.board[0] === null, '(1,1) 已为空');
  check(captureGame.game.board[1] === 'black', '(2,1) 为黑子');
  check(captureGame.game.board[2] === 'white', '右侧白子 (3,1) 未被提');

  section('2. 对照局：双方同意移去尚未被提的白串并正式入榜');
  await black.createGame({
    id: 'demo-control',
    mode: 'formal',
    size: 5,
    players: { black: '黑方-甲', white: '白方-乙' },
  });
  await playCoreGame('demo-control', 'control');
  const controlPassRequestId = 'control-black-pass-resend';
  const firstControlPass = await black.pass('demo-control', 'black', controlPassRequestId);
  const resentControlPass = await black.pass('demo-control', 'black', controlPassRequestId);
  check(firstControlPass.move.seq === 9, '第一手黑停着被受理为第 9 手');
  check(resentControlPass.idempotentReplay === true, '同一 requestId 重发停着只返回原受理结果');
  let gameResponse = await black.getGame('demo-control');
  check(gameResponse.game.moves.length === 9, '重发没有把同一端停着多记一手');
  await white.pass('demo-control', 'white', 'control-white-pass');
  gameResponse = await black.getGame('demo-control');
  check(gameResponse.game.status === 'scoring', '白方第二手停着后才进入终局处置');
  check(gameResponse.game.moves.length === 10, '局谱共 10 手（8 落子 + 2 停着）');

  const controlBlackMark = await black.markDead('demo-control', 'black', [{ x: 1, y: 1 }], 'control-black-mark');
  const controlWhiteMark = await white.markDead('demo-control', 'white', [{ x: 1, y: 2 }], 'control-white-mark');
  check(controlBlackMark.submission.groupKeys[0] === OLD_GROUP, '点 (1,1) 展开为整串 white:1,1|1,2');
  check(controlWhiteMark.submission.groupKeys[0] === OLD_GROUP, '点另一子 (1,2) 也代表同一整串');
  check(controlWhiteMark.markingState.consensus === true, '双方签认同一串为死串，意见一致');

  await club.suspend('demo-control', '入榜前模拟联络暂挂', 'league-recorder');
  await expectErrorCode('GAME_SUSPENDED', () => black.settle('demo-control', 'control-settle'));
  await club.resume('demo-control');
  const controlSettlement = await black.settle('demo-control', 'control-settle');
  check(controlSettlement.result.winner === 'black', '对照局计分结果为黑胜');
  check(controlSettlement.result.score.total.black === 4, '黑方总分 4（盘上 4 子，无独占空区）');
  check(controlSettlement.result.score.total.white === 2, '移去死串后白方总分 2（剩余 2 子）');
  check(controlSettlement.result.deadGroups[0].key === OLD_GROUP, '计分副本先移去共同认定的整串');
  const controlSettleReplay = await black.settle('demo-control', 'control-settle');
  check(controlSettleReplay.idempotentReplay === true, '重复结算请求不产生第二份联赛结果');

  section('3. 分开改收官一着：旧签认不能套到并入有气邻串的新串');
  await black.createGame({ id: 'demo-merge', mode: 'formal', size: 5, players: { black: '黑方-丙', white: '白方-丁' } });
  await playCoreGame('demo-merge', 'merge');
  await black.pass('demo-merge', 'black', 'merge-b-pass');
  await white.pass('demo-merge', 'white', 'merge-w-pass');
  const mergeMark = await black.markDead('demo-merge', 'black', [{ x: 1, y: 1 }], 'merge-black-mark');
  check(mergeMark.submission.groupKeys[0] === MERGED_GROUP, '点旧位置展开得到合并后的新整串');
  check(mergeMark.submission.groups[0].libertyCount === 3, '新串经邻串 (3,2) 保有 3 气，不是原两子串');

  const oldEndorsement = {
    boardSignature: controlWhiteMark.submission.boardSignature,
    groupKey: OLD_GROUP,
    receiptHash: controlWhiteMark.submission.receiptHash,
  };
  const verifyOnMerged = await black.verifyEndorsement('demo-merge', oldEndorsement);
  check(verifyOnMerged.applies === false, '对照局原签认对改着后的对局不生效');
  check(verifyOnMerged.reasons.includes('BOARD_SIGNATURE_MISMATCH'), '退回原因包含停局盘面指纹不同');
  check(verifyOnMerged.reasons.includes('GROUP_KEY_CHANGED_OR_MISSING'), '退回原因包含整串 key 已改变');
  await white.markDead('demo-merge', 'white', [], 'merge-white-empty');
  await expectErrorCode('DISPUTES_UNRESOLVED', () => black.settle('demo-merge', 'merge-copy-old-settlement'));

  section('4. 续弈场：指出作废旧意见的手，并过滤出下一次停局的真分歧');
  const continuationId = 'demo-continuation';
  await black.createGame({ id: continuationId, mode: 'formal', size: 5, players: { black: '黑方-续', white: '白方-弈' } });
  await playCoreGame(continuationId, 'control');
  await black.pass(continuationId, 'black', 'cont-initial-b-pass');
  await white.pass(continuationId, 'white', 'cont-initial-w-pass');
  await black.markDead(continuationId, 'black', [{ x: 1, y: 1 }, { x: 5, y: 5 }], 'cont-black-old-marks');
  await white.markDead(continuationId, 'white', [], 'cont-white-old-empty');
  let disputes = await black.disputes(continuationId);
  check(disputes.markingState.disputes.length === 2, '续弈前有两串形式分歧：角串和 (5,5) 子');
  await club.openContinuation(continuationId, '两串有异议，开续弈场');
  await black.pass(continuationId, 'black', 'cont-resume-b-pass');
  const mergingHand = await white.place(continuationId, 'white', 2, 2, 'cont-white-merging-hand');
  check(mergingHand.move.invalidatesOldOpinions === true, '服务明确标记这一着作废了绑定旧停局盘的意见');
  check(mergingHand.move.invalidatedOpinionIds.length === 2, '双方旧提交均被作废并留档');
  const oldLineage = mergingHand.move.groupLineage.find((item) => item.oldKey === OLD_GROUP);
  check(oldLineage?.newKey === MERGED_GROUP, '旧角串 lineage 指向合并后的新串');
  check(oldLineage?.state === 'merged_or_changed', 'lineage 状态为 merged_or_changed');
  await black.pass(continuationId, 'black', 'cont-after-b-pass');
  await white.pass(continuationId, 'white', 'cont-after-w-pass');
  disputes = await black.disputes(continuationId);
  check(disputes.markingState.complete === false, '新停局盘上旧意见不再充数，等待重新提交');
  check(disputes.markingState.disputes.length === 0, '未重签前不把已消失的旧串当现行分歧');
  await black.markDead(continuationId, 'black', [{ x: 5, y: 5 }], 'cont-black-remark-55');
  await white.markDead(continuationId, 'white', [], 'cont-white-remark-empty');
  disputes = await black.disputes(continuationId);
  check(disputes.markingState.disputes.length === 1, '重新提交后只剩一个真正分歧');
  check(disputes.markingState.disputes[0].key === 'white:5,5', '剩余真分歧是未被改着影响的 white:5,5');
  await white.markDead(continuationId, 'white', [{ x: 5, y: 5 }], 'cont-white-agree-55');
  const continuationResult = await black.settle(continuationId, 'cont-settle');
  check(continuationResult.result.deadGroups[0].key === 'white:5,5', '续弈后按新签认移去剩余一串并完整结算');

  section('5. 同一停局盘只改对方意见：不能沿用对照局结算');
  const disputeId = 'demo-dispute';
  await black.createGame({ id: disputeId, mode: 'formal', size: 5, players: { black: '黑方-裁', white: '白方-判' } });
  await playCoreGame(disputeId, 'control');
  const disputePassRequestId = 'dispute-black-pass-fixed-request';
  await black.pass(disputeId, 'black', disputePassRequestId);
  const disputePassReplay = await black.pass(disputeId, 'black', disputePassRequestId);
  check(disputePassReplay.idempotentReplay === true, '裁定局也模拟同一端重发停着');
  await white.pass(disputeId, 'white', 'dispute-white-pass-fixed');
  const disputeGameBefore = await black.getGame(disputeId);
  check(disputeGameBefore.game.stop.signature === controlSettlement.result.stoppedBoardSignature, '异议局与对照局是完全相同的停局盘面');
  await black.markDead(disputeId, 'black', [{ x: 1, y: 1 }], 'dispute-black-dead');
  await white.markDead(disputeId, 'white', [], 'dispute-white-changed-to-alive');
  disputes = await black.disputes(disputeId);
  check(disputes.markingState.disputes.length === 1 && disputes.markingState.disputes[0].key === OLD_GROUP, '只改对方意见后出现同一串死活分歧');
  await expectErrorCode('DISPUTES_UNRESOLVED', () => black.settle(disputeId, 'dispute-try-copy-control-result'));

  section('6. 封盘裁定：值班变动、回避、期限与两份生效意见');
  const opened = await club.openAdjudication(disputeId, { deadlineSeconds: 1, panelSize: 3 });
  const caseId = opened.adjudication.cases[0].id;
  check(Boolean(caseId), `已按争议串建立裁定案 ${caseId}`);
  check(opened.adjudication.cases[0].panelRefereeIds.join(',') === 'ref-zhang,ref-li,ref-qian', '开赛名单下初始小组为张、李、钱；王暂不值班，赵回避');

  await club.setDuty('ref-li', false, '李裁临时离岗且未交意见');
  await club.setDuty('ref-wang', true, '王裁开始值班并替补');

  const zhangVote = await club.vote(caseId, 'ref-zhang', 'remove', {
    reason: '角部两子只有一个共享气点，后续无法做两眼',
    requestId: 'fixed-vote-zhang',
  });
  check(zhangVote.evaluation.panelRefereeIds.includes('ref-wang'), '值班变动后小组已由王裁替补李裁');
  check(!zhangVote.evaluation.panelRefereeIds.includes('ref-li'), '未投票又离岗的李裁退出当前小组');

  await expectErrorCode('REFEREE_OFF_DUTY', () =>
    club.vote(caseId, 'ref-li', 'remove', { requestId: 'fixed-vote-li-after-duty' }),
  );
  await expectErrorCode('REFEREE_RECUSED', () =>
    club.vote(caseId, 'ref-zhao', 'remove', { requestId: 'fixed-vote-zhao-recused' }),
  );

  const wangVote = await club.vote(caseId, 'ref-wang', 'remove', {
    reason: '同意张裁判断，应移去该争议串',
    requestId: 'fixed-vote-wang',
  });
  check(wangVote.evaluation.resolution.state === 'removed', '两名无回避裁判同意后裁定移去');
  const effectiveIds = wangVote.evaluation.resolution.effectiveVoteIds;
  check(effectiveIds.length === 2, '明确只有两份意见作为生效配对');
  const effectiveVotes = wangVote.evaluation.votes.filter((v) => effectiveIds.includes(v.id));
  check(effectiveVotes.map((v) => v.refereeId).sort().join(',') === 'ref-wang,ref-zhang', '生效的是张、王两份意见');
  check(effectiveVotes.every((v) => v.groupKey === OLD_GROUP), '两份意见均对 white:1,1|1,2 这一串生效');

  await sleep(1150);
  const lateError = await expectErrorCode('OPINION_EXPIRED', () =>
    club.vote(caseId, 'ref-qian', 'remove', {
      reason: '网络延误送来的书面旧意见',
      oldOpinionId: 'paper-opinion-qian-001',
      requestId: 'fixed-vote-qian-late',
    }),
  );
  check(lateError.body.vote.status === 'late', '迟到旧意见留档为 late，但不生效');
  check(lateError.body.vote.clientOpinionId === 'paper-opinion-qian-001', '迟到意见保留其旧书面意见编号');

  const evaluated = await club.evaluateAdjudication(disputeId);
  const judgedCase = evaluated.adjudication.cases[0];
  check(judgedCase.resolution.effectiveVoteIds.join('|') === effectiveIds.join('|'), '期满复核后生效配对不变');
  check(judgedCase.votes.find((v) => v.refereeId === 'ref-qian').effective === false, '钱裁过期意见被标记为不生效');

  const disputeSettlement = await black.settle(disputeId, 'fixed-dispute-settlement');
  check(disputeSettlement.result.deadGroups.length === 1, '裁定后只移去目标争议串');
  check(disputeSettlement.result.deadGroups[0].basis.source === 'adjudication_remove', '计分依据写明来自裁定场');
  check(
    disputeSettlement.result.deadGroups[0].basis.effectiveVoteIds.join('|') === effectiveIds.join('|'),
    '结算结果绑定那两份生效意见',
  );

  section('6b. 意见分裂：两份生效意见不一致时按原样留盘');
  const splitId = 'demo-split';
  await black.createGame({ id: splitId, mode: 'formal', size: 5, players: { black: '黑方-分裂', white: '白方-留盘' } });
  await playCoreGame(splitId, 'control');
  await black.pass(splitId, 'black', 'split-b-pass');
  await white.pass(splitId, 'white', 'split-w-pass');
  await black.markDead(splitId, 'black', [{ x: 1, y: 1 }], 'split-b-mark');
  await white.markDead(splitId, 'white', [], 'split-w-mark');
  const splitOpened = await club.openAdjudication(splitId, { deadlineSeconds: 3600, panelSize: 2 });
  const splitCaseId = splitOpened.adjudication.cases[0].id;
  check(
    splitOpened.adjudication.cases[0].panelRefereeIds.join(',') === 'ref-zhang,ref-wang',
    '值班变动后的两人小组为张裁和已接班的王裁',
  );
  await club.vote(splitCaseId, 'ref-zhang', 'remove', { reason: '认为可移去', requestId: 'split-vote-zhang' });
  const splitKeep = await club.vote(splitCaseId, 'ref-wang', 'keep', { reason: '认为应留盘继续观察', requestId: 'split-vote-wang' });
  check(splitKeep.evaluation.resolution.state === 'left', '一 remove、一 keep，意见分裂，裁定留盘');
  check(splitKeep.evaluation.resolution.split === true, '裁定记录明确标注 split');
  check(splitKeep.evaluation.resolution.decidedBeforeDeadline === true, '两名所选裁判均已投票，不必等待期限');
  const splitSettlement = await black.settle(splitId, 'split-settle');
  check(splitSettlement.result.deadGroups.length === 0, '分裂后不移去任何争议串');
  check(splitSettlement.result.score.stones.white === 4, '原串仍在原始/计分盘上，白方保留 4 子');
  check(splitSettlement.result.winner === 'draw', '留盘后双方 4:4，记和');

  section('7. 联赛唯一结果、逐手局谱与 SGF');
  let results = await club.results();
  check(results.count === 4, `当前联赛恰有 4 盘正式结果（实际 ${results.count}）`);
  const idsBeforeRestart = results.results.map((r) => r.gameId);
  check(new Set(idsBeforeRestart).size === idsBeforeRestart.length, '联赛结果按对局号唯一');
  check(
    idsBeforeRestart.includes('demo-control') &&
      idsBeforeRestart.includes('demo-continuation') &&
      idsBeforeRestart.includes('demo-dispute') &&
      idsBeforeRestart.includes('demo-split'),
    '对照、续弈、裁定移除、分裂留盘四类正式处置均在榜',
  );

  const replay = await black.replay(disputeId);
  check(replay.totalSteps === 10, '异议局局谱可逐手重放 10 手');
  check(replay.steps.every((step) => Array.isArray(step.board) && step.board.length === 25), '每一手都带五路盘面快照');
  for (let step = 0; step <= replay.totalSteps; step += 1) {
    const state = await black.replay(disputeId, step);
    check(state.step === step && Array.isArray(state.board) && state.board.length === 25, `replay?step=${step} 可读取`);
  }
  const finalState = await black.replay(disputeId, 10);
  const liveGame = await black.getGame(disputeId);
  check(boardSignature(finalState.board, 5) === boardSignature(liveGame.game.board, 5), '重放终点与服务当前原始盘面一致');
  check(liveGame.game.result.deadGroups.length === 1, '结算记录包含一个在计分副本移去的整串');
  check(liveGame.game.board[0] === 'white' && liveGame.game.board[5] === 'white', '原始停局盘面和局谱仍保留该死串，可回看');
  const sgfResponse = await fetch(`${baseUrl}/api/games/demo-dispute/sgf`);
  const sgf = await sgfResponse.text();
  check(sgf.includes('SZ[5]') && sgf.includes(';B['), '同时提供可导出的 SGF 局谱');

  section('8. 关闭服务再打开：持久化、重发与不许多记一盘');
  await stopServer();
  await startServer();
  const health = await club.health();
  check(health.results === 4, '重启后从存档读回 4 份结果');

  const passAfterRestart = await black.pass(disputeId, 'black', disputePassRequestId);
  check(passAfterRestart.idempotentReplay === true, '重启后重发旧停着仍被幂等识别');
  const settleAfterRestart = await black.settle(disputeId, 'fixed-dispute-settlement');
  check(settleAfterRestart.idempotentReplay === true, '重启后重发结算仍返回原结果');
  const lateReplayError = await expectErrorCode('OPINION_EXPIRED', () =>
    club.vote(caseId, 'ref-qian', 'remove', {
      reason: '网络延误送来的书面旧意见',
      oldOpinionId: 'paper-opinion-qian-001',
      requestId: 'fixed-vote-qian-late',
    }),
  );
  check(lateReplayError.body.idempotentReplay === true, '重启后重投同一迟到意见返回存档原响应，不重复留档');

  const gameAfterRestart = await black.getGame(disputeId);
  check(gameAfterRestart.game.moves.length === 10, '幂等重发没有增加着手');
  const caseAfterRestart = gameAfterRestart.game.adjudication.cases[0];
  check(caseAfterRestart.votes.filter((v) => v.refereeId === 'ref-qian').length === 1, '同一份迟到意见重启后重发也不重复留档计数');

  results = await club.results();
  check(results.count === 4, `重启和重发后仍只有 4 盘结果（实际 ${results.count}）`);
  check(new Set(results.results.map((r) => r.gameId)).size === results.count, '联赛结果仍按对局号唯一');

  const standings = await club.standings();
  console.log('\n联赛榜：');
  for (const row of standings.standings) {
    console.log(
      `  ${row.player}: 积分${row.leaguePoints} ${row.wins}胜${row.draws}和${row.losses}负 ` +
        `得分${row.scoreFor} 失分${row.scoreAgainst} 净胜${row.scoreDifference}`,
    );
  }

  console.log(`\n🎉 端到端演示全部通过：${passed} 项断言；存档 ${dataFile}`);
}

main()
  .then(async () => {
    await stopServer().catch(() => {});
    process.exit(0);
  })
  .catch(async (error) => {
    console.error('\n❌ 演示失败：', error);
    try { await stopServer(); } catch { /* ignore */ }
    process.exit(1);
  });
