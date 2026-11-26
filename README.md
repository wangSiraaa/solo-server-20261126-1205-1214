# 街道清扫合作社本机后台

零外部依赖的 Node.js 服务。街图用**有方向的小路段**表达；只有按某条有方向路段行驶、刷头落下并扫过其**右侧路缘**的记录，才会形成实扫覆盖。

## 启动与业务员入口

```bash
npm start
# 打开 http://127.0.0.1:3000
```

页面中可：

- 一键重演“原趟迟报先到 / 补扫报告先到”；
- 用投诉号 `TS-EXT`、`TS-LIFT` 查询哪几米由哪一躺、哪条实扫记录补齐；
- 查看一辆车 `CAR1`、三个路口 `I1/I2/I3`、四家店的示范街图。

命令行重演并模拟重新读取持久化文件：

```bash
npm run demo -- --order=late-first
npm run demo -- --order=makeup-first
npm test
```

数据默认原子写入 `data/coop.json`；可用 `DATA_FILE=/tmp/x.json npm start` 改位置。

## 核心建模

- **原路段 / 路缘**：`SEG_12` 等是原始路段；`CURB_12_R/L` 是两侧路缘，门面永远按原路缘米区间签约。
- **方向小路段**：`L12 = I1 -> I2` 的右缘是 `CURB_12_R`；反向 `L21 = I2 -> I1` 的右缘是另一条 `CURB_12_L`。因此反向空驶或反向刷扫不会给同一门面销账。
- **重新划段**：`POST /api/graph/resegment` 生成 `L31_S1/S2...`，新链接保留 `originalSegmentId + sourceFromM/sourceToM`。报告先映射回原路缘坐标再做区间并集，不会因为划段产生第二份合同权利。
- **当班快照**：`POST /api/shifts` 按当时合同生成不可删除的 `rights`。之后修改/恢复合同不回写老当班权利和已立投诉。
- **实扫依据**：`POST /api/trips/:id/sweeps` 必须给行驶方向链接、开刷米数、收刷米数、实际清扫分钟和服务台收到分钟。回场后仍可按原 `tripId` 迟报；记录仍归实际那一趟。
- **欠扫回答**：`POST /api/orders/:id/assess` 同时给出物理缺口、期限内缺口、是否超期、是否仍欠。
- **投诉**：投诉的米段主张不可变。只有当每一米都有适格的右向实扫覆盖时，系统才把投诉算成/标成 `resolved`；人工不能在证据不足时办妥。
- **关单**：物理米段补齐后可关单；若实扫晚于 `promisedEndAt`，状态为 `closed_late`，保留超期事实。期限内刷足则为 `closed_on_time`。
- **派班**：
  - 固定整圈：路线和 manifest 创建后冻结；新欠段不能塞回已离场车，`/api/dispatch/evaluate` 会判断有无另一台可用车。
  - 逐段要路：`POST /api/trips/:id/next-link` 只使用已回报的 `currentNodeId` / `arrivedViaLinkId`，按封路链接和路口 `uTurnAllowed` 做最短合法路径规划。`I2` 示范为禁止掉头。

## 北门验收剧本

示范数据里 `M_NORTH` 北门菜站在 `CURB_31_R [0,2)`：

1. `L31` 上 `brushDownM=0, brushUpM=2`：恰好两米，`closed_on_time`。
2. 合同改成 `[0,3)` 后开新当班：原趟 `[0,2)` 不能结清，投诉 `TS-EXT` 保留 `[2,3)` 这一份权利。
3. 合同恢复 `[0,2)` 后开新当班：刷头在第二米前抬起，报告 `[0,1)`，投诉 `TS-LIFT` 保留 `[1,2)`。
4. 一趟 `T_MAKEUP` 合并补 `[1,3)`；原趟迟报 `T_EXT [0,2)`、`T_LIFT [0,1)` 与补扫报告无论谁先到，两份投诉权利都不删除，最终米级来源均可见。
5. JSON 文件持久化；重启进程后 `GET /api/owner/complaints/TS-LIFT` 仍返回米级 `tripId/reportId/sweptAt/receivedAt`。

自动化测试覆盖上述剧本、反向不计、重新划段不重复、另一台车派补扫、禁止掉头和封路绕行：`npm test`。

## 常用 HTTP 接口

```bash
curl -s localhost:3000/api/map | jq
curl -s -X POST localhost:3000/api/rehearsal \
  -H 'content-type: application/json' \
  -d '{"arrivalOrder":"late-first"}'

curl -s localhost:3000/api/owner/complaints/TS-LIFT | jq

curl -s -X POST localhost:3000/api/shifts \
  -H 'content-type: application/json' \
  -d '{"at":10,"promisedEndAt":30,"merchantIds":["M_NORTH"]}'

curl -s -X POST localhost:3000/api/trips/fixed-loop \
  -H 'content-type: application/json' \
  -d '{"carId":"CAR1","shiftId":"SHIFT_ID","at":11,"routeLinkIds":["L31"]}'

curl -s -X POST localhost:3000/api/trips/TRIP_ID/sweeps \
  -H 'content-type: application/json' \
  -d '{"linkId":"L31","brushDownM":0,"brushUpM":2,"sweptAt":18,"receivedAt":40}'
```

其他接口见 `src/server.js`：合同改签、当班中扩合同追加权利（`/api/orders/:id/amend-contract`，缩合同不删老权利）、重新划段、投诉立案/办妥检查、补扫派单、回场、逐段要路、路径预览和全部状态读取。
