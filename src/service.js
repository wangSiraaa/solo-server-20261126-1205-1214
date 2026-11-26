'use strict';

const {
  HttpError,
  badRequest,
  forbidden,
  notFound,
  conflict,
  unprocessable,
} = require('./errors');
const {
  isoNow,
  timeMs,
  generatedId,
  stableHash,
  uniqueStrings,
  allocateIntegerPoints,
} = require('./utils');

const COMMISSION_TYPES = new Set(['VIEWING', 'NEGOTIATION', 'SIGNING']);
const WORK_TYPES = new Set(['VIEWING', 'NEGOTIATION', 'SIGNING']);
const REGIMES = new Set(['FIRST_PROTECTION', 'RELAY_CONTRIBUTION']);

class PlatformService {
  constructor(store, options = {}) {
    this.store = store;
    this.state = store.state;
    this.clock = options.clock || (() => new Date());
  }

  now() {
    return isoNow(this.clock);
  }

  commit() {
    if (this.store && typeof this.store.save === 'function') this.store.save();
  }

  // ---------- generic lookups ----------

  find(collection, id, required = true) {
    const row = this.state[collection]?.find((item) => item.id === id);
    if (!row && required) throw notFound('NOT_FOUND', `${collection} ${id} 不存在`);
    return row || null;
  }

  requireId(input, field) {
    if (!input || typeof input[field] !== 'string' || !input[field].trim()) {
      throw badRequest('MISSING_ID', `缺少 ${field}`);
    }
    return input[field].trim();
  }

  assignId(collection, prefix, requestedId) {
    if (requestedId !== undefined && requestedId !== null) {
      if (typeof requestedId !== 'string' || !requestedId.trim()) {
        throw badRequest('INVALID_ID', 'ID 必须是非空字符串');
      }
      const id = requestedId.trim();
      if (this.state[collection].some((row) => row.id === id)) {
        throw conflict('DUPLICATE_ID', `${collection} ID 已存在: ${id}`);
      }
      return id;
    }
    let id = generatedId(prefix);
    while (this.state[collection].some((row) => row.id === id)) id = generatedId(prefix);
    return id;
  }

  requireTenant(tenantId) {
    const tenant = this.state.tenants.find((t) => t.id === tenantId);
    if (!tenant) throw notFound('TENANT_NOT_FOUND', `租客 ${tenantId} 不存在`);
    return tenant;
  }

  requireStore(storeId) {
    const store = this.state.stores.find((s) => s.id === storeId);
    if (!store) throw notFound('STORE_NOT_FOUND', `门店 ${storeId} 不存在`);
    return store;
  }

  requireAgent(agentId, storeId = null) {
    const agent = this.state.agents.find((a) => a.id === agentId);
    if (!agent) throw notFound('AGENT_NOT_FOUND', `经纪人 ${agentId} 不存在`);
    if (storeId && agent.storeId !== storeId) {
      throw forbidden('AGENT_STORE_MISMATCH', `${agentId} 不属于门店 ${storeId}`);
    }
    return agent;
  }

  resolveStoreActor(actor) {
    if (!actor) throw badRequest('MISSING_ACTOR', '缺少门店操作人');
    if (typeof actor === 'string') {
      if (this.state.stores.some((s) => s.id === actor)) return { storeId: actor, actorId: actor };
      const agent = this.state.agents.find((a) => a.id === actor);
      if (agent) return { storeId: agent.storeId, actorId: actor, agent };
      const clerk = this.state.clerks.find((c) => c.id === actor);
      if (clerk) return { storeId: clerk.storeId, actorId: actor, clerk };
      throw notFound('ACTOR_NOT_FOUND', `门店操作人 ${actor} 不存在`);
    }
    if (actor.storeId) {
      this.requireStore(actor.storeId);
      if (actor.agentId) this.requireAgent(actor.agentId, actor.storeId);
      return { storeId: actor.storeId, actorId: actor.agentId || actor.clerkId || actor.storeId };
    }
    if (actor.agentId) {
      const agent = this.requireAgent(actor.agentId);
      return { storeId: agent.storeId, actorId: agent.id, agent };
    }
    if (actor.clerkId) {
      const clerk = this.state.clerks.find((c) => c.id === actor.clerkId);
      if (!clerk) throw notFound('CLERK_NOT_FOUND', `店员 ${actor.clerkId} 不存在`);
      return { storeId: clerk.storeId, actorId: clerk.id, clerk };
    }
    throw badRequest('INVALID_ACTOR', '无法识别门店操作人');
  }

  requireRoom(roomId) {
    const room = this.state.rooms.find((r) => r.id === roomId);
    if (!room) throw notFound('ROOM_NOT_FOUND', `房间/空间 ${roomId} 不存在`);
    return room;
  }

  requireOrder(orderId) {
    const order = this.state.searchOrders.find((o) => o.id === orderId);
    if (!order) throw notFound('SEARCH_ORDER_NOT_FOUND', `找房单 ${orderId} 不存在`);
    return order;
  }

  activeMandate(order, personId, atIso = this.now()) {
    const at = timeMs(atIso);
    return this.state.companionMandates.find((mandate) => {
      if (mandate.searchOrderId !== order.id || mandate.personId !== personId) return false;
      if (mandate.status !== 'AUTHORIZED') return false;
      if (timeMs(mandate.validFrom) > at) return false;
      if (mandate.validTo && timeMs(mandate.validTo) < at) return false;
      return true;
    });
  }

  roomScopeMatches(scopeRoomIds, roomId) {
    return !Array.isArray(scopeRoomIds) || scopeRoomIds.length === 0 || scopeRoomIds.includes(roomId);
  }

  assertStoreWorkAuthorized(orderId, storeId, workType, roomIds) {
    if (!WORK_TYPES.has(workType)) throw badRequest('INVALID_WORK_TYPE', `未知工作类型 ${workType}`);

    const appointmentCovered =
      workType === 'VIEWING' &&
      this.state.appointments.some(
        (appointment) =>
          appointment.searchOrderId === orderId &&
          appointment.storeId === storeId &&
          roomIds.every((roomId) => appointment.plannedSpaceIds.includes(roomId)),
      );

    const explicitAuth = this.state.storeAuthorizations.some((auth) => {
      if (auth.searchOrderId !== orderId || auth.storeId !== storeId || auth.status !== 'ACTIVE') return false;
      if (!auth.scopes.includes(workType)) return false;
      return roomIds.every((roomId) => this.roomScopeMatches(auth.roomIds, roomId));
    });

    const handoffGrant = this.state.handoffs.some((handoff) => {
      if (handoff.searchOrderId !== orderId || handoff.toStoreId !== storeId) return false;
      if (handoff.status !== 'CONFIRMED') return false;
      return roomIds.every((roomId) =>
        handoff.items.some(
          (item) =>
            item.roomId === roomId &&
            item.workType === workType &&
            (item.action || 'INHERIT_FUTURE') === 'INHERIT_FUTURE',
        ),
      );
    });

    if (!appointmentCovered && !explicitAuth && !handoffGrant) {
      throw forbidden(
        'STORE_NOT_AUTHORIZED',
        `门店 ${storeId} 未获授权承接找房单 ${orderId} 的 ${workType} 工作`,
        { orderId, storeId, workType, roomIds },
      );
    }
  }

  // ---------- master data ----------

  health() {
    return {
      ok: true,
      service: 'shared-rental-viewing-commission',
      time: this.now(),
      counts: {
        rooms: this.state.rooms.length,
        listings: this.state.listings.length,
        searchOrders: this.state.searchOrders.length,
        visits: this.state.visits.length,
        leases: this.state.leases.length,
        commissionItems: this.state.commissionItems.length,
        settlements: this.state.settlements.length,
      },
    };
  }

  listRooms() {
    return this.state.rooms;
  }

  listListings() {
    return this.state.listings;
  }

  getAccount(accountId) {
    const account = this.state.accounts.find((a) => a.id === accountId);
    if (!account) throw notFound('ACCOUNT_NOT_FOUND', `模拟账户 ${accountId} 不存在`);
    return {
      ...account,
      ledgerEntries: this.state.ledgerEntries.filter((entry) => entry.accountId === accountId),
    };
  }

