import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  HttpError, uid, clone, canonicalColor, normPoint, samePoint, pointKey,
  normalizeOrder, normalizeSegment, planOrder, redBandFixture, straightTailOnlyChange
} from './model.js';

const PROJECT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DATA_FILE = process.env.STUDIO_DATA || process.env.DATA_FILE ||
  path.join(PROJECT_ROOT, 'data', 'studio-state.json');

function emptyDb() {
  return { schema: 1, counter: 0, orders: {}, tickets: {}, requests: {}, events: [] };
}

export class Studio {
  constructor(file = DATA_FILE) {
    this.file = file;
    this.db = emptyDb();
    this.load();
  }

  load() {
    try {
      if (fs.existsSync(this.file)) {
        const parsed = JSON.parse(fs.readFileSync(this.file, 'utf8'));
        this.db = { ...emptyDb(), ...parsed };
        this.db.orders ||= {};
        this.db.tickets ||= {};
        this.db.requests ||= {};
        this.db.events ||= [];
      }
    } catch (err) {
      // A corrupt persistence file must not hide the service; move it aside and start clean.
      const backup = `${this.file}.corrupt-${Date.now()}`;
      try { fs.renameSync(this.file, backup); } catch { /* ignore */ }
      this.db = emptyDb();
      this.persistenceWarning = `状态文件无法解析，已移至 ${backup}: ${err.message}`;
    }
  }

