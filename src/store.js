import fs from 'node:fs';
import path from 'node:path';
import { seedDb } from './model.js';

export class JsonStore {
  constructor(file = '/workspace/data/pottery.json') {
    this.file = file;
    if (file === ':memory:' || !file) {
      this.db = seedDb();
      this.inMemory = true;
      return;
    }
    fs.mkdirSync(path.dirname(file), { recursive: true });
    if (fs.existsSync(file)) {
      try {
        this.db = JSON.parse(fs.readFileSync(file, 'utf8'));
      } catch (error) {
        const backup = `${file}.corrupt-${Date.now()}`;
        fs.renameSync(file, backup);
        this.db = seedDb();
        this.save();
      }
    } else {
      this.db = seedDb();
      this.save();
    }
  }

  save() {
    if (this.inMemory) return;
    const tmp = `${this.file}.tmp-${process.pid}-${Date.now()}`;
    fs.writeFileSync(tmp, JSON.stringify(this.db, null, 2));
    fs.renameSync(tmp, this.file);
  }

  mutate(fn) {
    const result = fn(this.db);
    this.save();
    return result;
  }

  reset() {
    this.db = seedDb();
    this.save();
    return this.db;
  }
}
