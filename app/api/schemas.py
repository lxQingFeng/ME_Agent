"""API 请求/响应模型(对接 console/js/api.js 契约)。"""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


# ---- 对话 ----
class SendIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    sender_id: str = "console"
    sender_name: str = "控制台"


# ---- 插件 ----
class PluginToggleIn(BaseModel):
    enabled: bool


# ---- TTS ----
class TTSConfigIn(BaseModel):
    """PUT /api/v1/tts/config 请求体(console page-tts 保存动作)。

    headerTemplateRaw / bodyTemplateRaw 为两段原文, 服务端只做占位符替换、
    按字节落盘, 保证「保存→读回」往返一致(TTS 发送保真铁律)。
    """
    endpoint: str = ""
    apiKey: str = ""
    model: str = ""
    modelType: Literal["prompt", "preset", "clone"] = "prompt"
    voice: str = ""
    cloneAudioId: str = ""
    headerTemplateRaw: str = ""
    bodyTemplateRaw: str = ""
    providerId: str = ""      # 可选: 指定 provider; 缺省用默认 provider


class TTSProbeIn(BaseModel):
    """拉模型列表/检查配置: 从 endpoint 推导 API 根。"""
    endpoint: str = ""
    key: str = ""
    model: str = ""


class TTSSynthesizeIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    model: str = ""
    modelType: Literal["prompt", "preset", "clone"] = "prompt"
    voice: str = ""
    cloneAudioId: str = ""


class TTSBridgeIn(BaseModel):
    """插件回连桥接(内部令牌保护): Agent/插件请求合成并发送。"""
    text: str = Field(min_length=1, max_length=2000)
    voice: str | None = None
    chat_id: str = ""
    chat_type: str = "group"


# ---- 系统 ----
class EngineControlIn(BaseModel):
    action: Literal["tick", "pause", "resume"]
