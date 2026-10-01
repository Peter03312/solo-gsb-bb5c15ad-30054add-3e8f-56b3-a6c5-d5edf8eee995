# 供水管网故障隔离与复供服务

基于 **Python 3.12 + FastAPI + PostgreSQL** 的供水管网故障隔离与复供服务。

支持录入带编号的**水源(source)**、**用水点(sink)**、**连接点(junction)**及两端节点明确的**管段**（管段可设阀门）；
操作员只知道**故障管段编号**时，可请求**关阀建议**：按当前管网只读计算一套可直接提交的关阀方案；
针对一条故障管段，预演并提交阀门关闭方案，自动判断**能否隔离**并计算**失供用水点**；
隔离提交后生成带编号的隔离事件（区分**待修复 open / 已复供 restored**），
修复完成后操作员按**事件编号 + 当前管网版本**预演并确认**复供**，系统复核重开阀门后其余待修复故障是否仍被安全隔离。

管网运行中**水源可能停运或恢复**：操作员按已读版本指定一批水源编号及各自目标运行状态，
先经**只读预演**查看切换后新增失供/恢复供水的用水点与待修复故障的重新进水风险，
再在同一事务内**原子确认**水源状态与版本。

---

## 一、快速启动（Docker Compose 一键启动）

前置条件：安装 Docker 与 Docker Compose 插件。

```bash
docker compose up --build
```

该命令会：

1. 启动 PostgreSQL 16（端口 `5432`），并通过 `healthcheck` 等待数据库就绪；
2. 构建并启动 API 服务（端口 `8000`）；
3. API 启动时自动执行 `CREATE TABLE IF NOT EXISTS ...` 完成数据库初始化（无需手工建表）。

停止：

```bash
docker compose down          # 停止并删除容器（保留数据卷）
docker compose down -v       # 同时清空数据库数据卷
```

## 二、服务地址与端口

| 服务 | 地址 |
| --- | --- |
| API 基础地址 | <http://localhost:8000> |
| 健康检查 | <http://localhost:8000/health> |
| Swagger 交互文档 | <http://localhost:8000/docs> |
| ReDoc 文档 | <http://localhost:8000/redoc> |
| OpenAPI Schema | <http://localhost:8000/openapi.json> |
| PostgreSQL | `localhost:5432`（库/用户/密码默认均为 `water`） |

## 三、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://water:water@localhost:5432/water` | PostgreSQL 连接串；Compose 中指向 `db` 服务 |
| `DB_CONNECT_RETRIES` | `30` | 启动时等待数据库就绪的重试次数 |
| `DB_CONNECT_RETRY_INTERVAL` | `1.0` | 重试间隔秒数 |
| `HISTORY_LIMIT` | `100` | 历史记录接口返回上限 |

### 本地（非 Docker）运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 确保 PostgreSQL 已建库（create database water;）
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## 四、核心业务规则

**导入校验**（任一不满足即整体拒绝，返回全部问题明细，不写入数据）：

- 管段两端节点必须存在 —— 拒绝**悬空端点**；
- 节点编号、管段编号分别不可重复 —— 拒绝**重号**；
- 管段两端不可为同一节点 —— 拒绝**自环**；
- 没有阀门的管段不能标记为已关闭。

**隔离判定**（方案图 = 移除故障管段 + 关闭拟关阀门，**已关闭阀门继续生效**）：

- 仅当故障管段**两端**在方案图中都**无法从任一运行中水源到达**，方案才 `isolatable=true`，方可提交；
- 失供用水点 = **方案前**由任一运行中水源可达、**方案后**不可达的用水点，按编号（字符串字典序）排序；
  方案前已经失供的点不会重复计入。
- 其他**待修复(open)**隔离事件的故障管段在方案前/方案后均视为不可通行（故障仍在，不能通水）；
  已复供(restored)事件的故障管段不再计入。

**提交约束**（任一不满足即拒绝且不改任何状态）：

