"""合成引擎(三形态)。
text_prompt 形态的字节级铁律:
- 请求头: 解析成键值后仅做占位符替换, 原样作为 HTTP 头发送;
- 请求体: 占位符替换后按 UTF-8 字节【原样】发送;
- 严禁自动补 Content-Type / Authorization(模板里写了才有);
- 严禁 JSON.parse→重序列化 / pretty-print / 加前后缀。
"""
from __future__ import annotations
import base64
import binascii
import json
import re
import time
import httpx
from app.tts.models import TTSProvider, SynthResult
from app.tts.store import ProviderStore

PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

AUDIO_MAGICS = [b"ID3", b"RIFF", b"OggS", b"\xff\xfb", b"\xff\xf3", b"\xff\xe3", b"\x1aE\xdf\xa3"]
MIME_BY_EXT = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
               ".flac": "audio/flac", ".opus": "audio/opus", ".m4a": "audio/mp4", ".aac": "audio/aac"}

def substitute(text: str, mapping: dict) -> str:
    """只做 {{占位符}} 替换, 其余一字不动。未知占位符原样保留。"""
    def _repl(m):
        key = m.group(1)
        v = mapping.get(key)
        return m.group(0) if v is None else str(v)
    return PLACEHOLDER_RE.sub(_repl, text)

def parse_header_lines(raw: str) -> list[tuple[str, str]]:
    """原文请求头 → 键值列表(每行 k: v; 容忍 \r\n/空行/注释)。"""
    out: list[tuple[str, str]] = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k:
            out.append((k, v))
    return out

def looks_audio(data: bytes) -> bool:
    if not data or len(data) < 12:
        return False
    for m in AUDIO_MAGICS:
        if data[: len(m)] == m:
            return True
    if data[4:8] in (b"ftyp", b"M4A "):
        return True
    if data[:4] == b"fLaC":
        return True
    if data[:4] == b"8BNP" or data[:8] == b"!\x00\x00\x00\x00\x00\x00\x00":
        return True
    return False

def _json_path(data, path: str):
    cur = data
    for token in re.split(r"\.", path):
        if token == "":
            continue
        m = re.match(r"^([^\[]+)(?:\[(\d+)\])?$", token)
        key, idx = m.group(1), m.group(2)
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
        if idx is not None:
            try:
                cur = cur[int(idx)]
            except Exception:
                return None
    return cur

async def _fetch_url_audio(client: httpx.AsyncClient, url: str) -> tuple[bytes, str] | None:
    try:
        r = await client.get(url, timeout=20.0)
        if r.status_code == 200 and (looks_audio(r.content) or
                                     r.headers.get("content-type", "").startswith("audio/")):
            return r.content, r.headers.get("content-type", "audio/mpeg")
    except Exception:
        pass
    return None

def _scan_json_for_audio(data, client=None, depth=0):
    """递归扫描 JSON, 找 base64 音频或音频 URL。返回 (candidate, kind)。"""
    if depth > 6:
        return None
    if isinstance(data, str):
        s = data.strip()
        if s.startswith(("http://", "https://")):
            return s, "url"
        if len(s) > 256:
            try:
                raw = base64.b64decode(s + "=" * (-len(s) % 4), validate=False)
                if looks_audio(raw):
                    return raw, "bytes"
            except (binascii.Error, ValueError):
                pass
        return None
    if isinstance(data, dict):
        for key in ("audio", "audio_data", "voice", "output", "data", "content", "b64", "base64"):
            if key in data:
                got = _scan_json_for_audio(data[key], client, depth + 1)
                if got:
                    return got
        for v in data.values():
            got = _scan_json_for_audio(v, client, depth + 1)
            if got:
                return got
        return None
    if isinstance(data, list):
        for v in data:
            got = _scan_json_for_audio(v, client, depth + 1)
            if got:
                return got
    return None

