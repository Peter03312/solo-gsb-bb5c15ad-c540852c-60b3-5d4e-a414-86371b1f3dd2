# 路跑分段计时裁决服务

Python 3.12 + FastAPI + PostgreSQL 实现的路跑分段计时裁决（adjudication）服务。
支持赛事配置、乱序扫描上报、幂等/冲突判定、分段区间裁决、最早完赛路径选择、
撤销重算与封存快照，全部通过 HTTP API 调用。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与业务编排
│   ├── timing.py      # 纯函数裁决算法（含字典序平局规则），可独立单测
│   ├── db.py          # PostgreSQL 连接
│   └── schemas.py     # 请求/响应模型
├── initdb/
│   └── 01_schema.sql  # 数据库初始化脚本（Postgres 容器首启自动执行）
├── examples/
│   └── demo.py        # 端到端调用示例（仅标准库）
├── tests/
│   └── test_timing.py # 裁决算法单元测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 一键启动

需要 Docker 与 Docker Compose（v2）。

```bash
docker compose up --build
```

该命令会：

1. 启动 PostgreSQL 16（`db` 服务，带健康检查），数据卷为空时自动执行
   `initdb/01_schema.sql` 完成建表；
2. 构建并启动 API（`api` 服务），启动时等待数据库就绪。

启动后：

| 用途 | 地址 |
| --- | --- |
| API Base URL | `http://localhost:8000/api/v1` |
| Swagger 交互文档 | `http://localhost:8000/docs` |
| OpenAPI 描述 | `http://localhost:8000/openapi.json` |
| 健康检查 | `http://localhost:8000/healthz` |

停止并清理（含数据卷）：

```bash
docker compose down -v
```

### 不用 Docker 本地运行（可选）

```bash
pip install -r requirements.txt
# 让一个空 PostgreSQL 执行 initdb/01_schema.sql，然后：
export DATABASE_URL="postgresql://timer:timer@127.0.0.1:5432/race_timer"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 配置

通过环境变量配置（见 `docker-compose.yml`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://timer:timer@db:5432/race_timer` | PostgreSQL DSN |

数据库容器的账号、密码、库名同样在 `docker-compose.yml` 中设置
（`POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB`），与 `DATABASE_URL` 对应。
对外暴露端口：API `8000`、PostgreSQL `5432`。

## 领域规则

- **赛事配置（创建后固定）**：录入起跑时间、成绩截止时间、有序检查点（最后一个
  为终点）以及每段（起跑→首点、点→点）的非负耗时闭区间 `[lower, upper]`（秒），
  并绑定选手芯片。截止必须晚于起跑；点码不能重复；芯片码全局唯一；
  各段下限之和使最早完赛都超过截止的配置会被拒收。
- **扫描上报**：扫描包含唯一编号、芯片码、点位码、发生时间，可乱序提交
  （裁决时统一按发生时间处理）。
  - 同编号 + 同芯片 + 同点位 + 同发生时间重传：**幂等**，HTTP 200 返回原记录；
  - 同编号但内容不同：**冲突**，HTTP 409 拒收；
  - 未知芯片或未知点位：HTTP 400 拒收。
- **裁决**：对每位选手，在其全部有效扫描中选择一条
  `起跑 → CP1 → CP2 → … → 终点` 的路径，要求：
  1. 各点扫描发生时间**严格递增**；
  2. 每段耗时落在对应闭区间内；
  3. 终点扫描**不晚于**截止时间。

  在所有合法路径中，选择**终点发生时间最早**的；完赛时间相同则取所用
  **扫描编号序列字典序最小**的（按序列逐元素比较）。不存在合法完整路径记为
  **未完赛（DNF）**。
- **撤销与封存**：封存前撤销扫描，暂定成绩在下次查询时实时重算；封存会对全体
  选手固化一份一致的成绩与所用扫描快照。封存后拒绝任何扫描提交或撤销
 （HTTP 409），成绩查询永远返回封存时的快照。封存接口本身幂等。

## API 一览

