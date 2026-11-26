const EPS = 1e-9;

const clean = (n) => Math.round(n * 1e9) / 1e9;

export function mergeIntervals(intervals) {
  const sorted = intervals
    .map(([a, b]) => [Number(a), Number(b)])
    .filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b) && b - a > EPS)
    .sort((x, y) => x[0] - y[0] || x[1] - y[1]);

  const out = [];
  for (const [a, b] of sorted) {
    const last = out[out.length - 1];
    if (last && a <= last[1] + EPS) {
      last[1] = Math.max(last[1], b);
    } else {
      out.push([a, b]);
    }
  }
  return out.map(([a, b]) => [clean(a), clean(b)]);
}

export function subtractIntervals(source, cover) {
  let remaining = mergeIntervals(source);
  const covers = mergeIntervals(cover);

  for (const [c, d] of covers) {
    const next = [];
    for (const [a, b] of remaining) {
      if (d <= a + EPS || c >= b - EPS) {
        next.push([a, b]);
      } else {
        if (c > a + EPS) next.push([a, Math.min(c, b)]);
        if (d < b - EPS) next.push([Math.max(d, a), b]);
      }
    }
    remaining = mergeIntervals(next);
  }
  return remaining;
}

export function clipInterval(a, b, lo, hi) {
  const from = Math.max(a, lo);
  const to = Math.min(b, hi);
  return to - from > EPS ? [clean(from), clean(to)] : null;
}

export function intervalLength([a, b]) {
  return clean(b - a);
}

export function formatInterval([a, b]) {
  return `[${clean(a)}, ${clean(b)})`;
}

export function meterCells(fromM, toM) {
  const cells = [];
  for (let m = Math.floor(fromM + EPS); m < Math.ceil(toM - EPS); m += 1) {
    const lo = Math.max(m, fromM);
    const hi = Math.min(m + 1, toM);
    if (hi - lo > EPS) {
      cells.push({
        meterNo: m + 1,
        fromM: clean(lo),
        toM: clean(hi),
        label: `${clean(lo)}–${clean(hi)} 米`,
      });
    }
  }
  return cells;
}

export { EPS };
