# 社区运动馆失物受理台

零第三方依赖的本地服务：受理实物与申领、按登记特征核对、签领物凭条、两个窗口原子出库、按号复查，并使用 SQLite 在重启后保持库房状态。

## 先回答海豚壶问题

两只外观相同、都贴蓝贴的海豚水壶：

- **完整一对一登记**（轮次 `dolphin-complete`，明确 `complete_pairing=true` 且不允许未找到物）：
  - 甲确知杯底为“星”，而 `I-D1=星`、`I-D2=月`，所以甲唯一指向 **第一只 `I-D1`**。
  - 因为馆方输入明确保证本轮确实一对一配完，所有完美配对中乙都只能对应 **第二只 `I-D2`**。因此甲签第一只后，乙可签第二只。
- **开放登记 / 允许另一件未找到的壶**（`dolphin-unfound`）：
  - 甲仍可凭“星”唯一核对 `I-U1`。
  - 乙只记得“星或月”，即使甲已签走 `I-U1`，也**不能靠排除法**把 `I-U2` 签给乙；乙继续显示待核。系统在开放模式下始终把已预留/出库的第一件保留在乙的证据集合中，防止事后把“排除别人已领件”误当成乙本人的唯一特征。
- **保持完整配对但两只底记都变成星**（`dolphin-both-star`）：
  - 图变成 `K2,2`，存在两种完美配对，甲原来的唯一指向消失，两份申领都不能签。
- **一次真实检查可解开**（`sealed-check`，封存窗）：
  - `I-K1` 的底记锁在内袋。保管员执行一次真实检查，结果为“月”后，记“星”的申领 `C-K` 只剩 `I-K2` 相容，可签出。
- **一次复访可解开**（`revisit-answer`，复访窗）：
  - `C-R` 原本只记得“星或月”。复访时服务端生成当时可见的两选一题，失主有约束力地回答“月”后，唯一指向 `I-R2`。轮次由服务端顺序登记，不能补造前访，超过愿答轮数会被拒绝。

## 启动

```bash
./start.sh
# 或：python3 app.py --db lostfound.db --seed-demo --port 8000
```

打开 <http://127.0.0.1:8000/> 使用本地控制台。数据库是 `lostfound.db`，重启不丢号、不丢凭条和库房状态。

运行测试：

```bash
python3 -m unittest -v tests.test_lostfound
```

测试包含上述三种海豚壶重放、封存检查、复访轮数、隐私视图，以及真实 HTTP 两窗口并发出库和重启复查。

## 命令行入口

不启动网页/HTTP 也可以本地办理：

```bash
./lf --db lostfound.db seed --reset
./lf --db lostfound.db reconcile dolphin-complete
./lf --db lostfound.db track C-A
./lf --db lostfound.db sign --claim C-A
./lf --db lostfound.db outbound --voucher P-0001 --window W1
./lf --db lostfound.db record C-R
./lf --db lostfound.db stock-check
```

`item`、`claim`、`round` 接受 JSON 字符串或 `@payload.json`。

## 入口一览

| 环节 | 入口 |
| --- | --- |
| 受理轮次/选择窗口 | `POST /api/rounds` |
| 导入别的手续已确认的成对分配 | `POST /api/rounds/{round_id}/confirmed-pairs` |
| 受理实物 | `POST /api/items` |
| 受理申领/封存说明 | `POST /api/claims` |
| 核对 | `GET/POST /api/rounds/{round_id}/reconcile` |
| 签出全部当前确定申领 | `POST /api/rounds/{round_id}/sign-determinate` |
| 签单并生成凭条 | `POST /api/vouchers` |
| 封存窗检查优先级 | `GET /api/rounds/{round_id}/plan-inspection` |
| 保管员真实开袋检查 | `POST /api/inspections` |
| 复访题目 | `GET /api/rounds/{round_id}/next-question?claim_no=...` |
| 登记复访约束回答 | `POST /api/claims/{claim_no}/revisit` |
| 班次准备物件 | `POST /api/shifts/{shift_id}/prepare` |
| 出库交接 | `POST /api/outbound` |
| 普通按号复查 | `GET /api/track/{claimNoOrVoucherNoOrItemNo}` |
| 按序复访/封存笔录 | `GET /api/claims/{claim_no}/record` |
| 库房一致性检查 | `GET /api/stock-check` |
| 登记无失物虚报 | `POST /api/claims/{claim_no}/no-match` |
| 显式登记无人认领物 | `POST /api/items/{item_no}/unclaimed` |

