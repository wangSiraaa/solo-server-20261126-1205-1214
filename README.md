# 方言社本地词典编辑与制册服务

零第三方依赖的 Node.js 20 HTTP 服务，实现：

- 词头、稳定编号义项、例句片段标注、例句用法核义；
- 旧义拆分、义项合并、词头改名与当前索引更新；
- 教师出题、辨义标签、释义选项、唯一性/标签/词典证据检查；
- **只交付一种课堂：按版封题（versioned edition）**；
- 版本自足快照、学生开卷快照、断线幂等补交、按“当时所见释义”复核；
- 可查询的继续出 / 教师重编决定。

> 没有实现“随编课”的翻题时动态换选项。题目若要跟新义，由教师另发新版；旧版和已开卷学生继续使用冻结快照。

## 运行

```bash
npm test          # 自动端到端断言
npm run demo      # 内存库中跑一遍核心剧本并打印关键结果
npm start         # 默认 http://127.0.0.1:3000
PORT=8080 npm start
```

服务默认把数据写到 `data/db.json`；设置 `DB_PATH=:memory:` 可完全使用内存库。测试和 demo 都使用内存库。

启动后可看：

- `GET /api`：接口目录；
- `GET /health`：健康检查；
- `GET /api/state`：全部本地数据；
- `POST /api/admin/reset`：重置为 `{"scenario":"pre-split"}` 或 `{"scenario":"post-split"}`。

## 种子编号

| 编号 | 含义 |
| --- | --- |
| `HW_DADENG` | 词头“打灯”，ID 不随字符串修改 |
| `G` | 拆义前旧笼统义，语义键同时含 `illumination`、`signal` |
| `L` / `S` | 拆分后的“照明” / “提示” |
| `TAG_ILLUM` / `TAG_CUE` | 教师指定辨义标签“照明” / “提示” |
| `E1` | “天黑后的山路上，他**打灯**照清台阶。” |
| `E2` | “……他在滩头**打灯**：灯光照亮水道，也一明一暗地提示来船。” |
| `U_E1_G`、`U_E1_L`、`U_E2_L`、`U_E2_S` 等 | 例句片段上的稳定用法标注 |
| `Q_OLD` / `EDITION_OLD` / `ATT_A` | 拆义前旧练习、旧版、学生甲已打开但未交的作答 |
| `ATT_OLD_DONE` | 拆义前已按 `OPT_QOLD_G` 交卷的记录，可直接 `GET /api/attempts/ATT_OLD_DONE/review` |
| `Q_NEW` / `EDITION_NEW` / `ATT_B` | 新义剧本中的新练习、新版、学生乙（post-split 种子） |

默认 `pre-split` 种子已经包含 `EDITION_OLD` 和 `ATT_A`，表示学生甲在拆义前已开卷；当前词典仍只有活跃义项 `G`。

## 核心请求流程

下面命令假设服务运行在 `localhost:3000`。

### 1. 编辑把 G 拆成 L、S

```bash
curl -X POST http://localhost:3000/api/senses/G/split \
  -H 'content-type: application/json' \
  -d '{
    "editorId":"ED1",
    "reason":"旧 G 兼照明和灯语提示",
    "senses":[
      {"id":"L","number":2,"code":"L","label":"照明","semanticKey":"illumination",
       "definition":"点亮或举着灯，使某处有光、便于看清。"},
      {"id":"S","number":3,"code":"S","label":"提示","semanticKey":"signal",
       "definition":"用灯光、明灭或灯语向人示意、发出提示。"}
    ]
  }'
```

`G` 不会删除，而会变成 `status:"split"`、`splitInto:["L","S"]`，供旧版复核和谱系查询。当前活跃义项只有 L、S；连同历史 G 共三个义项记录，在五个以内。

### 2. 两名编者核义

例句一由两名编者分别核为 L：

