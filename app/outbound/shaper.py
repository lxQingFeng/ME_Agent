"""拟人整形输出器:延迟/拆条/令牌桶/广播限额/静默窗(方案 §6)。"""
from __future__ import annotations
import asyncio
import random
import re
import time

class OutboundShaper:
    def __init__(self, settings, gateway, store, bus, clock=None):
        self.settings = settings
        self.gateway = gateway
        self.store = store
        self.bus = bus
        self.clock = clock
        self.shaping = settings.shaping
        self._tokens: dict[str, tuple[float, int]] = {}   # chat -> (ts, tokens)
        self._broadcast_log: list[float] = []
        self.rng = random.Random()

    def _virtual(self) -> bool:
        return self.clock is not None and hasattr(self.clock, "advance")

    async def _typing_delay(self) -> None:
        if self._virtual():
            return
        lo, hi = self.shaping.get("typing_delay_ms", [500, 2500])
        await asyncio.sleep(self.rng.uniform(lo, hi) / 1000.0)

    async def _gap(self) -> None:
        if self._virtual():
            return
        lo, hi = self.shaping.get("inter_message_gap_ms", [1000, 3000])
        await asyncio.sleep(self.rng.uniform(lo, hi) / 1000.0)

    def _bucket_take(self, chat_id: str) -> bool:
        rate = int(self.shaping.get("group_rate_per_min", 8))
        now = time.time()
        ts, tokens = self._tokens.get(chat_id, (now, rate))
        elapsed = max(0.0, now - ts)
        tokens = min(rate, tokens + elapsed * rate / 60.0)
        if tokens < 1:
            self._tokens[chat_id] = (now, tokens)
            return False
        self._tokens[chat_id] = (now, tokens - 1)
        return True

    def split(self, text: str) -> list[str]:
        th = int(self.shaping.get("split_threshold", 120))
        max_parts = int(self.shaping.get("max_parts", 3))
        if len(text) <= th:
            return [text]
        parts = re.split(r"(?<=[。!?\n])", text)
        parts = [p for p in (x.strip() for x in parts) if p]
        out: list[str] = []
        buf = ""
        for p in parts:
            if len(buf) + len(p) <= th:
                buf += p
            else:
                if buf:
                    out.append(buf)
                buf = p
        if buf:
            out.append(buf)
        if len(out) > max_parts:
            out = out[:max_parts - 1] + ["".join(out[max_parts - 1:])[:th * 2]]
        return out

    async def send(self, chat_id: str, text: str, reply_to=None, segments=None,
                   origin: str = "agent") -> None:
        if not text and not segments:
            return
        chat = self._chat_meta(chat_id)
        chat_type = chat["chat_type"]
        parts = self._parts(text, segments)
        for i, part in enumerate(parts):
            while not self._bucket_take(chat_id):
                await asyncio.sleep(0.5 if not self._virtual() else 0)
            await self._typing_delay()
            segs = part if isinstance(part, list) else [{"type": "text", "data": {"text": part}}]
            res = await self.gateway.send_message(chat_type, chat_id, segs, reply_to=reply_to if i == 0 else None)
            ts = time.time()
            self.store.append_message(ts=ts, chat_id=chat_id, chat_type=chat_type,
                                      sender_id="agent", sender_name="小默", role="assistant",
                                      content=(part if isinstance(part, str) else "[语音]"),
                                      segments=segs, origin=origin)
            self.bus.emit("message.out", {"chat_id": chat_id, "text": part if isinstance(part, str) else "[语音]",
                                          "segments": segs, "origin": origin, "result": res})
            if i < len(parts) - 1:
                await self._gap()

    def _parts(self, text, segments):
        if segments:
            return [segments]
        return self.split(text)

    def _chat_meta(self, chat_id: str) -> dict:
        for c in self.store.chats():
            if c["chat_id"] == chat_id:
                return c
        return {"chat_id": chat_id, "chat_type": "group"}

    # ---- 语音 ----
    async def send_record(self, chat_id: str, audio_b64: str, origin: str = "tts") -> dict:
        from app.gateway.protocol import record_segment
        chat = self._chat_meta(chat_id)
        segs = [record_segment(f"base64://{audio_b64}")]
        res = await self.gateway.send_message(chat["chat_type"], chat_id, segs)
        self.store.append_message(ts=time.time(), chat_id=chat_id, chat_type=chat["chat_type"],
                                  sender_id="agent", sender_name="小默", role="assistant",
                                  content="[语音]", segments=segs, origin=origin)
        self.bus.emit("message.out", {"chat_id": chat_id, "text": "[语音]", "segments": segs,
                                      "origin": origin, "result": res})
        return res

    # ---- 广播限额 ----
    def broadcast_allowed(self) -> tuple[bool, str]:
        b = self.shaping.get("broadcast", {})
        now = time.time()
        self._broadcast_log = [t for t in self._broadcast_log if now - t < 86400]
        day = [t for t in self._broadcast_log if now - t < 86400]
        hour = [t for t in self._broadcast_log if now - t < 3600]
        if len(day) >= int(b.get("daily_max", 8)):
            return False, "daily quota"
        if len(hour) >= int(b.get("hourly_max", 2)):
            return False, "hourly quota"
        if self._broadcast_log and now - self._broadcast_log[-1] < int(b.get("min_gap_minutes", 25)) * 60:
            return False, "min gap"
        for w in b.get("silence", ["00:00-08:00"]):
            if w == "*" or _in_window(w, time.localtime()):
                return False, "silence window"
        return True, ""

    async def broadcast(self, chat_id: str, text: str) -> None:
        self._broadcast_log.append(time.time())
        await self.send(chat_id, text, origin="broadcast")

def _in_window(window: str, lt) -> bool:
    a, _, b = window.partition("-")
    lo = int(a[:2]) * 60 + int(a[3:5])
    hi = int(b[:2]) * 60 + int(b[3:5])
    t = lt.tm_hour * 60 + lt.tm_min
    if lo <= hi:
        return lo <= t <= hi
    return t >= lo or t <= hi
