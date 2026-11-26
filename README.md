# 棋社网络对局裁判（五路小方盘）

零第三方依赖的 Node.js HTTP 服务：受理落子/停着、提子、禁着（自杀、立即劫复）、双停终局、死活签认、续弈、封盘裁判、暂挂、计分、联赛榜和逐手复盘。

## 先回答：双方都点了停着，能记入联赛吗？

**两手连续停着只表示进入终局处置，不自动产生联赛结果。** 还要满足以下之一：

1. 双方对同一停局盘提交的整串死子清单完全一致（空清单也可以），随后结算入榜；
2. 有分歧时开续弈场，实际补着后在新停局盘重新签认一致；
3. 或开封盘裁定场，在回避名单之外、值班且未过期的裁判中，有两份“同意移去”的生效意见。

意见分裂、期限内不足两份 remove 意见时，争议串按原样留盘；联络不上可以暂挂。只有 `POST /api/games/:id/settle` 成功后，才会按对局号写入唯一联赛结果。

## 运行

```bash
npm test      # 17 条规则 / 裁定 / HTTP / 持久化测试
npm run demo  # 完整端到端剧情，包含重启服务，约 2 秒
npm start     # 默认 http://127.0.0.1:4178，存档 data/club-state.json
```

环境变量：

- `PORT`：默认 `4178`
- `HOST`：默认 `127.0.0.1`
- `DATA_FILE`：默认 `data/club-state.json`

`npm run demo` 会自行在 `4317` 端口启动/关闭/重启服务，使用临时 JSON 存档，并运行 94 项断言。

## 核心规则实现

- 坐标为 `{x,y}`，五路盘均取 `1..5`。
- 上下左右四邻连通的同色棋子为一串；空邻点为气。
- 落子后先检查并提尽无气敌串，再检查提子后的己串；己串仍无气则 `SUICIDE_FORBIDDEN`。
- 若落子后的盘面等于对手最近一手之前的盘面，返回 `KO_RESTORATION_FORBIDDEN`。
- 黑先；每次受理后轮换行棋方。同一 `requestId` / `Idempotency-Key` 的重发请求返回首次结果，不会重复落子、重复停着或重复计分。
- 连续两手停着后状态变为 `scoring`。
- 练习赛可在创建时传入棋社合法 `setup`；正式局传 `setup` 会被 `FORMAL_NO_IMPORT` 拒绝，结算哈希只覆盖服务实际受理的着手。
- 计分使用面积法：
  1. 在**计分副本**中拿掉共同签认或裁定移去的整串；
  2. 对剩余空点做四邻连通区分；
  3. 一个空区的边界只接触一种颜色时，整区归该色；接触两色或无色则不计；
  4. 加盘上剩余己子数；
  5. 总分相同为 `draw`。
- 原始盘、每手 `boardAfter`、提子和裁判记录均保留；计分移除只发生在 `scorePosition` 的副本中。

## 两个 HTTP 客户端

`clients/club-client.mjs` 是浏览器/Node 通用风格的 fetch 客户端。端到端演示中分别实例化为黑方和白方：

```js
import { ClubClient } from './clients/club-client.mjs';

const black = new ClubClient('http://127.0.0.1:4178', { prefix: 'black' });
const white = new ClubClient('http://127.0.0.1:4178', { prefix: 'white' });

await black.createGame({ id: 'demo', mode: 'formal' });
await black.place('demo', 'black', 3, 3, 'move-1');
await white.pass('demo', 'white', 'pass-1');
```

所有变更请求都建议带 `requestId`，也可用 HTTP 头 `Idempotency-Key`。

## 五路起势盘核对

创建练习赛：

```bash
curl -s -X POST http://127.0.0.1:4178/api/games \
  -H 'content-type: application/json' \
  -H 'Idempotency-Key: create-capture' \
  -d '{
    "id":"capture-demo",
    "mode":"practice",
    "size":5,
    "setup":[
      {"x":1,"y":1,"color":"white"},
      {"x":3,"y":1,"color":"white"},
      {"x":1,"y":2,"color":"black"}
    ]
  }'
```