```bash
curl -X POST http://localhost:3000/api/examples/E1/verifications \
  -H 'content-type: application/json' \
  -d '{"editorId":"ED1","senseIds":["L"]}'

curl -X POST http://localhost:3000/api/examples/E1/verifications \
  -H 'content-type: application/json' \
  -d '{"editorId":"ED2","senseIds":["L"]}'
```

例句二由两名编者分别核为 L、S：

```bash
curl -X POST http://localhost:3000/api/examples/E2/verifications \
  -H 'content-type: application/json' \
  -d '{"editorId":"ED1","senseIds":["L","S"]}'

curl -X POST http://localhost:3000/api/examples/E2/verifications \
  -H 'content-type: application/json' \
  -d '{"editorId":"ED2","senseIds":["L","S"]}'
```

同一套两人共识也可由编辑主任用下面接口直接登记：

```bash
curl -X PUT http://localhost:3000/api/examples/E1/certified-usages \
  -H 'content-type: application/json' \
  -d '{"senseIds":["L"],"editorIds":["ED1","ED2"]}'
```

### 3. 查询“谁还能继续出”的决定

```bash
curl http://localhost:3000/api/decisions/report
```

唯一答案题的判定规则：

1. 教师标签必须通过 `semanticKey` 或 `targetSenseId` 找到当前活跃词典义项；
2. 该义项必须在例句当前核定用法集合中；
3. 因为原题要求**例句本身唯一体现**，核定用法集合必须恰好只有这一个义项；
4. 练习引用的 `usageId/usageSenseId`、正确选项的 `senseId` 必须与该义项一致；
5. 单选题必须恰好有一个正确选项且选项不重复。

核心结果：

| 候选 | 核定用法 | 标签 | 可查决定 |
| --- | --- | --- | --- |
| `E1 × TAG_ILLUM` | `[L]` | 照明 | `eligible:true` / `ELIGIBLE_UNIQUE`，可入新版 |
| `E2 × TAG_ILLUM` | `[L,S]` | 照明 | `teacher_rewrite` / `AMBIGUOUS_EXAMPLE_FOR_UNIQUE_ANSWER` |
| `E1` 临时改成 `[L,S]` 后 | `[L,S]` | 照明 | 同一唯一答案失效，不能封新版 |
| 恢复 `[L]` 后换 `TAG_CUE` | `[L]` | 提示 | `teacher_rewrite` / `EXAMPLE_TAG_MISMATCH` |

历史版本不按当前规则重判：`EDITION_OLD` 和 `ATT_A` 仍按 G 快照判卷。

也可查询历史评估：

```bash
curl 'http://localhost:3000/api/decisions?subjectId=Q_NEW'
curl 'http://localhost:3000/api/decisions?verdict=teacher_rewrite'
```

### 4. 教师出 Q_NEW 并封新版

```bash
curl -X POST http://localhost:3000/api/questions \
  -H 'content-type: application/json' \
  -d '{
    "id":"Q_NEW",
    "teacherId":"TEA1",
    "exampleId":"E1",
    "usageSenseId":"L",
    "requiredTagId":"TAG_ILLUM",
    "selectionMode":"single",
    "requireUniqueExample":true,
    "options":[
      {"id":"OPT_QNEW_S","senseId":"S","correct":false,
       "definition":"用灯光明灭或灯语示意、提示。"},
      {"id":"OPT_QNEW_L","senseId":"L","correct":true,
       "definition":"点亮或举着灯，使某处有光；照明。"},
      {"id":"OPT_QNEW_COVER","correct":false,"definition":"用东西遮盖、遮蔽。"}
    ]
  }'

curl -X POST http://localhost:3000/api/editions \
  -H 'content-type: application/json' \
  -d '{"editionId":"EDITION_NEW","teacherId":"TEA1","questionIds":["Q_NEW"]}'
```

发行时会冻结：

- 例句全文、被标片段和 span；
- 当时核定用法、题面、辨义标签；
- 选项 ID、A/B/C 编号、释义和正确项；
- 可查 `glossary` 释义表；
- 词头字符串和词典版本。

