"""OneBot v11 网关(双模, 2026-10-25 补强):
- reverse(默认): 反向 WS 服务端, 协议端(SnowLuma 等)连进来, 单连接 + echo 动作 + 心跳监控;
- forward: 正向外连客户端, 主动连接协议端的正向 WS 服务(NapCat/Lagrange 等),
  断线自动重连自愈(云部署无需公网监听端口)。

模式由 config/channels.yaml `onebot.mode` 切换, 环境变量
ME_ONEBOT_MODE / ME_ONEBOT_WS_URL / ME_ONEBOT_RECONNECT_S 可覆盖(测试/运维用)。
两种模式共用同一消息循环(_serve), 差异仅由 WS 适配器抹平。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
import websockets

from app.gateway import protocol

log = logging.getLogger("gateway")


class _ConnectionLost(Exception):
    """适配器统一的连接断开信号(reverse: WebSocketDisconnect / forward: ConnectionClosed)。"""


class _StarletteWs:
    """reverse 模式适配器: 包 starlette WebSocket。"""

    def __init__(self, ws: WebSocket):
        self._ws = ws

    async def send_text(self, s: str) -> None:
        await self._ws.send_text(s)

    async def recv_text(self) -> str:
        try:
            return await self._ws.receive_text()
        except WebSocketDisconnect as e:
            raise _ConnectionLost(str(e.code)) from e

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self._ws.close(code=code, reason=reason[:120])
        except Exception:
            pass


class _ClientWs:
    """forward 模式适配器: 包 websockets 客户端连接。"""

    def __init__(self, ws):
        self._ws = ws

    async def send_text(self, s: str) -> None:
        await self._ws.send(s)

    async def recv_text(self) -> str:
        try:
            return await self._ws.recv()
        except websockets.exceptions.ConnectionClosed as e:
            raise _ConnectionLost(f"code={e.code}") from e

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self._ws.close(code=code, reason=reason[:120])
        except Exception:
            pass


class OneBotGateway:
    def __init__(self, settings, bus):
        self.settings = settings
        self.bus = bus
        self.token = settings.onebot_token()
        self.mode = settings.onebot_mode()               # reverse | forward
        self.ws_url = settings.onebot_forward_url()      # forward 模式目标
        self.reconnect_interval = settings.onebot_reconnect_interval()
        self.ws: _StarletteWs | _ClientWs | None = None
        self.self_id = settings.channels.get("onebot", {}).get("self_qq", "") or ""
        self.last_heartbeat: float | None = None
        self.last_event_ts: float | None = None
        self.connected_since: float | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self.recv_count = 0
        # forward 模式运行态
        self._forward_task: asyncio.Task | None = None
        self._forward_stopping = False
        self.reconnect_attempts = 0
        self.last_connect_error: str | None = None

    # ================= reverse 模式入口(FastAPI WS 路由) =================

    async def handle(self, ws: WebSocket) -> None:
        if self.mode != "reverse":
            await ws.close(code=4002, reason="gateway in forward mode")
            return
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
        await self._serve(_StarletteWs(ws))

    # ================= 共用消息循环 =================

    async def _serve(self, adapter) -> None:
        if self.ws is not None:
            try:
                await self.ws.close(code=4000, reason="replaced by new connection")
            except Exception:
                pass
        self.ws = adapter
        self.connected_since = time.time()
        self.last_heartbeat = None      # 新连接重置心跳时钟, 防旧值误判
        self.bus.emit("gateway.status", {"connected": True, "mode": self.mode})
        try:
            while True:
                raw = await adapter.recv_text()
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
        except _ConnectionLost:
            pass
        finally:
            if self.ws is adapter:
                self.ws = None
                self.bus.emit("gateway.status", {"connected": False, "mode": self.mode})

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

    # ================= forward 模式(正向外连 + 断线自愈) =================

    def start_forward(self) -> asyncio.Task | None:
        """启动外连循环(AppContext.startup 调用; reverse 模式返回 None)。"""
        if self.mode != "forward":
            return None
        if self._forward_task and not self._forward_task.done():
            return self._forward_task
        self._forward_stopping = False
        self._forward_task = asyncio.create_task(self._forward_loop(), name="gateway-forward")
        log.info("网关 forward 模式启动: %s (重连间隔 %ss)", self._safe_url(), self.reconnect_interval)
        return self._forward_task

    async def stop_forward(self) -> None:
        self._forward_stopping = True
        task, self._forward_task = self._forward_task, None
        if task:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        if self.ws is not None:
            await self.ws.close(code=1001, reason="shutdown")
            self.ws = None

    async def _forward_loop(self) -> None:
        while not self._forward_stopping:
            url = self._authed_url()
            try:
                conn = await websockets.connect(
                    url,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    additional_headers=self._auth_headers(),
                )
                self.last_connect_error = None
                await self._serve(_ClientWs(conn))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_connect_error = f"{type(e).__name__}: {e}"[:200]
                self.reconnect_attempts += 1
                if self.reconnect_attempts == 1 or self.reconnect_attempts % 10 == 0:
                    log.warning("forward 连接失败(第 %s 次): %s", self.reconnect_attempts, self.last_connect_error)
                self.bus.emit("gateway.status", {"connected": False, "mode": "forward",
                                                 "error": self.last_connect_error,
                                                 "attempt": self.reconnect_attempts})
            if self._forward_stopping:
                break
            await asyncio.sleep(self.reconnect_interval)
        log.info("forward 外连循环退出")

    def _safe_url(self) -> str:
        """状态展示用 URL(剥掉 query 里的 token, 严禁泄露)。"""
        u = self.ws_url or ""
        return u.split("?", 1)[0]

    def _authed_url(self) -> str:
        u = self.ws_url or ""
        if self.token and "access_token=" not in u:
            sep = "&" if "?" in u else "?"
            u = f"{u}{sep}access_token={self.token}"
        return u

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    # ================= 动作下发(两模式共用) =================

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
        out = {
            "mode": self.mode,
            "connected": connected,
            "self_id": self.self_id,
            "connected_since": self.connected_since,
            "uptime_s": (now - self.connected_since) if self.connected_since else None,
            "last_heartbeat_age_s": hb_age,
            "heartbeat_ok": connected and (hb_age is not None and hb_age < hb_timeout),
            "recv_count": self.recv_count,
        }
        if self.mode == "forward":
            out["forward_url"] = self._safe_url()
            out["reconnect_interval_s"] = self.reconnect_interval
            out["reconnect_attempts"] = self.reconnect_attempts
            out["last_error"] = self.last_connect_error
        return out

    async def poke(self, chat_type: str, chat_id: str, user_id: str) -> dict:
        if chat_type == "group":
            return await self.send_action("group_poke", {"group_id": int(chat_id), "user_id": int(user_id)})
        return await self.send_action("friend_poke", {"user_id": int(user_id)})