  createRoom(input = {}) {
    const id = this.assignId('rooms', 'R', input.id);
    const number = this.requireId(input, 'number');
    const name = this.requireId(input, 'name');
    const kind = input.kind || 'BEDROOM';
    const room = {
      id,
      unitId: input.unitId || `U-${number.split('-')[0]}`,
      number,
      name,
      kind,
      commissionable: input.commissionable ?? kind === 'BEDROOM',
      aliases: Array.isArray(input.aliases) ? input.aliases : [],
      createdAt: this.now(),
    };
    this.state.rooms.push(room);
    this.commit();
    return room;
  }

  renameRoom(roomId, name) {
    const room = this.requireRoom(roomId);
    if (!name || typeof name !== 'string') throw badRequest('INVALID_NAME', '新名称不能为空');
    if (name !== room.name) {
      room.aliases = [...new Set([...(room.aliases || []), room.name])];
      room.name = name;
      room.renamedAt = this.now();
    }
    this.commit();
    return room;
  }

  createListing(input = {}) {
    const id = this.assignId('listings', 'L', input.id);
    const title = this.requireId(input, 'title');
    const roomIds = uniqueStrings(input.roomIds || [], 'roomIds');
    if (roomIds.length === 0) throw badRequest('EMPTY_LISTING', '挂牌至少要指向一个物理房间');
    roomIds.forEach((roomId) => this.requireRoom(roomId));
    const commonAreaIds = uniqueStrings(input.commonAreaIds || [], 'commonAreaIds');
    commonAreaIds.forEach((roomId) => this.requireRoom(roomId));

    const viewingPointsByRoom = {};
    for (const roomId of roomIds) {
      const value = input.viewingPointsByRoom?.[roomId] ?? input.viewingPointsPerRoom;
      viewingPointsByRoom[roomId] =
        value === undefined ? 4 : Number.isInteger(value) ? value : Number(value);
      if (!Number.isInteger(viewingPointsByRoom[roomId]) || viewingPointsByRoom[roomId] < 0) {
        throw badRequest('INVALID_POINTS', `${roomId} 带看点数必须是非负整数`);
      }
    }

    const negotiationPointsByRoom = {};
    for (const roomId of roomIds) {
      const value = input.negotiationPointsByRoom?.[roomId] ?? 0;
      negotiationPointsByRoom[roomId] = Number(value);
      if (!Number.isInteger(negotiationPointsByRoom[roomId]) || negotiationPointsByRoom[roomId] < 0) {
        throw badRequest('INVALID_POINTS', `${roomId} 议价点数必须是非负整数`);
      }
    }

    const listing = {
      id,
      ownerId: input.ownerId || null,
      title,
      kind: input.kind || 'ROOM',
      roomIds,
      commonAreaIds,
      viewingPointsByRoom,
      negotiationPointsByRoom,
      active: input.active ?? true,
      createdAt: this.now(),
    };
    this.state.listings.push(listing);
    this.commit();
    return listing;
  }

  // ---------- search order and client-side authority ----------

  createSearchOrder(input = {}) {
    const tenantId = this.requireId(input, 'tenantId');
    this.requireTenant(tenantId);
    const regime = input.regime || 'FIRST_PROTECTION';
    if (!REGIMES.has(regime)) throw badRequest('INVALID_REGIME', `未知分佣制度 ${regime}`);

    const id = this.assignId('searchOrders', 'SO', input.id);
    const order = {
      id,
      tenantId,
      regime,
      status: 'OPEN',
      title: input.title || `找房单 ${id}`,
      note: input.note || '',
      createdAt: this.now(),
    };
    this.state.searchOrders.push(order);

    if (Array.isArray(input.authorizedStores)) {
      for (const grant of input.authorizedStores) {
        this.addStoreAuthorization(id, { ...grant, tenantId });
      }
    }

    this.commit();
    return order;
  }

  addCompanionMandate(orderId, input = {}) {
    const order = this.requireOrder(orderId);
    const tenantId = input.tenantId || input.actorId;
    if (tenantId !== order.tenantId) {
      throw forbidden('ONLY_TENANT_CAN_AUTHORIZE_COMPANION', '只有本单租客可以办理同行委托');
    }
    const personId = this.requireId(input, 'personId');
    let profile = this.state.companionProfiles.find((p) => p.id === personId);
    if (!profile) {
      profile = { id: personId, name: input.personName || personId, phoneExt: input.phoneExt || null };
      this.state.companionProfiles.push(profile);
    }

    let roomIds = null;
    if (input.roomIds !== undefined && input.roomIds !== null) {
      roomIds = uniqueStrings(input.roomIds, 'roomIds');
      roomIds.forEach((roomId) => this.requireRoom(roomId));
    }

    const validFrom = input.validFrom || this.now();
    const validTo = input.validTo || new Date(this.clock().getTime() + 30 * 86400000).toISOString();
    if (timeMs(validTo) <= timeMs(validFrom)) {
      throw badRequest('INVALID_VALIDITY_WINDOW', '委托结束时间必须晚于开始时间');
    }

    const mandate = {
      id: this.assignId('companionMandates', 'MDT', input.id),
      searchOrderId: orderId,
      tenantId,
      personId,
      personName: profile.name,
      authority: 'VIEWING_PROXY_AND_SITE_CONFIRMATION',
      roomIds,
      validFrom,
      validTo,
      status: 'AUTHORIZED',
      createdAt: this.now(),
    };
    this.state.companionMandates.push(mandate);
    this.commit();
    return mandate;
  }

  revokeCompanionMandate(mandateId, tenantId) {
    const mandate = this.find('companionMandates', mandateId);
    const order = this.requireOrder(mandate.searchOrderId);
    if (tenantId !== order.tenantId) throw forbidden('NOT_ORDER_TENANT', '只有本单租客可以撤销委托');
    mandate.status = 'REVOKED';
    mandate.revokedAt = this.now();
    this.commit();
    return mandate;
  }

  addStoreAuthorization(orderIdOrInput, inputMaybe = {}) {
    const input = typeof orderIdOrInput === 'object' ? orderIdOrInput : inputMaybe;
    const orderId =
      typeof orderIdOrInput === 'string' ? orderIdOrInput : this.requireId(input, 'searchOrderId');
    const order = this.requireOrder(orderId);
    const tenantId = input.tenantId || input.actorId;
    if (tenantId && tenantId !== order.tenantId) {
      throw forbidden('ONLY_TENANT_CAN_AUTHORIZE_STORE', '只有本单租客可以授权门店');
    }
    const storeId = this.requireId(input, 'storeId');
    this.requireStore(storeId);
    const scopes = uniqueStrings(input.scopes || ['VIEWING'], 'scopes');
    scopes.forEach((scope) => {
      if (!WORK_TYPES.has(scope)) throw badRequest('INVALID_SCOPE', `未知授权范围 ${scope}`);
    });
    let roomIds = null;
    if (input.roomIds !== undefined && input.roomIds !== null) {
      roomIds = uniqueStrings(input.roomIds, 'roomIds');
      roomIds.forEach((roomId) => this.requireRoom(roomId));
    }

    const auth = {
      id: this.assignId('storeAuthorizations', 'AUTH', input.id),
      searchOrderId: orderId,
      storeId,
      scopes,
      roomIds,
      source: input.source || 'TENANT_GRANT',
      sourceId: input.sourceId || null,
      status: 'ACTIVE',
      createdAt: this.now(),
      createdBy: tenantId || 'SYSTEM',
    };
    this.state.storeAuthorizations.push(auth);
    this.commit();
    return auth;
  }

  // ---------- appointment and protection ----------

