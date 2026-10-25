"""OneBot v11 反向 WS 服务端(SnowLuma 连进来):单连接、echo 动作、心跳监控。"""
from __future__ import annotations
import asyncio
import json
import time
from fastapi import WebSocket, WebSocketDisconnect
from app.gateway import protocol

class OneBotGateway:
    def __init__(self, settings, bus):
        self.settings = settings
        self.bus = bus
        self.token = settings.onebot_token()
        self.ws: WebSocket | None = None
        self.self_id = settings.channels.get("onebot", {}).get("self_qq", "") or ""
        self.last_heartbeat: float | None = None
        self.last_event_ts: float | None = None
        self.connected_since: float | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self.recv_count = 0

    async def handle(self, ws: WebSocket) -> None:
        # token 校验: query access_token 或 Authorization 头
        qtoken = ws.query_params.get("access_token", "")
        htoken = ""
        auth = ws.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            htoken = auth[7:]
        if self.token and qtoken != self.token and htoken != self.token:
            await ws.close(code=4001, reason="bad token")
            return
        await ws.accept()
        if self.ws is not None:
            try:
                await self.ws.close(code=4000, reason="replaced by new connection")
            except Exception:
                pass
        self.ws = ws
        self.connected_since = time.time()
        self.bus.emit("gateway.status", {"connected": True})
        try:
            while True:
                raw = await ws.receive_text()
                self.recv_count += 1
                self.last_event_ts = time.time()
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(frame, dict):
                    continue
                if frame.get("post_type"):
                    await self._on_event(frame)
                elif "echo" in frame and ("status" in frame or "retcode" in frame):
                    self._resolve(frame)
        except WebSocketDisconnect:
            pass
        finally:
            if self.ws is ws:
                self.ws = None
                self.bus.emit("gateway.status", {"connected": False})

    async def _on_event(self, event: dict) -> None:
        et = event.get("post_type")
        if et == "meta_event":
            if event.get("meta_event_type") == "lifecycle" and not self.self_id:
                self.self_id = str(event.get("self_id", ""))
            self.last_heartbeat = time.time()
            return
        if et == "notice" or et == "request":
            self.bus.emit("onebot.notice", event)
            return
        env = protocol.envelope(event)
        if env is None:
            return
        if not self.self_id:
            self.self_id = env["self_id"]
        self.bus.emit("onebot.message", env)

    def _resolve(self, frame: dict) -> None:
        echo = str(frame.get("echo", ""))
        fut = self._pending.pop(echo, None)
        if fut and not fut.done():
            fut.set_result(frame)

    async def send_action(self, action: str, params: dict, timeout_s: float = 15.0) -> dict:
        if self.ws is None:
            return {"status": "failed", "retcode": -1, "data": None, "msg": "onebot not connected"}
        frame = protocol.action_frame(action, params)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[frame["echo"]] = fut
        try:
            async with self._lock:
                await self.ws.send_text(json.dumps(frame, ensure_ascii=False))
        except Exception:
            self._pending.pop(frame["echo"], None)
            return {"status": "failed", "retcode": -2, "data": None, "msg": "ws send error"}
        try:
            res = await asyncio.wait_for(fut, timeout_s)
            return res
        except asyncio.TimeoutError:
            self._pending.pop(frame["echo"], None)
            return {"status": "failed", "retcode": -3, "data": None, "msg": "action timeout"}

    async def send_message(self, chat_type: str, chat_id: str, segments: list[dict],
                           reply_to: Any = None) -> dict:
        segs = list(segments)
        if reply_to:
            segs = [protocol.reply_segment(reply_to)] + segs
        action = "send_group_msg" if chat_type == "group" else "send_private_msg"
        return await self.send_action(action, protocol.send_params(chat_type, chat_id, segs))

    def status(self) -> dict:
        hb_timeout = int(self.settings.channels.get("onebot", {}).get("heartbeat_timeout_s", 120))
        now = time.time()
        connected = self.ws is not None
        hb_age = (now - self.last_heartbeat) if self.last_heartbeat else None
        return {
            "connected": connected,
            "self_id": self.self_id,
            "connected_since": self.connected_since,
            "uptime_s": (now - self.connected_since) if self.connected_since else None,
            "last_heartbeat_age_s": hb_age,
            "heartbeat_ok": connected and (hb_age is not None and hb_age < hb_timeout),
            "recv_count": self.recv_count,
        }

    async def poke(self, chat_type: str, chat_id: str, user_id: str) -> dict:
        if chat_type == "group":
            return await self.send_action("group_poke", {"group_id": int(chat_id), "user_id": int(user_id)})
        return await self.send_action("friend_poke", {"user_id": int(user_id)})
