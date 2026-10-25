"""Hindsight webhook 接收(consolidation/refresh 通知 → 总线 → 控制台 WS)。"""
from __future__ import annotations
from fastapi import APIRouter, Request, HTTPException
import hmac

def webhook_router(settings, bus) -> APIRouter:
    router = APIRouter()
    secret = settings.env("HINDSIGHT_WEBHOOK_SECRET", "")

    @router.post("/webhooks/hindsight")
    async def hindsight_webhook(request: Request):
        if secret:
            sig = request.headers.get("x-signature", "")
            body = await request.body()
            if not hmac.compare_digest(hmac.new(secret.encode(), body, "sha256").hexdigest(), sig):
                raise HTTPException(status_code=401, detail="bad signature")
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="bad json")
        evt = {"event": data.get("event", "unknown"), "bank": data.get("bank"),
               "data": data.get("data"), "received_at": data.get("ts")}
        bus.emit("hindsight.webhook", evt)
        return {"ok": True}

    return router
