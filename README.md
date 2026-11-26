# 合租房带看与分佣服务

一个零第三方依赖的 Node.js 服务，接通：

预约挂牌 → 同行委托校验 → 现场回条 → 双方确认实际进入的房间 → 首带保护/跨店接力 → 议价确认 → 按实际房间签订租约 → 门店结算与模拟账户入账 → 重启后按原单号查回证据。

## 先回答开场问题

经纪人只真正进入了东屋，即使后来签下整套（东屋 + 西屋），也只能提取**东屋那一笔 4 点带看佣金**。西屋没有实际进入/确认，不能提；只在门口看过共用客厅也不计作独立房间带看。

系统中的对应演示单是 `SO-0-EAST-ONLY`，结果为：

```text
R-1701-E  VIEWING  S-A  4 点
R-1701-W  无合格带看，不生成甲店带看佣金
```

## 运行与验证

```bash
npm test          # 领域规则、HTTP、真实杀进程重启测试
npm run demo      # 重建 data/platform.json，跑完全部规定情形并模拟重启读回
npm start         # 默认 http://0.0.0.0:3000，数据文件 data/platform.json
```

可选环境变量：

- `PORT`：HTTP 端口，默认 `3000`
- `DATA_FILE`：JSON 持久化文件
- `RESET_DATA=1`：启动时删除旧数据并重新播种

本项目只使用 Node 20 内置模块，不需要 `npm install`。

## 核心建模

### 1. 物理房间和挂牌是两层身份

- `Room` / 空间使用稳定 ID，例如 `R-1701-E`、`R-1701-W`、`R-1701-L`。
- `Listing` 是挂牌，通过 `roomIds` / `commonAreaIds` 指向物理空间。
- 整套挂牌 `L-1701-WHOLE` 指向东、西屋；`L-1701-E`、`L-1701-W` 是同一物理房间的另挂广告。
- `PATCH /rooms/:id` 只能改名，ID 不变，也不会自动生成新挂牌。
- 三条重叠挂牌 `L-1808-WHOLE`、`L-1808-E-SOLO`、`L-1808-RENAMED` 都指向同一个 `R-1808-M`；“阳光主卧”只是别名。

### 2. 什么才算一次可计佣带看

必须形成完整证据链：

1. `POST /appointments`：明确找房单、挂牌、门店、经纪、预约空间。
2. 实际到场人：
   - 本单租客；或
   - 持有本找房单有效 `COMPANION` 同行委托的同住人。
3. 经纪提交现场回条，区分：
   - `enteredRoomIds`：真正进入的独立房间；
   - `doorwayOnlyRoomIds`：只从门口看过，例如共用客厅；
   - `notSeenRoomIds`：未看房间。
4. `POST /visits/:id/confirm`：由租客或实际受托同住人确认。

只有 `status=CONFIRMED` 的 `ENTERED` 且 `commissionable=true` 的独立房间会生成 `VIEWING` 贡献。共用客厅即使记录为门口可见，也不产生按间带看佣金。

### 3. 两种所选分佣制度

创建找房单时用 `regime` 选择：

#### `FIRST_PROTECTION`：保护首带制

- 门店先就 `(找房单, 物理房间)` 申请保护和承诺时段。
- 同一找房单、同一物理房间只允许存在一个 `PENDING/FULFILLED` 保护；换一条重叠挂牌再申请也返回 `PROTECTION_CONFLICT`。
- 现场回条确认实际到达时间落在承诺时段内，保护才变成 `FULFILLED`。
- 签约时该房间的带看池 100% 分给有效首带保护门店。
- 保护错过承诺时段会变成 `MISSED`，之后其他门店可申请。
- 没有有效保护时，回退给最早确认进入该房间的门店。
- 只完成签约的乙店不拿 `VIEWING` 点数。

#### `RELAY_CONTRIBUTION`：跨店接力制

- `POST /handoffs` 必须由租客和承接门店双方确认。
- 交接项精确到 `{roomId, workType}`，可引用既有带看/议价来源，并授权承接店后续承担 `VIEWING` 或 `NEGOTIATION`。
- 默认重叠分账约定：每个签约房间的同一佣金池，按**不同贡献门店数平均分配**；整数点数用最大余数法，保证分完且总额不变。
- 可另签 `SplitAgreement`，用基点 `bps` 自定义比例，但比例合计必须为 `10000`，且每个分账门店必须有真实贡献或经确认的接办/交接授权。
- 没参与、也没有授权接办来源的门店无法进入分账，查询他店单据或提现会返回 403。

### 4. 重叠挂牌使用哪一份钱

不同挂牌指向同一房间时：

- 保护权按物理房间判冲突，不按挂牌重复计算。
- 同一房间、同一佣金类型只形成一个佣金池，不允许每条挂牌各发一份。
- 带看池默认取最早合格贡献对应预约时快照的 `viewingPointsByRoom[roomId]`，避免挂牌事后改点。
- 租约只对 `signedRoomIds` 中的实际签约房间生成 `CommissionCase` 和 `CommissionItem`。
- 同一找房单的同一房间已有有效租约时，不能再签第二份租约重复生成佣金。

### 5. 提现幂等和本地模拟账户

- 门店通过 `POST /settlements` 选择本门店的一条或多条 `CommissionItem` 提现。
- 支持 `Idempotency-Key` 请求头或 body.`requestId`。
- 相同幂等键和相同内容重放：返回原结算单，`replayed: true`，不再入账。
- 新请求试图包含已 `WITHDRAWN` 的明细：返回 `409 COMMISSION_ITEM_ALREADY_WITHDRAWN`。
- 结算在本地模拟账户中做复式记录：平台清算账户减点，门店账户加点，并写两条 `LedgerEntry`。

