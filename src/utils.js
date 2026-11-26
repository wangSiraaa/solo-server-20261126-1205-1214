'use strict';

const crypto = require('node:crypto');

function isoNow(clock = () => new Date()) {
  return clock().toISOString();
}

function timeMs(value) {
  if (value === undefined || value === null) return Date.now();
  const ms = value instanceof Date ? value.getTime() : Date.parse(value);
  if (Number.isNaN(ms)) throw new TypeError(`Invalid timestamp: ${value}`);
  return ms;
}

function integerPoints(value, field = 'points') {
  const n = Number(value);
  if (!Number.isInteger(n) || n < 0) {
    const error = new TypeError(`${field} must be a non-negative integer`);
    error.status = 400;
    error.code = 'INVALID_POINTS';
    throw error;
  }
  return n;
}

function generatedId(prefix) {
  return `${prefix}-${crypto.randomBytes(5).toString('hex').toUpperCase()}`;
}

function stableHash(value) {
  return crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
}

function uniqueStrings(values, field = 'ids') {
  if (!Array.isArray(values)) {
    const error = new TypeError(`${field} must be an array`);
    error.status = 400;
    error.code = 'INVALID_ARRAY';
    throw error;
  }
  const set = new Set();
  for (const value of values) {
    if (typeof value !== 'string' || !value.trim()) {
      const error = new TypeError(`${field} must contain non-empty strings`);
      error.status = 400;
      error.code = 'INVALID_ID';
      throw error;
    }
    if (set.has(value)) {
      const error = new Error(`${field} contains duplicate ${value}`);
      error.status = 400;
      error.code = 'DUPLICATE_ID';
      throw error;
    }
    set.add(value);
  }
  return [...set];
}

/**
 * Split total integer points by integer weights. Fractional remainders are
 * distributed one point at a time, largest fractional share first; ties keep
 * the caller's order. The returned sums are guaranteed to equal total.
 */
function allocateIntegerPoints(total, recipients) {
  const totalPoints = integerPoints(total, 'totalPoints');
  if (!Array.isArray(recipients) || recipients.length === 0) return [];

  const normalized = recipients.map((recipient, index) => {
    const weight = Number(recipient.weight ?? recipient.bps ?? 1);
    if (!Number.isFinite(weight) || weight < 0) {
      throw new TypeError('Allocation weights must be non-negative numbers');
    }
    return { ...recipient, weight, index };
  });

  const weightSum = normalized.reduce((sum, item) => sum + item.weight, 0);
  if (weightSum <= 0) {
    // No positive weight: preserve caller order and allocate to earliest.
    return normalized.map((item, i) => ({ ...item, points: i === 0 ? totalPoints : 0 }));
  }

  const exact = normalized.map((item) => (totalPoints * item.weight) / weightSum);
  const floors = exact.map((value) => Math.floor(value));
  let remaining = totalPoints - floors.reduce((a, b) => a + b, 0);

  const remainderOrder = exact
    .map((value, index) => ({ index, fraction: value - Math.floor(value) }))
    .sort((a, b) => {
      if (b.fraction !== a.fraction) return b.fraction - a.fraction;
      return a.index - b.index;
    });

  for (const slot of remainderOrder) {
    if (remaining <= 0) break;
    floors[slot.index] += 1;
    remaining -= 1;
  }

  return normalized.map((item, index) => ({
    payeeStoreId: item.payeeStoreId,
    weight: item.weight,
    points: floors[index],
    reason: item.reason,
    sourceIds: item.sourceIds,
  }));
}

function subtractDaysIso(days, clock = () => new Date()) {
  return new Date(clock().getTime() - days * 24 * 60 * 60 * 1000).toISOString();
}

function addHoursIso(hours, clock = () => new Date()) {
  return new Date(clock().getTime() + hours * 60 * 60 * 1000).toISOString();
}

module.exports = {
  isoNow,
  timeMs,
  integerPoints,
  generatedId,
  stableHash,
  uniqueStrings,
  allocateIntegerPoints,
  subtractDaysIso,
  addHoursIso,
};
