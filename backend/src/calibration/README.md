# 试展跨场评分校准（calibration 模块）

不同试展场评委口径不一：有的场偏松、有的场偏严。本模块用**共有展品（锚点）**
校准各场评分，把间接相连的场次放进同一张榜，互不连通的场次分榜；当闭环偏差
超过受理时冻结的容差，隔离该组并指出冲突环路，其余组照常出榜。再通过逐一移除
锚点复算，把名次逆转或失去可比性的展品标为不稳。

仅依赖 Python 3.11 标准库（`http.server` + `sqlite3`，WLS 用手写高斯消元），
无需安装第三方依赖，与仓库内 FastAPI 骨架互不影响。

## 运行

```bash
cd backend
# Web 服务 + 页面（默认 127.0.0.1:18321，SQLite 在 ./calibration_data/app.db）
CALIBRATION_PORT=18321 python3 -m src.calibration.server
# 或
python3 -m src.calibration.cli serve --port 18321 --db ./calibration_data/app.db

# 命令行
python3 -m src.calibration.cli demo                 # 内置演示，打印完整 JSON
python3 -m src.calibration.cli run payload.json    # 自定义载荷

# 测试（24 个，标准库 unittest）
python3 -m unittest discover -s tests -v
```

## 校准口径

观测模型 `score = 展品真实水平 t + 场次偏差 b + 噪声`。共有展品在两场 a、b
同时展出时得到边差 `d_ab = mean(score_a − score_b)`，权重为该边上锚点数。
连通分量内以编号最小场次为引用场（b=0），加权最小二乘求解

```
min  Σ_edges w · (d_ab − (b_a − b_b))²
```

- **同榜/分榜**：场次是图节点，共有展品形成边；同一连通分量内间接相连也可同榜，
  不同分量之间没有共同口径，分别出榜。孤立场自动成为单点分量。
- **闭环隔离**：对生成树基本回路计算闭合偏差（含弦边贡献）。任一回路
  `|闭合偏差| > 容差` 即把**整个分量**隔离、不出榜，并返回冲突环路的节点路径、
  逐边符号/锚点依据；其余分量照常。等值（`== 容差`）不算超差。
- **榜单**：未隔离分量内，每件展品取各场 `原始分 − 场偏差` 的均值作为校准均分，
  降序排名（并列按展品 id），逐场依据随结果返回。
- **稳定性**：逐一移除锚点后完整重算。展品若分量被隔离或自身掉出连通榜，标
  `LOST_COMPARABILITY`；若与任一仍同榜展品的均分大小关系翻转，标
  `RANK_REVERSAL`。锚点复算次数受 `max_anchor_runs` 限制（默认 200），
  超出部分在结果中列出。

## 受理冻结与恢复

- 受理时校验并规范化载荷，**冻结评分、锚点集合与容差**，计算输入 SHA256 落库；
  调用方之后修改内存对象不影响研究。
- 流水线按 `INTAKE_FROZEN → GRAPH_BUILT → COMPONENTS_SOLVED →
  STABILITY_DONE → PUBLISHED` 落检查点。中断后重启（或调 `/resume`、
  `/api/resume-all`）从最后一个检查点沿用**同一份冻结输入**续算。
- 发布是一次性的：终态 `PUBLISHED` 后重复请求不会产生第二次发布事件。
- 受理支持 `Idempotency-Key`（或请求体 `idempotency_key`），重试命中即返回
  原研究，不重复计算、不重复发布。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/api/studies` | 受理研究（可带 `Idempotency-Key`；`"async": false` 同步跑完） |
| GET | `/api/studies` | 同批研究列表与状态 |
| GET | `/api/studies/{id}` | 状态、检查点、冻结 SHA256 |
| GET | `/api/studies/{id}/result` | 分量/榜单/冲突环路/稳定性/冻结输入/审计事件 |
| GET | `/api/studies/{id}/events` | 审计事件流 |
| POST | `/api/studies/{id}/resume` | 单研究中断恢复 |
| POST | `/api/studies/{id}/publish` | 显式发布（未完成则拒绝） |
| POST | `/api/resume-all` | 启动/手动恢复全部未发布研究 |
| GET | `/api/demo-payload` | 内置演示载荷 |

载荷字段：`sessions[{id,name}]`、`observations[{session_id,exhibit_id,score}]`、
`tolerance`（非负数）、可选 `anchor_exhibit_ids`（省略则自动取所有跨场共有
展品）、可选 `max_anchor_runs`。非法输入返回 422 并说明原因。

## 内置演示的五个连通组

1. **A 组（S-A1/A2/A3）**：三个三场共有锚点 + 两个两场锚点，含闭合良好的环，
   WLS 精确还原偏差 0 / +0.8 / −0.6，正常出榜。
2. **B 组（S-B1/B2/B3）**：三条边各用独立锚点，三角闭合偏差 1.0 > 容差 0.5，
   整组隔离并报告冲突环路。
3. **C 组（S-C1/C2）**：仅靠唯一桥锚点相连；移除它两场断连，组内展品全部标
   `LOST_COMPARABILITY`。
4. **D 组（S-D1/S-D2）**：两个口径相反的锚点；移除强锚点后偏差估计反转，
   `D-LOCAL-E1/E2` 发生 `RANK_REVERSAL`。
5. **S-SOLO**：无共有展品的孤立场，独立成榜。

## 目录

```text
backend/src/calibration/
  engine.py   校验、锚点图、WLS、连通分榜、闭环隔离、留一稳定性
  store.py    SQLite：冻结输入、检查点、事件、幂等键、故障注入开关
  service.py  受理冻结、检查点流水线、恢复、一次性发布
  server.py   标准库 HTTP 服务与静态页面托管、启动恢复
  demo.py     固定演示数据（确定性）
  cli.py      命令行入口
  static/     index.html / styles.css / app.js（无构建步骤）
backend/tests/test_engine.py test_service.py test_http.py
```