async def extract_audio(resp: httpx.Response, provider: TTSProvider,
                        client: httpx.AsyncClient) -> tuple[bytes, str, str]:
    """响应 → (音频字节, mime, 提取路径说明)。"""
    ct = resp.headers.get("content-type", "")
    if ct.startswith("audio/") or (ct == "application/octet-stream" and looks_audio(resp.content)):
        return resp.content, ct or "audio/mpeg", "binary"
    if looks_audio(resp.content):
        return resp.content, ct or "audio/mpeg", "binary-magic"
    try:
        data = resp.json()
    except Exception:
        # 纯文本里可能就是 URL / base64
        got = _scan_json_for_audio(resp.text[:20000])
        if got and got[1] == "url":
            r = await _fetch_url_audio(client, got[0])
            if r:
                return r[0], r[1], "text-url"
        if got and got[1] == "bytes":
            return got[0], "audio/mpeg", "text-b64"
        raise ValueError("无法从响应提取音频")
    # 配置的 jsonpath 优先
    if provider.text_prompt.response_extract and provider.text_prompt.response_extract != "auto":
        v = _json_path(data, provider.text_prompt.response_extract)
        if isinstance(v, str):
            if v.startswith(("http://", "https://")):
                r = await _fetch_url_audio(client, v)
                if r:
                    return r[0], r[1], f"jsonpath-url:{provider.text_prompt.response_extract}"
            else:
                try:
                    raw = base64.b64decode(v + "=" * (-len(v) % 4), validate=False)
                    if looks_audio(raw):
                        return raw, "audio/mpeg", f"jsonpath-b64:{provider.text_prompt.response_extract}"
                except Exception:
                    pass
        got = _scan_json_for_audio(v)
        if got:
            if got[1] == "url":
                r = await _fetch_url_audio(client, got[0])
                if r:
                    return r[0], r[1], f"jsonpath:{provider.text_prompt.response_extract}"
            return got[0], "audio/mpeg", f"jsonpath:{provider.text_prompt.response_extract}"
    # auto 扫描
    got = _scan_json_for_audio(data)
    if got:
        if got[1] == "url":
            r = await _fetch_url_audio(client, got[0])
            if r:
                return r[0], r[1], "auto-url"
            raise ValueError(f"音频 URL 拉取失败: {got[0][:80]}")
        return got[0], "audio/mpeg", "auto-b64"
    raise ValueError("响应 JSON 里没找到音频(base64/URL)")

# ---------------- 三形态合成 ----------------

async def synth_text_prompt(p: TTSProvider, store: ProviderStore, text: str,
                            voice: str | None = None, speed: float | None = None,
                            transport: httpx.AsyncBaseTransport | None = None) -> SynthResult:
    t0 = time.time()
    header_raw = store.read_raw(p.id, "request_header")
    body_raw = store.read_raw(p.id, "request_body")
    if header_raw is None or body_raw is None:
        return SynthResult(error="缺少原始请求头/请求体(先保存两段原文)")
    url = p.text_prompt.endpoint or (p.base_url.rstrip("/") + p.text_prompt.path_hint)
    if not url:
        return SynthResult(error="未配置接口地址")
    mapping = {"api_key": p.api_key, "model": p.model, "text": text,
               "voice": voice or p.default_voice, "speed": speed or p.speed, "format": p.format}
    headers = [(k, substitute(v, mapping)) for k, v in parse_header_lines(substitute(header_raw, mapping))]
    body_bytes = substitute(body_raw, mapping).encode("utf-8")
    method = p.text_prompt.method or "POST"
    try:
        async with httpx.AsyncClient(transport=transport, timeout=60.0) as client:
            # 显式 headers + 原始 content 字节: httpx 不会补 Content-Type/Authorization
            req = client.build_request(method, url, headers=headers, content=body_bytes)
            sent_keys = sorted(k.lower() for k in req.headers.keys())
            resp = await client.send(req)
            audio, mime, path = await extract_audio(resp, p, client)
            return SynthResult(ok=True, audio=audio, mime=mime, extract_path=path,
                               status_code=resp.status_code,
                               elapsed_ms=int((time.time() - t0) * 1000),
                               debug={"url": url, "method": method,
                                      "header_keys_sent": sent_keys,
                                      "body_bytes": len(body_bytes),
                                      "body_sha256": _sha(body_bytes)})
    except ValueError as e:
        return SynthResult(error=str(e), elapsed_ms=int((time.time() - t0) * 1000))
    except Exception as e:
        return SynthResult(error=f"{type(e).__name__}: {e}", elapsed_ms=int((time.time() - t0) * 1000))

