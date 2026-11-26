import test from 'node:test';
import assert from 'node:assert/strict';
import { RepairService } from '../src/service.js';
import { MemoryStore } from '../src/store.js';
import { leakTopology } from '../src/scenarios.js';
import { windowsOverlap } from '../src/policy.js';

test('管家制度：许可时段重叠且源头相关时建议共案', () => {
  const svc = new RepairService({ store: new MemoryStore() });
  svc.setTopology(leakTopology());
  svc.createTicket({
    id: 'A1',
    residentName: '甲',
    location: '楼上',
    symptom: '滴水',
    pointIds: ['CP_JIA'],
    entryWindows: [
      { start: '2026-11-27T01:00:00Z', end: '2026-11-27T03:00:00Z' },
    ],
  });
  svc.createTicket({
    id: 'A2',
    residentName: '乙',
    location: '楼下',
    symptom: '返潮',
    pointIds: ['CP_YI'],
    adjacentTo: ['A1'],
    entryWindows: [
      { start: '2026-11-27T02:00:00Z', end: '2026-11-27T04:00:00Z' },
    ],
  });

  const recommendation = svc.getButlerRecommendation();
  assert.equal(recommendation.mode, 'joint_case');
  assert.deepEqual(recommendation.ticketIds, ['A1', 'A2']);
});

test('管家制度：无重叠时，相关公区确认结论建议借用而不是再派检查', () => {
  const svc = new RepairService({ store: new MemoryStore() });
  svc.setTopology(leakTopology());
  svc.createTicket({
    id: 'T2',
    residentName: '乙',
    location: '401',
    symptom: '返潮',
    pointIds: ['CP_YI'],
    entryWindows: [
      { start: '2026-11-27T02:00:00Z', end: '2026-11-27T04:00:00Z' },
    ],
  });
  svc.createTicket({
    id: 'T4',
    residentName: '丁',
    location: '邻户',
    symptom: '潮湿疑问',
    pointIds: ['CP_B_TEST'],
    adjacentTo: ['T2'],
    entryWindows: [
      { start: '2026-12-01T02:00:00Z', end: '2026-12-01T04:00:00Z' },
    ],
  });
  svc.recordInspection({
    id: 'PUB_B',
    ticketId: 'T2',
    segmentId: 'SEG_B_BRANCH',
    scope: 'public',
    confirmed: true,
    transferable: true,
    conclusion: 'B_EXCLUDED',
    pressureTest: { sourceId: 'B', result: 'dry', guaranteed: true },
  });

  const recommendation = svc.getButlerRecommendation();
  assert.equal(recommendation.mode, 'borrow_public_conclusion');
  assert.equal(recommendation.evidenceId, 'PUB_B');
  assert.equal(recommendation.targetTicketId, 'T4');
});

test('时段重叠判断为闭区间交叉', () => {
  assert.equal(
    windowsOverlap(
      [{ start: '2026-11-27T01:00:00Z', end: '2026-11-27T03:00:00Z' }],
      [{ start: '2026-11-27T03:00:00Z', end: '2026-11-27T04:00:00Z' }],
    ),
    true,
  );
  assert.equal(
    windowsOverlap(
      [{ start: '2026-11-27T01:00:00Z', end: '2026-11-27T02:00:00Z' }],
      [{ start: '2026-11-27T03:00:00Z', end: '2026-11-27T04:00:00Z' }],
    ),
    false,
  );
});
