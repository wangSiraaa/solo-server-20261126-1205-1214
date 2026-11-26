// Households, member groups, and applications (the "network request" surface).

import {
  badRequest, notFound, conflict, uniq, sortNights, parseNight,
  clone, nowIso, asArray,
} from './util.js';

// ---- households ----------------------------------------------------------

export function createHousehold(store, input) {
  const { id, displayName, members, inseparableGroups } = input ?? {};
  if (!id || typeof id !== 'string') throw badRequest('household id required');
  if (store.state.households[id]) throw conflict(`household ${id} already exists`);

  const memberList = asArray(members).map((m, i) => {
    if (typeof m === 'string') return { id: m, name: m };
    if (!m?.id) throw badRequest('member id required');
    return { id: m.id, name: m.name ?? m.id };
  });
  if (memberList.length === 0) throw badRequest('household needs at least one member');
  const memberIds = memberList.map((m) => m.id);
  if (uniq(memberIds).length !== memberIds.length) throw badRequest('duplicate member ids');

  // Normalize inseparable groups: every member belongs to exactly one group,
  // members not named become singleton groups.
  const groups = [];
  const seen = new Set();
  for (const g of asArray(inseparableGroups)) {
    const gg = asArray(g);
    if (gg.length === 0) continue;
    for (const m of gg) {
      if (!memberIds.includes(m)) throw badRequest(`inseparable member ${m} not in household`);
      if (seen.has(m)) throw badRequest(`member ${m} appears in more than one inseparable group`);
      seen.add(m);
    }
    groups.push([...gg].sort());
  }
  for (const m of memberIds) {
    if (!seen.has(m)) groups.push([m]);
  }

  const hh = {
    id,
    displayName: displayName ?? id,
    members: memberList,
    memberIds,
    inseparableGroups: groups,
    createdAt: nowIso(),
  };
  store.state.households[id] = hh;
  store.logEvent('household.created', { id });
  store.save();
  return clone(hh);
}

export function getHousehold(store, id) {
  const hh = store.state.households[id];
  if (!hh) throw notFound(`household ${id} not found`);
  return hh;
}

// Validate a traveling party: member ids are real and whole inseparable groups
// are kept together (no splitting a family unit).
export function validateParty(household, party) {
  const members = asArray(party);
  if (members.length === 0) throw badRequest('party must contain at least one member');
  for (const m of members) {
    if (!household.memberIds.includes(m)) throw badRequest(`member ${m} not in household ${household.id}`);
  }
  const set = new Set(members);
  for (const group of household.inseparableGroups) {
    const inside = group.filter((m) => set.has(m)).length;
    if (inside !== 0 && inside !== group.length) {
      throw badRequest(`inseparable group [${group.join(',')}] cannot be split`, { group });
    }
  }
  return [...new Set(members)];
}

export function isWholeFamily(household, party) {
  return party.length === household.memberIds.length &&
    household.memberIds.every((m) => party.includes(m));
}

// ---- applications --------------------------------------------------------

function normCombos(combos, selfId) {
  return asArray(combos).map((c) => {
    const arr = asArray(c);
    if (arr.length === 0) throw badRequest('empty companion combination');
    for (const h of arr) {
      if (typeof h !== 'string') throw badRequest('companion combination must list household ids');
      if (h === selfId) throw badRequest('a household cannot be its own companion');
    }
    return [...new Set(arr)].sort();
  });
}

// Normalize and validate an application document. The application carries both
// sides of a household's participation:
//   - host:  nights it can be home and the visiting combinations it accepts
//   - travel: nights it can be away, the party that travels, and preferences
export function normalizeApplication(store, householdId, input) {
  const household = getHousehold(store, householdId);
  const app = input ?? {};

  const party = app.party ? validateParty(household, app.party) : [...household.memberIds];
  const wholeFamily = isWholeFamily(household, party);

  const travelNights = sortNights(asArray(app.travelNights).map((n) => { parseNight(n); return n; }));
  const hostNights = sortNights(asArray(app.hostNights).map((n) => { parseNight(n); return n; }));

  // Note: declared travel and host dates may overlap. The whole-family rule
  // ("cannot host at home while away") is enforced when a *contract* claims a
  // specific role on a night, not at declaration time, since a household may
  // choose either role on any given available night.
  const minConsecutiveNights = app.minConsecutiveNights ?? 1;
  if (!Number.isInteger(minConsecutiveNights) || minConsecutiveNights < 1) {
    throw badRequest('minConsecutiveNights must be a positive integer');
  }
  const maxNights = app.maxNights ?? null;
  if (maxNights != null && (!Number.isInteger(maxNights) || maxNights < minConsecutiveNights)) {
    throw badRequest('maxNights must be an integer >= minConsecutiveNights');
  }

  const preferredHosts = uniq(asArray(app.preferredHosts));
  for (const h of preferredHosts) getHousehold(store, h);

  const visitOrder = asArray(app.visitOrder);
  // `acceptedCompanions` omitted => wildcard (accept any combination).
  // Explicit empty array => accept no combination.
  const acceptedCompanions = app.acceptedCompanions === undefined
    ? null
    : normCombos(app.acceptedCompanions, householdId);
  if (acceptedCompanions) {
    for (const combo of acceptedCompanions) for (const h of combo) getHousehold(store, h);
  }

  const normalized = {
    householdId,
    party,
    wholeFamily,
    travelNights,
    hostNights,
    minConsecutiveNights,
    maxNights,
    preferredHosts,
    visitOrder,
    acceptedCompanions,
    // Explicit per-host consent can be given via a map; folded into combos above.
    notes: app.notes ?? null,
    version: (store.state.applications[householdId]?.version ?? 0) + 1,
    updatedAt: nowIso(),
  };
  return normalized;
}

export function setApplication(store, householdId, input, { expectedVersion } = {}) {
  const current = store.state.applications[householdId];
  if (expectedVersion != null && (current?.version ?? 0) !== expectedVersion) {
    throw conflict('application version mismatch', {
      expectedVersion, actualVersion: current?.version ?? 0,
    });
  }
  const normalized = normalizeApplication(store, householdId, input);
  store.state.applicationHistory[householdId] ??= [];
  if (current) store.state.applicationHistory[householdId].push(clone(current));
  store.state.applications[householdId] = normalized;
  store.logEvent('application.set', { householdId, version: normalized.version });
  store.save();
  return clone(normalized);
}

export function getApplication(store, householdId) {
  const app = store.state.applications[householdId];
  if (!app) throw notFound(`application for ${householdId} not found`);
  return app;
}
