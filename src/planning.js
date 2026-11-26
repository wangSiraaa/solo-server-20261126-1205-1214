import { ApiError } from './errors.js';
import { mustFind, complaintView } from './core.js';
import { EPS } from './intervals.js';

function legalOutgoing(db, nodeId, incomingLinkId, blocked) {
  const incoming = incomingLinkId ? db.links.find((l) => l.id === incomingLinkId) : null;
  const node = mustFind(db.nodes, nodeId, '路口');
  return db.links.filter((link) => {
    if (link.fromNodeId !== nodeId || blocked.has(link.id)) return false;
    if (incoming && link.id === incoming.reverseLinkId && node.uTurnAllowed !== true) return false;
    return true;
  });
}

export function shortestPath(db, startNodeId, targetNodeId, options = {}) {
  mustFind(db.nodes, startNodeId, '起点');
  mustFind(db.nodes, targetNodeId, '终点');
  const blocked = new Set(options.blockedLinkIds ?? []);
  const startIncoming = options.incomingLinkId ?? null;
  const startKey = `${startNodeId}::${startIncoming ?? ''}`;
  const dist = new Map([[startKey, 0]]);
  const prev = new Map();
  const visited = new Set();

  while (true) {
    let currentKey = null;
    let currentDist = Infinity;
    for (const [key, d] of dist) {
      if (!visited.has(key) && d < currentDist) {
        currentDist = d;
        currentKey = key;
      }
    }
    if (!currentKey) break;
    visited.add(currentKey);
    const [nodeId, incomingPart] = currentKey.split('::');
    const incomingLinkId = incomingPart || null;
    if (nodeId === targetNodeId) {
      const linkIds = [];
      let key = currentKey;
      while (prev.has(key)) {
        const p = prev.get(key);
        linkIds.unshift(p.linkId);
        key = p.prevKey;
      }
      return { distance: currentDist, linkIds };
    }

    for (const link of legalOutgoing(db, nodeId, incomingLinkId, blocked)) {
      const nextKey = `${link.toNodeId}::${link.id}`;
      const nd = currentDist + link.lengthM;
      if (nd + EPS < (dist.get(nextKey) ?? Infinity)) {
        dist.set(nextKey, nd);
        prev.set(nextKey, { linkId: link.id, prevKey: currentKey });
      }
    }
  }
  return null;
}

function outstandingFromTrip(db, trip) {
  // On-demand trips can scope to complaints; otherwise all open complaint meters.
  const ids = trip.complaintIds?.length
    ? trip.complaintIds
    : db.complaints.filter((c) => c.status !== 'resolved').map((c) => c.id);
  const out = [];
  for (const id of ids) {
    const complaint = db.complaints.find((c) => c.id === id);
    if (!complaint) continue;
    const view = complaintView(db, complaint);
    for (const claim of view.claims) {
      for (const interval of claim.missingIntervals) {
        out.push({
          complaintId: complaint.id,
          claimId: claim.id,
          rightId: claim.rightId,
          curbId: claim.curbId,
          fromM: interval[0],
          toM: interval[1],
        });
      }
    }
  }
  return out;
}

export function planNextLink(db, tripId, payload = {}) {
  const trip = mustFind(db.trips, tripId, '趟次');
  if (trip.mode !== 'on_demand') throw new ApiError(409, 'NOT_ON_DEMAND', '该趟是固定整圈模式，路线已冻结');
  const currentNodeId = payload.currentNodeId ?? trip.currentNodeId;
  if (!currentNodeId) throw new ApiError(400, 'NO_REPORTED_POSITION', '服务端只能使用车头已回报的当前路口');
  mustFind(db.nodes, currentNodeId, '当前路口');

  const blockedLinkIds = payload.blockedLinkIds ?? [];
  const blockedSet = new Set(blockedLinkIds);
  const candidates = [];
  for (const item of outstandingFromTrip(db, trip)) {
    for (const link of db.links.filter((l) => l.rightCurbId === item.curbId && !blockedSet.has(l.id))) {
      const overlapFrom = Math.max(item.fromM, link.sourceFromM);
      const overlapTo = Math.min(item.toM, link.sourceToM);
      if (overlapTo - overlapFrom <= EPS) continue;
      const path = shortestPath(db, currentNodeId, link.fromNodeId, {
        blockedLinkIds,
        incomingLinkId: payload.arrivedViaLinkId ?? null,
      });
      if (!path) continue;
      candidates.push({ item, link, overlapFrom, overlapTo, path });
    }
  }

  if (!candidates.length) {
    return { done: true, nextLinkId: null, reason: '没有可规划的欠段，或封路/掉头限制导致无合法路线' };
  }
  candidates.sort((a, b) => a.path.distance + a.link.lengthM - (b.path.distance + b.link.lengthM));
  const chosen = candidates[0];
  const approachLinkId = chosen.path.linkIds[0] ?? null;
  const atServiceStart = currentNodeId === chosen.link.fromNodeId && approachLinkId === null;
  const nextLinkId = approachLinkId ?? chosen.link.id;
  const nextLink = mustFind(db.links, nextLinkId, '下一段');

  let brush = null;
  let instruction = 'deadhead';
  if (nextLinkId === chosen.link.id) {
    instruction = 'sweep';
    if (chosen.link.direction === 'ref') {
      brush = { brushDownM: chosen.overlapFrom - chosen.link.sourceFromM, brushUpM: chosen.overlapTo - chosen.link.sourceFromM };
    } else {
      brush = { brushDownM: chosen.link.sourceToM - chosen.overlapTo, brushUpM: chosen.link.sourceToM - chosen.overlapFrom };
    }
  }

  return {
    done: false,
    instruction,
    nextLinkId,
    nextLink,
    brush,
    approachPathLinkIds: chosen.path.linkIds,
    targetLinkId: chosen.link.id,
    outstanding: chosen.item,
    note: instruction === 'sweep' ? '沿此方向行驶时右侧路缘对应欠段，按下刷/收刷米数作业' : '此段为空驶接近段，刷頭抬起；到达后再次请求下一段',
  };
}
