"""实时推送 WS: /api/ws —— 世界心跳/消息进出/网关状态/程序状态统一推给控制台。

协议(JSON 帧):
  {"topic": "...", "data": {...}, "ts": "..."}

连接即先补发最近 history(环形 200 条, bus.py), 之后增量推送;
客户端可用 {"subscribe": ["topic", ...]} 过滤, 缺省全收。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["ws"])

PUSH_TOPICS = ("engine.tick", "engine.wake", "message.in", "message.out",
               "onebot.message", "onebot.notice", "gateway.status",
               "program.status", "hindsight.webhook", "tts.status",
               "obsidian.sync")


def ws_router(bus) -> APIRouter:
    clients: list[WebSocket] = []
    topics_filter: dict[int, set[str]] = {}
    lock = asyncio.Lock()

    def _handler(evt: dict):
        task = _broadcast(evt)
        if bus.loop and bus.loop.is_running():
            asyncio.run_coroutine_threadsafe(task, bus.loop)
        else:
            try:
                asyncio.get_event_loop().create_task(task)
            except RuntimeError:
                pass

    for topic in PUSH_TOPICS:
        bus.subscribe(topic, _handler)

    async def _broadcast(evt: dict) -> None:
        if not clients:
            return
        dead = []
        for ws in clients:
            try:
                flt = topics_filter.get(id(ws))
                if flt is not None and evt.get("topic") not in flt:
                    continue
                await ws.send_json(evt)
            except Exception:
                dead.append(ws)
        for ws in dead:
            async with lock:
                if ws in clients:
                    clients.remove(ws)
                    topics_filter.pop(id(ws), None)

    @router.websocket("/api/ws")
    async def console_ws(ws: WebSocket) -> None:
        await ws.accept()
        async with lock:
            clients.append(ws)
            topics_filter[id(ws)] = None
        # 补发最近事件(缺省最近 50 条)
        try:
            for evt in bus.history[-50:]:
                await ws.send_json(evt)
        except Exception:
            pass
        try:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict) and "subscribe" in msg:
                    topics_filter[id(ws)] = set(msg["subscribe"]) if msg["subscribe"] else None
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            async with lock:
                if ws in clients:
                    clients.remove(ws)
                topics_filter.pop(id(ws), None)

    return router
