# 战役借还后台（Campaign Lending Backend）

一套供共享桌游馆「实际借还」用的战役后台。它把**队伍存档**（逻辑进度）与**实物盒的永久痕迹**
（贴死的门牌、破封、刻痕）严格分开，支持两种借法：

- **认养战役盒**：队伍长期认养一只实物盒，盒上的永久变化保留在地图上。
- **空白底盒 + 个人战役袋**：可分离的门牌、已揭卡留在队伍的个人袋里；底盒上不可拆的破封、
  刻痕仍随实物走。借台负责搭配「兼容」的底盒与战役袋。

两种借法都完整覆盖 **领盒 → 现场行动 → 归还**。规则由馆员录入。全部状态持久化到磁盘，
馆长重启服务后可凭借单追溯。

---

## 一句话回答开篇的问题

> 一盒里那扇门已经贴死，周六来的另一队还能不能接它自己的战役？

**用 `GET /teams/:teamId/takeover-check/:boxId` 即可回答。** 它会同时检查：盒是否可借、
认养归属、盒上贴死的门牌 / 不可拆刻痕是否与本队在该章已经走过（或必须先走）的门**冲突**、
以及本队下一章是否真的还能续玩。只要盒上某章贴死的门与本队该章的门不同，返回
`ok:false` 并给出 `PLOT_REWRITE` / `INCOMPATIBLE_BOX` 及冲突的章节、盒上之门、本队之门，
外加制造这道痕迹的**来源回单号**。

---

## 设计要点与不变量

1. **队伍存档 ≠ 实物盒**。队伍对象只有进度 / 剧情 / 钥匙 / 已揭 / 暗袋；实物盒只记录永久痕迹。
   二者是两条独立记录，任何一方都不会被对方覆盖。
2. **还盒必须同时声明两件事**，缺一即拒：
   - `progress`：这队确实玩到哪里；
   - `boxMarks`：还回来的这盒真有哪道永久痕迹。
   回单（receipt）把两边**分别**落库，并保留发盒前 / 归还后的快照。
3. **两种合法剧情都接受**。发盒与现场选择都按规则实时推演，不依赖馆员预演的某一条；顾客选
   蓝还是红，只要队里条件（钥匙 / 前序门 / 互斥剧情）成立就走得通。互斥分支一旦选定不可改写。
4. **未揭情节必须真有资源才能承诺**。需要「完好密封图卡」的门，只有在馆内确实存在一张
   `sealed` 且未被占用的同情节图卡时才可发出；若所有图卡都被拆封，发盒直接被拒
   （`UNREVEALED_PLOT_UNAVAILABLE`）。
5. **并发只成一个**。所有写操作串行化；两支柜台同时借最后一盒时，恰好一个成功、
   另一个得到 `BOX_UNAVAILABLE`。
6. **重启可追溯**。每次写操作原子落盘（临时文件 + rename）。`/reports/rebindings`
   扫描持久化的盒上永久痕迹与各队存档，列出「哪队因哪道永久变化需要另挑盒」，并指向来源回单。

---

## 数据模型

| 实体 | 关键字段 | 说明 |
| --- | --- | --- |
| `rulebook` | `chapters[].doors[]`, `hiddenPouches[]` | 馆员录入：门、开门条件、互斥剧情、暗袋。 |
| `team` | `progress`, `plot[]`, `keys[]`, `reveals[]`, `hiddenPouchesOpened[]` | **纯逻辑存档**，与实物无关。 |
| `box` | `kind`（adopted/base）, `permanentMarks{affixedPlates,brokenSeals,grooves}` | **只**记永久痕迹，且带来源 `loanId/receiptId`。 |
| `pouch` | `separable{plates,revealedCards}` | 个人战役袋：可分离门牌与已揭卡，按队绑定。 |
| `card` | `plotKey`, `state`（sealed/opened） | 密封图卡；`opened` 不可逆。 |
| `loan` | `chapter`, `door`, `allocatedCardId`, `actions[]` | 一次借盒（现场行动追加到 `actions`）。 |
| `receipt` | `issue`, `result`, `status` | 出库 / 归还回单，重启后单查。 |

门的合法性由三层构成：**钥匙/前序/互斥**（结构合法）→ **密封图卡可满足**（资源可达）→
**实物盒痕迹兼容**（认养盒门牌 / 底盒刻痕不与队伍剧情冲突）。

---

## 快速开始

