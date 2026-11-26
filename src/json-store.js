'use strict';

const fs = require('node:fs');
const path = require('node:path');

/**
 * Tiny atomic JSON persistence. Every business operation calls save(), so a
 * process restart reloads exactly the last committed state.
 */
class JsonStore {
  constructor(file, seedState) {
    this.file = file;
    fs.mkdirSync(path.dirname(file), { recursive: true });

    if (fs.existsSync(file)) {
      const raw = fs.readFileSync(file, 'utf8');
      try {
        this.state = JSON.parse(raw);
      } catch (error) {
        throw new Error(`Persisted database is not valid JSON: ${file}: ${error.message}`);
      }
    } else {
      this.state = typeof seedState === 'function' ? seedState() : seedState;
      this.save();
    }
  }

  save() {
    const dir = path.dirname(this.file);
    fs.mkdirSync(dir, { recursive: true });
    const tmp = `${this.file}.${process.pid}.${Date.now()}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(this.state, null, 2), { mode: 0o600 });
    fs.renameSync(tmp, this.file);
  }

  reload() {
    const raw = fs.readFileSync(this.file, 'utf8');
    this.state = JSON.parse(raw);
    return this.state;
  }
}

module.exports = { JsonStore };
