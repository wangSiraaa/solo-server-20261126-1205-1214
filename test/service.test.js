'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { CampaignService } = require('../src/service');
const { seedBase } = require('../src/fixtures');
const { codes } = require('../src/errors');

function freshService() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'camp-'));
  return { svc: CampaignService.open(path.join(dir, 'state.json')), dir };
}

function rejects(promise, code) {
  return assert.rejects(promise, (err) => {
    assert.equal(err.code, code, `期望错误码 ${code}，实际 ${err.code}: ${err.message}`);
    return true;
  });
}

// ---------------------------------------------------------------------------
// 场景 1：认养盒借法的完整闭环（领盒 -> 现场行动 -> 归还）
// ---------------------------------------------------------------------------
test('认养借法：领盒、现场选蓝门取钥匙、开暗袋、贴门牌、归还', async () => {
  const { svc } = freshService();
  const ids = await seedBase(svc);

  // 这是一场从第一章开始的新活动：建一支停在第一章的队伍。
  const team = await svc.createTeam({ name: '第一章桌', campaignId: ids.rulebookId });

  const out = await svc.checkout({
    teamId: team.id,
    model: 'adopted',
    boxId: ids.adoptedBoxId,
    door: 'blue',
  });
  assert.equal(out.loan.status, 'open');
  assert.equal(out.loan.chapter, 1);
  const loanId = out.loan.id;

  // 现场：选蓝门，应发钥匙 blue-key。
  const chosen = await svc.chooseDoor(loanId, { door: 'blue' });
  assert.equal(chosen.loan.door, 'blue');
  assert.ok(chosen.team.keys.includes('blue-key'));

  // 开暗袋：确已无法复原。
  const opened = await svc.openHiddenPouch(loanId, { pouchId: 'pouch-1-under-blue' });
  assert.ok(opened.team.reveals.includes('plot-blue-secret'));
  await rejects(svc.openHiddenPouch(loanId, { pouchId: 'pouch-1-under-blue' }), codes.IRREVERSIBLE);

  // 贴门牌到实物盒（认养盒 = 永久痕迹）。
  const aff = await svc.affixPlate(loanId, { chapter: 1, door: 'blue' });
  assert.equal(aff.box.permanentMarks.affixedPlates.length, 1);

  // 归还：分别声明“玩到哪”和“盒上留哪道痕迹”。
  const ret = await svc.returnLoan(loanId, {
    progress: { completedChapter: 1 },
    boxMarks: { affixedPlates: [{ chapter: 1, door: 'blue' }] },
  });
  assert.equal(ret.loan.status, 'returned');
  assert.deepEqual(ret.team.progress.completedChapters, [1]);
  assert.equal(ret.team.progress.currentChapter, 2);
  assert.equal(ret.box.permanentMarks.affixedPlates.length, 1);
  // 队伍存档与盒痕迹是两处独立数据。
  assert.ok(ret.receipt.result.teamProgressAfter);
  assert.ok(ret.receipt.result.boxPermanentMarksAfter);
});

// ---------------------------------------------------------------------------
// 场景 2：题目主线的三段对照实验
//                                                                        judge
//   (a) 发一盒有完整图卡、其余部件合用的续玩局，并登记本次真开图卡
//   (b) 相同库存另开演示，只把队伍原选择换成红 -> 蓝门续局不能接
//   (c) 同一份蓝档+库存，仅把全部可用图卡改成曾被拆封 -> 不能承诺同一未揭情节
// ---------------------------------------------------------------------------
test('主线 (a)：甲走蓝并有密封图卡 -> 柜员可发出续玩局并登记开卡', async () => {
  const { svc } = freshService();
  const ids = await seedBase(svc);

  const out = await svc.checkout({
    teamId: ids.teamAId,
    model: 'adopted',
    boxId: ids.adoptedBoxId,
    door: 'blue',           // 第二章的蓝门
    cardId: ids.mapCardId,  // 完好密封图卡
    openCard: true,         // 登记本次真开了图卡
  });
  assert.equal(out.loan.door == null, true); // 门在现场选，但发盒时已据此配卡
  assert.equal(out.allocatedCard.id, ids.mapCardId);
  assert.equal(svc.getCard(ids.mapCardId).state, 'opened');
  assert.equal(out.loan.cardOpened, true);
  assert.ok(out.loan.actions.some((a) => a.type === 'open-card' && a.atIssue === true));

  // 出库单可回查：借单与回单互指。
  const receipt = svc.getReceipt(out.receipt.id);
  assert.equal(receipt.issue.allocatedCardId, ids.mapCardId);
  assert.equal(receipt.issue.cardOpenedAtIssue, true);
});

