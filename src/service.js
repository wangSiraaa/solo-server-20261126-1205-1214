'use strict';

const {
  EPS,
  buildManifest,
  findMainItem,
  normalizeIntervals,
  unionLength,
  coveredPrefix,
  holes,
  round,
} = require('./model');

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

class ServiceError extends Error {
  constructor(status, code, message, extra) {
    super(message || code);
    this.status = status;
    this.code = code;
    this.extra = extra || {};
  }
}

const err = {
  notFound: (m, e) => new ServiceError(404, 'NOT_FOUND', m, e),
  badRequest: (m, e) => new ServiceError(400, 'BAD_REQUEST', m, e),
  conflict: (m, e) => new ServiceError(409, m, m, e),
  forbidden: (m, e) => new ServiceError(403, m, m, e),
};

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function idFactory() {
  let n = 0;
  return (prefix) => `${prefix}_${++n}`;
}

const nextId = idFactory();

function clone(v) {
  return structuredClone(v);
}

/** Stable identity of a guide *instance* (content). Replacing the guide with a
 * different duration / mandatory flag / id yields a different key, so old
 * coverage of the previous guide cannot masquerade as coverage of the new one. */
function guideKey(it) {
  return `${it.slotId}::${it.guideId}::${it.duration}::${it.mustListen ? 1 : 0}`;
}

function segKey(segmentId, occurrence) {
  return `${segmentId}#${occurrence || 1}`;
}

function clampItemIntervals(rawIntervals, duration) {
  return normalizeIntervals(rawIntervals, duration);
}

// ---------------------------------------------------------------------------
// Service
// ---------------------------------------------------------------------------

class CoListeningService {
  constructor(options = {}) {
    this.rooms = new Map();
    this.now = options.now || (() => new Date().toISOString());
    this.clock = options.clock || (() => Date.now());
  }

  // ---- room lifecycle -----------------------------------------------------

  createRoom({ roomId, flow, rule, host, members, assignmentsByMember } = {}) {
    if (!roomId) throw err.badRequest('roomId required');
    if (this.rooms.has(roomId)) throw err.conflict('ROOM_EXISTS', { roomId });
    if (!Array.isArray(flow) || flow.length === 0) throw err.badRequest('flow required');
    if (rule && rule !== 'sync' && rule !== 'early') throw err.badRequest('rule must be sync|early');

    const seen = {};
    const normalizedFlow = flow.map((seg, i) => {
      if (!seg || typeof seg.segmentId !== 'string' || !(seg.duration > 0)) {
        throw err.badRequest(`flow[${i}] needs segmentId and positive duration`);
      }
      seen[seg.segmentId] = (seen[seg.segmentId] || 0) + 1;
      return { segmentId: seg.segmentId, duration: seg.duration };
    });

    const room = {
      id: roomId,
      flow: normalizedFlow,
      rule: rule || 'sync',
      hostMemberId: null,
      authorityEpoch: 1,
      members: new Map(),
      discussion: {
        pointId: null,
        point: null, // {segmentId, occurrence}
        status: 'unset', // unset | closed | open
        round: { id: null, memberIds: [] },
        nextRound: { id: null, memberIds: [] },
        resumes: {},
        openedAt: null,
        closedAt: null,
        openCount: 0,
      },
      events: [],
      permits: [],
      reports: [],
      createdAt: this.now(),
    };
    this.rooms.set(roomId, room);

    const hostSpec = host || (members && members[0]);
    if (!hostSpec || !hostSpec.id) throw err.badRequest('host required');
    this._addMember(room, hostSpec, (assignmentsByMember || {})[hostSpec.id]);
    room.hostMemberId = hostSpec.id;
    this._event(room, 'room_created', { hostMemberId: hostSpec.id, rule: room.rule });

    for (const m of members || []) {
      if (m.id === hostSpec.id) continue;
      this._addMember(room, m, (assignmentsByMember || {})[m.id]);
    }
    return this.roomSummary(room.id);
  }

  _addMember(room, spec, assignments) {
    if (!spec || !spec.id) throw err.badRequest('member id required');
    if (room.members.has(spec.id)) throw err.conflict('MEMBER_EXISTS', { memberId: spec.id });
    const member = {
      id: spec.id,
      name: spec.name || spec.id,
      joinedAt: this.now(),
      currentVersion: 1,
      versions: {},
      reports: [],
      connections: new Map(),
      permits: [],
      prelisten: { furthest: 0, version: 1 },
      nextRound: false,
    };
    this._installVersion(room, member, 1, assignments || {});
    room.members.set(spec.id, member);
    this._event(room, 'member_joined', { memberId: spec.id, version: member.currentVersion });
    return member;
  }

  _installVersion(room, member, version, assignments) {
    const built = buildManifest({ flow: room.flow }, assignments);
    member.versions[version] = {
      version,
      items: built.items,
      duration: built.duration,
      assignments: clone(assignments),
      builtAt: this.now(),
    };
    return member.versions[version];
  }

