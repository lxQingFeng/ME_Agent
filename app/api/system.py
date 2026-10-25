"""系统级路由:健康检查 / 全系统状态 / 引擎手动控制(冒烟与联调用)。"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import EngineControlIn

router = APIRouter(prefix="/api/v1/system", tags=["system"])


@router.get("/health")
def health() -> dict:
    return {"ok": True, "ts": time.time()}


@router.get("/status")
def status(request: Request):
    """全系统状态:引擎/网关/记忆/程序/插件/TTS 聚合(总览页数据源)。"""
    ctx = request.app.state.ctx
    return ctx.status()


@router.post("/engine")
async def engine_control(request: Request, body: EngineControlIn):
    """冒烟/调试用:手动推一拍 / 暂停 / 恢复。"""
    ctx = request.app.state.ctx
    eng = ctx.engine
    if body.action == "tick":
        report = await eng.tick()
        return {"ok": True, "action": "tick",
                "transitions": report.transitions, "errors": report.errors,
                "tick_count": eng.tick_count}
    if body.action == "pause":
        eng.stop()
        return {"ok": True, "action": "pause", "running": False}
    if body.action == "resume":
        for i, t in enumerate(ctx.tasks):
            if t.get_name() == "engine":
                t.cancel()
                ctx.tasks.pop(i)
                break
        ctx.tasks.append(_spawn_engine(eng))
        return {"ok": True, "action": "resume", "running": True}
    raise HTTPException(status_code=400, detail="unknown action")


def _spawn_engine(eng):
    import asyncio
    t = asyncio.create_task(eng.run(), name="engine")
    return t
