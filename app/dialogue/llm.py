"""LLM 客户端(OpenAI 兼容 /chat/completions):工具调用 + JSON mode;无 key 时优雅降级。"""
from __future__ import annotations
import json
import httpx

class LLMClient:
    def __init__(self, cfg: dict, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.api_key = api_key
        self.enabled = bool(api_key and cfg.get("base_url"))
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.cfg["base_url"],
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=float(self.cfg.get("timeout_s", 30)),
                transport=self._transport,
            )
        return self._client

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   json_mode: bool = False, model: str | None = None) -> dict:
        """返回 {content, tool_calls:[{name, arguments}]} 或 {} (不可用/失败)。"""
        if not self.enabled:
            return {}
        body: dict = {
            "model": model or self.cfg.get("chat_model", "deepseek-chat"),
            "messages": messages,
            "temperature": float(self.cfg.get("temperature", 0.7)),
        }
        if tools:
            body["tools"] = tools
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            r = await self._ensure_client().post("/chat/completions", json=body)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return {}
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            calls.append({"name": fn.get("name"), "arguments": args})
        return {"content": msg.get("content") or "", "tool_calls": calls}

    async def chat_json(self, prompt: str, model: str | None = None) -> str:
        res = await self.chat([{"role": "user", "content": prompt}], json_mode=True,
                              model=model or self.cfg.get("planner_model"))
        return res.get("content", "")

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
