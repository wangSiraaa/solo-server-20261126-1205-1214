// Small, dependency-free utilities shared across the service.

export class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details ?? null;
  }
}

export const badRequest = (msg, details) => new ApiError(400, 'BAD_REQUEST', msg, details);
export const notFound = (msg, details) => new ApiError(404, 'NOT_FOUND', msg, details);
export const conflict = (msg, details) => new ApiError(409, 'CONFLICT', msg, details);
export const unprocessable = (msg, details) => new ApiError(422, 'UNPROCESSABLE', msg, details);

let counter = 0;
export function uid(prefix = 'id') {
  counter = (counter + 1) % 1_000_000;
  const t = Date.now().toString(36);
  const r = Math.random().toString(36).slice(2, 8);
  return `${prefix}_${t}${counter.toString(36)}${r}`;
}

export function clone(x) {
  return x === undefined ? x : structuredClone(x);
}

export function asArray(x) {
  if (x === undefined || x === null) return [];
  return Array.isArray(x) ? x : [x];
}

export function uniq(arr) {
  return [...new Set(arr)];
}

// ---- night helpers -------------------------------------------------------
// A "night" is an ISO date string "YYYY-MM-DD" identifying the night that
// begins on that calendar day.

const DAY = 24 * 60 * 60 * 1000;

export function parseNight(n) {
  if (typeof n !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(n)) {
    throw badRequest(`invalid night "${n}" (expected YYYY-MM-DD)`);
  }
  const ts = Date.parse(`${n}T00:00:00Z`);
  if (Number.isNaN(ts)) throw badRequest(`invalid night "${n}"`);
  return ts;
}

export function nightKey(ts) {
  return new Date(ts).toISOString().slice(0, 10);
}

export function addNight(n, delta) {
  return nightKey(parseNight(n) + delta * DAY);
}

export function sortNights(nights) {
  return [...new Set(nights)].sort();
}

export function isConsecutive(nights) {
  const s = sortNights(nights);
  for (let i = 1; i < s.length; i += 1) {
    if (parseNight(s[i]) - parseNight(s[i - 1]) !== DAY) return false;
  }
  return true;
}

// Split a set of nights into maximal consecutive runs (sorted).
export function consecutiveRuns(nights) {
  const s = sortNights(nights);
  const runs = [];
  for (const n of s) {
    const last = runs[runs.length - 1];
    if (last && parseNight(n) - parseNight(last[last.length - 1]) === DAY) {
      last.push(n);
    } else {
      runs.push([n]);
    }
  }
  return runs;
}

export function intersectNights(a, b) {
  const bs = new Set(b);
  return sortNights(a.filter((n) => bs.has(n)));
}

export function differenceNights(a, b) {
  const bs = new Set(b);
  return sortNights(a.filter((n) => !bs.has(n)));
}

export function unionNights(...lists) {
  return sortNights(lists.flat());
}

export function nightsEqual(a, b) {
  const x = sortNights(a);
  const y = sortNights(b);
  if (x.length !== y.length) return false;
  return x.every((n, i) => n === y[i]);
}

export function expandWeekends(weekends) {
  // weekends: { w1: ["2026-12-04","2026-12-05"], ... } -> flat sorted nights
  const out = [];
  for (const v of Object.values(weekends ?? {})) out.push(...asArray(v));
  return sortNights(out);
}

export function nowIso() {
  return new Date().toISOString();
}