- `version` 必须等于当前管网版本，否则 `409 VERSION_CONFLICT`（版本过期）；
- 故障管段、拟关管段必须存在；拟关管段必须**带阀门**；
- 拟关列表不得重复、不得包含故障管段本身；已关闭的阀门不得重复关闭；
- 方案必须可隔离（`isolatable=true`），否则 `422 NOT_ISOLATABLE`；
- 提交在**单个数据库事务**内完成：行级锁校验版本 → 更新阀位 → 版本号 +1 → 写隔离事件（状态 `open`），原子生效。
- 整网导入为替换式：重写全部节点/管段、清空历史、版本重置为 `1`（旧事件随之失效，全部水源重置为运行）。

**关阀建议**（`POST /api/isolation/recommend`，**只读**，不改阀位/版本/事件）：

- 入参只需 `fault_pipe_id` 与已读 `version`；返回当前版本、`version_match`、
  拟关闭的尚未关闭阀门（`recommended_close_pipe_ids`）与预计失供用水点（`affected_sink_ids`）；
- 已关闭阀门与其他待修复事件的故障管段均不可通行；故障管段**两端**都须与所有水源断开；
- 本次只从**尚未关闭的有阀管段**中选取，且不含故障管段本身（与提交接口约束一致，保证建议可直接提交）；
- 优选顺序：**新增失供用水点数最少** → **本次关阀数最少** → 关阀编号序列（排序后）**字典序最小**；
- 故障管段不存在（`FAULT_PIPE_NOT_FOUND`）或关闭全部可用阀门仍无法隔离（`NO_FEASIBLE_PLAN`）时，
  返回 `feasible=false` 并在 `reason` 中给出原因，**不改任何状态**；
- 建议的提交仍走现有 `POST /api/isolation/submit`，按最新状态复核；
  版本过期沿用 `409 VERSION_CONFLICT` 冲突处理（重新建议后再提交即可）。

**复供判定**（操作员按 `事件编号 + 当前版本` 操作；复供图 = 重开该事件阀门，其他待修复故障管段继续移除）：

- 复供预演 `POST /api/isolation/events/{event_id}/restore/preview` 为**只读**：
  返回该事件所关阀门（`closed_pipe_ids`）、预计恢复供水的用水点（`restored_sink_ids`）
  及阻断原因（`block_reasons`），**不改变事件状态、阀位与版本**；
- 仅当重开本事件阀门后，**其余每条待修复故障管段的两端仍均无法从水源到达**，才 `safe=true`，方可确认；
  任一待修复故障有任一端重新进水，即存在**重新进水风险**，返回阻断原因
  `FAULT_REWATER_RISK`（含故障管段编号与重新进水的端点 `reachable_endpoints`）；
- 计算通水时排除：其他待修复事件的故障管段、以及**仍关闭的阀门**（仅重开本事件所关阀门）；
- 复供确认 `POST /api/isolation/events/{event_id}/restore` 在**同一事务**内：
  锁定事件行与版本行 → 复核版本 → 复核安全性 → 重开该事件阀门 → 版本 +1 → 事件置为 `restored`，原子生效。
- 复供明确拒绝（均不产生部分变更）：
  版本过期 `409 VERSION_CONFLICT`、事件不存在 `404 EVENT_NOT_FOUND`、
  事件已复供 `409 EVENT_ALREADY_RESTORED`、重新进水风险 `422 RESTORE_UNSAFE`。

**水源停运 / 恢复调度**（操作员按**一批水源编号 + 各自目标运行状态 + 当前版本**操作）：

- **只有水源(source)节点可以切换**；既有及整网导入的水源初始视为**运行(running)**，
  读网节点中水源带 `running` 字段（其余节点恒为 `true`）；
- 同批编号**不得重复**；一次可同时指定多个水源，各自目标状态可以不同
  （`running=true` 恢复、`running=false` 停运）；
- 仅**运行中**的水源才是通水种子：停运水源不向任何区域供水。**读网、关阀建议、
  隔离预演/提交、复供全部按运行水源计算**（保留既有接口约定，仅种子集合收敛）；
  停运可导致**全网无运行水源**（允许，不拒绝）。
