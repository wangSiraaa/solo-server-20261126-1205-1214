import { randomUUID } from 'node:crypto';

export class HttpError extends Error {
  constructor(status, code, message, details = {}) {
    super(message);
    this.name = 'HttpError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export const LOCATIONS = [
  'TUBE',
  'ESCROW',
  'CHANNEL',
  'JAMMED',
  'TRAY',
  'CUP',
  'CASHBOX',
  'CUSTOMER',
  'MAINT',
];

export function nowIso() {
  return new Date().toISOString();
}

function nextId(m, key, prefix) {
  m.counters[key] = (m.counters[key] || 0) + 1;
  return `${prefix}-${m.counters[key]}`;
}

export function newMachine(id) {
  const at = nowIso();
  return {
    id,
    createdAt: at,
    coins: {},
    orders: {},
    claims: {},
    cups: {},
    dispenses: {},
    channels: {
      'route-1': { id: 'route-1', denomination: 1, enabled: true, fault: null, repairs: [] },
      'route-2': { id: 'route-2', denomination: 2, enabled: true, fault: null, repairs: [] },
      'route-5': { id: 'route-5', denomination: 5, enabled: true, fault: null, repairs: [] },
      'route-10': { id: 'route-10', denomination: 10, enabled: true, fault: null, repairs: [] },
    },
    doorEvents: [],
    events: [],
    audits: [],
    idempotency: {},
    counters: { order: 0, claim: 0, cup: 0, dispense: 0, event: 0 },
  };
}

export function createInitialState() {
  return {
    schemaVersion: 1,
    createdAt: nowIso(),
    machines: {
      A: newMachine('A'),
      B: newMachine('B'),
    },
  };
}

function badRequest(code, message, details) {
  throw new HttpError(400, code, message, details);
}
function notFound(code, message, details) {
  throw new HttpError(404, code, message, details);
}
function conflict(code, message, details) {
  throw new HttpError(409, code, message, details);
}

function addEvent(m, type, payload = {}) {
  const event = {
    seq: ++m.counters.event,
    type,
    at: nowIso(),
    ...payload,
  };
  m.events.push(event);
  return event;
}

function mustOrder(m, orderId) {
  if (!orderId) badRequest('ORDER_ID_REQUIRED', 'An order id is required in the path.');
  const order = m.orders[orderId];
  if (!order) notFound('ORDER_NOT_FOUND_ON_THIS_MACHINE', `Order ${orderId} is not registered on machine ${m.id}.`);
  return order;
}

function mustClaim(m, claimId, orderId = null) {
  if (!claimId) badRequest('CLAIM_ID_REQUIRED', 'A claim id is required.');
  const claim = m.claims[claimId];
  if (!claim) notFound('CLAIM_NOT_FOUND_ON_THIS_MACHINE', `Claim ${claimId} is not registered on machine ${m.id}.`);
  if (orderId && claim.orderId !== orderId) {
    notFound('CLAIM_NOT_FOUND_FOR_ORDER', `Claim ${claimId} does not belong to order ${orderId}.`);
  }
  return claim;
}

function moveCoin(coin, location, patch = {}) {
  const from = coin.location;
  coin.location = location;
  Object.assign(coin, patch);
  coin.history.push({ at: nowIso(), from, to: location, ...patch.history });
  delete patch.history;
  return coin;
}

function channelHealthy(m, channelId) {
  const channel = m.channels[channelId];
  return !channel || channel.enabled === true;
}

function availableTubeCoins(m, denomination = null) {
  return Object.values(m.coins)
    .filter((c) => c.location === 'TUBE')
    .filter((c) => denomination === null || c.denomination === denomination)
    .filter((c) => channelHealthy(m, c.channelId))
    .sort((a, b) => a.id.localeCompare(b.id));
}

/**
 * Find an exact coin combination. Denominations are tried high-to-low and
 * infeasible greedy choices are backtracked. Trying high denominations first
 * also tends to preserve small coins needed by the immediately following order.
 */
export function findCombination(m, amount, { requireHealthyChannel = true } = {}) {
  if (!Number.isInteger(amount) || amount < 0) return null;
  if (amount === 0) return [];
  let coins = Object.values(m.coins).filter((c) => c.location === 'TUBE');
  if (requireHealthyChannel) coins = coins.filter((c) => channelHealthy(m, c.channelId));
  const byDenom = new Map();
  for (const coin of coins) {
    if (coin.denomination > amount) continue;
    if (!byDenom.has(coin.denomination)) byDenom.set(coin.denomination, []);
    byDenom.get(coin.denomination).push(coin);
  }
  for (const list of byDenom.values()) list.sort((a, b) => a.id.localeCompare(b.id));
  const denoms = [...byDenom.keys()].sort((a, b) => b - a);

  function dfs(index, remaining, chosen) {
    if (remaining === 0) return chosen;
    if (index >= denoms.length) return null;
    const denom = denoms[index];
    const list = byDenom.get(denom);
    const maxTake = Math.min(list.length, Math.floor(remaining / denom));
    for (let take = maxTake; take >= 0; take -= 1) {
      const next = dfs(index + 1, remaining - take * denom, chosen.concat(list.slice(0, take)));
      if (next) return next;
    }
    return null;
  }
  return dfs(0, amount, []);
}

export function restock(m, input = {}) {
  const specs = input.coins;
  if (!Array.isArray(specs) || specs.length === 0) {
    badRequest('RESTOCK_COINS_REQUIRED', 'Provide a non-empty coins array.');
  }
  const created = [];
  for (const spec of specs) {
    const denomination = Number(spec.denomination ?? spec.value);
    if (!Number.isInteger(denomination) || denomination <= 0) {
      badRequest('INVALID_DENOMINATION', 'Each coin needs a positive integer denomination.', spec);
    }
    const id = spec.id || nextId(m, 'coin', `${m.id}-COIN`);
    if (m.coins[id]) conflict('COIN_ID_EXISTS', `Coin ${id} is already registered.`);
    const channelId = spec.channelId || `route-${denomination}`;
    if (!m.channels[channelId]) {
      m.channels[channelId] = { id: channelId, denomination, enabled: true, fault: null, repairs: [] };
    }
    const coin = {
      id,
      machineId: m.id,
      denomination,
      location: 'TUBE',
      tubeId: spec.tubeId || channelId,
      channelId,
      orderId: null,
      claimId: null,
      cupId: null,
      dispenseId: null,
      serial: spec.serial || id,
      history: [{ at: nowIso(), from: null, to: 'TUBE', reason: 'RESTOCK' }],
    };
    m.coins[id] = coin;
    created.push(coinView(coin));
  }
  addEvent(m, 'maintenance.restocked', { coinIds: created.map((c) => c.id) });
  return { status: 201, body: { machineId: m.id, restocked: created, reconciliation: reconcile(m) } };
}

export function createOrder(m, input = {}) {
  let totalAmount = Number(input.totalAmount);
  if (!Number.isFinite(totalAmount)) {
    if (Array.isArray(input.items) && input.items.length) {
      totalAmount = input.items.reduce((sum, item) => sum + Number(item.price) * Number(item.quantity ?? item.qty ?? 1), 0);
    } else {
      totalAmount = Number(input.price ?? 7) * Number(input.quantity ?? input.qty ?? 1);
    }
  }
  if (!Number.isInteger(totalAmount) || totalAmount <= 0) {
    badRequest('INVALID_TOTAL_AMOUNT', 'totalAmount must be a positive integer.');
  }
  const id = input.orderId || input.id || nextId(m, 'order', `${m.id}-O`);
  if (m.orders[id]) conflict('ORDER_ID_EXISTS', `Order ${id} already exists on machine ${m.id}.`);
  const mode = String(input.mode || 'STREAM').toUpperCase();
  if (!['STREAM', 'CUP'].includes(mode)) badRequest('INVALID_MODE', 'mode must be STREAM or CUP.');

  const order = {
    id,
    machineId: m.id,
    mode,
    sku: input.sku || 'SODA',
    totalAmount,
    paidAmount: 0,
    changeDue: 0,
    handedOutAmount: 0,
    collectedAmount: 0,
    status: 'AWAIT_PAYMENT',
    paymentStatus: 'AWAIT_PAYMENT',
    fulfillmentStatus: 'NOT_REQUESTED',
    goods: { status: 'NOT_REPORTED' },
    coinIds: [],
    claimIds: [],
    activeClaimId: null,
    cupId: null,
    doorOpenCount: 0,
    createdAt: nowIso(),
    updatedAt: nowIso(),
    metadata: input.metadata || {},
  };
  m.orders[id] = order;
  addEvent(m, 'order.created', { orderId: id, mode, totalAmount });
  return { status: 201, body: orderView(m, id) };
}

export function insertCoin(m, orderId, input = {}) {
  const order = mustOrder(m, orderId);
  if (order.status === 'CANCELLED') conflict('ORDER_CANCELLED', 'Cannot insert a coin into a cancelled order.');
  if (order.fulfillmentStatus === 'DELIVERED' && order.status === 'COMPLETED') {
    conflict('ORDER_COMPLETED', 'Order is completed; register an adjustment instead.');
  }
  const denomination = Number(input.denomination ?? input.value);
  if (!Number.isInteger(denomination) || denomination <= 0) {
    badRequest('INVALID_DENOMINATION', 'Inserted coin needs a positive integer denomination.');
  }
  const id = input.coinId || input.id || randomUUID();
  if (m.coins[id]) conflict('COIN_ALREADY_REGISTERED', `Coin ${id} was already registered.`);
  const coin = {
    id,
    machineId: m.id,
    denomination,
    location: 'ESCROW',
    tubeId: null,
    channelId: null,
    orderId: order.id,
    claimId: null,
    cupId: null,
    dispenseId: null,
    serial: input.serial || id,
    insertedAt: nowIso(),
    history: [{ at: nowIso(), from: 'CUSTOMER', to: 'ESCROW', reason: 'INSERTED' }],
  };
  m.coins[id] = coin;
  order.coinIds.push(id);
  order.paidAmount += denomination;
  order.paymentStatus = order.paidAmount >= order.totalAmount ? 'PAID_ENOUGH' : 'PARTIAL';
  order.updatedAt = nowIso();
  addEvent(m, 'payment.coin_inserted', { orderId: order.id, coinId: id, denomination });
  return { status: 201, body: { ok: true, order: orderView(m, order.id) } };
}

function createChangeClaim(m, order, kind, amount, extra = {}) {
  const claimId = extra.claimId || nextId(m, 'claim', `CLM-${order.id}`);
  if (m.claims[claimId]) conflict('CLAIM_ID_EXISTS', `Claim ${claimId} already exists.`);
  const claim = {
    id: claimId,
    machineId: m.id,
    orderId: order.id,
    kind,
    status: extra.status || 'OPEN',
    amountExpected: amount,
    amountDelivered: 0,
    amountCollected: 0,
    deliveredCoinIds: [],
    dispenseIds: [],
    activeDispenseId: null,
    cupId: extra.cupId || null,
    parentClaimId: extra.parentClaimId || null,
    note: extra.note || '',
    createdAt: nowIso(),
    updatedAt: nowIso(),
  };
  m.claims[claimId] = claim;
  order.claimIds.push(claimId);
  if (!order.activeClaimId) order.activeClaimId = claimId;
  return claim;
}

export function vendOrder(m, orderId, input = {}) {
  const order = mustOrder(m, orderId);
  if (order.status === 'CANCELLED') conflict('ORDER_CANCELLED', 'Cancelled order cannot be vended.');
  if (order.fulfillmentStatus !== 'NOT_REQUESTED') {
    conflict('VEND_ALREADY_REQUESTED', 'A vend instruction already exists for this order.', orderView(m, order.id));
  }
  if (order.paidAmount < order.totalAmount) {
    throw new HttpError(402, 'PAYMENT_INSUFFICIENT', 'Registered inserted coins do not cover the price.', {
      orderId: order.id,
      price: order.totalAmount,
      paidAmount: order.paidAmount,
      missingAmount: order.totalAmount - order.paidAmount,
    });
  }

  order.changeDue = order.paidAmount - order.totalAmount;
  if (order.changeDue > 0 && order.mode === 'STREAM' && !order.activeClaimId) {
    createChangeClaim(m, order, 'CHANGE', order.changeDue, { claimId: `CLM-${order.id}-CHANGE` });
  }
  if (order.mode === 'CUP') {
    const cup = order.cupId ? m.cups[order.cupId] : null;
    if (!cup || cup.status !== 'SEALED_READY') {
      conflict('CUP_NOT_SEALED_COMPLETE', 'A complete, sealed change cup is required before vending.', {
        cupStatus: cup?.status || null,
      });
    }
    if (cup.expiresAt && Date.now() > Date.parse(cup.expiresAt)) {
      conflict('CUP_DEADLINE_EXCEEDED', 'The change cup turnover deadline has passed; cancel and remake/return funds.');
    }
  }

  order.fulfillmentStatus = 'VEND_INSTRUCTED';
  order.status = 'VENDING';
  order.vendRequestedAt = nowIso();
  order.updatedAt = nowIso();
  addEvent(m, 'goods.vend_instructed', { orderId: order.id, mode: order.mode });
  return {
    status: 202,
    body: {
      allowed: true,
      order: orderView(m, order.id),
      nextStep: order.mode === 'STREAM' && order.changeDue > 0 ? 'CALL /orders/{id}/dispense-next' : 'WAIT_FOR_GOODS_DELIVERY_REPORT',
    },
  };
}

export function reportGoodsDelivery(m, input = {}) {
  const order = mustOrder(m, input.orderId);
  if (order.goods.status === 'DELIVERED') {
    conflict('GOODS_ALREADY_REPORTED', 'This order already has a goods-out report; old receipts cannot change it.', {
      deliveredAt: order.goods.deliveredAt,
      deliveryEventId: order.goods.eventId,
    });
  }
  order.goods = {
    status: 'DELIVERED',
    eventId: input.eventId || input.requestId || null,
    reportedAt: nowIso(),
    sku: input.sku || order.sku,
  };
  order.fulfillmentStatus = 'DELIVERED';
  order.updatedAt = nowIso();
  maybeSettleEscrow(m, order.id);
  addEvent(m, 'goods.outlet_reported', { orderId: order.id, eventId: order.goods.eventId });
  return { status: 200, body: orderView(m, order.id) };
}

export function dispenseNext(m, orderId, input = {}) {
  const order = mustOrder(m, orderId);
  const claim = input.claimId
    ? mustClaim(m, input.claimId, order.id)
    : order.activeClaimId
      ? mustClaim(m, order.activeClaimId, order.id)
      : null;

  if (!claim) conflict('NO_CHANGE_CLAIM', 'Order has no active money claim.');
  if (['COLLECTED', 'CANCELLED'].includes(claim.status)) {
    conflict('CLAIM_CLOSED', `Claim ${claim.id} is ${claim.status}.`);
  }
  if (claim.activeDispenseId) {
    const existing = m.dispenses[claim.activeDispenseId];
    if (existing && ['IN_CHANNEL', 'SENSOR_PASSED'].includes(existing.status)) {
      conflict('DISPENSE_IN_FLIGHT', 'Finish or report the existing coin before choosing another.', {
        dispense: dispenseView(existing),
      });
    }
    claim.activeDispenseId = null;
  }

  const remaining = claim.amountExpected - claim.amountDelivered;
  if (remaining <= 0) {
    claim.status = 'READY_FOR_PICKUP';
    claim.updatedAt = nowIso();
    return { status: 200, body: { nothingRemaining: true, claim: claimView(m, claim.id), order: orderView(m, order.id) } };
  }

  // Prefer a coin from a combination that can exactly finish the claim.
  // If no full combination exists, still release a safe partial coin so a
  // physical channel failure can create a documented difference claim.
  const combo = findCombination(m, remaining, { requireHealthyChannel: true });
  let coin = combo?.[0] || null;
  let exactCompletionPlanned = Boolean(combo);
  if (!coin) {
    coin = availableTubeCoins(m)
      .filter((c) => c.denomination <= remaining)
      .sort((a, b) => b.denomination - a.denomination || a.id.localeCompare(b.id))[0] || null;
  }
  if (!coin) {
    claim.status = 'OPEN';
    claim.lastBlockedAt = nowIso();
    conflict('NO_DELIVERABLE_COIN', 'No healthy coin route can pay any part of the remaining claim amount.', {
      claimId: claim.id,
      orderId: order.id,
      remainingAmount: remaining,
      deliverableValue: deliverableValue(m),
      tubeValue: tubeValue(m),
    });
  }
  const dispenseId = input.dispenseId || nextId(m, 'dispense', 'DSP');
  if (m.dispenses[dispenseId]) conflict('DISPENSE_ID_EXISTS', `Dispense ${dispenseId} already exists.`);
  const dispense = {
    id: dispenseId,
    machineId: m.id,
    orderId: order.id,
    claimId: claim.id,
    coinId: coin.id,
    denomination: coin.denomination,
    channelId: coin.channelId,
    status: 'IN_CHANNEL',
    sensorPassed: false,
    createdAt: nowIso(),
  };
  m.dispenses[dispenseId] = dispense;
  moveCoin(coin, 'CHANNEL', {
    orderId: order.id,
    claimId: claim.id,
    dispenseId,
  });
  claim.dispenseIds.push(dispenseId);
  claim.activeDispenseId = dispenseId;
  claim.status = 'PAYING';
  claim.updatedAt = nowIso();
  order.updatedAt = nowIso();
  addEvent(m, 'dispense.instructed', { dispenseId, orderId: order.id, claimId: claim.id, coinId: coin.id });
  return {
    status: 202,
    body: {
      instruction: { ...dispenseView(dispense), exactCompletionPlanned },
      nextFacts: ['POST /maint/machines/{id}/events/channel-sensor', 'POST /maint/machines/{id}/events/coin-arrived'],
      order: orderView(m, order.id),
    },
  };
}

function mustDispense(m, dispenseId) {
  if (!dispenseId) badRequest('DISPENSE_ID_REQUIRED', 'dispenseId is required.');
  const d = m.dispenses[dispenseId];
  if (!d) notFound('DISPENSE_NOT_FOUND', `Dispense ${dispenseId} was not found on machine ${m.id}.`);
  return d;
}

export function reportSensor(m, input = {}) {
  const d = mustDispense(m, input.dispenseId);
  if (d.status === 'DELIVERED') conflict('DISPENSE_ALREADY_DELIVERED', 'A delivered coin cannot pass the sensor again.', { dispenseId: d.id });
  if (d.status === 'JAMMED') conflict('DISPENSE_JAMMED', 'This dispense is jammed; repair and recover it before retrying.', { dispenseId: d.id });
  if (d.status === 'RECOVERED' || d.status === 'CANCELLED') conflict('DISPENSE_CLOSED', `Dispense is ${d.status}.`);
  d.status = 'SENSOR_PASSED';
  d.sensorPassed = true;
  d.sensorEventId = input.eventId || input.requestId || null;
  d.sensorPassedAt = nowIso();
  const coin = m.coins[d.coinId];
  if (coin) coin.lastSensorPassedAt = d.sensorPassedAt;
  addEvent(m, 'channel.sensor_passed', { dispenseId: d.id, coinId: d.coinId, channelId: d.channelId });
  return { status: 200, body: dispenseView(d) };
}

export function reportArrival(m, input = {}) {
  const d = mustDispense(m, input.dispenseId);
  if (d.status === 'DELIVERED') conflict('DISPENSE_ALREADY_DELIVERED', 'Arrival was already recorded for this dispense.', { dispenseId: d.id });
  if (d.status === 'JAMMED') conflict('DISPENSE_JAMMED', 'A jammed coin cannot arrive until physically recovered/redispatched.', { dispenseId: d.id });
  if (d.status === 'RECOVERED' || d.status === 'CANCELLED') conflict('DISPENSE_CLOSED', `Dispense is ${d.status}.`);
  if (!d.sensorPassed && input.allowWithoutSensor !== true) {
    conflict('SENSOR_FACT_REQUIRED', 'Record the channel sensor passage before final arrival.', { dispenseId: d.id });
  }
  const coin = m.coins[d.coinId];
  if (!coin) throw new HttpError(500, 'COIN_RECORD_MISSING', 'Dispense coin record is missing.');
  moveCoin(coin, 'TRAY');
  d.status = 'DELIVERED';
  d.deliveredAt = nowIso();
  d.arrivalEventId = input.eventId || input.requestId || null;

  const claim = mustClaim(m, d.claimId, d.orderId);
  if (!claim.deliveredCoinIds.includes(coin.id)) claim.deliveredCoinIds.push(coin.id);
  claim.amountDelivered += coin.denomination;
  claim.activeDispenseId = null;
  claim.updatedAt = nowIso();
  if (claim.amountDelivered === claim.amountExpected) {
    claim.status = 'READY_FOR_PICKUP';
  } else if (claim.amountDelivered < claim.amountExpected) {
    claim.status = 'OPEN';
  } else {
    throw new HttpError(500, 'CLAIM_OVER_DELIVERED', 'Claim delivered amount exceeds expected amount.');
  }

  const order = mustOrder(m, d.orderId);
  order.handedOutAmount += coin.denomination;
  order.updatedAt = nowIso();
  addEvent(m, 'coin.arrived_pickup', { dispenseId: d.id, coinId: coin.id, orderId: order.id, claimId: claim.id });
  return { status: 200, body: { dispense: dispenseView(d), claim: claimView(m, claim.id), order: orderView(m, order.id) } };
}

export function reportJam(m, input = {}) {
  const d = mustDispense(m, input.dispenseId);
  if (d.status === 'DELIVERED') conflict('DISPENSE_ALREADY_DELIVERED', 'A delivered coin cannot later be reported jammed.', { dispenseId: d.id });
  if (d.status === 'JAMMED') return { status: 200, body: dispenseView(d) };
  const coin = m.coins[d.coinId];
  d.status = 'JAMMED';
  d.jamLocation = input.location || 'LAST_SEGMENT';
  d.jamReason = input.reason || input.code || 'COIN_JAM';
  d.jammedAt = nowIso();
  if (coin) moveCoin(coin, 'JAMMED');
  const channel = m.channels[d.channelId];
  if (channel) {
    channel.enabled = false;
    channel.fault = { code: d.jamReason, dispenseId: d.id, at: d.jammedAt, location: d.jamLocation };
  }
  const claim = m.claims[d.claimId];
  if (claim) {
    claim.activeDispenseId = null;
    claim.status = 'OPEN';
    claim.lastJamAt = d.jammedAt;
    claim.updatedAt = d.jammedAt;
  }
  addEvent(m, 'channel.jammed', { dispenseId: d.id, location: d.jamLocation, coinId: d.coinId });
  return {
    status: 200,
    body: { dispense: dispenseView(d), claim: claim ? claimView(m, claim.id) : null, order: orderView(m, d.orderId) },
  };
}

export function repairChannel(m, input = {}) {
  const channelId = input.channelId;
  if (!channelId) badRequest('CHANNEL_ID_REQUIRED', 'channelId is required.');
  const channel = m.channels[channelId];
  if (!channel) notFound('CHANNEL_NOT_FOUND', `Channel ${channelId} does not exist.`);
  const wasFaulted = channel.enabled === false;
  channel.enabled = true;
  channel.fault = null;
  channel.lastRepairedAt = nowIso();
  channel.repairs.push({ at: channel.lastRepairedAt, eventId: input.eventId || input.requestId || null });
  addEvent(m, 'channel.repaired', { channelId, wasFaulted });
  return {
    status: 200,
    body: { channel, warning: 'Repair clears the route fault but does not move a physically jammed coin.', reconciliation: reconcile(m) },
  };
}

export function setChannelFault(m, input = {}) {
  const channelId = input.channelId;
  if (!channelId) badRequest('CHANNEL_ID_REQUIRED', 'channelId is required.');
  const channel = m.channels[channelId];
  if (!channel) notFound('CHANNEL_NOT_FOUND', `Channel ${channelId} does not exist.`);
  channel.enabled = false;
  channel.fault = {
    code: input.code || input.reason || 'CHANNEL_UNAVAILABLE',
    at: nowIso(),
    location: input.location || null,
    estimatedDeliverableValue: input.estimatedDeliverableValue,
  };
  addEvent(m, 'channel.fault_registered', { channelId, code: channel.fault.code });
  return { status: 200, body: { channel, deliverableValue: deliverableValue(m) } };
}

export function recoverCoin(m, coinId, input = {}) {
  const coin = m.coins[coinId];
  if (!coin) notFound('COIN_NOT_FOUND', `Coin ${coinId} not found.`);
  if (coin.location !== 'JAMMED' && input.force !== true) {
    conflict('COIN_NOT_JAMMED', 'Only a JAMMED coin can be recovered by this endpoint.', { location: coin.location });
  }
  const toLocation = input.toLocation || 'TUBE';
  const oldDispense = coin.dispenseId ? m.dispenses[coin.dispenseId] : null;
  const previousOrderId = coin.orderId;
  const previousClaimId = coin.claimId;
  moveCoin(coin, toLocation, {
    tubeId: input.tubeId || coin.tubeId || coin.channelId,
    orderId: null,
    claimId: null,
    dispenseId: null,
    recoveredAt: nowIso(),
  });
  if (oldDispense) {
    oldDispense.status = 'RECOVERED';
    oldDispense.recoveredAt = nowIso();
  }
  addEvent(m, 'maintenance.coin_recovered', {
    coinId,
    toLocation,
    previousOrderId,
    previousClaimId,
    dispenseId: oldDispense?.id || null,
  });
  return { status: 200, body: { coin: coinView(coin), reconciliation: reconcile(m) } };
}

export function recordPickupDoor(m, input = {}) {
  const order = input.orderId ? mustOrder(m, input.orderId) : null;
  const trayCoinIds = Object.values(m.coins).filter((c) => c.location === 'TRAY').map((c) => c.id);
  const pickupCupIds = Object.values(m.cups).filter((c) => c.location === 'PICKUP' && c.status === 'AT_PICKUP').map((c) => c.id);
  const event = addEvent(m, 'pickup_door.opened', {
    orderId: order?.id || null,
    doorEventId: input.eventId || input.requestId || `DOOR-${m.events.length + 1}`,
  });
  m.doorEvents.push(event);
  if (order) order.doorOpenCount += 1;
  return {
    status: 200,
    body: {
      opened: true,
      at: event.at,
      trayCoinIds,
      pickupCupIds,
      warning: 'A door-open fact is not proof that every coin or cup was taken.',
    },
  };
}

function markCoinTaken(m, coinId, takenAt, source) {
  const coin = m.coins[coinId];
  if (!coin) notFound('COIN_NOT_FOUND', `Coin ${coinId} not found.`);
  if (coin.location !== 'TRAY') {
    conflict('COIN_NOT_IN_PICKUP_TRAY', `Coin ${coinId} is ${coin.location}, not TRAY.`, { location: coin.location });
  }
  moveCoin(coin, 'CUSTOMER', { takenAt, takeSource: source });
  if (coin.orderId) {
    const order = m.orders[coin.orderId];
    if (order) order.collectedAmount += coin.denomination;
  }
  if (coin.claimId) {
    const claim = m.claims[coin.claimId];
    if (claim) {
      claim.amountCollected += coin.denomination;
      if (claim.amountCollected >= claim.amountExpected && claim.amountDelivered >= claim.amountExpected) {
        claim.status = 'COLLECTED';
        claim.collectedAt = takenAt;
      } else if (claim.status === 'READY_FOR_PICKUP') {
        claim.status = 'PARTIALLY_COLLECTED';
      }
      claim.updatedAt = takenAt;
    }
  }
  return coin;
}

function markCupTaken(m, cupId, takenAt) {
  const cup = m.cups[cupId];
  if (!cup) notFound('CUP_NOT_FOUND', `Cup ${cupId} not found.`);
  if (cup.status !== 'AT_PICKUP') conflict('CUP_NOT_AT_PICKUP', `Cup ${cupId} is ${cup.status}.`);
  cup.status = 'TAKEN';
  cup.takenAt = takenAt;
  for (const coinId of cup.coinIds) {
    const coin = m.coins[coinId];
    if (!coin || coin.location !== 'CUP') conflict('CUP_CONTENT_INVALID', `Cup coin ${coinId} is not in CUP location.`);
    moveCoin(coin, 'CUSTOMER', { takenAt, takeSource: 'SEALED_CUP', cupId: cup.id });
    if (coin.orderId) {
      const order = m.orders[coin.orderId];
      if (order) order.collectedAmount += coin.denomination;
    }
    if (coin.claimId) {
      const claim = m.claims[coin.claimId];
      if (claim) claim.amountCollected += coin.denomination;
    }
  }
  const claim = m.claims[cup.claimId];
  if (claim) {
    claim.status = 'COLLECTED';
    claim.collectedAt = takenAt;
    claim.amountCollected = claim.amountExpected;
  }
  return cup;
}

function maybeSettleEscrow(m, orderId) {
  const order = m.orders[orderId];
  if (!order) return;
  const activeStatuses = ['OPEN', 'PAYING', 'PACKING', 'SEALED', 'READY_FOR_PICKUP', 'PARTIALLY_COLLECTED'];
  const hasActiveClaim = order.claimIds.some((id) => activeStatuses.includes(m.claims[id]?.status));
  if (hasActiveClaim) return;
  if (order.fulfillmentStatus !== 'DELIVERED' && order.status !== 'CANCELLED') return;
  let moved = false;
  for (const coinId of order.coinIds) {
    const coin = m.coins[coinId];
    if (coin?.location === 'ESCROW') {
      moveCoin(coin, 'CASHBOX', { settledAt: nowIso() });
      moved = true;
    }
  }
  if (moved) addEvent(m, 'payment.escrow_settled_to_cashbox', { orderId: order.id });
}

export function confirmPickup(m, input = {}) {
  const takenAt = nowIso();
  let coinIds = Array.isArray(input.takenCoinIds) ? [...input.takenCoinIds] : [];
  const cupIds = Array.isArray(input.takenCupIds) ? input.takenCupIds : [];

  if (Array.isArray(input.remainingCoinIds)) {
    if (coinIds.length) badRequest('PICKUP_INPUT_CONFLICT', 'Use either takenCoinIds or remainingCoinIds, not both.');
    const remaining = new Set(input.remainingCoinIds);
    for (const id of remaining) {
      const coin = m.coins[id];
      if (!coin) notFound('UNKNOWN_COUNTED_COIN', `Counted remaining coin ${id} is not registered.`);
      if (coin.location !== 'TRAY') conflict('COUNTED_COIN_NOT_IN_TRAY', `Coin ${id} is ${coin.location}, not TRAY.`);
    }
    coinIds = Object.values(m.coins)
      .filter((c) => c.location === 'TRAY' && !remaining.has(c.id))
      .map((c) => c.id);
  }

  if (!coinIds.length && !cupIds.length) {
    badRequest('NOTHING_TAKEN_SPECIFIED', 'Provide takenCoinIds, takenCupIds, or remainingCoinIds.');
  }
  const takenCoins = coinIds.map((id) => coinView(markCoinTaken(m, id, takenAt, input.source || 'PICKUP_COUNT')));
  const takenCups = cupIds.map((id) => cupView(markCupTaken(m, id, takenAt)));
  const affectedOrderIds = new Set([
    ...takenCoins.map((c) => c.orderId).filter(Boolean),
    ...takenCups.map((c) => c.orderId).filter(Boolean),
  ]);
  for (const orderId of affectedOrderIds) maybeSettleEscrow(m, orderId);
  addEvent(m, 'pickup.collected_confirmed', { coinIds, cupIds });
  return {
    status: 200,
    body: { takenCoins, takenCups, customerObtainedValue: customerObtainedValue(m), reconciliation: reconcile(m) },
  };
}

export function prepareCup(m, orderId, input = {}) {
  const order = mustOrder(m, orderId);
  if (order.mode !== 'CUP') badRequest('ORDER_NOT_CUP_MODE', 'This order was not created in CUP mode.');
  if (order.paidAmount < order.totalAmount) throw new HttpError(402, 'PAYMENT_INSUFFICIENT', 'Not enough registered payment.');
  if (order.cupId) conflict('CUP_ALREADY_EXISTS', 'Order already has a change cup.', { cupId: order.cupId });
  order.changeDue = order.paidAmount - order.totalAmount;
  if (order.changeDue <= 0) badRequest('NO_CHANGE_DUE', 'This order has no change due.');

  const combo = findCombination(m, order.changeDue, { requireHealthyChannel: false });
  if (!combo) {
    conflict('FULL_CHANGE_NOT_AVAILABLE', 'Cannot assemble the complete exact change; no coins were reserved.', {
      changeDue: order.changeDue,
      tubeValue: tubeValue(m),
    });
  }
  const capacity = input.capacityCoins ?? order.cupCapacityCoins ?? 10;
  if (combo.length > capacity) {
    conflict('CUP_CAPACITY_EXCEEDED', `Cup holds at most ${capacity} coins.`, { coinCount: combo.length, capacity });
  }
  const cupId = input.cupId || `CUP-${order.id}`;
  if (m.cups[cupId]) conflict('CUP_ID_EXISTS', `Cup ${cupId} already exists.`);
  const ttlSeconds = Number(input.ttlSeconds ?? order.cupTtlSeconds ?? 300);
  const claim = createChangeClaim(m, order, 'CUP', order.changeDue, {
    claimId: `CLM-${order.id}-CUP`,
    status: 'PACKING',
    cupId,
  });
  const cup = {
    id: cupId,
    machineId: m.id,
    orderId: order.id,
    claimId: claim.id,
    status: 'PACKING',
    location: 'INTERNAL',
    capacityCoins: capacity,
    expectedAmount: order.changeDue,
    coinIds: [],
    expiresAt: new Date(Date.now() + ttlSeconds * 1000).toISOString(),
    ttlSeconds,
    createdAt: nowIso(),
  };
  m.cups[cupId] = cup;
  order.cupId = cupId;
  for (const coin of combo) {
    moveCoin(coin, 'CUP', { orderId: order.id, claimId: claim.id, cupId, channelId: coin.channelId });
    cup.coinIds.push(coin.id);
  }
  addEvent(m, 'cup.prepared', { cupId, orderId: order.id, coinIds: cup.coinIds });
  return { status: 201, body: { cup: cupView(cup), claim: claimView(m, claim.id), order: orderView(m, order.id) } };
}

export function sealCup(m, cupId, input = {}) {
  const cup = mustCup(m, cupId);
  if (cup.status === 'SEALED_READY') return { status: 200, body: cupView(cup) };
  if (cup.status !== 'PACKING') conflict('CUP_NOT_PACKING', `Cup is ${cup.status}.`);
  const actualAmount = cup.coinIds.reduce((sum, id) => sum + (m.coins[id]?.denomination || 0), 0);
  const allInCup = cup.coinIds.every((id) => m.coins[id]?.location === 'CUP' && m.coins[id]?.cupId === cup.id);
  if (!allInCup || actualAmount !== cup.expectedAmount || cup.coinIds.length > cup.capacityCoins) {
    conflict('CUP_INCOMPLETE', 'Seal check failed; shipment remains blocked.', {
      expectedAmount: cup.expectedAmount,
      actualAmount,
      allInCup,
      coinCount: cup.coinIds.length,
      capacity: cup.capacityCoins,
    });
  }
  cup.status = 'SEALED_READY';
  cup.sealedAt = nowIso();
  cup.sealCheck = { complete: true, amount: actualAmount, coinCount: cup.coinIds.length, confirmedBy: input.confirmedBy || 'maintenance' };
  const claim = m.claims[cup.claimId];
  if (claim) claim.status = 'SEALED';
  addEvent(m, 'cup.sealed_complete', { cupId: cup.id, amount: actualAmount });
  return { status: 200, body: { cup: cupView(cup), order: orderView(m, cup.orderId) } };
}

export function sendCupToPickup(m, cupId) {
  const cup = mustCup(m, cupId);
  const order = mustOrder(m, cup.orderId);
  if (!['SEALED_READY'].includes(cup.status)) conflict('CUP_NOT_SEALED', 'Only a sealed ready cup can be sent to pickup.');
  if (order.fulfillmentStatus === 'NOT_REQUESTED') conflict('VEND_NOT_INSTRUCTED', 'Vend must be instructed before moving the cup to pickup.');
  cup.status = 'AT_PICKUP';
  cup.location = 'PICKUP';
  cup.arrivedPickupAt = nowIso();
  const claim = m.claims[cup.claimId];
  if (claim) claim.status = 'READY_FOR_PICKUP';
  order.handedOutAmount += cup.expectedAmount;
  addEvent(m, 'cup.arrived_pickup', { cupId: cup.id, orderId: order.id });
  return { status: 200, body: { cup: cupView(cup), claim: claimView(m, claim.id), order: orderView(m, order.id) } };
}

function mustCup(m, cupId) {
  if (!cupId) badRequest('CUP_ID_REQUIRED', 'cupId is required.');
  const cup = m.cups[cupId];
  if (!cup) notFound('CUP_NOT_FOUND', `Cup ${cupId} not found.`);
  return cup;
}

export function cancelOrder(m, orderId, input = {}) {
  const order = mustOrder(m, orderId);
  if (order.status === 'CANCELLED') conflict('ORDER_ALREADY_CANCELLED', 'Order is already cancelled.');
  if (order.fulfillmentStatus === 'DELIVERED') {
    conflict('GOODS_ALREADY_DELIVERED', 'Goods outlet report exists; use the order claim for change/refund differences.');
  }

  const refundClaims = [];

  // An internal cup has not reached the customer: release its tube coins.
  if (order.cupId) {
    const cup = m.cups[order.cupId];
    if (cup && ['PACKING', 'SEALED_READY'].includes(cup.status)) {
      for (const coinId of cup.coinIds) {
        const coin = m.coins[coinId];
        if (coin && coin.location === 'CUP') {
          moveCoin(coin, 'TUBE', { orderId: null, claimId: null, cupId: null });
        }
      }
      cup.status = 'CANCELLED';
      cup.cancelledAt = nowIso();
      const cupClaim = m.claims[cup.claimId];
      if (cupClaim) cupClaim.status = 'CANCELLED';
    } else if (cup && cup.status === 'AT_PICKUP') {
      conflict('CUP_ALREADY_AT_PICKUP', 'Cup is physically at the pickup opening; it remains claimable rather than internally cancelled.');
    }
  }

  // Close the future payout part of stream claims while preserving coins already at tray.
  for (const claimId of order.claimIds) {
    const claim = m.claims[claimId];
    if (!claim || ['COLLECTED', 'CANCELLED'].includes(claim.status)) continue;
    if (claim.kind === 'CUP' && claim.cupId && m.cups[claim.cupId]?.status === 'CANCELLED') {
      claim.status = 'CANCELLED';
      continue;
    }
    if (claim.activeDispenseId) {
      const d = m.dispenses[claim.activeDispenseId];
      if (d && ['IN_CHANNEL', 'SENSOR_PASSED'].includes(d.status)) {
        conflict('DISPENSE_IN_FLIGHT', 'Resolve the active channel coin before cancelling.', { dispenseId: d.id });
      }
    }
    const delivered = claim.amountDelivered;
    const remaining = claim.amountExpected - delivered;
    claim.cancelledRemainingAmount = Math.max(0, remaining);
    claim.originalExpectedAmount = claim.amountExpected;
    claim.amountExpected = delivered;
    if (delivered > 0) {
      claim.status = 'READY_FOR_PICKUP';
      claim.note = 'Remaining payout cancelled; delivered coins remain assigned for pickup.';
    } else {
      claim.status = 'CANCELLED';
    }
    claim.updatedAt = nowIso();
  }

  const handedOut = order.handedOutAmount;
  const originalCoinIds = [...order.coinIds];
  const canReturnOriginals = input.refundMode === 'ORIGINAL' && handedOut === 0;

  if (canReturnOriginals) {
    const claim = createChangeClaim(m, order, 'REFUND_ORIGINAL', order.paidAmount, {
      claimId: `CLM-${order.id}-REFUND-ORIGINAL`,
      status: 'READY_FOR_PICKUP',
      note: 'Original inserted coins moved from escrow to pickup.',
    });
    claim.originalCoinIds = originalCoinIds;
    for (const coinId of originalCoinIds) {
      const coin = m.coins[coinId];
      if (!coin || coin.location !== 'ESCROW') {
        conflict('ORIGINAL_COIN_NOT_IN_ESCROW', `Original coin ${coinId} is no longer in the temporary hopper.`);
      }
      moveCoin(coin, 'TRAY', { claimId: claim.id });
      claim.deliveredCoinIds.push(coinId);
    }
    claim.amountDelivered = order.paidAmount;
    refundClaims.push(claim.id);
  } else {
    const netRefundAmount = order.paidAmount - handedOut;
    if (netRefundAmount > 0) {
      const claim = createChangeClaim(m, order, 'REFUND_NET', netRefundAmount, {
        claimId: `CLM-${order.id}-REFUND-NET`,
        status: 'OPEN',
        note: 'Net refund after deducting coins/cup value already issued to this order.',
      });
      claim.deductedHandedOutAmount = handedOut;
      claim.originalCoinIds = originalCoinIds;
      refundClaims.push(claim.id);
    }
  }

  order.status = 'CANCELLED';
  order.cancelledAt = nowIso();
  order.updatedAt = order.cancelledAt;
  addEvent(m, 'order.cancelled', { orderId: order.id, refundClaims, handedOutAmount: handedOut });
  return { status: 200, body: { order: orderView(m, order.id), refundClaims } };
}

export function claimEntitlement(m, orderId, claimId, input = {}) {
  const order = mustOrder(m, orderId);
  const claim = mustClaim(m, claimId, order.id);
  if (['COLLECTED', 'CANCELLED'].includes(claim.status)) {
    conflict('CLAIM_CLOSED', `Claim ${claim.id} is ${claim.status}.`);
  }
  claim.claimRequestCount = (claim.claimRequestCount || 0) + 1;
  claim.lastClaimRequestAt = nowIso();
  claim.updatedAt = claim.lastClaimRequestAt;
  const remaining = Math.max(0, claim.amountExpected - claim.amountDelivered);
  addEvent(m, 'claim.requested', { orderId: order.id, claimId: claim.id, remaining });
  return {
    status: 202,
    body: {
      ok: true,
      orderId: order.id,
      claim: claimView(m, claim.id),
      nextAction: remaining > 0
        ? 'CALL dispense-next TO ROUTE THE REMAINING AMOUNT'
        : 'OPEN_PICKUP_DOOR_AND_TAKE_LISTED_COINS_OR_CUP',
    },
  };
}

function coinView(coin) {
  return {
    id: coin.id,
    denomination: coin.denomination,
    location: coin.location,
    channelId: coin.channelId,
    tubeId: coin.tubeId,
    orderId: coin.orderId,
    claimId: coin.claimId,
    cupId: coin.cupId,
    dispenseId: coin.dispenseId,
  };
}

function dispenseView(d) {
  return {
    id: d.id,
    orderId: d.orderId,
    claimId: d.claimId,
    coinId: d.coinId,
    denomination: d.denomination,
    channelId: d.channelId,
    status: d.status,
    sensorPassed: d.sensorPassed,
    jamLocation: d.jamLocation || null,
    createdAt: d.createdAt,
    sensorPassedAt: d.sensorPassedAt || null,
    deliveredAt: d.deliveredAt || null,
  };
}

function claimView(m, claimId) {
  const c = typeof claimId === 'object' ? claimId : m.claims[claimId];
  if (!c) return null;
  return {
    ...c,
    amountRemaining: Math.max(0, c.amountExpected - c.amountDelivered),
    amountUncollected: Math.max(0, c.amountDelivered - c.amountCollected),
    coins: c.deliveredCoinIds.map((id) => coinView(m.coins[id])).filter(Boolean),
  };
}

function cupView(cup) {
  return { ...cup, coins: cup.coinIds };
}

export function orderView(m, orderId) {
  const o = typeof orderId === 'object' ? orderId : m.orders[orderId];
  if (!o) return null;
  const claims = o.claimIds.map((id) => claimView(m, id)).filter(Boolean);
  const openClaims = claims.filter((c) => ['OPEN', 'PAYING', 'SEALED', 'READY_FOR_PICKUP', 'PARTIALLY_COLLECTED', 'PACKING'].includes(c.status));
  const trayCoins = Object.values(m.coins)
    .filter((c) => c.orderId === o.id && c.location === 'TRAY')
    .map(coinView);
  const customerCoins = Object.values(m.coins)
    .filter((c) => c.orderId === o.id && c.location === 'CUSTOMER')
    .map(coinView);
  const pickupCup = o.cupId ? cupView(m.cups[o.cupId]) : null;
  const totalClaimRemaining = claims
    .filter((c) => ['OPEN', 'PAYING'].includes(c.status))
    .reduce((sum, c) => sum + c.amountRemaining, 0);
  const pickupValue = trayCoins.reduce((s, c) => s + c.denomination, 0)
    + (pickupCup?.location === 'PICKUP' ? pickupCup.expectedAmount : 0);
  return {
    ...o,
    paidCoins: o.coinIds.map((id) => coinView(m.coins[id])).filter(Boolean),
    claims,
    openClaims,
    trayCoins,
    customerCoins,
    pickupCup,
    pickupValue,
    totalClaimRemaining,
    collectedAmount: o.collectedAmount,
    pickupSlotEmptyForOrder: pickupValue === 0,
    hasMoneyAwaitingPickup: pickupValue > 0,
    hasUnpaidClaim: totalClaimRemaining > 0,
    queryScope: `machine:${m.id}`,
  };
}

export function tubeValue(m) {
  return Object.values(m.coins).filter((c) => c.location === 'TUBE').reduce((s, c) => s + c.denomination, 0);
}

export function deliverableValue(m) {
  return availableTubeCoins(m).reduce((s, c) => s + c.denomination, 0);
}

export function customerObtainedValue(m) {
  return Object.values(m.coins).filter((c) => c.location === 'CUSTOMER').reduce((s, c) => s + c.denomination, 0);
}

export function reconcile(m, physicalInput = null) {
  const counts = {};
  const values = {};
  for (const loc of LOCATIONS) {
    counts[loc] = {};
    values[loc] = 0;
  }
  for (const coin of Object.values(m.coins)) {
    counts[coin.location][coin.denomination] = (counts[coin.location][coin.denomination] || 0) + 1;
    values[coin.location] = (values[coin.location] || 0) + coin.denomination;
  }
  const errors = [];

  for (const o of Object.values(m.orders)) {
    const paidSum = o.coinIds.reduce((s, id) => s + (m.coins[id]?.denomination || 0), 0);
    if (paidSum !== o.paidAmount) errors.push({ code: 'PAID_AMOUNT_MISMATCH', orderId: o.id, paidSum, paidAmount: o.paidAmount });
    const collectedSum = Object.values(m.coins)
      .filter((c) => c.orderId === o.id && c.location === 'CUSTOMER')
      .reduce((s, c) => s + c.denomination, 0);
    if (collectedSum !== o.collectedAmount) errors.push({ code: 'COLLECTED_AMOUNT_MISMATCH', orderId: o.id, collectedSum, recorded: o.collectedAmount });
  }

  const coinClaimUse = new Map();
  for (const claim of Object.values(m.claims)) {
    let delivered = 0;
    for (const coinId of claim.deliveredCoinIds || []) {
      const coin = m.coins[coinId];
      if (!coin) errors.push({ code: 'MISSING_CLAIM_COIN', claimId: claim.id, coinId });
      delivered += coin?.denomination || 0;
      if (coinClaimUse.has(coinId) && coinClaimUse.get(coinId) !== claim.id) {
        errors.push({ code: 'COIN_SHARED_BY_CLAIMS', coinId, claims: [coinClaimUse.get(coinId), claim.id] });
      }
      coinClaimUse.set(coinId, claim.id);
    }
    if (delivered !== claim.amountDelivered) {
      errors.push({ code: 'CLAIM_DELIVERED_MISMATCH', claimId: claim.id, delivered, recorded: claim.amountDelivered });
    }
  }

  for (const cup of Object.values(m.cups)) {
    if (m.orders[cup.orderId]?.cupId !== cup.id) {
      errors.push({ code: 'CUP_ORDER_BACKREFERENCE_MISMATCH', cupId: cup.id, orderId: cup.orderId });
    }
    for (const other of Object.values(m.cups)) {
      if (other.id !== cup.id && other.orderId === cup.orderId) {
        errors.push({ code: 'TWO_CUPS_FOR_ORDER', orderId: cup.orderId, cups: [cup.id, other.id] });
      }
    }
  }

  let physicalComparison = null;
  if (physicalInput?.physicalCounts) {
    const discrepancies = [];
    for (const loc of LOCATIONS) {
      const expected = counts[loc] || {};
      const actual = physicalInput.physicalCounts[loc] || {};
      const denoms = new Set([...Object.keys(expected), ...Object.keys(actual)]);
      for (const d of denoms) {
        if (Number(expected[d] || 0) !== Number(actual[d] || 0)) {
          discrepancies.push({ location: loc, denomination: Number(d), ledger: expected[d] || 0, physicallyCounted: actual[d] || 0 });
        }
      }
    }
    let customerComparison = null;
    if (physicalInput.customerObtainedAmount !== undefined) {
      const ledgerCustomer = customerObtainedValue(m);
      const reported = Number(physicalInput.customerObtainedAmount);
      customerComparison = { ledger: ledgerCustomer, physicallyReported: reported, match: ledgerCustomer === reported };
      if (ledgerCustomer !== reported) discrepancies.push({ code: 'CUSTOMER_OBTAINED_MISMATCH', ledger: ledgerCustomer, reported });
    }
    physicalComparison = { matches: discrepancies.length === 0, discrepancies, customerComparison };
    if (discrepancies.length) errors.push({ code: 'PHYSICAL_COUNT_MISMATCH', discrepancies });
    m.audits.push({ at: nowIso(), ...physicalInput, result: physicalComparison });
  }

  return {
    machineId: m.id,
    balanced: errors.length === 0,
    errors,
    coinCountsByLocationAndDenomination: counts,
    valueByLocation: values,
    tubeValue: tubeValue(m),
    deliverableValue: deliverableValue(m),
    customerObtainedValue: customerObtainedValue(m),
    orderCount: Object.keys(m.orders).length,
    claimCount: Object.keys(m.claims).length,
    cupCount: Object.keys(m.cups).length,
    physicalComparison,
    orders: Object.values(m.orders).map((o) => orderView(m, o.id)),
  };
}

export function listMachines(state) {
  return Object.values(state.machines).map((m) => ({ id: m.id, createdAt: m.createdAt, orderCount: Object.keys(m.orders).length }));
}
