import test from 'node:test';
import assert from 'node:assert/strict';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { rm } from 'node:fs/promises';
import { Store } from '../src/store.js';
import { seedDemo, FLOWER_WITH_RED_LEFT, FLOWER_WITH_RED_RIGHT } from '../src/demo.js';
import { matchActualGrid, mirrorHorizontal, planCompleteBoxes } from '../src/domain.js';

function tempDb() {
  return join(tmpdir(), `soap-test-${Date.now()}-${Math.random().toString(36).slice(2)}.json`);
}

test('反向红点只有在允许左右翻转时匹配带方向样卡', () => {
  const card = { pattern: FLOWER_WITH_RED_LEFT, matchMode: 'mask', orientationPolicy: 'horizontal-ok' };
  assert.equal(matchActualGrid(FLOWER_WITH_RED_LEFT, card, 'bar-direction').matches, true);
  assert.equal(matchActualGrid(FLOWER_WITH_RED_RIGHT, card, 'bar-direction').matches, false);

  const flipped = matchActualGrid(FLOWER_WITH_RED_RIGHT, card, 'horizontal-ok');
  assert.equal(flipped.matches, true);
  assert.equal(flipped.transform, 'hflip');

  const blueCenter = structuredClone(FLOWER_WITH_RED_RIGHT);
  blueCenter.cells[2][2] = 'B';
  assert.equal(matchActualGrid(blueCenter, card, 'horizontal-ok').matches, false);
  assert.deepEqual(mirrorHorizontal(FLOWER_WITH_RED_RIGHT), FLOWER_WITH_RED_LEFT);
});

test('样条、两片匹配、装盒、改黄格少一片、严格朝向少一片', async () => {
  const file = tempDb();
  const store = await new Store(file).init();
  await seedDemo(store);

  const flowerMatches = store.findMatches({ cardId: 'CARD-FLOWER' });
  assert.equal(flowerMatches.length, 2, '普通花卡应在两片的真实端面上各看到一朵花');

  const directional = store.findMatches({ cardId: 'CARD-REDL', orientationPolicy: 'horizontal-ok' });
  assert.equal(directional.length, 2);
  assert.deepEqual(directional.map((m) => m.cellIndex).sort((a, b) => a - b), [0, 6]);
  const flippedPiece = directional.find((m) => m.transform === 'hflip');
  assert.ok(flippedPiece, '应有一片必须左右翻转');
  assert.equal(flippedPiece.barCode, 'BAR-DEMO');

  const strict = store.findMatches({ cardId: 'CARD-REDL', orientationPolicy: 'bar-direction' });
  assert.equal(strict.length, 1, '只许按条坯头尾方向摆放时，反向红点片不合');
  assert.equal(strict[0].cellIndex, 0);

  const result = await store.mutate(() => store.createOrder({
    cardId: 'CARD-REDL',
    piecesPerBox: 2,
    boxCount: 1,
    orientationPolicy: 'horizontal-ok',
    clerk: 'clerk-test',
    customer: 'handmade-class',
    destination: '教室自提柜 A1'
  }));
  assert.equal(result.boxes.length, 1);
  assert.equal(result.boxes[0].pieceIds.length, 2);
  assert.equal(result.boxes[0].pieceFaces.length, 2);
  assert.ok(result.boxes[0].slots.every((s) => s.twoFaces.length === 2), '盒号必须保存每片两面');

  // 只把第二处花（第 6 层）实际浇注的中心黄格改成蓝色。
  await store.mutate(() => store.amendPourCell('BAR-DEMO', 6, 2, 2, 'B', 'test yellow to blue'));
  const afterBlue = store.findMatches({ cardId: 'CARD-REDL', orientationPolicy: 'horizontal-ok', availableOnly: false });
  assert.equal(afterBlue.length, 1, '蓝心后第二处花应失效，只剩一片');

  // 换回原条：用合模时保存的真实浇注快照恢复。
  await store.mutate(() => store.restoreOriginalPours('BAR-DEMO'));
  const restored = store.findMatches({ cardId: 'CARD-REDL', orientationPolicy: 'horizontal-ok', availableOnly: false });
  assert.equal(restored.length, 2);

  const strictAfterRestore = store.findMatches({ cardId: 'CARD-REDL', orientationPolicy: 'bar-direction', availableOnly: false });
  assert.equal(strictAfterRestore.length, 1);

  await rm(file, { force: true });
});

test('设计卡可编辑，但不改变已合模条坯的真实浇注快照', async () => {
  const file = tempDb();
  const store = await new Store(file).init();
  await seedDemo(store);
  const before = structuredClone(store.findBar('BAR-DEMO').closedPours);

  await store.mutate(() => store.updateCard('CARD-REDL', { name: '改过的设计图', pattern: ['.....', '.....', '..R..', '.....', '.....'] }));
  const bar = store.findBar('BAR-DEMO');
  assert.deepEqual(bar.closedPours, before);
  assert.equal(store.findCard('CARD-REDL').version, 2);
  await rm(file, { force: true });
});

