"""坡度差量 API 测试：版本号、差量取数、重启可复现、重录全量重建、旧库迁移。"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import (full_grid, init_map, stage_tile, tile_grid)


def stage_interior_change(client, row, col, i, j, value):
    """暂存某瓦片内部点 (i,j)（瓦片局部坐标，不触碰共边），发版必只动这一块。"""
    t = tile_grid(100)
    t[i][j] = value
    stage_tile(client, row, col, t)


def publish(client, row=0, col=0):
    r = client.post("/publish", json={"row": row, "col": col})
    assert r.status_code == 200, r.text
    return r.json()


# ---- 版本号：录入起版、整图与发版带版号 -----------------------------------

def test_basemap_starts_at_version_one(client):
    init_map(client, 2, 2)
    assert client.get("/basemap").json()["version"] == 1
    grid = client.get("/basemap/grid").json()
    assert grid["version"] == 1 and grid["generation"] == 1
    tile = client.get("/tiles/0/0").json()
    assert tile["release_version"] == 1


def test_publish_advances_version_and_reports_delta_counts(client):
    init_map(client, 1, 1)
    stage_interior_change(client, 0, 0, 5, 5, 150)
    body = publish(client)
    assert body["version"] == 2 and body["generation"] == 1
    assert body["elevation_changes"] == 1
    # 变化格点的四邻（均为内部点）共 4 个，且分量都发生了变化
    assert body["slope_delta_cells"] == 4
    assert client.get("/basemap").json()["version"] == 2
    assert client.get("/basemap/grid").json()["version"] == 2


# ---- 差量取数：坐标、目标值、四邻规则 -------------------------------------

def test_delta_returns_grid_coordinates_and_target_values(client):
    init_map(client, 1, 1)
    stage_interior_change(client, 0, 0, 5, 5, 150)
    publish(client)
    r = client.get("/delta", params={"from_version": 1, "to_version": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["from_version"] == 1 and body["to_version"] == 2
    assert body["current_version"] == 2 and body["grid_rows"] == 17
    coords = {(c["row"], c["col"]) for c in body["changes"]}
    assert coords == {(4, 5), (6, 5), (5, 4), (5, 6)}
    by = {k: c for k, c in zip(coords, body["changes"])}
    north = body["changes"][[ (c["row"], c["col"]) for c in body["changes"]].index((4, 5))]
    assert north["east_minus_west"] == 0 and north["south_minus_north"] == 50
    # 所有返回点都是四邻齐全的内部格点
    assert all(1 <= c["row"] <= 15 and 1 <= c["col"] <= 15 for c in body["changes"])


def test_delta_to_version_defaults_to_current(client):
    init_map(client, 1, 1)
    stage_interior_change(client, 0, 0, 5, 5, 150)
    publish(client)
    a = client.get("/delta", params={"from_version": 1}).json()
    b = client.get("/delta", params={"from_version": 1, "to_version": 2}).json()
    assert a == b


def test_equal_versions_returns_empty(client):
    init_map(client, 1, 1)
    body = client.get("/delta", params={"from_version": 1, "to_version": 1}).json()
    assert body["changes"] == [] and body["change_count"] == 0


def test_delta_skips_cancelled_components(client):
    init_map(client, 1, 1)
    # 东西两侧同时抬高 50：格点 (5,5) 的东减西相抵为 0，不出现在差量里
    t = tile_grid(100)
    t[5][4] = 150
    t[5][6] = 150
    stage_tile(client, 0, 0, t)
    publish(client)
    body = client.get("/delta", params={"from_version": 1, "to_version": 2}).json()
    assert (5, 5) not in {(c["row"], c["col"]) for c in body["changes"]}


def test_delta_seam_coordinates_are_unique(client):
    # 1×2 底图，改共边（整图 c=16）附近的内部点，坐标不得重复
    init_map(client, 1, 2)
    t0, t1 = tile_grid(100), tile_grid(100)
    t0[8][15] = 300  # 整图坐标 (8,15)，不落在共边上，两块无需一起换
    stage_tile(client, 0, 0, t0)
    stage_tile(client, 0, 1, t1)
    publish(client)
    body = client.get("/delta", params={"from_version": 1, "to_version": 2}).json()
    coords = [(c["row"], c["col"]) for c in body["changes"]]
    assert len(coords) == len(set(coords))


# ---- 下游一次追平多次发版 -------------------------------------------------

def test_catch_up_multiple_publishes_and_revert(client):
    init_map(client, 1, 1)
    stage_interior_change(client, 0, 0, 5, 5, 150)
    publish(client)  # v2
    t = tile_grid(100)  # 回改：(5,5) 恢复原值
    stage_tile(client, 0, 0, t)
    publish(client)  # v3
    stage_interior_change(client, 0, 0, 9, 9, 60)
    publish(client)  # v4

    # 下游从 v1 一次追平到 v4：回改格点的分量回到原值，必须被跳过
    body = client.get("/delta", params={"from_version": 1, "to_version": 4}).json()
    assert body["change_count"] == len(body["changes"])
    coords = {(c["row"], c["col"]) for c in body["changes"]}
    assert (4, 5) not in coords and (6, 5) not in coords  # 回改点的邻点已恢复
    assert (8, 9) in coords  # 第三次发版引入的变化仍在

    # 逐版追平与一次追平的最终目标值一致：下游逐相邻版区间应用，格点分量
    # 恢复到应用前的值时删除本地更新（与一次合并的跳过语义等价）
    applied = {}
    for v in range(1, 4):
        seg = client.get("/delta",
                         params={"from_version": v,
                                 "to_version": v + 1}).json()["changes"]
        for ch in seg:
            key = (ch["row"], ch["col"])
            cur = applied.get(key)
            if cur is not None and (cur["east_minus_west"] != ch["east_minus_west"]
                                    or cur["south_minus_north"]
                                    != ch["south_minus_north"]):
                applied.pop(key)  # 目标分量变回另一值（此处回到基线 0）-> 删除更新
            else:
                applied[key] = ch
    direct = {(c["row"], c["col"]): c for c in body["changes"]}
    assert set(applied) == set(direct)
    for k in direct:
        assert applied[k]["east_minus_west"] == direct[k]["east_minus_west"]
        assert applied[k]["south_minus_north"] == direct[k]["south_minus_north"]


# ---- 重启后同一对版号取同一差量 -------------------------------------------

def test_delta_reproducible_after_restart(tmp_path):
    db = str(tmp_path / "restart.db")
    app = create_app(db)
    with TestClient(app) as c:
        init_map(c, 1, 1)
        stage_interior_change(c, 0, 0, 5, 5, 150)
        publish(c)
        before = c.get("/delta",
                       params={"from_version": 1, "to_version": 2}).json()

    # 重新创建应用（模拟进程重启），数据库文件不变
    app2 = create_app(db)
    with TestClient(app2) as c:
        info = c.get("/basemap").json()
        assert info["version"] == 2
        after = c.get("/delta",
                      params={"from_version": 1, "to_version": 2}).json()
    assert after == before


# ---- 重录底图：旧版号提示全量重建 -----------------------------------------

def test_rebasemap_old_versions_require_full_rebuild(tmp_path):
    db = str(tmp_path / "rebuild.db")
    with TestClient(create_app(db)) as c:
        init_map(c, 1, 1)
        stage_interior_change(c, 0, 0, 5, 5, 150)
        publish(c)
        assert c.get("/delta",
                     params={"from_version": 1, "to_version": 2}).status_code == 200
        assert c.delete("/basemap").status_code == 200
        init_map(c, 1, 1)  # 重录：代数 +1，新版号重新从 1 起
        info = c.get("/basemap").json()
        assert info["generation"] == 2 and info["version"] == 1

        r = c.get("/delta", params={"from_version": 1, "to_version": 2})
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["error"] == "version_unavailable"
        assert detail["rebuild_required"] is True

        # 新代数内部的版号区间仍可用（v1->v1 空差量）
        ok = c.get("/delta", params={"from_version": 1, "to_version": 1})
        assert ok.status_code == 200


def test_unknown_version_requires_rebuild(client):
    init_map(client, 1, 1)
    r = client.get("/delta", params={"from_version": 1, "to_version": 99})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "version_unavailable"


def test_invalid_version_range_rejected(client):
    init_map(client, 1, 1)
    stage_interior_change(client, 0, 0, 5, 5, 150)
    publish(client)  # 当前版 2，版号 1、2 均存在
    r = client.get("/delta", params={"from_version": 2, "to_version": 1})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_version_range"


def test_unknown_version_outranks_range_check(client):
    # 重录式场景：本地版号高于当前版且版号不存在 -> 优先提示全量重建而非区间非法
    init_map(client, 1, 1)
    r = client.get("/delta", params={"from_version": 2, "to_version": 1})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "version_unavailable"


# ---- 发版失败保留原版本与差量 ---------------------------------------------

def test_failed_publish_keeps_version_and_delta(client):
    init_map(client, 1, 2)
    # 先发一次成功的版本，落一份差量
    stage_interior_change(client, 0, 0, 3, 3, 150)
    publish(client)
    good = client.get("/delta",
                      params={"from_version": 1, "to_version": 2}).json()

    # 制造无解发版：改右共边而邻块没有能接上的暂存版
    t = tile_grid(100)
    for row in t:
        row[16] = 200
    stage_tile(client, 0, 0, t)
    r = client.post("/publish", json={"row": 0, "col": 0})
    assert r.status_code == 409
    # 版本号未推进、旧差量原样可取
    assert client.get("/basemap").json()["version"] == 2
    again = client.get("/delta",
                       params={"from_version": 1, "to_version": 2}).json()
    assert again == good


# ---- 旧库迁移：从启用时的现行图起版 ---------------------------------------

def test_legacy_database_migrates_from_current_map(tmp_path):
    db = str(tmp_path / "legacy.db")
    # 手工构造一个"差量功能启用前"的旧库：只有三张旧表
    con = sqlite3.connect(db)
    con.executescript("""
    CREATE TABLE basemap (id INTEGER PRIMARY KEY CHECK (id = 1),
        rows INTEGER NOT NULL, cols INTEGER NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE tiles (row INTEGER NOT NULL, col INTEGER NOT NULL,
        version INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY (row, col));
    CREATE TABLE staged (id INTEGER PRIMARY KEY AUTOINCREMENT,
        row INTEGER NOT NULL, col INTEGER NOT NULL, data TEXT NOT NULL,
        created_at TEXT NOT NULL);
    """)
    import json
    con.execute("INSERT INTO basemap VALUES (1, 1, 1, '2026-01-01T00:00:00+00:00')")
    con.execute("INSERT INTO tiles VALUES (0, 0, 3, ?)",
                (json.dumps(tile_grid(100)),))
    con.commit()
    con.close()

    with TestClient(create_app(db)) as c:
        info = c.get("/basemap").json()
        # 现行图起版为版本 1；瓦片自身版本号 3 保留不动
        assert info["version"] == 1 and info["versions"] == [[3]]
        # 基线版本没有差量依据，但版号合法，空差量可取
        r = c.get("/delta", params={"from_version": 1, "to_version": 1})
        assert r.status_code == 200 and r.json()["changes"] == []
        # 迁移后可以正常发版并产出差量
        stage_interior_change(c, 0, 0, 5, 5, 150)
        body = publish(c)
        assert body["version"] == 2 and body["slope_delta_cells"] == 4
        r = c.get("/delta", params={"from_version": 1, "to_version": 2})
        assert r.status_code == 200 and r.json()["change_count"] == 4

    # 迁移必须幂等：再次初始化同一库不报错、版本不重复
    with TestClient(create_app(db)) as c:
        assert c.get("/basemap").json()["version"] == 2
        assert c.get("/delta",
                     params={"from_version": 1, "to_version": 2}
                     ).json()["change_count"] == 4
