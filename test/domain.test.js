import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { RepairService, DomainError } from '../src/service.js';
import { MemoryStore, JsonFileStore } from '../src/store.js';
import { seedScenario, leakTopology } from '../src/scenarios.js';

function service() {
  return new RepairService({ store: new MemoryStore() });
}

async function rejectsCode(promise, code) {
  await assert.rejects(
    promise,
    (error) => error instanceof DomainError && error.code === code,
  );
}

test('基准：A/B 拓扑下，B 加压干读数排除 B，两户约定干回访后均可结案签收', () => {
  const svc = service();
  seedScenario(svc, 'baseline');

  const jia = svc.evaluateClosure('T1');
  const yi = svc.evaluateClosure('T2');
  assert.equal(jia.eligible, true);
  assert.equal(yi.eligible, true);

  const yiCompatibility = svc.getCompatibility('T2');
  assert.deepEqual(yiCompatibility.combinations, [['A']]);
  assert.deepEqual(yiCompatibility.excludedSourceIds, ['B']);
  assert.deepEqual(yiCompatibility.unresolvedSourceIds, []);

  const notices = svc.state.notifications.filter((n) =>
    ['T1', 'T2'].includes(n.ticketId),
  );
  assert.equal(notices.length, 2);

  const signoff = svc.sign({ ticketId: 'T2', actorId: 'U_YI' });
  assert.equal(signoff.role, 'resident');
  assert.equal(svc.getTicketOrThrow('T2').status, 'closed');
});

test('撤下排除 B 的观察后：乙因 B 仍相容不能结案，甲仍可结案', () => {
  const svc = service();
  seedScenario(svc, 'withoutBExclusion');

  const jia = svc.evaluateClosure('T1');
  const yi = svc.evaluateClosure('T2');
  assert.equal(jia.eligible, true);
  assert.equal(yi.eligible, false);
  assert.equal(yi.reasons[0].code, 'UNRESOLVED_SOURCE');
  assert.deepEqual(yi.reasons[0].sourceIds, ['B']);
  assert.deepEqual(yi.compatibility.combinations, [['A'], ['A', 'B']]);

  // 普通回访两户均干，但 B 是间歇源，干读数不排除 B。
  const yiRevisit = svc.state.revisits.find((r) => r.ticketId === 'T2');
  assert.equal(yiRevisit.result, 'dry');

  assert.throws(
    () => svc.issueClosureNotice('T2'),
    (error) =>
      error instanceof DomainError &&
      error.code === 'CLOSURE_NOT_ALLOWED',
  );
  assert.deepEqual(
    svc.state.notifications.map((n) => n.ticketId),
    ['T1'],
  );
});

test('从基准直接撤下 B 证据时，已经生成的乙通知也不能再签收，且不重发甲通知', () => {
  const svc = service();
  seedScenario(svc, 'baseline');
  assert.equal(svc.state.notifications.length, 2);

  svc.retractInspection('INS_B_PRESSURE_DRY', '只撤下排除 B 的观察');

  assert.equal(svc.evaluateClosure('T2').eligible, false);
  assert.equal(svc.evaluateClosure('T1').eligible, true);
  assert.equal(svc.state.notifications.length, 2);
  assert.throws(() => svc.sign({ ticketId: 'T2', actorId: 'U_YI' }));
  assert.equal(svc.state.signoffs.length, 0);
});

test('来源已唯一，但乙在约定时段尚未入户回访时仍欠回访', () => {
  const svc = service();
  seedScenario(svc, 'missingYiRevisit');

  const yiCompatibility = svc.getCompatibility('T2');
  assert.deepEqual(yiCompatibility.combinations, [['A']]);
  assert.deepEqual(yiCompatibility.unresolvedSourceIds, []);

  const yi = svc.evaluateClosure('T2');
  assert.equal(yi.eligible, false);
  assert.equal(yi.reasons[0].code, 'REVISIT_MISSING');

  const nextStep = svc.getTicketNextStep('T2', 'maintainer');
  assert.equal(nextStep.code, 'RECORD_CONTRACTUAL_REVISIT');

  assert.equal(svc.evaluateClosure('T1').eligible, true);
});

test('丙户一次干回访的临时处置可签收，但其条款/结果不能替乙结案', () => {
  const svc = service();
  seedScenario(svc, 'withoutBExclusion', { includeBing: true });

  const bing = svc.evaluateClosure('T3');
  assert.equal(bing.eligible, true);
  assert.equal(
    svc.getTicketOrThrow('T3').contract.kind,
    'temporary_dry_once',
  );
  const signoff = svc.sign({ ticketId: 'T3', actorId: 'U_BING' });
  assert.equal(signoff.ticketId, 'T3');
  assert.equal(svc.getTicketOrThrow('T3').status, 'closed');

  const yi = svc.evaluateClosure('T2');
  assert.equal(yi.eligible, false);
  assert.equal(yi.reasons[0].code, 'UNRESOLVED_SOURCE');
  assert.equal(
    svc.state.notifications.some((n) => n.ticketId === 'T2'),
    false,
  );
  assert.throws(() => svc.sign({ ticketId: 'T2', actorId: 'U_YI' }));
});

