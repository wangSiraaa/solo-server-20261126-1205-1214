# 沙发送装接单与格图回放服务

一个零第三方依赖的 Python 本地服务：家具店按型号提供部件格形、**显式旋转模板**、允许组合、连接面和装配腾空面；物业录入最多 `6×6` 的格图。服务会穷举平移/90°转向、分件暂放和组装顺序，产出可逐步回放的报价单、步单和量尺版本溯源。

## 运行

```bash
python server.py --port 8000
# 或
python -m sofa_service.server --host 127.0.0.1 --port 8000
# npm start 也可；默认还会尝试监听 3000/5000，便于不同本地探测约定
```

运行验收剧情：

```bash
python -m sofa_service.demo
```

运行测试：

```bash
python -m unittest discover -s tests -v
```

仅使用 Python 3.11 标准库。

## 它如何保证不是“看起来能过”

- 每次移动都是离散动作：
  - `translate`：支点平移东/西/南/北一格；
  - `rotate`：按型号给定的 `orientation:direction` 旋转模板检查**所有扫过格**，而不是只检查起点和终点。
- 墙、物业临时封格、其他已暂放/已接死部件都会成为碰撞格。
- 整装单搜索整个放置状态；分件单搜索“哪一件先送、暂放到哪、何时移到接合位、按什么顺序接”的有限状态图。
- 报价中的每一步都可由 `sofa_service.verifier.replay_step_sheet` 独立回放。
- 封格、还原、换 SKU、改版腾空面后，旧步单都必须重新验证；服务不会静默沿用旧结论。

## 内置剧情

### 1. 顺利小单

平面图 `small-smooth` 是开放 `6×6` 空间。默认订单同时存在整装和分件方案；服务优先选择 9 步整装方案，含锁定朝向、预装组合和保护套信息。

### 2. 窄廊 L 形转向

平面图 `narrow-l-turn`：

```text
E#C###
EDCC##
##CC##
##C###
##NAAA
##AAAA
```

坐标为 `[x,y]`，x 向右、y 向下。座体从电梯中的 `[0,1], o=0` 水平姿态出发，先平移到支点 `[2,1]`，顺时针转 90°。该旋转模板显式扫过：

```text
[2,1] [3,1]
[2,2] [3,2]   # [3,2] 是中间扫过格
```

起点和终点姿态都不占据 `[3,2]`，但旋转过程会扫到它。物业封掉 `[3,2]` 后，整单复验会在该旋转步报 `PROPERTY_BLOCKED`，服务不再签发后续步骤。还原该格后几何哈希恢复为报价时哈希，原步单重新有效。

### 3. 凸耳扶手与腾空面改版

- `arm-lug` 比标准一格扶手多出凸耳格；旧标准扶手步单不能直接套用，复验会报 `ORDER_SNAPSHOT_MISMATCH`，并用新形体重放原动作，进一步给出墙/碰撞错误。
- 新凸耳订单会重新搜索暂放和转向方案。
- 把 `back-to-seat` 的腾空面从 `[[-1,-1]]` 改成 `[[2,0]]` 后，若先接左扶手，该空格已被占用，旧组装顺序失效；新报价会把靠背接合安排在左扶手就位之前。

### 4. 两版量尺与领单锁

系统为同一门口保存追加式量尺版本。报价固定引用具体 `measurementVersionId`。一张报价确认并由师傅领单后，另一版量尺产生的“迟到确认”不能替换该订单，接口返回 `409 ORDER_ALREADY_CLAIMED`。订单和报价的 `provenance` 可查到依据的量尺版本、量尺员和尺寸。

## 主要 HTTP 接口

```text
GET  /health
GET  /catalog
GET  /floorplans
GET  /floorplans/{id}
POST /floorplans
POST /floorplans/{id}/blocks
POST /floorplans/{id}/restore

GET  /survey/doors/{doorId}/measurements
POST /survey/doors/{doorId}/measurements
GET  /measurements/{id}

POST /quotes
GET  /quotes/{quoteId}
POST /quotes/{quoteId}/confirm

POST /orders/{orderId}/claim
GET  /orders/{orderId}
GET  /orders/{orderId}/frames
GET  /orders/{orderId}/provenance
POST /orders/{orderId}/revalidate
POST /orders/{orderId}/issue-next
POST /orders/{orderId}/steps/next
POST /orders/{orderId}/steps/{stepRef}/complete
POST /orders/{orderId}/report

POST /planner/plan
POST /planner/replay
POST /planner/render
POST /admin/catalog/models/{modelId}/clearance
POST /admin/reset
```