  _room(roomId) {
    const room = this.rooms.get(roomId);
    if (!room) throw err.notFound(`room ${roomId} not found`);
    return room;
  }

  _member(room, memberId) {
    const member = room.members.get(memberId);
    if (!member) throw err.notFound(`member ${memberId} not found`);
    return member;
  }

  _layout(member, version) {
    const v = member.versions[version == null ? member.currentVersion : version];
    if (!v) throw err.badRequest(`manifest version ${version} unknown for ${member.id}`);
    return v;
  }

  _event(room, type, details) {
    const e = {
      seq: room.events.length + 1,
      at: this.now(),
      type,
      ...details,
    };
    room.events.push(e);
    return e;
  }

  // ---- membership / connections ------------------------------------------

  join({ roomId, memberId, name, assignments }) {
    const room = this._room(roomId);
    const existing = room.members.get(memberId);
    if (existing) {
      // Leave-and-rejoin: keep the member's progress, reports and manifest
      // version; just mark them back as present. New members are still not
      // auto-added to any existing round.
      existing.away = false;
      if (name) existing.name = name;
      if (assignments) {
        const nextVersion = existing.currentVersion + 1;
        this._installVersion(room, existing, nextVersion, assignments);
        existing.currentVersion = nextVersion;
      }
      this._event(room, 'member_rejoined', { memberId, version: existing.currentVersion });
      this._reevaluatePermits(room);
      return {
        member: this._memberView(room, existing),
        discussion: this._discussionView(room, memberId),
        rejoined: true,
        note: 'previous progress, reports and permits were preserved',
      };
    }
    const member = this._addMember(room, { id: memberId, name }, assignments);
    // New members are intentionally NOT auto-added to an existing round.
    this._reevaluatePermits(room);
    return {
      member: this._memberView(room, member),
      discussion: this._discussionView(room, memberId),
      note: room.discussion.round.memberIds.length
        ? 'not part of the current round unless the host adds you'
        : 'room has no discussion round yet',
    };
  }

  /** Mark a member as away: close their live connections but keep all state. */
  leave({ roomId, memberId }) {
    const room = this._room(roomId);
    const member = this._member(room, memberId);
    const closed = [];
    for (const conn of member.connections.values()) {
      if (conn.alive) {
        conn.alive = false;
        conn.disconnectedAt = this.now();
        closed.push(conn.connId);
      }
    }
    member.away = true;
    this._event(room, 'member_left', { memberId, closedConnections: closed });
    return { memberId, away: true, closedConnections: closed };
  }

  connect({ roomId, memberId, connId }) {
    const room = this._room(roomId);
    const member = this._member(room, memberId);
    const id = connId || nextId('conn');
    const existing = member.connections.get(id);
    if (existing) {
      if (existing.alive) {
        // idempotent reconnect
      } else {
        // Re-open the same connection id (player reconnecting).
        existing.alive = true;
        existing.disconnectedAt = null;
        existing.reopenedAt = this.now();
        existing.reopenCount = (existing.reopenCount || 0) + 1;
        this._event(room, 'connection_reopened', { memberId, connId: id });
      }
    } else {
      member.connections.set(id, {
        connId: id,
        connectedAt: this.now(),
        disconnectedAt: null,
        alive: true,
        reopenCount: 0,
      });
      this._event(room, 'connection_opened', { memberId, connId: id });
    }
    const snap = this.snapshot({ roomId, memberId, connId: id });
    snap.reconnected = !!existing;
    return snap;
  }

  disconnect({ roomId, memberId, connId }) {
    const room = this._room(roomId);
    const member = this._member(room, memberId);
    const conn = member.connections.get(connId);
    if (!conn) throw err.notFound(`connection ${connId} not found`);
    if (conn.alive) {
      conn.alive = false;
      conn.disconnectedAt = this.now();
      this._event(room, 'connection_closed', { memberId, connId });
    }
    return { memberId, connId, alive: false };
  }

  _requireConnection(room, member, connId) {
    if (!connId) return null;
    const conn = member.connections.get(connId);
    if (!conn) throw err.conflict('UNKNOWN_CONNECTION', { connId });
    if (!conn.alive) throw err.conflict('CONNECTION_CLOSED', { connId });
    return conn;
  }

  snapshot({ roomId, memberId, connId }) {
    const room = this._room(roomId);
    const member = this._member(room, memberId);
    if (connId && !member.connections.has(connId)) {
      throw err.notFound(`connection ${connId} not found`);
    }
    const progress = this.progress({ roomId, memberId });
    const { effective, stale } = this._permitStatus(room, member);
    const nextRound = room.discussion.nextRound.memberIds.includes(memberId);
    const conn = connId ? member.connections.get(connId) : null;
    return {
      roomId,
      memberId,
      connId: connId || null,
      rule: room.rule,
      hostMemberId: room.hostMemberId,
      authorityEpoch: room.authorityEpoch,
      manifestVersion: member.currentVersion,
      connection: conn
        ? { connId: conn.connId, alive: conn.alive, reopenCount: conn.reopenCount || 0 }
        : null,
      progress,
      relisten: {
        position: progress.position,
        bodyPosition: progress.bodyPosition,
        missing: progress.missing,
        note: progress.missing.length
          ? 'resume re-listening at `position`; the listed items are not yet fully played'
          : 'no missing content; you are caught up on this manifest version',
      },
      // Only permits actually in force right now. Stale ones are reported
      // separately so a reconnect can never overstate what was granted.
      effectivePermits: effective,
      stalePermits: stale,
      discussion: this._discussionView(room, memberId),
      catchUp: nextRound ? this._catchUp(room, member) : null,
    };
  }

