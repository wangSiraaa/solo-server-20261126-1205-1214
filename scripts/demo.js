'use strict';

/**
 * 端到端演示：完全按题干的叙事跑一遍，并把每一步打印出来。
 *
 *  1. 甲走蓝门拿钥匙；柜员发出一盒“有完整图卡、其余部件也合用”的蓝门续玩局，
 *     并登记本次真开了图卡。
 *  2. 相同库存另开演示，只把队伍原选择换成红 -> 蓝门续局不能接（却接受红门续局）。
 *  3. 同一份蓝档与库存，仅把全部可用图卡改成曾被拆封 -> 不能承诺同一未揭情节。
 *  4. 红队认养盒走红并贴死红门牌；模拟馆长重启服务，
 *     凭持久化借单查到“哪队因哪道永久变化需另挑盒”，并可追到来源回单。
 *
 * 运行： node scripts/demo.js
 */

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { CampaignService } = require('../src/service');
const { seedBase } = require('../src/fixtures');

function hr(title) {
  console.log('\n' + '═'.repeat(72));
  console.log(title);
  console.log('═'.repeat(72));
}

function show(label, value) {
  console.log(`  ${label}: ${typeof value === 'string' ? value : JSON.stringify(value)}`);
}

async function expectReject(label, promise, wanted) {
  try {
    await promise;
    console.log(`  ✗ ${label}: 竟然成功了（不应发生）`);
    return null;
  } catch (err) {
    const ok = !wanted || err.code === wanted;
    console.log(`  ${ok ? '✓' : '✗'} ${label}: 被拒 -> [${err.code}] ${err.message}`);
    return err;
  }
}

function newSandbox() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'camp-demo-'));
  const svc = CampaignService.open(path.join(dir, 'state.json'));
  return { dir, svc };
}

