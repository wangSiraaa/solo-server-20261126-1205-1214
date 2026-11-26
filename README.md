# 小楼栋报修联办服务

一个零运行时依赖的 Node.js 服务：把“楼上滴水、楼下天花板返潮”等住户现象建成至多 6 张住户单，按虚构管段连通关系筛选至多 4 个候选源头、至多两处并存的源头组合，并依据各户合同约定的回访时刻判断能否发出结案通知。

所有管段、阀门、加压、施工和回访均为服务内模拟，不连接真实管道或硬件。

## 先回答业务问题

**不能因为两户普通回访都见干，就把两张单一起销掉。**

题述基准中可以一起结案，但必须同时满足：

1. 甲、乙都登记了实际维修的同源 **A**；
2. 乙在自己的约定时段取得本户干回访；
3. 乙的严格合同要求“排除/处置每一个仍相容源头”，因此还要有“隔开 A、只给 B 支路加模拟压力”的**干读数**，确定性排除间歇源 B。

只要撤下排除 B 的加压观察，即使乙的普通回访恰好是干的，B 仍相容，乙不能跟甲一起结案。若保留 B 的排除、来源已唯一为 A，但乙在约定时段尚未入户回访，乙仍欠一次合同回访。

## 运行与验证

要求 Node.js 20+，无需安装依赖。

```bash
npm test       # 20 项领域、HTTP、治理与幂等测试
npm run verify # 题述关键场景的命令行业务核验
npm start      # http://127.0.0.1:3000
```

服务首次启动会自动载入“基准 + 丙户临时处置”场景。数据默认写入 `data/service-state.json`；可用 `DATA_FILE=/path/to/state.json PORT=3100 npm start` 覆盖。

页面支持载入三个关键变体、提交报修单、查看相容性/下一步、签收、申请复查、定检查次序、模拟发队和拆案。

## 题述场景矩阵

| 场景 | 乙侧仍相容组合 | 甲 | 乙/丙 |
| --- | --- | --- | --- |
| `baseline`：B 加压干读数 + 两户约定时段干回访 + 修 A | `[A]` | 可结案 | 乙可结案 |
| `withoutBExclusion`：不录入或撤下 B 排除观察 | `[A]`、`[A,B]` | 可结案 | 乙被 `UNRESOLVED_SOURCE: B` 阻断 |
| `missingYiRevisit`：保留 B 排除，但乙在约定时段未回访 | `[A]`（来源唯一） | 可结案 | 乙被 `REVISIT_MISSING` 阻断 |
| 加办丙户 `temporary_dry_once` | 不要求源头排除 | — | 丙一次干回访后可签收；其合同/结果不能替乙结案 |
| 基准后直接撤下 B 证据 | 乙重新出现 B | 通知仍有效、不重发 | 原乙通知不能再签收 |

命令行可直接看结论：

```bash
npm run verify
```

也可通过接口重置：

```bash
curl -X POST http://127.0.0.1:3000/api/scenarios/seed \
  -H 'Content-Type: application/json' \
  -d '{"variant":"withoutBExclusion"}'

curl http://127.0.0.1:3000/api/tickets/T2/closure
```

## 源头与证据规则

拓扑在 `src/scenarios.js` 的 `leakTopology()` 中，可用 `POST /api/topology` 替换。核心概念：

- `sources`：候选源头，最多 4 个；
- `segments`：虚构管段，可标记公区/私域；
- `points`：检查点，检查点归属于一个管段；
- `edges`：输入的有向连通关系；
- 检查记录的 `isolatedSegmentIds` 表示本次临时隔开的管段；
- 湿读数要求当时隔离状态下组合中至少一个源头仍能到达该点；
- 受控干读数会排除“隔离后仍能到达该点”的源头组合；
- **普通回访干读数不参与排除间歇源**，所以带故障的 B 可以在普通回访中暂干；
- `pressureTest.guaranteed: true` 表达题述模拟加压规则：B 有故障则指定点必湿，B 完好则必干。

组合枚举允许空集、单源和双源，双源上限由 `MAX_INDEPENDENT_SOURCES = 2` 控制。

## 合同化结案

合同类型：

- `standard`：本户在约定回访窗口内有干读数，且实际维修的源头沿拓扑覆盖本户现象点；
- `strict_source_exclusion`：在标准条件外，还要求所有“仍相容且未实际维修”的源头为空；
- `temporary_dry_once`：临时处置后只要求一次本户干回访，不要求排除全部源头。

结案判断是**住户单级别**的。共案会让各户共享案件证据和一次联合上门，但回访结果逐户登记、逐户满足自己的合同；丙户的一次干回访条款不能拿去满足乙的严格条款。

## 共案、拆案与分户借用

### 共案

- `POST /api/cases`：把已获入户许可的至少两户纳入共案；
- `POST /api/cases/:id/plan`：发队前决定检查次序；
- `POST /api/cases/:id/dispatch`：无检查次序不能发队；
- `POST /api/cases/:id/revisit`：必须给当前在案的每一户一个结果；相同 `jointRevisitId` 重发为幂等返回；
- `POST /api/cases/:id/split`：不同源时拆案。回访、通知、签收均挂在住户单上，不随案件关系删除。

### 分户委托与公区结论借用

- 各户可通过 `POST /api/tickets/:id/closure-notice` 和 `/sign` 先结自己的问题；
- `POST /api/evidence/borrow` 只允许转借 `scope: "public"`、`confirmed: true`、`transferable: true` 且与邻单相关的公区结论；
- `scope: "private"` 的私人室内观察一律返回 `PRIVATE_EVIDENCE_NOT_TRANSFERABLE`，借用记录只保存公开结论，不复制私人读数。

## 管家制度

本实现采用 **“许可重叠优先共案，公区结论可借则不重复派队”**：

1. 两户以上均已许可入户、许可时段重叠，并且位置相邻或候选源头/管段相关时，建议合并一趟检查，并按现象点和支路预排检查次序；
2. 时段不重叠或只需确认同一公共支路时，若邻单已有确认且可转用的公区结论，建议借用；
3. 私人室内观察不进入借用候选；
4. 否则走分户委托/单独派检。

可通过 `GET /api/policy/recommendation` 查看当前建议，规则在 `src/policy.js`。

## 下一步、签收、复查与通知幂等

- 住户：`GET /api/tickets/:id/next-step?role=resident`
- 维修员/管家：`GET /api/tickets/:id/next-step?role=maintainer` 或 `GET /api/next-steps?role=maintainer`
- 住户签收：`POST /api/tickets/:id/sign`
- 提出复查：`POST /api/tickets/:id/recheck-requests`
- 登记回访：`POST /api/revisits`
- 结案扫描：`POST /api/closures/run`

通知使用自然去重键：

```text
closure_notice:<ticketId>:cycle:<closureCycle>
```

同一结案周期内，重复回访、重复扫描、重复点击或隔天从 JSON 文件重启，都返回/保留同一条通知。住户提出复查后进入新周期，新的干回访依据才会产生新通知；重复签收返回同一签收记录。

## 主要目录

```text
src/topology.js     # 连通关系、组合枚举、证据相容性
src/service.js      # 住户单、共案、维修、回访、结案、签收、幂等
src/policy.js       # 管家合并/借用建议
src/scenarios.js    # 题述基准与变体
src/server.js       # 零依赖 HTTP API + 静态页面
public/             # 可操作的网页工作台
scripts/verify-scenario.mjs
test/               # 领域、API、政策与幂等测试
```