  /**
   * Split a member's permits into those truly in force now vs. those whose
   * grant no longer applies. A permit is in force iff:
   *  - its status is `granted`,
   *  - it belongs to the current discussion point and round, and
   *  - a sync `continue` permit also still satisfies the live precondition
   *    (every round member is ready).
   * This is what a reconnecting player must read back — not the raw request.
   */
  _permitStatus(room, member) {
    const effective = [];
    const stale = [];
    for (const p of member.permits) {
      if (p.status !== 'granted') continue; // waiting/denied/revoked live in `permits`
      if (p.discussionPointId && p.discussionPointId !== room.discussion.pointId) {
        stale.push({ ...clone(p), staleReason: 'POINT_CHANGED' });
        continue;
      }
      if (p.roundId && p.roundId !== room.discussion.round.id) {
        stale.push({ ...clone(p), staleReason: 'ROUND_CHANGED' });
        continue;
      }
      if (p.kind === 'continue' && room.rule === 'sync' && !this._allReady(room)) {
        stale.push({ ...clone(p), staleReason: 'ROUND_NOT_READY' });
        continue;
      }
      effective.push(clone(p));
    }
    return { effective, stale };
  }

  _effectivePermits(room, member) {
    return this._permitStatus(room, member).effective;
  }

  // ---- reports ------------------------------------------------------------

  report({ roomId, memberId, connId, manifestVersion, items, reset, reportId }) {
    const room = this._room(roomId);
    const member = this._member(room, memberId);
    this._requireConnection(room, member, connId);

    if (manifestVersion == null) throw err.badRequest('manifestVersion required');
    const layout = this._layout(member, manifestVersion);
    if (!Array.isArray(items)) throw err.badRequest('items must be an array');

    if (reset) {
      member.reports = [];
      this._event(room, 'reports_reset', { memberId, byConnId: connId || null });
    }

    const normalized = [];
    for (const raw of items) {
      if (!raw || !Number.isInteger(raw.idx)) {
        throw err.badRequest('each item needs integer idx');
      }
      const it = layout.items[raw.idx];
      if (!it) throw err.badRequest(`idx ${raw.idx} not in manifest version ${manifestVersion}`);
      const ivs = clampItemIntervals([{ from: raw.from, to: raw.to }], it.duration);
      for (const iv of ivs) normalized.push({ idx: raw.idx, from: iv.from, to: iv.to });
    }

    const rid = reportId || nextId('rep');
    const record = {
      reportId: rid,
      memberId,
      connId: connId || null,
      manifestVersion,
      items: normalized,
      receivedAt: this.now(),
      stale: manifestVersion !== member.currentVersion,
    };
    member.reports.push(record);
    room.reports.push({ roomId, ...record });

    this._event(room, 'report_accepted', {
      memberId,
      connId: connId || null,
      reportId: rid,
      manifestVersion,
      stale: record.stale,
      itemCount: normalized.length,
    });

    this._reevaluatePermits(room);

    return {
      accepted: true,
      reportId: rid,
      manifestVersion,
      currentVersion: member.currentVersion,
      // Old-version reports are still accepted, but their raw logical seconds
      // are never used as a position in the new version's timeline. Body
      // progress is derived from (segmentId, occurrence, offset), which is
      // stable across guide edits; guide coverage is version/content specific.
      stale: record.stale,
      reportedAgainst: manifestVersion,
      progress: this.progress({ roomId, memberId }),
    };
  }

  deleteReports({ roomId, memberId }) {
    const room = this._room(roomId);
    const member = this._member(room, memberId);
    const n = member.reports.length;
    member.reports = [];
    this._event(room, 'reports_reset', { memberId, removed: n });
    this._reevaluatePermits(room);
    return { memberId, removed: n };
  }

  // ---- coverage computation ----------------------------------------------

  /**
   * Aggregate a member's reports (across all manifest versions) into
   * version-independent body coverage plus content-keyed guide coverage.
   */
  _coverage(member) {
    const body = new Map(); // segKey -> intervals
    const guides = new Map(); // guideKey -> intervals
    const add = (map, key, ivs) => {
      const cur = map.get(key) || [];
      for (const iv of ivs) cur.push(iv);
      map.set(key, cur);
    };
    for (const rep of member.reports) {
      const layout = member.versions[rep.manifestVersion];
      if (!layout) continue;
      for (const play of rep.items) {
        const it = layout.items[play.idx];
        if (!it) continue;
        const ivs = [{ from: play.from, to: play.to }];
        if (it.kind === 'main') {
          add(body, segKey(it.segmentId, it.occurrence), ivs);
        } else {
          add(guides, guideKey(it), ivs);
        }
      }
    }
    const outBody = {};
    for (const [k, v] of body) outBody[k] = normalizeIntervals(v);
    const outGuides = {};
    for (const [k, v] of guides) outGuides[k] = normalizeIntervals(v);
    return { body: outBody, guides: outGuides };
  }

