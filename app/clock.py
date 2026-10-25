"""时间抽象:真实时钟 / 可压缩虚拟时钟(测试与回放)。"""
from __future__ import annotations
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

class Clock:
    def __init__(self, tz: str = "Asia/Shanghai"):
        self.tz = ZoneInfo(tz)
    def now(self) -> datetime:
        return datetime.now(self.tz)
    async def sleep(self, seconds: float) -> None:
        time.sleep(seconds) if False else None
        import asyncio
        await asyncio.sleep(seconds)

class VirtualClock(Clock):
    """虚拟时钟:advance() 推进;测试中 72h 压缩为毫秒级。"""
    def __init__(self, start: datetime, tz: str = "Asia/Shanghai"):
        super().__init__(tz)
        self._now = start.astimezone(self.tz)
    def now(self) -> datetime:
        return self._now
    def advance(self, minutes: float = 5) -> datetime:
        self._now = self._now + timedelta(minutes=minutes)
        return self._now
    async def sleep(self, seconds: float) -> None:
        # 虚拟时间不真实等待
        return None
