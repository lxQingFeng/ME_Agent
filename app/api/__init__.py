"""app.api —— FastAPI 服务层总装。

create_app(ctx) 装配全部路由:
  /api/v1/world/*    世界心跳(状态/时间线/活动图)
  /api/v1/chat/*     对话流(会话/消息/测试发送)
  /api/v1/plugins/*  插件技能(列表/启停/手动调用)
  /api/internal/*    插件沙箱回连(X-Internal-Token)
  /api/v1/programs/* 第三方程序(状态/动作/日志/全量分页)
  /api/v1/settings/* LLM/人设设置(保存热生效, 密钥脱敏)
  /api/v1/tts/*      TTS 全套(模型/配置/检查/试合成/克隆)
  /api/v1/system/*   健康检查/全系统状态/引擎控制
  /api/v1/diagnostics/export  诊断包导出(密钥全部脱敏为 hasXxx 指纹)
  /api/ws            控制台实时推送(WS)
  /onebot            OneBot v11 反向 WS(SnowLuma 接入)
  /console           控制台前端(静态托管, 纯离线单页)

CORS 全开:控制台以 file:// 打开时 Origin=null, 需要显式放行。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import chat, diagnostics, plugins, programs, settings, system, tts, world
from app.api.ws import ws_router
from app.memory.webhooks import webhook_router


def create_app(ctx) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await ctx.startup()
        try:
            yield
        finally:
            await ctx.shutdown()

    app = FastAPI(title="ME_Agent Console", version="1.0.0", lifespan=lifespan)
    app.state.ctx = ctx

    # ---- CORS(file:// 控制台 Origin=null, 必须显式放行) ----
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- REST 路由 ----
    app.include_router(world.router)
    app.include_router(chat.router)
    app.include_router(plugins.router)
    app.include_router(programs.router)
    app.include_router(tts.router)
    app.include_router(system.router)
    app.include_router(diagnostics.router)
    app.include_router(settings.router)   # 第 11 轮: LLM/人设设置(保存热生效)
    app.include_router(webhook_router(ctx.settings, ctx.bus))

    # ---- WS 路由 ----
    app.include_router(ws_router(ctx.bus))

    # ---- OneBot v11 反向 WS(SnowLuma 接入口; forward 模式下拒绝接入防误配) ----
    ob = ctx.settings.channels.get("onebot", {})
    ws_path = ob.get("ws_path", "/onebot")
    gw = ctx.gateway

    @app.websocket(ws_path)
    async def onebot_endpoint(ws: WebSocket) -> None:
        await gw.handle(ws)

    # ---- 控制台前端静态托管(纯静态单页, 双击 file:// 亦可) ----
    console_dir = Path(ctx.settings.root) / "console"
    if console_dir.is_dir():
        app.mount("/console", StaticFiles(directory=str(console_dir), html=True), name="console")

    @app.get("/")
    def index() -> dict:
        return {"ok": True, "service": "ME_Agent Console",
                "console": "/console/index.html", "docs": "/docs",
                "onebot_ws": ws_path, "push_ws": "/api/ws"}

    return app