  _itemCoverage(cov, it) {
    if (it.kind === 'main') {
      return clampItemIntervals(cov.body[segKey(it.segmentId, it.occurrence)] || [], it.duration);
    }
    return clampItemIntervals(cov.guides[guideKey(it)] || [], it.duration);
  }

  /** Progress of a member, expressed in their *current* manifest timeline. */
  progress({ roomId, memberId }) {
    const room = this._room(roomId);
    const member = this._member(room, memberId);
    const layout = this._layout(member);
    const cov = this._coverage(member);

    const logical = [];
    const items = [];
    let furthest = 0;
    let missingMandatory = 0;
    for (const it of layout.items) {
      const ivs = this._itemCoverage(cov, it);
      for (const iv of ivs) {
        logical.push({ from: round(it.logicalStart + iv.from), to: round(it.logicalStart + iv.to) });
        if (it.logicalStart + iv.to > furthest) furthest = it.logicalStart + iv.to;
      }
      const coveredLen = unionLength(ivs, it.duration);
      const missingIvs = holes(ivs, it.duration);
      const full = coveredLen >= it.duration - EPS;
      if (!full && it.kind === 'guide' && it.mustListen) missingMandatory += 1;
      items.push({
        idx: it.idx,
        kind: it.kind,
        segmentId: it.segmentId,
        occurrence: it.occurrence,
        slotId: it.slotId,
        guideId: it.guideId,
        mustListen: it.mustListen,
        duration: it.duration,
        logicalStart: round(it.logicalStart),
        logicalEnd: round(it.logicalEnd),
        covered: round(coveredLen),
        full,
        missing: missingIvs.map((m) => ({
          from: round(m.from),
          to: round(m.to),
          logicalFrom: round(it.logicalStart + m.from),
          logicalTo: round(it.logicalStart + m.to),
        })),
      });
    }

    const totalLogical = normalizeIntervals(logical, layout.duration);
    const position = coveredPrefix(totalLogical, layout.duration);
    const missing = items
      .filter((it) => !it.full)
      .map((it) => ({
        idx: it.idx,
        kind: it.kind,
        segmentId: it.segmentId,
        occurrence: it.occurrence,
        slotId: it.slotId,
        guideId: it.guideId,
        mustListen: it.mustListen,
        intervals: it.missing,
      }));

    // Body position: first main occurrence (in canonical order) not fully heard.
    let bodyPosition = null;
    for (const it of items) {
      if (it.kind !== 'main') continue;
      if (it.full) continue;
      bodyPosition = {
        segmentId: it.segmentId,
        occurrence: it.occurrence,
        offset: coveredPrefix(this._itemCoverage(cov, it), it.duration),
        logicalStart: it.logicalStart,
        logicalEnd: it.logicalEnd,
        complete: false,
      };
      break;
    }
    if (!bodyPosition) {
      const mains = items.filter((it) => it.kind === 'main');
      const last = mains[mains.length - 1];
      bodyPosition = last
        ? {
            segmentId: last.segmentId,
            occurrence: last.occurrence,
            offset: last.duration,
            logicalStart: last.logicalStart,
            logicalEnd: last.logicalEnd,
            complete: true,
          }
        : null;
    }

    return {
      memberId,
      manifestVersion: member.currentVersion,
      totalDuration: layout.duration,
      totalCovered: unionLength(totalLogical, layout.duration),
      position,
      furthest: round(furthest),
      bodyPosition,
      missing,
      missingCount: missing.length,
      missingMandatoryGuideCount: missingMandatory,
      items,
    };
  }

