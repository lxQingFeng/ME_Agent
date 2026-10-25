"""AppContext:全系统装配与生命周期。

所有服务(心跳引擎/仲裁器/OneBot 网关/记忆/Obsidian/插件/程序管理/TTS)
在此构造并互相接线;FastAPI 应用与启动入口只依赖本上下文。

接线总览(方案 §2 总装图):
  OneBotGateway ──onebot.message──▶ Arbiter ──▶ DialoguePipeline ──▶ OutboundShaper ──▶ Gateway
                                       │              │                    │
                                       ▼              ▼                    ▼
                              HeartbeatEngine    LLM+Memory          console.db / bus
                                       │
                                       ▼
                              EventBus ──▶ /api/ws 推送
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.config import Settings
from app.bus import EventBus
from app.clock import Clock
from app.core.arbiter import Arbiter
from app.core.store import Store
from app.dialogue.llm import LLMClient
from app.dialogue.pipeline import DialoguePipeline
from app.gateway.server import OneBotGateway
from app.heartbeat.engine import HeartbeatEngine
from app.heartbeat.eventlog import EventLog
from app.memory.hindsight import MemoryService
from app.memory.obsidian import ObsidianChannel
from app.outbound.shaper import OutboundShaper
from app.plugins.registry import PluginRegistry
from app.programs.manager import ProgramManager
from app.telemetry.errors import RecentErrors
from app.tts.bridge import TTSService
from app.tts.store import ProviderStore


class AppContext:
    """运行时全系统句柄。create_app(ctx) 与 python -m app 共用。"""

    def __init__(self, settings: Settings, clock: Clock | None = None,
                 start_engine: bool | None = None):
        self.settings = settings
        self.started_at = time.time()
        self.bus = EventBus()

        # ---- 存储 ----
        self.store = Store(settings.state_dir / "console.db")
        self.eventlog = EventLog(settings.state_dir / "world.db")

        # ---- 时钟(测试可注入 VirtualClock) ----
        self.clock = clock or Clock(settings.tz)

        # ---- LLM(未配 key 时 enabled=False, 全链路优雅降级) ----
        self.llm = LLMClient(settings.llm_cfg, settings.llm_api_key())
        self.llm_or_none = self.llm if self.llm.enabled else None

        # ---- 世界心跳引擎 ----
        self.engine = HeartbeatEngine(settings, self.clock, self.eventlog,
                                      bus=self.bus, llm=self.llm_or_none)

        # ---- 记忆 / Obsidian ----
        self.memory = MemoryService(settings, bus=self.bus)
        self.obsidian = ObsidianChannel(settings, self.memory, bus=self.bus)

        # ---- OneBot 网关 + 输出整形 ----
        self.gateway = OneBotGateway(settings, self.bus)
        self.outbound = OutboundShaper(settings, self.gateway, self.store, self.bus)

        # ---- 插件 ----
        self.plugins = PluginRegistry(settings, bus=self.bus)

        # ---- TTS(Provider 档案含密钥 → state/tts/, 与 Obsidian vault 严格分离) ----
        self.tts_store = ProviderStore(settings.state_dir / "tts",
                                       default_cfg=settings.tts_cfg)
        self.tts = TTSService(settings, self.tts_store, outbound=self.outbound)

        # ---- 对话管道 / 仲裁器 ----
        self.pipeline = DialoguePipeline(settings, self.engine, self.llm_or_none,
                                         self.memory, self.plugins, self.outbound,
                                         self.store, self.engine.renderer, self.bus,
                                         tts=self.tts)
        self.arbiter = Arbiter(settings, self.engine, self.pipeline,
                               self.store, self.bus, self.outbound)

        # ---- 第三方程序 ----
        self.programs = ProgramManager(settings, bus=self.bus, memory=self.memory,
                                       obsidian=self.obsidian, gateway=self.gateway)
        # Obsidian 同步结果写入程序日志(state/programs/logs/obsidian_sync.log),
        # 控制台「日志」按钮即可看到每次同步的成败摘要(失败不再静默)。
        self.obsidian.set_log_hook(
            lambda line: self.programs.log_program("obsidian_sync", line))

        # ---- 近期错误环(诊断导出数据源: logging + bus 降级事件) ----
        self.telemetry = RecentErrors(capacity=50)
        self.telemetry.attach_logging()
        self.telemetry.attach_bus(self.bus)

        # ---- 生命周期 ----
        if start_engine is None:
            start_engine = bool(settings.console.get("engine", {}).get("auto_start", True))
        self.start_engine = start_engine
        self.tasks: list[asyncio.Task] = []
        self._started = False

    # ================= 启动 / 停止 =================

    async def startup(self) -> None:
        if self._started:
            return
        self._started = True
        self.bus.attach_loop(asyncio.get_running_loop())

        # 引擎回调接线: 心拍 retain / 冒泡候选
        self.engine.on_retain = self._engine_retain
        self.engine.on_broadcast = self._engine_broadcast

        # 世界心跳主循环
        if self.start_engine:
            self.tasks.append(asyncio.create_task(self.engine.run(), name="engine"))

        # 程序监控(进程状态轮询 + program.status 事件)
        await self.programs.start_monitor()

        # 通道 forward 模式: 正向外连循环(断线自动重连自愈)
        t = self.gateway.start_forward()
        if t is not None:
            self.tasks.append(t)

        # 恢复自动启停的第三方程序(如 obsidian 同步 internal 任务)
        try:
            await self.programs.autostart()
        except Exception:
            pass

        # 记忆 bank 自愈(后台, 失败不影响启动)
        asyncio.create_task(self._safe_ensure_banks(), name="ensure-banks")

        self.bus.emit("system.started", {"pid": True, "ts": time.time()})

    async def shutdown(self) -> None:
        # 先停 forward 外连循环(优雅关闭当前连接, 不留僵尸任务)
        try:
            await self.gateway.stop_forward()
        except Exception:
            pass
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            try:
                await asyncio.wait_for(t, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        self.engine.stop()
        try:
            await self.programs.close()
        except Exception:
            pass
        try:
            await self.memory.close()
        except Exception:
            pass
        try:
            await self.llm.close()
        except Exception:
            pass
        try:
            self.store.close()
        except Exception:
            pass
        try:
            self.eventlog.close()
        except Exception:
            pass
        self._started = False

    # ================= 引擎回调 =================

    async def _engine_retain(self, payload: str, kind: str, meta: dict) -> None:
        try:
            await self.memory.retain(payload, kind, meta)
        except Exception:
            pass    # Hindsight 不在线时静默丢弃(优雅降级)

    async def _engine_broadcast(self, text: str, ctx: dict) -> None:
        try:
            await self.arbiter.handle_broadcast_candidate(text, ctx)
        except Exception:
            pass

    async def _safe_ensure_banks(self) -> None:
        try:
            await self.memory.ensure_banks()
        except Exception:
            pass

    # ================= 状态汇总 =================

    def status(self) -> dict:
        engine_cfg = self.settings.console.get("engine", {})
        return {
            "ok": True,
            "started_at": self.started_at,
            "uptime_s": round(time.time() - self.started_at, 1),
            "llm_enabled": self.llm.enabled,
            "memory": self.memory.status(),
            "gateway": self.gateway.status(),
            "engine": {
                "running": bool(getattr(self.engine, "_running", False)),
                "tick_count": self.engine.tick_count,
                "tick_seconds": int(engine_cfg.get("tick_seconds", 300)),
                "clock": type(self.clock).__name__,
                "sleep_state": self.engine.state.sleep_state,
            },
            "programs": {"count": len(self.programs.list())},
            "plugins": {"count": len(self.plugins.status())},
            "tts": self.tts.status(),
        }


def build_context(root: Path | None = None, state_dir: Path | None = None,
                  clock: Clock | None = None, start_engine: bool | None = None) -> AppContext:
    """默认装配入口(生产/冒烟共用)。"""
    settings = Settings(root=root, state_dir=state_dir)
    return AppContext(settings, clock=clock, start_engine=start_engine)
