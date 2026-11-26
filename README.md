# 夜校报名表服务

一个零外部依赖的 Node.js 服务：版本化表单、条件小节、草稿续填、同伴邀请确认、相邻可结伴台位、柜台查询，以及重启后仍有效的幂等报名。

## 选择的办学办法

本项目只实现题目中的第一种：**“领到哪张表，就按哪张表办”**。

- 草稿创建时固定当时的已发布表单版本（当前初始为 v1）。
- 管理员发布把“来过实作室”拆成两题的 v2 后，旧草稿仍按 v1 解释旧题、条件和课程条款，直到草稿到期。
- 新草稿才使用 v2。
- 表单版本固定**不**等于台位预留：台位始终按递交瞬间的真实占用情况原子分配；已经被散客或其他单占走的台位不会因为旧表承诺而回来。
- 本项目不实现第二种“草稿追新表/同意沿用矩阵”的迁移办法。

### “上午填的两人组，晚上还算一起报过名吗？”

分三种情况：

1. **上午已经成功递交**：算。成功单、两个入班号和两个实际席位已持久化；晚上、发布新表后或服务重启后重复递交，都只返回原单和原来的两个席位。
2. **只是未交草稿，但两人信息和同伴确认仍匹配原目标**：还不算“已报名/已占台”，但晚上可凭草稿继续按上午的 v1 表填写、确认和递交。
3. **换过班级/场次、报名方式或同伴身份**：目标修订号会增加，旧邀请和旧回执立即失效，必须由同伴用新邀请重新确认；若台位已被别人先占，也不能保证原双台。

## 运行

要求 Node.js 20+，无需 `npm install`。

```bash
npm start
# 默认 http://localhost:3000
# 可设 PORT=4000 DATA_FILE=/absolute/path/state.json
```

打开 <http://localhost:3000/> 即网络入口。页面提供：

- 创建三台班并声明只有 `S1/S2` 可结伴；
- 领取/打开草稿、条件“结伴实作”、保存和续填；
- 生成同伴邀请、填写令牌并确认指定草稿；
- 故意把回执指向另一张草稿；
- 单人占 `S1` 后再试双人单；
- 管理员发布拆分版新表；
- 一键执行“旧草稿续填”网络演示；
- 柜台按订单号或 `ENR-...` 入班号查询；
- 一键跑核心验收场景。

## 测试与网络演示

```bash
npm test
```

包含领域规则和真实 HTTP 入口测试：

- 完整双人单分配 `S1/S2`；
- 同伴回执确认另一张草稿时拒绝且不占台；
- 散客先占 `S1` 后，即使 `S3` 空也不能用未公布为可结伴的 `S2/S3`；
- 隐藏后再次显示的旧条件答案不能充当新答复；
- 发布拆分新表后，旧草稿仍固定 v1；
- 更换同伴目标会作废旧邀请；
- 持久化文件上的“重启服务”和重复递交只返回原两个席位。

```bash
npm run demo
```

该脚本会启动真实 HTTP 子进程、跑完题目中的三个台位场景和旧稿续填，然后杀掉进程、用同一数据文件重启，再重复递交同一成功报名。

## 关键办案决定

### 1. 哪些台可以结伴

班级/场次显式提供：

```json
{
  "stationCount": 3,
  "pairGroups": [["S1", "S2"]]
}
```

服务**不会**仅凭数字相邻就自动允许 `S2/S3` 结伴。只有 `pairGroups`（也接受 `pairablePairs`、`pairEdges`、`sessions` 别名等）中公布的组才能出双人单。因此 `S1` 被散客领走后，即使 `S3` 还空、`S2/S3` 物理上相邻，也不能凑成双人单。

数字 `1/2`、默认台号 `S1/S2`、中文“一/二”均可识别。

### 2. 同伴确认何时变成不可再分配席位

同伴确认只生成绑定以下信息的回执：

- `invitationId` 与邀请令牌；
- 指定 `draftId`；
- 班级/场次；
- 当前 `targetRevision`；
- 同伴明确同意。

确认时**不预占台位**。只有调用递交且在同一个序列化事务中满足：

1. 草稿未到期、未成功递交过；
2. 当前表单版本下所有当前可见必填项有效；
3. 条件小节答案属于当前显示世代，隐藏旧答案未被充数；
4. 双人单有确认本草稿、本目标修订且邀请未作废的回执；
5. 所选可结伴组的两台在递交瞬间都空；

才会创建订单、写入两条占用记录和两个入班号。订单状态为 `confirmed_seats_fixed`，此后席位不可再分配。

### 3. 换过报名对象后旧邀请怎么办

以下变更会提高草稿的 `targetRevision`：

- 换班级/场次；
- `attendanceMode` 从双人改单人或反向修改；
- 改同伴姓名或联系方式。

旧邀请会标记为 `superseded`；旧回执保留用于审计，但递交时会得到 `COMPANION_CONFIRMATION_OUTDATED` 或 `COMPANION_INVITATION_SUPERSEDED`，必须重新邀请、重新取得同伴同意。

如果回执直接确认另一张草稿，则回执状态为 `target_mismatch`；即使把该回执 ID 关联回本单，递交也会以 `COMPANION_RECEIPT_FOR_OTHER_DRAFT` 拒绝，不写占用。

