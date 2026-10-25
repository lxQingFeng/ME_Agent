"""Hindsight REST 客户端 + 记忆服务(retain/recall/reflect/健康), 全部优雅降级。"""
from __future__ import annotations
import asyncio
import time
import httpx

class MemoryService:
    def __init__(self, settings, bus=None, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.bus = bus
        cfg = settings.memory
        self.h = cfg.get("hindsight", {})
        self.base = self.h.get("base_url", "http://127.0.0.1:8888").rstrip("/")
        self.api_key = settings.env(self.h.get("api_key_env", "HINDSIGHT_API_KEY"))
        eps = self.h.get("endpoints", {})
        self.ep_retain = eps.get("retain", "/v1/memory/retain")
        self.ep_recall = eps.get("recall", "/v1/memory/recall")
        self.ep_reflect = eps.get("reflect", "/v1/memory/reflect")
        self.ep_banks = eps.get("banks", "/v1/banks")
        self.ep_health = eps.get("health", "/health")
        self.banks = self.h.get("banks", {"dialogue": "qq-agent", "knowledge": "knowledge"})
        rc = cfg.get("recall", {})
        self.top_k = int(rc.get("top_k", 8))
        self.timeout = float(rc.get("timeout_ms", 800)) / 1000.0
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._busy = False   # Memory Defense(远端)开关;失败退避
        self._fail_streak = 0
        self._last_ok: float | None = None

    def _ensure(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            self._client = httpx.AsyncClient(base_url=self.base, headers=headers,
                                             timeout=self.timeout, transport=self._transport)
        return self._client

    async def _post(self, path: str, body: dict, timeout: float | None = None) -> dict | None:
        if self._busy:
            return None
        try:
            r = await self._ensure().post(path, json=body, timeout=timeout or self.timeout)
            if r.status_code >= 500:
                self._fail_streak += 1
                self._maybe_backoff()
                return None
            r.raise_for_status()
            self._fail_streak = 0
            self._last_ok = time.time()
            try:
                return r.json()
            except Exception:
                return {}
        except Exception:
            self._fail_streak += 1
            self._maybe_backoff()
            return None

    def _maybe_backoff(self) -> None:
        # 连续 5 次失败 → 暂停远端记忆 10 分钟(Engine 侧照常运行)
        if self._fail_streak >= 5:
            self._busy = True
            if self.bus:
                self.bus.emit("memory.degraded", {"fail_streak": self._fail_streak})
            asyncio.get_event_loop().call_later(600, self._recover)

    def _recover(self) -> None:
        self._busy = False
        self._fail_streak = 0

    # ---- 核心 API ----
    async def retain(self, content: str, kind: str, metadata: dict | None = None) -> bool:
        bank = self.banks.get("dialogue", "qq-agent")
        res = await self._post(self.ep_retain, {"bank": bank, "content": content,
                                                "metadata": {"kind": kind, **(metadata or {})}})
        return res is not None

    async def retain_knowledge(self, content: str, metadata: dict | None = None) -> bool:
        bank = self.banks.get("knowledge", "knowledge")
        res = await self._post(self.ep_retain, {"bank": bank, "content": content,
                                                "metadata": {"kind": "knowledge", **(metadata or {})}})
        return res is not None

    async def recall(self, query: str, top_k: int | None = None) -> list[dict]:
        bank = self.banks.get("dialogue", "qq-agent")
        res = await self._post(self.ep_recall, {"bank": bank, "query": query,
                                                "top_k": top_k or self.top_k})
        if not res:
            return []
        items = res.get("results") or res.get("memories") or res.get("data") or []
        return items if isinstance(items, list) else []

    async def reflect(self, summary: str) -> bool:
        bank = self.banks.get("dialogue", "qq-agent")
        res = await self._post(self.ep_reflect, {"bank": bank, "query": summary}, timeout=30.0)
        return res is not None

    async def health(self) -> dict:
        try:
            r = await self._ensure().get(self.ep_health, timeout=2.0)
            ok = r.status_code == 200
            if ok:
                self._last_ok = time.time()
            return {"reachable": ok, "status_code": r.status_code}
        except Exception:
            return {"reachable": False, "status_code": None}

    async def list_banks(self) -> list[dict]:
        try:
            r = await self._ensure().get(self.ep_banks, timeout=5.0)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return data
            return data.get("banks") or []
        except Exception:
            return []

    async def create_bank(self, bank_id: str) -> bool:
        res = await self._post(self.ep_banks, {"id": bank_id, "name": bank_id}, timeout=10.0)
        return res is not None

    async def ensure_banks(self) -> dict:
        """一键接入:确保两个 bank 存在。"""
        out = {}
        try:
            existing = {b.get("id") or b.get("name") for b in await self.list_banks()}
        except Exception:
            existing = set()
        for key in ("dialogue", "knowledge"):
            bid = self.banks.get(key)
            if bid and bid not in existing:
                ok = await self.create_bank(bid)
                out[key] = {"bank": bid, "created": ok}
            else:
                out[key] = {"bank": bid, "created": False, "exists": bid in existing}
        return out

    def status(self) -> dict:
        return {"base_url": self.base, "busy": self._busy, "fail_streak": self._fail_streak,
                "last_ok_age_s": (time.time() - self._last_ok) if self._last_ok else None,
                "banks": self.banks}

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
