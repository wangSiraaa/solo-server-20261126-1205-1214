/**
 * 纯领域函数：彩色方格、方向、匹配、装箱最大流。
 * 这里不访问数据库或 HTTP，方便单测和回放。
 */

export const EMPTY = null;

export const TOKEN_COLORS = {
  R: '#e5484d', // red dot
  B: '#2f6fed', // blue edge
  Y: '#f5c518', // yellow center
  G: '#30a46c', // green distractor
  W: '#f8fafc',
  K: '#111827',
  O: '#f76b15',
  P: '#8e4ec6'
};

export function normalizeColor(value) {
  if (value === undefined) return EMPTY;
  if (value === null) return EMPTY;
  let s = String(value).trim();
  if (!s || s === '.' || s === '·' || s === '。' || s === ' ') return EMPTY;
  if (TOKEN_COLORS[s.toUpperCase()]) return s.toUpperCase();
  if (/^[0-9a-f]{3}$/i.test(s)) s = s.split('').map((c) => c + c).join('');
  if (/^[0-9a-f]{6}$/i.test(s)) return `#${s.toUpperCase()}`;
  return s;
}

export function emptyPattern(rows, cols) {
  return {
    rows,
    cols,
    cells: Array.from({ length: rows }, () => Array.from({ length: cols }, () => EMPTY))
  };
}

/**
 * 接受：
 * - ["R....", ".BBB.", ...]
 * - [["R", null, ...], ...]
 * - { rows, cols, cells }
 */
export function normalizePattern(input, expectedRows = null, expectedCols = null) {
  if (input === null || input === undefined) {
    if (expectedRows && expectedCols) return emptyPattern(expectedRows, expectedCols);
    return null;
  }

  let matrix;
  if (Array.isArray(input)) {
    matrix = input;
  } else if (Array.isArray(input.cells)) {
    matrix = input.cells;
  } else if (typeof input.text === 'string') {
    matrix = input.text.split(/\r?\n/).filter((line) => line.length > 0);
  } else {
    throw new TypeError('pattern 必须是字符串数组、二维数组或 {cells}');
  }

  const rows = matrix.length;
  if (!rows) throw new TypeError('pattern 至少需要一行');
  const first = matrix[0];
  const cols = typeof first === 'string' ? [...first].length : first.length;
  if (!cols) throw new TypeError('pattern 至少需要一列');
  if (expectedRows && rows !== expectedRows) {
    throw new TypeError(`pattern 行数应为 ${expectedRows}，实际 ${rows}`);
  }
  if (expectedCols && cols !== expectedCols) {
    throw new TypeError(`pattern 列数应为 ${expectedCols}，实际 ${cols}`);
  }

  const cells = [];
  for (let r = 0; r < rows; r += 1) {
    const rawRow = matrix[r];
    const chars = typeof rawRow === 'string' ? [...rawRow] : rawRow;
    if (chars.length !== cols) {
      throw new TypeError(`第 ${r + 1} 行长度应为 ${cols}，实际 ${chars.length}`);
    }
    cells.push(chars.map(normalizeColor));
  }
  return { rows, cols, cells };
}

export function clonePattern(p) {
  return p ? { rows: p.rows, cols: p.cols, cells: p.cells.map((row) => [...row]) } : null;
}

export function sameDimensions(a, b) {
  return a && b && a.rows === b.rows && a.cols === b.cols;
}

/** 左右翻转：朝条尾看同一截面时使用。 */
export function mirrorHorizontal(p) {
  if (!p) return null;
  return {
    rows: p.rows,
    cols: p.cols,
    cells: p.cells.map((row) => [...row].reverse())
  };
}

export function flipVertical(p) {
  if (!p) return null;
  return { ...p, cells: [...p.cells].reverse().map((row) => [...row]) };
}

export function rotate180(p) {
  return mirrorHorizontal(flipVertical(p));
}

export function transformPattern(p, transform) {
  switch (transform) {
    case 'identity':
    case 'none':
      return clonePattern(p);
    case 'hflip':
    case 'mirror-horizontal':
      return mirrorHorizontal(p);
    case 'vflip':
      return flipVertical(p);
    case 'hv':
    case 'rotate180':
      return rotate180(p);
    default:
      throw new Error(`未知方向变换 ${transform}`);
  }
}