  createAppointment(input = {}) {
    const orderId = this.requireId(input, 'searchOrderId');
    const order = this.requireOrder(orderId);
    const listingId = this.requireId(input, 'listingId');
    const listing = this.find('listings', listingId);
    if (!listing) throw notFound('LISTING_NOT_FOUND', `挂牌 ${listingId} 不存在`);

    const storeId = this.requireId(input, 'storeId');
    this.requireStore(storeId);
    const agentId = this.requireId(input, 'agentId');
    this.requireAgent(agentId, storeId);

    const listingSpaces = [...listing.roomIds, ...(listing.commonAreaIds || [])];
    let plannedSpaceIds = input.plannedSpaceIds || input.plannedRoomIds || listingSpaces;
    plannedSpaceIds = uniqueStrings(plannedSpaceIds, 'plannedSpaceIds');
    plannedSpaceIds.forEach((spaceId) => {
      this.requireRoom(spaceId);
      if (!listingSpaces.includes(spaceId)) {
        throw badRequest(
          'SPACE_NOT_IN_LISTING',
          `预约空间 ${spaceId} 不属于挂牌 ${listingId}；请另约对应挂牌，避免混淆`,
        );
      }
    });

    const appointment = {
      id: this.assignId('appointments', 'APT', input.id),
      searchOrderId: orderId,
      listingId,
      storeId,
      agentId,
      scheduledAt: input.scheduledAt || this.now(),
      plannedSpaceIds,
      participantPlan: input.participantPlan || 'TENANT_OR_AUTHORIZED_COMPANION',
      status: 'SCHEDULED',
      commissionSnapshot: {
        viewingPointsByRoom: { ...listing.viewingPointsByRoom },
        negotiationPointsByRoom: { ...(listing.negotiationPointsByRoom || {}) },
      },
      createdAt: this.now(),
    };
    this.state.appointments.push(appointment);
    this.commit();
    return appointment;
  }

  expireProtections(orderId = null, atMs = this.clock().getTime()) {
    for (const protection of this.state.protections) {
      if (orderId && protection.searchOrderId !== orderId) continue;
      let changed = false;
      for (const roomState of protection.roomStates) {
        if (roomState.status === 'PENDING' && timeMs(protection.promisedEnd) < atMs) {
          roomState.status = 'MISSED';
          changed = true;
        }
      }
      const statuses = protection.roomStates.map((rs) => rs.status);
      let nextStatus = protection.status;
      if (statuses.every((s) => s === 'FULFILLED')) nextStatus = 'FULFILLED';
      else if (statuses.every((s) => s === 'MISSED')) nextStatus = 'MISSED';
      else if (statuses.some((s) => s === 'FULFILLED')) nextStatus = 'PARTIALLY_FULFILLED';
      else nextStatus = 'ACTIVE';
      if (nextStatus !== protection.status) {
        protection.status = nextStatus;
        changed = true;
      }
      if (changed) protection.updatedAt = this.now();
    }
  }

  createProtection(input = {}) {
    const orderId = this.requireId(input, 'searchOrderId');
    this.requireOrder(orderId);
    const storeId = this.requireId(input, 'storeId');
    this.requireStore(storeId);
    const agentId = this.requireId(input, 'agentId');
    this.requireAgent(agentId, storeId);
    const roomIds = uniqueStrings(input.roomIds || [], 'roomIds');
    if (roomIds.length === 0) throw badRequest('EMPTY_PROTECTION', '保护申请必须指定房间');
    roomIds.forEach((roomId) => {
      const room = this.requireRoom(roomId);
      if (!room.commissionable) {
        throw unprocessable(
          'NON_COMMISSIONABLE_SPACE',
          `${roomId} 是 ${room.kind}，不是可按间计佣的独立房间，不能申请带看份额保护`,
        );
      }
    });

    const promisedStart = input.promisedStart || this.now();
    const promisedEnd = this.requireId(input, 'promisedEnd');
    if (timeMs(promisedEnd) <= timeMs(promisedStart)) {
      throw badRequest('INVALID_PROMISE_WINDOW', '承诺时段结束必须晚于开始');
    }

    // An appointment or tenant grant proves the store is working this order.
    this.assertStoreWorkAuthorized(orderId, storeId, 'VIEWING', roomIds);
    this.expireProtections(orderId);

    const conflicts = [];
    for (const roomId of roomIds) {
      const conflict = this.state.protections.find((protection) => {
        if (protection.searchOrderId !== orderId) return false;
        return protection.roomStates.some(
          (rs) => rs.roomId === roomId && ['PENDING', 'FULFILLED'].includes(rs.status),
        );
      });
      if (conflict) {
        const roomState = conflict.roomStates.find((rs) => rs.roomId === roomId);
        conflicts.push({ roomId, existingProtectionId: conflict.id, status: roomState.status });
      }
    }
    if (conflicts.length > 0) {
      throw conflict(
        'PROTECTION_CONFLICT',
        '同一找房单、同一物理房间已有有效首带保护；不同挂牌指向同一房间也不能重复保护',
        { conflicts },
      );
    }

    const protection = {
      id: this.assignId('protections', 'PRT', input.id),
      searchOrderId: orderId,
      listingId: input.listingId || null,
      storeId,
      agentId,
      promisedStart,
      promisedEnd,
      roomStates: roomIds.map((roomId) => ({
        roomId,
        status: 'PENDING',
        visitId: null,
        broughtAt: null,
        fulfilledAt: null,
      })),
      status: 'ACTIVE',
      createdAt: this.now(),
    };
    this.state.protections.push(protection);
    this.commit();
    return protection;
  }

  // ---------- on-site visit and mutual confirmation ----------

  submitVisit(input = {}) {
    const appointmentId = this.requireId(input, 'appointmentId');
    const appointment = this.find('appointments', appointmentId);
    if (!appointment) throw notFound('APPOINTMENT_NOT_FOUND', `预约 ${appointmentId} 不存在`);
    const order = this.requireOrder(appointment.searchOrderId);

    const agentId = input.agentId || appointment.agentId;
    this.requireAgent(agentId, appointment.storeId);

    const participantInput = input.participant || {
      type: input.participantType,
      id: input.participantId || input.personId,
      mandateId: input.mandateId,
    };
    if (!participantInput.type || !participantInput.id) {
      throw badRequest('MISSING_PARTICIPANT', '必须记录实际到场的租客或受托同住人');
    }

    let participant;
    if (participantInput.type === 'TENANT') {
      if (participantInput.id !== order.tenantId) {
        throw forbidden('NOT_ORDER_TENANT', '到场租客不是本找房单当事人');
      }
      participant = { type: 'TENANT', id: order.tenantId };
    } else if (participantInput.type === 'COMPANION') {
      const personId = participantInput.id;
      const mandate = this.activeMandate(order, personId, input.occurredAt || this.now());
      if (!mandate) {
        throw forbidden(
          'NO_VIEWING_MANDATE',
          `同住人 ${personId} 未持有本找房单有效期内的同行委托，不能代看`,
        );
      }
      const enteredRoomIds = input.enteredRoomIds || [];
      const outsideMandate = enteredRoomIds.find(
        (roomId) => !this.roomScopeMatches(mandate.roomIds, roomId),
      );
      if (outsideMandate) {
        throw forbidden(
          'ROOM_OUTSIDE_MANDATE',
          `同行委托不覆盖房间 ${outsideMandate}，受托人不能代看该房间`,
        );
      }
      participant = {
        type: 'AUTHORIZED_COMPANION',
        id: personId,
        mandateId: mandate.id,
        confirmedByMandate: true,
      };
    } else {
      throw badRequest('INVALID_PARTICIPANT_TYPE', 'participant.type 必须是 TENANT 或 COMPANION');
    }

    const enteredRoomIds = uniqueStrings(input.enteredRoomIds || [], 'enteredRoomIds');
    const doorwayOnlyRoomIds = uniqueStrings(input.doorwayOnlyRoomIds || [], 'doorwayOnlyRoomIds');
    const notSeenRoomIds = uniqueStrings(input.notSeenRoomIds || [], 'notSeenRoomIds');

    for (const roomId of [...enteredRoomIds, ...doorwayOnlyRoomIds, ...notSeenRoomIds]) {
      this.requireRoom(roomId);
    }
    const all = [
      ...enteredRoomIds.map((id) => [id, 'ENTERED']),
      ...doorwayOnlyRoomIds.map((id) => [id, 'DOORWAY_ONLY']),
      ...notSeenRoomIds.map((id) => [id, 'NOT_SEEN']),
    ];
    const seen = new Map();
    for (const [roomId, status] of all) {
      if (seen.has(roomId) && seen.get(roomId) !== status) {
        throw badRequest('ROOM_RESULT_CONFLICT', `房间 ${roomId} 不能同时记为进入、门口看和未看`);
      }
      seen.set(roomId, status);
    }

    const occurredAt = input.occurredAt || this.now();
    timeMs(occurredAt);

    const visit = {
      id: this.assignId('visits', 'VST', input.id),
      appointmentId,
      searchOrderId: order.id,
      listingId: appointment.listingId,
      storeId: appointment.storeId,
      bookedAgentId: appointment.agentId,
      agentId,
      participant,
      occurredAt,
      enteredRoomIds,
      doorwayOnlyRoomIds,
      notSeenRoomIds,
      note: input.note || '',
      agentSignature: { agentId, signedAt: this.now() },
      clientConfirmation: null,
      status: 'PENDING_CONFIRMATION',
      createdAt: this.now(),
    };
    this.state.visits.push(visit);
    appointment.status = 'VISIT_RECORDED_PENDING_CONFIRMATION';
    this.commit();
    return visit;
  }

