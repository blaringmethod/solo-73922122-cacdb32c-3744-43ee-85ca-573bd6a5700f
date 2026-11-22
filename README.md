# 高程瓦片供数服务（含坡度差量供数）

面向坡度数据仓的高程供数 API。矩形底图按**行列编号**划分瓦片，每块 **17×17 个整数高程点**，相邻块**重复存储共边**上的点，因此跨瓦片取点（如坡度计算）无需额外拼接逻辑。数据员**按块暂存**修测，再**点名一块发版**：服务自动选出**换块最少且所有共边逐点吻合**的方案整批生效；无解时报告冲突边两侧瓦片坐标，现行底图与暂存原样保留。

坡度仓隔几次修测才更新一次，因此服务另提供**坡度差量取数**：传入已入仓版与目标版，返回坡度分量有变化的整图格点坐标及目标值，下游按坐标直接更新即可，无需重算全图。

- 技术栈：FastAPI + SQLite（WAL），Docker Compose 一键启动
- 交互式文档：启动后访问 `http://localhost:8000/docs`

## 快速开始

```bash
# Docker（推荐）
docker compose up --build
# 另开终端运行下游追平演示（多版追平、回改跳过、重录全量重建）
docker compose run --rm demo

# 或本地运行
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
python3 demo/catch_up_demo.py            # 下游追平演示（仅用标准库）
```

数据库文件路径由环境变量 `ELEV_DB_PATH` 指定（默认 `./data/elev.db`，Compose 中为 `/data/elev.db` 并挂载卷持久化）。

```bash
# 运行测试
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## 版本模型

- 录入底图即**全局版本 1**；每次成功发版全局版本号 +1，与瓦片自身的换版计数（`tiles.version`）相互独立。
- 整图取数（`GET /basemap/grid`）、窗口取数、单块取数与发版响应都带 `version`（当前全局版号）与 `generation`（底图代数）。
- **发版时高程与差量依据在同一事务内生效**：任一环节失败整体回滚，原版本、原高程与已落库差量全部保留。
- 差量依据（`slope_delta`）与高程变化明细（`elevation_change`）随发版持久化，**重启后按同一对版号取到完全相同的差量**。
- 重录底图（`DELETE /basemap` 后重新录入）代数 +1、版号从 1 重新起；旧代数的版号再来查差量时返回 **409 `version_unavailable` 且 `rebuild_required=true`**，提示全量重建。
- **旧库自动迁移**：差量功能启用前的数据库首次启动时自动建表，以启用时的现行图起版为版本 1（瓦片自身版本号保留不动）。

## 坡度差量 API

```
GET /delta?from_version={已入仓版}&to_version={目标版，省略=当前版}
```

响应：

```json
{
  "from_version": 1, "to_version": 4, "generation": 1, "current_version": 4,
  "grid_rows": 33, "grid_cols": 33, "change_count": 4,
  "changes": [
    {"row": 8, "col": 9, "east_minus_west": 0, "south_minus_north": -40}
  ]
}
```

规则（与题面一一对应）：

1. **仅四邻齐全的格点供数**：整图最外一圈缺邻，不返回；内部域为 `1 ≤ row ≤ H-2, 1 ≤ col ≤ W-2`。
2. **分量定义**：`东减西 = 东邻高程 − 西邻高程`，`南减北 = 南邻高程 − 北邻高程`，均为整数高程差。
3. **共边同一格点只返回一次**：差量在拼接后的整图坐标上计算，共边点物理上只占一个坐标。
4. **跨版回改或相抵后分量相同的格点跳过**：每格点取区间最早旧值与最晚新值比较，相同则不输出。
5. **只检查四邻曾在两版间发生高程变化的格点**：发版时先求高程变化格点，候选集 = 变化格点的四邻 ∩ 内部格点；其余格点分量恒定，根本不计算。
6. 下游追平方式：保存"本地已入仓版号"，每次以它为 `from_version`、当前版为 `to_version` 拉差量，应用 `changes` 后把本地版号更新为 `to_version`；一次调用即可追平期间的任意多次发版。

错误码：版号不存在或属于重录前旧底图 → 409 `version_unavailable`（带 `rebuild_required: true`）；同代数内 `from_version > to_version` → 422 `invalid_version_range`。

## 业务流程与 API

| 步骤 | 方法与路径 | 说明 |
|---|---|---|
| 录入底图 | `POST /basemap` | 两种方式二选一：`tiles` 逐块给出（校验所有共边，失配则 422 并列出失配边与点）；或 `grid` 给出 (16·rows+1)×(16·cols+1) 整图，自动切块、接缝天然吻合。录入即全局版本 1 |
| 底图概况 | `GET /basemap` | 行列数、代数、当前全局版号、各块版本号、各块暂存数量 |
| 暂存修测 | `POST /tiles/{row}/{col}/staged` | 可多次暂存同一块，发版时取 id 最大（最新）的一版 |
| 查看暂存 | `GET /tiles/{row}/{col}/staged`、`GET /staged` | 暂存版本列表 |
| 点名发版 | `POST /publish` | 请求体 `{"row": r, "col": c}`；成功返回新版号、换版坐标、清除的暂存 id 与本次差量格点数；冲突返回 409 与冲突边两侧瓦片坐标 |
| 整图取数 | `GET /basemap/grid` | 拼接后的完整高程面 + 当前版号 + 各块版本号 |
| 跨瓦片取点 | `GET /basemap/window?row0=&col0=&row1=&col1=` | 整图坐标系下的矩形窗口（含端点，带版号） |
| 单块取数 | `GET /tiles/{row}/{col}` | 含瓦片版本号与当前全局发版号 |
| **坡度差量** | `GET /delta?from_version=&to_version=` | 两版间坡度分量有变化的整图格点坐标及目标值 |
| 重置 | `DELETE /basemap` | 删除底图、全部版本与差量（重录/演示用）；代数保留，重录后 +1 |

坐标均从 0 开始。瓦片行列数上限 64×64（防止整图响应过大）。

### 发版语义

- **点名块必须**换上其最新暂存版（没有暂存版则 409 `no_staged_version`）；
- 其余每块可**保留现行版**或换上**各自最新暂存版**；
- 约束：发版后所有共边 17 个点逐点相等；目标：**换版块数最少**；
- 成功：整批原子生效（含差量依据与版本号推进），返回 `changed_tiles`（换版坐标与新版本号）与
  `cleared_staged_ids`；**只清除本次用到的暂存**（未涉及的、以及同块的
  更旧暂存版都保留）；
- 无解：409 `publish_conflict`，`conflicting_edges` 列出冲突边两侧瓦片
  坐标，**现行底图、版本号与差量均未改动**。

### 示例

```bash
# 录入 1×2 底图（整图方式，全图高程 100）-> 版本 1
curl -X POST localhost:8000/basemap -H 'Content-Type: application/json' \
  -d "{\"grid\": $(python -c 'import json;print(json.dumps([[100]*33]*17))')}"

