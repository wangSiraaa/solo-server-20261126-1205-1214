import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const DEFAULT_DATA_FILE = path.join(__dirname, '..', 'data', 'phonics-review-room.json');

export function emptyDatabase() {
  return {
    meta: {
      schemaVersion: 1,
      createdAt: new Date().toISOString(),
      currentDay: null,
    },
    students: [],
    kps: [],
    cards: [],
    units: [],
    assignments: [],
    gates: [],
    packs: [],
    rounds: [],
    submissions: [],
    evidence: [],
    makeupOrders: [],
    requestLog: {},
  };
}

export class JsonStore {
  constructor(filename = DEFAULT_DATA_FILE) {
    this.filename = filename;
    this.memory = filename === ':memory:';
    this.db = this.load();
  }

  load() {
    if (this.memory) return emptyDatabase();
    try {
      const raw = fs.readFileSync(this.filename, 'utf8');
      const parsed = JSON.parse(raw);
      return { ...emptyDatabase(), ...parsed };
    } catch (err) {
      if (err.code === 'ENOENT') return emptyDatabase();
      throw new Error(`Cannot read data file ${this.filename}: ${err.message}`);
    }
  }

  save() {
    if (this.memory) return;
    fs.mkdirSync(path.dirname(this.filename), { recursive: true });
    const tmp = `${this.filename}.tmp-${process.pid}-${Date.now()}`;
    fs.writeFileSync(tmp, JSON.stringify(this.db, null, 2), 'utf8');
    fs.renameSync(tmp, this.filename);
  }

  /**
   * All state changes go through mutate so every HTTP request is atomically
   * persisted before the response is sent.
   */
  mutate(fn) {
    const result = fn(this.db);
    this.save();
    return result;
  }

  find(collection, id) {
    return this.db[collection]?.find((x) => x.id === id) ?? null;
  }

  insert(collection, doc) {
    this.db[collection].push(doc);
    this.save();
    return doc;
  }

  reset() {
    this.db = emptyDatabase();
    this.save();
  }
}