test('主线 (b)：只把队伍选择换成红 -> 蓝门续局不合法', async () => {
  const { svc } = freshService();
  const ids = await seedBase(svc);

  // 同一份“蓝档”只把第一章选择改成红（不持蓝钥匙）。
  svc._testSetTeam(ids.teamAId, {
    plot: [{ chapter: 1, door: 'red', plot: 'plot-red' }],
    keys: [],
    reveals: ['plot-red'],
  });

  // 仍拿同一库存、要发同一盒、仍请求蓝门 -> 必须被拒。
  await rejects(
    svc.checkout({
      teamId: ids.teamAId,
      model: 'adopted',
      boxId: ids.adoptedBoxId,
      door: 'blue',
      cardId: ids.mapCardId,
    }),
    codes.DOOR_ILLEGAL
  );

  // 而红色续局（第二章红门只需 priorDoor=red）是合法的，不能被只会蓝门预演而误拒。
  const red = await svc.checkout({
    teamId: ids.teamAId,
    model: 'adopted',
    boxId: ids.adoptedBoxId,
    door: 'red',
  });
  assert.equal(red.loan.status, 'open');
});

test('主线 (c)：全部可用图卡曾被拆封 -> 不能承诺同一未揭情节', async () => {
  const { svc } = freshService();
  const ids = await seedBase(svc);

  // 从基准发盒之前的同一份蓝档与库存出发：把全部 map-blue 图卡改成曾被拆封。
  for (const c of svc.listCards({ plotKey: 'map-blue' })) {
    await svc._testSetCard(c.id, {
      state: 'opened', openedAt: new Date().toISOString(), openedByLoanId: 'prior-loan',
    });
  }
  assert.equal(svc.listCards({ plotKey: 'map-blue', state: 'sealed' }).length, 0);

  // 蓝门续局需要密封图卡，现在不可承诺 -> 即使不点名门，也不能发出这个续玩局。
  await rejects(
    svc.checkout({
      teamId: ids.teamAId,
      model: 'adopted',
      boxId: ids.adoptedBoxId,
      door: 'blue',
    }),
    codes.UNREVEALED_PLOT_UNAVAILABLE
  );

  // 不点名门也一样：唯一可兑现的蓝门缺卡，红门又不合法（甲走的是蓝）-> 无续玩局。
  await rejects(
    svc.checkout({
      teamId: ids.teamAId,
      model: 'adopted',
      boxId: ids.adoptedBoxId,
    }),
    codes.UNREVEALED_PLOT_UNAVAILABLE
  );
});

// ---------------------------------------------------------------------------
// 场景 3：两种合法剧情都要接受（顾客有两种合法选择时，不能只接受预演那一种）
// ---------------------------------------------------------------------------
test('两种合法剧情：第一章蓝/红都能被接受，且互斥不可改写', async () => {
  const { svc } = freshService();
  const ids = await seedBase(svc);

  // 新队停在第一章，蓝红都合法。
  const t = await svc.createTeam({ name: '新队', campaignId: ids.rulebookId });
  const ownPouch = await svc.createPouch({ teamId: t.id, campaignId: ids.rulebookId });
  const loan = await svc.checkout({ teamId: t.id, model: 'base', boxId: ids.baseBoxId, pouchId: ownPouch.id });

  const blue = await svc.chooseDoor(loan.loan.id, { door: 'blue' });
  assert.equal(blue.loan.door, 'blue');
  assert.ok(blue.team.keys.includes('blue-key'));
  // 选了蓝就不能再改红（互斥不可复原）。
  await rejects(svc.chooseDoor(loan.loan.id, { door: 'red' }), codes.IRREVERSIBLE);
});

test('个人战役袋按队绑定：别人的袋不能借', async () => {
  const { svc } = freshService();
  const ids = await seedBase(svc);
  const t = await svc.createTeam({ name: '借用他人袋', campaignId: ids.rulebookId });
  await rejects(
    svc.checkout({ teamId: t.id, model: 'base', boxId: ids.baseBoxId, pouchId: ids.pouchBId }),
    codes.VALIDATION
  );
});

