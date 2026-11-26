#!/usr/bin/env bash
# A copy-pasteable local request walkthrough.
# It starts an EMPTY backend on local ports, powers it off/on once, and ends
# with a physical-count reconciliation. No bank or real hardware is contacted.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
A="http://127.0.0.1:4501"
B="http://127.0.0.1:4502"
M="http://127.0.0.1:4600"
DATA_FILE="$ROOT/data/walkthrough-state.json"
LOG_FILE="$ROOT/data/walkthrough-server.log"
PID=""

mkdir -p "$ROOT/data"
rm -f "$DATA_FILE" "$DATA_FILE.tmp-"* "$LOG_FILE"

cleanup() {
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

start_server() {
  DATA_FILE="$DATA_FILE" POS_A_PORT=4501 POS_B_PORT=4502 MAINT_PORT=4600 \
    node "$ROOT/src/server.mjs" >"$LOG_FILE" 2>&1 &
  PID=$!
  for _ in $(seq 1 50); do
    curl -fsS "$A/health" >/dev/null 2>&1 && return 0
    sleep 0.1
  done
  echo "server failed to start; see $LOG_FILE" >&2
  exit 1
}

title() { printf '\n\033[1;36m### %s\033[0m\n' "$1"; }
post() {
  local base="$1" route="$2" body="$3"
  printf '\n→ POST %s\n  %s\n' "$route" "$body"
  curl -sS -X POST "$base$route" -H 'content-type: application/json' -d "$body" \
    -w '\n  [HTTP %{http_code}]\n'
}
get() {
  local base="$1" route="$2"
  printf '\n→ GET %s\n' "$route"
  curl -sS "$base$route" -w '\n  [HTTP %{http_code}]\n'
}

title 'Start empty backend: sales A=4501, sales B=4502, maintenance=4600'
start_server
get "$M" '/health'
get "$A" '/health'
get "$B" '/health'

title '1. Empty A reconciliation, then restock one 2-yuan and three 1-yuan tubes'
get "$M" '/machines/A/reconciliation'
post "$M" '/machines/A/restock' '{
  "requestId":"RESTOCK-A",
  "coins":[
    {"id":"A-T2-1","denomination":2},
    {"id":"A-T1-1","denomination":1},
    {"id":"A-T1-2","denomination":1},
    {"id":"A-T1-3","denomination":1}
  ]
}'
post "$M" '/machines/B/restock' '{
  "requestId":"RESTOCK-B",
  "coins":[{"id":"B-T2-1","denomination":2},{"id":"B-T1-1","denomination":1}]
}'

title '2. Changing a real 10-yuan fact to 5 without another coin cannot vend'
post "$A" '/orders' '{"requestId":"A05-CREATE","orderId":"A-05","totalAmount":7}'
post "$A" '/orders/A-05/coins' '{"requestId":"A05-PAY","coinId":"A05-PAY-5","denomination":5}'
post "$A" '/orders/A-05/vend' '{"requestId":"A05-VEND"}'

title '3. Main order A-1001: soda 7, inserted 10, change 3'
post "$A" '/orders' '{"requestId":"A1-CREATE","orderId":"A-1001","sku":"SODA","totalAmount":7,"mode":"STREAM"}'
post "$A" '/orders/A-1001/coins' '{"requestId":"A1-PAY","coinId":"A1-PAY-10","denomination":10}'
echo $'\n→ Reusing A1-PAY with denomination 5 must be 409; paid amount stays 10'
post "$A" '/orders/A-1001/coins' '{"requestId":"A1-PAY","coinId":"A1-PAY-10","denomination":5}'
post "$A" '/orders/A-1001/vend' '{"requestId":"A1-VEND"}'
post "$M" '/machines/A/events/goods-delivery' '{"requestId":"A1-GOODS","eventId":"RCPT-A1","orderId":"A-1001"}'
post "$M" '/machines/A/events/goods-delivery' '{"requestId":"A1-GOODS","eventId":"RCPT-A1","orderId":"A-1001"}'

title '4. Pay 2 successfully; route the first 1 through sensor then jam it at last segment'
post "$A" '/orders/A-1001/dispense-next' '{"requestId":"A1-DSP2","dispenseId":"DSP-A1-2"}'
post "$M" '/machines/A/events/channel-sensor' '{"requestId":"A1-S2","dispenseId":"DSP-A1-2"}'
post "$M" '/machines/A/events/coin-arrived' '{"requestId":"A1-ARR2","dispenseId":"DSP-A1-2"}'
post "$A" '/orders/A-1001/dispense-next' '{"requestId":"A1-DSP1","dispenseId":"DSP-A1-JAM"}'
post "$M" '/machines/A/events/channel-sensor' '{"requestId":"A1-S1","dispenseId":"DSP-A1-JAM"}'
post "$M" '/machines/A/events/coin-jam' '{"requestId":"A1-JAM","dispenseId":"DSP-A1-JAM","location":"LAST_SEGMENT"}'
get "$A" '/orders/A-1001'

