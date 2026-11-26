import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { RepairService } from '../src/service.js';
import { MemoryStore, JsonFileStore } from '../src/store.js';
import { seedScenario } from '../src/scenarios.js';

const results = [];
function check(name, fn) {
  fn();
  results.push(`✅ ${name}`);
}

function fresh(variant, options = {}) {
  const service = new RepairService({ store: new MemoryStore() });
  seedScenario(service, variant, options);
  return service;
}

check('基准同源：甲、乙均满足各自合同并收到结案通知', () => {
  const service = fresh('baseline');
  assert.equal(service.evaluateClosure('T1').eligible, true);
  assert.equal(service.evaluateClosure('T2').eligible, true);
  assert.equal(service.state.notifications.length, 2);
  assert.deepEqual(service.getCompatibility('T2').combinations, [['A']]);
});

check('撤下排除 B 的加压观察：甲可结，乙被 UNRESOLVED_SOURCE[B] 阻断', () => {
  const service = fresh('withoutBExclusion');
  assert.equal(service.evaluateClosure('T1').eligible, true);
  const yi = service.evaluateClosure('T2');
  assert.equal(yi.eligible, false);
  assert.equal(yi.reasons[0].code, 'UNRESOLVED_SOURCE');
  assert.deepEqual(yi.reasons[0].sourceIds, ['B']);
  assert.equal(
    service.state.notifications.some((n) => n.ticketId === 'T2'),
    false,
  );
});

check('保留 B 排除但乙在约定时段未回访：来源唯一为 A，乙仍被 REVISIT_MISSING 阻断', () => {
  const service = fresh('missingYiRevisit');
  const compatibility = service.getCompatibility('T2');
  assert.deepEqual(compatibility.combinations, [['A']]);
  const yi = service.evaluateClosure('T2');
  assert.equal(yi.eligible, false);
  assert.equal(yi.reasons[0].code, 'REVISIT_MISSING');
});

check('丙户一次干回访临时处置可签收，且不能替代乙的严格合同', () => {
  const service = fresh('withoutBExclusion', { includeBing: true });
  assert.equal(service.evaluateClosure('T3').eligible, true);
  service.sign({ ticketId: 'T3', actorId: 'U_BING' });
  assert.equal(service.getTicketOrThrow('T3').status, 'closed');
  assert.equal(service.evaluateClosure('T2').eligible, false);
});

check('六单上限', () => {
  const service = new RepairService({ store: new MemoryStore() });
  for (let i = 1; i <= 6; i += 1) {
    service.createTicket({
      id: `V${i}`,
      residentName: `住户${i}`,
      location: `位置${i}`,
      symptom: '返潮',
    });
  }
  assert.throws(() =>
    service.createTicket({
      id: 'V7',
      residentName: '第七户',
      location: '超限',
      symptom: '滴水',
    }),
  );
});

check('同一回访重发、重复扫描、隔天从文件重启均不重复通知', () => {
  const file = path.join(
    os.tmpdir(),
    `repair-verify-${Date.now()}-${Math.random()}.json`,
  );
  const first = new RepairService({ store: new JsonFileStore(file) });
  seedScenario(first, 'baseline');
  assert.equal(first.state.notifications.length, 2);
  first.recordJointRevisit({
    caseId: 'CASE_JIA_YI',
    jointRevisitId: 'JR_DAY2_BOTH_DRY',
    results: [
      { ticketId: 'T1', revisitId: 'RV_JIA_DRY', result: 'dry' },
      { ticketId: 'T2', revisitId: 'RV_YI_DRY', result: 'dry' },
    ],
  });
  first.dispatchClosureNotices({ at: '2026-11-30T00:00:00Z' });
  assert.equal(first.state.notifications.length, 2);

  const reopened = new RepairService({ store: new JsonFileStore(file) });
  reopened.dispatchClosureNotices({ at: '2026-12-01T00:00:00Z' });
  assert.equal(reopened.state.notifications.length, 2);
  fs.rmSync(file, { force: true });
});

console.log(results.join('\n'));
console.log(`\n全部 ${results.length} 项业务核验通过。`);