  confirmVisit(visitId, input = {}) {
    const visit = this.find('visits', visitId);
    if (!visit) throw notFound('VISIT_NOT_FOUND', `现场回条 ${visitId} 不存在`);
    const order = this.requireOrder(visit.searchOrderId);
    if (visit.status !== 'PENDING_CONFIRMATION') {
      throw conflict('VISIT_ALREADY_CLOSED', `现场回条已为 ${visit.status}，不能重复确认`);
    }

    const actorId = this.requireId(input, 'actorId');
    const decision = input.decision || 'CONFIRMED';
    if (!['CONFIRMED', 'DISPUTED'].includes(decision)) {
      throw badRequest('INVALID_DECISION', 'decision 必须是 CONFIRMED 或 DISPUTED');
    }

    const isTenant = actorId === order.tenantId;
    const isActualCompanion =
      visit.participant.type === 'AUTHORIZED_COMPANION' && visit.participant.id === actorId;
    if (!isTenant && !isActualCompanion) {
      throw forbidden(
        'ONLY_CLIENT_SIDE_CAN_CONFIRM',
        '必须由本单租客或实际到场且有委托的同住人确认现场回条',
      );
    }

    visit.clientConfirmation = {
      actorId,
      role: isTenant ? 'TENANT' : 'AUTHORIZED_COMPANION',
      decision,
      note: input.note || '',
      confirmedAt: this.now(),
    };

    if (decision === 'DISPUTED') {
      visit.status = 'DISPUTED';
      this.find('appointments', visit.appointmentId).status = 'VISIT_DISPUTED';
      this.commit();
      return visit;
    }

    visit.status = 'CONFIRMED';
    const appointment = this.find('appointments', visit.appointmentId);
    appointment.status = 'COMPLETED_CONFIRMED';

    for (const roomId of visit.enteredRoomIds) {
      const room = this.requireRoom(roomId);
      if (!room.commissionable) continue;

      this.state.contributions.push({
        id: this.assignId('contributions', 'WC', undefined),
        searchOrderId: order.id,
        roomId,
        type: 'VIEWING',
        storeId: visit.storeId,
        agentId: visit.agentId,
        sourceType: 'VISIT',
        sourceId: visit.id,
        appointmentId: visit.appointmentId,
        listingId: visit.listingId,
        occurredAt: visit.occurredAt,
        confirmedAt: visit.clientConfirmation.confirmedAt,
      });

      for (const protection of this.state.protections) {
        if (protection.searchOrderId !== order.id || protection.storeId !== visit.storeId) continue;
        const roomState = protection.roomStates.find((rs) => rs.roomId === roomId);
        if (!roomState || roomState.status !== 'PENDING') continue;
        const broughtAt = timeMs(visit.occurredAt);
        if (
          broughtAt >= timeMs(protection.promisedStart) &&
          broughtAt <= timeMs(protection.promisedEnd)
        ) {
          roomState.status = 'FULFILLED';
          roomState.visitId = visit.id;
          roomState.broughtAt = visit.occurredAt;
          roomState.fulfilledAt = visit.clientConfirmation.confirmedAt;
        }
      }
    }

    this.expireProtections(order.id);
    this.commit();
    return visit;
  }

  // ---------- negotiation and handoff ----------

  createNegotiation(input = {}) {
    const orderId = this.requireId(input, 'searchOrderId');
    this.requireOrder(orderId);
    const storeId = this.requireId(input, 'storeId');
    this.requireStore(storeId);
    const agentId = this.requireId(input, 'agentId');
    this.requireAgent(agentId, storeId);
    const roomIds = uniqueStrings(input.roomIds || [], 'roomIds');
    if (roomIds.length === 0) throw badRequest('EMPTY_NEGOTIATION', '议价记录必须关联至少一个房间');
    roomIds.forEach((roomId) => this.requireRoom(roomId));
    this.assertStoreWorkAuthorized(orderId, storeId, 'NEGOTIATION', roomIds);

    const negotiation = {
      id: this.assignId('negotiations', 'NEG', input.id),
      searchOrderId: orderId,
      storeId,
      agentId,
      roomIds,
      kind: input.kind || 'OFFER',
      terms: input.terms || {},
      note: input.note || '',
      occurredAt: input.occurredAt || this.now(),
      status: 'PENDING_TENANT_CONFIRMATION',
      tenantConfirmation: null,
      createdAt: this.now(),
    };
    this.state.negotiations.push(negotiation);
    this.commit();
    return negotiation;
  }

  confirmNegotiation(negotiationId, tenantId) {
    const negotiation = this.find('negotiations', negotiationId);
    const order = this.requireOrder(negotiation.searchOrderId);
    if (tenantId !== order.tenantId) throw forbidden('NOT_ORDER_TENANT', '只有本单租客可以确认议价');
    if (negotiation.status === 'CONFIRMED') {
      throw conflict('NEGOTIATION_ALREADY_CONFIRMED', '议价记录已确认');
    }
    negotiation.status = 'CONFIRMED';
    negotiation.tenantConfirmation = { tenantId, confirmedAt: this.now() };

    for (const roomId of negotiation.roomIds) {
      this.state.contributions.push({
        id: this.assignId('contributions', 'WC', undefined),
        searchOrderId: order.id,
        roomId,
        type: 'NEGOTIATION',
        storeId: negotiation.storeId,
        agentId: negotiation.agentId,
        sourceType: 'NEGOTIATION',
        sourceId: negotiation.id,
        occurredAt: negotiation.occurredAt,
        confirmedAt: negotiation.tenantConfirmation.confirmedAt,
        effective: negotiation.kind === 'TERM_AGREED',
      });
    }
    this.commit();
    return negotiation;
  }