  save() {
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      const tmp = `${this.file}.tmp-${process.pid}`;
      fs.writeFileSync(tmp, JSON.stringify(this.db, null, 2));
      fs.renameSync(tmp, this.file);
    } catch (err) {
      this.persistenceWarning = err.message;
    }
  }

  reset(seed = false) {
    this.db = emptyDb();
    this.save();
    if (seed === 'red-band' || seed === true) return this.createRedBandSites();
    if (seed === 'three-segments' || seed === 'three') return this.createThreeSegmentOrder();
    return { reset: true };
  }

  nextId(prefix) {
    this.db.counter += 1;
    return `${prefix}_${this.db.counter.toString(36)}${Date.now().toString(36).slice(-4)}`;
  }

  addEvent(type, payload) {
    const event = { id: this.nextId('evt'), type, at: new Date().toISOString(), ...payload };
    this.db.events.push(event);
    if (this.db.events.length > 1000) this.db.events = this.db.events.slice(-1000);
    return event;
  }

  // ---------- Order / design ----------

  createOrder(body = {}) {
    const spec = normalizeOrder(body);
    const inputs = Array.isArray(body.segments) ? clone(body.segments)
      : Array.isArray(body.samples) ? clone(body.samples)
      : Array.isArray(body.sections) ? clone(body.sections) : [];
    if (!inputs.length) {
      // Allow creating an empty design board and replacing samples later.
    }
    const id = body.id ? String(body.id) : (spec.id || this.nextId('order'));
    if (this.db.orders[id] && !body.replaceExisting) throw new HttpError(409, 'order_exists', `订单 ${id} 已存在`);

    const order = {
      id,
      name: body.name || body.title || '未命名绣单',
      mode: spec.mode,
      coordOrder: spec.coordOrder,
      rows: spec.rows,
      primaryColor: spec.primaryColor,
      left: spec.left,
      right: spec.right,
      forbidden: spec.forbidden,
      marks: spec.marks,
      segmentInputs: inputs,
      segmentVersions: inputs.map(() => 1),
      segmentPolicies: inputs.map(() => ({})),
      designVersion: 1,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      revisions: [{ version: 1, at: new Date().toISOString(), reason: 'create', summary: '初版样段' }],
      changeRequests: []
    };
    this.db.orders[id] = order;
    this.addEvent('order.created', { orderId: id, mode: order.mode });
    this.save();
    return this.getOrder(id);
  }

  getOrderRecord(id) {
    const order = this.db.orders[id];
    if (!order) throw new HttpError(404, 'order_not_found', `找不到订单 ${id}`);
    return order;
  }

  normalizeStored(order) {
    const base = normalizeOrder({
      ...order,
      segments: order.segmentInputs,
      coordOrder: order.coordOrder
    });
    const segments = order.segmentInputs.map((raw, i) => {
      const seg = normalizeSegment(raw, i, order.coordOrder);
      seg.version = order.segmentVersions?.[i] ?? 1;
      seg.policy = order.segmentPolicies?.[i] || {};
      return seg;
    });
    let offset = 0;
    for (const seg of segments) {
      seg.offset = offset;
      offset += seg.cols;
    }
    return { base, segments };
  }

  latestTicketBySegment(orderId) {
    const tickets = Object.values(this.db.tickets)
      .filter((t) => t.orderId === orderId && t.status !== 'cancelled')
      .sort((a, b) => b.issueNumber - a.issueNumber);
    const map = new Map();
    for (const t of tickets) {
      const key = t.segmentIndex;
      if (!map.has(key)) map.set(key, t);
    }
    return map;
  }

  buildLocks(order, latest = this.latestTicketBySegment(order.id)) {
    const locks = {};
    for (const [segmentIndex, ticket] of latest.entries()) {
      if (ticket.status === 'superseded' || ticket.status === 'cancelled') continue;
      const prefixByColor = {};
      for (const [color, thread] of Object.entries(ticket.threads || {})) {
        prefixByColor[color] = (thread.stitched || []).map((p) => ({ row: p.localRow ?? p.row, col: p.localCol ?? p.col }));
      }
      const lock = {
        ticketId: ticket.id,
        status: ticket.status,
        prefixByColor
      };
      if (ticket.status === 'issued' || ticket.status === 'in_progress' || ticket.status === 'completed') {
        // A checked-out needle sheet locks its complete approved routing and seam.
        // Geometry is authoritative; option keys may change when an identical route is re-registered.
        lock.optionKey = ticket.optionKey;
        lock.walkId = ticket.walkId;
        lock.orientation = ticket.orientation;
        lock.exactPathsByColor = Object.fromEntries(Object.entries(ticket.threads || {}).map(
          ([color, thread]) => [color, (thread.localPath || []).map((p) => ({ row: p.row, col: p.col }))]
        ));
      }
      if (ticket.status === 'returned') {
        // Only the actually stitched prefix remains immutable; the rest is released.
        lock.releasedAt = ticket.returnInfo?.at;
      }
      locks[segmentIndex] = lock;
    }
    return locks;
  }

  buildPolicies(order) {
    const policies = {};
    (order.segmentPolicies || []).forEach((p, i) => { if (p && Object.keys(p).length) policies[i] = p; });
    return policies;
  }

  evaluate(order) {
    const { segments } = this.normalizeStored(order);
    const locks = this.buildLocks(order);
    const policies = this.buildPolicies(order);
    const result = planOrder(order, segments, locks, policies);
    const latest = this.latestTicketBySegment(order.id);

    const segmentViews = segments.map((seg, i) => {
      const ticket = latest.get(i) || null;
      const policy = order.segmentPolicies?.[i] || {};
      const totalCells = Math.max(1, ...seg.options.flatMap((o) => Object.values(o.paths).map((p) => p.length)));
      return {
        index: i,
        segmentIndex: i,
        id: seg.id,
        segmentId: seg.id,
        rows: seg.rows,
        cols: seg.cols,
        currentVersion: order.segmentVersions?.[i] ?? 1,
        segmentVersion: order.segmentVersions?.[i] ?? 1,
        designVersion: order.designVersion,
        issuedVersion: ticket?.issueNumber ?? null,
        issueVersion: ticket?.issueNumber ?? null,
        issuedIssueVersion: ticket?.issueNumber ?? null,
        issuedSegmentVersion: ticket?.segmentVersion ?? null,
        segmentVersionAtIssue: ticket?.segmentVersion ?? null,
        issuedDesignVersion: ticket?.designVersion ?? null,
        ticketId: ticket?.id ?? null,
        status: ticket?.status ?? 'unissued',
        worker: ticket?.workerId ?? ticket?.hoopId ?? null,
        hoopId: ticket?.hoopId ?? null,
        stitchedCount: ticket ? this.primaryCursor(ticket) : 0,
        totalCells: ticket?.diagram?.primaryLength ?? totalCells,
        nextCell: ticket ? this.nextCellView(ticket) : null,
        optionKey: ticket?.optionKey ?? null,
        policy
      };
    });

    const issuedVersions = segmentViews.map((s) => ({
      segmentIndex: s.index,
      segmentId: s.segmentId,
      currentVersion: s.currentVersion,
      segmentVersion: s.currentVersion,
      issuedVersion: s.issuedVersion,
      issueVersion: s.issueVersion,
      issuedSegmentVersion: s.issuedSegmentVersion,
      segmentVersionAtIssue: s.segmentVersionAtIssue,
      issuedDesignVersion: s.issuedDesignVersion,
      designVersion: s.designVersion,
      ticketId: s.ticketId
    }));

    const actualPaths = {};
    for (const ticket of latest.values()) {
      for (const [color, thread] of Object.entries(ticket.threads || {})) {
        actualPaths[color] ||= [];
        // Segments are ordered; latest map iteration is insertion order not segment order. Sort later.
        actualPaths[color].push({ segmentIndex: ticket.segmentIndex, points: thread.stitched || [] });
      }
    }
    const actualPathByColor = {};
    for (const [color, chunks] of Object.entries(actualPaths)) {
      chunks.sort((a, b) => a.segmentIndex - b.segmentIndex);
      actualPathByColor[color] = chunks.flatMap((c) => c.points.map((p) => ({ row: p.row, col: p.col })));
    }

    const allTickets = Object.values(this.db.tickets).filter((t) => t.orderId === order.id);
    const activeTickets = allTickets.filter((t) => ['issued', 'in_progress', 'returned'].includes(t.status));
    const completedTickets = allTickets.filter((t) => t.status === 'completed');
    const expectedTicketCount = order.segmentInputs.length;
    const allComplete = expectedTicketCount > 0 &&
      order.segmentInputs.every((_, i) => latest.get(i)?.status === 'completed');

    let fullPath = result.feasible ? result.plan.fullPaths : {};
    if (!result.feasible) {
      // Still expose whatever issued needle paths exist for inspection/reconnection.
      fullPath = this.issuedPathSnapshot(order, latest);
    }

    return {
      feasible: result.feasible,
      completable: result.feasible,
      canComplete: result.feasible,
      viable: result.feasible,
      complete: allComplete,
      completed: allComplete,
      allSegmentsIssued: order.segmentInputs.every((_, i) => latest.has(i)),
      activeTicketCount: activeTickets.length,
      completedTicketCount: completedTickets.length,
      segmentCount: order.segmentInputs.length,
      violations: result.violations,
      plan: result.plan,
      board: {
        rows: order.rows,
        width: segments.reduce((n, s) => n + s.cols, 0),
        segments: segments.map((s) => ({
          index: s.index, id: s.id, rows: s.rows, cols: s.cols, offset: s.offset,
          orientations: s.orientations, options: s.options
        }))
      },
      seams: result.plan?.seams || [],
      fullPath,
      completeNeedlePath: fullPath,
      needlePath: fullPath[order.primaryColor] || fullPath,
      actualPath: actualPathByColor[order.primaryColor] || [],
      actualPathByColor,
      actualPathByColor,
      segmentViews,
      segments: segmentViews,
      issuedVersions,
      latest
    };
  }

  issuedPathSnapshot(order, latest) {
    const out = {};
    const byIndex = [...latest.entries()].sort((a, b) => Number(a[0]) - Number(b[0]));
    for (const [, ticket] of byIndex) {
      for (const [color, thread] of Object.entries(ticket.threads || {})) {
        out[color] ||= [];
        out[color].push(...(thread.path || []));
      }
    }
    return out;
  }

  getOrder(id) {
    const order = this.getOrderRecord(id);
    const view = this.evaluate(order);
    const tickets = Object.values(this.db.tickets)
      .filter((t) => t.orderId === id)
      .sort((a, b) => a.segmentIndex - b.segmentIndex || b.issueNumber - a.issueNumber);
    return {
      ...clone(order),
      orderId: order.id,
      ...clone({
        feasible: view.feasible,
        completable: view.completable,
        canComplete: view.canComplete,
        viable: view.viable,
        complete: view.complete,
        completed: view.completed,
        allSegmentsIssued: view.allSegmentsIssued,
        violations: view.violations,
        plan: view.plan,
        board: view.board,
        seams: view.seams,
        fullPath: view.fullPath,
        completeNeedlePath: view.completeNeedlePath,
        needlePath: view.needlePath,
        actualPath: view.actualPath,
        actualPathByColor: view.actualPathByColor,
        segmentViews: view.segmentViews,
        issuedVersions: view.issuedVersions
      }),
      tickets: clone(tickets),
      currentVersion: order.designVersion,
      version: order.designVersion
    };
  }

  listOrders() {
    return Object.keys(this.db.orders).map((id) => {
      const o = this.getOrder(id);
      return {
        id: o.id, name: o.name, mode: o.mode, designVersion: o.designVersion,
        segmentCount: o.segmentCount, completable: o.completable, complete: o.complete,
        updatedAt: o.updatedAt
      };
    });
  }

  // ---------- Design replacement / change applications ----------

  extractIncomingSegments(body, order) {
    const candidate = body.segments ?? body.samples ?? body.sections ?? body.newSegments ?? body.patterns;
    if (candidate !== undefined) return { all: true, inputs: clone(Array.isArray(candidate) ? candidate : [candidate]) };

    const target = body.segment ?? body.sample ?? body.newSegment ?? body.pattern;
    if (target !== undefined) {
      const idx = this.resolveSegmentIndex(order, body);
      return { all: false, index: idx, input: clone(target) };
    }

    if (body.tail !== undefined) {
      const tailInput = body.tail;
      if (Array.isArray(tailInput)) return { all: false, index: order.segmentInputs.length - 1, input: clone(tailInput[0]) };
      return { all: false, index: order.segmentInputs.length - 1, input: clone(tailInput) };
    }
    if (body.first !== undefined) return { all: false, index: 0, input: clone(body.first) };
    return { all: false, patchOnly: true };
  }

  resolveSegmentIndex(order, body) {
    let idx = body.segmentIndex ?? body.index ?? body.segment ?? body.segmentId ?? body.targetIndex;
    if (typeof idx === 'number' || /^\d+$/.test(String(idx))) {
      const n = Number(idx);
      if (n >= 0 && n < order.segmentInputs.length) return n;
    }
    if (typeof idx === 'string') {
      const found = order.segmentInputs.findIndex((s, i) => s.id === idx || s.segmentId === idx || `seg-${i + 1}` === idx);
      if (found >= 0) return found;
    }
    if (body.tail !== undefined || body.as === 'tail') return order.segmentInputs.length - 1;
    if (body.first !== undefined || body.as === 'first') return 0;
    throw new HttpError(400, 'segment_required', '请提供 segmentIndex 或 segmentId');
  }

  assertBaseVersion(order, body) {
    const base = body.baseVersion ?? body.expectedDesignVersion ?? body.designVersion ?? body.expectedVersion;
    if (base !== undefined && Number(base) !== order.designVersion) {
      throw new HttpError(409, 'design_version_conflict', '样单已被主编或另一终端更新，请基于最新版本提交', {
        currentVersion: order.designVersion,
        suppliedVersion: base
      });
    }
  }

  assertChangeDoesNotMoveLockedStitches(order, nextInputs, nextPolicies, changedIndexes) {
    const latest = this.latestTicketBySegment(order.id);
    for (const idx of changedIndexes) {
      const ticket = latest.get(idx);
      if (!ticket || ticket.status === 'cancelled' || ticket.status === 'superseded') continue;

      if (ticket.status === 'issued' || ticket.status === 'in_progress') {
        // The complete checked-out diagram is an approved set of needle positions.
        const nextRaw = nextInputs[idx];
        let nextSeg;
        try { nextSeg = normalizeSegment(nextRaw, idx, order.coordOrder); }
        catch (e) { throw e; }
        const hasSameApprovedRoute = nextSeg.options.some((o) => {
          if (o.orientation !== ticket.orientation) return false;
          for (const [color, oldPath] of Object.entries(ticket.diagram?.localThreads || ticket.localThreads || {})) {
            const newPath = o.paths[color];
            if (!newPath || newPath.length !== oldPath.path?.length) return false;
            if (!oldPath.path.every((p, i) => samePoint(p, newPath[i]))) return false;
          }
          return true;
        });
        const policy = nextPolicies[idx] || {};
        const keepsKey = nextSeg.options.some((o) => o.key === ticket.optionKey);
        if (!hasSameApprovedRoute && !keepsKey && !policy.__preserveTicket) {
          throw new HttpError(409, 'active_ticket_locked', `第 ${idx + 1} 段工单 ${ticket.id} 尚未交回，不能挪走已签发针位`, {
            segmentIndex: idx, ticketId: ticket.id, status: ticket.status
          });
        }
      }

      if (ticket.status === 'completed') {
        const stitched = [];
        for (const thread of Object.values(ticket.threads || {})) {
          stitched.push(...(thread.stitched || []).map((p) => ({ row: p.localRow ?? p.row, col: p.localCol ?? p.col })));
        }
        if (stitched.length) {
          let nextSeg;
          try { nextSeg = normalizeSegment(nextInputs[idx], idx, order.coordOrder); }
          catch (e) { throw e; }
          const preserves = nextSeg.options.some((o) => Object.values(o.paths).some((path) =>
            stitched.every((p, i) => path[i] && samePoint(path[i], p))
          ));
          if (!preserves) {
            throw new HttpError(409, 'stitched_part_immutable', `第 ${idx + 1} 段已有落针，旧作不能被改走`, {
              segmentIndex: idx, ticketId: ticket.id, stitched
            });
          }
        }
      }

      if (ticket.status === 'returned') {
        const prefixes = {};
        for (const [color, thread] of Object.entries(ticket.threads || {})) {
          prefixes[color] = (thread.stitched || []).map((p) => ({ row: p.localRow ?? p.row, col: p.localCol ?? p.col }));
        }
        let nextSeg;
        try { nextSeg = normalizeSegment(nextInputs[idx], idx, order.coordOrder); }
        catch (e) { throw e; }
        for (const [color, prefix] of Object.entries(prefixes)) {
          if (!prefix.length) continue;
          const preserves = nextSeg.options.some((o) => {
            const path = o.paths[color] || [];
            return prefix.every((p, i) => samePoint(path[i], p));
          });
          if (!preserves) {
            throw new HttpError(409, 'returned_prefix_not_preserved', `第 ${idx + 1} 段交回时保留的已刺前缀必须接回`, {
              segmentIndex: idx, ticketId: ticket.id, prefix
            });
          }
        }
      }
    }
  }

  prepareDesignChange(id, body = {}) {
    const order = this.getOrderRecord(id);
    this.assertBaseVersion(order, body);

    const nextInputs = clone(order.segmentInputs);
    while (nextInputs.length < order.segmentVersions.length) nextInputs.push({});
    const nextVersions = clone(order.segmentVersions);
    const nextPolicies = clone(order.segmentPolicies || []);
    const changed = new Set();

    const incoming = this.extractIncomingSegments(body, order);
    if (incoming.all) {
      incoming.inputs.forEach((raw, i) => {
        nextInputs[i] = raw;
        // A freshly supplied segment replaces old editor route restrictions too,
        // unless the caller explicitly asks to keep them.
        if (body.keepPolicy !== true) nextPolicies[i] = {};
        changed.add(i);
      });
      nextInputs.length = incoming.inputs.length;
      while (nextVersions.length < nextInputs.length) nextVersions.push(1);
      nextVersions.length = nextInputs.length;
      while (nextPolicies.length < nextInputs.length) nextPolicies.push({});
      nextPolicies.length = nextInputs.length;
    } else if (!incoming.patchOnly && incoming.input) {
      const idx = incoming.index;
      nextInputs[idx] = incoming.input;
      if (body.keepPolicy !== true) nextPolicies[idx] = {};
      changed.add(idx);
      while (nextVersions.length <= idx) nextVersions.push(1);
      while (nextPolicies.length <= idx) nextPolicies.push({});
    }

    const restoreTarget = body.restore === true || body.resetPolicy === true || body.clearPolicy === true
      ? this.resolvePolicyTarget(order, body, incoming)
      : null;
    if (restoreTarget !== null) {
      nextPolicies[restoreTarget] = {};
      changed.add(restoreTarget);
    }

    // Policy-only change, e.g. “tail becomes straight-only”.
    const hasPolicyFields = body.straightOnly !== undefined || 'allowedKinds' in body ||
      'allowedRouteIds' in body || 'allowedWalkIds' in body || body.routeId || body.walkId ||
      body.orientation || body.flip;
    const policyTarget = hasPolicyFields
      ? this.resolvePolicyTarget(order, body, incoming)
      : null;
    if (policyTarget !== null) {
      const idx = policyTarget;
      while (nextPolicies.length <= idx) nextPolicies.push({});
      const policy = nextPolicies[idx] || {};
      if (body.straightOnly !== undefined) {
        if (body.straightOnly) policy.straightOnly = true;
        else delete policy.straightOnly;
      }
      if ('allowedKinds' in body) {
        if (body.allowedKinds === null) delete policy.allowedKinds;
        else policy.allowedKinds = body.allowedKinds;
      }
      if ('allowedRouteIds' in body) {
        if (body.allowedRouteIds === null) delete policy.allowedRouteIds;
        else policy.allowedRouteIds = body.allowedRouteIds;
      }
      if ('allowedWalkIds' in body) {
        if (body.allowedWalkIds === null) delete policy.allowedWalkIds;
        else policy.allowedWalkIds = body.allowedWalkIds;
      }
      if (body.routeId) policy.allowedRouteIds = [].concat(body.routeId);
      if (body.walkId) policy.allowedWalkIds = [].concat(body.walkId);
      if (body.orientation || body.flip) policy.orientation = body.orientation || body.flip;
      nextPolicies[idx] = policy;
      changed.add(idx);
    }

    // Order-level constraint edits.
    const nextOrder = clone(order);
    if (body.left !== undefined || body.leftEnd !== undefined) {
      const spec = normalizeOrder({ ...order, left: body.left ?? body.leftEnd, coordOrder: order.coordOrder });
      nextOrder.left = spec.left;
    }
    if (body.right !== undefined || body.rightEnd !== undefined) {
      const spec = normalizeOrder({ ...order, right: body.right ?? body.rightEnd, coordOrder: order.coordOrder });
      nextOrder.right = spec.right;
    }
    if (body.forbidden !== undefined || body.blanks !== undefined) {
      const spec = normalizeOrder({ forbidden: body.forbidden ?? body.blanks, coordOrder: order.coordOrder });
      nextOrder.forbidden = spec.forbidden;
    }
    if (body.marks !== undefined || body.emblems !== undefined) {
      const spec = normalizeOrder({ marks: body.marks ?? body.emblems, coordOrder: order.coordOrder });
      nextOrder.marks = spec.marks;
    }

    this.assertChangeDoesNotMoveLockedStitches(order, nextInputs, nextPolicies, changed);

    nextOrder.segmentInputs = nextInputs;
    nextOrder.segmentPolicies = nextPolicies;
    const { segments } = this.normalizeStored({ ...nextOrder, segmentVersions: nextVersions });
    // Increment candidate versions only after protection/normalization succeeds.
    for (const idx of changed) nextVersions[idx] = (nextVersions[idx] ?? 1) + 1;
    const locks = this.buildLocks(order);
    const evaluation = planOrder(nextOrder, segments, locks, nextPolicies);
    return { order, nextOrder, nextInputs, nextVersions, nextPolicies, changed: [...changed], evaluation };
  }

  resolvePolicyTarget(order, body, incoming) {
    if (body.segmentIndex !== undefined || body.index !== undefined || body.segmentId !== undefined || body.segment !== undefined) {
      try { return this.resolveSegmentIndex(order, body); } catch { /* fall through */ }
    }
    if (incoming.index !== undefined) return incoming.index;
    if (body.target === 'tail' || body.as === 'tail' || body.tail !== undefined) return order.segmentInputs.length - 1;
    if (body.target === 'first' || body.as === 'first' || body.first !== undefined) return 0;
    if (body.targetIndex !== undefined) return Number(body.targetIndex);
    // If exactly one segment, use it; otherwise require target to avoid accidentally changing all.
    if (order.segmentInputs.length === 1) return 0;
    throw new HttpError(400, 'policy_target_required', '请说明要改哪一段（segmentIndex/segmentId/target=tail）');
  }

  savePreparedChange(prepared, body = {}, status = 'applied') {
    const { order, nextOrder, nextVersions, nextPolicies, changed, evaluation } = prepared;
    order.left = nextOrder.left;
    order.right = nextOrder.right;
    order.forbidden = nextOrder.forbidden;
    order.marks = nextOrder.marks;
    order.segmentInputs = nextOrder.segmentInputs;
    order.segmentPolicies = nextPolicies;
    order.segmentVersions = nextVersions;
    order.designVersion += 1;
    order.version = order.designVersion;
    order.updatedAt = new Date().toISOString();
    order.revisions.push({
      version: order.designVersion,
      at: order.updatedAt,
      reason: body.reason || body.note || body.description || 'change',
      changedSegments: changed,
      feasible: evaluation.feasible,
      status
    });
    this.addEvent('design.changed', {
      orderId: order.id, version: order.designVersion, changed, feasible: evaluation.feasible, status
    });
    this.save();
    return this.getOrder(order.id);
  }

  registerSegment(id, body = {}) {
    const order = this.getOrderRecord(id);
    const raw = body.segment ?? body.sample ?? body.data ??
      ((body.threads || body.colors || body.path || body.cells || body.rows !== undefined || body.cols !== undefined) ? body : null);
    const raws = Array.isArray(raw) ? raw : (Array.isArray(body.segments) && body.append === true ? body.segments : (raw ? [raw] : []));
    if (!raws.length) throw new HttpError(400, 'segment_required', '请提供要登记的样段对象');
    const now = new Date().toISOString();
    for (const item of raws) {
      // Normalize once to reject invalid grids/routes before mutating state.
      normalizeSegment(item, order.segmentInputs.length, order.coordOrder);
      order.segmentInputs.push(clone(item));
      order.segmentVersions.push(1);
      order.segmentPolicies.push({});
    }
    order.designVersion += 1;
    order.version = order.designVersion;
    order.updatedAt = now;
    order.revisions.push({ version: order.designVersion, at: now, reason: body.reason || body.note || 'register-segment', added: raws.length });
    this.addEvent('segment.registered', { orderId: id, version: order.designVersion, added: raws.length });
    this.save();
    return this.getOrder(id);
  }

  updateDesign(id, body = {}) {
    const prepared = this.prepareDesignChange(id, body);
    const saveInfeasible = body.saveDraft !== false; // Designer canvas can hold an infeasible draft.
    if (!prepared.evaluation.feasible && (body.strict || body.apply === true) && !body.draft && !saveInfeasible) {
      const err = new HttpError(409, 'change_not_viable', '改样后无法在锁住旧针/接缝的情况下达成订单', prepared.evaluation);
      err.prepared = prepared;
      throw err;
    }
    const view = this.savePreparedChange(prepared, body, prepared.evaluation.feasible ? 'applied' : 'draft-infeasible');
    return {
      ...view,
      accepted: prepared.evaluation.feasible,
      requestAccepted: prepared.evaluation.feasible,
      saved: true,
      draftSaved: true,
      applied: prepared.evaluation.feasible,
      feasible: prepared.evaluation.feasible,
      completable: prepared.evaluation.feasible,
      canComplete: prepared.evaluation.feasible,
      violations: prepared.evaluation.violations,
      changedSegments: prepared.changed
    };
  }

  submitChangeRequest(id, body = {}) {
    const order = this.getOrderRecord(id);
    const requestId = body.requestId || this.nextId('chg');
    let prepared;
    try {
      prepared = this.prepareDesignChange(id, body);
    } catch (err) {
      const request = {
        id: requestId, orderId: id, status: 'rejected', accepted: false,
        at: new Date().toISOString(), reason: err.code, message: err.message, details: err.details || {}
      };
      order.changeRequests.push(request);
      this.db.requests[requestId] = request;
      this.save();
      if (body.httpError !== false) {
        throw new HttpError(err.status || 409, err.code, err.message, { ...(err.details || {}), request });
      }
      return request;
    }

    if (!prepared.evaluation.feasible) {
      const request = {
        id: requestId, orderId: id, status: 'rejected', accepted: false,
        at: new Date().toISOString(), reason: 'change_not_viable',
        message: '仅替换未签发段还不够：边界不相容或订单已无法达成',
        violations: prepared.evaluation.violations,
        changedSegments: prepared.changed,
        baseVersion: order.designVersion
      };
      order.changeRequests.push(request);
      this.db.requests[requestId] = request;
      this.save();
      const error = new HttpError(409, 'change_request_rejected', request.message, { request, evaluation: prepared.evaluation });
      error.requestRecord = request;
      throw error;
    }

    const view = this.savePreparedChange(prepared, body, 'accepted');
    const request = {
      id: requestId, orderId: id, status: 'accepted', accepted: true,
      at: new Date().toISOString(), version: view.designVersion,
      changedSegments: prepared.changed
    };
    order.changeRequests.push(request);
    this.db.requests[requestId] = request;
    this.save();
    return { request, ...view };
  }

  // ---------- Ticketing / issuing / claiming ----------

  extractIssueConstraints(body = {}) {
    const c = body.constraints || body.choice || body.options || body.ticketOptions || body.ticket || {};
    const get = (...keys) => {
      for (const k of keys) if (body[k] !== undefined) return body[k];
      for (const k of keys) if (c[k] !== undefined) return c[k];
      return undefined;
    };
    const constraints = {};
    let routeId = get('routeId', 'route', 'onlyRoute', 'routeName', 'pathId', 'allowedRoute');
    if (routeId && typeof routeId === 'object') routeId = routeId.id || routeId.routeId || routeId.name;
    let walkId = get('walkId', 'walk', 'routing', '走法');
    if (walkId && typeof walkId === 'object') walkId = walkId.id || walkId.walkId || walkId.name;
    const allowedRouteIds = get('allowedRouteIds', 'routeIds', 'routes', 'allowedRoutes', 'routeWhitelist');
    const allowedWalkIds = get('allowedWalkIds', 'walkIds', 'walks', 'allowedWalks');
    let allowedKinds = get('allowedKinds', 'kinds', 'kind', 'routeKind', 'type', 'mode', 'only');
    const straightOnly = get('straightOnly');
    const orientation = get('orientation', 'flip', 'transform');
    const optionKey = get('optionKey');
    if (routeId) constraints.allowedRouteIds = [].concat(routeId);
    if (allowedRouteIds) constraints.allowedRouteIds = [].concat(allowedRouteIds);
    if (walkId) constraints.allowedWalkIds = [].concat(walkId);
    if (allowedWalkIds) constraints.allowedWalkIds = [].concat(allowedWalkIds);
    if (allowedKinds) {
      allowedKinds = [].concat(allowedKinds).map((k) => {
        if (k === '直' || k === '直走' || k === 'only-straight') return 'straight';
        if (k === '换行' || k === '折行') return 'switch';
        return k;
      });
      constraints.allowedKinds = allowedKinds;
      if (allowedKinds.length === 1 && allowedKinds[0] === 'straight') constraints.straightOnly = true;
    }
    if (straightOnly !== undefined) constraints.straightOnly = !!straightOnly;
    if (orientation !== undefined) constraints.orientation = orientation;
    if (optionKey) constraints.optionKey = optionKey;
    return constraints;
  }

  claim(id, body = {}) {
    const order = this.getOrderRecord(id);
    const latest = this.latestTicketBySegment(id);

    let segmentIndex;
    if (body.segmentIndex !== undefined || body.index !== undefined || body.segmentId !== undefined || body.segment !== undefined) {
      segmentIndex = this.resolveSegmentIndex(order, body);
    } else {
      segmentIndex = order.segmentInputs.findIndex((_, i) => !latest.has(i) || latest.get(i).status === 'returned');
      if (segmentIndex < 0) segmentIndex = 0;
    }

    const existing = latest.get(segmentIndex);
    const workerId = body.workerId || body.hoopId || body.assignee || body.claimedBy ||
      (existing?.workerId) || this.nextId('hoop');
    // Idempotent reconnect/claim by the same worker/terminal.
    if (existing && ['issued', 'in_progress'].includes(existing.status) &&
        (body.terminalId && existing.terminalId === body.terminalId ||
         body.workerId && existing.workerId === body.workerId && body.reclaim !== true)) {
      return this.getTicket(existing.id);
    }
    if (existing && ['issued', 'in_progress'].includes(existing.status) && body.force !== true && body.reclaim !== true) {
      throw new HttpError(409, 'segment_already_claimed', `第 ${segmentIndex + 1} 段已被 ${existing.workerId} 领取`, {
        ticketId: existing.id, status: existing.status
      });
    }

    if (order.mode === 'relay' && segmentIndex > 0 && body.allowOutOfOrder !== true) {
      const prev = latest.get(segmentIndex - 1);
      if (!prev || !['completed', 'returned'].includes(prev.status)) {
        throw new HttpError(409, 'relay_handoff_required', '接力办社必须等前段交回出口后，才能领取下一段', {
          previousSegment: segmentIndex - 1
        });
      }
    }

    const constraints = this.extractIssueConstraints(body);
    if (order.segmentPolicies?.[segmentIndex]) Object.assign(constraints, order.segmentPolicies[segmentIndex]);
    const locks = this.buildLocks(order, latest);
    // If replacing a returned ticket, its prefix lock is already present. If superseding active by force, keep option lock.
    const issueConstraints = { [segmentIndex]: constraints };
    const { segments } = this.normalizeStored(order);
    const evaluation = planOrder(order, segments, locks, issueConstraints);
    if (!evaluation.feasible) {
      throw new HttpError(409, 'no_viable_ticket', '无法在当前接缝、订单和已锁针位下开出这张针单', {
        violations: evaluation.violations
      });
    }
    const planned = evaluation.plan.segments[segmentIndex];
    if (!planned) throw new HttpError(409, 'plan_missing_segment', '规划中缺少该样段');

    // Supersede returned/old ticket when reissuing.
    let reissueOf = null;
    let prefixStitches = {};
    if (existing) {
      reissueOf = existing.id;
      for (const [color, thread] of Object.entries(existing.threads || {})) {
        prefixStitches[color] = thread.stitched || [];
      }
      existing.status = 'superseded';
      existing.supersededAt = new Date().toISOString();
      existing.supersededBy = this.nextId('ticket'); // placeholder id set below? not ideal
    }

    const ticketId = body.ticketId || this.nextId('ticket');
    if (existing) existing.supersededBy = ticketId;
    const issueNumber = (existing?.issueNumber || 0) + 1;
    const colors = Object.keys(planned.localPaths);
    const threads = {};
    const localThreads = {};
    for (const color of colors) {
      const globalPath = planned.paths[color];
      const localPath = planned.localPaths[color];
      const oldStitches = prefixStitches[color] || [];
      threads[color] = {
        color,
        path: clone(globalPath),
        localPath: clone(localPath),
        entry: planned.entries[color],
        exit: planned.exits[color],
        localEntry: planned.localEntries[color],
        localExit: planned.localExits[color],
        stitched: clone(oldStitches),
        cursor: oldStitches.length
      };
      localThreads[color] = { path: clone(localPath) };
    }
    const primaryColor = colors.includes(order.primaryColor) ? order.primaryColor : colors[0];
    const primaryThread = threads[primaryColor];

    const leftSeam = evaluation.plan.seams.find((s) => s.between[1] === segmentIndex) || null;
    const rightSeam = evaluation.plan.seams.find((s) => s.between[0] === segmentIndex) || null;

    const ticket = {
      id: ticketId,
      orderId: id,
      segmentIndex,
      segmentId: planned.segmentId,
      hoopId: body.hoopId || workerId,
      workerId,
      terminalId: body.terminalId || null,
      status: primaryThread.cursor >= primaryThread.path.length ? 'completed' : (primaryThread.cursor ? 'in_progress' : 'issued'),
      mode: order.mode,
      designVersion: order.designVersion,
      segmentVersion: order.segmentVersions[segmentIndex] ?? 1,
      issueNumber,
      issuedVersion: issueNumber,
      version: 1,
      reportVersion: 1,
      optionKey: planned.optionKey,
      walkId: planned.walkId,
      orientation: planned.orientation,
      constraints,
      colors,
      primaryColor,
      threads,
      diagram: {
        version: 1,
        ticketVersion: 1,
        orderId: id,
        segmentIndex,
        segmentId: planned.segmentId,
        rows: segments[segmentIndex].rows,
        cols: segments[segmentIndex].cols,
        offset: segments[segmentIndex].offset,
        colors,
        primaryColor,
        threads: clone(threads),
        localThreads: clone(localThreads),
        orderedCells: clone(primaryThread.path),
        localOrderedCells: clone(primaryThread.localPath),
        entry: primaryThread.entry,
        exit: primaryThread.exit,
        localEntry: primaryThread.localEntry,
        localExit: primaryThread.localExit,
        seamToPrevious: leftSeam,
        seamToNext: rightSeam,
        seams: { left: leftSeam, right: rightSeam },
        totalCells: primaryThread.path.length,
        primaryLength: primaryThread.path.length,
        offline: true,
        instruction: '按 orderedCells 顺序相邻下针；入口、出口、颜色和接缝行不得擅改。'
      },
      stitched: clone(primaryThread.stitched),
      cursor: primaryThread.cursor,
      nextCell: null,
      reissueOf,
      requestIds: {},
      createdAt: new Date().toISOString(),
      issuedAt: new Date().toISOString(),
      updatedAt: new Date().toISOString()
    };
    ticket.nextCell = this.nextCellView(ticket);
    this.db.tickets[ticketId] = ticket;
    this.addEvent('ticket.issued', { orderId: id, ticketId, segmentIndex, issueNumber, optionKey: ticket.optionKey });
    this.save();
    return this.getTicket(ticketId);
  }

  getTicketRecord(id) {
    const ticket = this.db.tickets[id];
    if (!ticket) throw new HttpError(404, 'ticket_not_found', `找不到工单/针单 ${id}`);
    return ticket;
  }

  getTicket(id) {
    const ticket = this.getTicketRecord(id);
    const order = this.db.orders[ticket.orderId];
    const view = order ? this.evaluate(order) : null;
    return {
      ...clone(ticket),
      ticketId: ticket.id,
      workOrderId: ticket.id,
      needleSheetId: ticket.id,
      version: ticket.version,
      reportVersion: ticket.version,
      expectedVersion: ticket.version,
      next: this.nextCellView(ticket),
      nextCell: this.nextCellView(ticket),
      currentStitch: this.lastStitchView(ticket),
      segmentIssuedVersion: view?.issuedVersions?.[ticket.segmentIndex]?.issuedVersion ?? ticket.segmentVersion,
      orderCompletable: view?.completable ?? null
    };
  }

  primaryCursor(ticket) {
    return ticket.threads?.[ticket.primaryColor]?.cursor ?? ticket.cursor ?? 0;
  }

  nextCellView(ticket, color = ticket.primaryColor) {
    const thread = ticket.threads?.[color];
    if (!thread) return null;
    const p = thread.path[thread.cursor];
    const local = thread.localPath[thread.cursor];
    if (!p) return null;
    return { ...p, global: { ...p }, local, row: p.row, col: p.col, color, index: thread.cursor };
  }

  lastStitchView(ticket) {
    const thread = ticket.threads?.[ticket.primaryColor];
    if (!thread?.cursor) return null;
    return thread.stitched[thread.stitched.length - 1] || null;
  }

  checkTicketVersion(ticket, body) {
    const expected = body.expectedVersion ?? body.version ?? body.baseVersion ??
      body.reportVersion ?? body.expectedReportVersion ?? body.expectedTicketVersion;
    if (expected !== undefined && expected !== null && Number(expected) !== ticket.version) {
      throw new HttpError(409, 'version_conflict', '报工版本已过期；服务按实际受理的第一个动作生效', {
        currentVersion: ticket.version,
        reportVersion: ticket.version,
        expectedVersion: Number(expected),
        current: this.getTicket(ticket.id)
      });
    }
  }

  normalizeReportCell(ticket, body, color) {
    const rawCell = body.cell ?? body.at ?? body.point ?? body.nextCell ?? body.stitch;
    if (rawCell === undefined) return null;
    const order = this.db.orders[ticket.orderId];
    const coordOrder = order?.coordOrder || 'rc';
    return normPoint(rawCell, coordOrder);
  }

  findThreadForCell(ticket, cell) {
    if (!cell) return ticket.primaryColor;
    for (const [color, thread] of Object.entries(ticket.threads)) {
      const next = thread.path[thread.cursor];
      if (next && samePoint(next, cell)) return color;
      const nextLocal = thread.localPath[thread.cursor];
      if (nextLocal && samePoint(nextLocal, cell)) return color;
    }
    return null;
  }

  report(id, body = {}) {
    let ticket = this.getTicketRecord(id);
    const requestId = body.requestId || body.idempotencyKey;
    if (requestId && ticket.requestIds?.[requestId]) {
      return this.getTicket(ticket.requestIds[requestId]);
    }
    if (['superseded', 'cancelled'].includes(ticket.status)) {
      throw new HttpError(409, 'ticket_not_active', `针单 ${id} 已${ticket.status}，请使用换发后的新针单`, {
        status: ticket.status, reissuedTo: ticket.supersededBy
      });
    }
    this.checkTicketVersion(ticket, body);
    if (ticket.status === 'returned') {
      throw new HttpError(409, 'ticket_returned', '这张针单已交回改样；已保留落针，请领取换发后的新针单', {
        currentVersion: ticket.version, ticket: this.getTicket(id)
      });
    }
    if (ticket.status === 'completed' && body.cell === undefined && body.count === undefined && body.stitchedCount === undefined) {
      return this.getTicket(id);
    }

    const desiredCount = body.count ?? body.stitchedCount ?? body.cursor ?? body.through;
    let color = canonicalColor(body.color ?? body.thread ?? ticket.primaryColor);
    let cell = this.normalizeReportCell(ticket, body, color);

    if (cell && !ticket.threads[color]) {
      const found = this.findThreadForCell(ticket, cell);
      if (found) color = found;
    }
    const thread = ticket.threads[color];
    if (!thread) throw new HttpError(400, 'unknown_color', `针单没有颜色 ${color}`);

    // Reconnect-friendly duplicate: this exact cell is already confirmed.
    if (cell) {
      const already = thread.stitched.some((p) => samePoint(p, cell) || samePoint({ row: p.localRow, col: p.localCol }, cell));
      if (already) {
        return { ...this.getTicket(id), duplicate: true, accepted: true, idempotent: true };
      }
    }

    let advanceTo = thread.cursor + 1;
    if (desiredCount !== undefined) {
      advanceTo = Number(desiredCount);
      if (body.increment !== undefined) advanceTo = thread.cursor + Number(body.increment);
    } else if (body.index !== undefined) {
      advanceTo = Number(body.index) + 1;
    } else if (cell) {
      const globalIdx = thread.path.findIndex((p) => samePoint(p, cell));
      const localIdx = thread.localPath.findIndex((p) => samePoint(p, cell));
      const idx = globalIdx >= 0 ? globalIdx : localIdx;
      if (idx < 0) throw new HttpError(422, 'cell_not_on_diagram', '所报格位不在这根线的针图上', { cell, color, diagram: thread.path });
      if (idx < thread.cursor) {
        return { ...this.getTicket(id), duplicate: true, accepted: true, idempotent: true };
      }
      if (idx !== thread.cursor && body.allowJump !== true) {
        throw new HttpError(409, 'not_next_cell', '必须逐格报工；该格之前还有未确认针迹', {
          expectedIndex: thread.cursor, expectedCell: this.nextCellView(ticket, color), suppliedIndex: idx
        });
      }
      advanceTo = idx + 1;
    }

    if (!Number.isInteger(advanceTo) || advanceTo < thread.cursor) {
      throw new HttpError(400, 'bad_progress', '报工进度不能倒退');
    }
    if (advanceTo > thread.path.length) {
      throw new HttpError(409, 'past_exit', '已经到达出口，不能再报更多针', { length: thread.path.length });
    }

    const now = new Date().toISOString();
    while (thread.cursor < advanceTo) {
      const index = thread.cursor;
      const global = thread.path[index];
      const local = thread.localPath[index];
      thread.stitched.push({
        ...global,
        globalRow: global.row,
        globalCol: global.col,
        localRow: local.row,
        localCol: local.col,
        color,
        index,
        at: thread.stitched.length + 1,
        reportedAt: now
      });
      thread.cursor += 1;
    }

    ticket.version += 1;
    ticket.reportVersion = ticket.version;
    ticket.updatedAt = now;
    ticket.status = Object.values(ticket.threads).every((t) => t.cursor >= t.path.length) ? 'completed' : 'in_progress';
    if (ticket.status === 'completed') ticket.completedAt = now;
    ticket.cursor = ticket.threads[ticket.primaryColor].cursor;
    ticket.stitched = clone(ticket.threads[ticket.primaryColor].stitched);
    ticket.nextCell = this.nextCellView(ticket);
    ticket.diagram.threads = clone(ticket.threads);
    ticket.diagram.ticketVersion = ticket.version;
    if (requestId) ticket.requestIds[requestId] = id;
    this.addEvent('ticket.reported', { orderId: ticket.orderId, ticketId: id, version: ticket.version, color, advanceTo });
    this.save();
    return this.getTicket(id);
  }

  returnTicket(id, body = {}) {
    const ticket = this.getTicketRecord(id);
    const requestId = body.requestId || body.idempotencyKey;
    if (requestId && ticket.requestIds?.[requestId]) return this.getTicket(ticket.requestIds[requestId]);

    this.checkTicketVersion(ticket, body);
    if (ticket.status === 'superseded' || ticket.status === 'cancelled') {
      throw new HttpError(409, 'ticket_not_active', `针单已${ticket.status}`, { supersededBy: ticket.supersededBy });
    }

    const color = canonicalColor(body.color ?? body.thread ?? ticket.primaryColor);
    const thread = ticket.threads[color];
    if (!thread) throw new HttpError(400, 'unknown_color', `针单没有颜色 ${color}`);

    const cell = this.normalizeReportCell(ticket, body, color) || this.nextCellView(ticket, color);
    if (!cell) throw new HttpError(409, 'nothing_to_return', '这根线已到出口，没有未刺针单可交回');

    const stitchedMatch = thread.stitched.find((p) =>
      samePoint(p, cell) || samePoint({ row: p.localRow, col: p.localCol }, cell));
    if (stitchedMatch) {
      throw new HttpError(409, 'already_stitched', '已确认的针不能当作未刺格交回', {
        cell, stitchedMatch, currentVersion: ticket.version
      });
    }

    const globalIdx = thread.path.findIndex((p) => samePoint(p, cell));
    const localIdx = thread.localPath.findIndex((p) => samePoint(p, cell));
    const idx = globalIdx >= 0 ? globalIdx : localIdx;
    if (idx < 0) throw new HttpError(422, 'cell_not_on_diagram', '交回格不在针图上', { cell });
    if (idx < thread.cursor) {
      throw new HttpError(409, 'already_stitched', '该格已经刺下，不能当未刺交回', { cell, index: idx, cursor: thread.cursor });
    }
    if (idx !== thread.cursor && body.allowLater !== true) {
      throw new HttpError(409, 'not_next_unstitched_cell', '只能从当前下一格连同后续未刺针迹一起交回', {
        expectedCell: this.nextCellView(ticket, color), suppliedIndex: idx
      });
    }

    // Explicit unstitched list protection.
    if (Array.isArray(body.unstitchedCells)) {
      for (const raw of body.unstitchedCells) {
        const p = normPoint(raw, this.db.orders[ticket.orderId]?.coordOrder || 'rc');
        const stitched = thread.stitched.some((s) => samePoint(s, p) || samePoint({ row: s.localRow, col: s.localCol }, p));
        if (stitched) throw new HttpError(409, 'already_stitched', '交回清单包含已确认针迹', { cell: p });
      }
    }

    const now = new Date().toISOString();
    ticket.status = 'returned';
    ticket.version += 1;
    ticket.reportVersion = ticket.version;
    ticket.returnInfo = {
      at: now,
      color,
      cell,
      fromIndex: idx,
      preservedStitches: thread.cursor,
      reason: body.reason || body.note || 'return-for-revision',
      includesCell: true
    };
    ticket.updatedAt = now;
    ticket.nextCell = null;
    if (requestId) ticket.requestIds[requestId] = id;
    this.addEvent('ticket.returned', { orderId: ticket.orderId, ticketId: id, version: ticket.version, cell });
    this.save();
    return this.getTicket(id);
  }

  // ---------- Built-in acceptance fixtures ----------

  createRedBandSites(body = {}) {
    const { first, tail } = redBandFixture();
    const common = {
      rows: 2,
      left: { red: { row: 0, col: 0 } },
      right: { red: { row: 0 } },
      marks: body.marks || [],
      forbidden: body.forbidden || [],
      mode: 'parallel'
    };

    const orderAId = body.orderAId || 'red-order-a';
    const orderBStraightId = body.straightId || 'red-site-straight';
    const orderBSwitchId = body.switchId || 'red-site-switch';

    for (const id of [orderAId, orderBStraightId, orderBSwitchId]) delete this.db.orders[id];

    // Order A: early bottom emblem forces first switch; tail-up can return to top.
    const orderA = this.createOrder({
      id: orderAId,
      name: '红带甲单：靠前徽记迫使换行',
      ...common,
      marks: [{ row: 1, col: 0, color: 'red' }],
      segments: [clone(first), clone(tail)]
    });
    const ticketAFirst = this.claim(orderAId, { segmentIndex: 0, routeId: 'first-switch', workerId: 'hoop-a1' });
    // Actually stitch the first segment; no fake records.
    let ta = ticketAFirst;
    while (ta.nextCell) ta = this.report(ta.id, { expectedVersion: ta.version });

    // Order B sites: emblem is the shared first cell.
    const bMark = [{ row: 0, col: 0, color: 'red' }];
    this.createOrder({
      id: orderBStraightId,
      name: '红带乙单现场一：首段已签直走',
      ...common,
      marks: bMark,
      segments: [clone(first), clone(tail)]
    });
    this.claim(orderBStraightId, { segmentIndex: 0, routeId: 'first-straight', workerId: 'hoop-b-s' });

    this.createOrder({
      id: orderBSwitchId,
      name: '红带乙单现场二：首段已签换行',
      ...common,
      marks: bMark,
      segments: [clone(first), clone(tail)]
    });
    this.claim(orderBSwitchId, { segmentIndex: 0, routeId: 'first-switch', workerId: 'hoop-b-x' });

    const change = {
      ...straightTailOnlyChange(),
      target: 'tail',
      baseVersion: 1,
      requestId: 'same-tail-change-template'
    };

    return {
      ok: true,
      orderA: this.getOrder(orderAId),
      straightSite: this.getOrder(orderBStraightId),
      switchSite: this.getOrder(orderBSwitchId),
      sameChange: change,
      // Convenience aliases used by different test vocabularies.
      straightOrderId: orderBStraightId,
      switchOrderId: orderBSwitchId,
      orderAId,
      change
    };
  }

  createThreeSegmentOrder(body = {}) {
    const id = body.id || 'three-segment-demo';
    delete this.db.orders[id];
    const cell = (row, col) => ({ row, col });
    const mk = (prefix, rowIn, rowOut) => ({
      id: prefix,
      rows: 2,
      cols: 2,
      threads: {
        red: {
          routes: [{
            id: `${prefix}-${rowIn}-${rowOut}`,
            kind: rowIn === rowOut ? 'straight' : 'switch',
            path: rowIn === rowOut
              ? [cell(rowIn, 0), cell(rowIn, 1)]
              : [cell(rowIn, 0), cell(rowIn, 1), cell(rowOut, 1)]
          }]
        }
      }
    });
    const segments = body.segments || [mk('a', 0, 0), mk('b', 0, 0), mk('c', 0, 0)];
    const order = this.createOrder({
      id,
      name: body.name || '三段新样验题单',
      rows: 2,
      left: { red: 0 },
      right: { red: body.rightRow ?? 0 },
      marks: body.marks || [],
      forbidden: body.forbidden || [],
      segments
    });
    return { ok: true, order: this.getOrder(id), orderId: id };
  }

  snapshot() {
    return { ...this.db, serverTime: new Date().toISOString() };
  }
}
