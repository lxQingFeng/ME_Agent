"""设置路由:LLM 与人设(第 11 轮, 对接 console V5 设置页)。

契约:
  GET  /api/v1/settings/llm          读回 LLM 配置(api_key 脱敏为 "***", 不回明文)
  PUT  /api/v1/settings/llm          保存(落 state/settings/llm.yaml, git 不追踪)
                                      + 热生效(重建客户端并接线 pipeline/engine)
  POST /api/v1/settings/llm/models   模型列表代理(后端直连 /models, 规避 CORS,
                                      密钥不经浏览器)
  GET  /api/v1/settings/persona      读回人设(名字/性格/风格规则/群规摘要/兜底回复)
  PUT  /api/v1/settings/persona      保存(落 state/settings/persona.yaml) + 热生效
                                      (对话管道每次现取 settings.persona, reload 即生效)

保存语义(api_key 三态, 与 TTS 页一致):
  - 请求体缺 api_key 字段或值为 "***"(脱敏回传) → 保留旧值不覆盖;
  - 值为空串 "" → 显式清除(回落到环境变量 DEEPSEEK_API_KEY, 若有);
  - 其他非空值 → 新密钥(明文仅存本机 state 目录, 任何 API 响应不含明文)。

安全口径: state/ 在 .gitignore(密钥不入库); 控制台端口仅本机使用, 勿暴露公网。
"""
from __future__ import annotations

import re
from typing import Literal

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.sanitizer import mask_display

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


# ---------- 请求体 ----------
class LLMSettingsIn(BaseModel):
    provider: str | None = None          # 标签(deepseek/openai/custom), 仅展示用
    base_url: str | None = None
    api_key: str | None = None           # None/含*** → 保留; "" → 清除; 其他 → 新值
    chat_model: str | None = None
    planner_model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tool_rounds: int | None = Field(default=None, ge=1, le=8)
    timeout_s: float | None = Field(default=None, ge=5.0, le=300.0)


class PersonaSettingsIn(BaseModel):
    name: str | None = Field(default=None, max_length=40)
    essence: str | None = Field(default=None, max_length=8000)       # 性格/设定(系统提示词核心)
    style_rules: list[str] | None = None                              # 说话风格规则
    group_rules_digest: str | None = Field(default=None, max_length=8000)
    reply_fallbacks: list[str] | None = None                          # 无 LLM 时的模板回复


class LLMModelsIn(BaseModel):
    base_url: str = ""
    api_key: str = ""


# ---------- 工具 ----------
def _overlay_path(ctx, name: str):
    return ctx.settings.state_dir / "settings" / name


