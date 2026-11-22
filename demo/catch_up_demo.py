#!/usr/bin/env python3
"""下游坡度仓追平演示：一次追平多次发版（仅依赖标准库，容器内外均可运行）。

流程：
  1. 录入一幅 2×2 底图（全局版本 1，全图高程 100，坡度分量全为 0）；
  2. 连续三次发版：
       v2：瓦片 (0,0) 内部点抬高；
       v3：另一瓦片内部点下沉；
       v4：把 v2 的修测回改（演示分量回到原值后差量自动跳过）；
  3. 下游坡度仓只保存"已入仓版号" local_version，用 GET /delta 一次取回
     local_version -> 当前版 的全部变化，按坐标应用目标分量后更新版号；
  4. 同时演示：重录底图后拿旧版号查询，服务返回 409 要求全量重建。

用法：
  python3 demo/catch_up_demo.py [BASE_URL]   # 默认 http://localhost:8000
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
TILE = 17


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def tile(value=100):
    return [[value] * TILE for _ in range(TILE)]


def grid(rows, cols, value=100):
    return [[value] * (16 * cols + 1) for _ in range(16 * rows + 1)]


def stage_and_publish(r, c, data):
    call("POST", f"/tiles/{r}/{c}/staged", {"data": data})
    status, body = call("POST", "/publish", {"row": r, "col": c})
    assert status == 200, body
    return body


class SlopeWarehouse:
    """下游坡度仓：内存模拟。只认版号与差量，从不直接拉整图高程。"""

    def __init__(self):
        self.local_version = None          # 已入仓版号
        self.components = {}               # (row,col) -> (东减西, 南减北)

    def bootstrap(self, version):
        self.local_version = version      # 首次按整图全量建仓（演示从空仓起步）
        print(f"[下游] 基于版本 {version} 全量建坡度假仓（初始分量全部为 0）")

    def catch_up(self):
        status, body = call(
            "GET", f"/delta?from_version={self.local_version}")
        if status == 409 and body["detail"].get("rebuild_required"):
            print(f"[下游] 服务要求全量重建：{body['detail']['message']}")
            return "rebuild_required"
        assert status == 200, body
        applied = 0
        for ch in body["changes"]:
            key = (ch["row"], ch["col"])
            self.components[key] = (ch["east_minus_west"],
                                    ch["south_minus_north"])
            applied += 1
        print(f"[下游] {self.local_version} -> {body['to_version']}："
              f"应用 {applied} 个格点的目标分量（回改/相抵格点已由服务端跳过，"
              f"不在响应中）")
        self.local_version = body["to_version"]
        return "ok"


def main():
    print(f"== 对接服务 {BASE} ==")
    call("DELETE", "/basemap")  # 演示环境清理（无库时忽略错误）

    # 1) 录入底图 -> 版本 1
    status, info = call("POST", "/basemap", {"grid": grid(2, 2)})
    assert status == 201, info
    print(f"[服务] 录入 2×2 底图：版本 {info['version']}（代数 {info['generation']}）")

    # 2) 连续三次发版
    t = tile(); t[5][5] = 150
    v2 = stage_and_publish(0, 0, t)
    print(f"[服务] 发版 -> 版本 {v2['version']}：高程变化 "
          f"{v2['elevation_changes']} 点，坡度差量 "
          f"{v2['slope_delta_cells']} 格点")

    t = tile(); t[2][2] = 40
    v3 = stage_and_publish(1, 1, t)
    print(f"[服务] 发版 -> 版本 {v3['version']}：坡度差量 "
          f"{v3['slope_delta_cells']} 格点")

    v4 = stage_and_publish(0, 0, tile())  # 回改 v2 的修测
    print(f"[服务] 发版 -> 版本 {v4['version']}（回改 v2 修测）：坡度差量 "
          f"{v4['slope_delta_cells']} 格点（回改邻点的分量恢复，合并时将跳过）")

    # 3) 下游从版本 1 一次追平到版本 4
    wh = SlopeWarehouse()
    wh.bootstrap(1)
    assert wh.catch_up() == "ok"
    _, delta = call("GET", "/delta?from_version=1&to_version=4")
    print(f"[核对] 服务端 v1->v4 合并差量 {delta['change_count']} 格点；"
          f"下游仓实际持有 {len(wh.components)} 格点，一致："
          f"{len(wh.components) == delta['change_count']}")
    assert len(wh.components) == delta["change_count"]
    sample = next(iter(wh.components.items()))
    print(f"[核对] 样例格点 {sample[0]} 的目标分量 (东减西, 南减北) = {sample[1]}")
    # 回改点 (5,5) 的四邻分量已恢复，不应出现在最终差量里
    reverted_neighbors = {(4, 5), (6, 5), (5, 4), (5, 6)}
    assert not (reverted_neighbors & set(wh.components)), "回改格点未被跳过！"
    print("[核对] v2 修测已回改，其四邻格点均未进入最终差量：符合预期")

    # 4) 重录底图后旧版号失效 -> 全量重建提示
    call("DELETE", "/basemap")
    call("POST", "/basemap", {"grid": grid(2, 2)})
    status, body = call("GET", "/delta?from_version=1&to_version=2")
    assert status == 409 and body["detail"]["rebuild_required"] is True
    print(f"[服务] 重录底图后旧版号查询 -> HTTP 409 "
          f"{body['detail']['error']}：{body['detail']['message'][:28]}...")
    assert wh.catch_up() == "rebuild_required"
    print("\n演示完成：差量取数、多版追平、回改跳过、重录全量重建均符合预期。")


if __name__ == "__main__":
    main()