## 规定情形与结果

播种的 1701 单元中，东、西屋的带看佣金均为每间 4 点；共用客厅不计佣。乙店 `S-B` 只签约。

| 单号 | 实际带看 | 最终租约 | 甲店可得 |
|---|---|---|---:|
| `SO-0-EAST-ONLY` | 仅进入东屋；西屋未看；客厅仅门口 | 东+西整套 | 东屋 4 |
| `SO-1-BOTH-VIEW-BOTH-SIGN` | 东、西均进入并确认 | 东+西整套 | 4 + 4 = **8** |
| `SO-2-WEST-DOORWAY-ONLY` | 仅东屋进入；西屋未看；客厅仅门口 | 东+西整套 | 仅东屋 **4** |
| `SO-3-FULL-VIEW-WEST-LEASE` | 东、西均进入并确认 | 仅西屋 | 仅西屋 **4** |
| `SO-4-AUTHORIZED-PROXY` | 受托同住人代看东、西 | 东+西 | **8** |

`SO-1-BOTH-VIEW-BOTH-SIGN` 的 8 点已在演示中真实提现到甲店模拟账户，结算单号 `STL-SO-1-A-8`。

## 三条重叠挂牌与两位经纪

1808 单元的三条挂牌都与 `R-1808-M` 重叠：

1. `L-1808-WHOLE`：东屋 M + 西屋 S 整套；
2. `L-1808-E-SOLO`：东屋 M 单间；
3. `L-1808-RENAMED`：改名“阳光主卧”的广告，仍指向同一个 M。

同一组事实：

- 甲经纪用 `L-1808-E-SOLO` 首看 M，并持有有效首带保护；
- 租客与丙店确认交接 M/S 的带看和议价；
- 丙经纪用 `L-1808-RENAMED` 复看 M，再用 `L-1808-WHOLE` 首看 S、完成议价；
- 乙店只签 M+S。

结果：

| 制度 | M 带看 4 点归属 | S 带看 4 点归属 | 总带看结果 |
|---|---|---|---|
| `FIRST_PROTECTION`（`SO-5-OVERLAP-FIRST`） | 甲店 4 | 丙店 4 | 甲 4、丙 4、乙 0 |
| `RELAY_CONTRIBUTION`（`SO-6-OVERLAP-RELAY`） | 甲 2 / 丙 2 | 丙 4 | 甲 2、丙 6、乙 0 |

1808 还单独列了每间 2 点的议价服务点；因此演示汇总中的丙店总点数会再增加 M/S 议价各 2。核心 1701 规定情形没有配置议价点，所以 8/4/4 不受影响。

丙店用第三条挂牌为同一 M 申请保护时，会得到 `PROTECTION_CONFLICT`。接力单中甲店的 2 点已通过 `STL-SO-6-OVERLAP-RELAY-A` 提现；重复请求只能重放或被 409 拦截。

## 重启后查单

数据在每次业务写入后原子写入 `DATA_FILE`（默认 `data/platform.json`）。真实进程重启测试会启动服务、写入演示、杀死进程、用同一数据文件重启，再验证：

- 租客能查回预约、到场确认、租约、结算；
- 门店店员凭 `CLERK-A` 或 `S-A` 能查回本店佣金和模拟账户；
- 原幂等键仍只能返回同一笔结算；
- 新请求不能再次支付已提现明细。

常用查询：

```bash
# 租客凭找房单号
curl http://127.0.0.1:3000/tenants/T-1001/trace/SO-1-BOTH-VIEW-BOTH-SIGN

# 甲店店员/门店凭找房单号
curl http://127.0.0.1:3000/stores/S-A/trace/SO-1-BOTH-VIEW-BOTH-SIGN

# 也可用预约号、回条号、租约号、结算号等任意原单号反查
curl 'http://127.0.0.1:3000/trace/LSE-SO-3-FULL-VIEW-WEST-LEASE?as=store&actorId=S-A'
curl 'http://127.0.0.1:3000/trace/SO-1-BOTH-VIEW-BOTH-SIGN?as=tenant&actorId=T-1001'

# 结算与账户
curl http://127.0.0.1:3000/settlements/STL-SO-1-A-8
curl http://127.0.0.1:3000/accounts/ACCT-S-A
```

## 主要 HTTP 接口

```text
GET  /health
GET  /rooms
POST /rooms
PATCH /rooms/:id
GET  /listings
POST /listings

POST /search-orders
GET  /search-orders/:id
POST /search-orders/:id/companion-mandates
POST /search-orders/:id/authorizations

POST /appointments
POST /protections
POST /visits
POST /visits/:id/confirm

POST /negotiations
POST /negotiations/:id/confirm
POST /handoffs
POST /handoffs/:id/tenant-confirm
POST /handoffs/:id/store-accept
POST /split-agreements

POST /leases
GET  /leases/:id

POST /settlements
GET  /settlements/:id
POST /commission-items/:id/withdraw
GET  /commission-items?storeId=S-A&status=PAYABLE

GET  /tenants/:tenantId/trace/:reference
GET  /stores/:storeId/trace/:reference
GET  /trace/:reference?as=tenant|store&actorId=...
POST /demo/run
```

## 代码结构

```text
src/json-store.js   原子 JSON 持久化
src/seed.js         虚构房间、挂牌、门店、经纪、模拟账户
src/service.js      全部业务规则、分佣引擎、幂等结算、单据追踪
src/app.js          零依赖 Node HTTP 路由
src/server.js       进程入口
src/scenarios.js    题目规定情形和重叠挂牌演示
scripts/demo.js     命令行演示 + 文件重开验证
test/               领域、HTTP、真实重启测试
```
