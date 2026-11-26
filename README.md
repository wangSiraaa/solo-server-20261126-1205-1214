# 邻里拼车撮合与行程服务

同一张换乘单/同一个站名**不能**证明两位邻居能碰上。服务只承认：

1. 前车司机报告的实际到站，以及乘客凭乘车单产生的真实下车回执；
2. 从该下客点到后车上客点的已登记走行时间；
3. 走到上客点的时刻不晚于后车实际/计划驶离时刻；
4. 乘客当前没有仍在另一辆车上。

因此：

| 实际情况 | 走行 | 最早到上客点 | 后车 12–14 分停靠 | 结论 |
| --- | ---: | ---: | --- | --- |
| 前车 10–12 分，乘客第 10 分下车 | 2 分 | 12 分 | 第 14 分驶离 | 可接手 |
| 前车推迟为 13–15 分，第 13 分下车 | 2 分 | 15 分 | 第 14 分驶离 | 赶不上，不能拿 B 车销单 |
| 还原为第 10 分下车 | 改为 5 分 | 15 分 | 第 14 分驶离 | 原 B 车仍不成立，只能另派真车 |

另派的 `car_c` 有自己的站序、座位和工作时段；上车成功后会留下持久化 `BOARD` 回执。系统不会创建“无行程、无工时限制、哪里都能补”的虚拟车。

## 经营决定

演示社区在市集日经营的是：**定点保接为主、失败后由随到真车另派/票面原站兜底履约的乘车单服务**。

最小例子保持为 **3 辆车、4 个站**：

- 站：`south` 社区南门、`center` 邻里中心换乘站、`old` 槐树巷、`market` 周末市集；
- `car_a` 王师傅：南门 → 换乘站东下客点 → 槐树巷 → 市集；
- `car_b` 李师傅：换乘站西上客点 → 槐树巷 → 市集，在换乘站最多多等 2 分钟；
- `car_c` 赵师傅：南门 → 换乘站 → 槐树巷 → 市集，工作时段 6–40 分，已有 `RS_C001` 一单，因此剩 2 座。

有限的两个兜底座位优先签给 `RS_241` 的两位结伴邻居；`RS_303` 延后。定点保接窗 `HF_DEMO` 只申请 B 车等到第 15 分（比计划离站多等 1 分）。等待测算确认顾阿姨到槐树巷的预计时刻从 18 分变为 19 分，仍早于承诺的第 20 分，窗口才允许两名司机共同签成。若申请等到第 18 分，会同时突破 B 车 2 分钟等待余量和顾阿姨的承诺时刻，服务拒绝签字。

## 运行

需要 Node.js 20+，无第三方依赖。

```bash
npm test        # 10 个领域/HTTP/持久化测试
npm run scenarios
npm run demo
npm start       # 默认 http://127.0.0.1:3000，状态写入 data/service-state.json
```

`npm run scenarios` 用内存领域服务逐项验证题目中的三个分钟数例子、未下车不能换乘、另派 C 车和真实回执。

`npm run demo` 会：

1. 启动真实 HTTP 服务并播种 3 车 4 站；
2. 做兜底座位分配；
3. 两名司机确认保接窗，重复确认返回同一结果；
4. 司机报实际到站/离站；
5. 两位邻居凭 `RS_241` 重复确认上车、下车，重复请求幂等；
6. 产生一笔 B 车第 15 分离站的迟到消息（计划 14 分，仍在保接窗内）；
7. 顾阿姨途中把下车站从 `old` 改为 `market`；
8. 杀掉服务、用同一 JSON 文件重启；
9. 查询 `RS_241` / `RS_GU242`，返回当前车辆、已完成段和欠下车回执的路段。

## 重启后查询

```http
GET /tickets/RS_241/location
```

关键响应形态：

```json
{
  "code": "RS_241",
  "currentCarIds": ["car_b"],
  "allDelivered": false,
  "passengers": [
    {
      "id": "lin",
      "status": "on_board",
      "currentCarId": "car_b",
      "completedLegNos": [1],
      "owedLegs": [
        {
          "legNo": 2,
          "signedCarId": "car_b",
          "currentCarId": "car_b",
          "action": "alight",
          "toStationId": "market",
          "detail": "仍在 car_b 上，欠 周末市集 下车回执"
        }
      ]
    }
  ]
}
```

