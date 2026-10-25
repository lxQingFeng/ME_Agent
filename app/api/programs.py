"""第三方程序路由:状态 / 动作(install|update|start|stop|restart|integrate|sync) / 日志。

长动作(install/update)经 manager.run_action_async 后台执行: 立即返回
{queued: true, status: "busy"}, HTTP 不等待下载/拉镜像完成;
调用方轮询 GET /api/v1/programs 观察状态(busy → installed/running)与
GET /api/v1/programs/{pid}/logs 尾部日志; busy 期间的重复触发会被拒绝。
sync = manual_sync 程序(obsidian_sync)的「立即同步」。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/v1/programs", tags=["programs"])

ACTIONS = ("install", "update", "start", "stop", "restart", "integrate", "sync")
LONG_ACTIONS = ("install", "update")   # 下载/拉镜像类: 后台任务 + 状态轮询


@router.get("")
def list_programs(request: Request):
    ctx = request.app.state.ctx
    return ctx.programs.list()


@router.post("/{pid}/{action}")
async def program_action(request: Request, pid: str, action: str):
    ctx = request.app.state.ctx
    if action not in ACTIONS:
        raise HTTPException(status_code=400, detail=f"action must be one of {ACTIONS}")
    m = ctx.programs
    if action in LONG_ACTIONS:
        out = await m.run_action_async(pid, action)
    else:
        out = await m.guarded_action(pid, action)
    return {"ok": bool(out.get("ok")), "id": pid, "action": action, "result": out}


@router.get("/{pid}/logs")
def program_logs(request: Request, pid: str, limit: int = 200, offset: int | None = None):
    """程序日志(第 11 轮升级: 全量/分页可读, 37 能翻到启动段拿 WebUI 凭据)。

    - 不传 offset(默认): 尾部语义 —— 返回末尾 limit 行(V4 console 兼容);
    - offset>=0: 行号分页 —— lines[offset : offset+limit], has_more 指示翻页;
    - limit=0: 从 offset(缺省 0)读到结尾(全量);
    - 响应恒带 total 总行数, offset 回真实起始行号。
    """
    ctx = request.app.state.ctx
    page = ctx.programs.logs_page(pid, offset=offset, limit=limit)
    return {"id": pid, "total": page["total"], "offset": page["offset"],
            "has_more": page["has_more"], "lines": page["lines"]}


@router.get("/{pid}/check-latest")
async def check_latest(request: Request, pid: str):
    """查上游最新版本(不动本地)。"""
    ctx = request.app.state.ctx
    return await ctx.programs.check_latest(pid)
