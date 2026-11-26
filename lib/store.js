'use strict';

const fs = require('node:fs');
const path = require('node:path');

class HttpError extends Error {
  constructor(status, code, message, details = {}) {
    super(message);
    this.name = 'HttpError';
    this.status = status;
    this.code = code;
    this.details = details;
    // Some expected failures still create an audit/state record (e.g. a failed call).
    this.persist = Boolean(details.persist);
  }
}

function initialDb() {
  return {
    schema: 1,
    revision: 0,
    seq: 0,
    agents: {
      a: {
        id: 'a',
        name: '甲',
        extension: '1001',
        skills: ['date'],
        onDuty: true,
        presence: 'available',
      },
      b: {
        id: 'b',
        name: '乙',
        extension: '1002',
        skills: ['date', 'pickup'],
        onDuty: true,
        presence: 'available',
      },
    },
    callers: {},
    cases: {},
    calls: {},
    rooms: {},
    utterances: {},
    handovers: {},
    audioSnapshots: [],
    providerEvents: {},
    audit: [],
  };
}

class JsonStore {
  constructor(file) {
    this.file = file;
    this.db = this.#load();
  }

  #load() {
    if (!this.file || this.file === ':memory:') return initialDb();
    try {
      const raw = fs.readFileSync(this.file, 'utf8');
      const parsed = JSON.parse(raw);
      return { ...initialDb(), ...parsed };
    } catch (err) {
      if (err.code === 'ENOENT') return initialDb();
      throw new Error(`无法读取持久化数据 ${this.file}: ${err.message}`);
    }
  }

  save() {
    if (!this.file || this.file === ':memory:') return;
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const tmp = `${this.file}.tmp-${process.pid}-${Date.now()}`;
    fs.writeFileSync(tmp, JSON.stringify(this.db, null, 2), 'utf8');
    fs.renameSync(tmp, this.file);
  }

  mutate(fn) {
    try {
      const result = fn(this.db);
      this.save();
      return result;
    } catch (err) {
      if (err instanceof HttpError && err.persist) this.save();
      throw err;
    }
  }

  reset() {
    this.db = initialDb();
    this.save();
    return this.db;
  }
}

module.exports = { JsonStore, HttpError, initialDb };
