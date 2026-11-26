#!/usr/bin/env bash
# Reproduce the forum scenario with real HTTP calls and at most six posts.
# It starts/stops/restarts the Python service itself and asserts the key states.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8799}"
BASE="http://127.0.0.1:${PORT}"
DB="${FORUM_DB:-$ROOT/demo-forum.db}"
LOG="/tmp/forum-demo-${PORT}.log"
SRV_PID=""

cleanup() {
  if [[ -n "$SRV_PID" ]]; then
    kill "$SRV_PID" 2>/dev/null || true
    wait "$SRV_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

start_server() {
  FORUM_DB="$DB" PORT="$PORT" python3 "$ROOT/server.py" >"$LOG" 2>&1 &
  SRV_PID=$!
  for _ in $(seq 1 80); do
    if curl -sf "$BASE/health" >/dev/null 2>&1; then return 0; fi
    sleep 0.12
  done
  echo "server failed to start; log:" >&2
  cat "$LOG" >&2
  exit 1
}

restart_server() {
  kill "$SRV_PID" 2>/dev/null || true
  wait "$SRV_PID" 2>/dev/null || true
  SRV_PID=""
  start_server
}

assert_eq() {
  local actual="$1" expected="$2" label="$3"
  if [[ "$actual" != "$expected" ]]; then
    echo "ASSERT FAIL: $label" >&2
    echo "  expected: $expected" >&2
    echo "  actual:   $actual" >&2
    exit 1
  fi
  echo "  ✓ $label"
}

jqf() { jq -r "$1" <<<"$2"; }

echo "== 0. fresh persistent service =="
rm -f "$DB" "$DB-wal" "$DB-shm"
start_server
curl -fsS "$BASE/health" | jq -c '{ok,event_seq}'

echo "== 1. login users; two groups (old public board, destination private lab) =="
curl -fsS -X POST "$BASE/users" -H 'Content-Type: application/json' \
  -d '{"id":"u_mod","username":"mod","password":"pass1234","display_name":"版主"}' >/dev/null
curl -fsS -X POST "$BASE/users" -H 'Content-Type: application/json' \
  -d '{"id":"u_alice","username":"alice","password":"pass1234","display_name":"甲作者"}' >/dev/null
curl -fsS -X POST "$BASE/users" -H 'Content-Type: application/json' \
  -d '{"id":"u_bob","username":"bob","password":"pass1234","display_name":"读者Bob"}' >/dev/null

MOD_TOKEN=$(curl -fsS -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d '{"username":"mod","password":"pass1234"}' | jq -r .token)
ALICE_TOKEN=$(curl -fsS -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d '{"username":"alice","password":"pass1234"}' | jq -r .token)
BOB_TOKEN=$(curl -fsS -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d '{"username":"bob","password":"pass1234"}' | jq -r .token)
MOD_AUTH="Authorization: Bearer $MOD_TOKEN"
ALICE_AUTH="Authorization: Bearer $ALICE_TOKEN"
BOB_AUTH="Authorization: Bearer $BOB_TOKEN"
JSON='Content-Type: application/json'

curl -fsS -X POST "$BASE/groups" -H "$JSON" -H "$MOD_AUTH" \
  -d '{"id":"g_home","slug":"home","title":"老街拼单版","visibility":"public"}' >/dev/null
curl -fsS -X POST "$BASE/groups" -H "$JSON" -H "$MOD_AUTH" \
  -d '{"id":"g_lab","slug":"lab","title":"不能泄露的私组标题","visibility":"private"}' >/dev/null
curl -fsS -X POST "$BASE/groups/g_lab/members" -H "$JSON" -H "$MOD_AUTH" -d '{"user_id":"u_bob"}' >/dev/null
curl -fsS -X POST "$BASE/groups/g_lab/members" -H "$JSON" -H "$MOD_AUTH" -d '{"user_id":"u_alice","role":"moderator"}' >/dev/null
curl -fsS -X POST "$BASE/topics" -H "$JSON" -H "$MOD_AUTH" \
  -d '{"id":"t_home","group_id":"g_home","title":"周末市集"}' >/dev/null
curl -fsS -X POST "$BASE/topics" -H "$JSON" -H "$MOD_AUTH" \
  -d '{"id":"t_lab","group_id":"g_lab","title":"筹备现场"}' >/dev/null
echo "  ✓ users, two groups, two topics ready"

echo "== 2. six posts max: 甲 p1, collapsed child 乙 p2, then 丙/丁/戊/己 =="
curl -fsS -X POST "$BASE/posts" -H "$JSON" -H "$ALICE_AUTH" \
  -d '{"id":"p1","topic_id":"t_home","title":"甲：周末市集分工","body":"甲 v1：周六两点门口集合。"}' >/dev/null
curl -fsS -X POST "$BASE/posts" -H "$JSON" -H "$ALICE_AUTH" \
  -d '{"id":"p2","topic_id":"t_home","parent_id":"p1","body":"乙：我可以带两张折叠桌。"}' >/dev/null

SCREEN_V1=$(curl -fsS "$BASE/posts/p1/screen" -H "$BOB_AUTH")
PG_V1=$(jqf .page_token "$SCREEN_V1")
assert_eq "$(jqf '.post.version' "$SCREEN_V1")" "1" "Bob sees 甲 v1"
assert_eq "$(jqf '.collapsed_replies | length' "$SCREEN_V1")" "1" "乙 exists but is collapsed"
assert_eq "$(jqf '.displayed_items | length' "$SCREEN_V1")" "1" "screen receipt records only 甲, not collapsed 乙"

R_V1=$(curl -fsS -X POST "$BASE/read-receipts" -H "$JSON" -H "$BOB_AUTH" \
  -d "{\"page_token\":\"$PG_V1\",\"intent\":\"read_now\"}" | jq -r .receipt.id)
PG_OLD_LATE=$(curl -fsS "$BASE/posts/p1/screen" -H "$BOB_AUTH" | jq -r .page_token)
echo "  ✓ prior receipt $R_V1 confirms p1@v1; stale tab token $PG_OLD_LATE retained"

SUB_BRANCH=$(curl -fsS -X POST "$BASE/subscriptions" -H "$JSON" -H "$BOB_AUTH" \
  -d "{\"id\":\"sub_branch\",\"kind\":\"branch\",\"anchor_post_id\":\"p1\",\"baseline_receipt_id\":\"$R_V1\"}" | jq -r .subscription.id)
SUB_BOARD=$(curl -fsS -X POST "$BASE/subscriptions" -H "$JSON" -H "$BOB_AUTH" \
  -d "{\"id\":\"sub_board\",\"kind\":\"board\",\"group_id\":\"g_home\",\"baseline_mode\":\"all\",\"baseline_receipt_id\":\"$R_V1\"}" | jq -r .subscription.id)
SUB_BRANCH_B=$(curl -fsS -X POST "$BASE/subscriptions" -H "$JSON" -H "$BOB_AUTH" \
  -d '{"id":"sub_branch_b","kind":"branch","anchor_post_id":"p2"}' | jq -r .subscription.id)

BOARD_PAGE_BEFORE_MOVE=$(curl -fsS "$BASE/subscriptions/$SUB_BOARD/pages?limit=1" -H "$BOB_AUTH")
CUR_CROSS_MOVE=$(jqf .next_cursor "$BOARD_PAGE_BEFORE_MOVE")
assert_eq "$(jqf '.items[0].id' "$BOARD_PAGE_BEFORE_MOVE")" "p1" "board page before move shows p1"
[[ -n "$CUR_CROSS_MOVE" && "$CUR_CROSS_MOVE" != "null" ]]
echo "  ✓ durable cursor crosses the later move/restart: ${CUR_CROSS_MOVE:0:18}..."

echo "== 3. moderator moves 乙; old address keeps destination =="
MOVE_P2=$(curl -fsS -X POST "$BASE/posts/p2/move" -H "$JSON" -H "$MOD_AUTH" \
  -d '{"target_group_id":"g_lab","target_topic_id":"t_lab"}')
assert_eq "$(jqf '.move.moved_post_ids | join(",")' "$MOVE_P2")" "p2" "moved 乙 with its current subtree"
OLD_ADDR=$(curl -fsS "$BASE/old-addresses/posts/p2" -H "$BOB_AUTH")
assert_eq "$(jqf '.moves | length' "$OLD_ADDR")" "1" "old address has one traceable move"
assert_eq "$(jqf '.currently_in_group_id' "$OLD_ADDR")" "g_lab" "old address points to current group"

echo "== 4. edit after read; 丙 in old topic; stale reply to 乙 attaches to true parent =="
curl -fsS -X PATCH "$BASE/posts/p1" -H "$JSON" -H "$ALICE_AUTH" \
  -d '{"expected_version":1,"body":"甲 v2：【补充】因雨改到周日十点，地点改为社区活动中心。"}' >/dev/null
curl -fsS -X POST "$BASE/posts" -H "$JSON" -H "$ALICE_AUTH" \
  -d '{"id":"p3","topic_id":"t_home","parent_id":"p1","body":"丙：请问活动中心要怎么布置？"}' >/dev/null

P4_CREATE=$(curl -fsS -X POST "$BASE/posts" -H "$JSON" -H "$ALICE_AUTH" \
  -d '{"id":"p4","topic_id":"t_home","parent_id":"p2","body":"丁：桌子几点可以搬？"}')
assert_eq "$(jqf '.post.group_id' "$P4_CREATE")" "g_lab" "reply to moved 乙 inherits its true current group"
assert_eq "$(jqf '.post.topic_id' "$P4_CREATE")" "t_lab" "reply to moved 乙 inherits its true current topic"
assert_eq "$(jqf '.location_resolution.rule' "$P4_CREATE")" "reply_attached_to_current_true_parent" "stale topic is resolved"
curl -fsS -X POST "$BASE/posts" -H "$JSON" -H "$ALICE_AUTH" \
  -d '{"id":"p5","parent_id":"p4","body":"戊：我有推车。"}' >/dev/null
curl -fsS -X POST "$BASE/posts" -H "$JSON" -H "$ALICE_AUTH" \
  -d '{"id":"p6","parent_id":"p5","body":"己：那九点在门口碰面。"}' >/dev/null

MOVE_P4=$(curl -fsS -X POST "$BASE/posts/p4/move" -H "$JSON" -H "$MOD_AUTH" \
  -d '{"target_group_id":"g_home","target_topic_id":"t_home"}')
assert_eq "$(jqf '.move.moved_post_ids | join(",")' "$MOVE_P4")" "p4,p5,p6" "later move carries p4 branch/subtree into home board"

echo "== 5. restart service; late stale 'mark all read' still confirms only p1@v1 =="
restart_server
HEALTH_AFTER=$(curl -fsS "$BASE/health")
echo "  ✓ restarted; $(jq -c '{ok,event_seq}' <<<"$HEALTH_AFTER")"

LATE_RECEIPT=$(curl -fsS -X POST "$BASE/read-receipts" -H "$JSON" -H "$BOB_AUTH" \
  -d "{\"page_token\":\"$PG_OLD_LATE\",\"intent\":\"late_old_page_mark_all_read\"}")
assert_eq "$(jqf '.receipt.items | length' "$LATE_RECEIPT")" "1" "late all-read contains only the one displayed post"
assert_eq "$(jqf '.receipt.items[0].post_id' "$LATE_RECEIPT")" "p1" "late receipt is for 甲"
assert_eq "$(jqf '.receipt.items[0].displayed_version' "$LATE_RECEIPT")" "1" "late receipt keeps displayed v1"
P1_AFTER_LATE=$(curl -fsS "$BASE/posts/p1" -H "$BOB_AUTH")
assert_eq "$(jqf '.post.read.state' "$P1_AFTER_LATE")" "updated_after_read" "v2 supplement remains unread despite late all-read"

UNREAD_BEFORE_CURRENT_RECEIPT=$(curl -fsS "$BASE/subscriptions/$SUB_BRANCH/unread" -H "$BOB_AUTH" | jq -r .unread_count)
PG_V2=$(curl -fsS "$BASE/posts/p1/screen" -H "$BOB_AUTH" | jq -r .page_token)
R_V2=$(curl -fsS -X POST "$BASE/read-receipts" -H "$JSON" -H "$BOB_AUTH" \
  -d "{\"page_token\":\"$PG_V2\"}" | jq -r .receipt.id)
UNREAD_AFTER_CURRENT_RECEIPT=$(curl -fsS "$BASE/subscriptions/$SUB_BRANCH/unread" -H "$BOB_AUTH" | jq -r .unread_count)
assert_eq "$((UNREAD_BEFORE_CURRENT_RECEIPT - 1))" "$UNREAD_AFTER_CURRENT_RECEIPT" "receipt containing current p1 v2 reduces unread by exactly one"
echo "    unread $UNREAD_BEFORE_CURRENT_RECEIPT -> $UNREAD_AFTER_CURRENT_RECEIPT via $R_V2"

PG_P2=$(curl -fsS "$BASE/posts/p2/screen" -H "$BOB_AUTH" | jq -r .page_token)
R_P2=$(curl -fsS -X POST "$BASE/read-receipts" -H "$JSON" -H "$BOB_AUTH" \
  -d "{\"page_token\":\"$PG_P2\"}" | jq -r .receipt.id)
echo "  ✓ while still a private-group member, Bob really displayed/acked 乙 via $R_P2"

echo "== 6. revoke only Bob's access to destination group: receipt remains, body disappears =="
curl -fsS -X DELETE "$BASE/groups/g_lab/members/u_bob" -H "$MOD_AUTH" >/dev/null
DIRECT_CODE=$(curl -s -o /tmp/forum-direct.json -w '%{http_code}' "$BASE/posts/p2" -H "$BOB_AUTH")
assert_eq "$DIRECT_CODE" "403" "direct fetch of 乙 is forbidden"
BRANCH_BLOCK_PAGE=$(curl -fsS "$BASE/subscriptions/$SUB_BRANCH/pages?focus=p2&limit=1" -H "$BOB_AUTH")
assert_eq "$(jqf '.items[0].accessible' "$BRANCH_BLOCK_PAGE")" "false" "branch subscription still points to moved 乙"
assert_eq "$(jqf '.items[0].body' "$BRANCH_BLOCK_PAGE")" "null" "private body is absent from paged list"
assert_eq "$(jqf '.items[0].title' "$BRANCH_BLOCK_PAGE")" "null" "private title is absent from paged list"
assert_eq "$(jqf '.items[0].apply.method' "$BRANCH_BLOCK_PAGE")" "POST" "reader is offered an access application"
ACCESS_REQ=$(curl -fsS -X POST "$BASE/groups/g_lab/access-requests" -H "$JSON" -H "$BOB_AUTH" \
  -d '{"reason":"followed branch"}')
assert_eq "$(jqf '.access_request.status' "$ACCESS_REQ")" "pending" "access request created"
KEPT_RECEIPT=$(curl -fsS "$BASE/read-receipts/$R_P2" -H "$BOB_AUTH")
assert_eq "$(jqf '.receipt.items[0].displayed_version' "$KEPT_RECEIPT")" "1" "old display receipt is retained for audit/basis"
B_ANCHOR_META=$(curl -fsS "$BASE/subscriptions/$SUB_BRANCH_B" -H "$BOB_AUTH")
assert_eq "$(jqf '.subscription.anchor.accessible' "$B_ANCHOR_META")" "false" "B-anchored subscription survives access loss"
assert_eq "$(jqf '.subscription.anchor.title' "$B_ANCHOR_META")" "null" "subscription metadata hides private title"
assert_eq "$(jqf '.subscription.anchor.body' "$B_ANCHOR_META")" "null" "subscription metadata hides private body"

echo "== 7. continue the pre-move cursor after restart: find all permitted undisplayed posts =="
PAGE_CROSS_1=$(curl -fsS "$BASE/subscriptions/$SUB_BOARD/pages?limit=2&cursor=$CUR_CROSS_MOVE" -H "$BOB_AUTH")
assert_eq "$(jqf '[.items[].id] | join(",")' "$PAGE_CROSS_1")" "p3,p4" "cross-move cursor returns 丙 then moved-in 丁"
PG_CROSS_1=$(jqf .page_token "$PAGE_CROSS_1")
CUR_CROSS_2=$(jqf .next_cursor "$PAGE_CROSS_1")
CONTINUE_MID=$(curl -fsS "$BASE/me/continue" -H "$BOB_AUTH")
echo "$CONTINUE_MID" | jq -C '.positions[] | select(.subscription_id=="sub_board") | {continue_position:.continue_position.post_id, because}'
assert_eq "$(jqf '.positions[] | select(.subscription_id=="sub_board") | .continue_position.post_id' "$CONTINUE_MID")" "p3" "continue API gives a concrete first position before receipting the page"
curl -fsS -X POST "$BASE/read-receipts" -H "$JSON" -H "$BOB_AUTH" -d "{\"page_token\":\"$PG_CROSS_1\"}" >/dev/null

PAGE_CROSS_2=$(curl -fsS "$BASE/subscriptions/$SUB_BOARD/pages?limit=2&cursor=$CUR_CROSS_2" -H "$BOB_AUTH")
assert_eq "$(jqf '[.items[].id] | join(",")' "$PAGE_CROSS_2")" "p5,p6" "restarted paging reaches 戊/己"
assert_eq "$(jqf '.has_more' "$PAGE_CROSS_2")" "false" "six-post fixture is fully paged"
PG_CROSS_2=$(jqf .page_token "$PAGE_CROSS_2")
curl -fsS -X POST "$BASE/read-receipts" -H "$JSON" -H "$BOB_AUTH" -d "{\"page_token\":\"$PG_CROSS_2\"}" >/dev/null

BOARD_UNREAD=$(curl -fsS "$BASE/subscriptions/$SUB_BOARD/unread" -H "$BOB_AUTH")
assert_eq "$(jqf '.unread_count' "$BOARD_UNREAD")" "0" "board-only sub has no remaining readable unread"
assert_eq "$(jqf '.blocked_count' "$BOARD_UNREAD")" "0" "home board itself remains readable"

BRANCH_UNREAD=$(curl -fsS "$BASE/subscriptions/$SUB_BRANCH/unread" -H "$BOB_AUTH")
assert_eq "$(jqf '.unread_count' "$BRANCH_UNREAD")" "0" "branch sub has no remaining accessible unread"
assert_eq "$(jqf '.blocked_count' "$BRANCH_UNREAD")" "1" "only 乙's inaccessible private tile remains"

CONTINUE=$(curl -fsS "$BASE/me/continue" -H "$BOB_AUTH")
echo "$CONTINUE" | jq -C '.positions[] | {subscription_id,kind,unread_count,continue_position,access_required_position:{post_id:.access_required_position.post_id,group_id:.access_required_position.group_id,apply:.access_required_position.apply.url},because}'
assert_eq "$(jqf '[.positions[] | select(.continue_position != null)] | length' "$CONTINUE")" "0" "no authorized unread position remains"
assert_eq "$(jqf '[.positions[].because.subscription_id] | length' "$CONTINUE")" "3" "each position names its subscription and receipt basis"

echo
echo "SUCCESS: six posts reproduce collapse, move, stale receipt, version edit, ACL masking, restart-safe paging, and both subscription types."
echo "Database retained at: $DB"
