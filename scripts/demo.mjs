import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { once } from 'node:events';
import { Store } from '../src/store.js';
import { createServer } from '../src/server.js';

const file = process.env.DATA_FILE ?? join(mkdtempSync(join(tmpdir(), 'joint-demo-')), 'election.json');
const store = new Store(file);
const server = createServer({ store });
server.listen(Number(process.env.PORT ?? 0), process.env.HOST ?? '127.0.0.1');
await once(server, 'listening');
const base = `http://127.0.0.1:${server.address().port}`;

async function call(path, options = {}) {
  const response = await fetch(`${base}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers ?? {}) },
  });
  const body = await response.json();
  return { status: response.status, body, headers: response.headers };
}

async function settledScenario(title, scenario, options = {}) {
  const roundId = options.roundId ?? `demo-${scenario}`;
  await call('/api/scenarios/seed', {
    method: 'POST',
    body: JSON.stringify({ scenario, options: { ...options, roundId } }),
  });
  await call(`/api/rounds/${roundId}/settle`, { method: 'POST' });
  const audit = await call(`/api/rounds/${roundId}/audit`);
  const roster = await call(`/api/rounds/${roundId}/duty-roster`);

  console.log(`\n=== ${title} ===`);
  for (const seat of roster.body.roster) {
    if (seat.status === 'filled') {
      console.log(
        `✅ ${seat.seatName}: ${seat.officeholder.name} 就任 ${seat.appointmentId} / ${seat.receiptCode}; 用票 ${seat.ballotIds.join(', ')}`,
      );
    } else {
      console.log(`⬜ ${seat.seatName}: 空席（无真实就任书）`);
    }
  }
  for (const item of audit.body.destinations) {
    const trail = item.trail.map((entry) => entry.reason).join(' -> ');
    console.log(`- ${item.ballotId}: ${item.status}${item.personId ? ` -> ${item.personId}/${item.seatId}` : ''} | ${trail}`);
  }
  return roundId;
}

await settledScenario('案卷 1：原四票，甲乙均上任，查回两组用票', 'transfer', { roundId: 'demo-1' });
await settledScenario('案卷 2：后项换成无球场资格的丙', 'ineligible-backup', { roundId: 'demo-2' });
await settledScenario('案卷 3：乙拒绝球场承诺', 'refused-backup', { roundId: 'demo-3' });
await settledScenario('案卷 4：甲愿任两席，偏好阅览', 'both-willing', {
  roundId: 'demo-4',
  preference: 'reading',
});
await settledScenario('附案：超过定额的取票规则', 'overquota', { roundId: 'demo-5' });

console.log('\n=== 并发：两个柜员同时发 demo-desk 的同一阅览席 ===');
await call('/api/scenarios/seed', {
  method: 'POST',
  body: JSON.stringify({ scenario: 'transfer', options: { roundId: 'demo-desk' } }),
});
const payload = JSON.stringify({ seatId: 'reading', personId: 'jia' });
const results = await Promise.all([
  call('/api/rounds/demo-desk/appointments', {
    method: 'POST',
    headers: { 'Idempotency-Key': 'demo-clerk-1' },
    body: payload,
  }),
  call('/api/rounds/demo-desk/appointments', {
    method: 'POST',
    headers: { 'Idempotency-Key': 'demo-clerk-2' },
    body: payload,
  }),
]);
for (const [index, result] of results.entries()) {
  console.log(`柜员${index + 1}: HTTP ${result.status} ${result.body.id ?? result.body.error?.code ?? ''}`);
  if (result.body.ballotIds) console.log(`  消耗票: ${result.body.ballotIds.join(', ')}; 回执: ${result.body.receiptCode}`);
  if (result.body.error?.details?.existingAppointment) {
    console.log(`  已存在就任书: ${result.body.error.details.existingAppointment.id}`);
  }
}
const winner = results.find((result) => result.status === 201).body;

server.close();
server.closeAllConnections?.();
await once(server, 'close');

console.log('\n=== 重启服务并凭回执/幂等键查回 ===');
const restartedStore = new Store(file);
const restarted = createServer({ store: restartedStore });
restarted.listen(0, '127.0.0.1');
await once(restarted, 'listening');
const restartedBase = `http://127.0.0.1:${restarted.address().port}`;
const retryResponse = await fetch(`${restartedBase}/api/rounds/demo-desk/appointments`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'demo-clerk-1' },
  body: payload,
});
const retry = await retryResponse.json();
console.log(`重发 Clerk-1 回执: HTTP ${retryResponse.status}, replay=${retryResponse.headers.get('idempotent-replayed')}, 就任书=${retry.id}, 用票=${retry.ballotIds.join(',')}`);
const receiptResponse = await fetch(`${restartedBase}/api/receipts/${winner.receiptCode}`);
const receipt = await receiptResponse.json();
console.log(`重启后 GET 回执: ${receipt.receiptCode}; 查回用票 ${receipt.usedBallotIds.join(', ')} -> 就任 ${receipt.appointment.personId}/${receipt.appointment.seatId}`);

restarted.close();
restarted.closeAllConnections?.();
await once(restarted, 'close');
console.log(`\n演示数据库（重启后仍保留）: ${file}`);
