import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');
const dataFile = path.join(root, 'data', 'e2e-state.json');
const logFile = path.join(root, 'data', 'e2e-server.log');
const A = Number(process.env.DEMO_A_PORT || 4311);
const B = Number(process.env.DEMO_B_PORT || 4312);
const M = Number(process.env.DEMO_MAINT_PORT || 4400);

fs.rmSync(dataFile, { force: true });
fs.mkdirSync(path.dirname(logFile), { recursive: true });
const logFd = fs.openSync(logFile, 'w');

let child;
function startServer() {
  const p = spawn(process.execPath, [path.join(root, 'src', 'server.mjs')], {
    cwd: root,
    env: {
      ...process.env,
      DATA_FILE: dataFile,
      POS_A_PORT: String(A),
      POS_B_PORT: String(B),
      MAINT_PORT: String(M),
    },
    stdio: ['ignore', logFd, logFd],
  });
  p.on('exit', (code) => {
    if (code !== 0 && code !== null) console.error(`server exited code=${code}; see ${logFile}`);
  });
  return p;
}

async function stopServer(p) {
  p.kill('SIGTERM');
  await once(p, 'exit');
}

async function waitHealth(port, name) {
  for (let i = 0; i < 60; i += 1) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/health`);
      if (r.ok) return;
    } catch {}
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`${name} did not become healthy; see ${logFile}`);
}

async function req(port, method, route, body) {
  const response = await fetch(`http://127.0.0.1:${port}${route}`, {
    method,
    headers: { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let json = null;
  const text = await response.text();
  try { json = text ? JSON.parse(text) : null; } catch { json = { raw: text }; }
  return { status: response.status, body: json };
}

let passed = 0;
const failures = [];
function check(name, condition, detail = '') {
  if (condition) {
    passed += 1;
    console.log(`  ✓ ${name}`);
  } else {
    failures.push(`${name} ${detail}`);
    console.log(`  ✗ ${name} ${detail}`);
  }
}

async function call(name, expected, port, method, route, body) {
  const r = await req(port, method, route, body);
  const ok = Array.isArray(expected) ? expected.includes(r.status) : r.status === expected;
  check(`${name} [${method} ${route} -> ${r.status}]`, ok,
    ok ? '' : `expected ${expected}, error=${r.body?.error} ${JSON.stringify(r.body?.details || {})}`);
  return r;
}

async function getOrder(port, id) {
  return (await req(port, 'GET', `/orders/${id}`)).body.order;
}

async function deliverNext(port, orderId, prefix) {
  const next = await call(`${prefix}: choose next coin`, 202, port, 'POST', `/orders/${orderId}/dispense-next`, {
    requestId: `${prefix}-DSP`,
  });
  const dispenseId = next.body.instruction.id;
  await call(`${prefix}: channel sensor`, 200, M, 'POST', `/machines/${port === A ? 'A' : 'B'}/events/channel-sensor`, {
    requestId: `${prefix}-SENSOR`, dispenseId,
  });
  const arrived = await call(`${prefix}: final arrival`, 200, M, 'POST', `/machines/${port === A ? 'A' : 'B'}/events/coin-arrived`, {
    requestId: `${prefix}-ARRIVAL`, dispenseId,
  });
  return arrived;
}

function trayIds(order) {
  return order.trayCoins.map((c) => c.id);
}

console.log('Starting empty local backend...');
child = startServer();
await waitHealth(A, 'sales-A');
await waitHealth(B, 'sales-B');
await waitHealth(M, 'maintenance');

try {
  console.log('\n1. Empty machine and maintenance restock');
  let rec = (await call('empty A reconciliation', 200, M, 'GET', '/machines/A/reconciliation')).body.reconciliation;
  check('A starts balanced with zero orders/coins', rec.balanced && rec.tubeValue === 0 && rec.orderCount === 0);

  await call('restock example coins: one 2 and three 1', 201, M, 'POST', '/machines/A/restock', {
    requestId: 'RESTOCK-A-EXAMPLE',
    coins: [
      { id: 'A-T2-1', denomination: 2 },
      { id: 'A-T1-1', denomination: 1 },
      { id: 'A-T1-2', denomination: 1 },
      { id: 'A-T1-3', denomination: 1 },
    ],
  });
  await call('restock B for isolation/refund demo', 201, M, 'POST', '/machines/B/restock', {
    requestId: 'RESTOCK-B',
    coins: [
      { id: 'B-T5-1', denomination: 5 },
      { id: 'B-T2-1', denomination: 2 },
      { id: 'B-T2-2', denomination: 2 },
      { id: 'B-T1-1', denomination: 1 },
      { id: 'B-T1-2', denomination: 1 },
    ],
  });
  rec = (await req(M, 'GET', '/machines/A/reconciliation')).body.reconciliation;
  check('A tube contains exactly 5 yuan', rec.tubeValue === 5 && rec.balanced);

  console.log('\n2. A 5-yuan payment for a 7-yuan soda cannot vend');
  await call('create underpaid twin order A-05', 201, A, 'POST', '/orders', {
    requestId: 'A05-CREATE', orderId: 'A-05', totalAmount: 7, mode: 'STREAM',
  });
  await call('register only a 5-yuan coin', 201, A, 'POST', '/orders/A-05/coins', {
    requestId: 'A05-PAY', coinId: 'A05-PAY-5', denomination: 5,
  });
  const underVend = await call('underpaid vend is rejected', 402, A, 'POST', '/orders/A-05/vend', {
    requestId: 'A05-VEND',
  });
  check('shortfall is 2 yuan', underVend.body.details?.missingAmount === 2);

  console.log('\n3. Main order: 7-yuan soda, 10 paid, 3 change');
  await call('create A-1001', 201, A, 'POST', '/orders', {
    requestId: 'A1-CREATE', orderId: 'A-1001', sku: 'SODA', totalAmount: 7, mode: 'STREAM',
  });
  const pay = await call('register 10-yuan inserted coin', 201, A, 'POST', '/orders/A-1001/coins', {
    requestId: 'A1-PAY', coinId: 'A1-PAY-10', denomination: 10,
  });
  check('paid amount is 10', pay.body.order.paidAmount === 10);
  const payReplay = await call('exact payment replay is idempotent', 201, A, 'POST', '/orders/A-1001/coins', {
    requestId: 'A1-PAY', coinId: 'A1-PAY-10', denomination: 10,
  });
  check('payment replay marked and still one coin', payReplay.body.replayed === true && payReplay.body.order.coinIds.length === 1);
  await call('same request id cannot change 10 to 5', 409, A, 'POST', '/orders/A-1001/coins', {
    requestId: 'A1-PAY', coinId: 'A1-PAY-10', denomination: 5,
  });
  await call('A-1001 vend allowed after immutable 10 payment', 202, A, 'POST', '/orders/A-1001/vend', {
    requestId: 'A1-VEND',
  });
  await call('goods port reports soda delivered', 200, M, 'POST', '/machines/A/events/goods-delivery', {
    requestId: 'A1-GOODS', eventId: 'RCPT-A1-GOODS', orderId: 'A-1001',
  });
  await call('duplicate old goods receipt returns same fact', 200, M, 'POST', '/machines/A/events/goods-delivery', {
    requestId: 'A1-GOODS', eventId: 'RCPT-A1-GOODS', orderId: 'A-1001',
  });

  const first = await call('first change coin chooses 2', 202, A, 'POST', '/orders/A-1001/dispense-next', {
    requestId: 'A1-DSP2',
  });
  check('chosen denomination is 2 and preserves small coins', first.body.instruction.denomination === 2);
  const dsp2 = first.body.instruction.id;
  await call('2-yuan channel sensor', 200, M, 'POST', '/machines/A/events/channel-sensor', {
    requestId: 'A1-SENS2', dispenseId: dsp2,
  });
  await call('2-yuan coin reaches pickup', 200, M, 'POST', '/machines/A/events/coin-arrived', {
    requestId: 'A1-ARR2', dispenseId: dsp2,
  });

  const second = await call('next chooses 1, leaving other ones for next order', 202, A, 'POST', '/orders/A-1001/dispense-next', {
    requestId: 'A1-DSP1',
  });
  check('second chosen denomination is 1', second.body.instruction.denomination === 1);
  const dsp1 = second.body.instruction.id;
  await call('1-yuan coin passes sensor', 200, M, 'POST', '/machines/A/events/channel-sensor', {
    requestId: 'A1-SENS1-JAM', dispenseId: dsp1,
  });
  await call('same coin jams in last segment', 200, M, 'POST', '/machines/A/events/coin-jam', {
    requestId: 'A1-JAM1', dispenseId: dsp1, location: 'LAST_SEGMENT', reason: 'PHYSICAL_JAM',
  });

  let order = await getOrder(A, 'A-1001');
  check('only 2 yuan is actually at pickup, not 3', order.handedOutAmount === 2 && order.collectedAmount === 0);
  check('order still has a 1-yuan claimable difference', order.totalClaimRemaining === 1 && order.openClaims[0]?.amountRemaining === 1);
  check('tray contains only the 2-yuan coin', order.trayCoins.length === 1 && order.trayCoins[0].denomination === 2);

  console.log('\n4. Repeated pickup-door events do not prove collection');
  const door1 = { requestId: 'A1-DOOR-1', orderId: 'A-1001' };
  const doorResp = await call('first pickup-door open', 200, M, 'POST', '/machines/A/events/pickup-door', door1);
  await call('duplicate pickup-door callback', 200, M, 'POST', '/machines/A/events/pickup-door', door1);
  await call('a second physical door opening is still only a fact', 200, M, 'POST', '/machines/A/events/pickup-door', {
    requestId: 'A1-DOOR-2', orderId: 'A-1001',
  });
  order = await getOrder(A, 'A-1001');
  check('two distinct opens recorded, duplicate not triple-counted', order.doorOpenCount === 2);
  check('door opens did not mark the 2-yuan coin collected', order.collectedAmount === 0 && trayIds(order).length === 1);
  check('door response warns it is not proof', /not proof/i.test(doorResp.body.warning));

  console.log('\n5. Power cycle while 1 yuan is jammed and 2 yuan waits in tray');
  await stopServer(child);
  child = startServer();
  await waitHealth(A, 'sales-A');
  await waitHealth(M, 'maintenance');
  order = await getOrder(A, 'A-1001');
  check('A-1001 survives restart with 2 handed out and 1 pending', order.handedOutAmount === 2 && order.totalClaimRemaining === 1);
  await call('door idempotency survives restart', 200, M, 'POST', '/machines/A/events/pickup-door', door1);
  const midAudit = await call('physical coin count matches after restart', 200, M, 'POST', '/machines/A/audit', {
    requestId: 'A-AUDIT-MID',
    physicalCounts: {
      TUBE: { 1: 2 },
      JAMMED: { 1: 1 },
      TRAY: { 2: 1 },
      ESCROW: { 5: 1, 10: 1 },
    },
    customerObtainedAmount: 0,
  });
  check('mid-state audit is balanced', midAudit.body.reconciliation.balanced && midAudit.body.reconciliation.physicalComparison.matches);

  console.log('\n6. Adjacent machine B pays; it cannot erase A claim');
  await call('create B-NEIGHBOR', 201, B, 'POST', '/orders', {
    requestId: 'BN-CREATE', orderId: 'B-NEIGHBOR', totalAmount: 7,
  });
  await call('B inserted 10', 201, B, 'POST', '/orders/B-NEIGHBOR/coins', {
    requestId: 'BN-PAY', coinId: 'BN-PAY-10', denomination: 10,
  });
  await call('B vend', 202, B, 'POST', '/orders/B-NEIGHBOR/vend', { requestId: 'BN-VEND' });
  await deliverNext(B, 'B-NEIGHBOR', 'BN-2');
  await deliverNext(B, 'B-NEIGHBOR', 'BN-1');
  let bOrder = await getOrder(B, 'B-NEIGHBOR');
  await call('customer takes B change', 200, M, 'POST', '/machines/B/pickup/confirm', {
    requestId: 'BN-TAKE', takenCoinIds: trayIds(bOrder),
  });
  order = await getOrder(A, 'A-1001');
  check('A still shows its own 1-yuan difference after B payout', order.totalClaimRemaining === 1);
  const cross = await req(B, 'GET', '/orders/A-1001');
  check('A order id is not visible on B sales terminal', cross.status === 404);

  console.log('\n7. Failed B refund deducts the 2 yuan already issued');
  await call('create B-REFUND', 201, B, 'POST', '/orders', {
    requestId: 'BR-CREATE', orderId: 'B-REFUND', totalAmount: 7,
  });
  await call('B refund order inserted 10', 201, B, 'POST', '/orders/B-REFUND/coins', {
    requestId: 'BR-PAY', coinId: 'BR-PAY-10', denomination: 10,
  });
  await call('B refund order vend', 202, B, 'POST', '/orders/B-REFUND/vend', { requestId: 'BR-VEND' });
  const br2 = await call('B issues 2 before failure', 202, B, 'POST', '/orders/B-REFUND/dispense-next', {
    requestId: 'BR-DSP2',
  });
  let d = br2.body.instruction.id;
  await call('B 2 sensor', 200, M, 'POST', '/machines/B/events/channel-sensor', { requestId: 'BR-S2', dispenseId: d });
  await call('B 2 arrival', 200, M, 'POST', '/machines/B/events/coin-arrived', { requestId: 'BR-A2', dispenseId: d });
  const br1 = await call('B selects 1 for next coin', 202, B, 'POST', '/orders/B-REFUND/dispense-next', { requestId: 'BR-DSP1' });
  d = br1.body.instruction.id;
  await call('B failing 1 sensor', 200, M, 'POST', '/machines/B/events/channel-sensor', { requestId: 'BR-S1', dispenseId: d });
  await call('B failing 1 jams', 200, M, 'POST', '/machines/B/events/coin-jam', {
    requestId: 'BR-J1', dispenseId: d, location: 'LAST_SEGMENT',
  });
  const cancel = await call('cancel before goods delivery creates net 8 refund', 200, B, 'POST', '/orders/B-REFUND/cancel', {
    requestId: 'BR-CANCEL', refundMode: 'NET',
  });
  const refundClaim = cancel.body.refundClaims
    .map((id) => cancel.body.order.claims.find((c) => c.id === id))
    .find((c) => c.kind === 'REFUND_NET');
  check('net refund is 10 paid minus 2 already issued = 8', refundClaim?.amountExpected === 8 && refundClaim.deductedHandedOutAmount === 2);

  console.log('\n8. Repair A channel, replay repair/receipt, then claim A difference');
  const blockedClaim = await call('claim fact is accepted while route is still broken', 202, A, 'POST', '/orders/A-1001/claims/CLM-A-1001-CHANGE/claim', {
    requestId: 'A1-CLAIM-BLOCKED',
  });
  check('accepted claim still has one yuan remaining', blockedClaim.body.claim.amountRemaining === 1);
  const blockedDispense = await req(A, 'POST', '/orders/A-1001/claims/CLM-A-1001-CHANGE/dispense-next', {
    requestId: 'A1-CLAIM-DSP-BLOCKED',
  });
  check('blocked dispense returns 409 and claim survives', blockedDispense.status === 409 && blockedDispense.body.error === 'NO_DELIVERABLE_COIN');
  const repairBody = { requestId: 'A1-REPAIR-R1', eventId: 'REPAIR-R1', channelId: 'route-1' };
  await call('repair route-1', 200, M, 'POST', '/machines/A/events/channel-repaired', repairBody);
  await call('duplicate repair callback is replayed', 200, M, 'POST', '/machines/A/events/channel-repaired', repairBody);
  await call('old goods receipt replay after restart', 200, M, 'POST', '/machines/A/events/goods-delivery', {
    requestId: 'A1-GOODS', eventId: 'RCPT-A1-GOODS', orderId: 'A-1001',
  });
  await call('a different stale goods receipt is rejected', 409, M, 'POST', '/machines/A/events/goods-delivery', {
    requestId: 'A1-GOODS-NEW-STALE', eventId: 'RCPT-DIFFERENT', orderId: 'A-1001',
  });
  await call('maintenance recovers physical jammed coin to tube', 200, M, 'POST', '/machines/A/coins/A-T1-1/recover', {
    requestId: 'A1-RECOVER', toLocation: 'TUBE',
  });
  await call('customer presents A order to claim 1-yuan difference', 202, A, 'POST', '/orders/A-1001/claims/CLM-A-1001-CHANGE/claim', {
    requestId: 'A1-CLAIM-OK',
  });
  const claimDsp = await call('claim routes one 1-yuan coin', 202, A, 'POST', '/orders/A-1001/claims/CLM-A-1001-CHANGE/dispense-next', {
    requestId: 'A1-CLAIM-DSP',
  });
  d = claimDsp.body.instruction.id;
  await call('claim coin sensor', 200, M, 'POST', '/machines/A/events/channel-sensor', {
    requestId: 'A1-CLAIM-SENSOR', dispenseId: d,
  });
  await call('claim coin arrives', 200, M, 'POST', '/machines/A/events/coin-arrived', {
    requestId: 'A1-CLAIM-ARRIVAL', dispenseId: d,
  });
  order = await getOrder(A, 'A-1001');
  const a1TakeBody = { requestId: 'A1-TAKE', takenCoinIds: trayIds(order) };
  check('A-1001 now has all 3 yuan at pickup, none collected yet', order.handedOutAmount === 3 && order.totalClaimRemaining === 0 && order.collectedAmount === 0);
  await call('maintenance confirms exact coins taken after door open', 200, M, 'POST', '/machines/A/pickup/confirm', a1TakeBody);
  order = await getOrder(A, 'A-1001');
  check('customer obtained all 3 yuan for A-1001', order.collectedAmount === 3 && trayIds(order).length === 0);

  console.log('\n9. Following 1-yuan order uses a different coin and claim');
  await call('create A-1002', 201, A, 'POST', '/orders', {
    requestId: 'A2-CREATE', orderId: 'A-1002', totalAmount: 4,
  });
  await call('A-1002 pays 5', 201, A, 'POST', '/orders/A-1002/coins', {
    requestId: 'A2-PAY', coinId: 'A2-PAY-5', denomination: 5,
  });
  await call('A-1002 vend', 202, A, 'POST', '/orders/A-1002/vend', { requestId: 'A2-VEND' });
  await deliverNext(A, 'A-1002', 'A2-CHG');
  let order2 = await getOrder(A, 'A-1002');
  check('A-1002 change is one yuan', order2.handedOutAmount === 1);
  await call('take A-1002 change', 200, M, 'POST', '/machines/A/pickup/confirm', {
    requestId: 'A2-TAKE', takenCoinIds: trayIds(order2),
  });
  order = await getOrder(A, 'A-1001');
  order2 = await getOrder(A, 'A-1002');
  check('two orders have different claim ids', order.claims[0].id !== order2.claims[0].id);
  check('two orders did not take same coin', order.customerCoins.every((c1) => !order2.customerCoins.some((c2) => c1.id === c2.id)));

  console.log('\n10. Two routes together can deliver only 2; 3 must not be shown as received');
  await call('restock one more 2-yuan coin', 201, M, 'POST', '/machines/A/restock', {
    requestId: 'RESTOCK-A2', coins: [{ id: 'A-T2-2', denomination: 2 }],
  });
  await call('register route-1 actual fault, leaving 2 deliverable', 200, M, 'POST', '/machines/A/channels/route-1/fault', {
    requestId: 'A2001-FAULT', code: 'ONE_YUAN_ROUTE_DOWN',
  });
  rec = (await req(M, 'GET', '/machines/A/reconciliation')).body.reconciliation;
  check('deliverable value across route-2 and route-1 is exactly 2', rec.deliverableValue === 2);
  await call('create A-2001', 201, A, 'POST', '/orders', {
    requestId: 'A2001-CREATE', orderId: 'A-2001', totalAmount: 7,
  });
  await call('A-2001 restored 10 payment', 201, A, 'POST', '/orders/A-2001/coins', {
    requestId: 'A2001-PAY', coinId: 'A2001-PAY-10', denomination: 10,
  });
  await call('A-2001 vend', 202, A, 'POST', '/orders/A-2001/vend', { requestId: 'A2001-VEND' });
  const partial = await call('machine pays the available 2', 202, A, 'POST', '/orders/A-2001/dispense-next', {
    requestId: 'A2001-DSP2',
  });
  d = partial.body.instruction.id;
  await call('A-2001 sensor', 200, M, 'POST', '/machines/A/events/channel-sensor', { requestId: 'A2001-S2', dispenseId: d });
  await call('A-2001 arrival', 200, M, 'POST', '/machines/A/events/coin-arrived', { requestId: 'A2001-A2', dispenseId: d });
  await call('next 1-yuan coin is unavailable', 409, A, 'POST', '/orders/A-2001/dispense-next', {
    requestId: 'A2001-DSP1-BLOCKED',
  });
  order = await getOrder(A, 'A-2001');
  check('customer is shown 2 received and 1 claimable, never 3 complete', order.handedOutAmount === 2 && order.totalClaimRemaining === 1);
  await call('repair route-1 for A-2001', 200, M, 'POST', '/machines/A/events/channel-repaired', {
    requestId: 'A2001-REPAIR', channelId: 'route-1',
  });
  await deliverNext(A, 'A-2001', 'A2001-LAST1');
  order = await getOrder(A, 'A-2001');
  await call('take A-2001 3 yuan', 200, M, 'POST', '/machines/A/pickup/confirm', {
    requestId: 'A2001-TAKE', takenCoinIds: trayIds(order),
  });

  console.log('\n11. Complete B net refund after repair/restock');
  await call('repair B route-1', 200, M, 'POST', '/machines/B/events/channel-repaired', {
    requestId: 'BR-REPAIR', channelId: 'route-1',
  });
  await call('recover B jammed 1 coin', 200, M, 'POST', '/machines/B/coins/B-T1-2/recover', {
    requestId: 'BR-RECOVER',
  });
  await call('restock B one 2 for net refund', 201, M, 'POST', '/machines/B/restock', {
    requestId: 'BR-RESTOCK2', coins: [{ id: 'B-T2-3', denomination: 2 }],
  });
  for (const prefix of ['BR-R1', 'BR-R2', 'BR-R3']) {
    await call(`route net refund coin ${prefix}`, 202, B, 'POST', '/orders/B-REFUND/claims/CLM-B-REFUND-REFUND-NET/dispense-next', {
      requestId: `${prefix}-DSP`,
    }).then(async (next) => {
      const id = next.body.instruction.id;
      await req(M, 'POST', '/machines/B/events/channel-sensor', { requestId: `${prefix}-S`, dispenseId: id });
      await req(M, 'POST', '/machines/B/events/coin-arrived', { requestId: `${prefix}-A`, dispenseId: id });
    });
  }
  bOrder = await getOrder(B, 'B-REFUND');
  check('B net refund claim delivered 8', bOrder.claims.find((c) => c.kind === 'REFUND_NET')?.amountDelivered === 8);
  await call('take all B refund tray coins', 200, M, 'POST', '/machines/B/pickup/confirm', {
    requestId: 'BR-TAKE', takenCoinIds: trayIds(bOrder),
  });
  bOrder = await getOrder(B, 'B-REFUND');
  check('B customer recovered 10 total: 2 issued earlier plus 8 net', bOrder.collectedAmount === 10);

  console.log('\n12. Sealed-cup mode, capacity/deadline, cancellation and unique cup');
  await call('restock cup coins', 201, M, 'POST', '/machines/A/restock', {
    requestId: 'CUP-RESTOCK',
    coins: [
      { id: 'A-CUP2', denomination: 2 },
      { id: 'A-CUP1', denomination: 1 },
      { id: 'A-CAP1-1', denomination: 1 },
      { id: 'A-CAP1-2', denomination: 1 },
      { id: 'A-CAP1-3', denomination: 1 },
    ],
  });
  await call('create cup order A-CUP-1', 201, A, 'POST', '/orders', {
    requestId: 'CUP1-CREATE', orderId: 'A-CUP-1', totalAmount: 7, mode: 'CUP',
  });
  await call('cup order pays 10', 201, A, 'POST', '/orders/A-CUP-1/coins', {
    requestId: 'CUP1-PAY', coinId: 'CUP1-PAY10', denomination: 10,
  });
  await call('cannot vend before sealed complete cup', 409, A, 'POST', '/orders/A-CUP-1/vend', { requestId: 'CUP1-VEND-EARLY' });
  await call('prepare 2+1 cup', 201, A, 'POST', '/orders/A-CUP-1/cup/prepare', {
    requestId: 'CUP1-PREPARE', cupId: 'CUP-A-1', capacityCoins: 2, ttlSeconds: 300,
  });
  await call('seal and confirm cup completeness', 200, M, 'POST', '/machines/A/cups/CUP-A-1/seal', {
    requestId: 'CUP1-SEAL',
  });
  await call('vend allowed only after seal', 202, A, 'POST', '/orders/A-CUP-1/vend', { requestId: 'CUP1-VEND' });
  await call('cup goods delivery receipt', 200, M, 'POST', '/machines/A/events/goods-delivery', {
    requestId: 'CUP1-GOODS', orderId: 'A-CUP-1',
  });
  await call('cup moves to pickup', 200, M, 'POST', '/machines/A/cups/CUP-A-1/send-to-pickup', {
    requestId: 'CUP1-PICKUP-MOVE',
  });
  await call('cup pickup door', 200, M, 'POST', '/machines/A/events/pickup-door', {
    requestId: 'CUP1-DOOR', orderId: 'A-CUP-1',
  });
  await call('customer takes the sealed cup', 200, M, 'POST', '/machines/A/pickup/confirm', {
    requestId: 'CUP1-TAKE', takenCupIds: ['CUP-A-1'],
  });
  let cupOrder = await getOrder(A, 'A-CUP-1');
  check('cup order collected 3 and has cup id', cupOrder.collectedAmount === 3 && cupOrder.pickupCup.status === 'TAKEN');

  await call('create capacity order', 201, A, 'POST', '/orders', {
    requestId: 'CAP-CREATE', orderId: 'A-CUP-CAP', totalAmount: 7, mode: 'CUP',
  });
  await call('capacity order pays 10', 201, A, 'POST', '/orders/A-CUP-CAP/coins', {
    requestId: 'CAP-PAY', coinId: 'CAP-PAY10', denomination: 10,
  });
  await call('three ones do not fit two-coin cup', 409, A, 'POST', '/orders/A-CUP-CAP/cup/prepare', {
    requestId: 'CAP-PREPARE', capacityCoins: 2,
  });
  await call('cancel unshipped capacity order', 200, A, 'POST', '/orders/A-CUP-CAP/cancel', {
    requestId: 'CAP-CANCEL', refundMode: 'ORIGINAL',
  });
  let capOrder = await getOrder(A, 'A-CUP-CAP');
  await call('take returned original 10 coin', 200, M, 'POST', '/machines/A/pickup/confirm', {
    requestId: 'CAP-TAKE', takenCoinIds: trayIds(capOrder),
  });
  capOrder = await getOrder(A, 'A-CUP-CAP');
  check('original inserted 10 coin returned and taken', capOrder.collectedAmount === 10 && capOrder.paidCoins[0].id === 'CAP-PAY10');

  await call('create expired-deadline cup order', 201, A, 'POST', '/orders', {
    requestId: 'EXP-CREATE', orderId: 'A-CUP-EXP', totalAmount: 7, mode: 'CUP',
  });
  await call('exp order pays 10', 201, A, 'POST', '/orders/A-CUP-EXP/coins', {
    requestId: 'EXP-PAY', coinId: 'EXP-PAY10', denomination: 10,
  });
  await call('prepare expired cup', 201, A, 'POST', '/orders/A-CUP-EXP/cup/prepare', {
    requestId: 'EXP-PREPARE', ttlSeconds: -1,
  });
  await call('seal expired cup', 200, M, 'POST', '/machines/A/cups/CUP-A-CUP-EXP/seal', { requestId: 'EXP-SEAL' });
  await call('expired turnover deadline blocks vend', 409, A, 'POST', '/orders/A-CUP-EXP/vend', { requestId: 'EXP-VEND' });
  await call('unshipped expired cup cancels and returns original coin', 200, A, 'POST', '/orders/A-CUP-EXP/cancel', {
    requestId: 'EXP-CANCEL', refundMode: 'ORIGINAL',
  });
  const expOrder = await getOrder(A, 'A-CUP-EXP');
  check('expiry cancel puts original coin in tray', expOrder.trayCoins.some((c) => c.id === 'EXP-PAY10'));
  await call('take expired-order original coin', 200, M, 'POST', '/machines/A/pickup/confirm', {
    requestId: 'EXP-TAKE', takenCoinIds: trayIds(expOrder),
  });

  await call('intruder order cannot prepare the already-used cup id', 201, A, 'POST', '/orders', {
    requestId: 'INTR-CREATE', orderId: 'A-CUP-INTRUDER', totalAmount: 7, mode: 'CUP',
  });
  await call('intruder pays', 201, A, 'POST', '/orders/A-CUP-INTRUDER/coins', {
    requestId: 'INTR-PAY', coinId: 'INTR-PAY10', denomination: 10,
  });
  await call('same cup id cannot serve second order', 409, A, 'POST', '/orders/A-CUP-INTRUDER/cup/prepare', {
    requestId: 'INTR-PREPARE', cupId: 'CUP-A-1',
  });

  console.log('\n13. Final power cycle, persistence and physical reconciliation');
  await stopServer(child);
  child = startServer();
  await waitHealth(A, 'sales-A');
  await waitHealth(B, 'sales-B');
  await waitHealth(M, 'maintenance');

  const stateA = (await req(M, 'GET', '/machines/A/state')).body.machine;
  const physicalCounts = {};
  for (const coin of Object.values(stateA.coins)) {
    physicalCounts[coin.location] ||= {};
    physicalCounts[coin.location][coin.denomination] = (physicalCounts[coin.location][coin.denomination] || 0) + 1;
  }
  const expectedCustomerA = (await req(M, 'GET', '/machines/A/reconciliation')).body.reconciliation.customerObtainedValue;
  const badAudit = await call('deliberately wrong physical count does not match', 200, M, 'POST', '/machines/A/audit', {
    requestId: 'A-AUDIT-BAD',
    physicalCounts: { ...physicalCounts, TRAY: { 999: 1 } },
    customerObtainedAmount: expectedCustomerA,
  });
  check('wrong count reports discrepancy', badAudit.body.reconciliation.physicalComparison.matches === false);
  const goodAudit = await call('correct coin实数 and customer amount reconcile', 200, M, 'POST', '/machines/A/audit', {
    requestId: 'A-AUDIT-GOOD',
    physicalCounts,
    customerObtainedAmount: expectedCustomerA,
  });
  check('A final ledger is balanced', goodAudit.body.reconciliation.balanced);
  const recB = (await req(M, 'GET', '/machines/B/reconciliation')).body.reconciliation;
  check('B final ledger is balanced', recB.balanced);
  check('no coin is shared by two claims/cups',
    goodAudit.body.reconciliation.errors.every((e) => e.code !== 'COIN_SHARED_BY_CLAIMS') &&
    recB.errors.every((e) => e.code !== 'COIN_SHARED_BY_CLAIMS'));

  const replayAfterRestart = await call('exact take replay after restart', 200, M, 'POST', '/machines/A/pickup/confirm', a1TakeBody);
  check('take replay does not move coin twice', replayAfterRestart.body.replayed === true);

  console.log(`\n${failures.length === 0 ? 'ALL CHECKS PASSED' : `${failures.length} CHECK(S) FAILED`}: ${passed} assertions passed.`);
  if (failures.length) {
    for (const f of failures) console.error(` - ${f}`);
    process.exitCode = 1;
  }
} finally {
  if (child) await stopServer(child).catch(() => {});
  fs.closeSync(logFd);
}
