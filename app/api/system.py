"""系统级路由:健康检查 / 全系统状态 / 引擎手动控制 / 开机联动(冒烟与联调用)。"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import BootIn, EngineControlIn

router = APIRouter(prefix="/api/v1/system", tags=["system"])


@router.get("/health")
def health() -> dict:
    return {"ok": True, "ts": time.time()}


@router.get("/status")
def status(request: Request):
    """全系统状态:引擎/网关/记忆/程序/插件/TTS 聚合(总览页数据源)。"""
    ctx = request.app.state.ctx
    return ctx.status()


@router.post("/boot")
async def system_boot(request: Request, body: BootIn | None = None, wait: bool = False):
    """开机联动(第 13 轮定稿契约, 勿改名): 手动/壳触发整套自启动编排。

    - 编排与启动自联动同源(config/programs.yaml autostart): 未安装自动安装 →
      start → integrate; 已运行跳过; 整链幂等; 失败降级不阻塞;
    - 缺省(空体)后台执行: 立即返回 {ok, queued, plan, started, skipped}; 进度与
      状态复用既有通道 —— GET /api/v1/programs 轮询 + WS program.status /
      program.progress / program.webui 推送; 完成报告 GET /api/v1/system/boot;
    - {"wait": true} 请求体或 ?wait=true: 同步等整链完成, 直接返回完整报告
      {ok, steps, started, skipped, degraded, failed}(测试/CLI 联调用)。
    """
    ctx = request.app.state.ctx
    if (body and body.wait) or wait:
        return await ctx.programs.boot(trigger="manual")
    return await ctx.programs.boot_async(trigger="manual")


@router.get("/boot")
def system_boot_status(request: Request):
    """联动状态: {status: idle|running|done, trigger, report}(壳/控制台轮询用)。"""
    ctx = request.app.state.ctx
    return ctx.programs.boot_status()


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
