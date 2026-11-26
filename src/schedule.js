// Eligibility checks and contract planning (pair-visits and mutual-aid rings).
//
// A directed edge host->guest is feasible on a set of nights when:
//   * nights are consecutive, within the host's hostable dates and the guest's
//     away dates, and satisfy both sides' min/max consecutive-night rules;
//   * the host has consented to that travelling combination;
//   * a whole family is never simultaneously away and receiving guests.
// A "complete pair-visit" needs both directions; a "complete ring" needs every
// edge of the 3-4 household cycle.

import {
  sortNights, consecutiveRuns, uniq, badRequest,
} from './util.js';

const MAX_CANDIDATES = 5000;

export function consentCovers(hostApp, guestHouseholds) {
  if (hostApp.acceptedCompanions == null) return true; // unspecified => wildcard
  const set = new Set(guestHouseholds);
  return hostApp.acceptedCompanions.some((combo) => {
    const c = new Set(combo);
    return [...set].every((g) => c.has(g));
  });
}

// Minimum / maximum nights acceptable to a directed visit.
function visitBounds(hostApp, guestApp) {
  const min = Math.max(hostApp.minConsecutiveNights ?? 1, guestApp.minConsecutiveNights ?? 1);
  const maxes = [];
  if (hostApp.maxNights != null) maxes.push(hostApp.maxNights);
  if (guestApp.maxNights != null) maxes.push(guestApp.maxNights);
  const max = maxes.length ? Math.min(...maxes) : Infinity;
  return { min, max };
}

// Check one directed edge against two applications. Returns {ok, reasons}.
export function checkEdge(hostApp, guestApps, nights) {
  const reasons = [];
  const guestIds = guestApps.map((a) => a.householdId);
  const ns = sortNights(nights);
  if (ns.length === 0) reasons.push('edge has no nights');
  const runs = consecutiveRuns(ns);
  if (runs.length > 1) reasons.push('nights are not consecutive');

  const { min, max } = visitBounds(hostApp, guestApps[0]);
  if (ns.length && ns.length < min) reasons.push(`visit shorter than min consecutive (${min})`);
  if (ns.length && ns.length > max) reasons.push(`visit longer than max (${max})`);

  if (!consentCovers(hostApp, guestIds)) {
    reasons.push(`host ${hostApp.householdId} has not accepted travelling combination [${guestIds.join(',')}]`);
  }

  const hostSet = new Set(hostApp.hostNights);
  for (const n of ns) {
    if (!hostSet.has(n)) reasons.push(`host ${hostApp.householdId} cannot host on ${n}`);
  }
  for (const ga of guestApps) {
    const gTravel = new Set(ga.travelNights);
    for (const n of ns) {
      if (!gTravel.has(n)) reasons.push(`guest ${ga.householdId} cannot travel on ${n}`);
    }
  }
  return { ok: reasons.length === 0, reasons: uniq(reasons) };
}

