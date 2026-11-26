import fs from 'node:fs';
import path from 'node:path';
import { defaultLeague } from './adjudication.mjs';

export class JsonStore {
  constructor(dataFile, now = new Date().toISOString()) {
    this.dataFile = dataFile;
    this.state = this.#load(now);
  }

  #load(now) {
    try {
      const raw = fs.readFileSync(this.dataFile, 'utf8');
      const parsed = JSON.parse(raw);
      if (!parsed.games || !parsed.results || !parsed.requests) {
        throw new Error('存档缺少 games/results/requests 字段');
      }
      parsed.league ||= defaultLeague(now);
      return parsed;
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      return {
        version: 1,
        league: defaultLeague(now),
        games: {},
        results: {},
        requests: {},
        createdAt: now,
      };
    }
  }

  save() {
    fs.mkdirSync(path.dirname(this.dataFile), { recursive: true });
    const tmp = `${this.dataFile}.tmp-${process.pid}-${Date.now()}`;
    fs.writeFileSync(tmp, JSON.stringify(this.state, null, 2));
    fs.renameSync(tmp, this.dataFile);
  }

  getGame(id) {
    return this.state.games[id] || null;
  }

  putGame(game) {
    this.state.games[game.id] = game;
  }

  findCase(caseId) {
    for (const game of Object.values(this.state.games)) {
      const found = game.adjudication?.cases?.find((item) => item.id === caseId);
      if (found) return { game, case: found };
    }
    return null;
  }

  resultFor(gameId) {
    return this.state.results[gameId] || null;
  }

  putResult(result) {
    this.state.results[result.gameId] = result;
  }

  idempotentResponse(key) {
    return key ? this.state.requests[key] || null : null;
  }

  saveIdempotentResponse(key, requestHash, statusCode, body) {
    if (!key) return;
    this.state.requests[key] = { requestHash, statusCode, body, at: new Date().toISOString() };
  }
}
