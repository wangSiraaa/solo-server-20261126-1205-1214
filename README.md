# 已实测评价服务

一个零外部依赖、可本机启动的 Node.js 服务，用来解决“一单两件、分批到货、只换走一件时，评价到底挂哪款”的问题。

核心规则只有一句：

> 一条实测意见挂在**收货凭证证明的那个实际款号、具体物件编号和实际持有区间**上，而不是订单最初下单的甲款，也不是售后/退款记录。

因此，订单下单甲款但某一分包实际签收乙款时，乙款体验刊在 `/p/YI`；另一件甲款仍留在买家手中，甲款首测和后续换件追记刊在 `/p/JIA`。

## 启动

```bash
npm start
# 或 PORT=3000 DATA_FILE=./data/store.json node server.js
```

要求 Node.js 20+，不需要 `npm install`。

- 首页（款号目录）：<http://127.0.0.1:3000/>
- 乙款公开页：<http://127.0.0.1:3000/p/YI>
- 乙款 JSON：<http://127.0.0.1:3000/api/products/YI/page>
- 接口目录：<http://127.0.0.1:3000/api>

所有写入都会原子保存到 `data/store.json`（可用环境变量 `DATA_FILE` 覆盖），重启后仍可按款号打开原测、凭证、追记和商家问答后续。

## 一条命令复做全部验收剧本

```bash
npm run e2e
```

该脚本会自己启动临时服务、调用真实 HTTP 接口、杀掉并重启服务，然后断言：

1. 买家确已收到乙、发文时仍持有乙且同意回答：乙体验可刊出，商家问题精确投到其账号，回答后产品页出现可定位后续。
2. 只把乙凭证更正为拒收：旧刊冻结并显示更正启事，未回问题作废，回答返回 `409`，款号仍为 `YI`，不会静默搬到甲款。
3. 正常收货但买家拒绝后续询问：原体验仍在产品页，问题状态为 `DECLINED`，商家只有“拒收/未答复”回条，没有“已答复”回条。
4. 同单两件、分批到货、只换走 `u_two_2`：`u_two_1` 仍持有，`u_two_2/YI` 已退回，`u_two_3/JIA` 为换件补交；甲款下有换件和左右调换追记，乙款评价展示其持有区间已结束。
5. 另买一件同款再发首测返回 `409 FIRST_REVIEW_ALREADY_EXISTS`，不会多出第二个人的意见。
6. 重启后，款号页仍能核对原测、凭证、追记和后续锚点。

## 主要接口

| 动作 | 方法与路径 |
| --- | --- |
| 建买家账号 | `POST /api/buyers` |
| 建订单并展开具体物件 | `POST /api/orders` |
| 到件/正常签收凭证 | `POST /api/deliveries` 或 `POST /api/receipts`（`type:"ARRIVAL"`） |
| 拒收凭证 | `POST /api/rejections` 或 `type:"REJECTION"` |
| 退件/换货收回凭证 | `POST /api/returns` 或 `type:"RETURN"` |
| 换件补交签收凭证 | `POST /api/exchange-deliveries` 或 `type:"EXCHANGE_DELIVERY"` |
| 发布首测 | `POST /api/reviews` |
| 追加有时间的换样/左右件记录 | `POST /api/reviews/:id/addenda` |
| 商家对唯一体验发一次询问 | `POST /api/reviews/:id/questions`（也支持 `/question`） |
| 买家查看账号收件箱 | `GET /api/account/questions`（带 Bearer token 或 `x-buyer-id`） |
| 买家打开问题 | `POST /api/questions/:id/open` |
| 买家答复 | `POST /api/questions/:id/answer`（`/reply` 也可） |
| 买家拒收询问 | `POST /api/questions/:id/decline`（`/refuse`、`/reject` 也可） |
| 商家查看回条 | `GET /api/merchant/questions` |
| 更正凭证 | `POST /api/receipts/:id/correct`，也支持 `PATCH /api/receipts/:id`、`POST /api/receipts/:id/reject` |
| 按款号读公开页数据 | `GET /api/products/:modelCode/page` |
| 按款号读 HTML 产品页 | `GET /p/:modelCode` |

本机为了便于 curl，买家接口既接受建号时返回的 `Authorization: Bearer <token>`，也接受 `x-buyer-id: byr_xxx`。商家接口默认本机商家为 `merchant_default`，开发 token 是 `merchant-dev-token`。

## 最小调用示例