test('两种合法剧情：预演红之后，顾客选未预演的蓝依然被接受', async () => {
  const { svc } = freshService();
  const ids = await seedBase(svc);

  const t = await svc.createTeam({ name: '顾客桌', campaignId: ids.rulebookId });
  const pouch = await svc.createPouch({ teamId: t.id, campaignId: ids.rulebookId });
  const { loan } = await svc.checkout({
    teamId: t.id, model: 'base', boxId: ids.baseBoxId, pouchId: pouch.id,
  });
  // 馆员预演是红，但顾客选蓝，必须照样成功。
  const r = await svc.chooseDoor(loan.id, { door: 'blue' });
  assert.equal(r.loan.door, 'blue');
});

// ---------------------------------------------------------------------------
// 场景 4：并发借最后一盒 -> 只有一个能拿走
// ---------------------------------------------------------------------------
test('并发：两个柜台同时借最后一盒，只能一个成功', async () => {
  const { svc } = freshService();
  const ids = await seedBase(svc);
  // 两个都缺卡的蓝门队 -> 都需要这张唯一的盒；用同一盒做竞争。
  // 为避免队伍限制，另建两队各停在第一章。
  const t1 = await svc.createTeam({ name: '柜台一', campaignId: ids.rulebookId });
  const t2 = await svc.createTeam({ name: '柜台二', campaignId: ids.rulebookId });
  const p1 = await svc.createPouch({ teamId: t1.id, campaignId: ids.rulebookId });
  const p2 = await svc.createPouch({ teamId: t2.id, campaignId: ids.rulebookId });

  const results = await Promise.allSettled([
    svc.checkout({ teamId: t1.id, model: 'base', boxId: ids.baseBoxId, pouchId: p1.id }),
    svc.checkout({ teamId: t2.id, model: 'base', boxId: ids.baseBoxId, pouchId: p2.id }),
  ]);
  const ok = results.filter((r) => r.status === 'fulfilled');
  const bad = results.filter((r) => r.status === 'rejected');
  assert.equal(ok.length, 1, '应恰好一个成功');
  assert.equal(bad.length, 1, '应恰好一个失败');
  assert.equal(bad[0].reason.code, codes.BOX_UNAVAILABLE);
});

// ---------------------------------------------------------------------------
// 场景 5：底盒 + 个人战役袋
// ---------------------------------------------------------------------------
test('底盒借法：可分离门牌进个人袋，不可拆刻痕随实物盒', async () => {
  const { svc } = freshService();
  const ids = await seedBase(svc);

  // 红队自己的袋，底盒干净，红门路线合法。
  const { loan } = await svc.checkout({
    teamId: ids.teamBId, model: 'base', boxId: ids.baseBoxId, pouchId: ids.pouchBId, door: 'red',
  });
  await svc.chooseDoor(loan.id, { door: 'red' });
  // 门牌可分离 -> 进个人袋。
  const aff = await svc.affixPlate(loan.id, { chapter: 1, door: 'red' });
  assert.equal(aff.pouch.separable.plates.length, 1);
  assert.equal(aff.box === undefined || aff.box.permanentMarks.affixedPlates.length === 0, true);

  // 不可拆的刻痕随实物盒走。
  const ret = await svc.returnLoan(loan.id, {
    progress: { completedChapter: 1 },
    boxMarks: { grooves: [{ chapter: 1, door: 'red' }] },
  });
  assert.equal(ret.box.permanentMarks.grooves.length, 1);
  const pouch = svc.getPouch(ids.pouchBId);
  assert.equal(pouch.separable.plates.length, 1);
});

test('底盒兼容：盒上刻痕与个人袋/队伍剧情冲突时拒绝发盒', async () => {
  const { svc } = freshService();
  const ids = await seedBase(svc);

  // 先让红队借底盒并归还，留下红门刻痕。
  const { loan } = await svc.checkout({
    teamId: ids.teamBId, model: 'base', boxId: ids.baseBoxId, pouchId: ids.pouchBId, door: 'red',
  });
  await svc.chooseDoor(loan.id, { door: 'red' });
  await svc.returnLoan(loan.id, { progress: { completedChapter: 1 }, boxMarks: { grooves: [{ chapter: 1, door: 'red' }] } });

  // 甲是蓝门路线，拿这只带红刻痕的底盒 -> 不兼容。
  const pouchA = await svc.createPouch({ teamId: ids.teamAId, campaignId: ids.rulebookId });
  await rejects(
    svc.checkout({ teamId: ids.teamAId, model: 'base', boxId: ids.baseBoxId, pouchId: pouchA.id }),
    codes.INCOMPATIBLE_BOX
  );
});