title '5. Repeated door-open facts do not count as taking the 2-yuan tray coin'
post "$M" '/machines/A/events/pickup-door' '{"requestId":"A1-DOOR","orderId":"A-1001"}'
post "$M" '/machines/A/events/pickup-door' '{"requestId":"A1-DOOR","orderId":"A-1001"}'

title '6. Physical count before power loss: two 1 tubes, one jammed 1, one tray 2, escrow 5+10'
post "$M" '/machines/A/audit' '{
  "requestId":"A-AUDIT-MID",
  "physicalCounts":{
    "TUBE":{"1":2},
    "JAMMED":{"1":1},
    "TRAY":{"2":1},
    "ESCROW":{"5":1,"10":1}
  },
  "customerObtainedAmount":0
}'

title '7. POWER CYCLE: kill and restart with the same JSON state file'
kill "$PID" 2>/dev/null || true
wait "$PID" 2>/dev/null || true
start_server
get "$A" '/health'
get "$A" '/orders/A-1001'

title '8. Sales terminal B cannot see or pay A-1001'
post "$B" '/orders' '{"requestId":"B777-CREATE","orderId":"B-777","totalAmount":7}'
post "$B" '/orders/B-777/coins' '{"requestId":"B777-PAY","coinId":"B777-PAY10","denomination":10}'
post "$B" '/orders/B-777/vend' '{"requestId":"B777-VEND"}'
post "$B" '/orders/B-777/dispense-next' '{"requestId":"B777-D2","dispenseId":"DSP-B777-2"}'
post "$M" '/machines/B/events/channel-sensor' '{"requestId":"B777-S2","dispenseId":"DSP-B777-2"}'
post "$M" '/machines/B/events/coin-arrived' '{"requestId":"B777-A2","dispenseId":"DSP-B777-2"}'
post "$B" '/orders/B-777/dispense-next' '{"requestId":"B777-D1","dispenseId":"DSP-B777-1"}'
post "$M" '/machines/B/events/channel-sensor' '{"requestId":"B777-S1","dispenseId":"DSP-B777-1"}'
post "$M" '/machines/B/events/coin-arrived' '{"requestId":"B777-A1","dispenseId":"DSP-B777-1"}'
get "$B" '/orders/B-777'
echo $'\n→ B terminal querying A order must be 404:'
get "$B" '/orders/A-1001'
echo $'\n→ A debt remains exactly 1:'
get "$A" '/orders/A-1001'

title '9. Duplicate repair and old receipt; physically recover jammed coin; pay A difference'
post "$M" '/machines/A/events/channel-repaired' '{"requestId":"A1-REPAIR","channelId":"route-1"}'
post "$M" '/machines/A/events/channel-repaired' '{"requestId":"A1-REPAIR","channelId":"route-1"}'
post "$M" '/machines/A/events/goods-delivery' '{"requestId":"A1-GOODS","eventId":"RCPT-A1","orderId":"A-1001"}'
post "$M" '/machines/A/coins/A-T1-1/recover' '{"requestId":"A1-RECOVER","toLocation":"TUBE"}'
post "$A" '/orders/A-1001/claims/CLM-A-1001-CHANGE/claim' '{"requestId":"A1-CLAIM"}'
post "$A" '/orders/A-1001/claims/CLM-A-1001-CHANGE/dispense-next' '{"requestId":"A1-CLAIM-DSP","dispenseId":"DSP-A1-CLAIM"}'
post "$M" '/machines/A/events/channel-sensor' '{"requestId":"A1-CLAIM-S","dispenseId":"DSP-A1-CLAIM"}'
post "$M" '/machines/A/events/coin-arrived' '{"requestId":"A1-CLAIM-A","dispenseId":"DSP-A1-CLAIM"}'
post "$M" '/machines/A/pickup/confirm' '{"requestId":"A1-TAKE","takenCoinIds":["A-T2-1","A-T1-1"]}'

