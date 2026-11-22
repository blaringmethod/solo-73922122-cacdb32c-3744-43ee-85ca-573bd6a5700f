"""坡度差量纯函数单元测试（app/slope.py）。"""

from __future__ import annotations

from app.slope import (build_transition_delta, candidate_cells,
                       diff_elevations, merge_transition_deltas,
                       stitch_grid)
from tests.conftest import full_grid, tile_grid


def make_tiles(rows, cols, value=100):
    return {(r, c): tile_grid(value) for r in range(rows) for c in range(cols)}


def modified_grid(rows, cols, edits, value=100):
    """整图上修改若干格点 (row,col)->new_value。"""
    grid = [row[:] for row in full_grid(rows, cols, value)]
    for r, c, v in edits:
        grid[r][c] = v
    return grid


# ---- 整图拼接与共边去重 ---------------------------------------------------

def test_stitch_seam_point_single_coordinate():
    # 1×2：共边上的物理点在两块里重复存储，拼接后整图只有一个坐标
    tiles = make_tiles(1, 2)
    grid = stitch_grid(1, 2, tiles)
    assert len(grid) == 17 and len(grid[0]) == 33
    assert grid[8][16] == 100  # 共边点只占一个整图坐标


def test_diff_elevations_reports_each_point_once():
    old = modified_grid(1, 1, [])
    new = modified_grid(1, 1, [(5, 5, 150), (8, 8, 90)])
    changed = diff_elevations(old, new)
    assert changed == [(5, 5, 100, 150), (8, 8, 100, 90)]


# ---- 候选集：只检查四邻发生高程变化的格点 ---------------------------------

def test_candidates_are_four_neighbors_and_interior_only():
    # 变化点在内部 (5,5)：候选恰为其四邻
    changed = diff_elevations(modified_grid(1, 1, []),
                              modified_grid(1, 1, [(5, 5, 150)]))
    cands = candidate_cells(changed, 17, 17)
    assert cands == {(4, 5), (6, 5), (5, 4), (5, 6)}


def test_candidates_exclude_border_points_without_full_neighborhood():
    # 变化点落在边界 (0,0)：它的四邻 (0,1)/(1,0) 等本身也缺邻（贴边或在边上），
    # 没有任何四邻齐全的内部格点 -> 候选集为空，边界点一律不供数
    changed = diff_elevations(modified_grid(1, 1, []),
                              modified_grid(1, 1, [(0, 0, 150)]))
    cands = candidate_cells(changed, 17, 17)
    assert cands == set()


def test_candidates_near_border_keep_only_interior():
    # 变化点 (1,1)：四邻中 (0,1)/(1,0) 贴边被排除，只剩 (2,1)/(1,2)
    changed = diff_elevations(modified_grid(1, 1, []),
                              modified_grid(1, 1, [(1, 1, 150)]))
    cands = candidate_cells(changed, 17, 17)
    assert cands == {(2, 1), (1, 2)}
    assert all(1 <= r <= 15 and 1 <= c <= 15 for r, c in cands)


# ---- 分量计算：东减西、南减北 ---------------------------------------------

def test_transition_component_values_east_minus_west_south_minus_north():
    old = modified_grid(1, 1, [])
    new = modified_grid(1, 1, [(5, 5, 150)])  # 北邻 (4,5) 的南邻抬高 50
    changed, rows_ = build_transition_delta(old, new)
    assert len(changed) == 1
    by = {(d["row"], d["col"]): d for d in rows_}
    # 北邻 (4,5)：南减北 = 150-100 = 50；东西未变，东减西 = 0
    assert by[(4, 5)]["new_ns"] == 50 and by[(4, 5)]["new_ew"] == 0
    # 南邻 (6,5)：南减北 = 100-150 = -50
    assert by[(6, 5)]["new_ns"] == -50
    # 西邻 (5,4)：东减西 = 150-100 = 50
    assert by[(5, 4)]["new_ew"] == 50 and by[(5, 4)]["new_ns"] == 0
    # 东邻 (5,6)：东减西 = 100-150 = -50
    assert by[(5, 6)]["new_ew"] == -50
    # 变化点本身不在候选里（它自己的四邻都没变化）
    assert (5, 5) not in by
    # 旧分量全为 0（全等值底图）
    assert all(d["old_ew"] == 0 and d["old_ns"] == 0 for d in rows_)


def test_border_points_never_supplied():
    old = modified_grid(1, 1, [])
    new = modified_grid(1, 1, [(0, 0, 150), (16, 16, 50)])
    _, rows_ = build_transition_delta(old, new)
    for d in rows_:
        assert 1 <= d["row"] <= 15 and 1 <= d["col"] <= 15


def test_seam_change_emits_each_grid_point_once():
    # 1×2 底图，在共边整列坐标 c=16 上改一个点，差量坐标必须唯一
    old = modified_grid(1, 2, [])
    new = modified_grid(1, 2, [(8, 16, 300)])
    _, rows_ = build_transition_delta(old, new)
    coords = [(d["row"], d["col"]) for d in rows_]
    assert len(coords) == len(set(coords))
    assert (8, 16) not in coords  # 变化点自身不在候选


# ---- 相抵与回改：分量相同则跳过 -------------------------------------------

def test_offsetting_neighbor_changes_cancel_component():
    # (5,4) 与 (5,6) 同时 +50：格点 (5,5) 的东减西 = (150)-(150)=0，相抵跳过
    old = modified_grid(1, 1, [])
    new = modified_grid(1, 1, [(5, 4, 150), (5, 6, 150)])
    _, rows_ = build_transition_delta(old, new)
    assert (5, 5) not in {(d["row"], d["col"]) for d in rows_}


def test_reverted_change_yields_empty_merged_delta():
    # 第一次发版改 (5,5)->150，第二次回改 ->100；v1->v3 合并差量为空
    v1 = modified_grid(1, 1, [])
    v2 = modified_grid(1, 1, [(5, 5, 150)])
    v3 = [row[:] for row in v1]
    _, d1 = build_transition_delta(v1, v2)
    _, d2 = build_transition_delta(v2, v3)
    merged = merge_transition_deltas(d1 + d2)
    assert merged == []


def test_merge_multi_transition_keeps_earliest_old_and_latest_new():
    v1 = modified_grid(1, 1, [])
    v2 = modified_grid(1, 1, [(5, 5, 150)])
    v3 = modified_grid(1, 1, [(5, 5, 150), (9, 9, 60)])
    _, d1 = build_transition_delta(v1, v2)
    _, d2 = build_transition_delta(v2, v3)
    merged = merge_transition_deltas(d1 + d2)
    by = {(d["row"], d["col"]): d for d in merged}
    # (4,5) 只在区间 1 出现，目标分量保留；(8,9) 只在区间 2 出现
    assert by[(4, 5)]["south_minus_north"] == 50
    assert by[(8, 9)]["south_minus_north"] == -40
    # 每个坐标只出现一次
    assert len(merged) == len({(d["row"], d["col"]) for d in merged})


def test_merge_empty_when_no_records():
    assert merge_transition_deltas([]) == []
