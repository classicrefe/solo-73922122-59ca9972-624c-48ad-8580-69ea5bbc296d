# 骑马钉返印 API

印房返印骑马钉小册时复用旧印张的排程服务。提交新版页序和旧印张，服务自动拼版、
择优分配旧纸、生成返印单，并支持派工确认完工与报废重排。

- FastAPI + SQLite（标准库 `sqlite3`，JSON 存嵌套数据）
- 纯 Python 精确求解分配（位掩码 DP），无外部求解器

## 业务规则

**拼版**：新版页序在末尾补白凑足 4 的倍数，再按骑马钉从外到内拼版。
第 `i` 张（`i` 从 0 开始，0 为最外，页码从 1 开始，共 `N` 页）：

| | 左页 | 右页 |
|---|---|---|
| 正面（折后外侧） | `N-2i` | `2i+1` |
| 背面 | `2i+2` | `N-2i-1` |

**旧纸复用**：旧纸按原朝向使用（正反面、左右不可对调），每张至多一次；
已印面的左右页须与该位置新版拼版完全一致，未印面不限、按新版内容补印；
纯空白面免印。

**优化目标**：先尽量少用新纸（尽量多复用）；新纸用量相同时，选待印面总数
最少的方案。重排时在目标值相同的情况下尽量不动原安排。

**派工与完工**：每个位置签发一个派工号；完工凭该位置当前有效派工确认
（重复确认幂等）。报废未完工的旧纸后自动重排：已完工的纸留在原位；被改派
位置的旧派工退回（再凭它确认返回 409），并签发新派工；未改派位置保留原
派工，仍可按原安排确认。

**并发与生效时机**：报废请求在数据库锁之外执行择纸计算，因此同一张单和
其他单的 `GET` 查单、完工确认在「排程还在算」时也能立即完成。

- 查单只显示**已生效（已提交）**的状态：报废意向在最终原子提交前不落库，
  计算期间查到的仍是上一个生效版本。
- 本批拟报废的印张若在报废生效前，已被凭该位置的有效派工号确认完工，
  整批报废返回 `409`：不登记任何报废、不改派（批内任意一张触发即整批拒绝）。
- 若先完工的是**其他**印张：这些完工位置留在原位，报废在生效时按最新状态
  重新为剩余位置择纸（完工纸移出纸池）。
- 并发报废同一工单时按工单排队、各自以**生效时**的最新状态判断：重叠报废
  只有一个成功，后来者按「已报废」返回 `409`；不重叠的批次可先后生效。
  `scraps` 登记与 `positions`/`dispatches` 变动在同一个 SQLite 事务内提交，
  一并生效。接口、择纸目标与派工规则均未改变。

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# API: http://localhost:8000  文档: http://localhost:8000/docs
```

SQLite 数据落在命名卷 `reprint-data`（容器内 `/data/app.db`）。

要稳定复现「重排计算进行中」的并发窗口，可给报废重排加一个可选延迟
`REPLAN_DELAY_SECONDS`（默认 `0`，仅用于演示/压测，不影响择纸规则）：

```bash
REPLAN_DELAY_SECONDS=2 docker compose up --build
# 等价地：
docker compose -f docker-compose.yml -f docker-compose.demo.yml up --build
```

### 本地开发

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload        # 数据在 ./data/app.db（可用 DB_PATH 覆盖）
pytest                               # 运行测试
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/work-orders` | 提交新版页序与旧印张，生成返印单（201） |
| `GET` | `/work-orders` | 工单列表（含汇总） |
| `GET` | `/work-orders/{oid}` | 工单当前状态 / 返印单 |
| `POST` | `/work-orders/{oid}/positions/{i}/confirm` | 凭派工号确认位置 `i` 完工 |
| `POST` | `/work-orders/{oid}/scrap` | 报废旧印张并重排 |

### 创建工单

```bash
curl -X POST http://localhost:8000/work-orders \
  -H 'Content-Type: application/json' -d '{
  "pages": ["COVER", "P2", "P3", "P4", "P5"],
  "old_sheets": [
    {"id": "S1", "front": {"left": null, "right": "COVER"}, "back": null},
    {"id": "S2", "front": null, "back": {"left": "P4", "right": "P5"}},
    {"id": "S3", "front": {"left": "OLD", "right": "STUFF"}, "back": null}
  ]}'
```

- `pages`：1–48 项，内容编号可重复；`null` 表示空白页；数字编号会规整为字符串。
- `old_sheets`：至多 40 张；`front`/`back` 为折后外侧正面与背面，`null` 表示该面未印。

响应（节选）：`positions` 从外到内列出每个位置——`use` 为旧纸编号或 `NEW`
（领新纸），`faces_to_print` 为还要印的面，`dispatch_id` 为该位置派工号；
`summary` 给出新纸用量与待印面数。

