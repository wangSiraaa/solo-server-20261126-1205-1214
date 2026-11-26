#!/usr/bin/env node
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { JsonStore } from './store.js';
import { runRehearsal, rehearsalSummary } from './rehearsal.js';
import { complaintView } from './core.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const arg = process.argv.find((x) => x.startsWith('--order='));
const order = arg?.split('=')[1] ?? 'late-first';
const file = process.env.DEMO_DATA_FILE || path.join(__dirname, '..', 'data', `rehearsal-${order}.json`);

const store = new JsonStore(file);
const result = store.transact((db) => runRehearsal(db, { arrivalOrder: order }));

console.log(JSON.stringify(rehearsalSummary(result), null, 2));
if (!result.ok) {
  console.error('重演断言失败:', result.assertions);
  process.exit(1);
}

const reloaded = new JsonStore(file);
for (const no of ['TS-EXT', 'TS-LIFT']) {
  const complaint = reloaded.db.complaints.find((c) => c.complaintNo === no);
  const view = complaintView(reloaded.db, complaint.id);
  console.log(`\n投诉号 ${no}（重启后读取）：${view.computedStatus}`);
  for (const claim of view.claims) {
    for (const meter of claim.meters) {
      const by = meter.filledBy.map((f) => `${f.tripId}/${f.reportId}@${f.sweptAt}分`).join('，');
      console.log(`  ${meter.label}: ${meter.status === 'filled' ? `已由 ${by} 补齐` : '仍欠'}`);
    }
  }
}
console.log(`\n业务员入口：启动 npm start 后打开 http://127.0.0.1:3000 ，点“一键重演”。`);
