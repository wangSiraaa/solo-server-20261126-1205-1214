# 直播来宾徽章本机服务

给直播社群用的本机（localhost）服务：录入主题、排期、发牌、开播、留档。
不涉及任何像素——图形与色块一律用**名字**表示。

## 一句话制度

| 关注点 | 采用的制度 |
| --- | --- |
| 主题 / 季 | **每季冻结一份主题快照**。改主题就开新一季；旧季已发牌与发言按旧约定永久可查。 |
| 整季固定牌 | 一季内 `guest -> style` 签约后不可更换（“整季固定牌”），任意场次复用。 |
| 现场借牌 | 按**当前在场集合**判定；退场（`leave`）后牌可借他人。 |
| 发言署名 | 发言号首次写定即冻结；借牌、重发回单都不会改写已公开截图上的署名。 |
| 同屏可辨 | 可能同屏的任意两人，必须在**所有受支持语境**（房间 / 字幕条 / 弹幕浮层 × 普通 / 大字）都能区分。 |
| 并屏 / 同场 | 先按现状判；不行再在允许牌池内**改借**可辨牌；仍不行才拒绝该次并屏——但**绝不“让所有人停播”**，两个码流继续各走各的。 |

## 核心概念

- **样式 style**：`shape`（圆/方/三角…）+ `edge` / `fill` 两个颜色引用。
- **颜色引用 ColorRef**：`explicit(code)` 或 `alias(style, part)` —— 别名“借”另一款样式的边色或填色（可检测环）。
- **覆写 surface**：房间 / 字幕条 / 弹幕浮层可分别覆写某样式在某模式下的边色或填色，可按 `(mode, style)` 精准覆写，也可整模式默认。
- **模式 mode**：普通 / 大字。每个模式一张**色号 → 实际色块名**的映射表（简化显示的两张表）。
- **语境 context**：`(surface, mode)` 组合，即“同一徽章在两个浮层可能完全不同”的具体落点。`Theme.contexts` 列出所有受支持语境。
- **可区分**：两种样式在**每一个** context 的渲染三元组 `(shape, edge_block, fill_block)` 都不同，才算可区分；只要有一个语境相同就不可区分。

## 目录

```
badges/
  model.py     # 主题/样式/覆写/模式/别名解析/渲染/可区分判定 + 两套内置主题
  planning.py  # 同屏图（session clique + 联播并集）、完整回溯求解器
  service.py   # 季快照、签约、开播、回单、发言冻结、现场借牌/退场/并屏
  live.py      # 假直播：把某次排布渲染成可回放的 JSON 画面
  http_api.py  # 纯函数请求处理 handle(service, method, path, body)
  server.py    # 真 socket 服务 + Recorder（录制调用）/ Replayer（回放校验）
  cli.py       # serve / demo / replay 三个子命令
  replay.py    # 回放命令行
demo/run_demo.py   # 微型剧情：分场→并屏→退场重发→改主题→借牌→第三人来宾
fixtures/          # 主题别名样例、排期样例
tests/             # 29 个单元 / 集成 / 真 socket 测试
```

## 运行

```bash
python3 -m badges.cli demo            # 跑微型剧情，生成 demo/calls.jsonl 并自我回放校验
python3 -m badges.replay demo/calls.jsonl   # 单独回放调用日志（逐条比对 status+response）
python3 -m badges.cli serve --port 8000     # 起本机 HTTP 服务
python3 -m unittest discover -s tests -v    # 全部测试
```

## HTTP 接口（全部 JSON）

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/seasons` | `{theme_kind: default|collapsed, season_id?}` 或 `{theme: <theme json>}` 开新季 |
| GET | `/seasons` | 列出各季与当前季 |
| GET | `/seasons/{id}` | 该季快照（签约/回单/发言/在场/事件） |
| POST | `/seasons/{id}/theme` | 仅在该季尚无记录时改主题 |
| POST | `/seasons/{id}/plan` | 传入 `sessions`/`allowances`/`simulcasts` 求可行排布 |
| POST | `/seasons/{id}/sign` | `{guest, style, sessions?}` 整季签约 |
| POST | `/seasons/{id}/check-pair` | `{a,b}` 判定两种样式是否处处可区分 |
| POST | `/seasons/{id}/sessions/start` | `{session_id, guests, speech_numbers?}` 开播并冻结回单/发言 |
| POST | `/seasons/{id}/receipts` | 重发登场回单 |
| POST | `/seasons/{id}/speeches` | `{guest|receipt_id, style?, speech_no?, session_id}` 记录发言（号码冻结） |
| GET | `/seasons/{id}/speeches/{n}` | 查该季发言 n 的署名 |
| GET | `/speeches/{n}` | 跨季查发言 n（歧义需指定季） |
| POST | `/seasons/{id}/live/enter` | 现场到场借牌（按在场集合判可辨） |
| POST | `/seasons/{id}/live/leave` | 确认退场，释放牌 |
| POST | `/seasons/{id}/live/assign` | 按牌池给在场者分派 |
| POST | `/seasons/{id}/live/merge` | 并屏：先现状、再改借、仍不行才拒 |
| GET | `/events` | 所有季的事件流（调用可回放） |

`server.Recorder` 会把每次调用写成 `{method,path,body,status,response}` 的 JSONL；
`Replayer` 用全新 `Service` 逐条重放并要求结果逐字节一致，证明服务确定且日志自足。

## 两套内置主题

- `default_theme()` “可辨主题”：`round_dark` / `round_light` 在普通与大字、三个覆层**处处**可分；
  另有 `square_dark` / `triangle_light` 提供额外**形状**，求解器在颜色不够时会自动用上。
- `collapsed_theme()` “大字字幕塌缩主题”：**仅**把字幕条在大字模式下的边色别名指向 `round_dark` 的深边，
  于是 `round_dark` 与 `round_light` 只在 `字幕条@大字` 这一语境重合——其余语境仍可分，但“处处可分”已被破坏，
  所以这对牌不能再签给同时登场的人。

## 剧情对应（demo/run_demo.py）

1. **S1**：`阿青/阿白` 拿圆边深/浅两牌，普通+大字都能分开 → 先后各开一场（`E1a`/`E1b`），再请求**并屏**成功。
2. `阿青` 退场 → 深边牌空出 → **重发登场回单**给借牌的 `阿紫`；
   此前已公开的 `发言号 1` 仍署名 `阿青`，`发言号 3` 署名 `阿紫`。
3. **S2**（新一季，大字字幕边色别名改为同色）：圆边对同场被拒；但一组**有方边牌可借**的新来宾仍能同场——
   充分利用可区分图案，绝不全停。
4. **S3**（新一季，主题恢复可辨）：唯一的深边牌已合法预先签给仍在场的**第三人**；
   前两人只剩两块浅边牌，于是其并屏被拒——服务仍旧不搞全停。

`render_screen` 把每次开播的排布渲染成 `overlays`（逐覆层/逐模式）与 `subtitle`（字幕条），
并附 `all_separable` 布尔位，便于“观众”回放核对。
