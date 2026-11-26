'use strict';

/**
 * 示例战役规则与基准库存，严格对应题干里的场景：
 *
 *  第一章：蓝门 / 红门（互斥）。走蓝门拿「蓝钥匙」，并揭晓「蓝门真相」；
 *          走红门揭晓「红门真相」。
 *  第二章：从蓝门继续需要 (a) 第一章确实走过蓝门 (b) 手里有蓝钥匙
 *          (c) 一张馆方可提供、情节为「蓝门地图」的完好密封图卡。
 *          从红门继续只需第一章走过红门。
 *
 * 这样：
 *  - 甲走蓝并持钥匙 -> 蓝门续局在两章都合法；
 *  - 把甲的选择换成红 -> 蓝门续局因 priorDoor 不符而不合法；
 *  - 蓝档 + 库存里所有图卡都被拆封 -> 无法承诺「蓝门地图」这个未揭情节。
 */
function standardRulebook() {
  return {
    id: 'gate-of-embers',
    title: '余烬之门',
    chapters: [
      {
        index: 1,
        title: '第一章 · 双门',
        doors: [
          {
            id: 'blue',
            label: '蓝门',
            grantsKey: 'blue-key',
            revealsPlot: 'plot-blue',
            affixPlate: 'plate-1-blue',
          },
          {
            id: 'red',
            label: '红门',
            revealsPlot: 'plot-red',
            affixPlate: 'plate-1-red',
          },
        ],
        hiddenPouches: [
          {
            id: 'pouch-1-under-blue',
            door: 'blue',
            revealsPlot: 'plot-blue-secret',
            grantsKey: 'blue-key',
          },
        ],
      },
      {
        index: 2,
        title: '第二章 · 蓝门之后',
        doors: [
          {
            id: 'blue',
            label: '蓝门（继续）',
            requires: {
              priorDoor: { chapter: 1, door: 'blue' },
              keys: ['blue-key'],
              sealedCardPlot: 'map-blue',
            },
            revealsPlot: 'plot-blue-2',
            affixPlate: 'plate-2-blue',
          },
          {
            id: 'red',
            label: '红门（继续）',
            requires: {
              priorDoor: { chapter: 1, door: 'red' },
            },
            revealsPlot: 'plot-red-2',
            affixPlate: 'plate-2-red',
          },
        ],
        hiddenPouches: [],
      },
    ],
  };
}

/**
 * 建立基准库存与“甲的蓝档”。返回关键实体 id，方便演示与测试引用。
 *
 * 注意：这里把甲设成“第一章已经走完蓝门、拿到蓝钥匙、正在第二章门口”，
 * 即题干里“甲实际走蓝并拿钥匙；下一章从蓝门进去还要一张完好密封图卡”。
 */
async function seedBase(service) {
  await service.createRulebook(standardRulebook());

  const teamA = await service.createTeam({ name: '甲队', campaignId: 'gate-of-embers' });
  // 甲在第一章真正走了蓝门、拿了蓝钥匙，完成第一章，停在第二章。
  await service._testSetTeam(teamA.id, {
    progress: { completedChapters: [1], currentChapter: 2 },
    plot: [{ chapter: 1, door: 'blue', plot: 'plot-blue' }],
    keys: ['blue-key'],
    reveals: ['plot-blue'],
  });

  // 另一支走红门的队伍，用于“永久痕迹导致需另挑盒”的追溯场景。
  const teamB = await service.createTeam({ name: '红队', campaignId: 'gate-of-embers' });
  await service._testSetTeam(teamB.id, {
    progress: { completedChapters: [1], currentChapter: 2 },
    plot: [{ chapter: 1, door: 'red', plot: 'plot-red' }],
    keys: [],
    reveals: ['plot-red'],
  });

  // 认养战役盒：一开始干净无永久痕迹。
  const adoptedBox = await service.createBox({
    kind: 'adopted', campaignId: 'gate-of-embers', label: '认养盒·余烬之门 A',
  });

  // 空白底盒 + 个人战役袋（红队自己的袋，红门路线）。
  const baseBox = await service.createBox({
    kind: 'base', campaignId: 'gate-of-embers', label: '空白底盒 #1',
  });
  const pouchB = await service.createPouch({ teamId: teamB.id, campaignId: 'gate-of-embers' });

  // 馆藏图卡：一张完好密封的「蓝门地图」，以及一张已拆封的对照卡。
  const mapCard = await service.createCard({
    campaignId: 'gate-of-embers', plotKey: 'map-blue', state: 'sealed', label: '完好密封·蓝门地图',
  });
  const openedMapCard = await service.createCard({
    campaignId: 'gate-of-embers', plotKey: 'map-blue', state: 'opened', label: '曾被拆封·蓝门地图',
  });

  return {
    rulebookId: 'gate-of-embers',
    teamAId: teamA.id,
    teamBId: teamB.id,
    adoptedBoxId: adoptedBox.id,
    baseBoxId: baseBox.id,
    pouchBId: pouchB.id,
    mapCardId: mapCard.id,
    openedMapCardId: openedMapCard.id,
  };
}

module.exports = { standardRulebook, seedBase };
