# 街坊小卖部：双售货端 + 维护端模拟后台

一个**零外部依赖**的 Node.js 本地服务，用来模拟售货机的投币、币筒、币道传感器、卡币、暂存斗、取币口、封闭找零杯、订单领取、断电恢复和对账。

不连接银行、支付网关或真实硬件；所有“硬件事实”都由本地 HTTP 请求登记。

## 运行

```bash
npm test          # 领域规则单测
npm run demo      # 自检查端到端演示：含断电重启、错误重放、对账
./walkthrough.sh  # 纯 curl 版本：从空机开始补币、购买、卡币、领取、重启、对账
```

默认手动启动：

```bash
rm -f data/vending-state.json
npm start
```

三个本地入口：

| 入口 | 地址 | 作用 |
|---|---|---|
| 售货端 A | http://127.0.0.1:4101 | 只查询/办理机器 A 的订单和领取 |
| 售货端 B | http://127.0.0.1:4102 | 只查询/办理机器 B 的订单和领取 |
| 维护端 | http://127.0.0.1:4200 | 补币、硬件事件、修币道、取币实数、审计、对账 |

状态在每次成功写入后原子保存到 `data/vending-state.json`。断电或 `kill` 后重新 `npm start` 即从硬币/订单/索赔/幂等记录的实数恢复。

可用环境变量：

```bash
DATA_FILE=./data/a.json POS_A_PORT=4101 POS_B_PORT=4102 MAINT_PORT=4200 npm start
```

## 核心事实模型

系统刻意区分以下事实，不能互相替代：

1. **投币事实**：每枚投入硬币有唯一 `coinId`，位置为 `ESCROW`（暂存斗）。
2. **币筒事实**：补币后硬币位置为 `TUBE`，按面值和币道管理。
3. **币道事实**：
   - `channel-sensor` 只表示硬币经过传感器；
   - `coin-arrived` 才表示到达取币口；
   - `coin-jam` 可记录卡在 `LAST_SEGMENT`；
   - `channel-repaired` 只清除币道故障，不会移动卡币；
   - 卡币必须由维护端 `coins/:coinId/recover` 登记实物归位。
4. **售货口事实**：`goods-delivery` 是出货回执，和找零进度分开。
5. **取币口事实**：`pickup-door` 只记录门被打开；只有 `pickup/confirm` 或 `pickup-count` 登记的实物取走/剩余硬币才增加 `collectedAmount`。
6. **领取事实**：每笔差额/退款/找零杯有独立 `claimId`，只属于一台机器和一个订单。B 机出币不会冲减 A 机索赔。

硬币位置：

```text
TUBE      币筒可找零硬币
ESCROW    顾客投入、仍在暂存斗的原币
CHANNEL   已进入币道
JAMMED    卡在币道（例如最后一段）
TRAY      已到取币口、尚未确认取走
CUP       在封闭找零杯内
CASHBOX   已结算入钱箱
CUSTOMER  已由顾客取走
MAINT     维护人员取出
```

## 幂等和乱序/重复回调

所有写请求应带 `requestId`、`eventId` 或 `Idempotency-Key` 请求头。

- 相同键 + 相同报文重放：返回首次结果，并带 `replayed: true`，不会重复出币/扣款。
- 相同键但金额等报文字段不同：返回 `409 IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_BODY`。
- 旧出货回执、重复开门、重复修币道都不会创建第二笔钱或改变新状态。
- 已 `DELIVERED` 的出币不能再被报成卡币；已 `JAMMED` 的币也不能用过时到达回执算成已交到顾客手中。

## 主要 HTTP 接口

### 售货端 A/B

```text
POST /orders
GET  /orders/:orderId
POST /orders/:orderId/coins
POST /orders/:orderId/vend
POST /orders/:orderId/dispense-next
POST /orders/:orderId/cancel
POST /orders/:orderId/cup/prepare

POST /orders/:orderId/claims/:claimId/claim
POST /orders/:orderId/claims/:claimId/dispense-next
POST /claims/:claimId/dispense-next
```