  /** Whether a member has fully heard the discussion occurrence + preceding
   *  mandatory guides in their own current manifest. */
  _readiness(room, member) {
    const point = room.discussion.point;
    if (!point) return { ready: false, reason: 'NO_DISCUSSION_POINT' };
    const layout = this._layout(member);
    const bound = findMainItem(layout.items, point.segmentId, point.occurrence);
    if (!bound) {
      return { ready: false, reason: 'POINT_NOT_IN_MANIFEST' };
    }
    const cov = this._coverage(member);
    const boundCov = this._itemCoverage(cov, bound);
    const segDuration = bound.duration;
    const heard = unionLength(boundCov, segDuration);
    if (heard < segDuration - EPS) {
      return {
        ready: false,
        reason: 'SEGMENT_NOT_FULLY_HEARD',
        segmentId: point.segmentId,
        occurrence: point.occurrence,
        heard: round(heard),
        duration: segDuration,
        /** Still-missing slices of the bound occurrence, in item-relative and
         *  logical (manifest-timeline) seconds — this is the "gap" to point out. */
        missingIntervals: holes(boundCov, segDuration).map((h) => ({
          from: round(h.from),
          to: round(h.to),
          logicalFrom: round(bound.logicalStart + h.from),
          logicalTo: round(bound.logicalStart + h.to),
        })),
      };
    }
    // Mandatory guides positioned at or before the end of the bound occurrence
    // must also be fully heard ("必听").
    const missingGuides = [];
    for (const it of layout.items) {
      if (it.kind !== 'guide' || !it.mustListen) continue;
      if (it.logicalEnd > bound.logicalEnd + EPS) continue;
      const gcov = this._itemCoverage(cov, it);
      const g = unionLength(gcov, it.duration);
      if (g < it.duration - EPS) {
        missingGuides.push({
          slotId: it.slotId,
          guideId: it.guideId,
          heard: round(g),
          duration: it.duration,
          missingIntervals: holes(gcov, it.duration).map((h) => ({
            from: round(h.from),
            to: round(h.to),
            logicalFrom: round(it.logicalStart + h.from),
            logicalTo: round(it.logicalStart + h.to),
          })),
        });
      }
    }
    if (missingGuides.length) {
      return { ready: false, reason: 'MANDATORY_GUIDE_MISSING', missingGuides };
    }
    return {
      ready: true,
      segmentId: point.segmentId,
      occurrence: point.occurrence,
      duration: segDuration,
      logicalStart: round(bound.logicalStart),
      logicalEnd: round(bound.logicalEnd),
    };
  }

  // ---- manifest edits -----------------------------------------------------

  editManifest({ roomId, actorMemberId, authorityEpoch, connId, memberId, assignments, all }) {
    const room = this._room(roomId);
    this._requireHost(room, { actorMemberId, authorityEpoch, connId, action: 'editManifest' });

    const targets = [];
    if (memberId) {
      targets.push([this._member(room, memberId), assignments]);
    } else if (all) {
      if (typeof all !== 'object') throw err.badRequest('`all` must be a {memberId: assignments} map');
      for (const [mid, asg] of Object.entries(all)) targets.push([this._member(room, mid), asg]);
    } else {
      throw err.badRequest('provide memberId+assignments or all');
    }

    const results = [];
    for (const [member, asg] of targets) {
      if (!asg || typeof asg !== 'object') throw err.badRequest('assignments must be an object');
      const nextVersion = member.currentVersion + 1;
      this._installVersion(room, member, nextVersion, asg);
      const prev = member.currentVersion;
      member.currentVersion = nextVersion;
      this._event(room, 'manifest_edited', {
        memberId: member.id,
        previousVersion: prev,
        newVersion: nextVersion,
        actorMemberId,
        authorityEpoch,
      });
      results.push({ memberId: member.id, previousVersion: prev, newVersion: nextVersion });
    }
    this._reevaluatePermits(room);
    return {
      edited: results,
      note: 'old-version reports are still accepted; their raw seconds are not used as new-version progress',
    };
  }

  // ---- discussion ---------------------------------------------------------

  setDiscussionPoint({ roomId, actorMemberId, authorityEpoch, connId, segmentId, occurrence, setRoundToCurrentMembers }) {
    const room = this._room(roomId);
    this._requireHost(room, { actorMemberId, authorityEpoch, connId, action: 'setDiscussionPoint' });
    const occ = occurrence || 1;
    // Validate the point exists in the canonical flow.
    const occurrences = room.flow.filter((s) => s.segmentId === segmentId).length;
    if (occurrences === 0) throw err.badRequest(`segment ${segmentId} not in flow`);
    if (occ > occurrences) throw err.badRequest(`occurrence ${occ} out of range for ${segmentId}`);

    room.discussion.pointId = nextId('point');
    room.discussion.point = { segmentId, occurrence: occ };
    room.discussion.status = 'closed';
    room.discussion.openedAt = null;
    room.discussion.closedAt = null;
    room.discussion.resumes = {};
    if (setRoundToCurrentMembers !== false) {
      // The round is the set of listeners expected to reach the point. The
      // host is an authority role, not automatically a listener: exclude it
      // unless the host later adds itself explicitly via setRound.
      const roundMembers = [...room.members.keys()].filter((id) => id !== room.hostMemberId);
      room.discussion.round = {
        id: nextId('round'),
        memberIds: roundMembers,
      };
      room.discussion.nextRound = { id: nextId('round'), memberIds: [] };
    }
    for (const m of room.members.values()) m.nextRound = false;
    this._event(room, 'discussion_point_set', {
      pointId: room.discussion.pointId,
      segmentId,
      occurrence: occ,
      round: room.discussion.round.memberIds,
    });
    this._reevaluatePermits(room);
    return this._discussionView(room, null);
  }

