import fs from 'node:fs';
import path from 'node:path';
import { createInitialState, HttpError } from './core.mjs';

export class JsonStore {
  constructor(file) {
    this.file = file;
    this.chain = Promise.resolve();
    this.state = this.#load();
  }

  #load() {
    try {
      const raw = fs.readFileSync(this.file, 'utf8');
      const parsed = JSON.parse(raw);
      if (!parsed.machines?.A || !parsed.machines?.B) throw new Error('State file is missing machines A/B');
      return parsed;
    } catch (error) {
      if (error.code === 'ENOENT') return createInitialState();
      throw error;
    }
  }

  reload() {
    this.state = this.#load();
    return this.state;
  }

  #persist() {
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const tmp = `${this.file}.tmp-${process.pid}`;
    fs.writeFileSync(tmp, JSON.stringify(this.state, null, 2), 'utf8');
    fs.renameSync(tmp, this.file);
  }

  /**
   * Serialize mutations. Work on the live state but restore a deep snapshot if
   * a domain rule rejects the operation, so a failed request cannot leave a
   * half-reserved coin behind in memory or on disk.
   */
  mutate(machineId, fn) {
    const run = this.chain.then(async () => {
      if (!this.state.machines[machineId]) {
        throw new HttpError(404, 'MACHINE_NOT_FOUND', `Unknown machine ${machineId}`);
      }
      const snapshot = structuredClone(this.state);
      try {
        const result = await fn(this.state.machines[machineId], this.state);
        this.#persist();
        return result;
      } catch (error) {
        this.state = snapshot;
        throw error;
      }
    });
    this.chain = run.catch(() => {});
    return run;
  }

  mutateGlobal(fn) {
    const run = this.chain.then(async () => {
      const snapshot = structuredClone(this.state);
      try {
        const result = await fn(this.state);
        this.#persist();
        return result;
      } catch (error) {
        this.state = snapshot;
        throw error;
      }
    });
    this.chain = run.catch(() => {});
    return run;
  }

  async idempotent(machineId, requestId, fingerprint, fn) {
    if (!requestId) {
      throw new HttpError(400, 'REQUEST_ID_REQUIRED', 'Supply requestId/eventId or an Idempotency-Key header.');
    }
    return this.mutate(machineId, (machine, state) => {
      const old = machine.idempotency[requestId];
      if (old) {
        if (old.fingerprint !== fingerprint) {
          throw new HttpError(409, 'IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_BODY',
            'This request id was already used with different parameters.', { requestId });
        }
        return {
          status: old.status,
          body: { ...old.body, replayed: true },
        };
      }
      const result = fn(machine, state) || { status: 200, body: { ok: true } };
      machine.idempotency[requestId] = {
        fingerprint,
        status: result.status || 200,
        body: result.body,
      };
      return result;
    });
  }

  snapshot() {
    return structuredClone(this.state);
  }
}