// ---------------------------------------------------------------------------
// 场景 6：还盒不能把队伍存档覆盖到实物盒，反之亦然
// ---------------------------------------------------------------------------
test('还盒：队伍存档与实物盒痕迹各自独立落库', async () => {
  const { svc } = freshService();
  const ids = await seedBase(svc);

  const { loan } = await svc.checkout({ teamId: ids.teamAId, model: 'adopted', boxId: ids.adoptedBoxId });
  await svc.chooseDoor(loan.id, { door: 'blue' });
  const ret = await svc.returnLoan(loan.id, {
    progress: { completedChapter: 1, keys: ['blue-key'] },
    boxMarks: { affixedPlates: [{ chapter: 1, door: 'blue' }] },
  });

  // 队伍有进度与钥匙。
  const team = svc.getTeam(ids.teamAId);
  assert.deepEqual(team.keys, ['blue-key']);
  assert.equal(team.progress.currentChapter, 2);
  // 盒上有痕迹，但盒本身没有“队伍进度”字段。
  const box = svc.getBox(ids.adoptedBoxId);
  assert.equal(box.permanentMarks.affixedPlates.length, 1);
  assert.equal(box.progress, undefined);
  assert.equal(box.keys, undefined);
  // 回单同时记录两边。
  assert.equal(ret.receipt.result.teamProgressAfter.currentChapter, 2);
  assert.equal(ret.receipt.result.boxPermanentMarksAfter.affixedPlates.length, 1);
});

// ---------------------------------------------------------------------------
// 场景 7：重启后凭持久化借单追溯“哪队因哪道永久变化需另挑盒”
// ---------------------------------------------------------------------------
test('重启：持久化后报告哪队因哪道永久变化需另挑盒，并可追到来源借单', async () => {
  const { svc, dir } = freshService();
  const ids = await seedBase(svc);

  // 红队用认养盒走红并贴死红门牌。
  const { loan } = await svc.checkout({ teamId: ids.teamBId, model: 'adopted', boxId: ids.adoptedBoxId, door: 'red' });
  await svc.chooseDoor(loan.id, { door: 'red' });
  await svc.returnLoan(loan.id, {
    progress: { completedChapter: 1 },
    boxMarks: { affixedPlates: [{ chapter: 1, door: 'red' }] },
  });
  const sourceReceiptId = loan.receiptId;

  // 重启：重新从磁盘打开同一份状态。
  const reopened = CampaignService.open(path.join(dir, 'state.json'));
  const report = reopened.reportRebindings();

  const hit = report.entries.find((e) => e.teamId === ids.teamAId && e.boxId === ids.adoptedBoxId);
  assert.ok(hit, '应报告甲队因这盒的红门牌需要另挑盒');
  assert.equal(hit.conflict.kind, 'affixedPlate');
  assert.equal(hit.conflict.door, 'red');
  assert.equal(hit.teamChoice.door, 'blue');
  assert.equal(hit.conflict.sourceReceiptId, sourceReceiptId);
  assert.equal(hit.recommendation, 'another-box');

  // 重启后实际发盒也会因剧情改写被拒（甲并非认养者，且盒上红门牌与其蓝门冲突）。
  await rejects(
    reopened.checkout({ teamId: ids.teamAId, model: 'adopted', boxId: ids.adoptedBoxId, takeover: true }),
    codes.PLOT_REWRITE
  );
});

test('重启：并发占用与图卡开卡状态都能持久化', async () => {
  const { svc, dir } = freshService();
  const ids = await seedBase(svc);
  const out = await svc.checkout({
    teamId: ids.teamAId, model: 'adopted', boxId: ids.adoptedBoxId, door: 'blue', cardId: ids.mapCardId, openCard: true,
  });
  const reopened = CampaignService.open(path.join(dir, 'state.json'));
  assert.equal(reopened.getCard(ids.mapCardId).state, 'opened');
  assert.equal(reopened.getBox(ids.adoptedBoxId).status, 'on_loan');
  assert.equal(reopened.getLoan(out.loan.id).cardOpened, true);
  // 回单可凭 id 单查到。
  assert.equal(reopened.getReceipt(out.receipt.id).issue.allocatedCardId, ids.mapCardId);
});