  setRound({ roomId, actorMemberId, authorityEpoch, connId, memberIds }) {
    const room = this._room(roomId);
    this._requireHost(room, { actorMemberId, authorityEpoch, connId, action: 'setRound' });
    if (!Array.isArray(memberIds)) throw err.badRequest('memberIds must be an array');
    for (const mid of memberIds) this._member(room, mid); // validate
    room.discussion.round = { id: nextId('round'), memberIds: [...new Set(memberIds)] };
    for (const mid of memberIds) {
      const m = room.members.get(mid);
      m.nextRound = false;
    }
    this._event(room, 'round_set', {
      round: room.discussion.round.memberIds,
      actorMemberId,
      authorityEpoch,
    });
    this._reevaluatePermits(room);
    return this._discussionView(room, null);
  }

  /**
   * Move an away/absent member into the next round so the current round can
   * proceed, and tell them how to catch up when they return.
   */
  deferMember({ roomId, actorMemberId, authorityEpoch, connId, memberId }) {
    const room = this._room(roomId);
    this._requireHost(room, { actorMemberId, authorityEpoch, connId, action: 'deferMember' });
    const member = this._member(room, memberId);
    const idx = room.discussion.round.memberIds.indexOf(memberId);
    if (idx === -1) throw err.badRequest(`${memberId} is not in the current round`);
    room.discussion.round.memberIds.splice(idx, 1);
    if (!room.discussion.nextRound.memberIds.includes(memberId)) {
      room.discussion.nextRound.memberIds.push(memberId);
    }
    member.nextRound = true;
    const catchUp = this._catchUp(room, member);
    this._event(room, 'member_deferred', {
      memberId,
      round: room.discussion.round.memberIds,
      nextRound: room.discussion.nextRound.memberIds,
      actorMemberId,
      authorityEpoch,
    });
    this._reevaluatePermits(room);
    return {
      deferredMemberId: memberId,
      round: this._discussionView(room, null),
      catchUp,
    };
  }

  _catchUp(room, member) {
    const progress = this.progress({ roomId: room.id, memberId: member.id });
    return {
      memberId: member.id,
      // Where to resume playing when they come back.
      resumeAt: progress.position,
      bodyPosition: progress.bodyPosition,
      // What they still owe, in item terms.
      missing: progress.missing,
      missingMandatoryGuideCount: progress.missingMandatoryGuideCount,
      note: 'on return, re-listen from resumeAt; the missing items below have not been fully played',
    };
  }

  openDiscussion({ roomId, actorMemberId, authorityEpoch, connId }) {
    const room = this._room(roomId);
    this._requireHost(room, { actorMemberId, authorityEpoch, connId, action: 'openDiscussion' });
    if (!room.discussion.point) throw err.conflict('NO_DISCUSSION_POINT');
    if (room.discussion.status === 'open') throw err.conflict('DISCUSSION_ALREADY_OPEN');

    const roundIds = room.discussion.round.memberIds.filter((id) => room.members.has(id));
    const waitingOn = [];
    for (const mid of roundIds) {
      const member = room.members.get(mid);
      const r = this._readiness(room, member);
      if (!r.ready) waitingOn.push({ memberId: mid, ...r });
    }
    if (waitingOn.length) {
      this._event(room, 'discussion_open_blocked', {
        waitingOn: waitingOn.map((w) => w.memberId),
        actorMemberId,
        authorityEpoch,
      });
      throw err.conflict('DISCUSSION_NOT_READY', {
        waitingOn,
        round: roundIds,
        message: 'discussion cannot open until every member in the round has heard the bound occurrence',
      });
    }

    // All ready. For early-rule listeners who have run ahead, work out the
    // return-to-discussion directive and preserve their pre-listen position.
    const resumes = {};
    for (const mid of roundIds) {
      const member = room.members.get(mid);
      const layout = this._layout(member);
      const bound = findMainItem(layout.items, room.discussion.point.segmentId, room.discussion.point.occurrence);
      const progress = this.progress({ roomId: room.id, memberId: mid });
      const ranAhead = progress.furthest > bound.logicalEnd + EPS;
      const directive = {
        memberId: mid,
        returnTo: round(bound.logicalStart),
        returnToSegment: { segmentId: bound.segmentId, occurrence: bound.occurrence },
        resumeAfterDiscussion: ranAhead ? progress.furthest : null,
        ranAhead,
      };
      resumes[mid] = directive;
      if (ranAhead) {
        member.prelisten = { furthest: progress.furthest, version: member.currentVersion };
      }
    }
    room.discussion.status = 'open';
    room.discussion.openedAt = this.now();
    room.discussion.openCount += 1;
    room.discussion.resumes = resumes;
    this._event(room, 'discussion_opened', {
      point: room.discussion.point,
      round: roundIds,
      directs: resumes,
      actorMemberId,
      authorityEpoch,
    });
    return this._discussionView(room, null);
  }