- 水源预演 `POST /api/sources/switch/preview` 为**只读**：
  返回当前版本、`version_match`、每个目标的切换结果、`changed`（是否真有状态变化）、
  切换后**新增失供用水点**（`new_loss_sink_ids`）与**恢复供水用水点**
  （`restored_sink_ids`）、切换后运行水源列表（`running_source_ids`）、
  以及每条待修复故障的**重新进水风险**（`block_reasons`，代码仍为
  `FAULT_REWATER_RISK`，含故障管段编号与重新进水的端点 `reachable_endpoints`）；
  已关闭阀门与待修复故障管段在切换前后仍阻断通水。
- **恢复水源后**，若任何待修复故障管段的**任一端**将能从任一（含本批复运的）运行水源
  可达，即 `safe=false`，**禁止确认**；纯停运只减少通水，不产生重新进水风险；
  编号非法或同批重复时预演在 `issues` 中给出（`SOURCE_NOT_FOUND` /
  `SOURCE_SWITCH_DUPLICATE`），不改变任何状态。
- 确认 `POST /api/sources/switch` 在**同一事务**内：
  锁定版本行 → 按最新状态复核版本/编号/重复/安全性 → **原子更新水源运行状态与版本**；
  全部目标状态**已满足**（`changed=false`）时幂等成功但**不推进版本**。
- 水源调度明确拒绝（均不产生部分写入）：
  版本过期 `409 VERSION_CONFLICT`、编号不是水源或同批重复
  `422 SOURCE_SWITCH_INVALID`、恢复将使待修复故障重新进水
  `422 SOURCE_REWATER_RISK`（含 `FAULT_REWATER_RISK` 明细）；
  预演遇过期版本不拒绝，只返回 `version_match=false`（与其他预演一致）。

## 五、HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/network/import` | 整网导入（版本重置为 1，旧事件失效） |
| GET | `/api/network` | 读取当前管网与版本（水源含 `running` 运行状态） |
| POST | `/api/sources/switch/preview` | 水源停运/恢复预演（只读：失供/恢复用水点、重新进水风险） |
| POST | `/api/sources/switch` | 水源停运/恢复确认（单事务复核 + 原子更新水源状态与版本） |
| POST | `/api/isolation/recommend` | 关阀建议（只读，按当前管网运行水源计算可提交方案） |
| POST | `/api/isolation/preview` | 隔离方案预演（只读，计入待修复故障） |
| POST | `/api/isolation/submit` | 提交隔离（原子关阀 + 版本 +1，事件状态 open） |
| GET | `/api/isolation/events` | 隔离/复供操作历史（含 open / restored 状态） |
| POST | `/api/isolation/events/{event_id}/restore/preview` | 复供预演（只读） |
| POST | `/api/isolation/events/{event_id}/restore` | 复供确认（单事务复核 + 重开阀门） |

业务错误统一返回：

```json
{ "error": { "code": "NOT_ISOLATABLE", "message": "...", "issues": [ ... ] } }
```

## 六、调用示例

### 1. 导入管网

```bash
curl -s -X POST http://localhost:8000/api/network/import \
  -H 'Content-Type: application/json' \
  -d @examples/sample_network.json
```

示例管网（`examples/sample_network.json`）：

```
S(水源) --P1[阀]-- A(连接点) --P2[阀]-- B(连接点) --P4[阀]-- D(连接点) --P6(无阀,故障)-- U3(用水点)
                    |                  |
                  P3[阀]             P5[阀]
                    |                  |
                   U1(用水点)          U2(用水点)
```

响应：

```json
{ "version": 1, "node_count": 7, "pipe_count": 6 }
```

导入非法数据（如悬空端点）会返回 `422 IMPORT_INVALID`：

```bash
curl -s -X POST http://localhost:8000/api/network/import \
  -H 'Content-Type: application/json' \
  -d '{"nodes":[{"id":"S","type":"source"}],"pipes":[{"id":"P1","from":"S","to":"X","has_valve":false}]}'
```

