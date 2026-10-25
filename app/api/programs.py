"""第三方程序路由:状态 / 动作(install|update|start|stop|restart|integrate) / 日志。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/v1/programs", tags=["programs"])

ACTIONS = ("install", "update", "start", "stop", "restart", "integrate")


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
    try:
        if action == "install":
            out = await m.install(pid)
        elif action == "update":
            out = await m.update(pid)
        elif action == "start":
            out = await m.start(pid)
        elif action == "stop":
            out = await m.stop(pid)
        elif action == "restart":
            out = await m.restart(pid)
        else:
            out = await m.integrate(pid)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"program {pid} not found") from None
    return {"ok": bool(out.get("ok")), "id": pid, "action": action, "result": out}


@router.get("/{pid}/logs")
def program_logs(request: Request, pid: str, limit: int = 80):
    ctx = request.app.state.ctx
    return {"id": pid, "lines": ctx.programs.logs(pid, limit)}


@router.get("/{pid}/check-latest")
async def check_latest(request: Request, pid: str):
    """查上游最新版本(不动本地)。"""
    ctx = request.app.state.ctx
    return await ctx.programs.check_latest(pid)
