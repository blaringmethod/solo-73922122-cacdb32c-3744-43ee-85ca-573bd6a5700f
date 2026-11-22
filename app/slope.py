"""坡度差量纯函数模块：不触碰数据库，便于单测。

整图坐标系：矩形底图拼接后为 H×W 个格点（H=16·rows+1, W=16·cols+1），
行列均从 0 开始。相邻瓦片共边上的点物理上是同一个点，拼接后只占一个
整图坐标，因此"共边上的同一格点只返回一次"由整图坐标天然保证。

坡度分量（仅四邻齐全的内部格点 1 <= r <= H-2, 1 <= c <= W-2）：
    east_minus_west(r, c) = elev(r, c+1) - elev(r, c-1)   # 东减西
    south_minus_north(r, c) = elev(r+1, c) - elev(r-1, c) # 南减北

差量生成的候选集合（题面"只检查四邻曾在这两版之间发生高程变化的格点"）：
若格点 p 的四邻在两版之间高程均未变化，则 p 的两个坡度分量恒定，无需检查；
故候选集 = 高程变化格点的四邻 ∩ 四邻齐全的内部格点。
"""

from __future__ import annotations

from .solver import TILE

# 四邻偏移：上（北）、下（南）、左（西）、右（东）
_NEIGHBORS = ((-1, 0), (1, 0), (0, -1), (0, 1))


def stitch_grid(rows, cols, tiles):
    """把带共边重复的 17×17 瓦片拼成 (16·rows+1)×(16·cols+1) 整图。

    共边上的点在相邻瓦片中值相同（录入/发版已保证接缝吻合），拼接时
    后写覆盖写，整图中每个物理格点只保留一个坐标。
    """
    gh, gw = 16 * rows + 1, 16 * cols + 1
    grid = [[0] * gw for _ in range(gh)]
    for r in range(rows):
        for c in range(cols):
            data = tiles[(r, c)]
            base_r, base_c = 16 * r, 16 * c
            for i in range(TILE):
                dst, src = grid[base_r + i], data[i]
                for j in range(TILE):
                    dst[base_c + j] = src[j]
    return grid


def diff_elevations(old_grid, new_grid):
    """对比两张同尺寸整图，返回高程发生变化的格点列表。

    返回: [(row, col, old_value, new_value), ...]，按行列排序。
    """
    changed = []
    for r, (orow, nrow) in enumerate(zip(old_grid, new_grid)):
        for c, (ov, nv) in enumerate(zip(orow, nrow)):
            if ov != nv:
                changed.append((r, c, ov, nv))
    return changed


def candidate_cells(changed_cells, height, width):
    """由高程变化格点求坡度差量候选集（四邻齐全且至少一个邻点高程变化）。"""
    changed_coords = {(r, c) for r, c, _ov, _nv in changed_cells}
    candidates = set()
    for r, c in changed_coords:
        for dr, dc in _NEIGHBORS:
            nr, nc = r + dr, c + dc
            if 1 <= nr <= height - 2 and 1 <= nc <= width - 2:
                candidates.add((nr, nc))
    return candidates


def components(grid, r, c):
    """计算格点 (r,c) 的 (东减西, 南减北) 两个坡度分量。调用方保证四邻齐全。"""
    east_minus_west = grid[r][c + 1] - grid[r][c - 1]
    south_minus_north = grid[r + 1][c] - grid[r - 1][c]
    return east_minus_west, south_minus_north


def build_transition_delta(old_grid, new_grid):
    """计算相邻两版之间的坡度差量依据。

    流程：
      1. 找出两版间所有高程变化格点；
      2. 候选集 = 变化格点的四邻 ∩ 内部格点（四邻齐全）；
      3. 逐候选点比对两版分量，分量有变化才输出。
         —— 跨版回改（分量回到原值）或高程相抵（分量恰好相同）在此被跳过。

    返回: (changed_cells, delta_rows)
      changed_cells: [(row, col, old_value, new_value), ...]
      delta_rows:    [{"row","col","old_ew","new_ew","old_ns","new_ns"}, ...]
                     仅含分量发生变化的内部格点，按行列排序。
    """
    height, width = len(old_grid), len(old_grid[0])
    changed_cells = diff_elevations(old_grid, new_grid)
    delta_rows = []
    for r, c in sorted(candidate_cells(changed_cells, height, width)):
        old_ew, old_ns = components(old_grid, r, c)
        new_ew, new_ns = components(new_grid, r, c)
        if old_ew != new_ew or old_ns != new_ns:
            delta_rows.append({"row": r, "col": c,
                               "old_ew": old_ew, "new_ew": new_ew,
                               "old_ns": old_ns, "new_ns": new_ns})
    return changed_cells, delta_rows


def merge_transition_deltas(transition_rows):
    """合并若干相邻版差量依据，得到任意两版间的最终差量。

    参数: 按 from_version 升序排列的差量行（含 row/col/old_*/new_*），
          同一格点在每个相邻版区间至多出现一次。
    规则: 每格点 old 取最早区间的旧值、new 取最晚区间的新值；
          合并后分量相同（多次发版间回改或相抵）则跳过；每格点只输出一次。

    返回: [{"row","col","east_minus_west","south_minus_north"}, ...]，按行列排序。
    """
    merged = {}
    for rec in transition_rows:
        key = (rec["row"], rec["col"])
        if key not in merged:
            merged[key] = {"old_ew": rec["old_ew"], "old_ns": rec["old_ns"]}
        merged[key]["new_ew"] = rec["new_ew"]
        merged[key]["new_ns"] = rec["new_ns"]

    changes = []
    for (r, c) in sorted(merged):
        v = merged[(r, c)]
        if v["new_ew"] != v["old_ew"] or v["new_ns"] != v["old_ns"]:
            changes.append({"row": r, "col": c,
                            "east_minus_west": v["new_ew"],
                            "south_minus_north": v["new_ns"]})
    return changes
