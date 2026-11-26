import fs from 'node:fs';
import path from 'node:path';
import { createDb } from './seed.js';

export class JsonStore {
  constructor(file) {
    this.file = file;
    this.db = createDb();
    this.load();
  }

  load() {
    if (!fs.existsSync(this.file)) {
      this.db = createDb();
      this.save();
      return this.db;
    }
    const raw = fs.readFileSync(this.file, 'utf8');
    this.db = raw.trim() ? JSON.parse(raw) : createDb();
    return this.db;
  }

  reload() {
    return this.load();
  }

  save() {
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const tmp = `${this.file}.tmp-${process.pid}`;
    fs.writeFileSync(tmp, JSON.stringify(this.db, null, 2));
    fs.renameSync(tmp, this.file);
  }

  transact(fn) {
    try {
      const result = fn(this.db);
      this.save();
      return result;
    } catch (err) {
      // Commands mutate the in-memory document as they run. If one fails,
      // reload the last atomically committed version rather than leaving a
      // half-applied transaction that could later be persisted.
      this.load();
      throw err;
    }
  }

  query(fn) {
    return fn(this.db);
  }
}