```bash
npm start                      # 启动 HTTP 服务（默认 :3000，状态存 data/state.json）
SEED=1 npm start               # 启动并灌入示例规则/库存（仅在空库时）
npm run demo                   # 运行端到端演示（对应题干叙事）
npm test                       # 运行全部测试（node:test，无第三方依赖）
```

Node ≥ 18，仅用标准库。

演示脚本 `scripts/demo.js` 完整复现题干主线：

1. 甲走蓝门拿钥匙；柜员发出一盒**有完整图卡、其余部件也合用**的蓝门续玩局，并登记**本次真开了图卡**。
2. 相同库存另开演示，**只把队伍原选择换成红** → 蓝门续局被拒（但红门续局仍被接受）。
3. 同一份蓝档与库存，**仅把全部可用图卡改成曾被拆封** → 不能承诺同一未揭情节。
4. 红队认养盒贴死红门牌；**模拟馆长重启服务**后，凭回单查到哪队因哪道永久变化需另挑盒。
5. **两支柜台并发借最后一盒**，只有一个成功。

---

## HTTP API

所有响应为 JSON；错误形如 `{ "error": { "code", "message", "details" } }`，
状态码按错误语义映射（400/404/409/422）。

### 规则 / 库存
| 方法 & 路径 | 作用 |
| --- | --- |
| `POST /rulebooks` · `GET /rulebooks` · `GET /rulebooks/:id` | 录入 / 查询战役规则 |
| `POST /teams` · `GET /teams` · `GET /teams/:id` | 队伍 |
| `POST /boxes` · `GET /boxes?kind=&campaignId=` · `GET /boxes/:id` | 实物盒 |
| `POST /cards` · `GET /cards?state=&plotKey=` · `GET /cards/:id` | 密封图卡 |
| `POST /pouches` · `GET /pouches?teamId=` · `GET /pouches/:id` | 个人战役袋 |

### 领盒 / 现场 / 归还
| 方法 & 路径 | 作用 |
| --- | --- |
| `POST /loans` | 发盒。body 支持 `teamId, model(adopted/base), boxId, pouchId?, door?, cardId?, openCard?, takeover?` |
| `POST /loans/:id/choose-door` | 现场选门（蓝或红）；互斥不可改 |
| `POST /loans/:id/open-hidden-pouch` | 开暗袋；揭过不可复原 |
| `POST /loans/:id/affix-plate` | 贴门牌（认养盒→实物盒；底盒→个人袋） |
| `POST /loans/:id/open-card` | 开图卡 |
| `POST /loans/:id/return` | 还盒。body 必须同时含 `progress` 与 `boxMarks` |
| `GET /loans` · `GET /loans/:id` | 借单 |

### 发盒前预检 / 追溯
| 方法 & 路径 | 作用 |
| --- | --- |
| `GET /teams/:id/continuation` | 本队下一章可续玩的门（结构合法 + 图卡可达 + 缺口原因） |
| `GET /teams/:id/takeover-check/:boxId?takeover=true&pouchId=` | 回答“这队能不能接这只盒、为什么” |
| `GET /receipts` · `GET /receipts/:id` | 回单（重启后单查） |
| `GET /reports/rebindings` | 哪队因哪道永久变化需要另挑盒，附来源回单 |

---

## 主要错误码

| code | 含义 |
| --- | --- |
| `BOX_UNAVAILABLE` | 盒已被借出 / 占用（并发抢最后一盒的失败方） |
| `PLOT_REWRITE` | 接手盒会改写既有剧情（认养盒门牌 vs 本队该章之门） |
| `INCOMPATIBLE_BOX` | 底盒不可拆痕迹与队伍/个人袋剧情不兼容 |
| `DOOR_ILLEGAL` | 门对本队不合法（钥匙 / 前序 / 互斥） |
| `UNREVEALED_PLOT_UNAVAILABLE` | 需要完好密封图卡，但馆内已无可用的一张 |
| `IRREVERSIBLE` | 互斥分支 / 暗袋 / 图卡已定，不能复原 |
| `NO_LEGAL_CONTINUATION` | 本队没有可兑现的下一章 |
| `TEAM_BUSY` | 该队已有未归还的借盒 |

---

## 目录结构

```
src/
  errors.js     领域错误与稳定错误码
  service.js    核心领域服务（事务、规则推演、借还、追溯）
  fixtures.js   示例规则与基准库存（对应题干场景）
  server.js     HTTP 路由（纯标准库）
  index.js      启动入口
scripts/
  demo.js       端到端演示
test/
  service.test.js  领域闭环、对照实验、并发、重启追溯、还盒契约、takeover 预检
  http.test.js     HTTP 层端到端
```
