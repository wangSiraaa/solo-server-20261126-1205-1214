# 课堂涂格白板服务

一台零第三方依赖的 JSON HTTP 服务：老师从当前已签字总板分出小组板；小组通过网络请求改共边挡线或给连通区域涂色；老师在“照样贴图”或“重演笔法”两种约定中选择一种，按教案约束和稿件分值求最高总分的合法合班，并签字生成新总板编号。

## 先回答：蓝色水洼该不该漫过乙刚开的缺口？

取决于本节课采用的合班约定：

- **照样贴图（`patch`）**：不回溯扩散。甲交稿时承诺看到的是“二、三格之间有挡线”，所以甲稿的贴图就是第一、二格蓝。乙后来去掉挡线可以同时贴上，但蓝色不会因为新缺口自动漫进第三格。它保留的是学生当时演示出的画面。
- **重演笔法（`replay`）**：如果乙开缺口的笔法排在甲涂色之前，或乙的改动已经提前签字合入当天总板，那么甲的同一笔会在新连通关系上重算，蓝色会漫过第一、二、三格。若甲笔排在乙之前，则甲仍只涂一、二格，之后缺口才打开。
- 如果开缺口的挡线只存在于一份**尚未入选**的稿件中，计划算法只在临时棋盘上推演；老师的当前总板和新分小组都不会提前看到这道缺口。

一行三格的固定示例：

```text
初始：        白  白 ┃ 白
甲从格0涂蓝： 蓝  蓝 ┃ 白
乙去掉挡线：  蓝  蓝   白   （贴图：蓝、蓝、白）

重演顺序 乙→甲：
先去线：      白  白   白
再涂蓝：      蓝  蓝   蓝   （水洼漫过缺口）

重演顺序 甲→乙：
先涂蓝：      蓝  蓝 ┃ 白
再去线：      蓝  蓝   白
```

第三格禁蓝时：

- 贴图甲稿本来没有写第三格，通常仍可与乙稿同时发表；
- 但若老师规定重演顺序为乙→甲，甲笔会把第三格涂蓝，因此双稿方案违法，优化器会改选其他合法高分组合。

恢复第三格后，再把第二格列为禁蓝：甲的连通涂色本来就覆盖第二格，因此无论贴图还是重演，甲稿都不能原样发表。

## 运行

要求 Python 3.10+，无需安装依赖。

```bash
python3 server.py
# 或 npm start
```

默认监听 `0.0.0.0:8000`，数据写到 `/workspace/data/whiteboard.json`。可用环境变量覆盖：

```bash
PORT=8080 WHITEBOARD_DATA=./data/demo.json python3 server.py
```

浏览器打开 <http://127.0.0.1:8000/> 有一个简易控制台；接口索引在 `GET /api`。

运行单元测试：

```bash
npm test
# 或 python3 -m unittest discover -s tests -v
```

运行完整 HTTP 情景核查（自动起服务、重启、用新老师会话核对）：

```bash
npm run e2e
# 或 python3 scripts/e2e.py
```

## 领域规则

- 棋盘最多 **5 行 × 5 列**。
- 单节课最多 **6 份稿件**。
- 每份稿件最多 **4 步操作**。
- 两格相通当且仅当它们共边，且该共边上没有挡线。
- 涂色使用颜料桶/洪水填充：从起点开始，只穿过无挡线共边，并且只进入与“落笔时起点颜色”相同的格子。
- 小组板从老师**当前签字总板**克隆；各小组通过独立、不可枚举的小组资源预览，课节详情不展开其他小组的私有棋盘。
- 每次正式发表都会生成新的 `board_...` 编号、合班记录和稿件去向；之后创建的小组从新编号开课。
- 所有状态每次写入都原子保存到 JSON 文件；重启服务后自动载入。

## 主要 API

所有请求和响应均为 JSON。

### 老师与课节

```http
POST /api/teacher-sessions
GET  /api/teacher-sessions/{teacherId}

POST /api/sessions
GET  /api/sessions
GET  /api/sessions/{sessionId}
PATCH /api/sessions/{sessionId}/lesson
```

创建课节示例：

```bash
curl -sX POST http://127.0.0.1:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "demo",
    "title": "一行三格",
    "convention": "patch",
    "rows": 1,
    "cols": 3,
    "walls": [[[0,1],[0,2]]],
    "lesson": {
      "must_keep": [],
      "forbidden": [{ "cell": [0,2], "color": "blue" }]
    }
  }'
```

`convention` 可取：

- `patch`、`sticker`、`照样贴图`、`贴图`
- `replay`、`stroke`、`重演笔法`、`重演`

一节课创建后不切换约定；若要对照两种约定，请像 `scripts/e2e.py` 一样创建两节使用同一套稿件的课。

### 小组、预览和操作

```http
POST /api/sessions/{sessionId}/groups
GET  /api/groups/{groupId}
POST /api/groups/{groupId}/preview
POST /api/groups/{groupId}/operations
POST /api/groups/{groupId}/drafts
POST /api/sessions/{sessionId}/drafts
```

