import test from 'node:test';
import assert from 'node:assert/strict';
import {
  BLACK,
  WHITE,
  boardSignature,
  collectGroup,
  placeStone,
  scorePosition,
  validateSetup,
} from '../src/go.mjs';
import { applyHand, createGame } from '../src/game.mjs';

test('四邻连通串计气：起势盘合法且白串各自独立', () => {
  const board = validateSetup(5, [
    { x: 1, y: 1, color: WHITE },
    { x: 3, y: 1, color: WHITE },
    { x: 1, y: 2, color: BLACK },
  ]);
  const left = collectGroup(board, 5, 0);
  assert.equal(left.key, 'white:1,1');
  assert.deepEqual(left.liberties, [{ x: 2, y: 1 }]);
  const right = collectGroup(board, 5, 2);
  assert.equal(right.libertyIndices.length, 3);
});

test('落子先提尽气敌串：黑 (2,1) 只提左侧白子', () => {
  const board = validateSetup(5, [
    { x: 1, y: 1, color: WHITE },
    { x: 3, y: 1, color: WHITE },
    { x: 1, y: 2, color: BLACK },
  ]);
  const result = placeStone(board, 5, { x: 2, y: 1 }, BLACK);
  assert.deepEqual(result.captured, [{ x: 1, y: 1, color: WHITE }]);
  assert.equal(result.board[0], null);
  assert.equal(result.board[1], BLACK);
  assert.equal(result.board[2], WHITE);
});

test('提尽敌串后仍无气的己串禁止落子（自杀）', () => {
  let board = validateSetup(5, [
    { x: 1, y: 2, color: BLACK },
    { x: 3, y: 2, color: BLACK },
    { x: 2, y: 1, color: BLACK },
    { x: 2, y: 3, color: BLACK },
  ]);
  assert.throws(
    () => placeStone(board, 5, { x: 2, y: 2 }, WHITE),
    (error) => error.code === 'SUICIDE_FORBIDDEN',
  );
});

test('简单劫：立即还原对手上一手之前盘面的落子被禁止', () => {
  const game = createGame({
    id: 'ko-rule',
    mode: 'practice',
    setup: [
      { x: 2, y: 2, color: WHITE },
      { x: 4, y: 2, color: WHITE },
      { x: 3, y: 1, color: WHITE },
      { x: 3, y: 3, color: WHITE },
      { x: 1, y: 2, color: BLACK },
      { x: 2, y: 1, color: BLACK },
      { x: 2, y: 3, color: BLACK },
    ],
  });

  const captureMove = applyHand(game, { color: BLACK, x: 3, y: 2 });
  assert.equal(captureMove.captured.length, 1);
  assert.deepEqual(captureMove.captured[0], { x: 2, y: 2, color: WHITE });

  assert.throws(
    () => applyHand(game, { color: WHITE, x: 2, y: 2 }),
    (error) => error.code === 'KO_RESTORATION_FORBIDDEN',
  );
});

test('连续两手停着才进入 scoring；重发同一停着由接口幂等处理而非领域重复', () => {
  const game = createGame({ id: 'pass-rule' });
  applyHand(game, { color: BLACK, type: 'pass' });
  assert.equal(game.status, 'active');
  assert.equal(game.consecutivePasses, 1);
  applyHand(game, { color: WHITE, type: 'pass' });
  assert.equal(game.status, 'scoring');
  assert.equal(game.stop.moveSeq, 2);
});

test('空区仅接一色计 territory；只有黑白各一子且空区接两色时和棋', () => {
  const onlyBlack = new Array(25).fill(null);
  onlyBlack[0] = BLACK;
  const blackScore = scorePosition(onlyBlack, 5);
  assert.equal(blackScore.stones.black, 1);
  assert.equal(blackScore.territory.black, 24);
  assert.equal(blackScore.totals.black, 25);
  assert.equal(blackScore.winner, BLACK);

  const drawBoard = new Array(25).fill(null);
  drawBoard[0] = BLACK;
  drawBoard[24] = WHITE;
  const drawScore = scorePosition(drawBoard, 5);
  assert.equal(drawScore.territory.black, 0);
  assert.equal(drawScore.territory.white, 0);
  assert.equal(drawScore.totals.black, 1);
  assert.equal(drawScore.totals.white, 1);
  assert.equal(drawScore.winner, 'draw');
});

test('计分副本移除死串，但 boardSignature 可区分原始盘与计分副本', () => {
  const board = validateSetup(5, [
    { x: 1, y: 1, color: WHITE },
    { x: 1, y: 2, color: WHITE },
    { x: 2, y: 1, color: BLACK },
    { x: 1, y: 3, color: BLACK },
  ]);
  const originalSignature = boardSignature(board, 5);
  const score = scorePosition(board, 5, ['white:1,1|1,2']);
  assert.equal(score.removedGroups.length, 1);
  assert.notEqual(score.scoredBoardSignature, originalSignature);
  assert.equal(board[0], WHITE, '原盘不被计分函数修改');
});
