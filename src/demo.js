import { mirrorHorizontal, normalizePattern } from './domain.js';

export const FLOWER_WITH_RED_LEFT = normalizePattern([
  'R....',
  '.BBB.',
  '.BYB.',
  '.BBB.',
  '.....'
]);

export const FLOWER_WITH_RED_RIGHT = mirrorHorizontal(FLOWER_WITH_RED_LEFT);

export const FLOWER_NO_RED = normalizePattern([
  '.....',
  '.BBB.',
  '.BYB.',
  '.BBB.',
  '.....'
]);

export const LEAF_DISTRACTOR = normalizePattern([
  '.....',
  '..G..',
  '.GGG.',
  '..G..',
  '.....'
]);

export async function seedDemo(store, { empty = false } = {}) {
  return store.mutate(() => {
    store.db.schemaVersion = 1;
    store.db.revision = 0;
    store.db.counters = {};
    for (const key of ['bars', 'cards', 'pieces', 'claims', 'orders', 'boxes', 'events']) {
      store.db[key].length = 0;
    }

    if (empty) return { seeded: false };

    const flowerCard = store.createCard({
      code: 'CARD-FLOWER',
      name: '蓝边黄心花（红点为额外标记，不检查）',
      directional: false,
      matchMode: 'mask',
      orientationPolicy: 'horizontal-ok',
      pattern: FLOWER_NO_RED
    });

    const redCard = store.createCard({
      code: 'CARD-REDL',
      name: '蓝边黄心＋左上单独红点（带方向）',
      directional: true,
      matchMode: 'mask',
      orientationPolicy: 'horizontal-ok',
      pattern: FLOWER_WITH_RED_LEFT
    });

    const bar = store.createBar({
      code: 'BAR-DEMO',
      length: 8,
      rows: 5,
      cols: 5,
      standardWidth: 3,
      kerfWidth: 1,
      closeMode: 'normal',
      shopMode: 'precut'
    });

    // 0：靠前一朵，红点在左上。它是 PIECE-0001 的向头面。
    store.pourLayer(bar.code, 0, FLOWER_WITH_RED_LEFT, { by: 'master-lin' });
    // 1：故意留空，演示空格不猜色。
    // 2：第一片的向尾面，放无关绿叶。
    store.pourLayer(bar.code, 2, LEAF_DISTRACTOR, { by: 'master-lin' });
    // 3：标准切法中的刀口格；即使藏着一朵同向花，也已被切口吃掉。
    store.pourLayer(bar.code, 3, FLOWER_WITH_RED_LEFT, { by: 'master-lin' });
    // 4：第二片的向头面，放无关绿叶。
    store.pourLayer(bar.code, 4, LEAF_DISTRACTOR, { by: 'master-lin' });
    // 5：故意留空。
    // 6：靠后一朵，红点在右上。它是 PIECE-0002 的向尾面。
    store.pourLayer(bar.code, 6, FLOWER_WITH_RED_RIGHT, { by: 'master-lin' });

    store.closeBar(bar.code, { closeMode: 'normal' });
    const pieces = store.precutBar({ barCode: bar.code, width: 3, kerfWidth: 1, clerk: 'system-precut' });

    return {
      seeded: true,
      bar: store.findBar(bar.code),
      cards: [flowerCard, redCard],
      pieces
    };
  });
}
