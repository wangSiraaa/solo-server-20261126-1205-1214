# 原型试用受理与计件复核台

本地零依赖服务：把委托人任务、事前计件约定、招募方派出、试用员身份、签发原型版本、逐步点击状态、提示曝光、审核、报酬和重启复查放在同一份 JSON 台账中。

## 先回答题目里的问题

**不能。** 走设计师直达捷径、只跳到外观相同的完成页，或重放主持人代点轨迹，都不能证明“这一轮是本人独立找到入口并完成动作”。

本系统只在以下条件同时成立时支付“自助发现奖”：

1. 会话属于该试用员的受理单，原型版本与受理时签发的版本一致；
2. 每个点击都是该画面/状态下的可点动作，并由服务端状态机真实改状态；
3. 规定动作按顺序命中精确的 `verb + targetId`，例如两次都是 `book-blue`；
4. 没有设计师捷径替代规定步骤，没有主持人代点/代点轨迹重放；
5. 该身份此前没有实际看过直接说破本题的提示，也没有从真实先行任务中掌握同一独立发现键；
6. 审核通过后按接单时冻结的单价结算，同一受理单/同一次完成/同一结算单绝不重复发钱。

最终画面的标题、外观键或同名“确定”**从来不是通过证据**。“确定”在公告页、真结束页和看起来相同的 lookalike 结束页有不同的 `actionId`。

## 运行入口

```bash
npm start
# 默认 http://127.0.0.1:4173
# 数据持久化到 data/local-db.json
```

| 入口 | 地址 | 用途 |
| --- | --- | --- |
| 总入口 | <http://127.0.0.1:4173/> | 四个本地入口和判定红线 |
| 受理/派出 | <http://127.0.0.1:4173/intake.html> | 委托人登记任务、检查任务包泄题、招募方真正派发邀约、生成领取回条 |
| 试用终端 | <http://127.0.0.1:4173/terminal.html> | 显示当前画面、可点动作、任务状态；拆封提示；提交和多设备重传 |
| 领报酬 | <http://127.0.0.1:4173/pay.html> | 查看事前单价、审核后的结算单并领取；重复请求不加工资 |
| 审核/重启复查 | <http://127.0.0.1:4173/review.html> | 轨迹检查、旧回条/提示来源、结算与发放台账、原始 JSON |
| 健康检查 | <http://127.0.0.1:4173/api/health> | 查看进程和实际数据文件路径 |

可通过环境变量修改监听和数据位置：

```bash
HOST=127.0.0.1 PORT=4173 DATA_FILE=/path/to/db.json npm start
```

## 一条命令走完题目要求的案卷

```bash
npm run verify
```

该命令会：

1. 启动一个使用临时 `DATA_FILE` 的真实 HTTP 服务；
2. 检查蓝册+红册可同包、蓝册+绿册共享收藏手势会泄题；
3. 用全新试用员独立完成“去书库 → 收藏蓝册 → 取消收藏蓝册”，审核 **3 点**并发放；
4. 并发用三台设备重传同一个 `completionId`，并并发重复领取，断言总工资仍为 **3**、发放流水只有一笔；
5. 另开一单，把第二个动作改绑绿册，再进入外观键相同的 `finish-lookalike`，断言审核失败且不能批准；
6. 录入主持人代点轨迹并把它重放进受理单，断言来源检查失败；
7. 让同一身份先有一单实际拆封查看 `hint-blue-answer` 并保存回条；
8. 再给该人派新蓝册单。受理服务按试用员身份读回旧回条，本次不展示任何新提示；即使恢复完整蓝册轨迹，行为通过但发现奖为 **0**；
9. 给同一人派发不被该提示泄题的红册待借任务，正常完成、审核并收到 **2 点**；
10. 杀掉服务、用同一数据文件重启，再从审核 API/页面复查，并在重启后重传旧完成和重复领取，断言仍不多发。

纯领域规则测试：

```bash
npm test
```

如果想直接在页面里查看一套固定案卷，可在 `npm start` 后另开终端执行：

```bash
npm run showcase
```