// Enumerate feasible consecutive blocks for one directed edge, bounded by
// min/max and the overlap of host's hostable dates with guest travel dates.
export function feasibleBlocks(hostApp, guestApps) {
  if (!consentCovers(hostApp, guestApps.map((a) => a.householdId))) return [];
  const { min, max } = visitBounds(hostApp, guestApps[0]);
  let usable = hostApp.hostNights.slice();
  for (const ga of guestApps) {
    const trav = new Set(ga.travelNights);
    usable = usable.filter((n) => trav.has(n));
  }
  const blocks = [];
  for (const run of consecutiveRuns(usable)) {
    const upper = Math.min(run.length, max === Infinity ? run.length : max);
    for (let len = min; len <= upper; len += 1) {
      for (let i = 0; i + len <= run.length; i += 1) {
        blocks.push(run.slice(i, i + len));
      }
    }
  }
  // dedupe
  const seen = new Set();
  return blocks.filter((b) => {
    const k = b.join('|');
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

function usedNightsByHousehold(edges) {
  const m = new Map();
  const add = (h, nights) => {
    if (!m.has(h)) m.set(h, new Set());
    for (const n of nights) m.get(h).add(n);
  };
  for (const e of edges) {
    add(e.host, e.nights);
    for (const g of e.guestHouseholds) add(g, e.nights);
  }
  return m;
}

function contractFeasible(edges, apps = null) {
  // A household cannot host and be away on the same night *as a whole family*.
  // (The rule from the spec: if the entire family is out, the home cannot at
  // the same time be claimed as receiving guests. Partial parties may overlap.)
  const per = new Map(); // h -> Map(night -> role)
  const wholeFamily = (h) => (apps?.get?.(h)?.wholeFamily ?? apps?.[h]?.wholeFamily ?? false);
  for (const e of edges) {
    for (const n of e.nights) {
      const roles = per.get(e.host) ?? new Map();
      if (wholeFamily(e.host) && roles.get(n) === 'guest') return false;
      roles.set(n, 'host');
      per.set(e.host, roles);
      for (const g of e.guestHouseholds) {
        const gr = per.get(g) ?? new Map();
        if (wholeFamily(g) && gr.get(n) === 'host') return false;
        gr.set(n, 'guest');
        per.set(g, gr);
      }
    }
  }
  return true;
}

function appOf(store, id) {
  return store.state.applications[id];
}

// Enumerate complete pair contracts for the given households.
export function pairCandidates(store, householdIds) {
  const out = [];
  const ids = householdIds.filter((id) => appOf(store, id));
  for (let i = 0; i < ids.length; i += 1) {
    for (let j = i + 1; j < ids.length; j += 1) {
      const a = ids[i];
      const b = ids[j];
      const appA = appOf(store, a);
      const appB = appOf(store, b);
      const apps = { [a]: appA, [b]: appB };
      const ab = feasibleBlocks(appA, [appB]); // A hosts B
      const ba = feasibleBlocks(appB, [appA]); // B hosts A
      for (const nAB of ab) {
        for (const nBA of ba) {
          const edges = [
            { host: a, guest: b, guestHouseholds: [b], nights: nAB },
            { host: b, guest: a, guestHouseholds: [a], nights: nBA },
          ];
          if (!contractFeasible(edges, apps)) continue;
          out.push({ type: 'pair', households: [a, b], edges });
          if (out.length >= MAX_CANDIDATES) return out;
        }
      }
    }
  }
  return out;
}

function permutationsCycle(ids) {
  // distinct directed cycles up to rotation, fixed first element to cut rotations
  const [first, ...rest] = ids;
  const results = [];
  const permute = (arr, acc) => {
    if (arr.length === 0) { results.push([first, ...acc]); return; }
    for (let i = 0; i < arr.length; i += 1) {
      const next = arr.slice();
      const [x] = next.splice(i, 1);
      permute(next, [...acc, x]);
    }
  };
  permute(rest, []);
  // dedupe reversal duplicates (cycle direction matters for host->guest, so only dedupe exact)
  const seen = new Set();
  return results.filter((c) => {
    const k = c.join('>');
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

// Enumerate complete ring contracts (A hosts B, B hosts C, C hosts A, ...).
export function ringCandidates(store, householdIds) {
  const ids = householdIds.filter((id) => appOf(store, id));
  const out = [];
  for (let size = 3; size <= 4; size += 1) {
    for (const subset of combinations(ids, size)) {
      for (const cycle of permutationsCycle(subset)) {
        const edgeOptions = [];
        for (let k = 0; k < cycle.length; k += 1) {
          const host = cycle[k];
          const guest = cycle[(k + 1) % cycle.length];
          const blocks = feasibleBlocks(appOf(store, host), [appOf(store, guest)]);
          if (blocks.length === 0) { edgeOptions.length = 0; break; }
          edgeOptions.push({ host, guest, blocks });
        }
        if (edgeOptions.length === 0) continue;
        const apps = {};
        for (const h of cycle) apps[h] = appOf(store, h);
        // cartesian product of edge blocks with per-household disjointness
        const build = (idx, acc) => {
          if (idx === edgeOptions.length) {
            if (contractFeasible(acc, apps)) {
              out.push({
                type: 'ring',
                households: cycle.slice(),
                edges: acc.map((e) => ({ ...e })),
              });
              if (out.length >= MAX_CANDIDATES) return true;
            }
            return false;
          }
          const opt = edgeOptions[idx];
          for (const nights of opt.blocks) {
            const cand = [...acc, { host: opt.host, guest: opt.guest, guestHouseholds: [opt.guest], nights }];
            if (!contractFeasible(cand, apps)) continue;
            if (build(idx + 1, cand)) return true;
          }
          return false;
        };
        build(0, []);
        if (out.length >= MAX_CANDIDATES) return out;
      }
    }
  }
  return out;
}

function combinations(arr, k) {
  const res = [];
  const go = (start, acc) => {
    if (acc.length === k) { res.push(acc.slice()); return; }
    for (let i = start; i < arr.length; i += 1) { acc.push(arr[i]); go(i + 1, acc); acc.pop(); }
  };
  go(0, []);
  return res;
}

function signature(c) {
  return `${c.type}:${c.households.join(',')}:` +
    c.edges.map((e) => `${e.host}>${e.guest}=${e.nights.join('|')}`).sort().join(';');
}

// Greedy-free exact set packing: choose as many complete contracts as possible
// such that no household is used in two contracts on an overlapping night.
// Among packings of equal size, prefer ones matching stated host preferences
// and visit order (tie-break only; never sacrifices contract count).
export function packContracts(candidates, apps = null) {
  // dedupe
  const seen = new Set();
  const cands = [];
  for (const c of candidates) {
    const s = signature(c);
    if (seen.has(s)) continue;
    seen.add(s);
    cands.push({ ...c, _used: usedNightsByHousehold(c.edges) });
  }

  const preferenceScore = (packing) => {
    if (!apps) return 0;
    let score = 0;
    for (const c of packing) {
      for (const e of c.edges) {
        const guestApp = apps[e.guest];
        const hostApp = apps[e.host];
        if (guestApp?.preferredHosts?.includes(e.host)) score += 2;
        if (Array.isArray(guestApp?.visitOrder) && guestApp.visitOrder.includes(e.host)) {
          score += 1 + (guestApp.visitOrder.length - guestApp.visitOrder.indexOf(e.host));
        }
        if (hostApp?.preferredHosts?.includes(e.guest)) score += 1;
      }
    }
    return score;
  };

  // deterministic: prefer more nights, then stable signature order
  cands.sort((x, y) => {
    const ny = y.edges.reduce((s, e) => s + e.nights.length, 0);
    const nx = x.edges.reduce((s, e) => s + e.nights.length, 0);
    if (ny !== nx) return ny - nx;
    return signature(x).localeCompare(signature(y));
  });

  let best = [];
  let bestScore = -1;
  const chosen = [];

  const conflicts = (a, b) => {
    for (const [h, nset] of a._used) {
      const other = b._used.get(h);
      if (!other) continue;
      for (const n of nset) if (other.has(n)) return true;
    }
    return false;
  };

  const dfs = (idx) => {
    if (idx === cands.length) {
      const score = preferenceScore(chosen);
      if (chosen.length > best.length ||
          (chosen.length === best.length && score > bestScore)) {
        best = chosen.slice();
        bestScore = score;
      }
      return;
    }
    if (chosen.length + (cands.length - idx) < best.length) return; // bound
    const c = cands[idx];
    let ok = true;
    for (const s of chosen) if (conflicts(s, c)) { ok = false; break; }
    if (ok) {
      chosen.push(c);
      dfs(idx + 1);
      chosen.pop();
    }
    dfs(idx + 1);
  };
  dfs(0);
  return best.map(({ _used, ...c }) => c);
}

export function planContracts(store, { type = 'pair', households } = {}) {
  if (!Array.isArray(households) || households.length < 2) {
    throw badRequest('planning requires at least two households');
  }
  let candidates;
  if (type === 'pair') candidates = pairCandidates(store, households);
  else if (type === 'ring') candidates = ringCandidates(store, households);
  else throw badRequest(`unknown contract type ${type}`);
  const apps = Object.fromEntries(
    households.map((h) => [h, store.state.applications[h]]).filter(([, a]) => a),
  );
  const packed = packContracts(candidates, apps);
  return { type, households, count: packed.length, contracts: packed };
}
