"""SQLite 连接、建表与旧库迁移。

每次操作新开连接，配合 WAL + busy_timeout 支撑并发。

版本模型（差量功能）：
  * 底图录入即全局版本 1；每次成功发版全局版本 +1，版本号随整图取数与
    发版响应返回；
  * releases 持久化每个版本，slope_delta / elevation_change 持久化每次
    发版的差量依据，重启后仍可按同一对版号取同一差量；
  * generation（代数）标识底图谱系：重录底图后代数 +1，旧代数的版号
    不再可取，查询时提示全量重建；
  * meta 保存当前代数与当前版本号两个单行计数器。
旧库（无上述表）在首次连接时自动迁移：以现行图起版为版本 1。
"""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS basemap (
    id         INTEGER PRIMARY KEY CHECK (id = 1),  -- 单幅底图，恒为 1
    rows       INTEGER NOT NULL,
    cols       INTEGER NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1,          -- 底图代数，重录 +1
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tiles (
    row     INTEGER NOT NULL,
    col     INTEGER NOT NULL,
    version INTEGER NOT NULL,   -- 瓦片自身版本号，每次换版 +1
    data    TEXT NOT NULL,      -- 17×17 整数高程，JSON
    PRIMARY KEY (row, col)
);
CREATE TABLE IF NOT EXISTS staged (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- 同时也是"最新"的判定依据
    row        INTEGER NOT NULL,
    col        INTEGER NOT NULL,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    generation   INTEGER NOT NULL,  -- 当前底图代数
    head_version INTEGER NOT NULL   -- 当前全局发版号
);
CREATE TABLE IF NOT EXISTS releases (
    id         INTEGER PRIMARY KEY,   -- 即全局版本号，从 1 起递增
    generation INTEGER NOT NULL,      -- 所属底图代数
    created_at TEXT NOT NULL,
    note       TEXT NOT NULL          -- 'basemap' 录入起版；'publish' 发版
);
CREATE TABLE IF NOT EXISTS elevation_change (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_version INTEGER NOT NULL,    -- 发版前版本号
    to_version   INTEGER NOT NULL,    -- 发版后版本号
    row          INTEGER NOT NULL,    -- 整图行坐标
    col          INTEGER NOT NULL,    -- 整图列坐标
    old_value    INTEGER NOT NULL,
    new_value    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_elev_change_versions
    ON elevation_change (from_version, to_version);
CREATE TABLE IF NOT EXISTS slope_delta (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_version INTEGER NOT NULL,    -- 差量依据所属相邻版区间
    to_version   INTEGER NOT NULL,
    row          INTEGER NOT NULL,    -- 整图行坐标（四邻齐全的内部格点）
    col          INTEGER NOT NULL,
    old_ew       INTEGER NOT NULL,    -- 旧版 东减西
    new_ew       INTEGER NOT NULL,    -- 新版 东减西（目标值来源）
    old_ns       INTEGER NOT NULL,    -- 旧版 南减北
    new_ns       INTEGER NOT NULL,    -- 新版 南减北（目标值来源）
    UNIQUE (from_version, row, col)
);
CREATE INDEX IF NOT EXISTS idx_slope_delta_versions
    ON slope_delta (from_version, to_version);
"""

BUSY_TIMEOUT_MS = 30000


def connect(db_path: str) -> sqlite3.Connection:
    """新建连接。isolation_level=None 进入自动提交模式，事务由 SQL 显式控制。"""
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _table_names(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}


def _has_column(conn, table, column):
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _migrate_legacy(conn):
    """旧库迁移：差量功能启用前的库从现行图起版（版本 1、代数 1）。

    判定：旧库有 basemap 表但没有 releases 表。迁移幂等——新库或已迁移
    过的库直接跳过。迁移只新增版本/差量表与计数，不动任何高程数据。
    """
    tables = _table_names(conn)
    if "basemap" not in tables or "releases" in tables:
        return
    conn.executescript(SCHEMA)
    # 极旧库可能已带 generation 列，ALTER 前先探测，保证迁移可重入
    if not _has_column(conn, "basemap", "generation"):
        conn.execute(
            "ALTER TABLE basemap ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
    bm = conn.execute(
        "SELECT rows, cols, created_at FROM basemap WHERE id = 1").fetchone()
    tile_count = conn.execute("SELECT COUNT(*) AS n FROM tiles").fetchone()["n"]
    if bm is not None and tile_count > 0:
        # 现行图作为版本 1；无差量依据（它是该代数的基线版本）。
        # 瓦片自身版本号保留不动，全局发版号独立从 1 起。
        conn.execute(
            "INSERT INTO meta (id, generation, head_version) VALUES (1, 1, 1)")
        conn.execute(
            "INSERT INTO releases (id, generation, created_at, note) "
            "VALUES (1, 1, ?, 'legacy_migration')", (bm["created_at"],))


def init_db(db_path: str) -> None:
    """建目录、开 WAL、建表并迁移旧库。WAL 让读写不互斥，写事务之间仍严格串行。"""
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        _migrate_legacy(conn)
        conn.executescript(SCHEMA)
    finally:
        conn.close()
