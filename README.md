# 泥火夜校 · 补修办理服务

一个零外部依赖的 Node.js 本地服务，用符号动作（`open` 开孔、`raise_wall` 立壁、`close` 收口等）和泥件台账办理陶艺夜校补修。**不连接任何真实拉坯设备。**

## 班型选择

本方案按题意**仅选一项**开办：

- ✅ **保留原件辅导**（`retained_original`）：未完成泥件按号保留，分配归属学员、存放位、下一次开放台和继续台位；只有核对为同一件、未回收重揉的泥件，才能认定连续段。
- ❌ **短环重练班**（`short_loop_repractice`）：本服务不开办；传入该 `classType` 会返回 `CLASS_TYPE_NOT_OFFERED`。评估结果中的 `shortestTeachableSegment` 只用于命名“下一班要接着教的最小连续段”，不代表开办第二种班型。

## 启动与验题

```bash
npm test          # 运行领域规则、HTTP、幂等/并发测试
npm start         # 默认 http://127.0.0.1:3000
```

未设置 `PORT` 时，同一服务还会监听 <http://127.0.0.1:8000>、<http://127.0.0.1:8080>、<http://127.0.0.1:5000>、<http://127.0.0.1:4173> 和 <http://127.0.0.1:5173>，方便不同验题入口。页面提供“一键验题脚本”、规则勾选、学员切换、补修单办理、泥件回收和查章。

数据默认落在 `data/pottery.json`，原子写入；测试使用内存库。`POST /api/admin/reset` 可恢复初始验题种子。

## 开篇问题的答案

种子中，丁（`S-DING`）的练习档 **`PR-DING-FLOW`** 缺了中段“立壁”。他后来在 `LS-1118-REPLAY` 补看了立壁示范 `EV-DING-WALL-DEMO`（泥件 `DEMO-RAISE-09`，参与方式 `observed`，助教已确认）。

因为 `WORKFLOW_AUDIT`（拉坯流程见闻补全档）明确允许跨件、旁听补入，所以这次示范能把 **`PR-DING-FLOW` 的流程见闻档补完整**。

但它**不能**补发 `BASIC_CYLINDER` 的亲手拉坯结课章：正式动作组要求 `hands_on`，且 `L-WALL-CLOSE` 规定立壁→收口必须在同一未回收泥件上完成。

可随时查询：

```bash
curl -s http://127.0.0.1:3000/api/demo-matches
```

## 甲的标准判定

甲（`S-JIA`）在 `P-JIA-01` 上已由助教确认独立完成：

- `center` 定中心：可迁移有效；
- `open` 开孔；
- `raise_wall` 立壁。

他提前离场，缺少符合同件连续要求的亲手 `close` 收口。

系统会拒绝两种拼法：

1. `PR-JIA-CROSS-CLOSE`：他在另一件 `P-JIA-02` 上亲手收口。动作真实，但 `L-WALL-CLOSE.samePiece=true`，原因码 `piece_mismatch`。
2. `PR-JIA-OBS-CLOSE`：补看收口示范。原因码 `target_mode_observed_not_accepted`。

正确路径是保留 `P-JIA-01` 给甲，在下一次开放台 `OPEN-1126` 的分配台位真实到课，并在同一件 `P-JIA-01` 上亲手收口。导师确认后签发唯一印章 `STAMP-S-JIA-BASIC_CYLINDER`。

## 可完成的 API 补修流程

```bash
BASE=http://127.0.0.1:3000

# 1. 恢复种子（验题人想从头跑时）
curl -X POST "$BASE/api/admin/reset"

# 2. 发起甲的补修单；返回保留泥件、存放位、场次和台号
curl -X POST "$BASE/api/cases" \
  -H 'Content-Type: application/json' \
  -d '{"studentId":"S-JIA","groupCode":"BASIC_CYLINDER","linkId":"L-WALL-CLOSE"}'

# 3. 真实到课，台号默认使用补修单中的保留台位
curl -X POST "$BASE/api/cases/CASE-S-JIA__BASIC_CYLINDER__L-WALL-CLOSE/attendance" \
  -H 'Content-Type: application/json' \
  -d '{"confirmedBy":"MENTOR-LIN"}'

# 4. 同件亲手完成回执
curl -X POST "$BASE/api/cases/CASE-S-JIA__BASIC_CYLINDER__L-WALL-CLOSE/receipt" \
  -H 'Content-Type: application/json' \
  -d '{"action":"close","mode":"hands_on","pieceNo":"P-JIA-01","confirmedBy":"TA-ZHOU"}'

# 5. 导师确认并查章
curl -X POST "$BASE/api/cases/CASE-S-JIA__BASIC_CYLINDER__L-WALL-CLOSE/confirm" \
  -H 'Content-Type: application/json' \
  -d '{"mentorId":"MENTOR-LIN"}'

curl "$BASE/api/stamps?studentId=S-JIA"
```

导师办理结果中的 `signedDecision.statement` 会签明：

