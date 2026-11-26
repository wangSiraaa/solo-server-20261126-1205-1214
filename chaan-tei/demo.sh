#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# 茶餐厅午市重放：本地 HTTP 从空店走到结桌（逐份跟单模式）
# 用法：bash demo.sh
set -u
cd "$(dirname "$0")"
PORT=8617
BASE="http://127.0.0.1:${PORT}"

call() { # call 说明 METHOD 路径 [JSON] [幂等键]
  local title="$1" method="$2" path="$3" body="${4:-}" idem="${5:-}"
  echo "──────────────────────────────────────────────"
  echo "▸ $title"
  echo "  $method $path ${body:+ body=$body}"
  local args=(-s -w '\n[HTTP %{http_code}]\n' -X "$method" "$BASE$path")
  [ -n "$body" ] && args+=(-H 'Content-Type: application/json' -d "$body")
  [ -n "$idem" ] && args+=(-H "Idempotency-Key: $idem")
  curl "${args[@]}"
}
# 中文 query 必须 URL 编码
qenc() { python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "$1"; }
# 发 POST 并从 JSON 响应取一个字段
postf() { # postf JSON 路径 JSON路径表达式
  curl -s -X POST "$BASE$2" -H 'Content-Type: application/json' -d "$1" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print(eval(sys.argv[1]))' "$3"
}
# 按做法拉队列里某食客的点菜单号
queueslip() { # queueslip 做法 diner_id
  curl -s "$BASE/queue?prep=$(qenc "$1")" \
    | python3 -c 'import sys,json
q=json.load(sys.stdin)["data"]["queue"]
print([x["id"] for x in q if x["diner_id"]==sys.argv[1]][0])' "$2"
}
wokplate() { # wokplate 锅号 slip_id
  curl -s "$BASE/wok/$1" | python3 -c 'import sys,json
ps=json.load(sys.stdin)["data"]["portions"]
print([p["plate_id"] for p in ps if p["slip_id"]==sys.argv[1] and p["plate_id"]][0])' "$2"
}
wait_up() { for _ in $(seq 1 50); do curl -s "$BASE/healthz" >/dev/null && return 0; sleep 0.1; done; }

rm -f chaan.db chaan.db-wal chaan.db-shm server.log r1.json r2.out
fuser -k "$PORT/tcp" 2>/dev/null; sleep 0.3
python3 kitchen.py --port "$PORT" >server.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT
wait_up || { echo '服务没起来'; cat server.log; exit 1; }

echo "================ 0. 空店重置 ================"
call "清场" POST /admin/reset '{}'

echo "================ 1. 普通小单：一次三份，只改其中一份 ================"
D_JI=$(postf '{"table":"己桌","name":"己"}' /diners 'd["data"]["diner_id"]')
curl -s -X POST "$BASE/orders" -H 'Content-Type: application/json' \
  -d "{\"diner_id\":\"$D_JI\",\"prep\":\"干炒牛河-标准\",\"qty\":3}" \
  | python3 -c 'import sys,json;[print(x) for x in json.load(sys.stdin)["data"]["slip_ids"]]' >.run/slips3
S1=$(sed -n 1p .run/slips3); S2=$(sed -n 2p .run/slips3); S3=$(sed -n 3p .run/slips3)
echo "  三份独立菜单：$S1 $S2 $S3"
call "只把第二份 $S2 改成免葱（另两份不动）" POST /slips/modify \
  "{\"slip_id\":\"$S2\",\"new_prep\":\"干炒牛河-免葱\"}"
call "撤掉第三份 $S3（未喊炒，直接作废）" POST /slips/cancel \
  "{\"slip_id\":\"$S3\"}"
call "炒档拉队列看剩余待炒" GET "/queue?prep=$(qenc 干炒牛河-标准)"

