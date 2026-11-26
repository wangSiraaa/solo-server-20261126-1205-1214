// Half-open interval helpers. All scenario times are invented seconds: [start, end).

export function toInterval(value, maybeEnd) {
  let start;
  let end;

  if (arguments.length === 2 && value !== null && maybeEnd !== undefined) {
    start = value;
    end = maybeEnd;
  } else if (Array.isArray(value)) {
    [start, end] = value;
  } else if (value && typeof value === 'object') {
    start = value.start ?? value.from ?? value.startSecond ?? value.begin;
    end = value.end ?? value.to ?? value.endSecond ?? value.finish;
  } else if (typeof value === 'string' && value.includes(',')) {
    [start, end] = value.split(',').map((x) => x.trim());
  }

  start = Number(start);
  end = Number(end);

  if (!Number.isFinite(start) || !Number.isFinite(end)) {
    const err = new Error(`区间无法解析: ${JSON.stringify(value)}`);
    err.code = 'INVALID_INTERVAL';
    throw err;
  }
  if (end < start) {
    const err = new Error(`区间终点不能早于起点: [${start}, ${end})`);
    err.code = 'INVALID_INTERVAL';
    throw err;
  }
  return { start, end };
}

export function intervalSeconds(iv) {
  if (!iv) return 0;
  return Math.max(0, Number(iv.end) - Number(iv.start));
}

export function overlapInterval(a, b) {
  if (!a || !b) return null;
  const start = Math.max(a.start, b.start);
  const end = Math.min(a.end, b.end);
  return start < end ? { start, end } : null;
}

export function clipInterval(iv, bounds) {
  return overlapInterval(iv, bounds);
}

export function mergeIntervals(intervals) {
  const sorted = (intervals || [])
    .map((iv) => ({ start: Number(iv.start), end: Number(iv.end) }))
    .filter((iv) => Number.isFinite(iv.start) && Number.isFinite(iv.end) && iv.end > iv.start)
    .sort((a, b) => a.start - b.start || a.end - b.end);

  const merged = [];
  for (const iv of sorted) {
    const last = merged[merged.length - 1];
    if (!last || iv.start > last.end) {
      merged.push({ ...iv });
    } else {
      last.end = Math.max(last.end, iv.end);
    }
  }
  return merged;
}

// Remove dropped-frame holes from a nominal exposure. Holes are mapped to the
// same clock before this is called.
export function subtractHoles(nominal, holes = []) {
  if (!nominal || nominal.end <= nominal.start) return [];
  let pieces = [{ ...nominal }];
  const sortedHoles = mergeIntervals((holes || []).map((hole) => toInterval(hole)));

  for (const hole of sortedHoles) {
    const next = [];
    for (const piece of pieces) {
      const cut = overlapInterval(piece, hole);
      if (!cut) {
        next.push(piece);
        continue;
      }
      if (piece.start < cut.start) {
        next.push({ start: piece.start, end: Math.min(piece.end, cut.start) });
      }
      if (cut.end < piece.end) {
        next.push({ start: Math.max(piece.start, cut.end), end: piece.end });
      }
    }
    pieces = next;
  }
  return mergeIntervals(pieces);
}

function intersectTwoUnions(a, b) {
  const left = mergeIntervals(a);
  const right = mergeIntervals(b);
  let i = 0;
  let j = 0;
  const out = [];

  while (i < left.length && j < right.length) {
    const iv = overlapInterval(left[i], right[j]);
    if (iv) out.push(iv);

    if (left[i].end < right[j].end) i += 1;
    else if (left[i].end > right[j].end) j += 1;
    else {
      i += 1;
      j += 1;
    }
  }
  return mergeIntervals(out);
}

export function intersectUnions(...unions) {
  if (unions.length === 0) return [];
  if (unions.some((u) => !u || u.length === 0)) return [];
  return unions.reduce((acc, union) => intersectTwoUnions(acc, union));
}

export function sumSeconds(intervals) {
  return mergeIntervals(intervals).reduce((sum, iv) => sum + intervalSeconds(iv), 0);
}

export function mapLocalInterval(iv, agreedOffset) {
  const offset = Number(agreedOffset || 0);
  return { start: iv.start + offset, end: iv.end + offset };
}

// Distance from a background calibration to an exposure piece.
// Negative/overlapping cases are reported rather than silently accepted.
export function backgroundRelation(background, exposure) {
  if (background.end <= exposure.start) {
    return {
      side: 'before',
      distance: exposure.start - background.end,
      interval: background,
    };
  }
  if (background.start >= exposure.end) {
    return {
      side: 'after',
      distance: background.start - exposure.end,
      interval: background,
    };
  }
  return {
    side: 'overlap',
    distance: 0,
    interval: background,
    overlap: overlapInterval(background, exposure),
  };
}

export function parseBackgroundPolicy(input = {}) {
  if (input === null) return null;
  if (typeof input === 'number') {
    return { before: input, after: input };
  }

  const source = input.background ?? input.backgroundPolicy ?? input;
  const radius = source.maxDistance ?? source.radius ?? source.backgroundRadiusSeconds
    ?? source.maxBackgroundDistance;
  const before = source.before ?? source.maxBefore ?? source.beforeSeconds
    ?? source.backgroundBeforeSeconds ?? source.beforeDistance ?? radius;
  const after = source.after ?? source.maxAfter ?? source.afterSeconds
    ?? source.backgroundAfterSeconds ?? source.afterDistance ?? radius;

  const policy = {};
  if (before !== undefined && before !== null) policy.before = Number(before);
  if (after !== undefined && after !== null) policy.after = Number(after);
  return policy;
}

export function isBackgroundEligible(background, exposure, policy) {
  const relation = backgroundRelation(background, exposure);
  if (relation.side === 'overlap') {
    return { eligible: false, relation, reason: 'BACKGROUND_OVERLAPS_EXPOSURE' };
  }
  if (!policy || (policy.before === undefined && policy.after === undefined)) {
    return { eligible: true, relation, reason: 'BACKGROUND_POLICY_ANY' };
  }
  const limit = relation.side === 'before' ? policy.before : policy.after;
  if (limit === undefined || limit === null || !Number.isFinite(limit)) {
    return { eligible: false, relation, reason: `NO_${relation.side.toUpperCase()}_POLICY` };
  }
  if (relation.distance <= limit) {
    return { eligible: true, relation, reason: 'WITHIN_DISTANCE' };
  }
  return { eligible: false, relation, reason: 'TOO_FAR' };
}