教师可看答案键：

```bash
curl 'http://localhost:3000/api/editions/EDITION_NEW?view=teacher'
curl http://localhost:3000/api/editions/EDITION_NEW/glossary/L
```

学员视图 `GET /api/editions/:id` 会移除 `correct/answerKey`。

### 5. 学生乙按新义开卷

```bash
curl -X POST http://localhost:3000/api/editions/EDITION_NEW/attempts \
  -H 'content-type: application/json' \
  -d '{"attemptId":"ATT_B","studentId":"STU_B"}'
```

新版故意排成 `A = OPT_QNEW_S（提示）`、`B = OPT_QNEW_L（照明）`，与旧版 `A = OPT_QOLD_G` 不同。

### 6. 学生甲断线补交：旧 A 不能套到新版

甲的 `ATT_A` 已绑定 `EDITION_OLD`。如果断线重连后错误地请求新版路径：

```bash
curl -i -X POST http://localhost:3000/api/editions/EDITION_NEW/submit \
  -H 'content-type: application/json' \
  -d '{
    "attemptId":"ATT_A",
    "clientSubmissionId":"offline-1",
    "answers":[{"questionId":"Q_OLD","optionNumber":"A"}]
  }'
# 409 EDITION_MISMATCH
```

回到自己的作答号提交。选项号 `A` 只会在 `ATT_A.snapshot` 的旧版选项中解析：

```bash
curl -X POST http://localhost:3000/api/attempts/ATT_A/submit \
  -H 'content-type: application/json' \
  -d '{
    "clientSubmissionId":"offline-1",
    "answers":[{"questionId":"Q_OLD","optionNumber":"A"}]
  }'
```

结果按旧 G 释义判对。同一请求或同一 `Idempotency-Key` 断线重试会返回同一个 review，不会重复交卷。

如果把旧 `optionId` 发到新学员的新版快照：

```bash
# OPT_QOLD_G 不在 EDITION_NEW/ATT_C 的可见选项中
# 400 OPTION_NOT_VISIBLE_IN_ATTEMPT
```

因此不能把旧选项号或旧选项 ID 套到新版。

### 7. 按实际所见释义复核

```bash
curl http://localhost:3000/api/attempts/ATT_A/review
curl http://localhost:3000/api/reviews/ATT_A
```

review 的每个选项都带冻结的 `optionId/optionNumber/senseId/definition`。旁边的 `currentDictionary` 只说明该义项现在的状态，例如 `G.status = "split"`、`splitInto = ["L","S"]`，不覆盖判卷依据。

### 8. 歧义变更与标签更换

把 E1 改成兼 L、S：

```bash
curl -X PUT http://localhost:3000/api/examples/E1/certified-usages \
  -H 'content-type: application/json' \
  -d '{"senseIds":["L","S"],"editorIds":["ED1","ED2"]}'

curl -X POST http://localhost:3000/api/questions/Q_NEW/evaluate
# reasonCode = AMBIGUOUS_EXAMPLE_FOR_UNIQUE_ANSWER
```

此时再发 `EDITION_NEW_BAD` 会得到 `422 EDITION_NOT_READY`，系统不会保留一个名义上的唯一答案。

恢复唯 L，只把教师标签换成“提示”：

```bash
curl -X PUT http://localhost:3000/api/examples/E1/certified-usages \
  -H 'content-type: application/json' \
  -d '{"senseIds":["L"],"editorIds":["ED1","ED2"]}'

curl -X PATCH http://localhost:3000/api/questions/Q_NEW \
  -H 'content-type: application/json' \
  -d '{"requiredTagId":"TAG_CUE"}'
# eligibility.reasonCode = EXAMPLE_TAG_MISMATCH
```

### 9. 改词头只影响后续索引

