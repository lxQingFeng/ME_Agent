"""进程内事件总线:gateway/engine/api 之间解耦。"""
from __future__ import annotations
import asyncio
from typing import Any, Callable
from datetime import datetime, timezone

class EventBus:
    def __init__(self):
        self._subs: dict[str, list[Callable]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.history: list[dict] = []   # 环形留痕(WS 推送兜底)
        self._max_history = 200

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def subscribe(self, topic: str, handler: Callable) -> None:
        self._subs.setdefault(topic, []).append(handler)

    def emit(self, topic: str, data: Any = None) -> None:
        evt = {"topic": topic, "data": data, "ts": _utcnow_iso()}
        self.history.append(evt)
        if len(self.history) > self._max_history:
            del self.history[: self._max_history]
        for handler in self._subs.get(topic, []):
            try:
                res = handler(evt)
                if asyncio.iscoroutine(res):
                    if self.loop and self.loop.is_running():
                        asyncio.run_coroutine_threadsafe(res, self.loop)
                    else:
                        asyncio.create_task(res)
            except Exception:
                pass

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