  endDiscussion({ roomId, actorMemberId, authorityEpoch, connId }) {
    const room = this._room(roomId);
    this._requireHost(room, { actorMemberId, authorityEpoch, connId, action: 'endDiscussion' });
    if (room.discussion.status !== 'open') throw err.conflict('DISCUSSION_NOT_OPEN');
    room.discussion.status = 'closed';
    room.discussion.closedAt = this.now();
    const restores = {};
    for (const [mid, d] of Object.entries(room.discussion.resumes)) {
      restores[mid] = {
        memberId: mid,
        // After the discussion, resume the pre-listen position if the listener
        // had run ahead; otherwise stay at the discussion body.
        resumeAt: d.resumeAfterDiscussion != null ? d.resumeAfterDiscussion : d.returnTo,
        prelistened: d.ranAhead,
        followFrom: d.returnTo,
      };
    }
    this._event(room, 'discussion_closed', {
      round: room.discussion.round.memberIds,
      restores,
      actorMemberId,
      authorityEpoch,
    });
    return { discussion: this._discussionView(room, null), restores };
  }

  discussionState({ roomId, memberId }) {
    const room = this._room(roomId);
    return this._discussionView(room, memberId || null);
  }

  /**
   * Host-facing query: for each member of the current round, who is still
   * missing what before the discussion can open. Works for a newly added
   * third member too (they show up as waiting).
   */
  whoMissing({ roomId, memberId }) {
    const room = this._room(roomId);
    const scope = memberId ? [memberId] : room.discussion.round.memberIds;
    const out = scope.map((mid) => {
      const member = this._member(room, mid);
      const ready = room.discussion.point ? this._readiness(room, member) : { ready: false, reason: 'NO_DISCUSSION_POINT' };
      return { memberId: mid, ready, inRound: room.discussion.round.memberIds.includes(mid) };
    });
    return {
      point: room.discussion.point,
      status: room.discussion.status,
      round: room.discussion.round.memberIds,
      nextRound: [...room.discussion.nextRound.memberIds],
      waitingOn: out.filter((o) => !o.ready.ready).map((o) => o.memberId),
      members: out,
    };
  }

  _discussionView(room, memberId) {
    const d = room.discussion;
    const roundIds = d.round.memberIds.filter((id) => room.members.has(id));
    const readiness = roundIds.map((mid) => {
      const member = room.members.get(mid);
      return { memberId: mid, ...(d.point ? this._readiness(room, member) : { ready: false, reason: 'NO_DISCUSSION_POINT' }) };
    });
    const view = {
      pointId: d.pointId,
      point: d.point,
      status: d.status,
      roundId: d.round.id,
      round: roundIds,
      nextRound: [...d.nextRound.memberIds],
      readiness,
      waitingOn: readiness.filter((r) => !r.ready).map((r) => ({ memberId: r.memberId, reason: r.reason })),
      allReady: !!d.point && readiness.every((r) => r.ready),
      openCount: d.openCount,
      openedAt: d.openedAt,
      closedAt: d.closedAt,
    };
    if (memberId && d.resumes[memberId]) {
      view.yourDirective = d.resumes[memberId];
    }
    return view;
  }

  // ---- permits ------------------------------------------------------------

  requestPermit({ roomId, memberId, connId, kind }) {
    const room = this._room(roomId);
    const member = this._member(room, memberId);
    this._requireConnection(room, member, connId);
    if (kind !== 'continue' && kind !== 'prelisten') {
      throw err.badRequest('kind must be continue|prelisten');
    }
    const permit = {
      permitId: nextId('permit'),
      memberId,
      connId: connId || null,
      kind,
      rule: room.rule,
      manifestVersion: member.currentVersion,
      status: 'waiting',
      issuedAt: this.now(),
      grantedAt: null,
      revokedAt: null,
      reason: null,
      authorityEpoch: room.authorityEpoch,
      discussionPointId: room.discussion.pointId,
      roundId: room.discussion.round.id,
    };
    member.permits.push(permit);
    room.permits.push(permit);

    if (kind === 'prelisten' && room.rule !== 'early') {
      permit.status = 'denied';
      permit.reason = 'RULE_SYNC_NO_PRELISTEN';
      this._event(room, 'permit_denied', { permitId: permit.permitId, memberId, kind, reason: permit.reason });
      return clone(permit);
    }
    if (room.rule === 'early') {
      // In the early rule a fast listener may go ahead immediately.
      permit.status = 'granted';
      permit.grantedAt = this.now();
      this._event(room, 'permit_granted', { permitId: permit.permitId, memberId, kind, reason: 'EARLY_RULE' });
      return clone(permit);
    }
    this._event(room, 'permit_requested', { permitId: permit.permitId, memberId, kind });
    this._reevaluatePermits(room);
    return clone(permit);
  }

  _allReady(room) {
    if (!room.discussion.point) return false;
    const roundIds = room.discussion.round.memberIds.filter((id) => room.members.has(id));
    // An empty round is vacuously ready: there is nobody to wait for.
    return roundIds.every((mid) => this._readiness(room, room.members.get(mid)).ready);
  }