/**
 * 样卡 null 格是留白/不检查格；非 null 格必须与实物一致。
 * exact 模式则要求包括留白在内完全相同。
 */
export function patternMatches(actual, card, mode = card?.matchMode || 'mask') {
  if (!sameDimensions(actual, card)) return false;
  for (let r = 0; r < card.rows; r += 1) {
    for (let c = 0; c < card.cols; c += 1) {
      const want = normalizeColor(card.cells[r][c]);
      const got = normalizeColor(actual.cells[r][c]);
      if (mode === 'exact') {
        if ((want ?? null) !== (got ?? null)) return false;
      } else if (want !== null && want !== got) {
        return false;
      }
    }
  }
  return true;
}

export function allowedTransforms(policy = 'horizontal-ok') {
  switch (policy) {
    case 'strict':
    case 'bar-direction':
    case 'head-tail':
      return ['identity'];
    case 'rotation-ok':
    case 'any-rotation':
      return ['identity', 'hflip', 'vflip', 'rotate180'];
    case 'horizontal-ok':
    case 'flip-ok':
    default:
      return ['identity', 'hflip'];
  }
}

/**
 * 用样卡匹配一个真实面的“条坯坐标图”。
 * 返回所用变换；所有输入都必须来自真实浇注格，不能由系统另造切面。
 */
export function matchActualGrid(actualBarView, card, policy) {
  if (!actualBarView || !card || !sameDimensions(actualBarView, card.pattern || card)) {
    return { matches: false };
  }
  const cardPattern = card.pattern || card;
  const transforms = allowedTransforms(policy || card.orientationPolicy || 'horizontal-ok');
  for (const transform of transforms) {
    const candidate = transformPattern(actualBarView, transform);
    if (patternMatches(candidate, cardPattern, card.matchMode)) {
      return { matches: true, transform };
    }
  }
  return { matches: false };
}

export function renderPatternText(p, { emptyChar = '·' } = {}) {
  if (!p) return '(无格)';
  return p.cells.map((row) => row.map((c) => (c === null ? emptyChar : colorLabel(c))).join(' ')).join('\n');
}

function colorLabel(c) {
  if (TOKEN_COLORS[c]) return c;
  if (c?.startsWith('#')) return c.length === 7 ? c.slice(1).toUpperCase() : c;
  return String(c);
}

/**
 * 从条坯连续区间得到确实形成的两个面。
 * headView 按条头方向记格；tailOutsideView 是站在条尾直接看的左右颠倒图。
 */
export function deriveFaces(bar, start, end) {
  const headCell = bar.cells[start];
  const tailCell = bar.cells[end];
  const headGrid = headCell?.pattern || emptyPattern(bar.rows, bar.cols);
  const tailGrid = tailCell?.pattern || emptyPattern(bar.rows, bar.cols);
  return [
    {
      side: 'head',
      cellIndex: start,
      source: 'actual-pour',
      barView: clonePattern(headGrid),
      outsideView: clonePattern(headGrid)
    },
    {
      side: 'tail',
      cellIndex: end,
      source: 'actual-pour',
      barView: clonePattern(tailGrid),
      outsideView: mirrorHorizontal(tailGrid)
    }
  ];
}

export function remnantSegments(bar) {
  const segments = [];
  let start = null;
  for (let i = 0; i < bar.length; i += 1) {
    const intact = bar.cells[i].state === 'intact';
    if (intact && start === null) start = i;
    if ((!intact || i === bar.length - 1) && start !== null) {
      const end = intact && i === bar.length - 1 ? i : i - 1;
      segments.push({ start, end, length: end - start + 1 });
      start = null;
    }
  }
  return segments;
}

/** 小规模最大流；用于多槽位样卡时把实物片分配给盒内位置。 */
class MaxFlow {
  constructor(n) {
    this.graph = Array.from({ length: n }, () => []);
  }

  addEdge(from, to, cap) {
    const forward = { to, cap, initialCap: cap, reverseIndex: this.graph[to].length };
    const backward = { to: from, cap: 0, initialCap: 0, reverseIndex: this.graph[from].length };
    this.graph[from].push(forward);
    this.graph[to].push(backward);
    return forward;
  }