- 已学成哪个完整动作组、使用了哪些证据事件；
- 哪些原班独立完成的可迁移项继续有效；
- 若尚未实际补做，还剩哪个动作段、为何不能跨件/观摩替代、须来下一班亲手补做。

## 幂等与并发

- 同一 `studentId + groupCode + linkId` 缺口重复发起，返回原补修单。
- 同一补修单重复提交导师确认，返回同一印章和 `duplicate:true`。
- 两名导师同时首次办理同一缺口时，Node 单线程内以同步领域事务检查并落盘；印章 ID 固定为 `STAMP-<studentId>-<groupCode>`，因此只会有一份。
- `npm test` 中的 `Promise.all` 用例会实际并发提交两位导师的确认，并断言印章数为 1。

## 规则对照：动手历史不变，改一条同件规定

乙（`S-YI`）有固定动手历史：

- `EV-YI-HANDLE`：在 `P-YI-H1` 上亲手装把；
- `EV-YI-JOIN`：在另一件 `P-YI-H2` 上亲手接缝抹平。

初始规则 `L-HANDLE-JOIN.samePiece=false`，所以 `HANDLE_JOIN_CONTROL` 完整组成立。验题人可修改这一条（不影响甲的 `L-WALL-CLOSE` 同件要求）：

```bash
curl -X PATCH "$BASE/api/rules/links/L-HANDLE-JOIN" \
  -H 'Content-Type: application/json' \
  -d '{"samePiece":true,"changedBy":"VERIFIER"}'

curl "$BASE/api/students/S-YI/evaluation"
```

修改后历史指纹 `historyFingerprint` 不变，但同一对动作因 `piece_mismatch` 不再满足。此时导师只能签发“下一班实际补做”通知；若保留 `P-YI-H1`、到课并在该件上补做 `smooth_join`，再确认后才盖章。

页面上的“跑乙的规则变更对照”会自动执行上述过程。

## 验题人换学生 / 改记录 / 改规则

```bash
# 新增一名学生，也可直接使用 S-BING、S-DING、S-WU
curl -X POST "$BASE/api/students" -H 'Content-Type: application/json' \
  -d '{"name":"己"}'

# 新增接口会返回实际 id，通常从 S-1001 开始；下例请替换为返回值
curl -X POST "$BASE/api/records" -H 'Content-Type: application/json' \
  -d '{
    "studentId":"S-1001",
    "sessionId":"LS-1118",
    "pieceNo":"P-NEW-1",
    "confirmedBy":"TA-ZHOU",
    "events":[{"action":"open","seq":1,"at":"2026-11-18T19:10:00+08:00","mode":"hands_on"}]
  }'

# 只把某个已有事件的参与方式改成观摩，再重新评估/办理
curl -X PATCH "$BASE/api/events/EV-JIA-NEW-CLOSE" \
  -H 'Content-Type: application/json' -d '{"mode":"observed"}'

# 修改任意衔接规则；支持 samePiece、sameContinuousChain、supplementByAudit 等字段
curl -X PATCH "$BASE/api/rules/links/L-WALL-CLOSE" \
  -H 'Content-Type: application/json' -d '{"samePiece":true}'
```

## 主要接口

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/health` | 服务与所选班型 |
| `GET /api/students` / `POST /api/students` | 学员列表 / 换一名验题学生 |
| `GET /api/records?studentId=` / `POST /api/records` | 练习记录 |
| `PATCH /api/events/:eventId` | 修改事件参与方式后重新办理 |
| `GET /api/students/:id/evaluation` | 按现行规则评估完整组、缺口、原因码、可保留泥件 |
| `GET /api/demo-matches` | 补看示范可补完整的见闻练习 |
| `GET /api/rulebook` / `PATCH /api/rules/links/:linkId` | 查看/修改衔接规定 |
| `POST /api/cases` / `GET /api/cases/:id` | 发起/查询补修单 |
| `POST /api/cases/:id/attendance` | 真实到课与台位核对 |
| `POST /api/cases/:id/receipt` | 真实亲手完成回执；观摩、错件、回收件会被拒 |
| `POST /api/cases/:id/confirm` | 导师确认、签明结论、盖章或签发下班补做段 |
| `GET /api/stamps?studentId=` | 查章 |
| `POST /api/pieces/:no/recycle` | 把泥件标记为回收重揉 |
| `GET /api/pieces` / `GET /api/sessions` / `GET /api/tables` | 台账 |
| `POST /api/admin/reset` | 恢复初始种子 |

## 关键判定原因码

- `piece_mismatch`：动作真实，但违反必须同件。
- `target_mode_observed_not_accepted` / `anchor_mode_observed_not_accepted`：观摩不能满足亲手动作要求。
- `assisted_not_independent`：助教把手不算学员独立完成。
- `sequence_not_after_anchor`：时序不能形成前后衔接。
- `piece_recycled_before_continuation` / `anchor_piece_recycled`：泥件已回收重揉。
- `continuous_chain_mismatch`：要求同一连续练习/链但链号不一致。
- `no_qualifying_continuous_chain`：现有事件无法组成符合全部衔接条件的动作链。
