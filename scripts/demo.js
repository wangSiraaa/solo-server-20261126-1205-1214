'use strict';

const fs = require('node:fs');
const path = require('node:path');

const { JsonStore } = require('../src/json-store');
const { seedState } = require('../src/seed');
const { PlatformService } = require('../src/service');
const { runAllScenarios } = require('../src/scenarios');

const dataFile = process.env.DATA_FILE
  ? path.resolve(process.env.DATA_FILE)
  : path.join(__dirname, '..', 'data', 'platform.json');

fs.rmSync(dataFile, { force: true });
const store = new JsonStore(dataFile, seedState);
let service = new PlatformService(store);

const summary = runAllScenarios(service);

function assertEqual(actual, expected, label) {
  if (actual !== expected) {
    console.error(`✗ ${label}: expected ${expected}, got ${actual}`);
    process.exitCode = 1;
  } else {
    console.log(`✓ ${label}: ${actual}`);
  }
}

console.log('\n=== 合租房带看/接力/分佣演示 ===\n');
assertEqual(summary.openingEastOnlyWholeLease.storeAPoints, 4, '开场：只看东屋却签整套，甲店只提东屋 4 点');
assertEqual(summary.required.case1BothViewedBothSigned.storeAPoints, 8, '情形一：两屋实看且两屋签约，甲店 8 点');
assertEqual(summary.required.case2WestOnlySharedLivingRoom.storeAPoints, 4, '情形二：西屋只到门口看客厅，仅东屋 4 点');
assertEqual(summary.required.case3WestOnlyLease.storeAPoints, 4, '情形三：全程带看但只签西屋，仅西屋 4 点');
console.log(`✓ 首带保护制重叠单：甲店 ${summary.overlap.firstProtection.totals.A} 点，丙店 ${summary.overlap.firstProtection.totals.C} 点（另含议价点），乙店 ${summary.overlap.firstProtection.totals.B} 点`);
console.log(`✓ 接力分账制重叠单：甲店 ${summary.overlap.relayContribution.totals.A} 点，丙店 ${summary.overlap.relayContribution.totals.C} 点（另含议价点），乙店 ${summary.overlap.relayContribution.totals.B} 点`);
console.log(`✓ 重复结算：首次 ${summary.required.case1BothViewedBothSigned.extraction.totalPoints} 点；同幂等键重放返回同一单；换请求再付被 ${summary.required.case1BothViewedBothSigned.extraction.duplicateError.code} 拦截`);

// Simulate service restart by dropping the in-memory object and reopening file.
const reopenedStore = new JsonStore(dataFile, seedState);
service = new PlatformService(reopenedStore);
const tenantTrace = service.traceForTenant('SO-1-BOTH-VIEW-BOTH-SIGN', 'T-1001');
const storeTrace = service.traceForStore('SO-1-BOTH-VIEW-BOTH-SIGN', { clerkId: 'CLERK-A' });

console.log('\n=== 重启后凭原单号查回 ===\n');
assertEqual(tenantTrace.visits.length, 1, '租客 T-1001 查回现场确认回条数');
assertEqual(tenantTrace.leases.length, 1, '租客查回租约数');
assertEqual(tenantTrace.evidence.withdrawnPointTotal, 8, '租客视角查回本单已提取点数');
assertEqual(storeTrace.mySettlements.find((s) => s.id === 'STL-SO-1-A-8').totalPoints, 8, '甲店查回本单结算金额');
assertEqual(storeTrace.myMockAccount.balancePoints, 10, '甲店店员查回模拟账户总余额（本单 8 + 重叠接力单 2）');
console.log(`✓ 甲店店员查回结算单：${storeTrace.mySettlements[0].id}`);
console.log(`\n数据文件：${dataFile}`);
console.log('查询示例：');
console.log('  curl http://127.0.0.1:3000/tenants/T-1001/trace/SO-1-BOTH-VIEW-BOTH-SIGN');
console.log('  curl http://127.0.0.1:3000/stores/S-A/trace/SO-1-BOTH-VIEW-BOTH-SIGN');

fs.writeFileSync(path.join(path.dirname(dataFile), 'demo-summary.json'), JSON.stringify(summary, null, 2));

if (process.exitCode) process.exit(process.exitCode);