### 2. 读取版本

```bash
curl -s http://localhost:8000/api/network
```

### 3. 预演：只关 P5（不能隔离）

```bash
curl -s -X POST http://localhost:8000/api/isolation/preview \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P5"],"version":1}'
```

`D` 点仍可经 P4 进水，故障管段 P6 的 D 端仍可达，故 `isolatable=false`。

### 4. 预演：关 P4（可以隔离，仅 U3 失供）

```bash
curl -s -X POST http://localhost:8000/api/isolation/preview \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P4"],"version":1}'
```

响应：

```json
{
  "version": 1,
  "version_match": true,
  "fault_pipe_id": "P6",
  "close_pipe_ids": ["P4"],
  "isolatable": true,
  "affected_sink_ids": ["U3"],
  "issues": []
}
```

`version_match=false` 表示你读取的版本已过期（预演仍基于当前管网计算，但不应直接提交）。

### 5. 提交隔离

```bash
curl -s -X POST http://localhost:8000/api/isolation/submit \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P4"],"version":1}'
```

成功响应（P4 阀位原子置为关闭，版本变为 2）：

```json
{
  "version": 2,
  "fault_pipe_id": "P6",
  "closed_pipe_ids": ["P4"],
  "affected_sink_ids": ["U3"],
  "event_id": 1
}
```

常见拒绝：

- 再次用 `"version":1` 提交 → `409 VERSION_CONFLICT`（版本过期，状态不变）；
- 取到版本 2 后再次关闭已关的 `P4` → `422 PLAN_INVALID` / `CLOSE_PIPE_DUPLICATE`；
- 关闭无阀管段 `P6` → `422 PLAN_INVALID` / `CLOSE_PIPE_NO_VALVE`；
- 提交不可隔离方案 → `422 NOT_ISOLATABLE`。

### 6. 隔离历史

```bash
curl -s http://localhost:8000/api/isolation/events
```

历史事件在既有字段基础上增量返回 `status`（`open` 待修复 / `restored` 已复供）
与 `restored_at`（复供时间，待修复时为 `null`），其余字段与旧版完全一致：

```json
{
  "id": 1,
  "version_before": 1,
  "version_after": 2,
  "fault_pipe_id": "P6",
  "closed_pipe_ids": ["P4"],
  "affected_sink_ids": ["U3"],
  "status": "open",
  "restored_at": null,
  "created_at": "2026-09-30T12:00:00Z"
}
```

### 7. 复供预演与确认（按事件编号 + 当前版本）

复供演示使用包含两个叠加故障的管网 `examples/restore_network.json`：

```
S --P1[阀]-- A --P2[阀]-- B --P4[阀]-- D --P6(无阀,故障1)-- U3
               |                     |
             P3[阀]                 P7(无阀,故障2)-- E --P8[阀]-- U4
               |
              U1
```

先导入并制造两条待修复事件：

```bash
curl -s -X POST http://localhost:8000/api/network/import \
  -H 'Content-Type: application/json' \
  -d @examples/restore_network.json
# 事件 1：故障 P6，关 P4 隔离整个 D 片区（U3/U4 失供）
curl -s -X POST http://localhost:8000/api/isolation/submit \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P4"],"version":1}'
# 事件 2：D 已被 P4 隔离，区内故障 P7 无需再关阀门即可隔离
curl -s -X POST http://localhost:8000/api/isolation/submit \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P7","close_pipe_ids":[],"version":2}'
```

复供预演事件 1（只读，不改状态）：重开 P4 会使待修复故障 P7 的 D 端重新进水，`safe=false`：

```bash
curl -s -X POST http://localhost:8000/api/isolation/events/1/restore/preview \
  -H 'Content-Type: application/json' -d '{"version":3}'
```