预览不会保存：

```bash
curl -sX POST http://127.0.0.1:8000/api/groups/jia/preview \
  -H 'Content-Type: application/json' \
  -d '{"steps":[{"op":"paint","at":[0,0],"color":"blue"}]}'
```

操作格式：

```json
{ "op": "paint", "at": [0, 0], "color": "blue" }
```

```json
{ "op": "remove_wall", "edge": [[0, 1], [0, 2]] }
```

也支持：

- `add_wall` / `set_wall`（配合 `"wall": true|false`）
- `toggle_wall`
- 字符串边：`"0,1|0,2"`
- 坐标字符串：`"0,0"`

也可以先反复调用 `/operations` 保存小组板，再调用 `POST /api/groups/{groupId}/drafts`；不带 `steps` 时，会默认把该组自开板以来的操作段整理成稿（仍受每稿 4 步限制）。

### 稿件

```http
POST  /api/sessions/{sessionId}/drafts
GET   /api/sessions/{sessionId}/drafts
GET   /api/drafts/{draftId}
PATCH /api/drafts/{draftId}
```

稿件保存：

- 原始 `steps`（最多 4 步）；
- 交稿时的 `observed_board`；
- 明确的 `promise`：交稿时亲眼看到的全部格色、共边挡线及摘要；
- 小组预览结果 `preview_board`；
- 与所见棋盘的差异 `patch`；
- 分值 `score`。

示例：

```bash
curl -sX POST http://127.0.0.1:8000/api/sessions/demo/drafts \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "jia",
    "group_id": "jia",
    "title": "甲的蓝色水洼",
    "score": 5,
    "steps": [{"op":"paint","at":[0,0],"color":"blue"}]
  }'
```

### 最优合班与签字发表

```http
POST /api/sessions/{sessionId}/merge/plan
POST /api/sessions/{sessionId}/merge/publish
GET  /api/sessions/{sessionId}/merges
GET  /api/merges/{mergeId}
GET  /api/boards/{boardId}
```

`plan` 是只读的，不会移动当前总板。请求体可指定：

```json
{
  "draft_ids": ["jia", "yi"],
  "allowed_orders": [["yi", "jia"], ["jia", "yi"]],
  "precedence": [["yi", "jia"]]
}
```

- 贴图约定枚举所有稿件子集，检查写冲突和教案约束。
- 重演约定枚举子集与顺序；若老师给了 `allowed_orders` 或 `precedence`，只保留允许的顺序。
- 目标是合法稿件分值总和最大；同分时再比较稿件数和 ID，结果确定。题目允许平分时任选。
- `best.draft_reports[].real_changes` 给出该稿在选定合班上下文中的真实变化。
- 重演报告还用 `changed_because_of_merge_context` 标出该稿是否因别的稿先改变连通关系而与小组预览不同。

发表最优方案：

```bash
curl -sX POST http://127.0.0.1:8000/api/sessions/demo/merge/publish \
  -H 'Content-Type: application/json' -d '{}'
```

也可精确发表某个顺序：

```bash
curl -sX POST http://127.0.0.1:8000/api/sessions/demo/merge/publish \
  -H 'Content-Type: application/json' \
  -d '{"draft_ids":["yi","jia"],"order":["yi","jia"]}'
```

发表后：

- 新棋盘存入 `boards/{newBoardId}`；
- 课节的 `current_board_id` 更新；
- 入选稿件标记 `published` 并记录 `merge_id` / `published_board_id`；
- 未入选稿件保留为 `available`，合班记录中写 `not_selected`；
- 服务重启后这些去向仍可查询。

## 教案约束

```json
{
  "must_keep": [
    { "cell": [0, 0], "color": "white" }
  ],
  "forbidden": [
    { "cell": [0, 2], "color": "blue" }
  ]
}
```

- `must_keep`：必留格；不写 `color` 时必须保持棋盘底色，写了 `color` 时该格最终必须是指定颜色（也可当作“必得格/必达标格”使用）。
- `forbidden` / `forbidden_paint`：禁涂规则。指定 `color` 时禁止该颜色；`"color": "*"` 表示禁止离开底色。
- 最终棋盘必须满足约束；重演过程中若画笔曾把禁涂格涂成禁色，即使后面又覆盖，也会留下 `forbidden_paint_during_replay` 并判为不合法。

## 代码结构

```text
server.py                  启动入口
whiteboard/model.py        棋盘、洪水填充、diff、两种优化器
whiteboard/service.py      课节/小组/稿件/发表的状态服务
whiteboard/store.py        JSON 原子持久化
whiteboard/server.py       零依赖 HTTP 路由
static/index.html          简易浏览器控制台
tests/test_whiteboard.py   领域与持久化测试
scripts/e2e.py             HTTP + 重启 + 第二老师会话情景核查
```