它会重置默认本地库并写入 `T-001-fresh`（三点已付）、`T-002-retarget`（错目标失败）、`T-003-hinted`（旧提示排除蓝册发现奖，但红册任务两点已付）。

## 无主持发包策略

本实现选择题目中的**无主持试用**。主持人代点接口仅作为“代点轨迹/带教来源”的审计与排除证据，不充当另一种完整试用模式。

每个任务声明：

- `requiredDiscoveryKeys`：本单要观察的首次发现；
- `teachesKeys`：做完本单后这个人会实际学会什么；
- `exclusionHintIds`：哪些提示一旦实际展示就直接排除发现奖；
- `allowedHintIds`：本单允许封存/拆封的提示。

发包前调用：

```http
POST /api/packages/plan
{ "taskIds": ["task-blue-roundtrip", "task-red-loan-flag"] }
```

系统比较前一项 `teachesKeys` 与后一项 `requiredDiscoveryKeys`：

- 蓝册收藏往返 + 红册待借标记：发现键不相交，可封入同一个无主持包；
- 蓝册收藏往返 + 绿册收藏往返：共享 `gesture:enter-library`、`gesture:favorite-toggle`，不能作为同一人的两个“首次发现”任务发包。应撤下后一项、另邀新人，或在接单前改按已声明的“已知手势复测单价”派发。

只有**实际发生**的行为会进入身份的已知内容：审核通过/行为通过的真实轨迹会生成 `knownKeys`，实际拆封提示会保存 `exposures`，主持人直接带教会保存 `demonstrations.taughtKeys`。仅登记提示、仅派发任务，不会算这个人已经知道。

## 持久化与重启复查

所有写操作经 `JsonStore.mutate()` 串行执行，并以“临时文件 + rename”原子写入 `data/local-db.json`。

重启后打开：

```text
http://127.0.0.1:4173/review.html
```

或直接请求：

```bash
curl http://127.0.0.1:4173/api/audit
curl http://127.0.0.1:4173/api/testers/T-hinted-xxxx/receipts
```

## 主要 HTTP 流程

```bash
# 招募方派出
curl -X POST http://127.0.0.1:4173/api/invitations/dispatch \
  -H 'content-type: application/json' \
  -d '{"testerId":"T-001","taskId":"task-blue-roundtrip","deviceId":"dev-A"}'

# 试用员接受事前三点约定，生成领取回条和签发原型会话
curl -X POST http://127.0.0.1:4173/api/invitations/<invitationId>/accept \
  -H 'content-type: application/json' \
  -d '{"testerId":"T-001","acceptTerms":true,"deviceId":"dev-A"}'

# 逐步点击；服务端返回当前画面、可点动作、任务状态
curl -X POST http://127.0.0.1:4173/api/sessions/<sessionId>/click \
  -H 'content-type: application/json' \
  -d '{"actionId":"go-library"}'

# 同一 completionId 可由多台设备反复 POST
curl -X POST http://127.0.0.1:4173/api/claims/<claimId>/completions \
  -H 'content-type: application/json' \
  -d '{"completionId":"cmp-fixed-uuid","deviceId":"dev-B"}'

# 招募方审核
curl -X POST http://127.0.0.1:4173/api/completions/<completionId>/review \
  -H 'content-type: application/json' \
  -d '{"decision":"approve","reviewerName":"recruiter"}'

# 试用员领报酬；重复调用只返回同一 payout
curl -X POST http://127.0.0.1:4173/api/claims/<claimId>/payout \
  -H 'content-type: application/json' \
  -d '{"deviceId":"dev-C"}'
```

## 目录

```text
server.mjs              零依赖 HTTP 服务、API、静态页面、幂等与持久化
lib/domain.mjs          原型状态机、任务/提示/约定、发包策略、资格和轨迹判定
lib/store.mjs           串行、原子 JSON 存储
public/                 本地控制台页面
scripts/verify.mjs      题目全案卷 + 并发重传 + 停服重启验证
test/domain.test.mjs    领域规则测试
data/local-db.json      默认运行后生成的本地台账
```
