"""近期错误环(诊断包「近期错误摘要」数据源, 2026-10-25 补强)。

两个采集面:
- logging Handler: 挂到 root logger, 捕获 WARNING/ERROR 记录(logger name 即来源);
- bus 订阅: memory.degraded 等降级事件(进程内事件总线上广播的异常信号)。

只留最近 N 条(环形), 供 GET /api/v1/diagnostics/export 输出; 输出前经
sanitizer 处理, 事件消息本身不允许携带密钥明文(采集时就截断限长)。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone

MAX_MESSAGE = 300      # 单条截断, 防止超长 traceback 撑爆诊断包
BUS_ERROR_TOPICS = ("memory.degraded",)

ERROR = "error"
WARN = "warning"


class _LogRingHandler(logging.Handler):
    def __init__(self, ring: "RecentErrors"):
        super().__init__(level=logging.WARNING)
        self.ring = ring

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 纯转发
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        self.ring.record(WARN if record.levelno < logging.ERROR else ERROR,
                         source=record.name, message=msg)


class RecentErrors:
    def __init__(self, capacity: int = 50):
        self._ring: deque[dict] = deque(maxlen=capacity)
        self._handler: _LogRingHandler | None = None

    # ---------- 采集 ----------
    def record(self, level: str, source: str, message: str) -> None:
        msg = " ".join(str(message).split())[:MAX_MESSAGE]
        self._ring.append({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "epoch": time.time(),
            "level": level,
            "source": str(source)[:80],
            "message": msg,
        })

    def attach_logging(self, logger: logging.Logger | None = None) -> None:
        """挂到 root logger(幂等: 重复调用不重复挂)。"""
        root = logger or logging.getLogger()
        if any(isinstance(h, _LogRingHandler) for h in root.handlers):
            return
        h = _LogRingHandler(self)
        h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(h)
        self._handler = h

    def attach_bus(self, bus) -> None:
        """订阅总线降级/异常类事件。"""
        def _on_degraded(evt: dict) -> None:
            data = evt.get("data") or {}
            self.record(WARN, f"bus:{evt.get('topic', '?')}", str(data))
        for topic in BUS_ERROR_TOPICS:
            bus.subscribe(topic, _on_degraded)

    # ---------- 输出 ----------
    def snapshot(self, limit: int = 20) -> list[dict]:
        return list(self._ring)[-limit:]

    @property
    def count(self) -> int:
        return len(self._ring)
