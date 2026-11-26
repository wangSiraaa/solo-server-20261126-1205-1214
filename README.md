# 青年活动中心联选后台

本地可运行的阅览室、球场值月代表联选服务。本实现只采用题目中的一种办事办法：

> **事先联选 + 候选人书面承诺 + 同轮原子结算并发出相容就任书。**

因此系统里没有“邀请后等待接受/拒绝/过期”的另一套流程；候选人通过 `commitment` 提前写明愿任席位、偏好席位或拒绝某席。真实就任书生成时，两张票才标记为已消耗。仅有足额支持而未生成就任书时，值班表仍显示空席。

## 启动

```bash
npm start
# 默认 http://127.0.0.1:3000
```

可选环境变量：

```bash
PORT=3000 HOST=0.0.0.0 DATA_FILE=./data/election-db.json npm start
```

运行全部测试：

```bash
npm test
```

运行端到端演示（自动起临时服务、执行四个指定案卷、并发发证、重启并凭回执查回）：

```bash
npm run demo
```

前端后台地址：<http://127.0.0.1:3000/>。页面可一键运行各案卷、查看空席/就任书、每张票的转移轨迹以及并发结果。

## 公开一致的计票/取票规则

规则版本：`joint-v1`，每席定额 `2` 票。

1. 票按交票顺序获得递增 `seq`；取票顺序为 `seq` 升序，再按 `ballotId`。
2. 每张未消耗票从第一项向后扫描，跳过：
   - 无该席资格；
   - 未承诺愿任或已拒绝/承诺失效；
   - 该人已凭真实就任书担任另一席；
   - 该席已由两张更早的票取满。
3. 第一个当前仍开放、人有资格且愿任的“人＋席”成为有效支持。
4. 同一组合超过两票时，只消耗最早的两张；其余票不消耗，并在看到定额已满后继续扫描后项。
5. 同一人在两个席都达额时，优先兑现其书面偏好席；另一席的该人项因“此人已就任”被跳过，票继续转后项；没有后项则保持未用，席位继续空着。
6. 同人冲突之外若仍需决定先处理哪个达额组合，按第二张定额票的流水号、票号序列、席位顺序和人员 ID 排序，保证结果可复现。

`GET /api/rules` 也会返回这份规则。

## 四个指定案卷的结果

| 案卷 | 实际结果 | 用票/去向 |
| --- | --- | --- |
| 原四票：`b1,b2=甲/阅览`；`b3,b4=甲/球场 > 乙/球场`；甲只承诺阅览，乙愿任球场 | 甲任阅览，乙任球场 | 阅览消耗 `b1,b2`；`b3,b4` 跳过甲/球场（未愿任）后由乙/球场消耗 |
| 仅把 `b3,b4` 次项改为无球场资格的丙 | 甲仍任阅览；球场空席 | `b1,b2` 用于甲；`b3,b4` 依次跳过甲/球场、丙/球场资格不符，保持未用；不产生乙的就任书 |
| 原四票，但乙拒绝球场 | 甲任阅览；球场空席 | `b3,b4` 跳过甲/球场后再跳过乙/球场（本人拒绝），保持未用；查不到乙的值班证 |
| 甲愿任两席且两处达额，偏好阅览 | 只向甲发阅览就任书；球场等待/空席 | `b1,b2` 消耗；`b3,b4` 因甲已任阅览而不能用于甲/球场，无后项时保持未用 |

另含 `overquota` 案卷：三票支持同一阅览组合时，只取最早 `b1,b2`，`b3` 不被消耗并继续后项。

## 主要 HTTP API

所有写接口接收 JSON。建议写请求带 `Idempotency-Key`；同一键重放会返回首次的状态码和响应体，并带响应头 `Idempotent-Replayed: true`。

- `GET /api/health`
- `GET /api/rules`
- `POST /api/rounds`
- `GET /api/rounds`
- `GET /api/rounds/:roundId`
- `POST /api/rounds/:roundId/candidates`
- `PATCH /api/rounds/:roundId/candidates/:candidateId/commitment`
- `POST /api/rounds/:roundId/ballots`
- `GET /api/rounds/:roundId/audit`：每张票的状态、最终落点和逐项跳过/用票轨迹
- `GET /api/rounds/:roundId/duty-roster`：柜员值班表；未发证席位明确为 `vacant`
- `POST /api/rounds/:roundId/settle`：按承诺和规则原子生成相容就任书
- `GET /api/rounds/:roundId/appointments`
- `POST /api/rounds/:roundId/appointments`：柜员对同一席发证的持久化入口，同样强制定额、资格、承诺、一人一席和取票规则
- `GET /api/appointments/:appointmentId`
- `GET /api/receipts/:receiptCode`
- `POST /api/receipts/:receiptCode/resend`
- `POST /api/scenarios/seed`：载入内置案卷

### 一键载入并结算案卷

```bash
curl -sS -X POST http://127.0.0.1:3000/api/scenarios/seed \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"transfer","options":{"roundId":"case-1"}}'

curl -sS -X POST http://127.0.0.1:3000/api/rounds/case-1/settle
curl -sS http://127.0.0.1:3000/api/rounds/case-1/audit
curl -sS http://127.0.0.1:3000/api/rounds/case-1/duty-roster
```

可选场景名：`transfer`、`ineligible-backup`、`refused-backup`、`both-willing`、`overquota`。

## 并发、重启与重发

- 状态持久化到 `DATA_FILE`，写入方式为同目录临时文件 + `rename` 原子替换。
- 每个 Node 服务进程中的“校验—取票—写入—记录幂等键”是一个同步事务，两个并发请求不会在中间交错。
- 两个柜员同时发同一席时：
  - 一个请求 `201`，生成唯一就任书并消耗规则选出的两张票；
  - 另一个请求得到 `409 SEAT_ALREADY_FILLED`，`details.existingAppointment` 带回同一张就任书。
- 服务重启后，`GET /api/receipts/:receiptCode` 仍可查回：
  - 哪一个 `appointmentId`；
  - 哪两张 `ballotId`；
  - 每张票经过哪些项后才被消耗。
- 用原来的 `Idempotency-Key` 和同一请求体重发，不会产生第二张就任书或第二次用票。

自动化验证见 `test/server.test.js`，其中包含真实 HTTP 并发、关闭服务、用同一数据文件重启、幂等重放和回执查询。
