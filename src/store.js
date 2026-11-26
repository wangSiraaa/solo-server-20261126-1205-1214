import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';

/**
 * Tiny durable JSON store. All mutations in a service call run inside one
 * serialized transaction, then the complete snapshot is atomically renamed.
 * This is sufficient for a single-night-school instance and makes restart
 * recovery deterministic without an external database.
 */
export class JsonStore {
  constructor(file, createInitialState) {
    this.file = file;
    this.createInitialState = createInitialState;
    this.state = null;
    this.queue = Promise.resolve();
    this.writeCount = 0;
  }

  async init() {
    try {
      const text = await readFile(this.file, 'utf8');
      this.state = JSON.parse(text);
      return this.state;
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      this.state = this.createInitialState();
      await this.save();
      return this.state;
    }
  }

  async save() {
    if (!this.state) throw new Error('Store is not initialized');
    await mkdir(path.dirname(this.file), { recursive: true });
    const tmp = `${this.file}.${process.pid}.${this.writeCount++}.tmp`;
    await writeFile(tmp, JSON.stringify(this.state, null, 2), 'utf8');
    await rename(tmp, this.file);
  }

  /**
   * Run a synchronous state mutation exclusively and persist it. Domain code
   * throws ServiceError to roll back simply by not saving the tentative change.
   * We pass a structured clone? No: domain mutates the loaded state directly;
   * if it throws before returning, callers should not continue using state.
   * To guarantee rollback, give each transaction a snapshot clone and replace
   * state only after successful mutation.
   */
  tx(fn) {
    const run = this.queue.then(async () => {
      const candidate = structuredClone(this.state);
      const result = await fn(candidate);
      this.state = candidate;
      await this.save();
      return result;
    });

    // Keep the queue alive after an expected failed transaction.
    this.queue = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }

  /** Read against a deep clone so API callers cannot corrupt persisted state. */
  read(fn) {
    const snapshot = structuredClone(this.state);
    return fn ? fn(snapshot) : snapshot;
  }
}