  createHandoff(input = {}) {
    const orderId = this.requireId(input, 'searchOrderId');
    const order = this.requireOrder(orderId);
    const toStoreId = this.requireId(input, 'toStoreId');
    this.requireStore(toStoreId);
    let fromStoreId = input.fromStoreId || null;
    if (fromStoreId) this.requireStore(fromStoreId);

    const items = Array.isArray(input.items) ? input.items : [];
    if (items.length === 0) throw badRequest('EMPTY_HANDOFF', '交接必须列明可继承的房间与工作');
    const normalizedItems = items.map((item) => {
      if (!item.roomId || !item.workType) {
        throw badRequest('INVALID_HANDOFF_ITEM', '每个交接项必须包含 roomId 和 workType');
      }
      this.requireRoom(item.roomId);
      if (!WORK_TYPES.has(item.workType)) {
        throw badRequest('INVALID_WORK_TYPE', `未知交接工作类型 ${item.workType}`);
      }
      if (item.sourceId) {
        const contribution = this.state.contributions.find(
          (c) =>
            c.sourceId === item.sourceId &&
            c.roomId === item.roomId &&
            c.type === item.workType &&
            c.searchOrderId === orderId,
        );
        if (!contribution) {
          throw badRequest(
            'HANDOFF_SOURCE_NOT_FOUND',
            `交接来源 ${item.sourceId} 与房间/工作类型不匹配`,
          );
        }
        fromStoreId = fromStoreId || contribution.storeId;
      }
      return {
        roomId: item.roomId,
        workType: item.workType,
        sourceId: item.sourceId || null,
        action: item.action || 'INHERIT_FUTURE',
        note: item.note || '',
      };
    });

    const handoff = {
      id: this.assignId('handoffs', 'HND', input.id),
      searchOrderId: orderId,
      fromStoreId,
      toStoreId,
      items: normalizedItems,
      tenantConfirmation: null,
      receiverConfirmation: null,
      status: 'PENDING_BOTH_CONFIRMATIONS',
      createdAt: this.now(),
    };

    if (input.tenantConfirmed && (input.tenantId || input.actorId) === order.tenantId) {
      handoff.tenantConfirmation = { actorId: order.tenantId, confirmedAt: this.now() };
    }
    if (input.receiverAccepted) {
      const actor = this.resolveStoreActor(input.receiverActor || { storeId: toStoreId });
      if (actor.storeId !== toStoreId) throw forbidden('WRONG_RECEIVER_STORE', '承接确认门店不一致');
      handoff.receiverConfirmation = { actorId: actor.actorId, storeId: toStoreId, acceptedAt: this.now() };
    }
    if (handoff.tenantConfirmation && handoff.receiverConfirmation) {
      handoff.status = 'CONFIRMED';
      handoff.confirmedAt = this.now();
    }

    this.state.handoffs.push(handoff);
    this.commit();
    return handoff;
  }

  confirmHandoffByTenant(handoffId, tenantId) {
    const handoff = this.find('handoffs', handoffId);
    const order = this.requireOrder(handoff.searchOrderId);
    if (tenantId !== order.tenantId) throw forbidden('NOT_ORDER_TENANT', '只有租客可以确认交接');
    handoff.tenantConfirmation = { actorId: tenantId, confirmedAt: this.now() };
    if (handoff.receiverConfirmation) {
      handoff.status = 'CONFIRMED';
      handoff.confirmedAt = this.now();
    } else {
      handoff.status = 'PENDING_RECEIVER_ACCEPTANCE';
    }
    this.commit();
    return handoff;
  }

  acceptHandoffByStore(handoffId, actorInput) {
    const handoff = this.find('handoffs', handoffId);
    const actor = this.resolveStoreActor(actorInput);
    if (actor.storeId !== handoff.toStoreId) {
      throw forbidden('WRONG_RECEIVER_STORE', `只有承接门店 ${handoff.toStoreId} 可以接收交接`);
    }
    handoff.receiverConfirmation = {
      actorId: actor.actorId,
      storeId: actor.storeId,
      acceptedAt: this.now(),
    };
    if (handoff.tenantConfirmation) {
      handoff.status = 'CONFIRMED';
      handoff.confirmedAt = this.now();
      for (const item of handoff.items) {
        this.state.storeAuthorizations.push({
          id: this.assignId('storeAuthorizations', 'AUTH', undefined),
          searchOrderId: handoff.searchOrderId,
          storeId: handoff.toStoreId,
          scopes: [item.workType],
          roomIds: [item.roomId],
          source: 'HANDOFF',
          sourceId: handoff.id,
          status: 'ACTIVE',
          createdAt: this.now(),
          createdBy: actor.actorId,
        });
      }
    } else {
      handoff.status = 'PENDING_TENANT_CONFIRMATION';
    }
    this.commit();
    return handoff;
  }

  createSplitAgreement(input = {}) {
    const orderId = this.requireId(input, 'searchOrderId');
    this.requireOrder(orderId);
    const roomId = this.requireId(input, 'roomId');
    this.requireRoom(roomId);
    const commissionType = input.commissionType || 'VIEWING';
    if (!COMMISSION_TYPES.has(commissionType)) {
      throw badRequest('INVALID_COMMISSION_TYPE', `未知佣金类型 ${commissionType}`);
    }
    const sharesInput = Array.isArray(input.shares) ? input.shares : [];
    if (sharesInput.length === 0) throw badRequest('EMPTY_SPLIT', '分账约定必须包含份额');
    const shares = sharesInput.map((share) => {
      const storeId = this.requireId(share, 'storeId');
      this.requireStore(storeId);
      const bps = Number(share.bps);
      if (!Number.isInteger(bps) || bps <= 0 || bps > 10000) {
        throw badRequest('INVALID_BPS', 'bps 必须是 1 到 10000 的整数');
      }
      return { storeId, bps };
    });
    const storeIds = shares.map((s) => s.storeId);
    if (new Set(storeIds).size !== storeIds.length) {
      throw badRequest('DUPLICATE_SPLIT_STORE', '同一门店在分账约定中只能出现一次');
    }
    if (shares.reduce((sum, s) => sum + s.bps, 0) !== 10000) {
      throw badRequest('BPS_SUM_MUST_BE_10000', '分账比例合计必须为 10000 个基点');
    }

    // Every custom-share store must have real work or a confirmed handoff.
    for (const storeId of storeIds) {
      const hasWork = this.state.contributions.some(
        (c) =>
          c.searchOrderId === orderId &&
          c.roomId === roomId &&
          c.type === commissionType &&
          c.storeId === storeId,
      );
      const hasHandoff = this.state.handoffs.some(
        (h) =>
          h.searchOrderId === orderId &&
          h.status === 'CONFIRMED' &&
          h.toStoreId === storeId &&
          h.items.some((i) => i.roomId === roomId && i.workType === commissionType),
      );
      if (!hasWork && !hasHandoff) {
        throw forbidden(
          'SPLIT_PARTY_HAS_NO_CONTRIBUTION',
          `门店 ${storeId} 未参与 ${roomId} 的${commissionType}工作，也没有经确认的接办授权，不能进入分账约定`,
        );
      }
    }

    const agreement = {
      id: this.assignId('splitAgreements', 'SPL', input.id),
      searchOrderId: orderId,
      roomId,
      commissionType,
      shares,
      tenantConfirmation: input.tenantConfirmed
        ? { tenantId: this.requireOrder(orderId).tenantId, confirmedAt: this.now() }
        : null,
      storeConfirmations: input.storeConfirmed ? storeIds.map((storeId) => ({ storeId, confirmedAt: this.now() })) : [],
      status: 'PENDING_SIGNATURES',
      createdAt: this.now(),
    };
    this.refreshSplitAgreementStatus(agreement);
    this.state.splitAgreements.push(agreement);
    this.commit();
    return agreement;
  }

  refreshSplitAgreementStatus(agreement) {
    const allStoresConfirmed = agreement.shares.every((share) =>
      agreement.storeConfirmations.some((c) => c.storeId === share.storeId),
    );
    if (agreement.tenantConfirmation && allStoresConfirmed) {
      agreement.status = 'CONFIRMED';
      agreement.confirmedAt = this.now();
    } else {
      agreement.status = 'PENDING_SIGNATURES';
    }
  }

  confirmSplitAgreementByTenant(agreementId, tenantId) {
    const agreement = this.find('splitAgreements', agreementId);
    const order = this.requireOrder(agreement.searchOrderId);
    if (tenantId !== order.tenantId) throw forbidden('NOT_ORDER_TENANT', '只有租客可以确认分账约定');
    agreement.tenantConfirmation = { tenantId, confirmedAt: this.now() };
    this.refreshSplitAgreementStatus(agreement);
    this.commit();
    return agreement;
  }

  confirmSplitAgreementByStore(agreementId, actorInput) {
    const agreement = this.find('splitAgreements', agreementId);
    const actor = this.resolveStoreActor(actorInput);
    if (!agreement.shares.some((share) => share.storeId === actor.storeId)) {
      throw forbidden('NOT_SPLIT_PARTY', '该门店不是分账约定当事人');
    }
    if (!agreement.storeConfirmations.some((c) => c.storeId === actor.storeId)) {
      agreement.storeConfirmations.push({ storeId: actor.storeId, actorId: actor.actorId, confirmedAt: this.now() });
    }
    this.refreshSplitAgreementStatus(agreement);
    this.commit();
    return agreement;
  }

