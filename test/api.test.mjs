import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { ClubApiError, ClubClient } from '../clients/club-client.mjs';
import { createApp } from '../src/app.mjs';
import { JsonStore } from '../src/store.mjs';

const dataFile = path.join(os.tmpdir(), `club-api-test-${process.pid}.json`);
let server;
let baseUrl;
let client;

test.before(async () => {
  fs.rmSync(dataFile, { force: true });
  const store = new JsonStore(dataFile);
  const app = createApp({ store });
  server = http.createServer(app);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  baseUrl = `http://127.0.0.1:${server.address().port}`;
  client = new ClubClient(baseUrl, { prefix: 'api-test' });
});

test.after(async () => {
  await new Promise((resolve) => server.close(resolve));
  fs.rmSync(dataFile, { force: true });
});

test('HTTP：练习赛导入起势盘并核对提子', async () => {
  await client.createGame({
    id: 'http-capture',
    mode: 'practice',
    setup: [
      { x: 1, y: 1, color: 'white' },
      { x: 3, y: 1, color: 'white' },
      { x: 1, y: 2, color: 'black' },
    ],
  });
  const move = await client.place('http-capture', 'black', 2, 1, 'http-capture-move');
  assert.equal(move.move.captured.length, 1);
  assert.deepEqual(move.move.captured[0], { x: 1, y: 1, color: 'white' });
});

test('HTTP：正式局拒绝导入起势盘', async () => {
  await assert.rejects(
    () => client.createGame({ id: 'http-formal-bad', mode: 'formal', setup: [{ x: 1, y: 1, color: 'black' }] }),
    (error) => error instanceof ClubApiError && error.code === 'FORMAL_NO_IMPORT',
  );
});

test('HTTP：双方空清单一致即可入榜，重复结算唯一', async () => {
  await client.createGame({ id: 'http-routine', mode: 'formal', players: { black: 'IT黑', white: 'IT白' } });
  await client.pass('http-routine', 'black', 'routine-b-pass');
  await client.pass('http-routine', 'white', 'routine-w-pass');
  await client.markDead('http-routine', 'black', [], 'routine-b-mark');
  await client.markDead('http-routine', 'white', [], 'routine-w-mark');
  const settled = await client.settle('http-routine', 'routine-settle');
  assert.equal(settled.result.winner, 'draw');
  assert.equal(settled.result.score.total.black, 0);
  assert.equal(settled.result.score.total.white, 0);

  const replaySettle = await client.settle('http-routine', 'routine-settle');
  assert.equal(replaySettle.idempotentReplay, true);
  const results = await client.results();
  assert.equal(results.count, 1);
  assert.equal(results.results[0].gameId, 'http-routine');
});

test('HTTP：重发同一停着不增加手数', async () => {
  await client.createGame({ id: 'http-pass-idem', mode: 'formal' });
  const first = await client.pass('http-pass-idem', 'black', 'same-pass-key');
  const replay = await client.pass('http-pass-idem', 'black', 'same-pass-key');
  assert.equal(first.move.seq, 1);
  assert.equal(replay.idempotentReplay, true);
  const game = await client.getGame('http-pass-idem');
  assert.equal(game.game.moves.length, 1);
  assert.equal(game.game.turn, 'white');
});

test('HTTP：并发重发同一停着也只增加一手', async () => {
  await client.createGame({ id: 'http-concurrent-pass', mode: 'formal' });
  const [first, second] = await Promise.all([
    client.pass('http-concurrent-pass', 'black', 'concurrent-pass-key'),
    client.pass('http-concurrent-pass', 'black', 'concurrent-pass-key'),
  ]);
  assert.equal(first.move.seq, 1);
  assert.equal(second.idempotentReplay, true);
  const game = await client.getGame('http-concurrent-pass');
  assert.equal(game.game.moves.length, 1);
});

test('持久化：重新加载 JSON 存档不会产生额外结果', () => {
  const reloaded = new JsonStore(dataFile);
  assert.equal(Object.keys(reloaded.state.results).length, 1);
  assert.equal(reloaded.getGame('http-routine').result.gameId, 'http-routine');
});