### 报价示例

```bash
curl -sS -X POST http://127.0.0.1:8000/quotes \
  -H 'Content-Type: application/json' \
  -d '{
    "customerId":"customer-1",
    "floorPlanId":"narrow-l-turn",
    "measurementVersionId":"m-door-v1",
    "order":{
      "modelId":"yunwu-two-seat",
      "orientation":"right",
      "mode":"piecewise",
      "seatSku":"seat-2",
      "backSku":"back-detachable",
      "leftArmSku":"arm-std",
      "rightArmSku":"arm-std",
      "sourcePlacement":{"pivot":[0,1],"orientation":0},
      "targetSeatPlacement":{"pivot":[3,5],"orientation":0}
    }
  }'
```

确认报价、师傅领单、报第 1 步：

```bash
curl -sS -X POST .../quotes/q-1/confirm \
  -H 'Content-Type: application/json' -d '{"customerId":"customer-1"}'

curl -sS -X POST .../orders/ord-1/claim \
  -H 'Content-Type: application/json' -d '{"masterId":"master-zhao"}'

curl -sS -X POST .../orders/ord-1/steps/1/complete \
  -H 'Content-Type: application/json' \
  -d '{"claimToken":"<claim-token>","masterId":"master-zhao"}'
```

本地服务默认也允许只传 `masterId` 或不带凭证；领单人和 token 仍会记录。

### 封格与还原

```bash
curl -sS -X POST http://127.0.0.1:8000/floorplans/narrow-l-turn/blocks \
  -H 'Content-Type: application/json' \
  -d '{"cells":[[3,2]],"reason":"property cabinet"}'

curl -sS -X POST http://127.0.0.1:8000/orders/ord-1/revalidate -d '{}'

curl -sS -X POST http://127.0.0.1:8000/floorplans/narrow-l-turn/restore -d '{}'
```

## 自定义数据格式

部件：

```json
{
  "sku": "seat-2",
  "kind": "seat",
  "cells": [[0,0],[1,0]],
  "fulcrum": [0,0],
  "rotationTemplates": [{
    "from": 0,
    "direction": "cw",
    "pivot": [0,0],
    "swept": [[0,0],[1,0],[1,1],[0,1]],
    "phases": [
      [[0,0],[1,0]],
      [[0,0],[1,0],[1,1],[0,1]],
      [[0,0],[0,1]]
    ]
  }],
  "completeTemplates": true
}
```

只给 `0:cw` 模板时，服务会派生其余三个方向和逆时针模板；若给出全部模板，则完全以店里的模板为准。

平面图可用字符串行：

- `#`：墙；
- `E`：电梯内厢；
- `D`：电梯/门口；
- `C`：走廊；
- `N`：入户位置；
- `H`：屋内；
- `A`：允许装配区域；
- `.`：普通可通行格。

## 代码结构

- `sofa_service/geometry.py`：格坐标、朝向、显式旋转模板。
- `sofa_service/floorplan.py`：`6×6` 平面图和区域解析。
- `sofa_service/catalog.py`：默认沙发型号、SKU、工艺连接规则和价格。
- `sofa_service/movement.py`：单搬件 BFS、碰撞和扫格校验。
- `sofa_service/ordering.py`：订单实例化、左右朝向和最终接合坐标。
- `sofa_service/planner.py`：整装与分件/暂放/组装搜索。
- `sofa_service/verifier.py`：步单独立回放与失效原因。
- `sofa_service/service.py`：报价、确认、量尺、领单锁、报步和溯源。
- `sofa_service/server.py`：标准库 HTTP JSON 服务。
- `sofa_service/demo.py`：五个规定剧情的命令行验收。
- `tests/`：工作流和 HTTP 接口测试。
