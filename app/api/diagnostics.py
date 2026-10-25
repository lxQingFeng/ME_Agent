"""诊断导出: GET /api/v1/diagnostics/export(2026-10-25 补强)。

排障时一键导出诊断包 JSON, 严禁携带任何密钥明文:
- runtime      运行状态(来自 ctx.status(): 引擎/网关/记忆/TTS 汇总);
- components   组件健康(网关/记忆/LLM/TTS/程序/插件 逐项);
- recent_errors 近期错误摘要(telemetry 错误环, WARNING 起步);
- config       配置密钥指纹: 全部按 SECRET_KEY_PATTERN 脱敏为 hasXxx 布尔,
               *_env 环境变量名指针保留(名字本身不是密钥), TTS 两段原文
               只输出存在性指纹(hasRequestHeader / hasBodyTemplate)。

现有含密钥的 API 输出(TTS 配置回显)已统一改走 app.api.sanitizer。
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from app.api.sanitizer import sanitize_tree

router = APIRouter(prefix="/api/v1/diagnostics", tags=["diagnostics"])

# 环境变量密钥指纹清单(名字硬编码白名单, 只输出 hasXxx)
ENV_SECRET_KEYS = (
    "ONEBOT_ACCESS_TOKEN",
    "ME_INTERNAL_TOKEN",
    "DEEPSEEK_API_KEY",
    "HINDSIGHT_API_KEY",
)

# 参与配置指纹的 yaml(敏感字段统一 sanitize_tree)
CONFIG_FINGERPRINT_FILES = (
    "channels.yaml", "console.yaml", "llm.yaml", "memory.yaml",
    "tts.yaml", "programs.yaml",
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_flag_name(name: str) -> str:
    """ONEBOT_ACCESS_TOKEN -> hasOnebotAccessToken(驼峰指纹名)。"""
    parts = [p for p in name.lower().split("_") if p]
    return "has" + "".join(p.capitalize() for p in parts)


def _env_fingerprint(settings) -> dict:
    out: dict = {"env_file": bool((settings.root / ".env").exists())}
    for name in ENV_SECRET_KEYS:
        out[_env_flag_name(name)] = bool(os.environ.get(name))
    return out


def _config_fingerprint(settings) -> dict:
    out: dict = {}
    import yaml
    from app.config import CONFIG_DIR
    for fname in CONFIG_FINGERPRINT_FILES:
        p = CONFIG_DIR / fname
        try:
            data = yaml.safe_load(p.read_text("utf-8")) or {}
        except Exception as e:
            out[fname] = {"error": f"unreadable: {type(e).__name__}"}
            continue
        out[fname] = sanitize_tree(data)
    return out


def _tts_fingerprint(ctx) -> dict:
    """TTS Provider 档案指纹: 只输出形态/端点/存在性, 两段原文绝不导出。"""
    try:
        pid = ctx.settings.tts_cfg.get("default_provider_id", "") or "default"
        p = ctx.tts_store.load(pid)
        return {
            "provider_id": pid,
            "kind": p.kind,
            "endpoint": p.text_prompt.endpoint if p.kind == "text_prompt" else p.base_url,
            "model": p.default_model or p.preset_voice.model or "",
            "hasApiKey": bool(p.api_key),
            "hasRequestHeader": (ctx.tts_store.read_raw(pid, "request_header") is not None),
            "hasBodyTemplate": (ctx.tts_store.read_raw(pid, "request_body") is not None),
            "updated_at": p.updated_at,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _components(ctx) -> dict:
    gw = ctx.gateway.status()
    programs = [{"id": x.get("id"), "kind": x.get("kind"), "status": x.get("status")}
                for x in ctx.programs.list()]
    plugins = ctx.plugins.status()
    return {
        "gateway": gw,
        "memory": ctx.memory.status(),
        "llm": {"enabled": ctx.llm.enabled, "hasApiKey": bool(ctx.settings.llm_api_key())},
        "tts": ctx.tts.status(),
        "engine": ctx.status().get("engine", {}),
        "programs": programs,
        "plugins": {"count": len(plugins)},
    }


@router.get("/export")
def export_diagnostics(request: Request) -> dict:
    ctx = request.app.state.ctx
    bundle = {
        "ok": True,
        "exported_at": _iso_now(),
        "process": {"pid": os.getpid(), "python": sys.version.split()[0],
                    "argv0": sys.argv[0]},
        "runtime": ctx.status(),
        "components": _components(ctx),
        "recent_errors": ctx.telemetry.snapshot(20),
        "config": {
            "mode": "diagnostic-fingerprint",
            "note": "全部密钥按 SECRET_KEY_PATTERN 脱敏为 hasXxx 布尔指纹, 不含任何明文",
            "env": _env_fingerprint(ctx.settings),
            "files": _config_fingerprint(ctx.settings),
            "tts_provider": _tts_fingerprint(ctx),
        },
    }
    return sanitize_tree(bundle)
