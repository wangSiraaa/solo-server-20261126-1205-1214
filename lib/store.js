import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_DATA_FILE = path.join(__dirname, '..', 'data', 'store.json');

export function dataFilePath() {
  return process.env.DATA_FILE
    ? path.resolve(process.env.DATA_FILE)
    : DEFAULT_DATA_FILE;
}

export function emptyStore() {
  const now = new Date().toISOString();
  return {
    meta: {
      schemaVersion: 1,
      createdAt: now,
      updatedAt: now,
    },
    counters: {
      buyer: 0,
      merchant: 0,
      product: 0,
      order: 0,
      unit: 0,
      receipt: 0,
      review: 0,
      addendum: 0,
      question: 0,
      answer: 0,
      event: 0,
    },
    products: {},
    buyers: {},
    merchants: {},
    orders: {},
    receipts: {},
    reviews: {},
    questions: {},
    events: [],
  };
}

export function loadStore(file = dataFilePath()) {
  try {
    const raw = fs.readFileSync(file, 'utf8');
    if (!raw.trim()) return emptyStore();
    const parsed = JSON.parse(raw);
    const base = emptyStore();
    return {
      ...base,
      ...parsed,
      meta: { ...base.meta, ...(parsed.meta || {}) },
      counters: { ...base.counters, ...(parsed.counters || {}) },
      products: parsed.products || {},
      buyers: parsed.buyers || {},
      merchants: parsed.merchants || {},
      orders: parsed.orders || {},
      receipts: parsed.receipts || {},
      reviews: parsed.reviews || {},
      questions: parsed.questions || {},
      events: Array.isArray(parsed.events) ? parsed.events : [],
    };
  } catch (err) {
    if (err.code === 'ENOENT') return emptyStore();
    throw err;
  }
}

export function saveStore(store, file = dataFilePath()) {
  store.meta.updatedAt = new Date().toISOString();
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(store, null, 2), 'utf8');
  fs.renameSync(tmp, file);
}

export function nextId(store, kind, prefix) {
  store.counters[kind] = (store.counters[kind] || 0) + 1;
  return `${prefix}_${String(store.counters[kind]).padStart(6, '0')}`;
}

export function appendEvent(store, type, payload = {}) {
  const event = {
    id: nextId(store, 'event', 'evt'),
    type,
    at: new Date().toISOString(),
    ...payload,
  };
  store.events.push(event);
  return event;
}