```json
{
  "version": 3,
  "version_match": true,
  "event_id": 1,
  "status": "open",
  "fault_pipe_id": "P6",
  "closed_pipe_ids": ["P4"],
  "safe": false,
  "restored_sink_ids": ["U3"],
  "block_reasons": [
    {
      "code": "FAULT_REWATER_RISK",
      "message": "重开阀门后，待修复故障管段 P7 的 D 端将重新进水，禁止复供",
      "pipe_id": "P7",
      "reachable_endpoints": ["D"]
    }
  ]
}
```

此时强行确认复供事件 1 → `422 RESTORE_UNSAFE`（明确拒绝，阀位/版本/事件状态均不变）：

```bash
curl -s -X POST http://localhost:8000/api/isolation/events/1/restore \
  -H 'Content-Type: application/json' -d '{"version":3}'
```

先复供区内事件 2（安全；预演 `safe=true`），确认后版本变为 4，事件 2 状态变为 `restored`：

```bash
curl -s -X POST http://localhost:8000/api/isolation/events/2/restore/preview \
  -H 'Content-Type: application/json' -d '{"version":3}'
curl -s -X POST http://localhost:8000/api/isolation/events/2/restore \
  -H 'Content-Type: application/json' -d '{"version":3}'
```

再用当前版本复供事件 1（此时其余待修复故障已不存在），P4 重开、U3/U4 恢复、版本变为 5：

```bash
curl -s -X POST http://localhost:8000/api/isolation/events/1/restore \
  -H 'Content-Type: application/json' -d '{"version":4}'
```

```json
{
  "version": 5,
  "event_id": 1,
  "status": "restored",
  "fault_pipe_id": "P6",
  "reopened_pipe_ids": ["P4"],
  "restored_sink_ids": ["U3", "U4"]
}
```

其他拒绝：

- 版本过期（如事件 2 复供后仍用 `"version":3` 复供事件 1）→ `409 VERSION_CONFLICT`；
- 事件不存在（如 `events/999`）→ `404 EVENT_NOT_FOUND`；
- 已复供事件再次复供 → `409 EVENT_ALREADY_RESTORED`；
- 整网重新导入后旧事件编号失效 → `404 EVENT_NOT_FOUND`。
- 复供预演遇到过期版本不拒绝，只返回 `version_match=false`（与隔离预演一致）。

### 8. 水源停运 / 恢复调度

水源调度演示使用双水源管网 `examples/source_switch_network.json`：

```
S1(江北水厂) --P1[阀]-- A --P4[阀]-- D --P6(无阀)-- U3(东郊工业园)
                         |
S2(江南水厂) --P2[阀]-- A --P3[阀]-- U1(幸福小区)
```

导入后两个水源初始均为运行。**只读预演**停运冗余水源 S2（S1 仍供水，无新增失供）：

```bash
curl -s -X POST http://localhost:8000/api/sources/switch/preview \
  -H 'Content-Type: application/json' \
  -d '{"switches":[{"source_id":"S2","running":false}],"version":1}'
```

```json
{
  "version": 1,
  "version_match": true,
  "switches": [{"source_id": "S2", "running": false}],
  "changed": true,
  "safe": true,
  "new_loss_sink_ids": [],
  "restored_sink_ids": [],
  "running_source_ids": ["S1"],
  "block_reasons": [],
  "issues": []
}
```

确认停运（原子更新水源状态，版本变为 2；`running_source_ids` 为切换后仍在运行的水源）：

```bash
curl -s -X POST http://localhost:8000/api/sources/switch \
  -H 'Content-Type: application/json' \
  -d '{"switches":[{"source_id":"S2","running":false}],"version":1}'
```

全部目标状态已满足时为幂等确认：`changed=false`，**版本保持不变**：

```bash
curl -s -X POST http://localhost:8000/api/sources/switch \
  -H 'Content-Type: application/json' \
  -d '{"switches":[{"source_id":"S2","running":false}],"version":2}'
```

停运**最后一个**运行水源 S1 不被拒绝（允许全网无运行水源），U1/U3 全部新增失供：