test('同一片被两个柜员同时领取时只能成功一次', async () => {
  const file = tempDb();
  const store = await new Store(file).init();
  await seedDemo(store);

  const results = await Promise.allSettled([
    store.mutate(() => store.claimPieces({ pieceId: 'PIECE-0001', clerk: 'A' })),
    store.mutate(() => store.claimPieces({ pieceId: 'PIECE-0001', clerk: 'B' }))
  ]);
  assert.equal(results[0].status, 'fulfilled');
  assert.equal(results[1].status, 'rejected');
  assert.match(results[1].reason.code || '', /PIECE_NOT_AVAILABLE/);
  assert.equal(store.findPiece('PIECE-0001').claimedBy, 'A');
  await rm(file, { force: true });
});

test('两个柜员同时从同一条切取时得到不重叠且真实存在的格段', async () => {
  const file = tempDb();
  const store = await new Store(file).init();
  await store.mutate(() => {
    store.createBar({ code: 'BAR-CONC', length: 12, rows: 3, cols: 3, standardWidth: 3, kerfWidth: 1 });
    store.closeBar('BAR-CONC');
  });

  const cuts = await Promise.all([
    store.mutate(() => store.cutFromBar({ barCode: 'BAR-CONC', clerk: 'A', width: 3, kerfWidth: 1 })),
    store.mutate(() => store.cutFromBar({ barCode: 'BAR-CONC', clerk: 'B', width: 3, kerfWidth: 1 }))
  ]);

  assert.deepEqual(cuts.map((p) => [p.start, p.end]), [[0, 2], [4, 6]]);
  assert.deepEqual(cuts.flatMap((p) => p.kerfIndexes), [3, 7]);
  const assigned = new Set();
  for (const p of cuts) {
    for (let i = p.start; i <= p.end; i += 1) {
      assert.equal(assigned.has(i), false, '成品格不能重叠');
      assigned.add(i);
      assert.equal(store.findBar('BAR-CONC').cells[i].pieceId, p.code);
    }
  }
  const remainder = store.remnants('BAR-CONC');
  assert.deepEqual(remainder.segments.map((s) => [s.start, s.end]), [[8, 11]]);
  await rm(file, { force: true });
});

test('切口格不属于相邻成品，余料查询给出连续段', async () => {
  const file = tempDb();
  const store = await new Store(file).init();
  await store.mutate(() => {
    store.createBar({ code: 'BAR-CUT', length: 8, rows: 2, cols: 2, standardWidth: 3, kerfWidth: 1 });
    store.closeBar('BAR-CUT');
  });
  const piece = await store.mutate(() => store.cutFromBar({ barCode: 'BAR-CUT', from: 'head' }));
  assert.deepEqual([piece.start, piece.end], [0, 2]);
  assert.deepEqual(piece.kerfIndexes, [3]);
  const rem = store.remnants('BAR-CUT');
  assert.deepEqual(rem.segments.map((s) => [s.start, s.end]), [[4, 7]]);
  await rm(file, { force: true });
});

test('多槽位样卡可调换装盒顺序，最大流不会被固定槽位顺序卡死', () => {
  const pieces = [{ id: 'P0' }, { id: 'P1' }];
  const template = [{ cardId: 'A' }, { cardId: 'B' }];
  const can = {
    P0: new Set(['A', 'B']),
    P1: new Set(['A'])
  };
  const plan = planCompleteBoxes(pieces, template, 1, (piece, slot) => can[piece.id].has(slot.cardId) ? { ok: true } : null);
  assert.equal(plan.feasible, true);
  assert.equal(plan.assignments.find((a) => a.slotIndex === 0).pieceId, 'P1');
  assert.equal(plan.assignments.find((a) => a.slotIndex === 1).pieceId, 'P0');
});

test('停业重启后仍可按盒号找到原片和去向', async () => {
  const file = tempDb();
  let store = await new Store(file).init();
  await seedDemo(store);
  const created = await store.mutate(() => store.createOrder({ cardId: 'CARD-REDL', piecesPerBox: 2, boxCount: 1 }));
  const boxCode = created.boxes[0].code;
  await store.mutate(() => store.pickupBox(boxCode, { receivedBy: 'wang', destination: '上海柜台转寄' }));

  const reopened = await new Store(file).init();
  const box = reopened.findBox(boxCode);
  assert.equal(box.status, 'picked_up');
  assert.equal(box.pickup.destination, '上海柜台转寄');
  assert.equal(box.pieceIds.length, 2);
  assert.equal(box.pieceFaces.every((f) => f.headFace && f.tailFace), true);
  await rm(file, { force: true });
});