`GET /orders/:orderId` 返回：

- `paidAmount`：已登记实投金额；
- `totalAmount`：货款；
- `changeDue`：应付找零；
- `handedOutAmount`：已到本机取币口/找零杯、已从退款中扣除的金额；
- `collectedAmount`：经实物领取确认、顾客已取得的金额；
- `totalClaimRemaining`：仍可由本机订单领取/路由的金额；
- `openClaims`、`trayCoins`、`customerCoins`、`pickupCup`；
- `hasUnpaidClaim`、`hasMoneyAwaitingPickup`。

在 B 端查询 A 订单直接 `404 ORDER_NOT_FOUND_ON_THIS_TERMINAL`。

### 维护端

```text
GET  /health
GET  /machines
GET  /machines/:machineId/state
GET  /machines/:machineId/reconciliation

POST /machines/:machineId/restock
POST /machines/:machineId/orders/:orderId/coins
POST /machines/:machineId/audit

POST /machines/:machineId/channels/:channelId/fault
POST /machines/:machineId/events/channel-repaired
POST /machines/:machineId/coins/:coinId/recover

POST /machines/:machineId/events/goods-delivery
POST /machines/:machineId/events/channel-sensor
POST /machines/:machineId/events/coin-arrived
POST /machines/:machineId/events/coin-jam
POST /machines/:machineId/events/pickup-door

POST /machines/:machineId/pickup/confirm
POST /machines/:machineId/pickup-count

POST /machines/:machineId/cups/:cupId/seal
POST /machines/:machineId/cups/:cupId/send-to-pickup
```

## 题目例子对应的请求顺序

商品 7 元，投入 10 元；A 机币筒有一枚 2 元、三枚 1 元。系统优先选择属于可行 exact-change 组合且面值较大的硬币，因此第一单找 `2+1`，留下两枚 1 元，足以接随后的 1 元找零单。

完整可运行版本见 [`walkthrough.sh`](./walkthrough.sh)，关键顺序如下：

```bash
# 空机补币
curl -sS localhost:4600/machines/A/restock -H 'content-type: application/json' -d '{
  "requestId":"RESTOCK-A",
  "coins":[
    {"id":"A-T2-1","denomination":2},
    {"id":"A-T1-1","denomination":1},
    {"id":"A-T1-2","denomination":1},
    {"id":"A-T1-3","denomination":1}
  ]
}'

# 下单并投入唯一编号的 10 元硬币
curl -sS localhost:4101/orders -H 'content-type: application/json' -d '{
  "requestId":"A1-CREATE","orderId":"A-1001","totalAmount":7
}'
curl -sS localhost:4101/orders/A-1001/coins -H 'content-type: application/json' -d '{
  "requestId":"A1-PAY","coinId":"A1-PAY-10","denomination":10
}'

# 10 改成 5 但不补投：另建 A-05 时 vend 返回 402；
# 复用 A1-PAY 改 denomination:5 会返回 409，实投仍是 10。

curl -sS localhost:4101/orders/A-1001/vend -H 'content-type: application/json' -d '{
  "requestId":"A1-VEND"
}'

# 第一枚出 2 元：指令、传感器、到达是三个请求
curl -sS localhost:4101/orders/A-1001/dispense-next -H 'content-type: application/json' -d '{
  "requestId":"A1-DSP2","dispenseId":"DSP-A1-2"
}'
curl -sS localhost:4600/machines/A/events/channel-sensor -H 'content-type: application/json' -d '{
  "requestId":"A1-S2","dispenseId":"DSP-A1-2"
}'
curl -sS localhost:4600/machines/A/events/coin-arrived -H 'content-type: application/json' -d '{
  "requestId":"A1-A2","dispenseId":"DSP-A1-2"
}'

# 第二枚 1 元经过传感器后卡在最后一段
curl -sS localhost:4600/machines/A/events/coin-jam -H 'content-type: application/json' -d '{
  "requestId":"A1-JAM","dispenseId":"DSP-A1-JAM","location":"LAST_SEGMENT"
}'

# 此时订单显示 handedOutAmount=2、totalClaimRemaining=1，绝不会显示 3 元已取齐。

# 重复开门只是重放；不会把托盘里的 2 元变已取走。
curl -sS localhost:4600/machines/A/events/pickup-door -H 'content-type: application/json' -d '{
  "requestId":"A1-DOOR","orderId":"A-1001"
}'

# 修币道、找回卡币，然后凭本机订单和 CLM-A-1001-CHANGE 领剩余 1 元。
# 另一台机器 B 的任何出币都不会改动这笔 claim。

# 最后用硬币实数审计；customerObtainedAmount 必须等于顾客实际取走的金额。
curl -sS localhost:4600/machines/A/audit -H 'content-type: application/json' -d '{
  "requestId":"A-AUDIT-FINAL",
  "physicalCounts":{
    "CUSTOMER":{"1":3,"2":2},
    "CASHBOX":{"5":1,"10":2},
    "ESCROW":{"5":1}
  },
  "customerObtainedAmount":7
}'
```