  // ---------- lease and commission engine ----------

  signLease(input = {}) {
    const orderId = this.requireId(input, 'searchOrderId');
    const order = this.requireOrder(orderId);
    const tenantActorId = input.tenantActorId || input.tenantId || input.actorId;
    if (tenantActorId !== order.tenantId) {
      throw forbidden('TENANT_SIGNATURE_REQUIRED', '租约必须由本单租客确认签订');
    }

    const listingId = this.requireId(input, 'listingId');
    const listing = this.find('listings', listingId);
    if (!listing) throw notFound('LISTING_NOT_FOUND', `挂牌 ${listingId} 不存在`);

    const signingStoreId = this.requireId(input, 'signingStoreId');
    this.requireStore(signingStoreId);
    const signingAgentId = this.requireId(input, 'signingAgentId');
    this.requireAgent(signingAgentId, signingStoreId);

    const signedRoomIds = uniqueStrings(input.signedRoomIds || [], 'signedRoomIds');
    if (signedRoomIds.length === 0) throw badRequest('EMPTY_LEASE', '租约必须至少签下一个独立房间');
    for (const roomId of signedRoomIds) {
      const room = this.requireRoom(roomId);
      if (!room.commissionable) {
        throw unprocessable('CANNOT_LEASE_COMMON_AREA_AS_ROOM', `${roomId} 是共用空间，不能作为按间计佣的签约房间`);
      }
      if (!listing.roomIds.includes(roomId)) {
        throw badRequest('ROOM_NOT_IN_LISTING', `签约房间 ${roomId} 不在挂牌 ${listingId} 范围内`);
      }
    }

    if (input.wholeSet === true) {
      const expected = [...listing.roomIds].sort();
      const actual = [...signedRoomIds].sort();
      if (expected.length !== actual.length || expected.some((id, i) => id !== actual[i])) {
        throw badRequest('WHOLE_SET_MISMATCH', 'wholeSet=true 时必须签下整套挂牌中的全部独立房间');
      }
    }

    const duplicateRoomLease = this.state.leases.find(
      (lease) =>
        lease.searchOrderId === orderId &&
        lease.status === 'SIGNED' &&
        lease.signedRoomIds.some((roomId) => signedRoomIds.includes(roomId)),
    );
    if (duplicateRoomLease) {
      throw conflict(
        'ROOM_ALREADY_SIGNED',
        '同一找房单的房间已有有效租约；不能用第二份租约重复产生带看佣金',
        { existingLeaseId: duplicateRoomLease.id },
      );
    }

    const lease = {
      id: this.assignId('leases', 'LSE', input.id),
      searchOrderId: orderId,
      listingId,
      signingStoreId,
      signingAgentId,
      tenantId: order.tenantId,
      signedRoomIds,
      wholeSet: input.wholeSet ?? signedRoomIds.length === listing.roomIds.length,
      signedAt: input.signedAt || this.now(),
      tenantSignature: { actorId: tenantActorId, signedAt: this.now() },
      storeSignature: { storeId: signingStoreId, agentId: signingAgentId, signedAt: this.now() },
      status: 'SIGNED',
      createdAt: this.now(),
    };
    this.state.leases.push(lease);
    order.status = 'LEASED';

    const calculated = this.calculateCommission(lease, order, listing);
    this.commit();
    return this.getLeaseDetail(lease.id, calculated);
  }

  pointPoolForContribution(contribution, type, fallbackListing) {
    if (contribution?.appointmentId) {
      const appointment = this.find('appointments', contribution.appointmentId, false);
      const snapshotKey = type === 'VIEWING' ? 'viewingPointsByRoom' : 'negotiationPointsByRoom';
      const points = appointment?.commissionSnapshot?.[snapshotKey]?.[contribution.roomId];
      if (Number.isInteger(points)) return points;
    }
    const scheduleKey = type === 'VIEWING' ? 'viewingPointsByRoom' : 'negotiationPointsByRoom';
    return fallbackListing?.[scheduleKey]?.[contribution?.roomId] ?? 0;
  }

  effectiveSplitAgreement(orderId, roomId, type) {
    return this.state.splitAgreements.find(
      (agreement) =>
        agreement.searchOrderId === orderId &&
        agreement.roomId === roomId &&
        agreement.commissionType === type &&
        agreement.status === 'CONFIRMED',
    );
  }

  allocateViewingForRoom(order, lease, roomId, listing) {
    const contributions = this.state.contributions
      .filter(
        (c) =>
          c.searchOrderId === order.id &&
          c.roomId === roomId &&
          c.type === 'VIEWING',
      )
      .sort((a, b) => timeMs(a.occurredAt) - timeMs(b.occurredAt) || a.id.localeCompare(b.id));

    if (contributions.length === 0) {
      return { pool: 0, allocations: [], basis: 'NO_CONFIRMED_ENTRY' };
    }

    if (order.regime === 'FIRST_PROTECTION') {
      const fulfilled = [];
      for (const protection of this.state.protections) {
        if (protection.searchOrderId !== order.id) continue;
        const roomState = protection.roomStates.find(
          (rs) => rs.roomId === roomId && rs.status === 'FULFILLED',
        );
        if (roomState) fulfilled.push({ protection, roomState });
      }
      fulfilled.sort((a, b) => timeMs(a.protection.createdAt) - timeMs(b.protection.createdAt));

      let winnerContribution;
      let basis;
      let sourceIds = [];
      if (fulfilled.length > 0) {
        const winner = fulfilled[0];
        winnerContribution = contributions.find(
          (c) => c.sourceId === winner.roomState.visitId && c.storeId === winner.protection.storeId,
        ) || contributions.find((c) => c.storeId === winner.protection.storeId);
        basis = 'VALID_FIRST_PROTECTION';
        sourceIds = [winner.protection.id, winner.roomState.visitId].filter(Boolean);
      } else {
        [winnerContribution] = contributions;
        basis = 'EARLIEST_CONFIRMED_ENTRY_WITHOUT_VALID_PROTECTION';
      }
      if (!winnerContribution) {
        return { pool: 0, allocations: [], basis: 'PROTECTION_WITHOUT_CONTRIBUTION' };
      }
      const pool = this.pointPoolForContribution(winnerContribution, 'VIEWING', listing);
      return {
        pool,
        basis,
        allocations: [
          {
            payeeStoreId: winnerContribution.storeId,
            weight: 1,
            points: pool,
            reason: basis,
            sourceIds: [...sourceIds, winnerContribution.sourceId].filter(Boolean),
          },
        ],
      };
    }

    // RELAY_CONTRIBUTION
    const agreement = this.effectiveSplitAgreement(order.id, roomId, 'VIEWING');
    if (agreement) {
      const earliest = contributions[0];
      const pool = this.pointPoolForContribution(earliest, 'VIEWING', listing);
      const allocated = allocateIntegerPoints(
        pool,
        agreement.shares.map((share) => ({ payeeStoreId: share.storeId, bps: share.bps, weight: share.bps })),
      );
      return {
        pool,
        basis: 'CONFIRMED_CUSTOM_SPLIT_AGREEMENT',
        agreementId: agreement.id,
        allocations: allocated.map((item) => ({
          ...item,
          reason: 'CUSTOM_SPLIT_BPS',
          sourceIds: [agreement.id],
        })),
      };
    }

    const byStore = new Map();
    for (const contribution of contributions) {
      if (!byStore.has(contribution.storeId)) byStore.set(contribution.storeId, []);
      byStore.get(contribution.storeId).push(contribution);
    }
    const recipients = [...byStore.entries()].map(([storeId, storeContributions]) => ({
      payeeStoreId: storeId,
      weight: 1,
      reason: 'EQUAL_RELAY_VIEWING_SHARE',
      sourceIds: storeContributions.map((c) => c.sourceId),
    }));
    const pool = this.pointPoolForContribution(contributions[0], 'VIEWING', listing);
    return {
      pool,
      basis: 'EQUAL_SPLIT_PER_DISTINCT_RELAY_STORE',
      allocations: allocateIntegerPoints(pool, recipients),
    };
  }

