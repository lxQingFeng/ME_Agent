"""TTS 数据模型:三类形态(文字提示语音/预设音色/语音克隆)。
⚠️ 原始请求头/体两段文本【不】存在本模型里 —— 单独以 .txt 原文落盘(字节级保真),
meta.json 只存结构化设置, 避免 JSON 序列化/转义造成任何往返差异。"""
from __future__ import annotations
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field

Kind = Literal["text_prompt", "preset_voice", "voice_clone"]

class Voice(BaseModel):
    value: str            # 音色 id(发给 API 的)
    label: str            # 中文名(界面展示, 4.2 要求"官方选择音色需要中文")
    desc: str = ""
    source: str = "builtin"   # builtin / custom / discovered / cloned

class ClonedVoice(BaseModel):
    id: str
    name: str
    status: Literal["local", "cloning", "active", "failed"] = "local"
    remote_id: str | None = None
    audio_file: str | None = None
    created_at: str = ""

class TextPromptSettings(BaseModel):
    endpoint: str = ""                  # 完整请求 URL;空则用 base_url + path_hint
    method: str = "POST"
    response_extract: str = "auto"      # auto 或 dotted jsonpath 如 choices.0.message.content
    path_hint: str = "/chat/completions"

class PresetVoiceSettings(BaseModel):
    voices: list[Voice] = Field(default_factory=list)
    sample_text: str = "你好,这是音色试听。"

class CloneSettings(BaseModel):
    endpoint: str = ""                  # 克隆调用 URL;空=仅本地登记
    method: str = "POST"
    multipart_field: str = "file"       # multipart 模式的音频字段名
    extra_fields: dict[str, str] = Field(default_factory=dict)
    use_raw_templates: bool = False     # True=走 clone_header.txt/clone_body.txt 原文模板(同字节保真规则)
    response_voice_field: str = "auto"  # auto=扫 id/voice_id/speaker_id

class VoiceCloneSettings(BaseModel):
    clone: CloneSettings = Field(default_factory=CloneSettings)
    cloned: list[ClonedVoice] = Field(default_factory=list)

class TTSProvider(BaseModel):
    id: str
    name: str = ""
    kind: Kind = "text_prompt"
    base_url: str = ""                  # API 根(如 https://api.xiaomimimo.com/v1), 模型列表 GET {base}/models
    api_key: str = ""
    model: str = ""
    default_voice: str = ""
    speed: float = 1.0
    format: str = "mp3"
    enabled: bool = True
    notes: str = ""
    text_prompt: TextPromptSettings = Field(default_factory=TextPromptSettings)
    preset_voice: PresetVoiceSettings = Field(default_factory=PresetVoiceSettings)
    voice_clone: VoiceCloneSettings = Field(default_factory=VoiceCloneSettings)
    created_at: str = ""
    updated_at: str = ""

class SynthResult(BaseModel):
    ok: bool = False
    audio: bytes = b""
    mime: str = ""
    extract_path: str = ""
    status_code: int | None = None
    elapsed_ms: int = 0
    error: str = ""
    debug: dict = Field(default_factory=dict)
