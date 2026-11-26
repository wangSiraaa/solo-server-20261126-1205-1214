import fs from 'node:fs';
import path from 'node:path';

export function createInitialState(now = new Date().toISOString()) {
  return {
    schemaVersion: 1,
    createdAt: now,
    topology: {
      sources: [],
      segments: [],
      points: [],
      edges: [],
    },
    tickets: [],
    cases: [],
    inspections: [],
    borrows: [],
    repairs: [],
    revisits: [],
    notifications: [],
    signoffs: [],
    recheckRequests: [],
    audit: [],
    requestLog: {},
  };
}

export class MemoryStore {
  constructor(state = null) {
    this.state = state ?? createInitialState();
  }

  readState() {
    return this.state;
  }

  writeState(state) {
    this.state = state;
  }
}

/** JSON 文件持久化：隔天重启进程后，通知去重键和签收记录仍在。 */
export class JsonFileStore {
  constructor(filePath) {
    this.filePath = filePath;
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    if (!fs.existsSync(filePath)) {
      this.state = createInitialState();
      this.writeState(this.state);
    } else {
      this.state = JSON.parse(fs.readFileSync(filePath, 'utf8'));
    }
  }

  readState() {
    return this.state;
  }

  writeState(state) {
    this.state = state;
    const temporary = `${this.filePath}.tmp`;
    fs.writeFileSync(temporary, JSON.stringify(state, null, 2), 'utf8');
    fs.renameSync(temporary, this.filePath);
  }
}