### 确认完工

```bash
curl -X POST http://localhost:8000/work-orders/$OID/positions/0/confirm \
  -H 'Content-Type: application/json' -d '{"dispatch_id": "<位置0的dispatch_id>"}'
```

### 报废重排

```bash
curl -X POST http://localhost:8000/work-orders/$OID/scrap \
  -H 'Content-Type: application/json' -d '{"sheet_ids": ["S2"]}'
```

响应中的 `returned_dispatches` 列出被改派位置及其已退回的旧派工号。

### 错误码

`404` 工单/位置/派工/旧印张不存在；`409` 派工不属于该位置、旧派工已退回、
报废已完工或已报废的旧纸（含重排计算期间拟报废纸被先确认完工的竞争情形，
此时整批拒绝、不报废不改派）；`422` 请求体校验失败（页数/张数超限、编号重复等）。

## 演示

启动服务后执行（需本机有 `python3` 解析 JSON）：

```bash
./demo.sh                  # 或 ./demo.sh http://localhost:8000
```

演示完整流程：建单 → 位置 0 派工确认 → 报废 S2 重排 → 旧派工确认被退回（409）
→ 新派工确认 → 纯空白面免印示例。

### 并发演示（重排期间查单 / 确认）

先以 2 秒重排延迟启动（见上），然后：

```bash
./demo-concurrent.sh        # 或 ./demo-concurrent.sh http://localhost:8000
```

脚本会实测三类操作并打印状态码与耗时：A 拟报废印张先完工 → 整批 `409`
且无副作用；B 其他印张先完工 → 留原位、剩余位置按最新态重排；C 相同批
并发报废恰一个 `200`、其余 `409`，不同批并发均生效。

也可手动发请求（需 `python3` 提取字段）。下面整段一次性粘贴即可；
若要逐条手敲，建议把服务端延迟调大（如 `REPLAN_DELAY_SECONDS=15`），
并在提交报废后尽快执行第 3、4 步：

```bash
BASE=http://localhost:8000

# 1) 建单，记下 OID 与位置 1（S2）的派工号 D1
RESP=$(curl -sS -X POST $BASE/work-orders -H 'Content-Type: application/json' -d '{
  "pages": ["COVER","P2","P3","P4","P5"],
  "old_sheets": [
    {"id":"S1","front":{"left":null,"right":"COVER"},"back":null},
    {"id":"S2","front":null,"back":{"left":"P4","right":"P5"}}
  ]}')
OID=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['work_order_id'])")
D1=$( echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['positions'][1]['dispatch_id'])")

# 2) 后台提交整批报废（排程开始计算，约 2s）
curl -sS -o /tmp/scrap.json -w '%{http_code}\n' \
  -X POST $BASE/work-orders/$OID/scrap \
  -H 'Content-Type: application/json' -d '{"sheet_ids":["S2"]}' > /tmp/scrap.code &

# 3) 计算结束前：查同单（仍只显示已生效的旧状态）、查列表/他单，都即时 200
curl -sS $BASE/work-orders/$OID
curl -sS $BASE/work-orders

# 4) 拟报废的 S2 位置先凭有效派工号确认完工 -> 200
curl -sS -X POST $BASE/work-orders/$OID/positions/1/confirm \
  -H 'Content-Type: application/json' -d "{\"dispatch_id\":\"$D1\"}"

# 5) 等后台 scrap：整批返回 409；再查单确认无报废、无改派
wait; cat /tmp/scrap.code          # 409
curl -sS $BASE/work-orders/$OID    # scrapped_sheets 仍为 []，pos1 completed、派工号不变
```

把第 4 步换成确认**位置 0**（S1，不在报废批内），scrap 会返回 `200`：
位置 0 以原派工保持完工，位置 1 改领新纸并签发新派工，旧 `D1` 再确认返回 `409`。

## 结构

```
app/
  imposition.py   补白 + 骑马钉拼版
  planner.py      旧纸分配优化（位掩码 DP：先最少新纸，再最少待印面）
  service.py      建单 / 确认 / 报废重排业务逻辑（锁外择纸 + 生效时复核 + 原子提交）
  db.py           SQLite 连接与建表
  schemas.py      请求校验（Pydantic）
  main.py         FastAPI 路由
tests/            pytest（拼版、优化器、端到端 API、并发报废/确认）
demo.sh           原有完整流程演示
demo-concurrent.sh  重排期间查单/确认与并发报废演示
docker-compose.demo.yml  并发演示用的 2s 重排延迟覆盖文件
```

## 限制

- 单进程 SQLite，适合单店/单机等场景；未做鉴权。
- 内容编号按字符串精确匹配（数字会规整为字符串，如 `1` 与 `"1"` 视为同一编号）。