黑下 `(2,1)`：

```bash
curl -s -X POST http://127.0.0.1:4178/api/games/capture-demo/moves \
  -H 'content-type: application/json' \
  -H 'Idempotency-Key: black-2-1' \
  -d '{"color":"black","x":2,"y":1}'
```

响应中的 `move.captured` 只有：

```json
[{ "x": 1, "y": 1, "color": "white" }]
```

右侧 `(3,1)` 白串仍在盘上。

## 死活提交与分歧

棋手提交的是所点棋子，服务会自动展开为当前停局盘上的整串：

```bash
curl -s -X POST .../api/games/demo-dispute/markings \
  -H 'content-type: application/json' \
  -d '{"as":"black","dead":[{"x":1,"y":1}],"requestId":"b-mark"}'
```

`dead` 是该方认定为死的所有代表点；空数组表示不要求移去任何串。服务比较双方最新完整清单：

- 两边都列入：`agreedDeadKeys`
- 只有一边列入：`disputes`，另一方按“未列入即保留”记为 `alive_by_omission`
- 清单完全相同：`consensus: true`

查询分歧：

```bash
curl -s .../api/games/demo-dispute/disputes
```

## 续弈场

```bash
curl -s -X POST .../api/games/demo-continuation/continuation \
  -H 'content-type: application/json' \
  -d '{"note":"角串死活有分歧，补下解决"}'
```

之后正常调用 `/moves` 或 `/pass`。第一手真正改变停局盘面的落子会在响应中给出：

- `move.invalidatesOldOpinions: true`
- `move.invalidatedOpinionIds`: 被作废的旧签认
- `move.groupLineage`: 旧串 key 到新串 key 的合并/变化关系

旧意见只标记为 `superseded`，不删除。下一次双停后，只有绑定新盘面指纹的新提交才算现行意见；尚未重签时 `complete:false`、`disputes:[]`，不会把已消失/合并的旧分歧误当成当前分歧。

校验旧签认能否用于新盘/新串：

```bash
curl -s -X POST .../api/games/demo-merge/endorsements/verify \
  -H 'content-type: application/json' \
  -d '{
    "boardSignature":"5|wb..b/w.w../bb.../...../....w",
    "groupKey":"white:1,1|1,2",
    "receiptHash":"旧签认哈希"
  }'
```

不匹配时返回 `applies:false` 和 `BOARD_SIGNATURE_MISMATCH`、`GROUP_KEY_CHANGED_OR_MISSING`、`RECEIPT_NOT_BOUND_TO_THIS_GAME` 等原因。

## 封盘裁定场

开赛时公布的联赛名单在：

```bash
curl -s http://127.0.0.1:4178/api/league
```

默认示例裁判为张、李、王、赵、钱；赵对固定演示对局号 `demo-dispute` 有回避义务，王初始不值班。

为某局所有现行争议串建案，可给很短期限用于演练：

```bash
curl -s -X POST .../api/games/demo-dispute/adjudication \
  -H 'content-type: application/json' \
  -d '{"deadlineSeconds":86400,"panelSize":3}'
```

值班变动：

```bash
curl -s -X POST .../api/referees/ref-li/duty \
  -H 'content-type: application/json' \
  -d '{"onDuty":false,"reason":"临时离岗"}'

curl -s -X POST .../api/referees/ref-wang/duty \
  -H 'content-type: application/json' \
  -d '{"onDuty":true,"reason":"接班"}'
```

裁判意见：

```bash
curl -s -X POST .../api/cases/case-demo-dispute-1/votes \
  -H 'content-type: application/json' \
  -d '{
    "refereeId":"ref-zhang",
    "decision":"remove",
    "reason":"只有一个共享气点",
    "requestId":"vote-zhang-1"
  }'
```

规则：