test('至多六张住户单，第七张被拒绝', () => {
  const svc = service();
  for (let i = 1; i <= 6; i += 1) {
    svc.createTicket({
      id: `LIMIT_${i}`,
      residentName: `住户${i}`,
      location: `楼栋位置${i}`,
      symptom: '返潮',
    });
  }
  assert.equal(svc.state.tickets.length, 6);
  assert.throws(
    () =>
      svc.createTicket({
        id: 'LIMIT_7',
        residentName: '第七户',
        location: '超限位置',
        symptom: '滴水',
      }),
    (error) => error.code === 'TICKET_LIMIT',
  );
});

test('重发同一回访、重复结案扫描或隔天重启服务，均不重复通知', () => {
  const file = path.join(
    os.tmpdir(),
    `repair-service-${Date.now()}-${Math.random()}.json`,
  );
  const store1 = new JsonFileStore(file);
  const svc1 = new RepairService({ store: store1 });
  seedScenario(svc1, 'baseline');
  assert.equal(svc1.state.notifications.length, 2);

  const repeatedJoint = svc1.recordJointRevisit({
    caseId: 'CASE_JIA_YI',
    jointRevisitId: 'JR_DAY2_BOTH_DRY',
    results: [
      { revisitId: 'RV_JIA_DRY', ticketId: 'T1', result: 'dry' },
      { revisitId: 'RV_YI_DRY', ticketId: 'T2', result: 'dry' },
    ],
  });
  assert.equal(repeatedJoint.duplicate, true);
  svc1.recordRevisit({
    ticketId: 'T1',
    revisitId: 'RV_JIA_DRY',
    result: 'dry',
  });
  svc1.dispatchClosureNotices({ at: '2026-11-29T00:30:00Z' });
  svc1.dispatchClosureNotices({ at: '2026-11-29T01:30:00Z' });
  assert.equal(svc1.state.notifications.length, 2);

  // 模拟隔天重新启动服务：从同一 JSON 文件恢复。
  const svc2 = new RepairService({ store: new JsonFileStore(file) });
  svc2.dispatchClosureNotices({ at: '2026-11-30T01:00:00Z' });
  assert.equal(svc2.state.notifications.length, 2);

  const firstSignoff = svc2.sign({ ticketId: 'T1', actorId: 'U_JIA' });
  const repeatedSignoff = svc2.sign({ ticketId: 'T1', actorId: 'U_JIA' });
  assert.equal(firstSignoff.id, repeatedSignoff.id);
  assert.equal(
    svc2.state.signoffs.filter((s) => s.ticketId === 'T1').length,
    1,
  );

  fs.rmSync(file, { force: true });
});

test('公区确认结论可借给相邻工单；私人室内观察不得复制', () => {
  const svc = service();
  seedScenario(svc, 'baseline');
  svc.createTicket({
    id: 'T4',
    residentId: 'U_DING',
    residentName: '丁',
    location: '相邻公区支路另一端',
    symptom: '检查点潮湿疑问',
    pointIds: ['CP_YI'],
    adjacentTo: ['T2'],
    contract: { kind: 'strict_source_exclusion' },
  });

  svc.recordInspection({
    id: 'INS_YI_PRIVATE',
    ticketId: 'T2',
    segmentId: 'SEG_YI_BRANCH',
    scope: 'private',
    authorized: true,
    readings: [{ pointId: 'CP_YI', wet: true }],
    conclusion: '乙室內私域痕迹',
  });

  const before = svc.getCompatibility('T4');
  assert.equal(before.excludedSourceIds.length, 0);

  const borrow = svc.borrowEvidence({
    evidenceId: 'INS_B_PRESSURE_DRY',
    targetTicketId: 'T4',
    actorId: 'butler',
  });
  assert.equal(borrow.publicConclusion.includes('B_EXCLUDED'), true);
  assert.equal(borrow.readings, undefined);

  const after = svc.getCompatibility('T4');
  assert.deepEqual(after.excludedSourceIds, ['B']);

  assert.throws(
    () =>
      svc.borrowEvidence({
        evidenceId: 'INS_YI_PRIVATE',
        targetTicketId: 'T4',
      }),
    (error) => error.code === 'PRIVATE_EVIDENCE_NOT_TRANSFERABLE',
  );
  assert.equal(
    svc.state.borrows.some((b) => b.evidenceId === 'INS_YI_PRIVATE'),
    false,
  );

  // 借入的公区结论可真正用于邻单：补 A 维修和本户回访后，丁单可结案。
  const t4 = svc.getTicketOrThrow('T4');
  t4.revisitWindows = [
    {
      id: 'RW_T4',
      start: '2026-11-28T06:00:00Z',
      end: '2026-11-28T08:00:00Z',
    },
  ];
  t4.contract.revisitWindowId = 'RW_T4';
  svc.registerRepair({ ticketId: 'T4', sourceIds: ['A'] });
  svc.recordRevisit({
    ticketId: 'T4',
    revisitId: 'RV_T4_DRY',
    at: '2026-11-28T06:20:00Z',
    result: 'dry',
  });
  const t4Closure = svc.evaluateClosure('T4');
  assert.equal(t4Closure.eligible, true, JSON.stringify(t4Closure.reasons));
});