## 受理数据格式

特征记忆支持三种表达：

```json
{ "bottom_mark": "星" }
```

确知一个值；

```json
{ "bottom_mark": ["星", "月"] }
```

不确定，只知道是两个取值之一；

```json
{ "knot_count": "unseen" }
```

或省略该特征，表示没看过。

实物可以区分拾物员已登记的非公开暗记和锁在内袋、尚未揭示的真实特征：

```json
{
  "round_id": "sealed-check",
  "item_no": "I-K1",
  "label": "待开袋壶",
  "appearance": {"form": "dolphin_kettle", "sticker": "blue"},
  "features": {
    "bottom_mark": {
      "value": "月",
      "revealed": false,
      "options": ["星", "月"]
    }
  }
}
```

## 命令行走一遍

```bash
curl -sS -X POST http://127.0.0.1:8000/api/admin/seed-dolphins \
  -H 'Content-Type: application/json' -d '{"reset":true}'

# 完整一对一：甲 -> I-D1，乙在所有完美配对中 -> I-D2
curl -sS http://127.0.0.1:8000/api/rounds/dolphin-complete/reconcile

# 普通申领人复查：只回本人能否领取和下一步
curl -sS http://127.0.0.1:8000/api/track/C-A

# 甲签单
curl -sS -X POST http://127.0.0.1:8000/api/vouchers \
  -H 'Content-Type: application/json' \
  -d '{"claim_no":"C-A","window_id":"sign-desk"}'
# 假设返回 P-0001

# 两个窗口拿同一凭条同时出库
curl -sS -X POST http://127.0.0.1:8000/api/outbound \
  -H 'Content-Type: application/json' \
  -d '{"voucher_no":"P-0001","window_id":"W1"}' &
curl -sS -X POST http://127.0.0.1:8000/api/outbound \
  -H 'Content-Type: application/json' \
  -d '{"voucher_no":"P-0001","window_id":"W2"}' &
wait
```

预期一个响应为 `200` 成功交接，另一个为 `409 voucher_already_redeemed`。SQLite 的 `BEGIN IMMEDIATE` 事务、凭条状态条件更新和 `handoffs.voucher_no` 主键共同保证只有一份成功交接。

重启服务后：

```bash
curl -sS http://127.0.0.1:8000/api/track/P-0001
curl -sS http://127.0.0.1:8000/api/stock-check
```

仍显示该凭条已出库、实物状态为 `out`、`consistent=true`、`handoff_count=1`。

## 核对规则

- 默认是**开放登记**：不把新来的一群失主和实物假设成完美配对；只按本人特征在全部登记实物中找唯一相容件，并阻止两份唯一申领冲突同一件。
- 只有显式给出 `complete_pairing=true` 且 `allow_unfound=false` 的封闭完整登记，才计算所有完美一对一配对。若某条边属于所有完美配对，才允许凭排除/全局唯一性签出。
- 完整登记找不到完美配对时返回 `complete_manifest_inconsistent`，不会硬签。
- 封存窗预算按“检查了几件实物”计数；复访窗按服务端顺序轮次和失主 `willing_rounds` 限制，班次可准备物件数由 `prepare_budget` 限制。
- 普通申领号复查不返回暗记、候选实物列表或其他申领描述；内部核对入口才返回编号级匹配理由。