状态通过临时文件 + `rename` 原子写入 `DATA_FILE`。可这样手动启动：

```bash
DATA_FILE=./data/market.json PORT=3000 npm start
```

## 主要 HTTP API

| 方法与路径 | 作用 |
| --- | --- |
| `POST /stations` | 登记车站及下客点/上客点 |
| `POST /walk-paths` | 登记两点间定向站内走行分钟；`PATCH /walk-paths/:id` 可把 2 分钟改为 5 分钟 |
| `POST /cars` | 按站序申报 ETA、计划时刻、可等待分钟、工时、在车乘客最迟下车时刻 |
| `POST /cars/:id/events` | 司机报实际 `arrived` / `departed`；迟到离站自动写 `/messages` |
| `POST /tickets` | 开乘车单，最多两段，可声明同伴同车路段和原站兜底权利 |
| `POST /tickets/:code/board` | 乘客凭单确认上车；服务端校验前车下车回执、走行时刻和实际停靠 |
| `POST /tickets/:code/alight` | 乘客确认下车并产生持久回执 |
| `POST /tickets/:code/change-station` | 途中改未来下车站 |
| `GET /tickets/:code/location` | 查人在哪辆车、已完成/还欠哪一段 |
| `POST /transfers/evaluate` | 只做换乘可行性判定，返回 `readyAtMinute`、`slackMinutes` 和原因码 |
| `POST /handoffs` | 创建定点保接窗并测算等待是否伤害原有承诺 |
| `POST /handoffs/:id/confirm` | 前后车司机分别确认；两人齐了才 `signed`，写入接手和留车 |
| `POST /dispatches/on-demand` | 必须见到第一段真实 `ALIGHT` 回执后，才向仍能到该站的真车发约 |
| `POST /dispatches/fallback` | 派发票面约定的原站兜底；备用车同样校验行程、座位和工时 |
| `POST /dispatches/:id/accept` | 司机接约；只签下一段车，不产生上车回执 |
| `POST /operator/allocate-fallback` | 在真实座位/兜底预算内决定给哪组人签单、延后哪组 |
| `GET /messages` | 实际到离站、迟到、签窗、发约等消息 |

所有写接口支持 `Idempotency-Key` 请求头或 body 中的 `requestId`。相同键、相同请求体重放原响应；相同键但请求体不同返回 `409 IDEMPOTENCY_KEY_CONFLICT`。领域内对同一乘客同段同车的重复上车/下车、同一司机重复签窗也会返回原记录，不会新增第二张回执。

## 核心不变量

- 站名按 ID 和站内点识别；两个同名但没有走行路径的站返回 `WALK_PATH_MISSING`，不会因为字符串相等而判为可换乘。
- 没有前车 `ALIGHT` 回执，或乘客状态仍是 `on_board` 时，后一辆车的 `BOARD` 返回 `MUST_ALIGHT_FIRST` / `PREVIOUS_LEG_NOT_DELIVERED`。
- `alightAt + walkMinutes > secondDeparture` 时返回 `MISSED_DEPARTURE`，上车接口返回 `TRANSFER_NOT_FEASIBLE`。
- 接约/派单不等于上车；只有司机报实际到站且乘客通过时刻校验后，才产生真实 `BOARD` 回执。
- 留车时间超过该站 `canWaitMinutes`、车辆工时，或使已在车上的承诺乘客晚于 `latestAlightMin` 时，保接窗不能签成。
- 候选车必须有站序、足够座位和覆盖目的地到达时刻的 `workWindow`；系统不会自动生成虚拟补位车。

## 代码地图

- `src/domain.js`：纯领域规则、状态和所有撮合/幂等确认逻辑；
- `src/store.js`：原子 JSON 持久化；
- `src/server.js`：零依赖 Node HTTP 服务和路由；
- `src/canonical.js`：3 车 4 站的题目样例数据；
- `scripts/scenarios.mjs`：三个分钟数边界例子；
- `scripts/demo.mjs`：HTTP + 杀进程 + 重启恢复的完整演示；
- `test/domain.test.js`：领域不变量；
- `test/api.test.js`：HTTP 幂等和重启后持久化查询。