title '10. Following 1-yuan order A-1002 gets a different claim and coin A-T1-2'
post "$A" '/orders' '{"requestId":"A2-CREATE","orderId":"A-1002","totalAmount":4}'
post "$A" '/orders/A-1002/coins' '{"requestId":"A2-PAY","coinId":"A2-PAY5","denomination":5}'
post "$A" '/orders/A-1002/vend' '{"requestId":"A2-VEND"}'
post "$M" '/machines/A/events/goods-delivery' '{"requestId":"A2-GOODS","orderId":"A-1002"}'
post "$A" '/orders/A-1002/dispense-next' '{"requestId":"A2-DSP","dispenseId":"DSP-A2-1"}'
post "$M" '/machines/A/events/channel-sensor' '{"requestId":"A2-S","dispenseId":"DSP-A2-1"}'
post "$M" '/machines/A/events/coin-arrived' '{"requestId":"A2-A","dispenseId":"DSP-A2-1"}'
post "$M" '/machines/A/pickup/confirm' '{"requestId":"A2-TAKE","takenCoinIds":["A-T1-2"]}'

title '11. Explicit two-route capacity test: only 2 deliverable, never report 3 received'
post "$M" '/machines/A/restock' '{"requestId":"RESTOCK-A2","coins":[{"id":"A-T2-2","denomination":2}]}'
post "$M" '/machines/A/channels/route-1/fault' '{"requestId":"A2001-FAULT","channelId":"route-1","code":"ONE_ROUTE_DOWN"}'
get "$M" '/machines/A/reconciliation'
post "$A" '/orders' '{"requestId":"A2001-CREATE","orderId":"A-2001","totalAmount":7}'
post "$A" '/orders/A-2001/coins' '{"requestId":"A2001-PAY","coinId":"A2001-PAY10","denomination":10}'
post "$A" '/orders/A-2001/vend' '{"requestId":"A2001-VEND"}'
post "$M" '/machines/A/events/goods-delivery' '{"requestId":"A2001-GOODS","orderId":"A-2001"}'
post "$A" '/orders/A-2001/dispense-next' '{"requestId":"A2001-D2","dispenseId":"DSP-A2001-2"}'
post "$M" '/machines/A/events/channel-sensor' '{"requestId":"A2001-S2","dispenseId":"DSP-A2001-2"}'
post "$M" '/machines/A/events/coin-arrived' '{"requestId":"A2001-A2","dispenseId":"DSP-A2001-2"}'
echo $'\n→ Next request must be 409; order still has 1-yuan claim:'
post "$A" '/orders/A-2001/dispense-next' '{"requestId":"A2001-D1-BLOCKED"}'
post "$M" '/machines/A/events/channel-repaired' '{"requestId":"A2001-REPAIR","channelId":"route-1"}'
post "$A" '/orders/A-2001/dispense-next' '{"requestId":"A2001-D1","dispenseId":"DSP-A2001-1"}'
post "$M" '/machines/A/events/channel-sensor' '{"requestId":"A2001-S1","dispenseId":"DSP-A2001-1"}'
post "$M" '/machines/A/events/coin-arrived' '{"requestId":"A2001-A1","dispenseId":"DSP-A2001-1"}'
post "$M" '/machines/A/pickup/confirm' '{"requestId":"A2001-TAKE","takenCoinIds":["A-T2-2","A-T1-3"]}'

title '12. Final restart and physical-count reconciliation'
kill "$PID" 2>/dev/null || true
wait "$PID" 2>/dev/null || true
start_server
post "$M" '/machines/A/audit' '{
  "requestId":"A-AUDIT-FINAL",
  "physicalCounts":{
    "TUBE":{},
    "TRAY":{},
    "JAMMED":{},
    "CUSTOMER":{"1":3,"2":2},
    "CASHBOX":{"5":1,"10":2},
    "ESCROW":{"5":1}
  },
  "customerObtainedAmount":7
}'
get "$M" '/machines/A/reconciliation'
get "$M" '/machines/B/reconciliation'

a_balanced="$(curl -fsS "$M/machines/A/reconciliation" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>process.stdout.write(String(JSON.parse(s).reconciliation.balanced)))')"
b_balanced="$(curl -fsS "$M/machines/B/reconciliation" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>process.stdout.write(String(JSON.parse(s).reconciliation.balanced)))')"
if [[ "$a_balanced" != "true" || "$b_balanced" != "true" ]]; then
  echo "reconciliation failed: A=$a_balanced B=$b_balanced" >&2
  exit 1
fi
echo "Reconciliation passed: A and B are balanced."

title 'Done. State persisted in data/walkthrough-state.json; server log in data/walkthrough-server.log'