echo "================ 2. 食客换桌：锅里已喊的份不能少 ================"
D_GENG=$(postf '{"table":"庚桌","name":"庚"}' /diners 'd["data"]["diner_id"]')
postf "{\"diner_id\":\"$D_GENG\",\"prep\":\"豉油皇炒面-标准\",\"qty\":1}" /orders 'd["data"]["slip_ids"][0]' >.run/g
G=$(cat .run/g)
W_MOVE=$(curl -s -X POST "$BASE/wok/call" -H 'Content-Type: application/json' \
  -d "{\"slip_ids\":[\"$G\"]}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["wok_id"])')
echo "──────────────────────────────────────────────"
echo "▸ 喊炒 -> 锅 $W_MOVE 钉住这份菜单 $G"
call "庚换到辛桌（菜还在锅里，份跟着人走）" POST /diners/move \
  "{\"diner_id\":\"$D_GENG\",\"to_table\":\"辛桌\"}"
call "这锅报熟" POST /wok/finish "{\"wok_id\":\"$W_MOVE\"}"
GP=$(wokplate "$W_MOVE" "$G")
call "按辛桌派盘（旧庚桌派不出去）" POST /plates/deliver \
  "{\"plate_id\":\"$GP\",\"table\":\"辛桌\"}"
call "若仍报庚桌 -> 应当 409" POST /plates/deliver \
  "{\"plate_id\":\"$GP\",\"table\":\"庚桌\"}"

echo "================ 3. 午市主场景：甲乙同做法一锅，甲撤单，丙加入 ================"
D_JIA=$(postf '{"table":"甲桌","name":"甲"}' /diners 'd["data"]["diner_id"]')
D_YI=$(postf  '{"table":"乙桌","name":"乙"}' /diners 'd["data"]["diner_id"]')
postf "{\"diner_id\":\"$D_JIA\",\"prep\":\"豉油皇炒面-标准\",\"qty\":1}" /orders 'd["data"]["slip_ids"][0]' >.run/a
postf "{\"diner_id\":\"$D_YI\",\"prep\":\"豉油皇炒面-标准\",\"qty\":1}"  /orders 'd["data"]["slip_ids"][0]' >.run/b
A=$(cat .run/a); B=$(cat .run/b)
echo "  甲=$A 乙=$B"
W1=$(curl -s -X POST "$BASE/wok/call" -H 'Content-Type: application/json' \
  -d "{\"slip_ids\":[\"$A\",\"$B\"]}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["wok_id"])')
echo "──────────────────────────────────────────────"
echo "▸ 炒档把两份同锅喊炒（每份钉菜单号）-> 锅 $W1"
call "甲撤单 —— 锅里那份撤不掉，熟后按余菜处理" POST /slips/cancel \
  "{\"slip_id\":\"$A\"}"
D_BING=$(postf '{"table":"丙桌","name":"丙"}' /diners 'd["data"]["diner_id"]')
postf "{\"diner_id\":\"$D_BING\",\"prep\":\"豉油皇炒面-标准\",\"qty\":1}" /orders 'd["data"]["slip_ids"][0]' >.run/cin
CIN=$(cat .run/cin)
echo "──────────────────────────────────────────────"
echo "▸ 丙此时点同款（此锅已满，丙单 $CIN 进队列等下一锅）"
call "照单结这锅：两份都真炒出来，甲那份起锅即余菜" POST /wok/finish \
  "{\"wok_id\":\"$W1\"}"
call "传菜查 $W1 的盘号来路（portion 钉菜单、盘钉 portion）" GET "/wok/$W1"
PA=$(wokplate "$W1" "$A"); PB=$(wokplate "$W1" "$B")
echo "  甲的盘=$PA（余菜） 乙的盘=$PB（待派）"
call "余菜想再派出去 -> 409 余菜不可派" POST /plates/deliver \
  "{\"plate_id\":\"$PA\",\"table\":\"甲桌\"}"
call "乙的盘派到乙桌" POST /plates/deliver \
  "{\"plate_id\":\"$PB\",\"table\":\"乙桌\"}"
call "乙已上桌后撤单 -> 409 已派盘不能退款" POST /slips/cancel \
  "{\"slip_id\":\"$B\"}"
W2=$(curl -s -X POST "$BASE/wok/call" -H 'Content-Type: application/json' \
  -d "{\"slip_ids\":[\"$CIN\"]}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["wok_id"])')
echo "──────────────────────────────────────────────"
echo "▸ 丙的单单独喊炒 -> 锅 $W2"
call "报熟" POST /wok/finish "{\"wok_id\":\"$W2\"}"
PC=$(wokplate "$W2" "$CIN")
call "派给丙桌" POST /plates/deliver "{\"plate_id\":\"$PC\",\"table\":\"丙桌\"}"
call "余菜清单（甲那份在此，不可重派）" GET /leftovers

echo "================ 4. 另开一锅：乙丁两份炒前入单、均不撤，报熟各派一盘 ================"
D_DING=$(postf '{"table":"丁桌","name":"丁"}' /diners 'd["data"]["diner_id"]')
postf "{\"diner_id\":\"$D_YI\",\"prep\":\"星洲炒米-标准\",\"qty\":1}"  /orders 'd["data"]["slip_ids"][0]' >.run/b2
postf "{\"diner_id\":\"$D_DING\",\"prep\":\"星洲炒米-标准\",\"qty\":1}" /orders 'd["data"]["slip_ids"][0]' >.run/d1
B2=$(cat .run/b2); D1=$(cat .run/d1)
W3=$(curl -s -X POST "$BASE/wok/call" -H 'Content-Type: application/json' \
  -d "{\"slip_ids\":[\"$B2\",\"$D1\"]}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["wok_id"])')
echo "──────────────────────────────────────────────"
echo "▸ 两份同锅喊炒、报熟两份 -> 锅 $W3"
call "" POST /wok/finish "{\"wok_id\":\"$W3\"}"
PB2=$(wokplate "$W3" "$B2"); PD1=$(wokplate "$W3" "$D1")
call "各派一盘（乙、丁）" POST /plates/deliver "{\"plate_id\":\"$PB2\",\"table\":\"乙桌\"}"
call "" POST /plates/deliver "{\"plate_id\":\"$PD1\",\"table\":\"丁桌\"}"

echo "--- 试 1：乙临时改免葱，而锅里已经有葱 -> 原锅不能给乙这份 ---"
D_REN=$(postf '{"table":"壬桌","name":"壬"}' /diners 'd["data"]["diner_id"]')
postf "{\"diner_id\":\"$D_YI\",\"prep\":\"干炒牛河-标准\",\"qty\":1}"  /orders 'd["data"]["slip_ids"][0]' >.run/b3
postf "{\"diner_id\":\"$D_REN\",\"prep\":\"干炒牛河-标准\",\"qty\":1}" /orders 'd["data"]["slip_ids"][0]' >.run/r1
B3=$(cat .run/b3); R1=$(cat .run/r1)
W4=$(curl -s -X POST "$BASE/wok/call" -H 'Content-Type: application/json' \
  -d "{\"slip_ids\":[\"$B3\",\"$R1\"]}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["wok_id"])')
echo "──────────────────────────────────────────────"
echo "▸ 喊炒并报熟（葱已下锅）-> 锅 $W4"
call "" POST /wok/finish "{\"wok_id\":\"$W4\"}"
call "乙这时改免葱 -> 旧单留锅转余菜，另开免葱新单等下一锅" POST /slips/revise \
  "{\"slip_id\":\"$B3\",\"new_prep\":\"干炒牛河-免葱\"}"
PB3=$(wokplate "$W4" "$B3")
call "原锅那盘再想派给乙 -> 409 余菜不可派" POST /plates/deliver \
  "{\"plate_id\":\"$PB3\",\"table\":\"乙桌\"}"
PR1=$(wokplate "$W4" "$R1")
call "壬的盘照派" POST /plates/deliver "{\"plate_id\":\"$PR1\",\"table\":\"壬桌\"}"
call "免葱新单在队列里（下一锅才轮到）" GET "/queue?prep=$(qenc 干炒牛河-免葱)"

echo "--- 试 2：保留原做法，炒档只少报一份成品 -> 传菜派不出两盘 ---"
D_GUI=$(postf '{"table":"癸桌","name":"癸"}' /diners 'd["data"]["diner_id"]')
D_ZI=$(postf  '{"table":"子桌","name":"子"}' /diners 'd["data"]["diner_id"]')
postf "{\"diner_id\":\"$D_GUI\",\"prep\":\"豉油皇炒面-标准\",\"qty\":1}" /orders 'd["data"]["slip_ids"][0]' >.run/k1
postf "{\"diner_id\":\"$D_ZI\",\"prep\":\"豉油皇炒面-标准\",\"qty\":1}"  /orders 'd["data"]["slip_ids"][0]' >.run/z1
K1=$(cat .run/k1); Z1=$(cat .run/z1)
W5=$(curl -s -X POST "$BASE/wok/call" -H 'Content-Type: application/json' \
  -d "{\"slip_ids\":[\"$K1\",\"$Z1\"]}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["wok_id"])')
echo "──────────────────────────────────────────────"
echo "▸ 喊炒两份 -> 锅 $W5"
call "起锅只报癸那份熟，子的那份缺报（SHORT）" POST /wok/finish \
  "{\"wok_id\":\"$W5\",\"reported\":[\"$K1\"]}"
call "锅里只有一个 READY 盘，子那份是 SHORT" GET "/wok/$W5"
PK1=$(wokplate "$W5" "$K1")
call "派癸的盘（成功）" POST /plates/deliver "{\"plate_id\":\"$PK1\",\"table\":\"癸桌\"}"
call "给不存在的盘号派 -> 404（不会凭空派第二盘）" POST /plates/deliver \
  '{"plate_id":"P9999","table":"子桌"}'
call "子那张单已退回队列等重炒" GET "/queue?prep=$(qenc 豉油皇炒面-标准)"

echo "================ 5. 最后一盘两人同时报送不同桌：唯一归属 ================"
D_CHOU=$(postf '{"table":"丑桌","name":"丑"}' /diners 'd["data"]["diner_id"]')
postf "{\"diner_id\":\"$D_CHOU\",\"prep\":\"雪菜肉丝面-标准\",\"qty\":1}" /orders 'd["data"]["slip_ids"][0]' >.run/u1
U1=$(cat .run/u1)
W6=$(curl -s -X POST "$BASE/wok/call" -H 'Content-Type: application/json' \
  -d "{\"slip_ids\":[\"$U1\"]}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["wok_id"])')
echo "──────────────────────────────────────────────"
echo "▸ 单独成锅报熟，成为最后一盘 -> 锅 $W6"
call "" POST /wok/finish "{\"wok_id\":\"$W6\"}"
PU=$(wokplate "$W6" "$U1")
echo "  两个传菜员同时抢 $PU（都报丑桌），只有一个能赢："
curl -s -w '\n[HTTP %{http_code}]\n' -X POST "$BASE/plates/deliver" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: race-a' \
  -d "{\"plate_id\":\"$PU\",\"table\":\"丑桌\"}" > .run/r1.json &
RA=$!
curl -s -w '\n[HTTP %{http_code}]\n' -X POST "$BASE/plates/deliver" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: race-b' \
  -d "{\"plate_id\":\"$PU\",\"table\":\"丑桌\"}" > .run/r2.json &
RB=$!
wait "$RA" "$RB"
echo "  --- 报送结果 A ---"; cat .run/r1.json
echo "  --- 报送结果 B ---"; cat .run/r2.json
call "再查盘：只可能有一个 delivery 归属" GET "/plates/$PU"
call "若有人错报甲桌 -> 409 桌别不符（食客当前在丑桌）" POST /plates/deliver \
  "{\"plate_id\":\"$PU\",\"table\":\"甲桌\"}"

echo "================ 6. 客户端丢回应：幂等键重连查回同一次派盘 ================"
D_YIN=$(postf '{"table":"寅桌","name":"寅"}' /diners 'd["data"]["diner_id"]')
postf "{\"diner_id\":\"$D_YIN\",\"prep\":\"雪菜肉丝面-标准\",\"qty\":1}" /orders 'd["data"]["slip_ids"][0]' >.run/y1
Y1=$(cat .run/y1)
W7=$(curl -s -X POST "$BASE/wok/call" -H 'Content-Type: application/json' \
  -d "{\"slip_ids\":[\"$Y1\"]}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["wok_id"])')
echo "──────────────────────────────────────────────"
echo "▸ 成锅报熟 -> 锅 $W7"
call "" POST /wok/finish "{\"wok_id\":\"$W7\"}"
PY=$(wokplate "$W7" "$Y1")
call "第一次派盘（回应在网络上丢了）" POST /plates/deliver \
  "{\"plate_id\":\"$PY\",\"table\":\"寅桌\"}" "lost-response-key-001"
call "客户端重连，带同一幂等键重试 -> 同一 delivery，不产生第二笔" POST /plates/deliver \
  "{\"plate_id\":\"$PY\",\"table\":\"寅桌\"}" "lost-response-key-001"

echo "================ 7. 结桌：桌账只列实际交给该桌的菜 ================"
for t in 甲桌 乙桌 丙桌 丁桌 辛桌 壬桌 癸桌 丑桌 寅桌 己桌; do
  call "$t 桌账" GET "/tables/$(qenc "$t")/bill"
done

echo "================ 8. 重启服务：凭桌号/锅号查回 ================"
kill "$SRV"; wait "$SRV" 2>/dev/null
python3 kitchen.py --port "$PORT" >>server.log 2>&1 &
SRV=$!
wait_up
call "重启后查乙桌账" GET "/tables/$(qenc 乙桌)/bill"
call "重启后查甲乙那锅 $W1（含余菜来路）" GET "/wok/$W1"
call "重启后查余菜清单" GET /leftovers
call "重启后总览" GET /state
echo
echo "演示完成。数据库文件 chaan.db 已落盘，可用 curl 继续查；服务进程随脚本退出而停。"