## 两种营业办法

### 办法一：封闭找零杯（`mode:"CUP"`）

1. 创建订单时传 `"mode":"CUP"`。
2. 投足后调用 `/orders/:id/cup/prepare`；系统只有在能凑出**完整 exact change** 时才预留硬币。
3. 可传 `capacityCoins` 检查杯容量，传 `ttlSeconds` 记录周转期限。
4. 维护端 `/cups/:cupId/seal` 复核金额和枚数并封存。
5. 未封存齐套时 `/vend` 返回 `409 CUP_NOT_SEALED_COMPLETE`；超过周转期限返回 `409 CUP_DEADLINE_EXCEEDED`。
6. 未实际报出货前撤单：杯内币退回币筒，原投入硬币从 `ESCROW` 转到 `TRAY`，生成 `REFUND_ORIGINAL` 领取记录。
7. 出货后杯子送到取币口；`takenCupIds` 整杯确认取走。一个 `cupId` 只能属于一个订单和一个 claim。

### 办法二：逐枚即时找零（默认 `mode:"STREAM"`）

- 每次 `dispense-next` 都根据当前剩余金额、健康币道和币筒实数重新决定下一枚面值。
- 传感器通过不增加 `handedOutAmount`；只有 `coin-arrived` 才增加。
- 卡币后 claim 仍为 `OPEN`，剩余金额仍可凭**本机订单号 + claimId** 领取，不会被改成取消单。
- 撤单时生成 `REFUND_NET`：

```text
净退款 = 已投金额 - handedOutAmount
```

  已到本机托盘/杯中的钱仍挂在原订单下，顾客取走这些钱加净退款后总额等于已投金额，不能重复领取。
- 未交过找零时可选择 `"refundMode":"ORIGINAL"`，系统会把该订单投入的原硬币从暂存斗转到取币口。

## 断电恢复与对账

`POST /machines/:id/audit` 接收维护人员清点的实数：

```json
{
  "requestId": "A-AUDIT-1",
  "physicalCounts": {
    "TUBE": {"1": 1, "2": 0},
    "TRAY": {"2": 1},
    "JAMMED": {"1": 1},
    "ESCROW": {"10": 1}
  },
  "customerObtainedAmount": 0
}
```

返回的 `reconciliation` 会检查：

- 每种位置、面值的台账数量与实点是否一致；
- 订单 `paidAmount` 是否等于其投入硬币面值之和；
- 订单 `collectedAmount` 是否等于位置 `CUSTOMER` 的订单硬币之和；
- claim 的 `amountDelivered` 是否等于其硬币面值之和；
- 是否有一枚硬币被两个 claim 共用；
- 每个杯是否只回指一个订单；
- `balanced` 是否为 `true`。

`npm run demo` 会在卡币、托盘有 2 元、顾客尚未取走的时刻 kill 并重启，再用上述实数审计；最后再次重启并验证旧领取请求重放不会重复入账。