def _load_overlay(ctx, name: str) -> dict:
    p = _overlay_path(ctx, name)
    if p.exists():
        try:
            data = yaml.safe_load(p.read_text("utf-8")) or {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _save_overlay(ctx, name: str, data: dict) -> None:
    p = _overlay_path(ctx, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8")
    tmp.replace(p)


def _guess_provider(base_url: str) -> str:
    host = re.sub(r"^https?://", "", base_url or "").split("/")[0].lower()
    for kw in ("deepseek", "openai", "anthropic", "moonshot", "siliconflow", "zhipu", "bigmodel",
               "dashscope", "aliyun", "volces", "gemini", "google", "azure", "local", "127.0.0.1",
               "localhost", "ollama", "vllm", "lmstudio"):
        if kw in host:
            return "local" if kw in ("127.0.0.1", "localhost", "ollama", "vllm", "lmstudio") else kw
    return "custom"


def _llm_view(ctx) -> dict:
    """LLM 配置脱敏视图(GET 与 PUT 响应共用, 严禁明文 key)。"""
    cfg = ctx.settings.llm_cfg
    key_state = ctx.settings.llm_api_key()
    overlay_has_key = bool(str(cfg.get("api_key", "") or "").strip())
    env_name = cfg.get("api_key_env", "DEEPSEEK_API_KEY")
    env_val = ctx.settings.env(env_name)
    source = "state" if overlay_has_key else ("env" if env_val else "none")
    base_url = str(cfg.get("base_url", "") or "")
    return {
        "ok": True,
        "provider": str(cfg.get("provider") or "") or _guess_provider(base_url),
        "base_url": base_url,
        "api_key": mask_display(key_state),
        "has_api_key": bool(key_state),
        "api_key_source": source,
        "api_key_env_name": env_name,
        "chat_model": str(cfg.get("chat_model", "") or ""),
        "planner_model": str(cfg.get("planner_model", "") or ""),
        "temperature": cfg.get("temperature", 0.7),
        "max_tool_rounds": cfg.get("max_tool_rounds", 2),
        "timeout_s": cfg.get("timeout_s", 30),
        "enabled": bool(key_state and base_url),
    }


def _persona_view(ctx) -> dict:
    p = ctx.settings.persona
    style = p.get("style") or {}
    return {
        "ok": True,
        "name": str(p.get("name", "") or ""),
        "essence": str(p.get("essence", "") or ""),
        "style": {"rules": list(style.get("rules") or [])},
        "group_rules_digest": str(p.get("group_rules_digest", "") or ""),
        "reply_fallbacks": list(p.get("reply_fallbacks") or []),
    }


# ---------- LLM ----------
@router.get("/llm")
def get_llm(request: Request):
    return _llm_view(request.app.state.ctx)


@router.put("/llm")
async def put_llm(request: Request, body: LLMSettingsIn):
    ctx = request.app.state.ctx
    overlay = _load_overlay(ctx, "llm.yaml")

    if body.provider is not None:
        overlay["provider"] = body.provider.strip()
    if body.base_url is not None:
        u = body.base_url.strip()
        if u and not re.match(r"^https?://", u):
            raise HTTPException(status_code=400,
                                detail="base_url 必须以 http:// 或 https:// 开头")
        overlay["base_url"] = u
    if body.chat_model is not None:
        overlay["chat_model"] = body.chat_model.strip()
    if body.planner_model is not None:
        overlay["planner_model"] = body.planner_model.strip()
    if body.temperature is not None:
        overlay["temperature"] = body.temperature
    if body.max_tool_rounds is not None:
        overlay["max_tool_rounds"] = body.max_tool_rounds
    if body.timeout_s is not None:
        overlay["timeout_s"] = body.timeout_s
    if body.api_key is not None and "***" not in body.api_key:
        # "" → 显式清除(删除覆盖层键, 回落环境变量); 非空 → 新密钥
        if body.api_key.strip():
            overlay["api_key"] = body.api_key.strip()
        else:
            overlay.pop("api_key", None)

    if overlay.get("base_url") is None and not ctx.settings.llm_cfg.get("base_url"):
        raise HTTPException(status_code=400, detail="base_url 不能为空(先填接口地址)")

    _save_overlay(ctx, "llm.yaml", overlay)
    applied = await ctx.apply_llm_settings()   # 热生效: 重建客户端 + 接线 pipeline/engine
    out = _llm_view(ctx)
    out["applied"] = applied
    return out


@router.post("/llm/models")
async def llm_models(request: Request, body: LLMModelsIn):
    """拉模型列表(前端「选择模型」用): 后端代理 GET {base_url}/models。

    body 省略 base_url/api_key 或传脱敏值("***")时, 用已保存配置;
    密钥只在本后端与 LLM 服务之间流动, 不进浏览器。
    """
    ctx = request.app.state.ctx
    cfg = ctx.settings.llm_cfg
    base = (body.base_url or "").strip()
    if not base or "***" in base:
        base = str(cfg.get("base_url", "") or "")
    base = base.rstrip("/")
    if not base:
        return {"ok": False, "error": "base_url 未配置(先在设置页保存接口地址)"}
    key = (body.api_key or "").strip()
    if not key or "***" in key:
        key = ctx.settings.llm_api_key()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{base}/models", headers=headers)
        if resp.status_code != 200:
            return {"ok": False, "error": f"上游返回 HTTP {resp.status_code}",
                    "detail": resp.text[:200]}
        data = resp.json()
        models = []
        for item in (data.get("data") or []):
            mid = item.get("id") if isinstance(item, dict) else None
            if mid:
                models.append(str(mid))
        models = sorted(set(models))   # 去重排序(模型选择器友好)
        return {"ok": True, "models": models, "count": len(models)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------- 人设 ----------
@router.get("/persona")
def get_persona(request: Request):
    return _persona_view(request.app.state.ctx)


@router.put("/persona")
async def put_persona(request: Request, body: PersonaSettingsIn):
    ctx = request.app.state.ctx
    overlay = _load_overlay(ctx, "persona.yaml")

    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(status_code=400, detail="Agent 名字不能为空")
        overlay["name"] = body.name.strip()
    if body.essence is not None:
        overlay["essence"] = body.essence
    if body.style_rules is not None:
        rules = [r.strip() for r in body.style_rules if r.strip()]
        if not rules:
            raise HTTPException(status_code=400, detail="风格规则至少保留一条")
        overlay["style"] = {"rules": rules}
    if body.group_rules_digest is not None:
        overlay["group_rules_digest"] = body.group_rules_digest
    if body.reply_fallbacks is not None:
        overlay["reply_fallbacks"] = [r for r in body.reply_fallbacks if r.strip()]

    _save_overlay(ctx, "persona.yaml", overlay)
    ctx.settings.reload()   # 人设无状态: 管道每次现取 persona, reload 即热生效
    out = _persona_view(ctx)
    out["applied"] = {"persona_name": ctx.settings.persona.get("name", "")}
    return out