  maxFlow(source, sink) {
    let flow = 0;
    while (true) {
      const parentNode = new Array(this.graph.length).fill(-1);
      const parentEdge = new Array(this.graph.length).fill(-1);
      parentNode[source] = source;
      const queue = [source];
      while (queue.length) {
        const u = queue.shift();
        for (let ei = 0; ei < this.graph[u].length; ei += 1) {
          const e = this.graph[u][ei];
          if (e.cap > 0 && parentNode[e.to] === -1) {
            parentNode[e.to] = u;
            parentEdge[e.to] = ei;
            queue.push(e.to);
          }
        }
      }
      if (parentNode[sink] === -1) break;
      let add = Infinity;
      for (let v = sink; v !== source; v = parentNode[v]) {
        const u = parentNode[v];
        add = Math.min(add, this.graph[u][parentEdge[v]].cap);
      }
      for (let v = sink; v !== source; v = parentNode[v]) {
        const u = parentNode[v];
        const e = this.graph[u][parentEdge[v]];
        e.cap -= add;
        this.graph[v][e.reverseIndex].cap += add;
      }
      flow += add;
    }
    return flow;
  }
}

/**
 * pieces: 实物片，需含 id 和 matchOptionsBySlot? 这里传入 canMatch(piece, slotDef)
 * template: 每盒槽位定义
 * quantity: 要求盒数；null 时求最大完整盒数
 */
export function planCompleteBoxes(pieces, template, quantity, findMatch) {
  const slotCount = template.length;
  const wantsMaximum = quantity === undefined || quantity === null;
  const upper = wantsMaximum ? Math.floor(pieces.length / slotCount) : Number(quantity);

  if (!wantsMaximum) {
    const exact = evaluateBoxCount(upper, pieces, template, findMatch);
    return exact
      ? { feasible: true, requestedBoxes: upper, boxCount: upper, assignments: exact, maxBoxes: upper }
      : { feasible: false, requestedBoxes: upper, boxCount: 0, assignments: [], maxBoxes: 0 };
  }

  for (let boxCount = upper; boxCount > 0; boxCount -= 1) {
    const assignments = evaluateBoxCount(boxCount, pieces, template, findMatch);
    if (assignments) return { feasible: true, boxCount, assignments, maxBoxes: boxCount };
  }
  return { feasible: true, boxCount: 0, assignments: [], maxBoxes: 0 };
}

function evaluateBoxCount(boxCount, pieces, template, findMatch) {
  if (boxCount <= 0) return [];
  const slotInstances = [];
  for (let b = 0; b < boxCount; b += 1) {
    template.forEach((slot, j) => slotInstances.push({ boxIndex: b, slotIndex: j, slot }));
  }

  const source = 0;
  const pieceBase = 1;
  const slotBase = pieceBase + pieces.length;
  const sink = slotBase + slotInstances.length;
  const mf = new MaxFlow(sink + 1);

  pieces.forEach((_, i) => mf.addEdge(source, pieceBase + i, 1));
  const usedEdges = [];
  slotInstances.forEach((instance, si) => {
    const slotNode = slotBase + si;
    mf.addEdge(slotNode, sink, 1);
    pieces.forEach((piece, pi) => {
      const match = findMatch(piece, instance.slot);
      if (match) {
        const edge = mf.addEdge(pieceBase + pi, slotNode, 1);
        usedEdges.push({ edge, pi, si, match });
      }
    });
  });

  const flow = mf.maxFlow(source, sink);
  if (flow !== slotInstances.length) return null;

  const assignments = [];
  for (const used of usedEdges) {
    // initialCap=1 and residual cap=0 means this edge carries one unit.
    if (used.edge.initialCap === 1 && used.edge.cap === 0) {
      const instance = slotInstances[used.si];
      assignments.push({
        boxIndex: instance.boxIndex,
        slotIndex: instance.slotIndex,
        pieceIndex: used.pi,
        pieceId: pieces[used.pi].id,
        match: used.match
      });
    }
  }
  assignments.sort((a, b) => a.boxIndex - b.boxIndex || a.slotIndex - b.slotIndex);
  return assignments.length === slotInstances.length ? assignments : null;
}
