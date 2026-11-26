# 纸上风筝编队赛比赛台

无硬件、无第三方依赖。当前为标准库内存版比赛台，适合单场趣味赛和进程级重放；HTTP 服务重启后不保留赛程。实现包含：

- 有向场图：同一纸上绳道可建模为正、反两条弧；只有单向弧时反走会被拒绝。
- 裁判报点与旗语：旗正面/背面是两个不同记号，报记号必须同时有同节点同拍位置。
- 两个独立客户端：每位队员使用自己的令牌确认交棒。
- 交棒分归属：只记给在允许交接点、同拍、经双方分别确认的实际一对，不记给首次绕旗者个人。
- 原边折返消去：同一绳道正向走完后立刻反向折回，且中间没有交棒，则两条边和到达记号一起消去；确认交棒会隔断轨迹，不能跨棒互消。
- 预告场（briefing）：赛前发放各自线路、条件分支和约定交接点；封闭岔路在承诺时拒绝。
- 临场指挥场（live）：首位到可联络点后才生成邀请；接手人此前不能已在该点，防止倒填在场；坏分支当场退回（refund）。
- 幂等：HTTP `request_id` 按端点作用域保存响应；迟到单方“我到了”和跨进程重送都不会新增接头。

## 目录

- `kite_race/core.py`：规则引擎和状态机。
- `kite_race/server.py`：标准库 HTTP 服务。
- `kite_race/client.py`：客户端库和 CLI。
- `kite_race/scenarios.py`：题述场图、轨迹和岔路夹具。
- `tests/test_race.py`：规则与 HTTP 集成测试。
- `demo.py`：启动真实 HTTP 服务并以两个客户端跑完题述情形。

## 运行测试和演示

```bash
python3 -m unittest discover -s tests -v
python3 demo.py
```

启动服务：

```bash
python3 -m kite_race.server --host 127.0.0.1 --port 8080
```

## 主要 HTTP 接口

- `POST /races/create`
- `POST /races/{race_id}/positions`
- `POST /races/{race_id}/marks`
- `POST /races/{race_id}/briefing`
- `POST /races/{race_id}/handoff-request`
- `POST /races/{race_id}/invitations-accept`
- `POST /races/{race_id}/handoff-confirm`
- `POST /races/{race_id}/finalize`
- `GET /races/{race_id}/me`（带个人 `X-Auth-Token`）
- `GET /races/{race_id}/result`

所有 POST 均可带 `request_id`。同一端点作用域内重送相同 `request_id` 返回首次响应，不会重复建赛、重复报点或重复生成接头。

## 题述情形的判定

1. 甲：`S -> R(红正) -> X -> P`；乙在 `P` 等待，接手后 `P -> B(蓝背) -> F`。两人在 `P` 同拍分别确认，终场形成一个 `formation_segment`，`score_pair` 为 `["甲", "乙"]`。
2. 甲改为 `S -> R(红正) -> S -> Q -> P`：`r1` 正向后立即沿同一绳道反向，两条边消去，红正也消去；之后的无旗单向旁路不能补红正。即使交棒确认存在，也不记双人编队分，并给出拒分原因。
3. 乙接手当拍仍隔一条边：乙的裁判轨迹不在 `P`，其确认被拒绝；只有甲一侧不能形成双方确认，终场不产生交棒分。
4. 带岔路节目：预告场可在赛前验证多条路线，只有每条可选路线都沿存在的有向弧并能完成本侧旗语时才承诺；临场场在首位申请时校验，失败邀请标记为 `refunded`，成功邀请经乙接受后才锁定实际接手。
