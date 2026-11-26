'use strict';

/**
 * Domain model for the co-listening service.
 *
 * Vocabulary
 * ----------
 * - episode : the canonical programme. An ordered `flow` of *main* segments
 *             (正文, e.g. 甲/乙/丙) with stable `segmentId`s and durations.
 *             The same segmentId may appear more than once in the flow; each
 *             appearance is a distinct *occurrence*.
 * - seam    : the boundary between two consecutive main occurrences. Guides
 *             (导听) are inserted at seams. Seam keys are stable:
 *               - "intro"        before the first main occurrence
 *               - "seam:<i>"     between flow[i] and flow[i+1]
 *               - "outro"        after the last main occurrence
 * - manifest: a member's subscribed *version* of the episode. It is the
 *             episode flow with the member's own guides injected at seams.
 *             Guides differ per member in length (duration) and in whether
 *             they are mandatory (mustListen).
 * - item    : one entry of a manifest, addressed by a stable `idx`
 *             (出现次序). Main items carry {segmentId, occurrence}; guide
 *             items carry {guideId, slotId, mustListen}.
 * - played  : the intervals a player actually rendered, reported per item.
 *             Dragging (seeking) over a range does NOT add it to played.
 *             Progress is the union of played intervals, so a drag that
 *             skips content leaves a hole that is *missing*.
 *
 * All time is "logical seconds" measured inside a manifest version.
 */

const EPS = 1e-6;

function isNum(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

/** Read a numeric field that may be spelled `from/to` or `start/end`. */
function readInterval(iv) {
  const from = iv.from != null ? iv.from : iv.start;
  const to = iv.to != null ? iv.to : iv.end;
  return { from, to };
}

/**
 * Clip to [0, limit], drop empties, sort and merge overlapping/adjacent
 * intervals. Returns an array of {from, to} with from < to.
 */
function normalizeIntervals(intervals, limit) {
  const arr = [];
  for (const raw of intervals || []) {
    if (!raw) continue;
    const { from, to } = readInterval(raw);
    if (!isNum(from) || !isNum(to)) continue;
    let s = Math.max(0, from);
    let e = limit != null ? Math.min(limit, to) : to;
    if (!(e > s + EPS)) continue;
    arr.push([s, e]);
  }
  arr.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged = [];
  for (const [s, e] of arr) {
    const last = merged[merged.length - 1];
    if (last && s <= last[1] + EPS) {
      if (e > last[1]) last[1] = e;
    } else {
      merged.push([s, e]);
    }
  }
  return merged.map(([from, to]) => ({ from, to }));
}

/** Total covered length. */
function unionLength(intervals, limit) {
  const m = normalizeIntervals(intervals, limit);
  let sum = 0;
  for (const iv of m) sum += iv.to - iv.from;
  return round(sum);
}

/**
 * Length of the contiguous run of coverage that starts at 0. Used for
 * "playback position": how far the listener has heard without a gap.
 */
function coveredPrefix(intervals, limit) {
  const m = normalizeIntervals(intervals, limit);
  let pos = 0;
  for (const iv of m) {
    if (iv.from > pos + EPS) break;
    if (iv.to > pos) pos = iv.to;
  }
  return round(Math.min(pos, limit == null ? pos : limit));
}

/** Complement of the played intervals within [0, limit]. */
function holes(intervals, limit) {
  const m = normalizeIntervals(intervals, limit);
  const out = [];
  let pos = 0;
  for (const iv of m) {
    if (iv.from > pos + EPS) out.push({ from: round(pos), to: round(iv.from) });
    if (iv.to > pos) pos = iv.to;
  }
  if (limit != null && limit > pos + EPS) out.push({ from: round(pos), to: round(limit) });
  return out;
}

function round(n) {
  // logical seconds are decimals-friendly; keep a tiny epsilon clean-up
  if (Math.abs(n - Math.round(n)) < EPS) return Math.round(n);
  return Math.round(n * 1e6) / 1e6;
}

/** Union of two interval sets (both raw). */
function unionIntervals(a, b, limit) {
  return normalizeIntervals([...(a || []), ...(b || [])], limit);
}

// ---------------------------------------------------------------------------
// Manifest construction
// ---------------------------------------------------------------------------

function guideItem(slotId, guide) {
  return {
    kind: 'guide',
    slotId,
    guideId: guide.guideId != null ? guide.guideId : slotId,
    duration: guide.duration,
    mustListen: !!guide.mustListen,
  };
}

/**
 * Build the ordered item list for a member's manifest version.
 *
 * @param {object} episode     { id?, flow: [{segmentId, duration}] }
 * @param {object} assignments { [seamKey]: {guideId?, duration, mustListen?} }
 * @returns {{items: object[], duration: number, assignments: object}}
 */
function buildManifest(episode, assignments) {
  const a = assignments || {};
  const items = [];
  const occCount = {};
  let t = 0;

  const push = (it) => {
    it.idx = items.length;
    it.logicalStart = t;
    it.logicalEnd = t + it.duration;
    t += it.duration;
    items.push(it);
  };

  if (a.intro) push(guideItem('intro', a.intro));

  episode.flow.forEach((seg, i) => {
    if (i > 0) {
      const key = `seam:${i - 1}`;
      if (a[key]) push(guideItem(key, a[key]));
    }
    const occurrence = (occCount[seg.segmentId] = (occCount[seg.segmentId] || 0) + 1);
    push({
      kind: 'main',
      segmentId: seg.segmentId,
      occurrence,
      duration: seg.duration,
    });
  });

  if (a.outro) push(guideItem('outro', a.outro));

  return { items, duration: round(t), assignments: a };
}

/** Find a main item by the (segmentId, occurrence) pair that a point binds to. */
function findMainItem(items, segmentId, occurrence) {
  return items.find(
    (it) => it.kind === 'main' && it.segmentId === segmentId && it.occurrence === (occurrence || 1)
  );
}

/**
 * Convert a simple "played continuously from 0 up to `upto` logical seconds"
 * description into per-item intervals. Convenience for clients/scripts/tests.
 */
function logicalPrefixToIntervals(items, upto) {
  const out = [];
  for (const it of items) {
    if (upto <= it.logicalStart + EPS) break;
    const to = Math.min(upto, it.logicalEnd) - it.logicalStart;
    if (to > EPS) out.push({ idx: it.idx, from: 0, to: round(to) });
  }
  return out;
}

/**
 * Build the "played intervals" for a continuous run [from, to) in logical
 * seconds, split across item boundaries.
 */
function logicalRangeToIntervals(items, fromLogical, toLogical) {
  const out = [];
  for (const it of items) {
    const s = Math.max(fromLogical, it.logicalStart);
    const e = Math.min(toLogical, it.logicalEnd);
    if (e > s + EPS) {
      out.push({
        idx: it.idx,
        from: round(s - it.logicalStart),
        to: round(e - it.logicalStart),
      });
    }
  }
  return out;
}

module.exports = {
  EPS,
  normalizeIntervals,
  unionLength,
  coveredPrefix,
  holes,
  unionIntervals,
  buildManifest,
  findMainItem,
  logicalPrefixToIntervals,
  logicalRangeToIntervals,
  round,
};
