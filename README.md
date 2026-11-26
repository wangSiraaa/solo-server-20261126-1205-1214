# 换场后台 — Shared-office changeover backend

A durable, event-sourced backend for swapping a shared office hall between
activities. It models the floor as **≤ 6 zones and ≤ 6 direction-aware doors**,
and treats what a visitor can *actually* walk as the single source of truth.

## The core rule

> A caption the backend merely **sent** is never the same as a screen the
> visitor can **see**. A screen counts as showing something only after the
> device confirms it *displayed* that exact copy (receive → buffer → display).
> Everything downstream — openings, receipts, retractions — is derived from
> confirmed displays plus field-reported door state.

Walkability is recomputed from **confirmed screen displays** and **real door
state**. A route that loops with no exit is not walkable; an unreached route may
be pointed at the reception desk as a *holding* sign, but that is never counted
as the activity being open.

## Mapping to the brief

| Brief clause | Where |
|---|---|
| floor ≤ 6 zones, ≤ 6 direction doors | `state.py` (`MAX_ZONES`, `MAX_DOORS`, `Door.can_traverse`) |
| screen row = activity + first door, visitor follows to next screen | `State.walk` |
| screen confirms receive / buffer / mask / real display | `/screens/{id}/{received,buffered,display,mask}` |
| sending a new copy ≠ visitor has seen it | display requires confirmed `screen.displayed` |
| guide must follow currently-open passages; loops aren't walkable | `State.walk` + `CycleTest` |
| unreached route may point at reception, not "open" | `RECEPTION` target; `open_direct` refuses |
| offline inspection of *actual* remaining guidance | `GET /guidance`, `Engine.left_guidance` |
| late stale "displayed" ignored; restart reproduces route | `screen.displayed` ordering + `RestartTest` |

## Two changeover agreements

**`whole_hall`** — swap the whole hall at once:

1. close the declared **partitions** and wait for the site crew to
   **field-confirm** them really in place;
2. **uniformly mask** every screen still showing the old activity;
3. stage the **full route signage** and wait for real display confirmations;
4. report the passage **doors open**;
5. only then **certify** each entrance and issue the host a
   *"访客可进 / visitors may enter"* receipt.

If a screen lags, `GET /changeovers/{id}` lists `pending_confirmations` and
`still_possible` — you can keep masking other conflicts, field-confirming
partitions, sending copies and confirming other devices, but you **cannot** sign
open until the lagging screen confirms.

**`successive`** — hand each independent entrance over one at a time:

* never drop the **last complete route** from some entrance to the old activity;
* only the parts of the new route that avoid the protected old route may switch
  early; if the new route *needs* a screen the old lifeline still carries, the
  switch is refused (`blocked_by_lifeline`) rather than silently broken;
* the **last entrance** still guiding visitors to the old activity may only be
  re-pointed **after the host registers the old activity ended**.

This guard is enforced twice: when asking to switch, *and* at the point of no
return — a raw `screen.displayed` confirmation that would erase the last old
route is rejected (`would_drop_old_lifeline`).

## Run it

```bash
# durable HTTP service
python3 -m changeover.serve --port 8080 --store /tmp/ev.jsonl

# scripted stories from the brief (each prints real server responses)
python3 -m changeover.demo baseline      # east may open for poetry
python3 -m changeover.demo retract       # partitioned receipt retracts opening
python3 -m changeover.demo stale         # corridor points back to east -> no open
python3 -m changeover.demo whole-hall    # mask/partition/stage/open/certify
python3 -m changeover.demo successive    # lifeline-protected per-entrance switch
python3 -m changeover.demo restart       # late stale display + restart reproduces route

# tests
python3 -m unittest discover -s tests -v
```

## HTTP surface

**Setup (back-office):** `POST /zones`, `/doors`, `/screens`, `/activities`,
`/activities/{id}/end`.

**Devices (visitor-facing screens only confirm):**
`POST /screens/{id}/copy` (backend sends), `/received`, `/buffered`,
`/display`, `/mask`, `/online`. `GET /screens/{id}` reads the *real* display.

**Field:** `POST /doors/{id}/field` (report truly open/partitioned),
`POST /changeovers/{id}/field`.

**Changeovers:** `POST /changeovers`, `GET /changeovers/{id}`,
`GET /changeovers/{id}/plan`, `POST /changeovers/{id}/switch`,
`POST /changeovers/{id}/open`.

**Openings & receipts:** `POST /openings`, `GET /openings`, `GET /openings/{n}`,
`GET /receipts`.

**Visitor trial walk / offline inspection:** `POST /walk`,
`GET /guidance?entrance=...&target=...`.

## Persistence & determinism

Every mutation is an event appended to a JSONL log and `fsync`ed before the
request returns. On start-up the log is replayed. Opening **numbers** and their
route **snapshots** are themselves events, so after a restart
`GET /openings/{n}` reproduces the route that was actually walkable at
certification — regardless of which confirmations arrived later (a late, stale
"displayed" of an older version is ignored both live and on replay).

## Layout

```
changeover/
  state.py       zones/doors/screens/activities, walkability, event application
  store.py       append-only JSONL log + fsync + replay
  service.py     changeover logic, openings, receipts, guards
  server.py      stdlib HTTP JSON API
  serve.py       runnable service
  demo.py        scripted stories from the brief
  errors.py
  fixtures.py           baseline floor (east/west/corridor/lecture/reception)
  fixtures_successive.py per-entrance lifeline layout
tests/
  test_changeover.py     domain semantics
  test_http.py           HTTP smoke flow
```