Base：`/api/v1`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/races` | 创建赛事（201） |
| GET | `/races/{race_id}` | 查询赛事 |
| POST | `/races/{race_id}/seals` | 封存赛事，返回全部选手快照 |
| POST | `/scans` | 提交扫描（201 新增 / 200 幂等命中 / 400 拒收 / 409 冲突或已封存） |
| DELETE | `/scans/{scan_code}?chip_code=..` 或 `?race_id=..` | 撤销扫描 |
| GET | `/races/{race_id}/results` | 查询成绩（封存前暂定，封存后快照） |

所有时间均使用 ISO-8601（建议带时区，如 `2026-09-30T08:00:00+08:00`；
不带时区按 UTC 解释），内部统一存 `TIMESTAMPTZ`。

### curl 示例

```bash
# 1. 创建赛事
curl -s -X POST http://localhost:8000/api/v1/races \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "2026 示例十公里",
    "start_time": "2026-09-30T08:00:00+08:00",
    "cutoff_time": "2026-09-30T09:00:00+08:00",
    "points": [
      {"code": "CP1", "name": "检查点1", "lower_seconds": 300, "upper_seconds": 900},
      {"code": "CP2", "name": "检查点2", "lower_seconds": 300, "upper_seconds": 900},
      {"code": "FIN", "name": "终点",    "lower_seconds": 300, "upper_seconds": 900}
    ],
    "chips": [
      {"bib": "A001", "name": "选手甲", "chip_code": "CHIP-A"},
      {"bib": "B001", "name": "选手乙", "chip_code": "CHIP-B"}
    ]
  }'

# 2. 提交扫描（可乱序）
curl -s -X POST http://localhost:8000/api/v1/scans \
  -H 'Content-Type: application/json' \
  -d '{"scan_id":"S-1","chip_code":"CHIP-A","point_code":"CP1",
       "occurred_at":"2026-09-30T08:05:00+08:00"}'

# 3. 查询暂定成绩
curl -s http://localhost:8000/api/v1/races/<RACE_ID>/results

# 4. 撤销扫描
curl -s -X DELETE 'http://localhost:8000/api/v1/scans/S-1?chip_code=CHIP-A'

# 5. 封存
curl -s -X POST http://localhost:8000/api/v1/races/<RACE_ID>/seals
```

### 完整调用示例

`examples/demo.py` 演示完整生命周期：创建赛事 → 乱序提交 → 幂等/冲突/拒收
→ 同成绩字典序决胜 → 撤销后重算 → 封存 → 封存后拒绝写入。

```bash
docker compose up --build -d
python3 examples/demo.py            # 默认 http://127.0.0.1:8000
```

成绩响应示例（节选）：

```json
{
  "race_id": "...",
  "sealed": false,
  "results": [
    {
      "bib": "A001", "name": "选手甲", "chip_code": "CHIP-A",
      "finished": true,
      "finish_time": "2026-09-30T00:15:00Z",
      "total_seconds": 900.0,
      "scans": [ {"scan_id": "S-A1-CP1", "point_code": "CP1", "...": "..." } ]
    },
    { "bib": "B001", "finished": false, "finish_time": null, "scans": [] }
  ]
}
```

## 测试

裁决算法不依赖数据库与 Web 框架：

```bash
python3 tests/test_timing.py
# 或
pytest tests/
```

覆盖：基本路径、乱序提交、最早完赛优先、同成绩字典序最小（含中后段编号
决胜）、严格递增、上下限闭区间（含边界浮点容差）、截止边界、贪心死路反例
（DP 保留所有非支配前缀）、缺未完赛等。

## 设计说明

- 裁决是一个按检查点推进的 DAG 动态规划：状态以“到达某个扫描”为键。
  到达同一扫描的不同前缀，当前时间相同、后续可行性与完赛成绩只取决于时间，
  故每个扫描只保留编号序列字典序最小的前缀；终点阶段再按
  “完赛时间最早、序列字典序最小”择优。这样可以正确处理“较早的前缀反而是
  死路”的非贪心情形。
- 扫描与封存的并发安全通过对 `races` 行加 `FOR UPDATE` 行锁串行化；
  扫描活动编号由数据库部分唯一索引兜底。