test('共案可定检查次序、发队、逐户回访；证实不同源拆案不抹掉回访和通知', () => {
  const svc = service();
  svc.setTopology(leakTopology());
  svc.createTicket({
    id: 'T1',
    residentId: 'U_JIA',
    residentName: '甲',
    location: '楼上',
    symptom: '滴水',
    pointIds: ['CP_JIA'],
    revisitWindows: [
      { id: 'RW1', start: '2026-11-28T02:00:00Z', end: '2026-11-28T04:00:00Z' },
    ],
    contract: { kind: 'standard', revisitWindowId: 'RW1' },
  });
  svc.createTicket({
    id: 'T2',
    residentId: 'U_YI',
    residentName: '乙',
    location: '楼下',
    symptom: '天花板返潮',
    pointIds: ['CP_YI'],
    revisitWindows: [
      { id: 'RW2', start: '2026-11-28T06:00:00Z', end: '2026-11-28T08:00:00Z' },
    ],
    contract: {
      kind: 'strict_source_exclusion',
      revisitWindowId: 'RW2',
    },
  });

  const created = svc.createCase({ ticketIds: ['T1', 'T2'] });
  assert.equal(created.status, 'planning');
  assert.throws(() => svc.dispatchCase({ caseId: created.id }));

  svc.planCase({
    caseId: created.id,
    inspectionOrder: ['CP_JIA', 'ISOLATE_A_THEN_CP_YI'],
  });
  svc.dispatchCase({ caseId: created.id });

  svc.recordInspection({
    id: 'I_JIA',
    caseId: created.id,
    ticketId: 'T1',
    segmentId: 'SEG_JIA_BRANCH',
    readings: [{ pointId: 'CP_JIA', wet: true }],
  });
  svc.recordInspection({
    id: 'I_YI_STILL_WET',
    caseId: created.id,
    ticketId: 'T2',
    segmentId: 'SEG_YI_BRANCH',
    kind: 'isolation_water_test',
    controlled: true,
    isolatedSegmentIds: ['SEG_A_SUPPLY'],
    readings: [{ pointId: 'CP_YI', wet: true }],
    conclusion: '隔开 A 后乙点仍湿，B 独立存在',
  });

  assert.deepEqual(svc.getCompatibility('T2').combinations, [['A', 'B']]);

  svc.registerRepair({ caseId: created.id, sourceIds: ['A'] });
  const joint = svc.recordJointRevisit({
    caseId: created.id,
    jointRevisitId: 'JR_SPLIT_CASE',
    results: [
      { ticketId: 'T1', revisitId: 'R1', at: '2026-11-28T02:30:00Z', result: 'dry' },
      { ticketId: 'T2', revisitId: 'R2', at: '2026-11-28T06:30:00Z', result: 'dry' },
    ],
  });
  assert.equal(joint.revisits.length, 2);

  const split = svc.splitCase({
    caseId: created.id,
    ticketIds: ['T2'],
    reason: '隔离 A 后乙点仍湿，证实乙为 B 源，与甲不同源',
  });
  assert.deepEqual(split.preservedRevisitIds, ['R2']);
  assert.deepEqual(
    svc.state.revisits.map((r) => r.revisitId).sort(),
    ['R1', 'R2'],
  );
  assert.equal(svc.getTicketOrThrow('T1').activeCaseId, created.id);
  assert.equal(svc.getTicketOrThrow('T2').activeCaseId, split.newCase.id);

  // 甲的结案通知和签收不受拆案影响。
  const jiaDecision = svc.evaluateClosure('T1');
  assert.equal(jiaDecision.eligible, true);
  const jiaNotice = svc.state.notifications.find((n) => n.ticketId === 'T1');
  assert.ok(jiaNotice);
  const signoff = svc.sign({ ticketId: 'T1', actorId: 'U_JIA' });
  assert.equal(signoff.notificationId, jiaNotice.id);

  // 乙虽有普通干回访，但 B 未排除/未修，拆入新案后仍有下一步。
  const yiDecision = svc.evaluateClosure('T2');
  assert.equal(yiDecision.eligible, false);
  assert.equal(yiDecision.reasons[0].code, 'UNRESOLVED_SOURCE');
});