# 暂存 (0,0)：右共边改为 200（同时给 (0,1) 一版能接上的暂存）
python - <<'EOF'
import json, urllib.request
def post(path, payload):
    req = urllib.request.Request("http://localhost:8000"+path,
        data=json.dumps(payload).encode(), headers={"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(req).read())
t0 = [[100]*17 for _ in range(17)]
for row in t0: row[16] = 200
t1 = [[100]*17 for _ in range(17)]
for row in t1: row[0] = 200
post("/tiles/0/0/staged", {"data": t0})
post("/tiles/0/1/staged", {"data": t1})
print(post("/publish", {"row": 0, "col": 0}))      # -> 版本 2，两块同换
print(urllib.request.urlopen(
    "http://localhost:8000/delta?from_version=1").read().decode())
EOF
```

## 发版算法（为什么"换块最少"是确定的）

对每条共边，记现行值为 K、两侧最新暂存版在该边上的值为 A、B（现行底图
本身接缝吻合，两侧 K 相同）：

| 情形 | 结论 |
|---|---|
| A=K 且 B=K | 无约束 |
| A≠K 且 B=K | 左侧永远不能换（换了右侧接不上） |
| A=K 且 B≠K | 右侧永远不能换 |
| A≠K、B≠K、A=B | **等值边**：两侧必须同进退 |
| A≠K、B≠K、A≠B | 两侧都不能换 |
| 一侧无暂存 | 另一侧仅当 A=K 时可换 |

把等值边做**并查集**连边，点名块所在连通分量就是唯一被强制换版的最小集合：
任何可行方案都必须整个换掉它，而分量之外全部保留现行版必然可行。对该分量
做全量共边校验：通过即为换块最少的方案（唯一）；不通过则不存在任何可行方案
（分量外的块即使换版也接不上，否则它早被等值边连进分量），校验发现的失配边
就是要报告的冲突边。算法复杂度 O(瓦片数 × 17)。实现见 `app/solver.py`。

## 差量是怎么算出来的

实现见 `app/slope.py`（纯函数）与 `app/service.py`（事务编排）：

1. 发版规划确定换版集合后，在同一写事务内把现行瓦片与换版后瓦片分别拼成两张整图；
2. `diff_elevations` 求两版间高程变化格点；
3. 候选集 = 每个变化格点的四邻，剔除贴边（四邻不齐）的点；
4. 对候选点分别计算两版的 (东减西, 南减北)，分量有变化才写入 `slope_delta`，
   同时把高程变化明细写入 `elevation_change`；
5. 取差量时只读合并区间内各相邻版的差量依据（最早旧值 vs 最晚新值），
   相同则跳过。差量依据落库，故计算只发生在发版时，取数为 O(差量行数)。

## 并发与可串行化

暂存、发版、整图取数并发时，结果与某种依次执行的顺序一致：

- SQLite **WAL** 模式：读者不阻塞写者，读事务看到一致性快照；
- 写操作（录入/暂存/发版/重置）一律 `BEGIN IMMEDIATE`：事务开始即拿写锁，
  写者之间严格串行，`busy_timeout=30s` 兜底等待；
- 发版的"读取现行版+暂存 → 规划 → 差量计算 → 批量更新+清除暂存+版本推进"
  在**同一个写事务**内完成，因此两个并发发版不会基于过期快照做决策，
  也不可能出现高程已换而差量未更新的中间态；
- 读操作（整图/窗口/单块/差量）在只读事务内完成，不会看到发版换了一半的中间态。

`tests/test_concurrency.py` 用真实 uvicorn + 多线程验证：两个发版竞争同一
暂存版时恰好一个成功、另一个报 `no_staged_version`；发版 4×4 全图换版期间
并发整图取数，每次取到的都是完整旧图或完整新图。

## 项目结构

```
app/
  main.py     FastAPI 入口与路由（/docs、/delta）
  service.py  业务层：事务边界、发版编排、差量落库与合并、旧库迁移
  solver.py   发版规划器（纯函数）与接缝校验
  slope.py    整图拼接、高程变化、候选集、坡度分量与差量合并（纯函数）
  db.py       SQLite 连接、建表、WAL、旧库迁移
  models.py   请求体模型（17×17 校验）
demo/
  catch_up_demo.py  下游坡度仓一次追平多次发版的端到端演示（仅标准库）
tests/        pytest：API、规划器单测、坡度差量、并发测试
Dockerfile / docker-compose.yml（含一次性 demo 服务）
```

## 实测结果

- `python -m pytest tests/ -q`：**59 passed**（原 32 个用例无回归；新增
  `test_slope.py` 11 个纯函数用例、`test_delta.py` 15 个 API/迁移/重启用例，
  含 3 个真实 uvicorn 并发用例，重复运行稳定）
- 真实 uvicorn 端到端实测（本地等效于容器内命令）：
  - `demo/catch_up_demo.py` 全绿：三次发版后下游从 v1 一次追平到 v4，
    回改格点的四邻分量恢复、自动跳过，下游仓格点数与服务端合并差量一致；
    重录底图后旧版号查询返回 409 `rebuild_required`，下游转入全量重建分支
  - 共边发版（1×2 改右共边，两块同换、17 点高程变化）：差量 30 个内部格点，
    与手工计算的东减西 ±100、南减北 0 完全一致，坐标无重复、共边列自身不供数
  - 进程重启后同一对版号的差量响应逐字节一致
  - 含现行图的旧库迁移：全局版号从现行图起为 1、瓦片自身版本号（5）保留，
    迁移后发版产出 v2 差量正常；空旧库仅补表结构
  - 发版失败（无暂存/冲突）版本号不推进、旧差量原样可取；非法区间 422、
    未知版号 409、无底图 409 均符合预期
  - `/docs`、`/openapi.json`、`/`（重定向到 /docs）均可正常访问，
    `/delta` 可直接在 Swagger UI 中执行

## 待完成项

- 本环境无 Docker 守护进程，镜像未实际构建；`Dockerfile` 沿用基线结构
  （仅 `COPY app`，演示脚本通过 compose 的只读挂载进入 demo 容器），
  已用本地 `uvicorn app.main:app` + 同一演示脚本等效验证
- 无认证与权限控制；暂存、发版未记录操作人
- 仅支持单幅底图（重录需先 `DELETE /basemap`）
- 窗口取数目前整图拼接后切片，瓦片规模放大后可改为按需读取相交瓦片
- 历史暂存版无自动清理策略（发版只清除用到的版本，旧版需人工管理）
- 差量依据与高程变化明细随发版持续累积，暂未做按版本保留窗口的压缩/清理
- 瓦片尺寸 17×17 为常量（`app/solver.py: TILE`），未参数化