- 回避裁判的意见以 `REFEREE_RECUSED` 拒绝并留审计；
- 提交时不值班返回 `REFEREE_OFF_DUTY`；
- 超过 `deadlineAt` 的意见保存为 `status:"late"`，但 `effective:false`；
- 投票时在岗且未回避的意见，不因投票后正常换班而失效；
- 值班/回避导致未投票席位缺失时，`syncCasePanel` 会从当前合格裁判中替补；
- 两份无回避、及时、同盘同串的 `remove` 意见即可把案件标为 `removed`，结果中写明 `effectiveVoteIds`；
- 期满仍无两份 remove，尤其 remove/keep 分裂时，案件标为 `left`，按原样留盘并在 `note`/`split` 中注明。

复核全部案件：

```bash
curl -s -X POST .../api/games/demo-dispute/adjudication/evaluate -d '{}'
```

## 暂挂与入榜

一方失联时：

```bash
curl -s -X POST .../api/games/demo-control/suspension \
  -H 'content-type: application/json' \
  -d '{"suspended":true,"reason":"黑方客户端失联","pendingWith":"black"}'
```

暂挂期间不能落子、提交或结算；联络恢复后用 `{"suspended":false}` 恢复。双方清单一致的常规局恢复后可正常结算入榜。

结算：

```bash
curl -s -X POST .../api/games/demo-dispute/settle \
  -H 'content-type: application/json' \
  -H 'Idempotency-Key: settle-demo-dispute' \
  -d '{}'
```

未满足条件时会返回：

- `AWAITING_PLAYER_OPINIONS`
- `DISPUTES_UNRESOLVED`
- `ADJUDICATION_PENDING`
- `GAME_SUSPENDED`

## 局谱、复盘和联赛结果

整份逐手局谱（每手都带 `board` 快照）：

```bash
curl -s .../api/games/demo-dispute/replay
```

逐手重放某一步：

```bash
curl -s '.../api/games/demo-dispute/replay?step=0'
curl -s '.../api/games/demo-dispute/replay?step=10'
```

SGF 导出：

```bash
curl -s .../api/games/demo-dispute/sgf
```

唯一联赛结果和积分榜：

```bash
curl -s .../api/league/results
curl -s .../api/league/standings
```

结果保存在 `state.results[gameId]`，因此同一对局号无论结算请求重发多少次、服务重启多少次，都只有一份结果。`JsonStore` 使用临时文件 + `rename` 原子写入；所有已受理着手、幂等响应、裁判意见和结果都会持久化。

## 端到端演示对应剧情

`npm run demo` 使用固定对局号完整验证：

| 对局号 | 剧情 |
|---|---|
| `demo-capture` | 题目给定起势盘；黑 `(2,1)` 只提 `(1,1)` 左白子，右白子保留 |
| `demo-control` | 同一角串尚未被提，双方点不同子但展开为同一整串并同意移去；暂挂/恢复后入榜，黑 `4:2` |
| `demo-merge` | 分开改收官手为白 `(2,2)`，旧串并入 `white:1,1|1,2|2,2|3,2` 且有 3 气；对照局签认校验失败，不能直接结算 |
| `demo-continuation` | 两串分歧开续弈；白 `(2,2)` 作废旧意见并显示 lineage；重新提交后只剩 `white:5,5` 真分歧，随后新签认一致并结算 |
| `demo-dispute` | 与对照局同盘，只把对方意见改为保留；重发停着、值班变动、回避、迟到旧意见、两份生效裁判意见、重启后重发，均不增加手数或联赛盘数 |
| `demo-split` | 两名当值裁判一 remove、一 keep，小组已投完，立即按意见分裂、原样留盘；结算为 `4:4` 和棋 |

演示结束时联赛有 4 盘正式结果；`demo-merge` 故意不入榜，用来说明旧结算不能照抄。

## 目录

```text
server.js                 HTTP 服务入口
src/go.mjs                棋盘、串/气、提子、劫、空区计分
src/game.mjs              对局状态、着手、标记、续弈、结算依据
src/adjudication.mjs      名单/回避/值班/期限/裁判意见与积分榜
src/app.mjs               零依赖 HTTP 路由和幂等层
src/store.mjs             JSON 原子持久化
clients/club-client.mjs   HTTP 客户端
demo/e2e.mjs              双客户端完整剧情 + 重启验证
test/                     规则、裁定、HTTP、持久化测试
```