async def synth_speech(p: TTSProvider, text: str, voice: str | None = None,
                       speed: float | None = None,
                       transport: httpx.AsyncBaseTransport | None = None) -> SynthResult:
    """预设音色/克隆形态:标准 OpenAI 兼容 /audio/speech JSON 请求。"""
    t0 = time.time()
    if not p.base_url:
        return SynthResult(error="未配置 base_url")
    url = p.text_prompt.endpoint and (p.kind != "preset_voice") and p.text_prompt.endpoint or \
          p.base_url.rstrip("/") + "/audio/speech"
    body = {"model": p.model, "input": text,
            "voice": voice or p.default_voice, "response_format": p.format,
            "speed": speed or p.speed}
    headers = {"Content-Type": "application/json"}
    if p.api_key:
        headers["Authorization"] = f"Bearer {p.api_key}"
    try:
        async with httpx.AsyncClient(transport=transport, timeout=60.0) as client:
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 200 and (
                    resp.headers.get("content-type", "").startswith("audio/") or looks_audio(resp.content)):
                return SynthResult(ok=True, audio=resp.content,
                                   mime=resp.headers.get("content-type", "audio/mpeg"),
                                   extract_path="binary", status_code=resp.status_code,
                                   elapsed_ms=int((time.time() - t0) * 1000))
            # JSON 响应(base64/URL)
            audio, mime, path = await extract_audio(resp, p, client)
            return SynthResult(ok=True, audio=audio, mime=mime, extract_path=path,
                               status_code=resp.status_code,
                               elapsed_ms=int((time.time() - t0) * 1000))
    except ValueError as e:
        return SynthResult(error=str(e), elapsed_ms=int((time.time() - t0) * 1000))
    except Exception as e:
        return SynthResult(error=f"{type(e).__name__}: {e}", elapsed_ms=int((time.time() - t0) * 1000))

async def synth(p: TTSProvider, store: ProviderStore, text: str, voice: str | None = None,
                speed: float | None = None,
                transport: httpx.AsyncBaseTransport | None = None) -> SynthResult:
    if p.kind == "text_prompt":
        return await synth_text_prompt(p, store, text, voice, speed, transport)
    return await synth_speech(p, text, voice, speed, transport)

# ---------------- 语音克隆 ----------------

