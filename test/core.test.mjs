import test from 'node:test';
import assert from 'node:assert/strict';
import {
  HttpError,
  newMachine,
  restock,
  createOrder,
  insertCoin,
  vendOrder,
  dispenseNext,
  reportSensor,
  reportArrival,
  reportJam,
  repairChannel,
  recoverCoin,
  confirmPickup,
  reconcile,
  prepareCup,
  sealCup,
} from '../src/core.mjs';

function body(result) {
  return result.body;
}

function payAndVendStream(m, orderId = 'O1') {
  createOrder(m, { orderId, totalAmount: 7, mode: 'STREAM' });
  insertCoin(m, orderId, { coinId: `${orderId}-10`, denomination: 10 });
  vendOrder(m, orderId, {});
}

function deliverDispense(m, dispenseId) {
  reportSensor(m, { dispenseId });
  reportArrival(m, { dispenseId });
}

test('greedy feasible allocation uses 2+1 and preserves small coins for next 1-yuan order', () => {
  const m = newMachine('A');
  restock(m, {
    coins: [
      { id: '2', denomination: 2 },
      { id: '1a', denomination: 1 },
      { id: '1b', denomination: 1 },
      { id: '1c', denomination: 1 },
    ],
  });
  payAndVendStream(m);

  let next = body(dispenseNext(m, 'O1', { requestId: 'd1' }));
  assert.equal(next.instruction.denomination, 2);
  deliverDispense(m, next.instruction.id);

  next = body(dispenseNext(m, 'O1', { requestId: 'd2' }));
  assert.equal(next.instruction.denomination, 1);
  deliverDispense(m, next.instruction.id);

  createOrder(m, { orderId: 'O2', totalAmount: 4 });
  insertCoin(m, 'O2', { coinId: 'O2-5', denomination: 5 });
  vendOrder(m, 'O2', {});
  const next2 = body(dispenseNext(m, 'O2', {}));
  assert.equal(next2.instruction.denomination, 1);
  assert.equal(reconcile(m).balanced, true);
});

test('sensor passage plus jam does not count as customer money; claim remains open and is unique', () => {
  const m = newMachine('A');
  restock(m, { coins: [{ id: '2', denomination: 2 }, { id: '1', denomination: 1 }] });
  payAndVendStream(m, 'JAM');

  let d = body(dispenseNext(m, 'JAM', {})).instruction;
  assert.equal(d.denomination, 2);
  deliverDispense(m, d.id);

  d = body(dispenseNext(m, 'JAM', {})).instruction;
  assert.equal(d.denomination, 1);
  reportSensor(m, { dispenseId: d.id });
  reportJam(m, { dispenseId: d.id, location: 'LAST_SEGMENT' });

  const order = reconcile(m).orders.find((o) => o.id === 'JAM');
  assert.equal(order.handedOutAmount, 2);
  assert.equal(order.totalClaimRemaining, 1);
  assert.notEqual(order.status, 'CANCELLED');

  repairChannel(m, { channelId: 'route-1' });
  recoverCoin(m, '1', { toLocation: 'TUBE' });
  const claimId = order.claims[0].id;
  const retry = body(dispenseNext(m, 'JAM', { claimId }));
  deliverDispense(m, retry.instruction.id);

  const updated = reconcile(m).orders.find((o) => o.id === 'JAM');
  assert.equal(updated.handedOutAmount, 3);
  assert.equal(updated.totalClaimRemaining, 0);
  assert.equal(reconcile(m).balanced, true);
});

test('underpayment is rejected and does not create a vend instruction', () => {
  const m = newMachine('A');
  createOrder(m, { orderId: 'SHORT', totalAmount: 7 });
  insertCoin(m, 'SHORT', { coinId: '5', denomination: 5 });
  assert.throws(() => vendOrder(m, 'SHORT', {}), (error) => {
    assert.ok(error instanceof HttpError);
    assert.equal(error.status, 402);
    assert.equal(error.details.missingAmount, 2);
    return true;
  });
  assert.equal(m.orders.SHORT.fulfillmentStatus, 'NOT_REQUESTED');
});

test('sealed cup must be complete before vend and cannot be reused by another order', () => {
  const m = newMachine('A');
  restock(m, { coins: [{ id: '2', denomination: 2 }, { id: '1', denomination: 1 }] });
  createOrder(m, { orderId: 'C1', totalAmount: 7, mode: 'CUP' });
  insertCoin(m, 'C1', { coinId: '10', denomination: 10 });
  prepareCup(m, 'C1', { cupId: 'CUP-X' });
  sealCup(m, 'CUP-X', {});
  const allowed = vendOrder(m, 'C1', {});
  assert.equal(allowed.status, 202);

  createOrder(m, { orderId: 'C2', totalAmount: 7, mode: 'CUP' });
  insertCoin(m, 'C2', { coinId: '10-2', denomination: 10 });
  restock(m, { coins: [{ id: '2-2', denomination: 2 }, { id: '1-2', denomination: 1 }] });
  assert.throws(() => prepareCup(m, 'C2', { cupId: 'CUP-X' }), /already exists/);
  assert.equal(reconcile(m).balanced, true);
});

test('pickup confirmation moves exact coins to customer and keeps reconciliation balanced', () => {
  const m = newMachine('A');
  restock(m, { coins: [{ id: '2', denomination: 2 }] });
  createOrder(m, { orderId: 'P', totalAmount: 8 });
  insertCoin(m, 'P', { coinId: '10', denomination: 10 });
  vendOrder(m, 'P', {});
  const d = body(dispenseNext(m, 'P', {})).instruction;
  deliverDispense(m, d.id);
  confirmPickup(m, { takenCoinIds: [d.coinId] });
  const r = reconcile(m);
  assert.equal(r.customerObtainedValue, 2);
  assert.equal(r.balanced, true);
});
