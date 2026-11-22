"""高程瓦片供数 API（FastAPI 入口）。

业务流程：录入底图（版本 1）-> 按块暂存修测 -> 点名一块发版（版本号 +1，
高程与坡度差量依据同事务生效）-> 整图/窗口/差量取数。
交互式文档见 /docs（Swagger UI，可在其中完整演示下游一次追平多次发版）。
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, RedirectResponse

from .models import BasemapCreate, PublishRequest, StageCreate
from .service import (ElevationService, InvalidShape, ServiceError,
                      tiles_from_grid)

DEFAULT_DB_PATH = "./data/elev.db"


def create_app(db_path: str | None = None) -> FastAPI:
    service = ElevationService(
        db_path or os.environ.get("ELEV_DB_PATH", DEFAULT_DB_PATH))

    app = FastAPI(
        title="高程瓦片供数服务",
        version="2.0.0",
        summary="面向坡度仓的高程供数与坡度差量 API",
        description=(
            "矩形底图按行列划分瓦片，每块 17×17 个整数高程点，相邻块重复存储共边。"
            "数据员按块暂存修测，再点名一块发版：点名块必须换上最新暂存版，"
            "其余块可保留或换上各自最新暂存版，服务自动选出换块最少且所有共边"
            "逐点吻合的方案整批生效；无解时报告冲突边两侧瓦片坐标且不改任何数据。\n\n"
            "**版本与差量**：录入底图即全局版本 1，每次成功发版版本号 +1；"
            "整图取数与发版响应均带版本号。发版时在同一事务内生成并落库坡度"
            "差量依据——仅四邻齐全的内部格点供数，分量取东减西、南减北的高程差；"
            "只检查四邻曾在两版间发生高程变化的格点，分量回改或相抵后相同的格点"
            "自动跳过；共边格点在整图坐标下天然唯一。重启后按同一对版号取到同一"
            "差量；重录底图后旧版号查询返回 409 并提示全量重建。\n\n"
            "暂存、发版、整图取数并发时满足可串行化。"))

    @app.exception_handler(ServiceError)
    async def _service_error_handler(_, exc: ServiceError):
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": exc.detail()})

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    # ---- 底图 ------------------------------------------------------------

    @app.post("/basemap", status_code=201, tags=["底图"],
              summary="录入矩形底图（接缝必须吻合），即全局版本 1")
    def create_basemap(body: BasemapCreate):
        if body.grid is not None:
            rows, cols, tiles = tiles_from_grid(body.grid, body.rows, body.cols)
        else:
            rows, cols = body.rows, body.cols
            tiles = {}
            for t in body.tiles:
                if (t.row, t.col) in tiles:
                    raise InvalidShape(f"瓦片 ({t.row},{t.col}) 重复提供")
                tiles[(t.row, t.col)] = t.data
        return service.create_basemap(rows, cols, tiles)

    @app.delete("/basemap", tags=["底图"], summary="删除底图及全部版本/差量（重录用）")
    def reset_basemap():
        return service.reset()

    @app.get("/basemap", tags=["底图"], summary="底图概况（尺寸、代数、当前版本号）")
    def get_basemap():
        return service.get_basemap()

    @app.get("/basemap/grid", tags=["取数"],
             summary="整图取数：拼接后的完整高程面（带当前版本号）")
    def get_grid():
        return service.get_grid()

    @app.get("/basemap/window", tags=["取数"],
             summary="跨瓦片取点：整图坐标系下的矩形窗口（含端点，带版本号）")
    def get_window(row0: int = Query(ge=0), col0: int = Query(ge=0),
                   row1: int = Query(ge=0), col1: int = Query(ge=0)):
        return service.get_window(row0, col0, row1, col1)

    @app.get("/tiles/{row}/{col}", tags=["取数"],
             summary="单块取数（含瓦片版本号与当前全局发版号）")
    def get_tile(row: int, col: int):
        return service.get_tile(row, col)

    # ---- 暂存 ------------------------------------------------------------

    @app.post("/tiles/{row}/{col}/staged", status_code=201, tags=["暂存"],
              summary="暂存一版修测（可多次暂存，发版取最新）")
    def stage(row: int, col: int, body: StageCreate):
        return service.stage(row, col, body.data)

    @app.get("/tiles/{row}/{col}/staged", tags=["暂存"],
             summary="查看某块的暂存版本列表")
    def list_tile_staged(row: int, col: int):
        return service.list_staged(row, col)

    @app.get("/staged", tags=["暂存"], summary="查看全部暂存版本")
    def list_all_staged():
        return service.list_staged()

    # ---- 发版 ------------------------------------------------------------

    @app.post("/publish", tags=["发版"],
              summary="点名一块发版：换块最少且共边吻合，高程与坡度差量同事务生效")
    def publish(body: PublishRequest):
        return service.publish(body.row, body.col)

    # ---- 坡度差量 --------------------------------------------------------

    @app.get("/delta", tags=["坡度差量"],
             summary="差量取数：两版间坡度分量有变化的整图格点坐标及目标值")
    def get_delta(
        from_version: int = Query(
            ..., ge=1, description="已入仓（下游已追平）的版本号"),
        to_version: int | None = Query(
            default=None, ge=1,
            description="目标版本号；省略时取当前最新版本"),
    ):
        """返回 from_version→to_version 的坡度差量，供下游直接更新坡度仓。

        - 仅返回四邻齐全的内部格点（整图最外一圈无四邻，不供数）；
        - 分量定义：东减西 = 东邻高程 − 西邻高程，南减北 = 南邻 − 北邻；
        - 每个整图坐标至多返回一次；跨版回改或相抵后分量相同的格点跳过；
        - 只检查四邻曾在这两版之间发生高程变化的格点（发版时已落库依据）；
        - 版号不存在或属于重录前的旧底图：409 `version_unavailable`，
          响应带 `rebuild_required=true`，下游须按当前整图全量重建。

        下游追平多次发版：持续以"本地已追平版号"为 from_version、当前版为
        to_version 调用本接口，应用 changes 后把本地版号更新为 to_version 即可。
        """
        return service.get_delta(from_version, to_version)

    return app


app = create_app()
