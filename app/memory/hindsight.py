"""Hindsight REST 客户端 + 记忆服务(retain/recall/reflect/健康), 全部优雅降级。
端点对齐官方 HTTP API 1.1(openapi.json, 2026-10-25 核实):
  retain  POST /v1/default/banks/{bank}/memories        {"items": [...]}
  recall  POST /v1/default/banks/{bank}/memories/recall {"query", "max_tokens"}
  reflect POST /v1/default/banks/{bank}/reflect          {"query"}
  banks   GET/PUT /v1/default/banks[/{bank}]
  config  PATCH /v1/default/banks/{bank}/config          {"updates": {...}}(Memory Defense)
  webhooks POST /v1/default/banks/{bank}/webhooks        {"url","secret","event_types"}
"""
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
        self.ep_retain = eps.get("retain", "/v1/default/banks/{bank}/memories")
        self.ep_recall = eps.get("recall", "/v1/default/banks/{bank}/memories/recall")
        self.ep_reflect = eps.get("reflect", "/v1/default/banks/{bank}/reflect")
        self.ep_banks = eps.get("banks", "/v1/default/banks")
        self.ep_config = eps.get("config", "/v1/default/banks/{bank}/config")
        self.ep_webhooks = eps.get("webhooks", "/v1/default/banks/{bank}/webhooks")
        self.ep_health = eps.get("health", "/health")
        self.banks: dict[str, str] = dict(self.h.get("banks", {"dialogue": "qq-agent", "knowledge": "knowledge"}))
        self.defense = bool(self.h.get("defense", True))
        wh = cfg.get("hindsight", {}).get("webhook", {})
        self.wh_cfg = wh if isinstance(wh, dict) else {}
        self.timeout = float(cfg.get("recall", {}).get("timeout_ms", 800)) / 1000.0 or 0.8
        self.top_k = int(cfg.get("recall", {}).get("top_k", 8))
        self.token_budget = int(cfg.get("recall", {}).get("token_budget", 600))
        self._client: httpx.AsyncClient | None = None
        self._transport = transport
        self._busy = False   # 失败退避开关
        self._fail_streak = 0
        self._last_ok: float | None = None

    def _ensure(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            self._client = httpx.AsyncClient(base_url=self.base, headers=headers,
                                             timeout=self.timeout, transport=self._transport)
        return self._client

    async def _post(self, path: str, body: dict, timeout: float | None = None,
                    method: str = "POST") -> dict | None:
        if self._busy:
            return None
        try:
            c = self._ensure()
            r = await (c.post(path, json=body, timeout=timeout or self.timeout) if method == "POST"
                       else c.put(path, json=body, timeout=timeout or self.timeout))
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
        meta = {"kind": kind, **(metadata or {})}
        res = await self._post(self.ep_retain.format(bank=bank),
                               {"items": [{"content": content,
                                           "metadata": {k: str(v) for k, v in meta.items()}}]})
        return res is not None

    async def retain_knowledge(self, content: str, metadata: dict | None = None) -> bool:
        bank = self.banks.get("knowledge", "knowledge")
        meta = {"kind": "knowledge", **(metadata or {})}
        res = await self._post(self.ep_retain.format(bank=bank),
                               {"items": [{"content": content,
                                           "metadata": {k: str(v) for k, v in meta.items()}}]})
        return res is not None

    async def recall(self, query: str, top_k: int | None = None) -> list[dict]:
        bank = self.banks.get("dialogue", "qq-agent")
        res = await self._post(self.ep_recall.format(bank=bank),
                               {"query": query, "max_tokens": self.token_budget},
                               timeout=self.timeout)
        if not res:
            return []
        items = res.get("results") or res.get("memories") or res.get("data") or []
        return items if isinstance(items, list) else []

    async def reflect(self, summary: str) -> bool:
        bank = self.banks.get("dialogue", "qq-agent")
        res = await self._post(self.ep_reflect.format(bank=bank), {"query": summary}, timeout=30.0)
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
        # 官方 1.1: PUT /v1/default/banks/{bank_id} 创建或更新(幂等)
        res = await self._post(self.ep_banks + "/" + bank_id, {"name": bank_id},
                               timeout=10.0, method="PUT")
        return res is not None

    async def set_bank_config(self, bank_id: str, updates: dict) -> bool:
        # 官方 1.1: PATCH .../config {"updates": {...}} — Memory Defense 等按 bank 配置
        res = await self._post(self.ep_config.format(bank=bank_id), {"updates": updates},
                               timeout=10.0, method="PATCH")
        return res is not None

    async def register_webhook(self, bank_id: str, url: str, secret: str | None,
                               event_types: list[str]) -> bool:
        """注册生命周期事件回调(官方: X-Hindsight-Signature: sha256=<hex> 签名)。"""
        body = {"url": url, "event_types": event_types, "enabled": True}
        if secret:
            body["secret"] = secret
        res = await self._post(self.ep_webhooks.format(bank=bank_id), body, timeout=10.0)
        return res is not None

    async def ensure_banks(self) -> dict:
        """一键接入:确保两个 bank 存在 + Memory Defense + webhook 回连(配置了才注册)。"""
        out = {}
        try:
            existing = {b.get("bank_id") or b.get("id") or b.get("name")
                        for b in await self.list_banks()}
        except Exception:
            existing = set()
        for key in ("dialogue", "knowledge"):
            bid = self.banks.get(key)
            if bid and bid not in existing:
                ok = await self.create_bank(bid)
                out[key] = {"bank": bid, "created": ok}
            else:
                out[key] = {"bank": bid, "created": False, "exists": bid in existing}
        # Memory Defense(官方按 bank 配置; 红线: 密钥/PII 进库前远端脱敏)
        if self.defense:
            policy = {"memory_defense": {"enabled": True,
                                         "rules": [{"on": "sensitive_data", "action": "redact"}]}}
            for key in ("dialogue", "knowledge"):
                bid = self.banks.get(key)
                if bid:
                    out[key]["defense"] = await self.set_bank_config(bid, policy)
        # webhook 回连 ME_Agent(可选: 配置了 url 才注册)
        wurl = self.wh_cfg.get("url", "")
        if wurl:
            secret = self.settings.env(self.wh_cfg.get("secret_env", "HINDSIGHT_WEBHOOK_SECRET"))
            evts = self.wh_cfg.get("event_types", ["retain.completed", "consolidation.completed",
                                                   "memory_defense.triggered"])
            for key in ("dialogue", "knowledge"):
                bid = self.banks.get(key)
                if bid:
                    out[key]["webhook"] = await self.register_webhook(bid, wurl, secret, evts)
        return out

    def status(self) -> dict:
        return {"base_url": self.base, "busy": self._busy, "fail_streak": self._fail_streak,
                "last_ok_age_s": (time.time() - self._last_ok) if self._last_ok else None,
                "banks": self.banks}

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
