"""Hindsight webhook 接收(consolidation/retain/defense 通知 → 总线 → 控制台 WS)。
签名对齐官方: X-Hindsight-Signature: sha256=<hex>(HMAC-SHA256 over raw body);
兼容 X-Hub-Signature-256 与历史裸头 x-signature。
官方载荷字段: event / bank_id / data / timestamp(兼容 bank / ts)。
"""
from __future__ import annotations
from fastapi import APIRouter, Request, HTTPException
import hmac

def _sig_hex(sig: str) -> str:
    # "sha256=<hex>" / "sha1=<hex>" / 裸 <hex> → 只取 hex
    _, _, v = sig.partition("=")
    return v.strip() or sig.strip()

def webhook_router(settings, bus) -> APIRouter:
    router = APIRouter()
    secret = settings.env("HINDSIGHT_WEBHOOK_SECRET", "")

    @router.post("/webhooks/hindsight")
    async def hindsight_webhook(request: Request):
        if secret:
            sig = (request.headers.get("x-hindsight-signature")
                   or request.headers.get("x-hub-signature-256")
                   or request.headers.get("x-signature", ""))
            body = await request.body()
            if not hmac.compare_digest(hmac.new(secret.encode(), body, "sha256").hexdigest(),
                                       _sig_hex(sig)):
                raise HTTPException(status_code=401, detail="bad signature")
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="bad json")
        evt = {"event": data.get("event", "unknown"),
               "bank": data.get("bank_id", data.get("bank")),
               "data": data.get("data"), "received_at": data.get("timestamp", data.get("ts"))}
        bus.emit("hindsight.webhook", evt)
        return {"ok": True}

    return router
