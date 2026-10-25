"""Provider 存储:meta.json(结构化) + request_header.txt / request_body.txt(两段原文)。
字节级保真规则(用户 10-11 需求原文要求):
- 两段原文按 UTF-8 文本、newline='' 直写直读(不做任何换行翻译/裁剪/规范化);
- 保存什么读出什么, sha256 落盘校验, 往返字节一致;
- 严禁把原文嵌进 JSON 再序列化(会引入转义差异风险)。
"""
from __future__ import annotations
import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path
from app.tts.models import TTSProvider, Voice, ClonedVoice, Kind

RAW_FILES = {"request_header": "request_header.txt", "request_body": "request_body.txt",
             "clone_header": "clone_header.txt", "clone_body": "clone_body.txt"}

class ProviderStore:
    def __init__(self, root: Path, default_cfg: dict | None = None):
        self.root = Path(root)
        self.providers_dir = self.root / "providers"
        self.cache_dir = self.root / "cache"
        self.providers_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.default_cfg = default_cfg or {}

    # ---------- 目录 ----------
    def _dir(self, pid: str) -> Path:
        return self.providers_dir / pid

    def list_ids(self) -> list[str]:
        if not self.providers_dir.exists():
            return []
        return sorted(d.name for d in self.providers_dir.iterdir()
                      if d.is_dir() and (d / "meta.json").exists())

    # ---------- meta ----------
    def load(self, pid: str) -> TTSProvider | None:
        d = self._dir(pid)
        mp = d / "meta.json"
        if not mp.exists():
            return None
        try:
            data = json.loads(mp.read_text("utf-8"))
            return TTSProvider.model_validate(data)
        except Exception:
            return None

    def save(self, p: TTSProvider) -> None:
        d = self._dir(p.id)
        d.mkdir(parents=True, exist_ok=True)
        p.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        mp = d / "meta.json"
        tmp = mp.with_suffix(".tmp")
        tmp.write_text(json.dumps(p.model_dump(), ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(mp)

    def delete(self, pid: str) -> bool:
        d = self._dir(pid)
        if d.exists():
            shutil.rmtree(d)
            return True
        return False

    def create(self, name: str, kind: Kind, base_url: str = "", api_key: str = "") -> TTSProvider:
        pid = uuid.uuid4().hex[:10]
        p = TTSProvider(id=pid, name=name or pid, kind=kind, base_url=base_url,
                        api_key=api_key, created_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        # 新建 text_prompt 时播种默认模板(占位符形态)
        if kind == "text_prompt":
            tp = self.default_cfg.get("default_text_prompt", {})
            p.text_prompt.endpoint = base_url or tp.get("endpoint", "")
            if not p.base_url:
                # 从默认 endpoint 推导 API 根(模型列表用)
                p.base_url = _base_from_endpoint(p.text_prompt.endpoint)
            self.write_raw(pid, "request_header", tp.get("request_header", ""))
            self.write_raw(pid, "request_body", tp.get("request_body", ""))
        if kind == "preset_voice":
            p.preset_voice.voices = [Voice(**v) for v in self.default_cfg.get("default_preset_voices", [])]
            p.preset_voice.sample_text = self.default_cfg.get("sample_text", "你好,这是音色试听。")
        if kind == "voice_clone":
            p.base_url = base_url or "https://api.fish.audio/v1"
            p.model = p.model or "s2.1-pro-free"
        self.save(p)
        return p

    # ---------- 原始两段(核心保真) ----------
    def raw_path(self, pid: str, which: str) -> Path:
        return self._dir(pid) / RAW_FILES[which]

    def read_raw(self, pid: str, which: str) -> str | None:
        p = self.raw_path(pid, which)
        if not p.exists():
            return None
        # newline='': 不做任何换行翻译; encoding 声明 utf-8
        # 注: Path.read_text 的 newline 参数是 Python 3.13 才加入的,
        #     运行环境为 3.12, 必须用显式 open() 以保持字节保真语义不变。
        with open(p, "r", encoding="utf-8", newline="") as f:
            return f.read()

    def write_raw(self, pid: str, which: str, content: str) -> dict:
        """按原文直写(不做 trim/规范化)。返回校验信息。"""
        p = self.raw_path(pid, which)
        p.parent.mkdir(parents=True, exist_ok=True)
        # newline='': \r\n 原样落盘, 不被翻译
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        back = self.read_raw(pid, which)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        round_trip = (back == content)
        return {"file": p.name, "sha256": digest, "length": len(content.encode("utf-8")),
                "round_trip_identical": round_trip}

    def raw_status(self, pid: str) -> dict:
        out = {}
        for which in ("request_header", "request_body"):
            s = self.read_raw(pid, which)
            if s is None:
                out[which] = {"exists": False}
            else:
                out[which] = {"exists": True, "sha256": hashlib.sha256(s.encode("utf-8")).hexdigest(),
                              "bytes": len(s.encode("utf-8"))}
        return out

    # ---------- 自定义音色 ----------
    def voices_path(self, pid: str) -> Path:
        return self._dir(pid) / "voices.json"

    def load_voices(self, pid: str) -> list[Voice]:
        p = self.voices_path(pid)
        if not p.exists():
            return []
        return [Voice.model_validate(v) for v in json.loads(p.read_text("utf-8"))]

    def save_voices(self, pid: str, voices: list[Voice]) -> None:
        p = self.voices_path(pid)
        p.write_text(json.dumps([v.model_dump() for v in voices], ensure_ascii=False, indent=2), "utf-8")

    # ---------- 克隆音频 ----------
    def cloned_dir(self, pid: str) -> Path:
        return self._dir(pid) / "cloned"

    def save_clone_audio(self, pid: str, audio: bytes, filename: str) -> str:
        d = self.cloned_dir(pid)
        d.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in filename if c.isalnum() or c in "._-")[-40:] or "ref.wav"
        name = f"{int(time.time())}_{safe}"
        (d / name).write_bytes(audio)
        return name

    # ---------- 合成缓存 ----------
    def cache_meta_path(self) -> Path:
        return self.cache_dir / "cache.json"

    def _load_cache_meta(self) -> dict:
        p = self.cache_meta_path()
        if p.exists():
            return json.loads(p.read_text("utf-8"))
        return {}

    def put_cache(self, audio: bytes, mime: str, provider_id: str, text: str, voice: str) -> dict:
        cid = uuid.uuid4().hex[:12]
        ext = (mime.split("/")[-1] or "mp3").replace("mpeg", "mp3").replace("x-wav", "wav").replace("wav", "wav")
        if ext not in ("mp3", "wav", "ogg", "flac", "opus", "m4a", "aac", "silk"):
            ext = "mp3"
        f = self.cache_dir / f"{cid}.{ext}"
        f.write_bytes(audio)
        meta = self._load_cache_meta()
        meta[cid] = {"file": f.name, "mime": mime or f"audio/{ext}", "provider": provider_id,
                     "text": text[:120], "voice": voice, "bytes": len(audio), "ts": time.time()}
        self.cache_meta_path().write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
        return {"audio_id": cid, "url": f"/api/v1/tts/audio/{cid}", "mime": meta[cid]["mime"],
                "bytes": len(audio)}

    def get_cache(self, cid: str) -> dict | None:
        meta = self._load_cache_meta()
        entry = meta.get(cid)
        if not entry:
            return None
        f = self.cache_dir / entry["file"]
        if not f.exists():
            return None
        return {"path": f, "mime": entry["mime"], "entry": entry}

    def cache_stats(self) -> dict:
        meta = self._load_cache_meta()
        return {"entries": len(meta), "dir": str(self.cache_dir)}

def _base_from_endpoint(endpoint: str) -> str:
    """https://api.xiaomimimo.com/v1/chat/completions → https://api.xiaomimimo.com/v1"""
    if not endpoint:
        return ""
    from urllib.parse import urlsplit
    parts = urlsplit(endpoint)
    path = parts.path
    if "/" in path.lstrip("/"):
        root = "/" + path.lstrip("/").split("/")[0]
        return f"{parts.scheme}://{parts.netloc}{root}"
    return f"{parts.scheme}://{parts.netloc}"
