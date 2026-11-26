import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createSeedDb } from './seed.js';

const PROJECT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

export class Store {
  constructor(options = {}) {
    this.dbPath = options.dbPath ??
      process.env.DB_PATH ??
      path.join(PROJECT_ROOT, 'data', 'db.json');
    this.db = this.load(options.scenario || options.defaultScenario || 'pre-split');
  }

  load(scenario) {
    if (this.dbPath && this.dbPath !== ':memory:' && fs.existsSync(this.dbPath)) {
      try {
        const parsed = JSON.parse(fs.readFileSync(this.dbPath, 'utf8'));
        if (parsed && parsed.meta && parsed.headwords) return parsed;
      } catch (error) {
        console.warn(`[store] 无法读取 ${this.dbPath}，将重建种子：${error.message}`);
      }
    }
    return createSeedDb({ scenario });
  }

  reset(scenario = 'pre-split') {
    const fresh = createSeedDb({ scenario });
    // 原地替换顶层集合，使 server 启动时捕获的 db 引用在 reset 后仍指向同一根对象。
    for (const key of Object.keys(this.db)) delete this.db[key];
    Object.assign(this.db, fresh);
    this.save();
    return this.db;
  }

  save() {
    if (!this.dbPath || this.dbPath === ':memory:') return;
    fs.mkdirSync(path.dirname(this.dbPath), { recursive: true });
    const tmp = `${this.dbPath}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(this.db, null, 2), 'utf8');
    fs.renameSync(tmp, this.dbPath);
  }
}
