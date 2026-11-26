# 彩皂订单后台：方格浇注、切片回放与样卡装盒

一个零外部依赖的 Node.js 20 服务，只模拟彩色方格、长条浇注、切片两端真实花纹、样卡方向匹配、整盒分配、并发领片/切取、余料和取货去向。

## 运行

```bash
npm test
npm start
# 默认 http://127.0.0.1:4310 ，数据原子写入 ./data/db.json
```

浏览器打开 <http://127.0.0.1:4310/> 是可操作的后台页面。

也可以指定数据文件和端口：

```bash
DATA_FILE=/tmp/soap-demo.json PORT=4310 npm start
```

一键本地请求示例：

```bash
bash examples/local-requests.sh
```

## 题目样条

系统重置后会生成 `BAR-DEMO`：

- 长 8 格，截面 5×5；标准片宽 3 格，切口宽 1 格。
- 第 0 层：蓝边黄心，红点在左上角。
- 第 3 层：切口格，即使藏有花也会被切掉。
- 第 6 层：蓝边黄心，红点在右上角。
- 标准切法得到：
  - `PIECE-0001`：保留第 0..2 格，向头真实面是第 0 格，正放即合左上红点样卡。
  - `PIECE-0002`：保留第 4..6 格，向尾真实面是第 6 格；条坯坐标中红点在右上，允许左右翻转摆放后合卡。
- 第 3、7 格是切口格，不属于任何成品。

样卡：

- `CARD-FLOWER`：只检查蓝边黄心，红点位置不检查，所以两片都能看出花。
- `CARD-REDL`：蓝边黄心且左上角单独红点，是带方向样卡。

## 核心方向约定

- 浇注矩阵按“从条头看截面”的左右顺序保存为 `barView`。
- 站在条尾直接看同一格时，`outsideView = hflip(barView)`，接口会同时给出。
- `orientationPolicy: "horizontal-ok"`：允许实物片调头/左右读序翻转，匹配 `identity` 或 `hflip`。
- `orientationPolicy: "bar-direction"`：只许照条坯头尾方向摆放，只匹配 `identity`。
- 匹配输入只能来自真实浇注层和实物片的头、尾端点格。系统不会扫描中间格来虚构一个切面，也不会把镜像结果冒充成新切出来的面；镜像只作为已有真实面的摆放变换记录。

## 关键场景验证

### 1. 带方向样卡允许翻转：找到两片并办一盒

```bash
curl -sS -X POST http://127.0.0.1:4310/api/admin/reset \
  -H 'content-type: application/json' -d '{}'

curl -sS -X POST http://127.0.0.1:4310/api/quotes \
  -H 'content-type: application/json' \
  -d '{
    "barCode":"BAR-DEMO",
    "cardId":"CARD-REDL",
    "piecesPerBox":2,
    "boxCount":1,
    "orientationPolicy":"horizontal-ok"
  }'
```

关键结果：

```text
physicalMatchCount = 2
PIECE-0001 / head / cell 0 / identity
PIECE-0002 / tail / cell 6 / hflip
maxBoxes = 1
```

办理一盒：

```bash
curl -sS -X POST http://127.0.0.1:4310/api/orders \
  -H 'content-type: application/json' \
  -d '{
    "barCode":"BAR-DEMO",
    "cardId":"CARD-REDL",
    "piecesPerBox":2,
    "boxCount":1,
    "orientationPolicy":"horizontal-ok",
    "clerk":"clerk-li",
    "customer":"手作教室",
    "destination":"教室自提柜 A1"
  }'
```

返回的 `BOX-0001` 中，每个槽位都保存：

- 实际片号、条坯号、真实层号；
- 朝外是向头面还是向尾面；
- 是否使用左右翻转；
- 样卡快照；
- 该片向头、向尾两面的完整方格；
- 客人和确切去向。

### 2. 只把第二处黄心改成蓝色：少找到一片

```bash
curl -sS -X PATCH http://127.0.0.1:4310/api/bars/BAR-DEMO/cells/6 \
  -H 'content-type: application/json' \
  -d '{"row":2,"col":2,"color":"B","reason":"仅第二处黄心改蓝"}'

curl -sS -X POST http://127.0.0.1:4310/api/quotes \
  -H 'content-type: application/json' \
  -d '{"barCode":"BAR-DEMO","cardId":"CARD-REDL","orientationPolicy":"horizontal-ok"}'
```

此时 `physicalMatchCount` 从 2 变成 1。样卡没有改，蓝心的第二处花不再匹配。

换回合模时保存的原条：

