import { mkdir, rename, writeFile, readFile } from 'node:fs/promises';
import path from 'node:path';

/**
 * 极简 JSON 持久化。所有变更经过同一条 Promise 链串行执行，
 * 避免多设备同时重传时在进程内产生写覆盖或重复结算。
 */
export class JsonStore {
  constructor(file) {
    this.file = file;
    this.data = null;
    this.writeChain = Promise.resolve();
  }

  async load(factory) {
    try {
      const raw = await readFile(this.file, 'utf8');
      this.data = JSON.parse(raw);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      this.data = factory();
      await this.persist();
    }
    return this.data;
  }

  async mutate(fn) {
    const run = this.writeChain.then(async () => {
      const result = await fn(this.data);
      await this.persist();
      return result;
    });
    // 不让一次失败永久毒化后续写队列。
    this.writeChain = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }

  async persist() {
    const dir = path.dirname(this.file);
    await mkdir(dir, { recursive: true });
    const tmp = path.join(
      dir,
      `.${path.basename(this.file)}.${process.pid}.${Date.now()}.${Math.random().toString(16).slice(2)}.tmp`,
    );
    await writeFile(tmp, JSON.stringify(this.data, null, 2), 'utf8');
    await rename(tmp, this.file);
  }
}
