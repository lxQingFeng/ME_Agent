"""TTS 服务门面:供 REST API / QQ Agent AI(speak 工具) / 插件统一调用。"""
from __future__ import annotations
import base64
import time
import httpx
from app.tts.models import TTSProvider, Voice
from app.tts.store import ProviderStore
from app.tts import synth as synth_mod

class TTSService:
    def __init__(self, settings, store: ProviderStore, outbound=None):
        self.settings = settings
        self.store = store
        self.outbound = outbound          # 可后置注入(OutboundShaper)
        self._models_cache: dict[str, tuple[float, dict]] = {}

    # ---------- 概览 ----------
    def providers(self) -> list[dict]:
        out = []
        for pid in self.store.list_ids():
            p = self.store.load(pid)
            if p:
                out.append(self._masked(p))
        return out

    def _masked(self, p: TTSProvider) -> dict:
        d = p.model_dump()
        d["api_key_masked"] = _mask(p.api_key)
        d.pop("api_key", None)
        d["raw_status"] = self.store.raw_status(p.id)
        if p.kind == "preset_voice":
            d["voices"] = self._merged_voices(p)
        return d

    def _merged_voices(self, p: TTSProvider) -> list[dict]:
        merged = {v.value: v for v in p.preset_voice.voices}
        for v in self.store.load_voices(p.id):
            merged[v.value] = v
        return [v.model_dump() for v in merged.values()]

    def get(self, pid: str) -> TTSProvider | None:
        return self.store.load(pid)

    def default_provider(self) -> TTSProvider | None:
        pref = self.settings.tts_cfg.get("default_provider_id", "")
        if pref and self.store.load(pref):
            return self.store.load(pref)
        for pid in self.store.list_ids():
            p = self.store.load(pid)
            if p and p.enabled:
                return p
        return None

    # ---------- 合成 ----------
    async def synthesize(self, text: str, voice: str | None = None,
                         provider_id: str | None = None) -> dict:
        p = (self.store.load(provider_id) if provider_id else None) or self.default_provider()
        if p is None:
            return {"ok": False, "error": "没有可用的 TTS 配置(先在控制台添加)"}
        if not p.enabled:
            return {"ok": False, "error": f"提供商 {p.name} 已停用"}
        res = await synth_mod.synth(p, self.store, text, voice)
        out = {"ok": res.ok, "provider": p.id, "provider_name": p.name, "kind": p.kind,
               "elapsed_ms": res.elapsed_ms, "mime": res.mime, "bytes": len(res.audio),
               "extract_path": res.extract_path, "debug": res.debug, "error": res.error}
        if res.ok:
            cache = self.store.put_cache(res.audio, res.mime, p.id, text, voice or p.default_voice)
            out.update(cache)
            out["audio_b64"] = base64.b64encode(res.audio).decode()
        return out

    async def speak_and_send(self, chat_id: str, chat_type: str, text: str,
                             voice: str | None = None, provider_id: str | None = None) -> dict:
        """QQ Agent AI → TTS → OneBot 语音消息(桥接核心)。"""
        out = await self.synthesize(text, voice, provider_id)
        if not out.get("ok"):
            return out
        if self.outbound is None:
            out["sent"] = False
            out["detail"] = "输出通道未接入(控制台未挂 outbound)"
            return out
        res = await self.outbound.send_record(chat_id, out["audio_b64"], origin="tts")
        out["sent"] = res.get("status") in ("ok", "async")
        out["send_result"] = res
        return out

    # ---------- 模型列表 ----------
    async def models(self, pid: str, refresh: bool = False,
                     transport: httpx.AsyncBaseTransport | None = None) -> dict:
        cached = self._models_cache.get(pid)
        if cached and not refresh and time.time() - cached[0] < 60:
            return cached[1]
        p = self.store.load(pid)
        if p is None:
            return {"ok": False, "error": "provider 不存在", "models": []}
        data = await synth_mod.fetch_models(p, transport=transport)
        self._models_cache[pid] = (time.time(), data)
        return data

    async def check(self, pid: str, transport: httpx.AsyncBaseTransport | None = None) -> dict:
        """保存和检查模块:连通性+认证检查。"""
        p = self.store.load(pid)
        if p is None:
            return {"ok": False, "error": "provider 不存在"}
        data = await synth_mod.fetch_models(p, transport=transport)
        return {"ok": data.get("ok", False), "detail": data.get("error") or
                f"模型端点可达, {len(data.get('models', []))} 个模型",
                "latency_ms": data.get("latency_ms"), "url": data.get("url")}

    # ---------- 音色发现(best effort) ----------
    async def discover_voices(self, pid: str) -> dict:
        p = self.store.load(pid)
        if p is None:
            return {"ok": False, "error": "provider 不存在"}
        found: list[Voice] = []
        tried = []
        for path in ("/audio/voices", "/voices", "/v1/audio/voices"):
            url = p.base_url.rstrip("/") + path
            tried.append(url)
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    headers = {"Authorization": f"Bearer {p.api_key}"} if p.api_key else {}
                    r = await client.get(url, headers=headers)
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    items = data if isinstance(data, list) else (data.get("voices") or data.get("data") or [])
                    for it in items:
                        if isinstance(it, dict):
                            val = str(it.get("id") or it.get("value") or it.get("name") or "")
                            label = str(it.get("name_zh") or it.get("label") or it.get("name") or val)
                            if val:
                                found.append(Voice(value=val, label=label, source="discovered"))
                        elif isinstance(it, str):
                            found.append(Voice(value=it, label=it, source="discovered"))
                    break
            except Exception:
                continue
        if found:
            merged = {v.value: v for v in self.store.load_voices(pid)}
            for v in found:
                merged.setdefault(v.value, v)
            self.store.save_voices(pid, list(merged.values()))
        return {"ok": bool(found), "found": len(found), "tried": tried}

    def status(self) -> dict:
        return {"providers": len(self.store.list_ids()),
                "default_provider": (self.default_provider().id if self.default_provider() else None),
                "cache": self.store.cache_stats()}

def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"

# ---- LLM 工具(speak)清单 ----
def speak_tool_manifest() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "speak",
            "description": "用语音说一句话(TTS 合成后以语音消息发出);适合念内容/发语音/唱歌类请求",
            "parameters": {"type": "object",
                           "properties": {"text": {"type": "string", "description": "要说的话(口语化)"},
                                          "voice": {"type": "string", "description": "音色(可选)"}},
                           "required": ["text"]},
        },
    }

async def speak_tool_invoke(tts: TTSService, outbound, args: dict, context: dict) -> str:
    """pipeline._dispatch_tool 的 speak 分支。"""
    text = (args.get("text") or "").strip()
    if not text:
        return "没说要说什么"
    out = await tts.speak_and_send(context.get("chat_id", ""), context.get("chat_type", "group"),
                                   text, args.get("voice"))
    if out.get("ok") and out.get("sent"):
        return f"(语音已发出,{out.get('bytes', 0)} 字节 {out.get('mime')})"
    if out.get("ok"):
        return f"(合成成功 {out.get('bytes')} 字节,但发送失败: {out.get('send_result')})"
    return f"(TTS 失败: {out.get('error')})"