### 4. 隐藏旧答案为什么不能充数

每个条件小节有“显示世代” `sectionEpochs[sectionKey]`。当前答触发题发生实际变化时，相关小节进入新世代；答案写入时记录当时世代。

小节隐藏期间保留原始值仅供回看，但它不在 `effectiveAnswers` 中。即使小节后来再次显示，旧答案的世代也不匹配，必须由客人重新填写/明确保存，才能成为当前可见小节的新答复。柜台和订单只保存/展示 `effectiveAnswers`。

### 5. 重启与幂等

状态通过原子重命名写入 `DATA_FILE`（默认 `data/night-school.json`）。

成功后有两层防重：

- 草稿变为 `submitted` 并记录 `submittedOrderId`；任何后续递交都返回原订单；
- 若提供 `Idempotency-Key` 请求头或 body 字段，还会持久化幂等键与请求哈希。

因此重启后重复 POST 已成功草稿或同一幂等键，都不会新增订单、入班号或席位。

## 主要 HTTP API

所有响应使用 JSON；错误形如：

```json
{ "ok": false, "error": { "code": "PAIR_STATIONS_UNAVAILABLE", "message": "..." } }
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查与办学策略 |
| POST | `/api/classes` | 建班/场次，给台号和 `pairGroups` |
| GET | `/api/classes` | 查所有班、空台、占用和可结伴状态 |
| GET | `/api/classes/:id` | 查一个班及实际占用者 |
| POST | `/api/drafts` | 领草稿，固定当前或指定表单版本 |
| GET | `/api/drafts/:id` | 取草稿、完整固定表单、生效/失效答案与校验错误 |
| PATCH/PUT | `/api/drafts/:id` | 退回修改、保存答案或关联回执 |
| POST | `/api/drafts/:id/invitations` | 生成同伴邀请令牌 |
| GET | `/api/invitations/:id?token=...` | 查看邀请 |
| POST | `/api/invitations/:id/confirm` | 同伴用令牌确认；body 可传另一个 `draftId` 模拟错回执 |
| POST | `/api/companion-receipts` | 同上的别名入口 |
| POST | `/api/drafts/:id/submit` | 校验、分台、入班并生成订单 |
| POST | `/api/orders` | body 传 `draftId` 的递交入口 |
| GET | `/api/orders/:id` | 查完整订单 |
| GET | `/api/counter/orders/:id` | 柜台视图：版本、生效 Q&A、入班号、就坐 |
| GET | `/api/counter/enrollments/:ENR-...` | 按任一人班号查单 |
| POST | `/api/admin/forms/publish-split` | 发布拆分“来过实作室”的 v2 |
| GET | `/api/forms` / `/api/forms/active` | 列版本或取当前表 |
| POST | `/api/admin/reset` | 清空本地演示数据（无鉴权，仅供本机演示） |

`/api/sessions...` 是 `/api/classes...` 的别名。

## 最小调用示例

```bash
# 1. 三台班，仅 S1/S2 可结伴
curl -sS -X POST http://localhost:3000/api/classes \
  -H 'content-type: application/json' \
  -d '{"id":"c1","stationCount":3,"pairGroups":[["S1","S2"]]}'

# 2. 领草稿并一次填完双人条件小节
curl -sS -X POST http://localhost:3000/api/drafts \
  -H 'content-type: application/json' \
  -d '{
    "id":"d1","classId":"c1",
    "answers":{
      "guestName":"阿夜","contact":"aye@example.test",
      "attendanceMode":"pair","generalConsent":true,
      "partnerName":"小校","partnerContact":"xiao@example.test",
      "pairWorkPlan":"接线与复核","pairConsent":true
    }
  }'

# 3. 生成邀请、同伴确认（把返回的 token 填入下一条）
curl -sS -X POST http://localhost:3000/api/drafts/d1/invitations -d '{}'
curl -sS -X POST http://localhost:3000/api/invitations/INV_ID/confirm \
  -H 'content-type: application/json' \
  -d '{"token":"INV_TOKEN","consent":true,"companionName":"小校"}'

# 4. 递交；可重复执行，也可加 -H 'Idempotency-Key: k1'
curl -sS -X POST http://localhost:3000/api/drafts/d1/submit \
  -H 'content-type: application/json' \
  -H 'Idempotency-Key: k1' -d '{}'

# 5. 柜台查询
curl -sS http://localhost:3000/api/counter/orders/ORDER_ID
```

错回执场景：建第二张草稿 `d2`，确认邀请时传 `"draftId":"d2"`，再 `PATCH /api/drafts/d1` 把返回的 `receipt.id` 设为 `companionReceiptId`；递交 `d1` 会被拒绝且不写占用。

## 目录

```text
server.js              # 根入口，等价于 npm start
public/index.html      # 无构建网络入口
src/form.js            # 表单版本、条件可见性、显示世代、生效答案与校验
src/store.js           # 串行事务 + JSON 原子持久化
src/service.js         # 草稿、邀请、回执、台位、订单、柜台领域服务
src/server.js          # 零依赖 Node HTTP 服务与静态入口
scripts/demo.mjs       # 真实 HTTP/重启/幂等验收演示
test/                  # 领域规则、HTTP、重启持久化测试
```