  _reevaluatePermits(room) {
    if (room.rule !== 'sync') return;
    if (!room.discussion.point) return;
    const ready = this._allReady(room);
    for (const permit of room.permits) {
      if (permit.kind !== 'continue') continue;
      if (permit.discussionPointId !== room.discussion.pointId) continue;
      if (room.discussion.status === 'open') continue; // already discussed
      if (permit.status === 'granted' && !ready) {
        permit.status = 'revoked';
        permit.revokedAt = this.now();
        permit.reason = 'ROUND_NOT_READY';
        this._event(room, 'permit_revoked', {
          permitId: permit.permitId,
          memberId: permit.memberId,
          reason: permit.reason,
        });
      } else if (permit.status === 'waiting' && ready) {
        permit.status = 'granted';
        permit.grantedAt = this.now();
        permit.reason = 'ALL_ROUND_MEMBERS_READY';
        this._event(room, 'permit_granted', {
          permitId: permit.permitId,
          memberId: permit.memberId,
          kind: permit.kind,
          reason: permit.reason,
        });
      }
    }
  }

  permitsFor({ roomId, memberId }) {
    const room = this._room(roomId);
    if (memberId) {
      const member = this._member(room, memberId);
      const all = member.permits;
      const { effective, stale } = this._permitStatus(room, member);
      return { permits: clone(all), effective, stale };
    }
    const all = room.permits;
    const effective = [];
    const stale = [];
    for (const member of room.members.values()) {
      const s = this._permitStatus(room, member);
      effective.push(...s.effective);
      stale.push(...s.stale);
    }
    return { permits: clone(all), effective, stale };
  }

  // ---- authority ----------------------------------------------------------

  transferAuthority({ roomId, actorMemberId, authorityEpoch, connId, toMemberId }) {
    const room = this._room(roomId);
    this._requireHost(room, { actorMemberId, authorityEpoch, connId, action: 'transferAuthority' });
    this._member(room, toMemberId);
    if (toMemberId === room.hostMemberId) throw err.badRequest('already host');
    const previousHost = room.hostMemberId;
    const previousEpoch = room.authorityEpoch;
    room.hostMemberId = toMemberId;
    room.authorityEpoch += 1;
    this._event(room, 'authority_transferred', {
      from: previousHost,
      to: toMemberId,
      previousEpoch,
      newEpoch: room.authorityEpoch,
    });
    return {
      hostMemberId: room.hostMemberId,
      authorityEpoch: room.authorityEpoch,
      previousHost,
      note: 'all host actions now require the new authorityEpoch; the old epoch is rejected even if it arrives late',
    };
  }

  _requireHost(room, { actorMemberId, authorityEpoch, connId, action }) {
    // Epoch is checked first so a stale (returned) authority is rejected
    // explicitly even if its request arrives late from a dead connection.
    if (authorityEpoch == null || authorityEpoch !== room.authorityEpoch) {
      this._event(room, 'authority_rejected', {
        actorMemberId,
        action: action || null,
        reason: 'STALE_AUTHORITY',
        gotEpoch: authorityEpoch == null ? null : authorityEpoch,
        expectedEpoch: room.authorityEpoch,
      });
      throw err.conflict('STALE_AUTHORITY', {
        expectedEpoch: room.authorityEpoch,
        gotEpoch: authorityEpoch == null ? null : authorityEpoch,
      });
    }
    if (actorMemberId !== room.hostMemberId) {
      this._event(room, 'authority_rejected', {
        actorMemberId,
        action: action || null,
        reason: 'NOT_HOST',
        hostMemberId: room.hostMemberId,
      });
      throw err.forbidden('NOT_HOST', { hostMemberId: room.hostMemberId });
    }
    const member = this._member(room, actorMemberId);
    this._requireConnection(room, member, connId);
    return member;
  }

  // ---- views / audit ------------------------------------------------------

  roomSummary(roomId) {
    const room = this._room(roomId);
    return {
      id: room.id,
      rule: room.rule,
      flow: room.flow,
      hostMemberId: room.hostMemberId,
      authorityEpoch: room.authorityEpoch,
      members: [...room.members.values()].map((m) => this._memberView(room, m)),
      discussion: this._discussionView(room, null),
      createdAt: room.createdAt,
    };
  }

  _memberView(room, member) {
    return {
      id: member.id,
      name: member.name,
      manifestVersion: member.currentVersion,
      assignments: member.versions[member.currentVersion].assignments,
      connections: [...member.connections.values()].map((c) => ({
        connId: c.connId,
        alive: c.alive,
        connectedAt: c.connectedAt,
        disconnectedAt: c.disconnectedAt,
      })),
      effectivePermits: this._permitStatus(room, member).effective.map((p) => ({
        permitId: p.permitId,
        kind: p.kind,
      })),
      inRound: room.discussion.round.memberIds.includes(member.id),
      nextRound: room.discussion.nextRound.memberIds.includes(member.id),
    };
  }

  audit({ roomId, since }) {
    const room = this._room(roomId);
    const from = since ? Number(since) : 0;
    return {
      events: room.events.filter((e) => e.seq > from),
      lastSeq: room.events.length ? room.events[room.events.length - 1].seq : 0,
    };
  }
}

module.exports = { CoListeningService, ServiceError, err, guideKey, segKey, nextId };