```bash
curl -sS -X POST http://127.0.0.1:4310/api/bars/BAR-DEMO/restore-original \
  -H 'content-type: application/json' -d '{}'
```

### 3. 只许按条坯头尾方向摆放：原来须翻转的一片不合

```bash
curl -sS -X POST http://127.0.0.1:4310/api/quotes \
  -H 'content-type: application/json' \
  -d '{
    "barCode":"BAR-DEMO",
    "cardId":"CARD-REDL",
    "orientationPolicy":"bar-direction"
  }'
```

`physicalMatchCount` 为 1，只剩第 0 格的 `PIECE-0001`；第 6 格红点在条坯坐标中位于右上，不能靠翻转或虚构切面匹配左上红点卡。

## 两种经营方式

题目允许二选一；本后台把两种接口都提供出来，默认演示“预先标准切片、柜员领片装盒”的店。

### A. 预先切成标准宽度

- `POST /api/bars/:code/precut`
- `GET /api/pieces`
- `POST /api/quotes`
- `POST /api/claims`
- `POST /api/orders`

多槽位订单可传 `template`，服务端用最大流把现有片分配到各盒槽位，片不会重复使用，并求最大完整盒数。相同槽位可以调换顺序。

### B. 签样后开切

- `POST /api/bars/:code/cut`
- `GET /api/bars/:code/remnants`

切取请求可指定：

```json
{ "from": "head", "width": 3, "kerfWidth": 1, "clerk": "A" }
```

服务端只允许从当前连续余料段切取；成品区间和切口格都会被标记。切口格变成 `kerf`，不会再出现在相邻成品或下一段余料中。下一张单只能使用 `intact` 的连续格段。

## 并发一致性

所有变更进入同一个串行事务队列，并在事务内重新读取状态：

- 两个柜员同时领同一片：一个返回 `201`，另一个返回 `409 PIECE_NOT_AVAILABLE`。
- 两个柜员同时切同一条：两个事务顺序提交，典型结果为 `[0,2] + kerf[3]` 和 `[4,6] + kerf[7]`，格不重叠。
- 每个事务失败会回滚内存快照；成功后原子写临时文件再 `rename`。

## 停业重启与盒号追踪

每次成功变更都会持久化到 `DATA_FILE`。停止后用同一文件重启：

```bash
DATA_FILE=./data/db.json PORT=4310 npm start
curl -sS http://127.0.0.1:4310/api/boxes/BOX-0001
```

即使服务重启，仍可按盒号查到原片号、两片真实端面、朝外选择、翻转记录、样卡快照和取货去向。

办理取货：

```bash
curl -sS -X POST http://127.0.0.1:4310/api/boxes/BOX-0001/pickup \
  -H 'content-type: application/json' \
  -d '{"receivedBy":"wang","destination":"上海展会柜台 B12"}'
```

## 常用接口

| 动作 | 方法与路径 |
| --- | --- |
| 重置题目演示 | `POST /api/admin/reset` |
| 开模/建条 | `POST /api/bars` |
| 逐层入样浇注 | `POST /api/bars/:code/pours` |
| 合模 | `POST /api/bars/:code/close` |
| 回放实际截面 | `GET /api/bars/:code/replay` |
| 修改实际浇注格 | `PATCH /api/bars/:code/cells/:index` |
| 恢复合模原条 | `POST /api/bars/:code/restore-original` |
| 查询余料 | `GET /api/bars/:code/remnants` |
| 签样后切一片 | `POST /api/bars/:code/cut` |
| 标准预切 | `POST /api/bars/:code/precut` |
| 样卡列表/新增/编辑 | `GET/POST /api/cards`，`PATCH /api/cards/:code` |
| 询单/最大整盒 | `POST /api/quotes` |
| 今天哪些样卡能装盒 | `GET /api/recommendations?barCode=BAR-DEMO&piecesPerBox=2` |
| 并发领片 | `POST /api/claims` 或 `POST /api/pieces/:code/claim` |
| 办单装盒 | `POST /api/orders` |
| 查盒 | `GET /api/boxes/:code` |
| 取货 | `POST /api/boxes/:code/pickup` |

设计卡编辑只写卡的新版本，不会改写 `BAR-DEMO.closedPours`；已合模条坯始终保留合模时的真实浇注快照。

## 代码结构

```text
server.js              零依赖 HTTP 服务、路由、静态页面
src/domain.js          方格变换、匹配、余料段、整盒最大流
src/store.js           事务、持久化、条/片/卡/单/盒/领取状态
src/demo.js            八格样条和两张样卡
public/                后台页面
examples/              本地 curl 全流程脚本
test/                  Node 内置测试：场景、并发、持久化
```