```bash
BASE=http://127.0.0.1:3000

# 1. 同意接收补充询问的买家
curl -sS -X POST $BASE/api/buyers \
  -H 'content-type: application/json' \
  -d '{"id":"byr_demo","displayName":"乙买家","agreesToAnswer":true}'

# 2. 下单甲款一件（这里用一个具体物件 u_demo）
curl -sS -X POST $BASE/api/orders \
  -H 'content-type: application/json' \
  -d '{"id":"ord_demo","buyerId":"byr_demo","orderedModelCode":"JIA",
       "units":[{"id":"u_demo","orderedModelCode":"JIA"}]}'

# 3. 拆分件实际签收的是乙款
curl -sS -X POST $BASE/api/deliveries \
  -H 'content-type: application/json' \
  -d '{"id":"rcp_demo","orderId":"ord_demo","unitId":"u_demo",
       "modelCode":"YI","voucherNo":"VCH-DEMO-YI",
       "proof":{"confirmedSubstitute":true}}'

# 4. 发文时仍在持有区间，因此乙款实测可刊出
curl -sS -X POST $BASE/api/reviews \
  -H 'content-type: application/json' -H 'x-buyer-id: byr_demo' \
  -d '{"id":"rvw_demo","modelCode":"YI","receiptId":"rcp_demo",
       "rating":5,"title":"乙款体验","body":"确实收到并持有乙。",
       "agreesToAnswer":true}'

# 5. 商家针对这条 review 发唯一一次补充询问；服务端从 review 找到 byr_demo
curl -sS -X POST $BASE/api/reviews/rvw_demo/questions \
  -H 'content-type: application/json' \
  -d '{"id":"q_demo","body":"长时间佩戴感受如何？"}'

# 6. 买家从正常账号看到、打开并回答
curl -sS $BASE/api/account/questions -H 'x-buyer-id: byr_demo'
curl -sS -X POST $BASE/api/questions/q_demo/open -H 'x-buyer-id: byr_demo'
curl -sS -X POST $BASE/api/questions/q_demo/answer \
  -H 'content-type: application/json' -H 'x-buyer-id: byr_demo' \
  -d '{"body":"六小时略有压感。"}'

# 7. 下一位读者从乙款号打开原测和后续
curl -sS $BASE/api/products/YI/page
# 浏览器打开： http://127.0.0.1:3000/p/YI#followup-q_demo
```

## 凭证更正策略

把 `rcp_demo` 改成拒收：

```bash
curl -sS -X POST $BASE/api/receipts/rcp_demo/correct \
  -H 'content-type: application/json' \
  -d '{"correctTo":"REJECTION","reason":"凭证复做：实际为拒收"}'

# 等价：
# curl -X PATCH $BASE/api/receipts/rcp_demo \
#   -H 'content-type: application/json' -d '{"type":"REJECTION"}'
```

服务端会：

- 保留原凭证和新更正凭证，形成审计链；
- 将 `rvw_demo.modelCode` 原样保留为 `YI`，状态改为 `WITHHELD`，公开页移除正文但显示冻结启事；
- 将投给 `byr_demo` 的未回答问题改为 `VOIDED`，买家不能再制造“已答复”回条；
- 如果问题已经回答，公开产品页会随旧刊一起隐藏该问答，但审计记录保留；
- **不会**把评价或问题自动改挂到甲款/丙款，也**不会**转投给其他买家。

若之后确有另一张正常签收乙的凭证，应重新发起一篇有依据的首测；这不是系统静默搬移旧刊。

## 两件分批、只换一件的模型

订单中的每个展开件都有独立 `unitId`：

- `rcp_two_1`：`u_two_1` 签收 `JIA`，之后无退件 → 仍持有；
- `rcp_two_2`：`u_two_2` 实际签收 `YI`（确认替代品）；
- `rcp_two_return`：凭退件/换货收回凭证只收回 `u_two_2/YI`；
- `rcp_two_exchange`：换件补交新物件 `u_two_3/JIA`，`replacesUnitId=u_two_2`。

所以：

- `rvw_jia_first` 用 `rcp_two_1` 证明甲款首测；
- `rvw_yi_second` 用 `rcp_two_2` 证明乙款实测，页面同时显示它的持有区间在 `rcp_two_return` 结束；
- 新 `u_two_3/JIA` 的体验不新建第二份甲款首测，而是作为带 `receiptId` 和时间的甲款追记；
- 左右件调换可用 `kind:"SIDE_SWAP"`、`side:"left/right"` 追加时间记录。

退款金额字段 `refundAmountCents` 只记录售后事实，不参与 `VERIFIED` 判断；哪怕退款为 0、1 分或全额，实际试用资格只看有效签收/拒收/退件/补交凭证与时间区间。
