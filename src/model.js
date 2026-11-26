import crypto from 'node:crypto';

export class HttpError extends Error {
  constructor(status, code, message, details = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function uid(prefix = 'id') {
  return `${prefix}_${Date.now().toString(36)}${crypto.randomBytes(5).toString('hex')}`;
}

export function clone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

const COLOR_ALIASES = {
  r: 'red', red: 'red', rouge: 'red', 红: 'red', 红色: 'red', 红线: 'red',
  b: 'blue', blue: 'blue', 蓝: 'blue', 蓝色: 'blue',
  g: 'green', green: 'green', 绿: 'green', 绿色: 'green',
  y: 'yellow', yellow: 'yellow', 黄: 'yellow', 黄色: 'yellow',
  p: 'purple', purple: 'purple', 紫: 'purple',
  k: 'black', black: 'black', 黑: 'black',
  w: 'white', white: 'white', 白: 'white'
};

export function canonicalColor(color) {
  if (color === undefined || color === null) return 'red';
  const key = String(color).trim().toLowerCase();
  return COLOR_ALIASES[key] || COLOR_ALIASES[String(color).trim()] || String(color);
}

export function pointKey(p) {
  return p ? `${p.row},${p.col}` : '';
}

export function samePoint(a, b) {
  return !!a && !!b && a.row === b.row && a.col === b.col;
}

function isPointLike(v) {
  if (v === null || v === undefined) return false;
  if (typeof v === 'number') return true;
  if (Array.isArray(v)) return v.length >= 2 && v.every((x) => typeof x === 'number');
  if (typeof v === 'object') {
    return ['row', 'r', 'y'].some((k) => k in v) && ['col', 'column', 'c', 'x'].some((k) => k in v);
  }
  return false;
}

export function normPoint(p, coordOrder = 'rc') {
  if (p === null || p === undefined) return null;
  if (typeof p === 'number') return { row: p, col: 0 };
  if (Array.isArray(p)) {
    if (coordOrder === 'xy') return { row: p[1], col: p[0] };
    return { row: p[0], col: p[1] };
  }
  if (typeof p === 'object') {
    let row = p.row ?? p.r ?? p.y;
    let col = p.col ?? p.column ?? p.c ?? p.x;
    if (coordOrder === 'xy' && p.x !== undefined && p.y !== undefined) {
      col = p.x;
      row = p.y;
    }
    if ((row === undefined || row === null) && (col === undefined || col === null)) return null;
    if (row === undefined || row === null || col === undefined || col === null) {
      return { row: row === undefined || row === null ? undefined : Number(row), col: col === undefined || col === null ? undefined : Number(col) };
    }
    return { row: Number(row), col: Number(col) };
  }
  return null;
}

export function normPoints(value, coordOrder = 'rc') {
  if (!value) return [];
  if (!Array.isArray(value)) return [];
  // A point itself can be [r,c], so distinguish point array from path array.
  if (isPointLike(value) && typeof value[0] === 'number') return [normPoint(value, coordOrder)];
  return value.map((p) => normPoint(p, coordOrder)).filter(Boolean);
}

function asArray(value) {
  if (value === undefined || value === null) return [];
  return Array.isArray(value) ? value : [value];
}

function firstKey(obj, keys) {
  for (const k of keys) if (obj && obj[k] !== undefined) return obj[k];
  return undefined;
}

export function normalizeOrientation(o) {
  if (!o) return 'original';
  if (o === true) return 'flip-v';
  const s = String(o).trim().toLowerCase();
  if (['original', 'none', 'identity', 'normal', 'up', '0', '原放', '原样', '不翻面'].includes(s)) return 'original';
  if (['flip-v', 'v', 'vertical', 'up-down', 'updown', 'ud', 'top-bottom', '上下', '上下翻面', '翻面', '竖翻'].includes(s)) return 'flip-v';
  if (['flip-h', 'h', 'horizontal', 'left-right', 'leftright', 'lr', '左右', '左右翻面', '横翻'].includes(s)) return 'flip-h';
  if (['both', 'hv', '180', 'rotate180', 'rot180', 'double'].includes(s)) return 'both';
  return s;
}

function transformPoint(p, orientation, rows, cols) {
  let { row, col } = p;
  if (orientation === 'flip-v' || orientation === 'both') row = rows - 1 - row;
  if (orientation === 'flip-h' || orientation === 'both') col = cols - 1 - col;
  return { row, col };
}

function routeKind(path, explicitKind) {
  const k = explicitKind ? String(explicitKind).toLowerCase() : '';
  if (k.includes('straight') || k.includes('直')) return 'straight';
  if (k.includes('switch') || k.includes('turn') || k.includes('cross') || k.includes('换') || k.includes('折')) return 'switch';
  if (!path.length) return 'unknown';
  const first = path[0];
  const last = path[path.length - 1];
  const allSameRow = path.every((p) => p.row === first.row);
  if (allSameRow && first.row === last.row) return 'straight';
  if (first.row !== last.row) return 'switch';
  return 'detour';
}

function normalizeRoute(value, color, idx, coordOrder) {
  let route = value;
  if (Array.isArray(value)) {
    route = { id: `route-${idx + 1}`, path: value };
  }
  if (typeof route !== 'object' || route === null) return [];

  const pathValue = firstKey(route, ['path', 'cells', 'points', 'stitches', 'sequence', 'orderedCells', 'needles', '针迹', '针路']);
  const path = normPoints(pathValue ?? route, coordOrder);
  let entry = normPoint(firstKey(route, ['entry', 'entryCell', 'start', 'startCell', 'input', '入口']), coordOrder) || (path[0] ? { ...path[0] } : null);
  let exit = normPoint(firstKey(route, ['exit', 'exitCell', 'end', 'endCell', 'output', '出口']), coordOrder) || (path.at(-1) ? { ...path.at(-1) } : null);
  if (entry && path[0]) {
    if (entry.row === undefined || entry.row === null) entry.row = path[0].row;
    if (entry.col === undefined || entry.col === null) entry.col = path[0].col;
  }
  if (exit && path.at(-1)) {
    if (exit.row === undefined || exit.row === null) exit.row = path.at(-1).row;
    if (exit.col === undefined || exit.col === null) exit.col = path.at(-1).col;
  }
  const id = String(firstKey(route, ['id', 'routeId', 'walkId', 'name', 'label']) ?? `route-${idx + 1}`);
  return [{
    id,
    name: route.name || route.label || id,
    kind: routeKind(path, firstKey(route, ['kind', 'type', 'mode', 'routeKind', '走法'])),
    color,
    path,
    entry,
    exit,
    raw: route
  }];
}

function normalizeThread(value, color, coordOrder) {
  if (value === undefined || value === null) return [];
  if (Array.isArray(value)) {
    if (value.length && isPointLike(value[0])) {
      return normalizeRoute({ id: 'route-1', path: value }, color, 0, coordOrder);
    }
    return value.flatMap((v, i) => normalizeRoute(v, color, i, coordOrder));
  }
  if (typeof value === 'object') {
    const routesValue = firstKey(value, ['routes', 'options', 'walks', 'choices', 'paths', '走法']);
    if (routesValue !== undefined) return asArray(routesValue).flatMap((v, i) => normalizeRoute(v, color, i, coordOrder));
    return normalizeRoute(value, color, 0, coordOrder);
  }
  return [];
}

function cartesian(arrays) {
  if (!arrays.length) return [{}];
  return arrays.reduce((acc, entries) => {
    const out = [];
    for (const base of acc) {
      for (const [key, value] of Object.entries(entries)) out.push({ ...base, [key]: value });
    }
    return out;
  }, [{}]);
}

export function normalizeSegment(input, index = 0, coordOrder = 'rc') {
  if (!input || typeof input !== 'object') {
    throw new HttpError(400, 'invalid_segment', `第 ${index + 1} 段不是对象`);
  }
  const id = String(firstKey(input, ['id', 'segmentId', 'key', 'name']) ?? `seg-${index + 1}`);
  const segmentIndex = Number(firstKey(input, ['index', 'segmentIndex', 'order']) ?? index);
  const rows = Number(firstKey(input, ['rows', 'rowCount', 'height', '行']) ?? 4);
  let cols = Number(firstKey(input, ['cols', 'columns', 'colCount', 'width', '列']) ?? 0);

  const threadSource = firstKey(input, ['threads', 'colors', 'lines', 'yarns', '线', '颜色']) ?? {};
  const threadEntries = Array.isArray(threadSource)
    ? threadSource.map((t) => [canonicalColor(t.color ?? t.id ?? t.name), t])
    : Object.entries(threadSource).map(([c, v]) => [canonicalColor(c), v]);

  const threads = {};
  for (const [rawColor, source] of threadEntries) {
    const color = canonicalColor(rawColor);
    threads[color] = normalizeThread(source, color, coordOrder);
  }

  // Convenience: one-color `path` / `routes` on the segment itself.
  if (!Object.keys(threads).length) {
    const direct = firstKey(input, ['path', 'cells', 'stitches', 'routes', 'paths', 'walks', 'needleDiagram']);
    if (direct !== undefined) threads.red = normalizeThread(direct, 'red', coordOrder);
  }

  for (const routes of Object.values(threads)) {
    for (const route of routes) {
      for (const p of route.path) cols = Math.max(cols, p.col + 1);
    }
  }
  if (!cols) cols = Number(firstKey(input, ['cols', 'columns', 'width']) ?? 1);

  if (!Number.isFinite(rows) || rows < 1 || rows > 4) {
    throw new HttpError(400, 'invalid_rows', `样段 ${id} 必须为 1 至 4 行`, { rows });
  }
  if (!Number.isFinite(cols) || cols < 1) {
    throw new HttpError(400, 'invalid_cols', `样段 ${id} 无法确定列数`, { cols });
  }

  let orientations = firstKey(input, ['allowedOrientations', 'orientations', 'flips', 'allowedFlips', 'transforms', '允许翻面']);
  if (orientations === undefined) {
    orientations = input.allowFlip || input.flippable || input.canFlip || input.flipable ? ['original', 'flip-v'] : ['original'];
  }
  if (!Array.isArray(orientations)) orientations = [orientations];
  orientations = [...new Set(orientations.map(normalizeOrientation))];

  // Explicit whole-segment walks. Most requests use per-color routes; this also works.
  const explicitWalks = asArray(firstKey(input, ['walks', 'routings', 'routeOptions', 'needleOptions']))
    .map((w, wi) => {
      const wid = String(w.id ?? w.walkId ?? w.name ?? `walk-${wi + 1}`);
      const pathSource = w.paths ?? w.threads ?? w.routes ?? (w.path ? { [canonicalColor(w.color)]: w.path } : null);
      const routeByColor = {};
      if (pathSource && typeof pathSource === 'object') {
        for (const [c, v] of Object.entries(pathSource)) {
          const color = canonicalColor(c);
          const routes = normalizeThread(v, color, coordOrder);
          // If an ID reference was supplied and normalization cannot resolve it, resolve below.
          routeByColor[color] = routes[0];
          if (typeof v === 'string') routeByColor[color] = { __ref: v, color };
        }
      }
      return { id: wid, name: w.name || wid, routeByColor };
    });

  let baseOptions;
  if (explicitWalks.length) {
    baseOptions = explicitWalks.map((w) => {
      const routes = {};
      for (const [color, maybeRoute] of Object.entries(w.routeByColor)) {
        if (maybeRoute?.__ref) {
          const found = threads[color]?.find((r) => r.id === maybeRoute.__ref);
          if (!found) throw new HttpError(400, 'unknown_route', `走法 ${w.id} 引用了不存在的路线 ${maybeRoute.__ref}`);
          routes[color] = found;
        } else {
          routes[color] = maybeRoute;
        }
      }
      return { walkId: w.id, walkName: w.name, routes };
    });
  } else {
    const colors = Object.keys(threads);
    const combos = cartesian(colors.map((color) => Object.fromEntries(
      threads[color].map((route) => [route.id, { color, route }])
    )));
    baseOptions = combos.map((combo, i) => {
      const routes = {};
      const ids = [];
      for (const [routeId, pair] of Object.entries(combo)) {
        routes[pair.color] = pair.route;
        ids.push(routeId);
      }
      const walkId = ids.join('+') || `walk-${i + 1}`;
      return { walkId, walkName: walkId, routes };
    });
  }

  const options = [];
  for (const base of baseOptions) {
    for (const orientation of orientations) {
      const paths = {};
      const entries = {};
      const exits = {};
      const kinds = {};
      for (const [color, route] of Object.entries(base.routes)) {
        const path = route.path.map((p) => transformPoint(p, orientation, rows, cols));
        const entry = route.entry ? transformPoint(route.entry, orientation, rows, cols) : path[0];
        const exit = route.exit ? transformPoint(route.exit, orientation, rows, cols) : path[path.length - 1];
        paths[color] = path;
        entries[color] = entry;
        exits[color] = exit;
        kinds[color] = routeKind(path, route.kind);
      }
      const key = `${base.walkId}|${orientation}`;
      const routeIds = Object.fromEntries(Object.entries(base.routes).map(([color, route]) => [color, route.id]));
      options.push({
        key,
        walkId: base.walkId,
        walkName: base.walkName,
        routeIds,
        orientation,
        paths,
        entries,
        exits,
        kinds,
        labels: { ...(base.labels || {}) }
      });
    }
  }

  return {
    id,
    index: Number.isFinite(segmentIndex) ? segmentIndex : index,
    name: input.name || input.label || id,
    rows,
    cols,
    orientations,
    threads,
    options,
    raw: input
  };
}

export function normalizeEndpointMap(value, coordOrder = 'rc') {
  if (!value) return {};
  if (Array.isArray(value)) {
    const out = {};
    for (const item of value) {
      if (item && typeof item === 'object' && ('color' in item)) {
        out[canonicalColor(item.color)] = normPoint(item.cell ?? item.point ?? item, coordOrder);
      }
    }
    return out;
  }
  if (typeof value === 'number') return { '*': { row: value } };
  if (isPointLike(value)) return { '*': normPoint(value, coordOrder) };
  const out = {};
  for (const [color, v] of Object.entries(value)) {
    if (typeof v === 'number') out[canonicalColor(color)] = { row: v };
    else if (isPointLike(v)) out[canonicalColor(color)] = normPoint(v, coordOrder);
    else if (v && typeof v === 'object') out[canonicalColor(color)] = normPoint(v, coordOrder);
  }
  return out;
}

export function normalizeConstraintPoints(value, coordOrder = 'rc') {
  return asArray(value).map((item) => {
    if (item && typeof item === 'object' && !Array.isArray(item) && ('cell' in item || 'point' in item || 'row' in item || 'r' in item)) {
      const p = normPoint(item.cell ?? item.point ?? item, coordOrder);
      return { ...p, color: item.color ? canonicalColor(item.color) : undefined, segmentIndex: item.segmentIndex ?? item.segment ?? item.seg };
    }
    return normPoint(item, coordOrder);
  }).filter((p) => p && Number.isFinite(p.row) && Number.isFinite(p.col));
}

export function normalizeOrder(input = {}) {
  const coordOrder = input.coordOrder || input.coordinates || 'rc';
  const segmentsInput = firstKey(input, ['segments', 'samples', 'sections', '样段']) ?? [];
  const segments = asArray(segmentsInput).map((s, i) => normalizeSegment(s, i, coordOrder));
  segments.sort((a, b) => a.index - b.index);
  segments.forEach((s, i) => { s.index = i; });

  const rows = Number(input.rows ?? input.rowCount ?? input.height ?? Math.max(1, ...segments.map((s) => s.rows), 1));
  if (rows > 4) throw new HttpError(400, 'too_many_rows', '窄样带至多四行');

  const width = segments.reduce((x, s) => x + s.cols, 0);
  let offset = 0;
  for (const s of segments) {
    s.offset = offset;
    offset += s.cols;
  }

  const left = normalizeEndpointMap(firstKey(input, ['left', 'leftEnd', 'leftEndPoint', 'leftEndpoint', 'start', 'startEnd', 'entry', 'entries', '左端']), coordOrder);
  const right = normalizeEndpointMap(firstKey(input, ['right', 'rightEnd', 'rightEndPoint', 'rightEndpoint', 'end', 'finish', 'exit', 'exits', '右端']), coordOrder);
  const forbidden = normalizeConstraintPoints(firstKey(input, ['forbidden', 'blanks', 'blankCells', 'negativeSpace', 'blocked', '留白', '禁针']), coordOrder);
  const marks = normalizeConstraintPoints(firstKey(input, ['marks', 'emblems', 'badges', 'requiredCells', 'mustPass', '徽记']), coordOrder);

  // Scoped constraints stay on the point object; global planner uses global coordinates.
  return {
    id: input.id ? String(input.id) : uid('order'),
    name: input.name || input.title || '未命名绣单',
    mode: input.mode === 'relay' ? 'relay' : 'parallel',
    coordOrder,
    rows,
    width: Number(input.width ?? width),
    left,
    right,
    forbidden,
    marks,
    primaryColor: canonicalColor(input.primaryColor ?? input.color ?? 'red'),
    allowInfeasibleDraft: !!input.allowInfeasibleDraft,
    createdAt: new Date().toISOString()
  };
}

export function globalizePoint(segment, p) {
  return { row: p.row, col: segment.offset + p.col };
}

export function optionGlobalPaths(segment, option) {
  const out = {};
  for (const [color, path] of Object.entries(option.paths)) {
    out[color] = path.map((p) => globalizePoint(segment, p));
  }
  return out;
}

function pathViolations(segment, option, order) {
  const violations = [];
  const inGrid = (p) => p.row >= 0 && p.row < segment.rows && p.col >= 0 && p.col < segment.cols;
  for (const [color, path] of Object.entries(option.paths)) {
    if (!path.length) violations.push({ code: 'empty_path', segment: segment.id, color });
    if (path.length && option.entries[color] && !samePoint(option.entries[color], path[0])) {
      violations.push({ code: 'entry_not_path_start', segment: segment.id, color, entry: option.entries[color], first: path[0] });
    }
    if (path.length && option.exits[color] && !samePoint(option.exits[color], path.at(-1))) {
      violations.push({ code: 'exit_not_path_end', segment: segment.id, color, exit: option.exits[color], last: path.at(-1) });
    }
    for (const p of path) {
      if (!inGrid(p)) violations.push({ code: 'out_of_grid', segment: segment.id, color, point: p });
    }
    for (let i = 1; i < path.length; i++) {
      const a = path[i - 1];
      const b = path[i];
      const adjacent = Math.abs(a.row - b.row) + Math.abs(a.col - b.col) === 1;
      if (!adjacent) violations.push({ code: 'not_adjacent', segment: segment.id, color, from: a, to: b });
    }
  }

  const forbiddenKeys = new Set(order.forbidden.map(pointKey));
  for (const [color, path] of Object.entries(optionGlobalPaths(segment, option))) {
    for (const p of path) {
      if (forbiddenKeys.has(pointKey(p))) violations.push({ code: 'forbidden_cell', segment: segment.id, color, point: p });
    }
  }
  // Segment-local forbidden cells.
  const localForbidden = order.forbidden.filter((p) => p.segmentIndex === segment.index || p.segmentIndex === segment.id);
  const localKeys = new Set(localForbidden.map((p) => pointKey({ row: p.row, col: p.col - (typeof p.segmentIndex === 'number' ? segment.offset : 0) })));
  // The above global/local ambiguity is handled again in planner; keep simple direct local fields on segment.
  for (const f of segment.forbidden || []) {
    const key = pointKey(normPoint(f, order.coordOrder));
    for (const [color, path] of Object.entries(option.paths)) {
      if (path.some((p) => pointKey(p) === key)) violations.push({ code: 'segment_forbidden_cell', segment: segment.id, color, point: key });
    }
  }
  return violations;
}

function endpointMatches(endpoint, p, side) {
  if (!endpoint || !p) return true;
  if (endpoint.row !== undefined && endpoint.row !== p.row) return false;
  if (endpoint.col !== undefined) {
    // Global endpoint columns are compared by planner; local endpoint may be undefined col.
    if (endpoint.col !== p.col && endpoint.__globalCol !== p.col) return false;
  }
  return true;
}

function routeTokenMatches(option, token) {
  const wanted = String(token).toLowerCase();
  if (['straight', '直走', '直'].includes(wanted)) return Object.values(option.kinds).every((k) => k === 'straight');
  if (['switch', 'turn', 'cross', '换行', '折行'].includes(wanted)) return Object.values(option.kinds).some((k) => k === 'switch');
  return Object.entries(option.routeIds || {}).some(([color, id]) => {
    return id === token || String(id).toLowerCase() === wanted ||
      String(id).toLowerCase().includes(wanted) ||
      option.walkId?.toLowerCase().includes(wanted);
  });
}

function candidateAllowed(candidate, lock, constraints = {}) {
  if (constraints.optionKey && candidate.key !== constraints.optionKey) return false;
  if (constraints.walkId && !routeTokenMatches(candidate, constraints.walkId) && candidate.walkId !== constraints.walkId) return false;
  if (constraints.orientation && candidate.orientation !== normalizeOrientation(constraints.orientation)) return false;
  if (Array.isArray(constraints.allowedOptionKeys) && constraints.allowedOptionKeys.length && !constraints.allowedOptionKeys.includes(candidate.key)) return false;
  if (Array.isArray(constraints.allowedWalkIds) && constraints.allowedWalkIds.length &&
      !constraints.allowedWalkIds.some((w) => candidate.walkId === w || routeTokenMatches(candidate, w))) return false;
  if (Array.isArray(constraints.allowedOrientations) && constraints.allowedOrientations.length && !constraints.allowedOrientations.map(normalizeOrientation).includes(candidate.orientation)) return false;
  if (constraints.straightOnly) {
    for (const kind of Object.values(candidate.kinds)) if (kind !== 'straight') return false;
  }
  if (Array.isArray(constraints.allowedKinds) && constraints.allowedKinds.length) {
    const allowed = constraints.allowedKinds.map((k) => String(k).toLowerCase());
    for (const kind of Object.values(candidate.kinds)) {
      const aliases = kind === 'straight' ? ['straight', '直', '直走'] : [kind, '换行', 'switch'];
      if (!allowed.some((a) => aliases.includes(a) || aliases.some((x) => x.includes(a)))) return false;
    }
  }
  if (constraints.allowedRouteIds) {
    const allowed = [].concat(constraints.allowedRouteIds);
    if (allowed.length) {
      if (typeof allowed[0] === 'object') {
        for (const [color, id] of Object.entries(allowed[0])) {
          if (candidate.routeIds?.[canonicalColor(color)] !== id) return false;
        }
      } else {
        // Flat route tokens: every thread's selected route must satisfy at least one token.
        for (const [color, id] of Object.entries(candidate.routeIds || {})) {
          const ok = allowed.some((token) => id === token || routeTokenMatches(candidate, token));
          if (!ok) return false;
        }
      }
    }
  }
  if (constraints.routeIds) {
    for (const [color, id] of Object.entries(constraints.routeIds)) {
      if (candidate.routeIds?.[canonicalColor(color)] !== id && !routeTokenMatches(candidate, id)) return false;
    }
  }
  if (lock) {
    if (lock.exactPathsByColor) {
      for (const [color, exact] of Object.entries(lock.exactPathsByColor)) {
        const path = candidate.paths[color] || [];
        if (path.length !== exact.length || !exact.every((p, i) => samePoint(path[i], p))) return false;
      }
    } else if (lock.optionKey && candidate.key !== lock.optionKey) {
      return false;
    }
    if (!lock.exactPathsByColor && lock.walkId && candidate.walkId !== lock.walkId) return false;
    if (!lock.exactPathsByColor && lock.orientation && candidate.orientation !== lock.orientation) return false;
    if (lock.allowedOptionKeys?.length && !lock.allowedOptionKeys.includes(candidate.key)) return false;
    if (lock.allowedWalkIds?.length && !lock.allowedWalkIds.some((w) => candidate.walkId === w || routeTokenMatches(candidate, w))) return false;
    if (lock.straightOnly) {
      for (const kind of Object.values(candidate.kinds)) if (kind !== 'straight') return false;
    }
    if (lock.prefixByColor) {
      for (const [color, prefix] of Object.entries(lock.prefixByColor)) {
        const path = candidate.paths[color] || [];
        if (prefix.some((p, i) => !samePoint(path[i], p))) return false;
      }
    }
  }
  return true;
}

/**
 * Find a globally compatible routing plan.
 * locks: segmentIndex -> lock constraints.
 */
export function planOrder(order, segments, locks = {}, issueConstraints = {}) {
  const violations = [];
  const colorSet = new Set([
    ...Object.keys(order.left),
    ...Object.keys(order.right),
    ...segments.flatMap((s) => s.options.flatMap((o) => Object.keys(o.paths)))
  ]);
  colorSet.delete('*');
  const colors = [...colorSet];

  const markList = order.marks.map((m, i) => ({ ...m, bit: 1 << i }));
  const targetMask = markList.reduce((a, m) => a | m.bit, 0);

  // Build valid candidates per segment after local geometry and locks.
  const perSegment = segments.map((segment, si) => {
    const lock = locks[si] || locks[segment.id] || null;
    const constraints = issueConstraints[si] || issueConstraints[segment.id] || {};
    const candidates = [];
    for (const option of segment.options) {
      const localViolations = pathViolations(segment, option, order);
      if (localViolations.length) continue;
      if (!candidateAllowed(option, lock, constraints)) continue;

      const globalPaths = optionGlobalPaths(segment, option);
      let markMask = 0;
      for (const mark of markList) {
        // Segment-scoped mark.
        if (mark.segmentIndex !== undefined && mark.segmentIndex !== si && mark.segmentIndex !== segment.id) continue;
        let mp = mark;
        if (mark.segmentIndex !== undefined && typeof mark.segmentIndex !== 'number') {
          // id match; use as global? no.
        }
        for (const [color, path] of Object.entries(globalPaths)) {
          if (mark.color && canonicalColor(mark.color) !== color) continue;
          if (path.some((p) => samePoint(p, mark))) {
            markMask |= mark.bit;
            break;
          }
        }
        // Segment-local marks supplied with segmentIndex are compared after offset above only if caller gave global col.
        // Also try local coordinate.
        if (!(markMask & mark.bit) && mark.segmentIndex !== undefined) {
          const local = { row: mark.row, col: mark.col };
          for (const [color, path] of Object.entries(option.paths)) {
            if (mark.color && canonicalColor(mark.color) !== color) continue;
            if (path.some((p) => samePoint(p, local))) {
              markMask |= mark.bit;
              break;
            }
          }
        }
      }
      candidates.push({ segment, option, globalPaths, markMask });
    }
    if (!candidates.length) {
      violations.push({ code: 'no_candidate', segmentIndex: si, segmentId: segment.id, lock: lock ? { optionKey: lock.optionKey, reason: lock.reason } : undefined });
    }
    return candidates;
  });

  if (violations.some((v) => v.code === 'no_candidate')) {
    return { feasible: false, plan: null, violations };
  }

  // DP over segments. State includes boundary exits and covered marks.
  let states = new Map();
  for (const cand of perSegment[0]) {
    const { option, globalPaths, markMask } = cand;
    let endpointOk = true;
    for (const color of colors) {
      const explicitLeft = order.left[color];
      const wildcardLeft = order.left['*'];
      const entry = option.entries[color] ? globalizePoint(cand.segment, option.entries[color]) : globalPaths[color]?.[0];
      if (explicitLeft && !entry) endpointOk = false;
      if (entry && explicitLeft && !endpointMatches(explicitLeft, entry, 'left')) endpointOk = false;
      if (entry && !explicitLeft && wildcardLeft && !endpointMatches(wildcardLeft, entry, 'left')) endpointOk = false;
    }
    if (!endpointOk) continue;
    const exits = {};
    for (const color of colors) exits[color] = option.exits[color] ? globalizePoint(cand.segment, option.exits[color]) : globalPaths[color]?.at(-1);
    const state = { exits, mask: markMask, choices: [cand] };
    states.set(JSON.stringify({ exits, mask: markMask, key: cand.option.key }), state);
  }

  for (let si = 1; si < segments.length; si++) {
    const next = new Map();
    for (const prevState of states.values()) {
      for (const cand of perSegment[si]) {
        const { option } = cand;
        let seamOk = true;
        const entries = {};
        const exits = {};
        for (const color of colors) {
          const entry = option.entries[color] ? globalizePoint(cand.segment, option.entries[color]) : cand.globalPaths[color]?.[0];
          const exit = option.exits[color] ? globalizePoint(cand.segment, option.exits[color]) : cand.globalPaths[color]?.at(-1);
          entries[color] = entry;
          exits[color] = exit;
          const prevExit = prevState.exits[color];
          if (prevExit && !entry) {
            seamOk = false;
            violations.push({ code: 'color_missing_at_seam', between: [si - 1, si], color, message: '同一颜色不能在接缝处剪线，下一段缺少该连续线' });
          } else if (prevExit && entry && prevExit.row !== entry.row) {
            seamOk = false;
            violations.push({ code: 'seam_row_mismatch', between: [si - 1, si], color, expectedRow: prevExit.row, actualRow: entry.row });
          }
        }
        if (!seamOk) continue;

        let endpointOk = true;
        if (si === segments.length - 1) {
          for (const color of colors) {
            const explicitRight = order.right[color];
            const wildcardRight = order.right['*'];
            if (explicitRight && !exits[color]) endpointOk = false;
            if (exits[color] && explicitRight && !endpointMatches(explicitRight, exits[color], 'right')) endpointOk = false;
            if (exits[color] && !explicitRight && wildcardRight && !endpointMatches(wildcardRight, exits[color], 'right')) endpointOk = false;
          }
        }
        if (!endpointOk) continue;

        const mask = prevState.mask | cand.markMask;
        const choices = [...prevState.choices, cand];
        const state = { exits, entries, mask, choices };
        next.set(JSON.stringify({ exits, mask, keys: choices.map((c) => c.option.key) }), state);
      }
    }
    states = next;
    if (!states.size) break;
  }

  let feasibleState = null;
  for (const state of states.values()) {
    if ((state.mask & targetMask) !== targetMask) continue;
    let rightOk = true;
    for (const color of colors) {
      const explicitRight = order.right[color];
      const wildcardRight = order.right['*'];
      if (explicitRight && !state.exits[color]) rightOk = false;
      if (explicitRight && state.exits[color] && !endpointMatches(explicitRight, state.exits[color], 'right')) rightOk = false;
      if (!explicitRight && wildcardRight && state.exits[color] && !endpointMatches(wildcardRight, state.exits[color], 'right')) rightOk = false;
    }
    if (rightOk) { feasibleState = state; break; }
  }

  if (!feasibleState) {
    // Deduplicate violations and add a readable summary.
    const seen = new Set();
    const dedup = violations.filter((v) => {
      const k = JSON.stringify(v);
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    }).slice(0, 50);
    if (!dedup.length) dedup.push({ code: 'no_complete_routing', message: '没有找到同时满足端点、接缝、留白和徽记的走法' });
    if (!markList.length && segments.length === 0) dedup.push({ code: 'no_segments' });
    return { feasible: false, plan: null, violations: dedup };
  }

  const planSegments = feasibleState.choices.map((cand, i) => {
    const segment = cand.segment;
    return {
      segmentIndex: i,
      segmentId: segment.id,
      optionKey: cand.option.key,
      walkId: cand.option.walkId,
      orientation: cand.option.orientation,
      kinds: cand.option.kinds,
      entries: Object.fromEntries(Object.entries(cand.option.entries).map(([c, p]) => [c, globalizePoint(segment, p)])),
      exits: Object.fromEntries(Object.entries(cand.option.exits).map(([c, p]) => [c, globalizePoint(segment, p)])),
      localEntries: cand.option.entries,
      localExits: cand.option.exits,
      paths: cand.globalPaths,
      localPaths: cand.option.paths
    };
  });

  const seams = [];
  for (let i = 1; i < planSegments.length; i++) {
    const prev = planSegments[i - 1];
    const cur = planSegments[i];
    for (const color of colors) {
      seams.push({
        between: [i - 1, i],
        color,
        row: prev.exits[color]?.row,
        leftCell: prev.exits[color],
        rightCell: cur.entries[color],
        compatible: prev.exits[color]?.row === cur.entries[color]?.row
      });
    }
  }

  const fullPaths = {};
  for (const color of colors) {
    fullPaths[color] = planSegments.flatMap((ps) => ps.paths[color] || []);
  }

  return {
    feasible: true,
    colors,
    plan: { segments: planSegments, seams, fullPaths },
    violations: []
  };
}

export function redBandFixture() {
  // Two columns per segment keeps the canonical two-row example compact.
  const first = {
    id: 'first',
    rows: 2,
    cols: 2,
    threads: {
      red: {
        routes: [
          { id: 'first-straight', kind: 'straight', path: [[0, 0], [0, 1]] },
          // Both walks share the first cell; the bottom cell is unique to switching.
          { id: 'first-switch', kind: 'switch', path: [[0, 0], [1, 0], [1, 1]] }
        ]
      }
    }
  };
  const tail = {
    id: 'tail',
    rows: 2,
    cols: 2,
    threads: {
      red: {
        routes: [
          { id: 'tail-top-straight', kind: 'straight', path: [[0, 0], [0, 1]] },
          { id: 'tail-bottom-straight', kind: 'straight', path: [[1, 0], [1, 1]] },
          { id: 'tail-down', kind: 'switch', path: [[0, 0], [0, 1], [1, 1]] },
          { id: 'tail-up', kind: 'switch', path: [[1, 0], [0, 0], [0, 1]] }
        ]
      }
    }
  };
  return { first, tail };
}

export function straightTailOnlyChange() {
  return {
    segmentIndex: 1,
    segmentId: 'tail',
    // Existing tail design remains; only these route kinds are permitted after change.
    allowedKinds: ['straight'],
    straightOnly: true,
    note: '未领尾段改为只能直走'
  };
}
