# 街区串饰铺 / Beadworks

一个零第三方依赖的接件、试配、租借、装扣、拆还与重启可查服务。提供 Python 领域接口和 JSON HTTP 服务，数据以原子写入的 JSON 文件持久化。

## 先回答柜台问题

**原事实下：能。** 青珠珠孔为 `3`，途中唯一固定圈宽为 `2`；规则是“圈宽**超过**珠孔才挡住”，所以 `2 <= 3`，青珠可以越过。左端不可开，但右端有店里实际配上、且店方有权开启的扣；上面也没有其他仍在有效租借占有状态、需要先暂存的邻珠。因此周五 `2026-11-27` 可以单独取下青珠并拆还给登记物主。

两笔变案都不能按原日子交：

1. 固定圈改为宽 `4`：`4 > 3`，触发 `RING_BLOCKS_HOLE`。
2. 圈还原为宽 `2`，但唯一出口改成须另一位持有人到场同意的压封扣，而该人当天缺席：触发 `HOLDER_ABSENT`。

这三笔都只是接单前报价；服务不会把库存中未实配的扣当成出口，也不会自动加装扣件。

## 运行与验收

```bash
# 单元测试 + HTTP 往返测试
./test.sh

# 只跑测试
python3 -m unittest discover -s tests -v

# 跑完整中文验收剧本（三笔询价、分段单、拆还、转租、重启查询）
python3 -m beadworks.demo --db data/acceptance-demo.json

# 启动 HTTP 服务；默认数据库 data/beadworks.json，端口 8000
./run.sh
# 或
python3 server.py --db data/beadworks.json --port 8000
```

服务仅使用 Python 3.10+ 标准库。

## 本实现选择的经营款式：分段款

柜台选择“卖出时排好独立短链并实配店控连接扣/端扣”的方案：

- 每位持有人的珠在自己的独立短链段上；
- 段顺序、珠顺序、固定圈和已有扣由店员登记；
- 连接扣和端扣有尺寸，并且必须从库存扣执行实配/安装后才是出口；
- 分段款只接受 `shopControlled=true` 的店控扣，因此某位物主按不同日期来取约定珠时，不需要其他持有人到场；
- 若路径上有普通邻珠，会生成“从端口向里”的暂存顺序，每件都检查在店状态和临时取下许可，并留下暂存/穿回记录。

到店重穿款没有作为主交付款式实现，但到柜签到、持有人同意、压封扣校验和开扣审计已经具备，可支持约人重穿场景。

## 物理可行性规则

`quote_removal` / `plan_removal` 会分别计算左右方向：

1. 该端必须是 `port`，且引用的扣 `fitted=true`；目录里未安装的扣不是出口。
2. 目标珠与端口之间的每个固定圈都按位置检查：`ring.width > piece.holeWidth` 才阻挡；等宽可以通过。
3. 路径上的普通邻珠必须：
   - 有接件记录；
   - `tempRemovalAllowed=true`；
   - 当前在店，而不是被有效租借人领走；
   - 自身也能越过它到端口之间的固定圈。
4. 非店控/压封扣必须有对应持有人当天的实际到柜签到和明确同意。
5. 报价只返回完整路径和阻塞项，不安装扣、不建单、不改链。

真正的 `remove_and_return_piece` 只有在报价可行、领取人是物主或预授权收件人时才提交：暂存邻珠 → 验证/记录开扣 → 目标珠滑出 → 按相反顺序穿回邻珠 → 建立按件拆还记录并更新持有人。

`return_whole_chain` 是不同的整链退回记录：它不把珠从链轨道移除，也不会产生 `individualReturn=true`，因此不能冒充单独还珠。

## 持久化与重启查询

所有写操作都会原子替换 JSON 账本。重启后可查：

```http
GET /api/pieces/{pieceId}/location
GET /api/chains/{chainId}/report
```

返回重点包括：

- 拆出的珠现在由谁持有；
- 是否有单独拆还记录及对应拆还单号；
- 余链当前包含哪些珠；
- 余链仍连接在哪些实配扣上，以及每个扣连接哪些链段/端点；
- 余链现在在柜台还是某位租借人手中。

## 主要 HTTP 接口

JSON 入参同时接受 `snake_case` 和顶层 `camelCase`。链设计 DSL 内继续使用 `pieceId`、`ringId`、`claspId`。

| 方法与路径 | 作用 |
| --- | --- |
| `POST /api/people` | 建档人员/持有人/租客 |
| `POST /api/pieces` | 接件，登记珠孔、物主、在店和允许临时取下 |
| `POST /api/pieces/{id}/handling-permission` | 追加临时取下许可/在店记录 |
| `POST /api/clasps` | 登记扣或库存扣；`fitted=false` 时只是可用硬件 |
| `POST /api/clasps/{id}/open` | 校验到柜同意并记录开扣 |
| `POST /api/chains` | 店员登记链段先后、珠序、固定圈和已有实配扣 |
| `POST /api/chains/{id}/install` | 真正安装扣到端点或相邻链段之间 |
| `POST /api/try-fit` | 试算珠孔与圈宽 |
| `POST /api/quotes/removal` | 接单前单独拆还可行性报价 |
| `POST /api/orders/segmented` | 排珠、选扣、做实配分段成品单 |
| `POST /api/visits` | 预约到柜 |
| `POST /api/visits/{id}/check-in` | 实际到柜签到 |
| `POST /api/visits/{id}/absent` | 记录当天缺席 |
| `POST /api/consents` | 凭实际到柜记录登记开扣同意 |
| `POST /api/removals` | 真正按件拆还 |
| `POST /api/chain-returns` | 整链退回，明确不等同按件还珠 |
| `POST /api/rentals` | 建立有期租借/寄租单 |
| `POST /api/rentals/{id}/handover` | 租客真实领走 |
| `POST /api/rentals/{id}/return` | 租借物回柜 |
| `GET /api/pieces/{id}/location` | 查询珠在谁手、是否按件归还 |
| `GET /api/chains/{id}/report` | 查询余链、扣连接和持有人 |
| `GET /api/state` | 导出整本账 |

## Python 接口示例

```python
from beadworks import Beadworks

svc = Beadworks("data/shop.json")
svc.create_person(person_id="lin", name="林物主")
svc.receive_piece(
    piece_id="green",
    name="青珠",
    owner_id="lin",
    hole_width=3,
    temp_removal_allowed=True,
)
svc.add_clasp(clasp_id="right-clasp", size=3, kind="shop", fitted=True)
svc.create_chain(
    chain_id="old-half-product",
    segments=[{
        "left": {"type": "closed", "openable": False},
        "right": {"type": "port", "claspId": "right-clasp"},
        "track": [
            {"type": "bead", "pieceId": "green"},
            {"type": "ring", "ringId": "ring-2", "width": 2},
        ],
    }],
)

quote = svc.quote_removal("old-half-product", "green", "2026-11-27")
assert quote["feasible"]

removal = svc.remove_and_return_piece(
    "old-half-product", "green", "lin", "2026-11-27"
)
assert removal["wholeChainReturned"] is False
assert svc.locate_piece("green")["holderId"] == "lin"
```

## 代码结构

```text
beadworks/
  service.py   # 领域规则、事务式工作流、持久化触发
  store.py     # JSON 账本与原子写
  server.py    # 标准库 ThreadingHTTPServer JSON API
  demo.py      # 题目完整验收剧本
tests/
  test_beadworks.py
server.py      # python server.py 入口
main.py        # python main.py 入口
```