  allocateNegotiationForRoom(order, roomId, listing) {
    const contributions = this.state.contributions
      .filter((c) => c.searchOrderId === order.id && c.roomId === roomId && c.type === 'NEGOTIATION')
      .sort((a, b) => timeMs(a.occurredAt) - timeMs(b.occurredAt));

    if (contributions.length === 0) return { pool: 0, allocations: [], basis: 'NO_CONFIRMED_NEGOTIATION' };

    const pool = listing.negotiationPointsByRoom?.[roomId] ?? 0;
    if (!Number.isInteger(pool) || pool <= 0) return { pool: 0, allocations: [], basis: 'NO_NEGOTIATION_POINTS' };

    const agreement = this.effectiveSplitAgreement(order.id, roomId, 'NEGOTIATION');
    if (agreement) {
      const allocated = allocateIntegerPoints(
        pool,
        agreement.shares.map((share) => ({ payeeStoreId: share.storeId, weight: share.bps })),
      );
      return { pool, basis: 'CONFIRMED_CUSTOM_SPLIT_AGREEMENT', allocations: allocated };
    }

    if (order.regime === 'FIRST_PROTECTION') {
      const effective = [...contributions].reverse().find((c) => c.effective) || contributions[contributions.length - 1];
      return {
        pool,
        basis: 'LAST_EFFECTIVE_NEGOTIATOR',
        allocations: [
          {
            payeeStoreId: effective.storeId,
            points: pool,
            weight: 1,
            reason: 'LAST_EFFECTIVE_NEGOTIATOR',
            sourceIds: [effective.sourceId],
          },
        ],
      };
    }

    const stores = [...new Set(contributions.map((c) => c.storeId))];
    const allocations = allocateIntegerPoints(
      pool,
      stores.map((storeId) => ({
        payeeStoreId: storeId,
        weight: 1,
        reason: 'EQUAL_RELAY_NEGOTIATION_SHARE',
      })),
    );
    return { pool, basis: 'EQUAL_SPLIT_NEGOTIATION', allocations };
  }

  calculateCommission(lease, order, listing) {
    for (const roomId of lease.signedRoomIds) {
      const viewing = this.allocateViewingForRoom(order, lease, roomId, listing);
      const negotiation = this.allocateNegotiationForRoom(order, roomId, listing);
      const totalPoints = viewing.pool + negotiation.pool;

      const kase = {
        id: this.assignId('commissionCases', 'CASE', undefined),
        leaseId: lease.id,
        searchOrderId: order.id,
        roomId,
        listingId: listing.id,
        regime: order.regime,
        viewingPoints: viewing.pool,
        negotiationPoints: negotiation.pool,
        totalPoints,
        viewingBasis: viewing.basis,
        negotiationBasis: negotiation.basis,
        status: totalPoints > 0 ? 'PAYABLE' : 'ZERO_POINTS',
        createdAt: this.now(),
      };
      this.state.commissionCases.push(kase);

      for (const allocation of viewing.allocations.filter((a) => a.points > 0)) {
        const item = {
          id: this.assignId('commissionItems', 'ITEM', undefined),
          caseId: kase.id,
          leaseId: lease.id,
          searchOrderId: order.id,
          roomId,
          commissionType: 'VIEWING',
          payeeStoreId: allocation.payeeStoreId,
          points: allocation.points,
          reason: allocation.reason,
          basis: viewing.basis,
          sourceIds: allocation.sourceIds || [],
          status: 'PAYABLE',
          settlementId: null,
          createdAt: this.now(),
        };
        this.state.commissionItems.push(item);
      }

      for (const allocation of negotiation.allocations.filter((a) => a.points > 0)) {
        const item = {
          id: this.assignId('commissionItems', 'ITEM', undefined),
          caseId: kase.id,
          leaseId: lease.id,
          searchOrderId: order.id,
          roomId,
          commissionType: 'NEGOTIATION',
          payeeStoreId: allocation.payeeStoreId,
          points: allocation.points,
          reason: allocation.reason,
          basis: negotiation.basis,
          sourceIds: allocation.sourceIds || [],
          status: 'PAYABLE',
          settlementId: null,
          createdAt: this.now(),
        };
        this.state.commissionItems.push(item);
      }
    }
  }

  getLeaseDetail(leaseId, precomputed = null) {
    const lease = precomputed || this.find('leases', leaseId);
    const cases = this.state.commissionCases.filter((c) => c.leaseId === lease.id);
    const caseIds = new Set(cases.map((c) => c.id));
    const commissionItems = this.state.commissionItems.filter((i) => caseIds.has(i.caseId));
    return { lease, commissionCases: cases, commissionItems };
  }

  // ---------- money extraction ----------

  postLedger(accountId, deltaPoints, reference) {
    const account = this.find('accounts', accountId);
    const nextBalance = account.balancePoints + deltaPoints;
    if (nextBalance < 0) {
      throw unprocessable('INSUFFICIENT_MOCK_BALANCE', `模拟账户 ${accountId} 余额不足`);
    }
    account.balancePoints = nextBalance;
    const entry = {
      id: this.assignId('ledgerEntries', 'LED', undefined),
      accountId,
      deltaPoints,
      balanceAfterPoints: nextBalance,
      referenceType: reference.type,
      referenceId: reference.id,
      idempotencyKey: reference.idempotencyKey || null,
      createdAt: this.now(),
    };
    this.state.ledgerEntries.push(entry);
    return entry;
  }

  createSettlement(input = {}) {
    const storeId = this.requireId(input, 'storeId');
    const store = this.requireStore(storeId);
    const requestedItemIds = uniqueStrings(input.itemIds || [], 'itemIds');
    if (requestedItemIds.length === 0) throw badRequest('EMPTY_SETTLEMENT', '请选择至少一条佣金明细');
    const itemIds = [...requestedItemIds].sort();

    const idempotencyKey = input.requestId || input.idempotencyKey || null;
    const canonical = stableHash({ storeId, itemIds });
    if (idempotencyKey) {
      const previous = this.state.idempotencyRecords.find((r) => r.key === idempotencyKey);
      if (previous) {
        if (previous.requestHash !== canonical) {
          throw conflict('IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST', '相同幂等键对应了不同结算内容');
        }
        if (previous.resourceType === 'SETTLEMENT') {
          const detail = this.getSettlementDetail(previous.resourceId);
          return { ...detail, replayed: true };
        }
      }
    }

    const items = itemIds.map((itemId) => this.find('commissionItems', itemId));
    for (const item of items) {
      if (item.payeeStoreId !== storeId) {
        throw forbidden('ITEM_BELONGS_TO_OTHER_STORE', `佣金明细 ${item.id} 不属于 ${storeId}`);
      }
      if (item.status === 'WITHDRAWN') {
        throw conflict(
          'COMMISSION_ITEM_ALREADY_WITHDRAWN',
          `佣金明细 ${item.id} 已提取，不能再次支付`,
          { itemId: item.id, settlementId: item.settlementId },
        );
      }
      if (item.status !== 'PAYABLE') {
        throw conflict('ITEM_NOT_PAYABLE', `佣金明细 ${item.id} 状态为 ${item.status}`);
      }
    }

    const totalPoints = items.reduce((sum, item) => sum + item.points, 0);
    if (!Number.isInteger(totalPoints) || totalPoints <= 0) {
      throw unprocessable('ZERO_SETTLEMENT', '没有可提取的正整数点数');
    }

    const settlementId = this.assignId('settlements', 'STL', input.id);
    const clearingAccountId = 'ACCT-PLATFORM-CLEARING';
    const clearingEntry = this.postLedger(clearingAccountId, -totalPoints, {
      type: 'SETTLEMENT_CLEARING_DEBIT',
      id: settlementId,
      idempotencyKey,
    });
    const storeEntry = this.postLedger(store.accountId, totalPoints, {
      type: 'SETTLEMENT_STORE_CREDIT',
      id: settlementId,
      idempotencyKey,
    });

    const settlement = {
      id: settlementId,
      idempotencyKey,
      storeId,
      accountId: store.accountId,
      itemIds,
      totalPoints,
      status: 'PAID',
      paidAt: this.now(),
      ledgerEntryIds: [clearingEntry.id, storeEntry.id],
    };
    this.state.settlements.push(settlement);

    for (const item of items) {
      item.status = 'WITHDRAWN';
      item.settlementId = settlementId;
      item.withdrawnAt = settlement.paidAt;
    }

    if (idempotencyKey) {
      this.state.idempotencyRecords.push({
        key: idempotencyKey,
        requestHash: canonical,
        resourceType: 'SETTLEMENT',
        resourceId: settlementId,
        createdAt: this.now(),
      });
    }

    this.commit();
    return { ...this.getSettlementDetail(settlementId), replayed: false };
  }

