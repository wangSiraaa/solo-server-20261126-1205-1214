import { DomainError, badRequest } from './errors.mjs';

export const BLACK = 'black';
export const WHITE = 'white';
export const COLORS = [BLACK, WHITE];

export function opponent(color) {
  if (color === BLACK) return WHITE;
  if (color === WHITE) return BLACK;
  throw badRequest('BAD_COLOR', `未知棋色：${color}`);
}

export function assertColor(color) {
  if (!COLORS.includes(color)) throw badRequest('BAD_COLOR', `棋色必须是 black 或 white，收到：${String(color)}`);
  return color;
}

export function coordIndex(size, x, y) {
  if (!Number.isInteger(x) || !Number.isInteger(y) || x < 1 || x > size || y < 1 || y > size) {
    throw badRequest('OUT_OF_BOUNDS', `坐标必须是 1..${size} 的整数，收到 (${String(x)}, ${String(y)})`);
  }
  return (y - 1) * size + (x - 1);
}

export function parseCoord(coord, size) {
  if (!coord || typeof coord !== 'object') {
    throw badRequest('BAD_COORD', '坐标必须是 {x, y}');
  }
  const x = Number(coord.x);
  const y = Number(coord.y);
  if (!Number.isInteger(x) || !Number.isInteger(y)) {
    throw badRequest('BAD_COORD', `坐标必须是整数，收到 ${JSON.stringify(coord)}`);
  }
  const index = coordIndex(size, x, y);
  return { x, y, index };
}

export function indexCoord(index, size) {
  return { x: (index % size) + 1, y: Math.floor(index / size) + 1 };
}

export function neighborIndices(index, size) {
  const x = index % size;
  const y = Math.floor(index / size);
  const result = [];
  if (x > 0) result.push(index - 1);
  if (x < size - 1) result.push(index + 1);
  if (y > 0) result.push(index - size);
  if (y < size - 1) result.push(index + size);
  return result;
}

export function createEmptyBoard(size) {
  if (!Number.isInteger(size) || size < 2 || size > 19) {
    throw badRequest('BAD_SIZE', '棋盘路数必须是 2..19 的整数');
  }
  return Array(size * size).fill(null);
}

export function boardSignature(board, size = Math.sqrt(board.length)) {
  const rows = [];
  for (let y = 0; y < size; y += 1) {
    let row = '';
    for (let x = 0; x < size; x += 1) {
      const stone = board[y * size + x];
      row += stone === BLACK ? 'b' : stone === WHITE ? 'w' : '.';
    }
    rows.push(row);
  }
  return `${size}|${rows.join('/')}`;
}

export function groupKey(color, indicesOrCoords, size) {
  const indices = indicesOrCoords
    .map((item) => (Number.isInteger(item) ? item : parseCoord(item, size).index))
    .sort((a, b) => a - b);
  const points = indices.map((i) => {
    const c = indexCoord(i, size);
    return `${c.x},${c.y}`;
  });
  return `${color}:${points.join('|')}`;
}

export function collectGroup(board, size, startIndex) {
  const color = board[startIndex];
  if (!color) return null;

  const indices = [];
  const indexSet = new Set();
  const liberties = [];
  const libertySet = new Set();
  const stack = [startIndex];
  const visited = new Set([startIndex]);

  while (stack.length) {
    const index = stack.pop();
    if (!indexSet.has(index)) {
      indexSet.add(index);
      indices.push(index);
    }

    for (const next of neighborIndices(index, size)) {
      const stone = board[next];
      if (stone === null) {
        if (!libertySet.has(next)) {
          libertySet.add(next);
          liberties.push(next);
        }
      } else if (stone === color && !visited.has(next)) {
        visited.add(next);
        stack.push(next);
      }
    }
  }

  indices.sort((a, b) => a - b);
  return {
    color,
    key: groupKey(color, indices, size),
    indices,
    points: indices.map((i) => indexCoord(i, size)),
    libertyIndices: [...libertySet].sort((a, b) => a - b),
    liberties: [...libertySet].sort((a, b) => a - b).map((i) => indexCoord(i, size)),
  };
}

export function allGroups(board, size) {
  const seen = new Set();
  const groups = [];
  for (let i = 0; i < board.length; i += 1) {
    if (!board[i] || seen.has(i)) continue;
    const group = collectGroup(board, size, i);
    for (const index of group.indices) seen.add(index);
    groups.push(group);
  }
  return groups;
}

export function validateSetup(size, stones = []) {
  const board = createEmptyBoard(size);
  if (!Array.isArray(stones)) throw badRequest('BAD_SETUP', 'setup 必须是棋子数组');

  for (const raw of stones) {
    const coord = parseCoord(raw, size);
    assertColor(raw.color);
    if (board[coord.index]) {
      throw badRequest('BAD_SETUP', `起势盘在 (${coord.x},${coord.y}) 重复置子`);
    }
    board[coord.index] = raw.color;
  }

  for (const group of allGroups(board, size)) {
    if (group.libertyIndices.length === 0) {
      throw badRequest('ILLEGAL_SETUP', '棋社起势盘存在无气串', { group: group.points });
    }
  }
  return board;
}