test('住户可申请复查；新的干回访周期产生新通知，同一结果不重复', () => {
  const svc = service();
  seedScenario(svc, 'baseline');
  svc.sign({ ticketId: 'T1', actorId: 'U_JIA' });
  assert.equal(svc.getTicketOrThrow('T1').status, 'closed');

  const recheck = svc.requestRecheck({
    ticketId: 'T1',
    residentId: 'U_JIA',
    reason: '雨后又滴水',
  });
  assert.equal(
    svc.getTicketNextStep('T1', 'resident').code,
    'WAIT_RECHECK_VISIT',
  );
  const sameOpen = svc.requestRecheck({ ticketId: 'T1', residentId: 'U_JIA' });
  assert.equal(sameOpen.id, recheck.id);

  svc.recordRevisit({
    ticketId: 'T1',
    recheckId: recheck.id,
    revisitId: 'RV_RECHECK_WET',
    at: '2026-11-28T03:10:00Z',
    result: 'wet',
  });
  assert.equal(svc.evaluateClosure('T1').eligible, false);

  svc.recordRevisit({
    ticketId: 'T1',
    revisitId: 'RV_RECHECK_DRY',
    at: '2026-11-28T03:30:00Z',
    result: 'dry',
  });
  assert.equal(svc.evaluateClosure('T1').eligible, true);
  const t1Notices = () =>
    svc.state.notifications.filter((n) => n.ticketId === 'T1');
  assert.equal(t1Notices().length, 2);

  svc.dispatchClosureNotices();
  assert.equal(t1Notices().length, 2);
  const signoff = svc.sign({ ticketId: 'T1', actorId: 'U_JIA' });
  assert.equal(signoff.role, 'resident');
});

test('模拟加压的湿读数按 iff 规则锁定 B，普通干回访仍不排除间歇源', () => {
  const svc = service();
  svc.setTopology(leakTopology());
  svc.createTicket({
    id: 'T2',
    residentName: '乙',
    location: '楼下',
    symptom: '返潮',
    pointIds: ['CP_YI'],
  });
  svc.recordInspection({
    id: 'PRESSURE_WET',
    ticketId: 'T2',
    segmentId: 'SEG_B_BRANCH',
    kind: 'pressure_test',
    controlled: true,
    isolatedSegmentIds: ['SEG_A_SUPPLY'],
    pressureTest: {
      guaranteed: true,
      sourceId: 'B',
      result: 'wet',
    },
    readings: [{ pointId: 'CP_B_TEST', wet: true, deterministic: true }],
  });

  const combinations = svc.getCompatibility('T2').combinations;
  assert.ok(combinations.length > 0);
  assert.ok(combinations.every((combo) => combo.includes('B')));

  // 普通回访干读数不进入相容性证据；不会把 B 从上述组合中移除。
  svc.recordRevisit({
    ticketId: 'T2',
    revisitId: 'ORDINARY_DRY',
    at: '2026-11-28T06:30:00Z',
    result: 'dry',
  });
  assert.ok(
    svc
      .getCompatibility('T2')
      .combinations.every((combo) => combo.includes('B')),
  );
});

test('候选源头至多四个，组合模型不允许超过两处独立源头', () => {
  const svc = service();
  assert.throws(
    () =>
      svc.setTopology({
        sources: ['S1', 'S2', 'S3', 'S4', 'S5'].map((id) => ({ id })),
        edges: [],
        points: [],
      }),
    (error) => error.code === 'TOO_MANY_SOURCES',
  );

  svc.setTopology({
    sources: ['A', 'B', 'C', 'D'].map((id) => ({ id })),
    points: [{ id: 'P' }],
    edges: [
      { from: 'A', to: 'P' },
      { from: 'B', to: 'P' },
      { from: 'C', to: 'P' },
      { from: 'D', to: 'P' },
    ],
  });
  svc.createTicket({
    id: 'T',
    residentName: '测试',
    location: '位置',
    symptom: '湿',
    pointIds: ['P'],
  });
  svc.recordInspection({
    ticketId: 'T',
    segmentId: 'X',
    readings: [{ pointId: 'P', wet: true }],
  });
  const combinations = svc.getCompatibility('T').combinations;
  assert.ok(combinations.every((combo) => combo.length <= 2));
  assert.ok(combinations.some((combo) => combo.length === 2));
});