  getSettlementDetail(settlementId) {
    const settlement = this.find('settlements', settlementId);
    const items = settlement.itemIds.map((id) => this.find('commissionItems', id));
    const ledgerEntries = this.state.ledgerEntries.filter((e) =>
      settlement.ledgerEntryIds.includes(e.id),
    );
    const account = this.find('accounts', settlement.accountId);
    return { settlement, items, ledgerEntries, storeAccount: account };
  }

  withdrawItem(itemId, input = {}) {
    return this.createSettlement({ ...input, itemIds: [itemId] });
  }

  // ---------- read-back / restart trace ----------

  resolveSearchOrderRef(reference) {
    if (this.state.searchOrders.some((o) => o.id === reference)) return reference;

    const collections = [
      'appointments',
      'protections',
      'visits',
      'negotiations',
      'handoffs',
      'leases',
      'commissionCases',
      'commissionItems',
    ];
    for (const collection of collections) {
      const row = this.state[collection].find((item) => item.id === reference);
      if (row?.searchOrderId) return row.searchOrderId;
    }
    const settlement = this.state.settlements.find((s) => s.id === reference);
    if (settlement) {
      const item = this.state.commissionItems.find((i) => i.id === settlement.itemIds[0]);
      if (item) return item.searchOrderId;
    }
    return null;
  }

  buildTrace(orderId) {
    const order = this.requireOrder(orderId);
    const filterByOrder = (collection) =>
      this.state[collection].filter((row) => row.searchOrderId === orderId);

    const appointments = filterByOrder('appointments');
    const appointmentIds = new Set(appointments.map((a) => a.id));
    const protections = filterByOrder('protections');
    const visits = filterByOrder('visits');
    const negotiations = filterByOrder('negotiations');
    const handoffs = filterByOrder('handoffs');
    const splitAgreements = filterByOrder('splitAgreements');
    const leases = filterByOrder('leases');
    const commissionCases = filterByOrder('commissionCases');
    const commissionItems = filterByOrder('commissionItems');
    const mandates = filterByOrder('companionMandates');
    const authorizations = filterByOrder('storeAuthorizations');

    const caseIds = new Set(commissionCases.map((c) => c.id));
    const settlements = this.state.settlements
      .filter((settlement) =>
        settlement.itemIds.some((itemId) => {
          const item = this.state.commissionItems.find((i) => i.id === itemId);
          return item && caseIds.has(item.caseId);
        }),
      )
      .map((settlement) => ({
        ...settlement,
        ledgerEntries: this.state.ledgerEntries.filter((entry) =>
          settlement.ledgerEntryIds.includes(entry.id),
        ),
      }));

    const referencedRoomIds = new Set();
    for (const appointment of appointments) appointment.plannedSpaceIds.forEach((id) => referencedRoomIds.add(id));
    for (const visit of visits) {
      [...visit.enteredRoomIds, ...visit.doorwayOnlyRoomIds, ...visit.notSeenRoomIds].forEach((id) =>
        referencedRoomIds.add(id),
      );
    }
    for (const lease of leases) lease.signedRoomIds.forEach((id) => referencedRoomIds.add(id));
    commissionCases.forEach((c) => referencedRoomIds.add(c.roomId));

    const rooms = [...referencedRoomIds].map((id) => this.find('rooms', id, false)).filter(Boolean);
    const listingIds = new Set(appointments.map((a) => a.listingId));
    leases.forEach((l) => listingIds.add(l.listingId));
    const listings = [...listingIds].map((id) => this.find('listings', id, false)).filter(Boolean);

    const visitsWithResults = visits.map((visit) => ({
      ...visit,
      roomResults: [
        ...visit.enteredRoomIds.map((roomId) => ({ roomId, result: 'ENTERED_INDEPENDENT_ROOM' })),
        ...visit.doorwayOnlyRoomIds.map((roomId) => ({ roomId, result: 'DOORWAY_ONLY_COMMON_AREA' })),
        ...visit.notSeenRoomIds.map((roomId) => ({ roomId, result: 'NOT_SEEN' })),
      ],
    }));

    return {
      generatedAt: this.now(),
      searchOrder: order,
      rooms,
      listings,
      companionMandates: mandates,
      storeAuthorizations: authorizations,
      appointments,
      protections,
      visits: visitsWithResults,
      contributions: filterByOrder('contributions'),
      negotiations,
      handoffs,
      splitAgreements,
      leases,
      commissionCases,
      commissionItems,
      settlements,
      evidence: {
        attendanceCount: visits.filter((v) => v.status === 'CONFIRMED').length,
        signedRoomIds: [...new Set(leases.flatMap((l) => l.signedRoomIds))],
        withdrawnPointTotal: settlements.reduce((sum, s) => sum + s.totalPoints, 0),
        payablePointTotal: commissionItems
          .filter((i) => i.status === 'PAYABLE')
          .reduce((sum, i) => sum + i.points, 0),
      },
    };
  }

  traceForTenant(reference, tenantId) {
    const orderId = this.resolveSearchOrderRef(reference);
    if (!orderId) throw notFound('TRACE_NOT_FOUND', `原单号 ${reference} 未对应任何找房单`);
    const order = this.requireOrder(orderId);
    if (tenantId !== order.tenantId) throw forbidden('NOT_ORDER_TENANT', '只能查询本租客自己的找房单');
    return { viewer: { role: 'TENANT', actorId: tenantId }, ...this.buildTrace(orderId) };
  }

  traceForStore(reference, storeActor) {
    const actor = this.resolveStoreActor(storeActor);
    const orderId = this.resolveSearchOrderRef(reference);
    if (!orderId) throw notFound('TRACE_NOT_FOUND', `原单号 ${reference} 未对应任何找房单`);
    const trace = this.buildTrace(orderId);
    const storeId = actor.storeId;

    const nexus =
      trace.appointments.some((a) => a.storeId === storeId) ||
      trace.protections.some((p) => p.storeId === storeId) ||
      trace.contributions.some((c) => c.storeId === storeId) ||
      trace.negotiations.some((n) => n.storeId === storeId) ||
      trace.handoffs.some((h) => h.fromStoreId === storeId || h.toStoreId === storeId) ||
      trace.leases.some((l) => l.signingStoreId === storeId) ||
      trace.commissionItems.some((i) => i.payeeStoreId === storeId);
    if (!nexus) throw forbidden('STORE_NOT_CONNECTED_TO_ORDER', '该门店没有参与此单，不能查询完整单据');

    const myCommissionItems = trace.commissionItems.filter((i) => i.payeeStoreId === storeId);
    const mySettlementIds = new Set(
      myCommissionItems.filter((i) => i.settlementId).map((i) => i.settlementId),
    );
    const mySettlements = trace.settlements.filter((s) => mySettlementIds.has(s.id));
    const account = this.state.accounts.find((a) => a.id === this.requireStore(storeId).accountId);
    const ledgerEntries = this.state.ledgerEntries.filter((e) => e.accountId === account.id);

    return {
      viewer: { role: 'STORE_CLERK', actorId: actor.actorId, storeId },
      ...trace,
      myCommissionItems,
      mySettlements,
      myMockAccount: { ...account, ledgerEntries },
    };
  }
}

module.exports = { PlatformService };