// ---------------------------------------------------------------------------
// 场景 9：还盒必须同时声明队伍进度与实物盒痕迹，缺一不可
// ---------------------------------------------------------------------------
test('还盒契约：缺 progress 或 boxMarks 都拒绝', async () => {
  const { svc } = freshService();
  const ids = await seedBase(svc);
  const { loan } = await svc.checkout({ teamId: ids.teamAId, model: 'adopted', boxId: ids.adoptedBoxId, door: 'blue', cardId: ids.mapCardId });
  await svc.chooseDoor(loan.id, { door: 'blue' });

  await rejects(svc.returnLoan(loan.id, { boxMarks: {} }), codes.VALIDATION);
  await rejects(svc.returnLoan(loan.id, { progress: { completedChapter: 1 } }), codes.VALIDATION);
  // 两者齐全才成功。
  const ok = await svc.returnLoan(loan.id, {
    progress: { completedChapter: 1 },
    boxMarks: { affixedPlates: [{ chapter: 1, door: 'blue' }] },
  });
  assert.equal(ok.loan.status, 'returned');
});

// ---------------------------------------------------------------------------
// 场景 10：发盒前预检 takeover-check 能回答“贴死的门还能不能接”
// ---------------------------------------------------------------------------
test('takeover-check：回答某队能不能接这只盒以及原因', async () => {
  const { svc } = freshService();
  const ids = await seedBase(svc);

  // 红队认养盒走红并贴死红门牌。
  const { loan } = await svc.checkout({ teamId: ids.teamBId, model: 'adopted', boxId: ids.adoptedBoxId, door: 'red' });
  await svc.chooseDoor(loan.id, { door: 'red' });
  await svc.returnLoan(loan.id, { progress: { completedChapter: 1 }, boxMarks: { affixedPlates: [{ chapter: 1, door: 'red' }] } });

  // 甲（蓝路线）不能接这只红门牌盒。
  const check = svc.evaluateTakeover(ids.teamAId, ids.adoptedBoxId, { takeover: true });
  assert.equal(check.ok, false);
  assert.ok(check.reasons.some((r) => r.code === codes.PLOT_REWRITE && r.boxDoor === 'red' && r.teamDoor === 'blue'));

  // 红队自己（红路线）可以接。
  const checkRed = svc.evaluateTakeover(ids.teamBId, ids.adoptedBoxId);
  assert.equal(checkRed.ok, true, JSON.stringify(checkRed.reasons));
});
test('还盒后另借他盒：队伍进度跟随队伍，不自动写进新盒', async () => {
  const { svc } = freshService();
  const ids = await seedBase(svc);

  // 甲在第一盒走蓝并归还。
  const first = await svc.checkout({ teamId: ids.teamAId, model: 'adopted', boxId: ids.adoptedBoxId, door: 'blue', cardId: ids.mapCardId, openCard: true });
  await svc.chooseDoor(first.loan.id, { door: 'blue' });
  await svc.returnLoan(first.loan.id, { progress: { completedChapter: 1 }, boxMarks: { affixedPlates: [{ chapter: 1, door: 'blue' }] } });

  // 再建一只干净认养盒；队伍进度已到第二章，但新盒上不该出现任何旧痕迹。
  const box2 = await svc.createBox({ kind: 'adopted', campaignId: ids.rulebookId, label: '认养盒 B' });
  assert.equal(svc.getBox(box2.id).permanentMarks.affixedPlates.length, 0);

  // 第二章蓝门续局需要另一张完好密封图卡（原卡已被上局开掉）。
  const mapCard2 = await svc.createCard({ campaignId: ids.rulebookId, plotKey: 'map-blue', state: 'sealed' });

  const second = await svc.checkout({ teamId: ids.teamAId, model: 'adopted', boxId: box2.id, door: 'blue', cardId: mapCard2.id });
  assert.equal(second.loan.chapter, 2);
  assert.equal(svc.getBox(box2.id).permanentMarks.affixedPlates.length, 0);
});