```bash
curl -s -X POST http://localhost:8000/api/sources/switch \
  -H 'Content-Type: application/json' \
  -d '{"switches":[{"source_id":"S1","running":false}],"version":2}'
```

```json
{
  "version": 3,
  "switches": [{"source_id": "S1", "running": false}],
  "changed": true,
  "new_loss_sink_ids": ["U1", "U3"],
  "restored_sink_ids": [],
  "running_source_ids": []
}
```

**恢复水源的安全约束**：无运行水源时先提交一条待修复故障（故障 P6 两端本就不可达，空方案即可隔离），
随后直接恢复 S1，预演给出 `safe=false` —— P4 阀开启，待修复故障管段 P6 的 D 端将重新进水：

```bash
# 无运行水源：空方案隔离 P6（版本 4，事件 open）
curl -s -X POST http://localhost:8000/api/isolation/submit \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":[],"version":3}'

# 预演恢复 S1
curl -s -X POST http://localhost:8000/api/sources/switch/preview \
  -H 'Content-Type: application/json' \
  -d '{"switches":[{"source_id":"S1","running":true}],"version":4}'
```

```json
{
  "version": 4,
  "version_match": true,
  "switches": [{"source_id": "S1", "running": true}],
  "changed": true,
  "safe": false,
  "new_loss_sink_ids": [],
  "restored_sink_ids": ["U1", "U3"],
  "running_source_ids": ["S1"],
  "block_reasons": [
    {
      "code": "FAULT_REWATER_RISK",
      "message": "恢复水源后，待修复故障管段 P6 的 D 端将重新进水，禁止确认",
      "pipe_id": "P6",
      "reachable_endpoints": ["D"]
    }
  ],
  "issues": []
}
```

此时强行确认 → `422 SOURCE_REWATER_RISK`（水源状态与版本均不变，无部分写入）；
先按既有复供流程恢复事件（无运行水源时重开阀门也无进水风险），再恢复 S1 即安全，
U1/U3 恢复供水。其他拒绝：

- 编号不是水源（如 `U1`/`A`）或不存在、同批编号重复 → `422 SOURCE_SWITCH_INVALID`
  （预演不拒绝，在 `issues` 中给出 `SOURCE_NOT_FOUND` / `SOURCE_SWITCH_DUPLICATE`）；
- 版本过期确认 → `409 VERSION_CONFLICT`；预演不拒绝，只标记 `version_match=false`。

批量切换可在一次请求中给出多个水源及各自目标状态（原子生效）：

```bash
curl -s -X POST http://localhost:8000/api/sources/switch \
  -H 'Content-Type: application/json' \
  -d '{"switches":[{"source_id":"S1","running":false},{"source_id":"S2","running":true}],"version":2}'
```

水源调度全流程演示脚本（标准库实现）：

```bash
python3 examples/source_switch_demo.py
# 或指定地址：
python3 examples/source_switch_demo.py http://localhost:8000
```

脚本依次演示：导入 → 冗余水源停运预演/确认 → 幂等确认不推进版本 → 停运最后水源（全网失供）→
过期版本 409 → 无水源时空方案隔离故障 → 恢复被重新进水风险拦截（预演 safe=false / 确认 422）→
编号非法与同批重复拒绝 → 先复供再安全恢复水源 → 读网核对 `running`。

### 9. 关阀建议（只知道故障管段时）

操作员只知道故障管段编号时，无需手工构造关阀列表，直接请求建议（**只读**，不改任何状态）：

```bash
curl -s -X POST http://localhost:8000/api/isolation/recommend \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","version":1}'
```

响应（建议关 `P4`，预计 `U3` 失供；`version_match=false` 表示已读版本过期，应重新读取）：

```json
{
  "version": 1,
  "version_match": true,
  "fault_pipe_id": "P6",
  "feasible": true,
  "recommended_close_pipe_ids": ["P4"],
  "affected_sink_ids": ["U3"],
  "reason": null
}
```

建议方案可直接交给现有提交接口（按最新状态复核，版本过期仍为 `409 VERSION_CONFLICT`）：

