# 社办周报勘误后台

编辑、印厂、投递站、发行部共用一套 HTTP 后台（仅依赖 Python 标准库）。
所有状态落盘到单个 SQLite 文件，**重开进程后站员仍可凭包号追查投递与补正去向**。

## 启动与验证

```bash
python3 server.py --port 8000 --db paper.db     # 启动服务
python3 verify.py                               # 六户端到端验证（44 项断言）
```

`verify.py` 会自动拉起/关闭服务，使用独立临时库，不污染工作目录。

## 领域模型（关键身份与不变量）

| 概念 | 表 | 说明 |
| --- | --- | --- |
| 地方版 | `edition` / `page` | 集市、学校两版；版面分**固定旧稿型**（`fixed_ms_id` 常驻）与**跟随校样型** |
| 消息/稿件 | `message` / `manuscript` | 同一条消息可有多版短文，以不同稿号区分身份 |
| 落版 | `placement` | 编辑决定某消息以某短文登上某版面，一条消息可登若干版面 |
| 本期校样 | `issue_proof` | 跟随型版面在当期取哪篇稿 |
| 印次 | `print_run` / `run_page` | **开印即把每一页实际用稿身份钉死**；固定版面取固定稿，跟随版面取本期校样 |
| 包 | `bundle` | 隶属某印次某版，状态 stock→sealed→delivered，可 rerouted 回库存 |
| 回单 | `receipt` / `receipt_item` | 页面稿号只从 `run_page` 读取，不接受外部传入；同回单重发幂等 |
| 更正 | `correction` | 只存两稿之间的**字段级 diff**，不重发整篇 |
| 纸条 | `slip` / `slip_correction` | 按户合并多条更正；状态 prepared/dispatched/signed/misdirected |
| 车次 | `trip` | 出车时把清单 JSON 快照冻结，车走后不可改 |

### 欠件判定（全部从真实印次与签收回执计算）

一户对某条更正“欠着”，当且仅当：

1. 回单证明它签读过该消息**目标稿之前的任一稿**（链式更正：读 MS1 的人对
   MS2→MS3 同样欠，因为他既没见过 MS2 也没见过 MS3）；
2. 它没有任何一张**已签收（signed）**且覆盖该更正的纸条。

因此三类情况都**不触发**补正：未投的库存包、只订了这份报却没签收相关页的住户、
只在另一地方版出现的文字。按户去重，同一户签十次旧稿也只欠一笔。

纸条文本只渲染 diff（如 `date：十二日 → 十三日`），不会向“从未见过旧消息”的人
泄出整篇消息。

## 两种发行业务（发行部只开其中一种）

### 专递勘误 `special`
- `POST /special/prepare`：把各户尚未覆盖的更正**按户合并**成纸条；
- `POST /special/trips`：车开走，清单冻结、纸条转 dispatched；
- 晚到的签收回单只结清车上**实际带的**纸条；已签收/在途的更正不会再被备条。

### 随刊夹送 `insert`
- `POST /insert/seal`：下一包面向该户封包时配条；
- **封好没送到不销账**；拆包改投（`/insert/reroute`）原条误投、欠件保留、包回库存；
- 退订（`/households/{id}/unsubscribe`）不销已有欠账；
- `GET /insert/overdue?today=YYYY-MM-DD`：超承诺日且无在途条、无有效封包者列入逾期，
  误投条的承诺日不计入。

## 主要 HTTP 接口

```
# 基础资料
POST /editions  /households  /subscriptions
POST /households/{hid}/unsubscribe

# 编辑
POST /messages /manuscripts /pages /placements /issues /proofs /corrections
GET  /corrections/{cid}                 # 这处更正还有谁没收到

# 印厂
POST /print-runs                        # 开印钉稿，返回每页冻结稿号
POST /bundles                           # 按印次+版面生成包
GET  /bundles/{bid}                     # 包号追查：钉稿/回单/纸条去向

# 投递站
POST /receipts                          # 签收（page 项 / slip 项），同回单重发幂等
POST /receipts/{rid}/delete             # 撤单（用于改投/修正印次），回滚销账
GET  /receipts/{rid}                    # 回单追到具体页与原稿
GET  /households/{hid}/trace            # 这户读过哪些稿号、收过哪些条、还欠什么

# 发行部
POST /distribution/mode                 # special | insert
GET  /distribution/mode
GET  /owed                              # 全局欠件名单
POST /special/prepare  /special/trips
POST /insert/seal  /insert/reroute
GET  /insert/overdue
POST /slips/{sid}/cancel                # 撤销未签收条（如专递转随刊），欠件保留
```

## 验证脚本覆盖的题面硬性场景

- 两批印刷：一批在更正前、一批在更正后；固定栏在新印次里仍是旧稿，新读者照样欠；
- 只改变一户实际签收的印次（撤单+同回单号重发），欠件名单随之变化；
- 固定印次，只把纸条从 prepared 推到 signed，欠件随之改变；
- 同包拆投（B1 拆两户）、两包混送（一户签两个印次的包）、同一回单重发幂等；
- 链式更正按户合并为一条、纸条只带 diff；
- 出车冻结、晚回签收只结清本户、封而不达不销账、改投误投、退订不销账、逾期列户；
- SIGTERM 后重开进程，凭包号完整查回投递与补正，发行模式选择持久化。

## 标题之问

> 周报修正了集市日期，究竟哪一包报纸还欠读者一张勘误？

系统不按“包”欠勘误，而是按**真实签收回单上的住户**欠：包只是印次的物理载体，
未投出的库存包不欠任何人；真正欠勘误的，是回单证明读过旧稿、且尚未签收覆盖该
更正纸条的住户。`GET /corrections/{cid}` 与 `GET /owed` 给出的就是这份名单，
`GET /bundles/{bid}` 则能反查任一包的钉稿、签收与补正去向。
