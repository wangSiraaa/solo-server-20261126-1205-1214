import { createHousehold, setApplication } from '../src/domain.js';
import { reportNights, confirmBooking } from '../src/contracts.js';

export function hh(store, id, extra = {}) {
  return createHousehold(store, {
    id,
    displayName: id.toUpperCase(),
    members: extra.members ?? [`${id}-a`, `${id}-b`],
    inseparableGroups: extra.inseparableGroups ?? [],
  });
}

export function app(store, id, {
  nights = [],
  travelNights = nights,
  hostNights = nights,
  minConsecutiveNights = 1,
  maxNights = null,
  acceptedCompanions, // omit => wildcard
  preferredHosts = [],
  party,
} = {}) {
  return setApplication(store, id, {
    party,
    travelNights, hostNights, minConsecutiveNights, maxNights,
    acceptedCompanions, preferredHosts,
  });
}

export function stay(store, contractId, edgeIndex, nights) {
  reportNights(store, contractId, edgeIndex, 'host', nights, { confirm: true });
  reportNights(store, contractId, edgeIndex, 'guest', nights, { confirm: true });
  confirmBooking(store, contractId, edgeIndex, 'host');
  confirmBooking(store, contractId, edgeIndex, 'guest');
}