async def clone_voice(p: TTSProvider, store: ProviderStore, name: str, audio: bytes,
                      filename: str, mime: str,
                      transport: httpx.AsyncBaseTransport | None = None) -> tuple[object, str]:
    """上传克隆音频 → 调克隆 API(可选) → 登记 ClonedVoice。返回 (ClonedVoice, 说明)。"""
    from app.tts.models import ClonedVoice
    audio_file = store.save_clone_audio(p.id, audio, filename)
    cs = p.voice_clone.clone
    remote_id = None
    note = "仅本地登记(未配置克隆端点)"
    if cs.use_raw_templates:
        ch = store.read_raw(p.id, "clone_header")
        cb = store.read_raw(p.id, "clone_body")
        if ch is None or cb is None:
            return ClonedVoice(id="", name=name, status="failed",
                               audio_file=audio_file), "缺少克隆原文模板(clone_header.txt/clone_body.txt)"
        mapping = {"name": name, "filename": filename, "mime": mime,
                   "audio_b64": base64.b64encode(audio).decode(),
                   "api_key": p.api_key, "model": p.model, "format": p.format}
        headers = [(k, substitute(v, mapping)) for k, v in parse_header_lines(substitute(ch, mapping))]
        body_bytes = substitute(cb, mapping).encode("utf-8")
        try:
            async with httpx.AsyncClient(transport=transport, timeout=60.0) as client:
                resp = await client.request(cs.method or "POST", cs.endpoint, headers=headers, content=body_bytes)
                if resp.status_code < 300:
                    remote_id = _scan_voice_id(resp.text)
                    note = f"克隆调用成功({resp.status_code})" + (f", voice_id={remote_id}" if remote_id else "")
                else:
                    return ClonedVoice(id="", name=name, status="failed", audio_file=audio_file), \
                        f"克隆端点返回 {resp.status_code}: {resp.text[:150]}"
        except Exception as e:
            return ClonedVoice(id="", name=name, status="failed", audio_file=audio_file), f"克隆调用失败: {e}"
    elif cs.endpoint:
        try:
            async with httpx.AsyncClient(transport=transport, timeout=60.0) as client:
                files = {cs.multipart_field: (filename, audio, mime or "audio/wav")}
                data = {"name": name, **cs.extra_fields}
                if p.api_key:
                    headers = {"Authorization": f"Bearer {p.api_key}"}
                    resp = await client.request(cs.method or "POST", cs.endpoint, files=files, data=data, headers=headers)
                else:
                    resp = await client.request(cs.method or "POST", cs.endpoint, files=files, data=data)
                if resp.status_code < 300:
                    remote_id = _scan_voice_id(resp.text)
                    note = f"克隆上传成功({resp.status_code})" + (f", voice_id={remote_id}" if remote_id else "")
                else:
                    return ClonedVoice(id="", name=name, status="failed", audio_file=audio_file), \
                        f"克隆端点返回 {resp.status_code}: {resp.text[:150]}"
        except Exception as e:
            return ClonedVoice(id="", name=name, status="failed", audio_file=audio_file), f"克隆上传失败: {e}"
    cv = ClonedVoice(
        id=remote_id or f"clone_{int(time.time())}",
        name=name, status="active" if remote_id else "local",
        remote_id=remote_id, audio_file=audio_file,
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    return cv, note

def _scan_voice_id(text: str) -> str | None:
    try:
        data = json.loads(text)
    except Exception:
        return None
    def walk(d, depth=0):
        if depth > 4:
            return None
        if isinstance(d, dict):
            for key in ("voice_id", "speaker_id", "id", "voice", "uuid"):
                v = d.get(key)
                if isinstance(v, str) and v:
                    return v
            for v in d.values():
                r = walk(v, depth + 1)
                if r:
                    return r
        elif isinstance(d, list):
            for v in d:
                r = walk(v, depth + 1)
                if r:
                    return r
        return None
    return walk(data)

async def fetch_models(p: TTSProvider, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """依据官方 API 地址获取模型列表: GET {base_url}/models, Bearer key。"""
    if not p.base_url:
        return {"ok": False, "error": "未配置 base_url", "models": []}
    url = p.base_url.rstrip("/") + "/models"
    headers = {}
    if p.api_key:
        headers["Authorization"] = f"Bearer {p.api_key}"
    t0 = time.time()
    try:
        async with httpx.AsyncClient(transport=transport, timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                        "models": [], "url": url}
            data = resp.json()
            items = data.get("data") if isinstance(data, dict) else data
            models = []
            if isinstance(items, list):
                for it in items:
                    mid = it.get("id") if isinstance(it, dict) else str(it)
                    if mid:
                        models.append({"id": mid, "owned_by": (it.get("owned_by") if isinstance(it, dict) else None)})
            return {"ok": True, "models": models, "url": url,
                    "latency_ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "models": [], "url": url}

def _sha(b: bytes) -> str:
    import hashlib
    return hashlib.sha256(b).hexdigest()[:16]
