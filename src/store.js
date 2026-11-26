'use strict';

const fs = require('fs');
const path = require('path');

/**
 * Append-only JSON Lines event store.
 *
 * One writer per file is assumed (the Engine serialises all commands through a
 * single in-memory state). Every append is written and fsync'd before the
 * in-memory state is advanced, so a crash can never make the state ahead of the
 * log. On restart the log is folded back into state.
 */
class EventStore {
  constructor(filePath) {
    this.filePath = filePath;
    this.fd = null;
    this.seq = 0;
  }

  /** Read and parse every event currently persisted, in order. */
  load() {
    const events = [];
    if (fs.existsSync(this.filePath)) {
      const raw = fs.readFileSync(this.filePath, 'utf8');
      for (const line of raw.split('\n')) {
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        events.push(ev);
        if (typeof ev.seq === 'number' && ev.seq > this.seq) this.seq = ev.seq;
      }
    }
    return events;
  }

  /** Durably append one event, returning the stored record (with seq/ts). */
  append(event) {
    if (this.fd === null) {
      fs.mkdirSync(path.dirname(this.filePath), { recursive: true });
      this.fd = fs.openSync(this.filePath, 'a');
    }
    this.seq += 1;
    const record = {
      ...event,
      seq: this.seq,
      ts: event.ts || new Date().toISOString(),
    };
    fs.writeSync(this.fd, JSON.stringify(record) + '\n');
    fs.fsyncSync(this.fd);
    return record;
  }

  close() {
    if (this.fd !== null) {
      fs.closeSync(this.fd);
      this.fd = null;
    }
  }
}

module.exports = { EventStore };