```bash
curl -X PATCH http://localhost:3000/api/headwords/HW_DADENG \
  -H 'content-type: application/json' \
  -d '{"text":"打灯儿"}'

curl 'http://localhost:3000/api/index?q=打灯儿'
```

当前索引和后续新版使用新词头；`EDITION_OLD.headword.text` 仍是“打灯”。改名不会改变例句的 `certifiedSenseIds`，所以 E2 的歧义决定也不变。

### 10. 合并义项

```bash
curl -X POST http://localhost:3000/api/editor/senses/merge \
  -H 'content-type: application/json' \
  -d '{
    "editorId":"ED1",
    "sourceSenseIds":["L","S"],
    "targetSense":{
      "id":"M",
      "label":"灯光用途合并义",
      "definition":"用灯照亮或示意。"
    }
  }'
```

合并会迁移当前例句用法和草稿引用；已发行 edition 与已生成 review 仍是不可变快照。

## 主要 HTTP 接口

| 方法与路径 | 作用 |
| --- | --- |
| `POST /api/admin/reset` | 重置种子剧本 |
| `GET /api/state` | 查看完整库 |
| `GET /api/index?q=...` | 当前词头/义项索引 |
| `PATCH /api/headwords/:id` | 改词头字符串并保留曾用名 |
| `GET/POST /api/senses` | 义项查询 |
| `POST /api/senses/:id/split` | 拆分旧义 |
| `POST /api/editor/senses/merge` | 合并义项并迁移当前用法 |
| `GET/POST /api/examples` | 例句查询/新建 |
| `POST /api/examples/:id/verifications` | 一名编者核义 |
| `PUT/PATCH /api/examples/:id/certified-usages` | 直接重设核定用法 |
| `GET/POST/PATCH /api/questions/:id?` | 练习增改查 |
| `POST /api/questions/:id/evaluate` | 重新生成可查决定 |
| `GET /api/decisions/report` | 例句 × 标签判定矩阵 |
| `POST /api/decisions/evaluate` | 评估不落练习的候选 |
| `GET /api/decisions` | 历史决定，可按 `subjectId/verdict/eligible` 过滤 |
| `POST /api/editions` | 检查并封自足版本 |
| `GET /api/editions/:id` | 学员版（隐藏答案）或 `?view=teacher` |
| `GET /api/editions/:id/glossary/:senseId?` | 本版冻结释义表 |
| `POST /api/editions/:id/attempts` | 学生开卷并复制所见快照 |
| `POST /api/attempts` | 不带版本路径开卷 |
| `POST /api/attempts/:id/submit` | 按 attempt 快照判卷，支持幂等重试 |
| `POST /api/editions/:id/submit` | 版本路径交卷；路径版本与 attempt 不符则 `EDITION_MISMATCH` |
| `GET /api/attempts/:id/review` | 按实际所见释义复核 |
| `GET /api/reviews/:attemptId` | review 别名 |
| `GET /api/impact/senses/:id` | 查义项影响到的例句、题、版本、作答 |
| `GET /api/events` | 编辑、发行、开卷、交卷事件流 |

## 设计说明：为什么旧选项号不会串到新版

选项编号 `A/B/C` 只是某一版、某一题冻结数组中的位置。系统另外使用全局稳定的 `optionId`，并在学生开卷时把学员所见题目/选项复制进 `attempt.snapshot`。

判卷和复核只读取：

```text
attempt -> attempt.snapshot / attempt.editionId -> 不可变 edition
```

绝不读取“当前最新 edition”或当前词典定义来解释旧答案。因此：

- 旧 A 与新 A 可以是完全不同的释义；
- 旧 `optionId` 出现在新 attempt 中会被拒绝；
- 拿着 `ATT_A` 请求 `EDITION_NEW/submit` 会得到 `EDITION_MISMATCH`；
- 不带新版路径的断线重试仍可在 `ATT_A` 快照内安全解析原来的 A；
- 相同 `clientSubmissionId` / `Idempotency-Key` 返回同一 review。
