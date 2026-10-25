"""对话流路由:会话列表 / 消息流 / 控制台测试发送(console LIVE_ROUTES 对接)。

控制台发送是「管理口」:构造 envelope 走完整仲裁→管道→整形链路,
输出经 OneBot 网关下发;网关未连接时仍走完整管道并把回复落在消息流里。
"""
from __future__ import annotations

import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import SendIn

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _message_view(row: dict, llm_name: str | None) -> dict:
    m = {
        "id": row["id"],
        "from": row["sender_name"] or row["sender_id"],
        "role": row["role"],
        "text": row["content"],
        "ts": _iso(row["ts"]),
        "origin": row.get("origin"),
        "audio": "[语音]" in (row["content"] or ""),
    }
    if row["role"] == "assistant" and llm_name:
        m["llm"] = llm_name
    return m


@router.get("/conversations")
def conversations(request: Request):
    ctx = request.app.state.ctx
    out = []
    for c in ctx.store.chats():
        last = ctx.store.last_message(c["chat_id"]) or {}
        out.append({
            "id": c["chat_id"],
            "type": c["chat_type"],
            "name": c["chat_id"],
            "unread": 0,
            "last": {
                "from": last.get("sender_name") or last.get("sender_id") or "",
                "text": (last.get("content") or "")[:80],
                "ts": _iso(last["ts"]) if last.get("ts") else None,
            },
            "stats": ctx.store.chat_stats_1h(c["chat_id"]),
        })
    out.sort(key=lambda x: x["last"]["ts"] or "", reverse=True)
    return out


@router.get("/conversations/{chat_id}/messages")
def messages(request: Request, chat_id: str, limit: int = 100):
    ctx = request.app.state.ctx
    rows = ctx.store.recent_messages(chat_id, limit=limit)
    llm_name = "deepseek" if ctx.llm.enabled else None
    return [_message_view(dict(r), llm_name) for r in rows]


@router.post("/conversations/{chat_id}/send")
async def send(request: Request, chat_id: str, body: SendIn):
    ctx = request.app.state.ctx
    # 会话已存在则沿用其类型, 否则按控制台测试默认私聊
    chat_type = "private"
    for c in ctx.store.chats():
        if c["chat_id"] == chat_id:
            chat_type = c["chat_type"]
            break
    env = {
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender_id": body.sender_id,
        "sender_name": body.sender_name,
        "text": body.text,
        "segments": [{"type": "text", "data": {"text": body.text}}],
        "ts": time.time(),
        "message_id": None,
        "at_me": True,          # 控制台测试发送按 @ 处理 → P0
        "origin": "console",
    }
    before = ctx.store.last_message(chat_id)
    try:
        await ctx.arbiter.on_message(env)
    except Exception as e:  # 管道异常不 500, 如实回报
        raise HTTPException(status_code=500, detail=f"pipeline error: {e!r}") from e
    # 找最新一条 assistant 消息(本轮回复)
    after = ctx.store.last_message(chat_id)
    reply = None
    if after and (not before or after["id"] != before["id"]) and after["role"] == "assistant":
        reply = _message_view(after, "deepseek" if ctx.llm.enabled else None)
    delivered = ctx.gateway.ws is not None
    return {
        "ok": True,
        "dispatched": True,
        "priority": env.get("priority"),
        "onebot_connected": delivered,
        "reply": reply,
        "note": "" if delivered else "OneBot 未连接, 回复已入消息流但未下发 QQ",
    }
