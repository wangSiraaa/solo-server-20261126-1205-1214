# Exchange Homestay — night-level offset backend

A dependency-free Node service for a language-exchange club's reciprocal
homestay program. Households apply over the network, get grouped into
**pair-visits** or **mutual-aid rings**, check in night by night from both
sides, agree/correct/dispute those nights, and close with immutable statements.

The whole point is **traceability**: every night that was actually slept is
attributed to the household that *provided* it, and only mutually confirmed
nights count toward offsetting a reciprocal obligation.

## Adopted club contract

**Mutual-aid ring (互助环)**, with pair-visits as the 2-household special case:

- A **pair contract** is two households that each agree to host the other. Only
  nights A actually hosts B offset B's obligation to host A. Both households
  must have selected overlapping feasible dates before the pair can be signed.
- A **ring contract** is 3–4 households in a directed cycle `A hosts B →
  B hosts C → C hosts A`. Nights already actually provided offset one another
  by the **equal portion** shared by all edges (`common = min over edges`).
  Anything beyond that common portion stays as a **direct residual obligation
  on that edge** — it is *not* netted across the ring, and nights from outside
  the signed ring are never borrowed in.
- Scheduling picks the **maximum number of complete contracts** (pairs or rings)
  that fit within everyone's available dates, with no household double-booked
  on a night. Ties prefer each household's stated hosts / visit order.

## Night semantics

- A **night** is the calendar day a stay begins (`YYYY-MM-DD`).
- **Availability ≠ commitment.** `travelNights` / `hostNights` are dates a
  household *could* be away / host. The whole-family rule ("if the whole family
  is out, the home cannot also claim to be receiving") is enforced when a
  contract actually assigns a role on a night, across all active contracts.
- **Report then confirm.** Each side reports the nights it remembers
  (`report`). Only nights both sides reported **and** both confirmed count.
  Re-reporting a weekend replaces that side's set (idempotent) — history is
  kept for audit but never re-summed.
- **Disputes.** When the two remembered sets differ, the agreed intersection is
  held as confirmed and the symmetric difference is `disputedNights`, pending
  correction / re-confirmation against the original booking.
- **Statements.** First `close` issues an immutable **final** statement. Any
  later reopen + close issues a **supplementary** statement carrying only the
  **delta** since the last statement. Old statements stay queryable forever and
  confirmed nights are never double-counted.

## Per-household position

`settlement.householdPositions[i].net = hosted − received` (confirmed nights).
`carryingHouseholds` advanced nights; `owingHouseholds` still owe. `debts`
splits outstanding nights per edge under the equal-portion rule, each with
`advancedBy` (who provided it) and `nights_detail` (which dates).

## Data model

| entity | meaning |
|---|---|
| household | members, inseparable family groups |
| application | party, travel/host availability, min consecutive nights, accepted companion combos, preferences |
| contract | pair or ring: directed edges with planned nights |
| booking | per edge: host/guest reported sets, confirmations, history |
| statement | immutable final / supplementary settlement snapshot |

Everything is file-backed (atomic JSON write). Accounting is **derived** from
bookings on every read, so restarts and repeated reports cannot double count.

## API

```
POST   /households                              create household
GET    /households/:id
PUT    /households/:id/application              submit/update application
GET    /households/:id/application
POST   /households/:id/application/validate     dry-run normalization

POST   /plans                                   {type:"pair"|"ring", households:[...]}

POST   /contracts                               sign (re-validated against current apps)
POST   /contracts                               with "declines":[...] => revised sign
GET    /contracts/:id
GET    /contracts/:id/settlement
POST   /contracts/:id/edges/:e/report           {side, nights, confirm?}
POST   /contracts/:id/edges/:e/confirm          {side}
POST   /contracts/:id/edges/:e/correct          {side, nights}
GET    /contracts/:id/edges/:e/dispute
POST   /contracts/:id/edges/:e/cancel           {night}
POST   /contracts/:id/close                     final, then supplementary
POST   /contracts/:id/reopen
GET    /contracts/:id/statements
POST   /contracts/:id/revise                    {declines, splits}

GET    /statements/:id
GET    /bookings/:id
GET    /health
```

## Run

```bash
npm test              # 27 tests across accounting, signing, planning, statements, persistence, HTTP, narrative
node demo.js          # prints a signed 3-ring and a night that gets no cross-household offset
PORT=3000 DATA_FILE=./data/state.json npm start
```

All households / members use invented ids; no real data is involved.
