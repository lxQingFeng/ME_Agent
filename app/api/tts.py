"""TTS 全套路由(对接 console page-tts 五步流程)。

字节级保真铁律(用户 10-11 需求原文):
- 请求头/请求体两段原文经 headerTemplateRaw/bodyTemplateRaw 直传, 服务端
  只做 {{占位符}} 替换, 严禁重序列化/自动补头/pretty-print;
- 落盘用 ProviderStore.write_raw(newline=""), 保存→读回字节一致, sha256 可校验;
- PUT 收到「脱敏回显值」(含 ***)时保留旧密钥, 不覆盖。

路由契约(console/js/api.js LIVE_ROUTES):
  GET  /api/v1/tts/config           读回配置(密钥脱敏 + 两段原文 + sha256)
  PUT  /api/v1/tts/config           保存配置(原文落盘)
  POST /api/v1/tts/models           拉模型列表(后端代理, 规避 CORS)
  POST /api/v1/tts/voices           预设音色发现
  POST /api/v1/tts/synthesize       试合成(返回 audio_url)
  GET  /api/v1/tts/audio/{cid}      缓存音频文件(试听)
  POST /api/v1/tts/clone/audio      克隆参考音频上传(multipart)
  POST /api/v1/tts/check            配置检查(连通性+认证)
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.sanitizer import is_masked, mask_display
from app.api.schemas import TTSConfigIn, TTSProbeIn, TTSSynthesizeIn
from app.tts.models import ClonedVoice, TTSProvider
from app.tts.store import _base_from_endpoint

router = APIRouter(prefix="/api/v1/tts", tags=["tts"])

KIND_BY_TYPE = {"prompt": "text_prompt", "preset": "preset_voice", "clone": "voice_clone"}
TYPE_BY_KIND = {v: k for k, v in KIND_BY_TYPE.items()}

MAX_CLONE_AUDIO = 12 * 1024 * 1024   # 12 MB


def _default_pid(ctx) -> str:
    return ctx.settings.tts_cfg.get("default_provider_id", "") or "default"


def _load_or_init_default(ctx) -> TTSProvider:
    pid = _default_pid(ctx)
    p = ctx.tts_store.load(pid)
    if p is not None:
        return p
    # 首次: 从 config/tts.yaml 默认模板播种
    p = TTSProvider(id=pid, name="默认", kind="text_prompt",
                    created_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    tp = ctx.settings.tts_cfg.get("default_text_prompt", {})
    p.text_prompt.endpoint = tp.get("endpoint", "")
    p.base_url = _base_from_endpoint(p.text_prompt.endpoint)
    ctx.tts_store.save(p)
    ctx.tts_store.write_raw(pid, "request_header", tp.get("request_header", ""))
    ctx.tts_store.write_raw(pid, "request_body", tp.get("request_body", ""))
    return p


def _masked_key(key: str) -> str:
    # 2026-10-25 补强: 统一走 sanitizer 展示脱敏(不再保留首尾 4 字符明文片段)
    return mask_display(key)


def _is_masked(key: str) -> bool:
    return is_masked(key)


@router.get("/config")
def config_get(request: Request):
    ctx = request.app.state.ctx
    p = _load_or_init_default(ctx)
    raw_h = ctx.tts_store.read_raw(p.id, "request_header")
    raw_b = ctx.tts_store.read_raw(p.id, "request_body")
    rs = ctx.tts_store.raw_status(p.id)
    clone_sel = ""
    if p.voice_clone.cloned:
        active = [c for c in p.voice_clone.cloned if c.status in ("active", "local")]
        clone_sel = active[0].id if active else ""
    return {
        "ok": True,
        "providerId": p.id,
        "endpoint": p.text_prompt.endpoint,
        "apiKey": _masked_key(p.api_key),
        "model": p.model,
        "modelType": TYPE_BY_KIND.get(p.kind, "prompt"),
        "voice": p.default_voice,
        "cloneAudioId": clone_sel,
        "headerTemplateRaw": raw_h or "",
        "bodyTemplateRaw": raw_b or "",
        "rawStatus": {k: v for k, v in rs.items()},
        "presetVoices": [v.model_dump() for v in ctx.tts_store.load_voices(p.id)],
        "clonedVoices": [c.model_dump() for c in p.voice_clone.cloned],
        "updatedAt": p.updated_at,
    }


@router.put("/config")
def config_save(request: Request, body: TTSConfigIn):
    ctx = request.app.state.ctx
    pid = body.providerId or _default_pid(ctx)
    p = ctx.tts_store.load(pid) or _load_or_init_default(ctx)

    kind = KIND_BY_TYPE.get(body.modelType, p.kind)
    p.kind = kind
    if body.endpoint:
        p.text_prompt.endpoint = body.endpoint
        p.base_url = _base_from_endpoint(body.endpoint)
    # 密钥: 显式新值才覆盖; 脱敏回显值/空值保留旧密钥
    if body.apiKey and not _is_masked(body.apiKey):
        p.api_key = body.apiKey
    if body.model:
        p.model = body.model
    p.default_voice = body.voice or ""
    if body.cloneAudioId:
        for c in p.voice_clone.cloned:
            c.status = "active" if c.id == body.cloneAudioId else c.status
    p.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    ctx.tts_store.save(p)

    # 两段原文独立落盘(字节保真, 双向 sha256 校验)
    out_h, out_b = {}, {}
    if body.headerTemplateRaw is not None:
        out_h = ctx.tts_store.write_raw(pid, "request_header", body.headerTemplateRaw)
    if body.bodyTemplateRaw is not None:
        out_b = ctx.tts_store.write_raw(pid, "request_body", body.bodyTemplateRaw)
    return {
        "ok": True,
        "providerId": pid,
        "saved_at": p.updated_at,
        "request_header": out_h,
        "request_body": out_b,
    }


@router.post("/models")
async def models(request: Request, body: TTSProbeIn):
    """后端代理拉模型列表(console 规避浏览器 CORS)。"""
    ctx = request.app.state.ctx
    p = _load_or_init_default(ctx)
    if body.endpoint:
        p.text_prompt.endpoint = body.endpoint
        p.base_url = _base_from_endpoint(body.endpoint)
    if body.key and not _is_masked(body.key):
        p.api_key = body.key
    if body.model:
        p.model = body.model
    data = await ctx.tts.models(p.id, refresh=True)
    # 前端期望 OpenAI 风格 {object:"list", data:[...]}
    return {"object": "list", "data": data.get("models", []),
            "ok": data.get("ok", False), "error": data.get("error"), "url": data.get("url")}


@router.post("/voices")
async def voices(request: Request, body: TTSProbeIn):
    ctx = request.app.state.ctx
    p = _load_or_init_default(ctx)
    if body.endpoint:
        p.text_prompt.endpoint = body.endpoint
        p.base_url = _base_from_endpoint(body.endpoint)
    if body.key and not _is_masked(body.key):
        p.api_key = body.key
    if body.model:
        p.model = body.model
    out = await ctx.tts.discover_voices(p.id)
    if not out.get("ok"):
        # 官方音色端点不可用时, 回退内置预设(config/tts.yaml default_preset_voices)
        fallback = ctx.settings.tts_cfg.get("default_preset_voices", [])
        if not ctx.tts_store.load_voices(p.id) and fallback:
            from app.tts.models import Voice as _V
            p.preset_voice.voices = [
                _V(value=v.get("value", ""), label=v.get("label", ""), source="builtin")
                for v in fallback]
            ctx.tts_store.save(p)
        return [{"id": v.get("value"), "name": v.get("label"),
                 "tags": [], "desc": v.get("label", "")} for v in fallback]
    items = out.get("voices", [])
    return [{"id": v.value, "name": v.label, "tags": [v.source], "desc": v.desc}
            for v in items]


@router.post("/synthesize")
async def synthesize(request: Request, body: TTSSynthesizeIn):
    ctx = request.app.state.ctx
    p = _load_or_init_default(ctx)
    # 请求级覆盖(不落盘)
    kind = KIND_BY_TYPE.get(body.modelType, TYPE_BY_KIND.get(p.kind, "prompt"))
    p.kind = KIND_BY_TYPE.get(body.modelType, p.kind)
    if body.model:
        p.model = body.model
    voice = body.voice or p.default_voice or None
    if body.modelType == "clone" and body.cloneAudioId:
        for c in p.voice_clone.cloned:
            if c.id == body.cloneAudioId:
                voice = c.remote_id or c.id

    out = await ctx.tts.synthesize(body.text, voice=voice, provider_id=p.id)
    if not out.get("ok"):
        return {"ok": False, "error": out.get("error", "synth failed")}
    # TTSService.synthesize 内部已完成合成+缓存, 返回 {url, mime, bytes, elapsed_ms, debug}
    return {
        "ok": True,
        "engine": "server",
        "provider_kind": KIND_BY_TYPE.get(body.modelType, p.kind),
        "audio_url": out.get("url"),
        "mime": out.get("mime") or "audio/mpeg",
        "bytes": out.get("bytes", 0),
        "elapsed_ms": out.get("elapsed_ms"),
        "sha256_16": (out.get("debug") or {}).get("body_sha", ""),
    }


@router.get("/audio/{cid}")
def audio_file(request: Request, cid: str):
    ctx = request.app.state.ctx
    entry = ctx.tts_store.get_cache(cid)
    if entry is None:
        raise HTTPException(status_code=404, detail="audio not found")
    return FileResponse(entry["path"], media_type=entry["mime"])


@router.post("/clone/audio")
async def clone_upload(request: Request, file: UploadFile = File(...),
                       model: str = Form(default=""), endpoint: str = Form(default="")):
    ctx = request.app.state.ctx
    p = _load_or_init_default(ctx)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > MAX_CLONE_AUDIO:
        raise HTTPException(status_code=413, detail=f"file too large (> {MAX_CLONE_AUDIO // 1024 // 1024} MB)")
    if model:
        p.model = model
    if endpoint:
        p.text_prompt.endpoint = endpoint
        p.base_url = _base_from_endpoint(endpoint)
    name = ctx.tts_store.save_clone_audio(p.id, data, file.filename or "ref.wav")
    cv = ClonedVoice(id=f"clone_{int(time.time())}", name=file.filename or "参考音频",
                     status="local", audio_file=name,
                     created_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    p.voice_clone.cloned.append(cv)
    p.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    ctx.tts_store.save(p)
    return {"ok": True, "id": cv.id, "name": cv.name, "size": len(data),
            "audio_file": name, "providerId": p.id}


@router.post("/check")
async def check(request: Request, body: TTSProbeIn):
    """保存和检查模块: 模型端点连通性 + 认证。"""
    ctx = request.app.state.ctx
    p = _load_or_init_default(ctx)
    if body.endpoint:
        p.text_prompt.endpoint = body.endpoint
        p.base_url = _base_from_endpoint(body.endpoint)
    if body.key and not _is_masked(body.key):
        p.api_key = body.key
    out = await ctx.tts.check(p.id)
    return out