/**
 * Low-level placement.
 *
 * Order is the club rule: place stone, capture enemy strings with no
 * liberties, then reject if the player's own string still has no liberty.
 * koSignature is the signature immediately before the opponent's last hand.
 */
export function placeStone(board, size, coordInput, colorInput, options = {}) {
  const color = assertColor(colorInput);
  const coord = parseCoord(coordInput, size);
  if (board[coord.index]) {
    throw new DomainError('POINT_OCCUPIED', `(${coord.x},${coord.y}) 已有棋子`, 409);
  }

  const nextBoard = board.slice();
  nextBoard[coord.index] = color;

  // Find every orthogonally adjacent enemy string before removing anything.
  const enemyGroups = [];
  const seenEnemyStarts = new Set();
  for (const neighbor of neighborIndices(coord.index, size)) {
    const enemyColor = nextBoard[neighbor];
    if (!enemyColor || enemyColor === color || seenEnemyStarts.has(neighbor)) continue;
    const group = collectGroup(nextBoard, size, neighbor);
    for (const index of group.indices) seenEnemyStarts.add(index);
    enemyGroups.push(group);
  }

  const capturedIndices = new Set();
  for (const group of enemyGroups) {
    if (group.libertyIndices.length === 0) {
      for (const index of group.indices) capturedIndices.add(index);
    }
  }

  const captured = [...capturedIndices].sort((a, b) => a - b).map((index) => {
    const c = indexCoord(index, size);
    return { x: c.x, y: c.y, color: nextBoard[index] };
  });
  for (const index of capturedIndices) nextBoard[index] = null;

  const ownGroup = collectGroup(nextBoard, size, coord.index);
  if (ownGroup.libertyIndices.length === 0) {
    throw new DomainError(
      'SUICIDE_FORBIDDEN',
      '提尽敌串后己方串仍无气，此点不能下',
      409,
      { at: { x: coord.x, y: coord.y }, group: ownGroup.points },
    );
  }

  const signature = boardSignature(nextBoard, size);
  if (options.koSignature && signature === options.koSignature) {
    throw new DomainError(
      'KO_RESTORATION_FORBIDDEN',
      '此落子会把盘面立即还原到对手上一手之前，禁止',
      409,
      { at: { x: coord.x, y: coord.y }, restoredSignature: signature },
    );
  }

  return {
    board: nextBoard,
    signature,
    captured,
    placed: { x: coord.x, y: coord.y, color },
    ownGroup: { key: ownGroup.key, points: ownGroup.points, liberties: ownGroup.liberties },
  };
}

export function scorePosition(board, size, deadGroupKeys = []) {
  const deadKeys = deadGroupKeys instanceof Set ? deadGroupKeys : new Set(deadGroupKeys);
  const scoredBoard = board.slice();
  const removedGroups = [];
  const seen = new Set();

  // Remove complete jointly/adjudicated dead strings from the scoring copy only.
  for (let i = 0; i < scoredBoard.length; i += 1) {
    if (!scoredBoard[i] || seen.has(i)) continue;
    const group = collectGroup(scoredBoard, size, i);
    for (const index of group.indices) seen.add(index);
    if (deadKeys.has(group.key)) {
      removedGroups.push({ key: group.key, color: group.color, points: group.points });
      for (const index of group.indices) scoredBoard[index] = null;
    }
  }

  const stones = { [BLACK]: 0, [WHITE]: 0 };
  for (const stone of scoredBoard) {
    if (stone) stones[stone] += 1;
  }

  const visitedEmpty = new Set();
  const territories = [];
  const territory = { [BLACK]: 0, [WHITE]: 0 };

  for (let start = 0; start < scoredBoard.length; start += 1) {
    if (scoredBoard[start] !== null || visitedEmpty.has(start)) continue;

    const points = [];
    const borderColors = new Set();
    const stack = [start];
    visitedEmpty.add(start);

    while (stack.length) {
      const index = stack.pop();
      points.push(indexCoord(index, size));
      for (const neighbor of neighborIndices(index, size)) {
        const stone = scoredBoard[neighbor];
        if (stone === null) {
          if (!visitedEmpty.has(neighbor)) {
            visitedEmpty.add(neighbor);
            stack.push(neighbor);
          }
        } else {
          borderColors.add(stone);
        }
      }
    }

    const owner = borderColors.size === 1 ? [...borderColors][0] : null;
    const sortedPoints = points.sort((a, b) => a.y - b.y || a.x - b.x);
    territories.push({
      points: sortedPoints,
      size: sortedPoints.length,
      borderColors: [...borderColors].sort(),
      owner,
    });
    if (owner) territory[owner] += sortedPoints.length;
  }

  const totals = {
    [BLACK]: stones[BLACK] + territory[BLACK],
    [WHITE]: stones[WHITE] + territory[WHITE],
  };

  return {
    size,
    deadGroupKeys: [...deadKeys],
    removedGroups,
    stones,
    territory,
    totals,
    winner: totals[BLACK] === totals[WHITE] ? 'draw' : totals[BLACK] > totals[WHITE] ? BLACK : WHITE,
    territories,
    scoredBoardSignature: boardSignature(scoredBoard, size),
  };
}