```bash
curl -s -X POST http://localhost:8000/api/isolation/submit \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P4"],"version":1}'
```

无法给出建议时返回 `feasible=false` 并在 `reason` 中说明原因（状态不变）：

- 故障管段不存在 → `reason.code = FAULT_PIPE_NOT_FOUND`；
- 关闭全部可用阀门后故障管段仍有端点与水源相通（如故障管段直连水源且无阀可关）
  → `reason.code = NO_FEASIBLE_PLAN`。

```json
{
  "version": 1,
  "version_match": true,
  "fault_pipe_id": "P1",
  "feasible": false,
  "recommended_close_pipe_ids": [],
  "affected_sink_ids": [],
  "reason": {
    "code": "NO_FEASIBLE_PLAN",
    "message": "关闭全部可用阀门后，故障管段 P1 仍至少有一端可从水源到达，无可行关阀方案",
    "pipe_id": "P1"
  }
}
```

关阀建议全流程演示脚本（标准库实现）：

```bash
python3 examples/recommend_demo.py
# 或指定地址：
python3 examples/recommend_demo.py http://localhost:8000
```

脚本依次演示：导入 → 建议（故障 P6）→ 按建议提交 → 已隔离故障的空方案建议 →
新故障建议（不含已关闭阀门）→ 过期版本提交 409 → 按当前版本提交 →
无可行方案 / 故障管段不存在 → 确认建议全程只读。

### 一键完整演示脚本（标准库实现，无需安装依赖）

```bash
python3 examples/demo.py
# 或指定地址：
python3 examples/demo.py http://localhost:8000
```

脚本依次演示：导入 → 读取版本 → 不可行/可行预演 → 过期版本 409 → 成功提交 → 重复提交 409 → 重复关阀 422 → 历史。

复供流程（两条叠加故障：风险拒绝 → 区内事件安全复供 → 版本/状态拒绝 → 外层事件安全复供 → 历史）：

```bash
python3 examples/restore_demo.py
# 或指定地址：
python3 examples/restore_demo.py http://localhost:8000
```

水源停运/恢复调度流程（冗余停运 → 幂等确认 → 全网停供 → 隔离 → 恢复风险拒绝 → 复供后安全恢复）：

```bash
python3 examples/source_switch_demo.py
# 或指定地址：
python3 examples/source_switch_demo.py http://localhost:8000
```

## 七、测试

核心图算法与校验逻辑（`tests/test_planning.py`，42 个用例）不依赖数据库与第三方库，可直接用标准库运行：

```bash
python3 -m unittest tests.test_planning -v
```

路由层集成测试（`tests/test_api.py`，44 个用例）用内存替身替换数据库，覆盖完整 HTTP 契约（含水源停运/恢复调度、关阀建议、复供预演/确认、版本与安全拒绝、整网替换失效），需先安装依赖：

```bash
pip install -r requirements.txt httpx
python3 -m unittest discover -s tests -v
```

## 八、项目结构

```
app/
  config.py       # 环境变量配置
  schemas.py      # Pydantic 请求/响应模型
  planning.py     # 纯领域逻辑：导入校验、无向图可达性(BFS，仅运行水源为种子)、隔离评估、复供安全评估、关阀建议、水源停运/恢复评估
  db.py           # PostgreSQL：DDL/迁移、快照、整网导入、事务化隔离提交、事务化复供、事务化水源切换、历史
  serializers.py  # 响应序列化
  main.py         # FastAPI 路由与错误处理
examples/
  sample_network.json        # 隔离演示管网
  restore_network.json       # 复供演示管网（叠加故障）
  source_switch_network.json # 水源调度演示管网（双水源）
  demo.py                    # 隔离全流程示例
  restore_demo.py            # 复供全流程示例
  recommend_demo.py          # 关阀建议全流程示例
  source_switch_demo.py      # 水源停运/恢复全流程示例
tests/
  test_planning.py
  test_api.py
Dockerfile
docker-compose.yml
requirements.txt
```