async function main() {
  // ------------------------------------------------------------------
  hr('基准库存与甲队的蓝档');
  const { svc, dir } = newSandbox();
  const ids = await seedBase(svc);
  const plan = svc.getContinuationPlan(ids.teamAId);
  console.log('  甲队停在第二章，可续玩报告：');
  show('结构合法门', plan.legalDoors);
  show('含图卡可达的门', plan.satisfiableDoors);
  show('缺口原因', plan.reasons);
  console.log(`  （完好密封图卡：${ids.mapCardId}；被拆封对照卡：${ids.openedMapCardId}）`);

  // ------------------------------------------------------------------
  hr('① 柜台发出蓝门续玩局，配完整密封图卡，并登记本次真开卡');
  const issued = await svc.checkout({
    teamId: ids.teamAId,
    model: 'adopted',
    boxId: ids.adoptedBoxId,
    door: 'blue',
    cardId: ids.mapCardId,
    openCard: true,
  });
  show('借单', issued.loan.id);
  show('回单', issued.receipt.id);
  show('配到的图卡', issued.allocatedCard && `${issued.allocatedCard.id}（${issued.allocatedCard.state}）`);
  show('本章', issued.loan.chapter);
  show('本次已开卡', issued.loan.cardOpened);
  show('现场合法门（顾客可选）', issued.legalDoors);
  // 顾客现场确认走蓝门。
  const chosen = await svc.chooseDoor(issued.loan.id, { door: 'blue' });
  show('顾客现场选门', chosen.loan.door);
  const receipt = svc.getReceipt(issued.receipt.id);
  show('回单记录的配卡', receipt.issue.allocatedCardId);
  show('回单记录开卡于发盒时', receipt.issue.cardOpenedAtIssue);

  // 归还，留下蓝门牌（永久痕迹）。
  await svc.returnLoan(issued.loan.id, {
    progress: { completedChapter: 2, keys: ['blue-key'] },
    boxMarks: { affixedPlates: [{ chapter: 1, door: 'blue' }] },
  });

  // ------------------------------------------------------------------
  hr('② 相同库存另开演示：只把甲第一章的选择换成红');
  const demoB = newSandbox();
  const idsB = await seedBase(demoB.svc);
  await demoB.svc._testSetTeam(idsB.teamAId, {
    plot: [{ chapter: 1, door: 'red', plot: 'plot-red' }],
    keys: [],
    reveals: ['plot-red'],
  });
  console.log('  甲现在第一章走的是红门、且没有蓝钥匙。');
  await expectReject(
    '请求蓝门续局',
    demoB.svc.checkout({
      teamId: idsB.teamAId, model: 'adopted', boxId: idsB.adoptedBoxId, door: 'blue', cardId: idsB.mapCardId,
    }),
    'DOOR_ILLEGAL'
  );
  // 红门续局应仍被接受（不能只会蓝门）。
  const redOk = await demoB.svc.checkout({
    teamId: idsB.teamAId, model: 'adopted', boxId: idsB.adoptedBoxId, door: 'red',
  });
  console.log(`  ✓ 改走红门后，红门续局可发出：借单 ${redOk.loan.id}`);

  // ------------------------------------------------------------------
  hr('③ 同一份蓝档与库存：仅把全部可用图卡改成曾被拆封');
  const demoC = newSandbox();
  const idsC = await seedBase(demoC.svc);
  for (const c of demoC.svc.listCards({ plotKey: 'map-blue' })) {
    await demoC.svc._testSetCard(c.id, {
      state: 'opened', openedAt: new Date().toISOString(), openedByLoanId: 'prior-loan',
    });
  }
  console.log('  map-blue 现有密封图卡数量：' + demoC.svc.listCards({ plotKey: 'map-blue', state: 'sealed' }).length);
  await expectReject(
    '点名要蓝门续局',
    demoC.svc.checkout({
      teamId: idsC.teamAId, model: 'adopted', boxId: idsC.adoptedBoxId, door: 'blue',
    }),
    'UNREVEALED_PLOT_UNAVAILABLE'
  );
  await expectReject(
    '不点名、只要求发一盒续玩局',
    demoC.svc.checkout({
      teamId: idsC.teamAId, model: 'adopted', boxId: idsC.adoptedBoxId,
    }),
    'UNREVEALED_PLOT_UNAVAILABLE'
  );

  // ------------------------------------------------------------------
  hr('④ 红队认养盒贴死红门牌 -> 重启后追溯“哪队因哪道永久变化需另挑盒”');
  // 红队有自己的认养盒（甲那只已被甲认养并留有蓝门牌）。
  const redBox = await svc.createBox({
    kind: 'adopted', campaignId: ids.rulebookId, label: '认养盒·红队',
  });
  const redLoan = await svc.checkout({
    teamId: ids.teamBId, model: 'adopted', boxId: redBox.id, door: 'red',
  });
  await svc.chooseDoor(redLoan.loan.id, { door: 'red' });
  const redReturn = await svc.returnLoan(redLoan.loan.id, {
    progress: { completedChapter: 1 },
    boxMarks: { affixedPlates: [{ chapter: 1, door: 'red' }] },
  });
  show('红队认养盒上的贴死门牌', redReturn.box.permanentMarks.affixedPlates);
  show('制造该痕迹的回单', redLoan.receipt.id);

  // 模拟馆长重启服务：重新从磁盘打开。
  const reopened = CampaignService.open(path.join(dir, 'state.json'));
  console.log('\n  ── 馆长重启后查询 /reports/rebindings ──');
  const report = reopened.reportRebindings();
  for (const e of report.entries) {
    console.log(`  • ${e.message}`);
    show('    来源回单', e.conflict.sourceReceiptId);
    show('    建议', e.recommendation);
  }
  // 重启后实际发盒会被拒：另取一支蓝路线、仍停在第二章的队伍，
  // 试图接手这只红门牌盒（需 takeover 越过“别人认养”这一层，才能暴露剧情冲突）。
  const blueTwo = await reopened.createTeam({ name: '蓝队二分队', campaignId: ids.rulebookId });
  await reopened._testSetTeam(blueTwo.id, {
    progress: { completedChapters: [1], currentChapter: 2 },
    plot: [{ chapter: 1, door: 'blue', plot: 'plot-blue' }],
    keys: ['blue-key'],
    reveals: ['plot-blue'],
  });
  await expectReject(
    '重启后把这只红门牌盒发给蓝路线的队',
    reopened.checkout({
      teamId: blueTwo.id, model: 'adopted', boxId: redBox.id, takeover: true,
    }),
    'PLOT_REWRITE'
  );

  // ------------------------------------------------------------------
  hr('⑤ 并发：两个柜台同时借最后一盒，只能一个拿走');
  const demoD = newSandbox();
  const idsD = await seedBase(demoD.svc);
  const t1 = await demoD.svc.createTeam({ name: '柜台一', campaignId: idsD.rulebookId });
  const t2 = await demoD.svc.createTeam({ name: '柜台二', campaignId: idsD.rulebookId });
  const p1 = await demoD.svc.createPouch({ teamId: t1.id, campaignId: idsD.rulebookId });
  const p2 = await demoD.svc.createPouch({ teamId: t2.id, campaignId: idsD.rulebookId });
  const race = await Promise.allSettled([
    demoD.svc.checkout({ teamId: t1.id, model: 'base', boxId: idsD.baseBoxId, pouchId: p1.id }),
    demoD.svc.checkout({ teamId: t2.id, model: 'base', boxId: idsD.baseBoxId, pouchId: p2.id }),
  ]);
  const winners = race.filter((r) => r.status === 'fulfilled');
  const losers = race.filter((r) => r.status === 'rejected');
  console.log(`  ✓ 成功 ${winners.length} 个，失败 ${losers.length} 个`);
  if (losers[0]) console.log(`    失败方原因：[${losers[0].reason.code}] ${losers[0].reason.message}`);

  console.log('\n演示结束。所有状态已落到临时目录，服务可重启复现。\n');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
