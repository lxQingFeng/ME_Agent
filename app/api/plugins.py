"""插件技能路由:列表 / 启停 / 手动调用 / 沙箱回连桥接(ME_INTERNAL_TOKEN)。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from app.api.schemas import PluginToggleIn, TTSBridgeIn

router = APIRouter(prefix="/api", tags=["plugins"])


def _plugin_view(name: str, st: dict, manifest_raw: dict | None) -> dict:
    m = manifest_raw or {}
    active = m.get("active") or {}
    trigger = []
    if active.get("every_minutes"):
        trigger.append(f"每 {active['every_minutes']} 分钟")
    if active.get("condition"):
        trigger.append(str(active["condition"]))
    return {
        "id": name,
        "name": m.get("name", name),
        "type": st.get("type", "passive"),
        "version": st.get("version"),
        "desc": st.get("description") or m.get("description", ""),
        "enabled": st.get("enabled", True),
        "perms": st.get("permissions") or [],
        "trigger": " / ".join(trigger) or ("被动触发" if st.get("type") == "passive" else "—"),
        "tool": st.get("tool"),
        "last_invoked_age_s": st.get("last_invoked_age_s"),
        "last_error": st.get("last_error"),
        "manifest": m,
    }


@router.get("/v1/plugins")
def list_plugins(request: Request):
    ctx = request.app.state.ctx
    reg = ctx.plugins
    out = []
    for st in reg.status():
        name = st["name"]
        p = Path(reg.plugins_dir) / name / "skill.yaml"
        raw: dict = {}
        try:
            import yaml
            raw = yaml.safe_load(p.read_text("utf-8")) or {}
        except Exception:
            raw = {}
        out.append(_plugin_view(name, st, raw))
    return out


@router.put("/v1/plugins/{name}/enabled")
def toggle_plugin(request: Request, name: str, body: PluginToggleIn):
    ctx = request.app.state.ctx
    ok = ctx.plugins.set_enabled(name, body.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail=f"plugin {name} not found")
    return {"ok": True, "id": name, "enabled": body.enabled}


class PluginTestIn(BaseModel):
    params: dict = {}
    context: dict = {}


@router.post("/v1/plugins/{name}/test")
async def test_plugin(request: Request, name: str, body: PluginTestIn):
    """手动调用一次插件(走沙箱), 返回 result/actions —— 冒烟与调试用。"""
    ctx = request.app.state.ctx
    try:
        out = await ctx.plugins.invoke(name, body.params, body.context)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"ok": True, "name": name, **out}


# ================= 内部回连(插件沙箱 → 控制台) =================
# 契约见 plugins/voice/main.py: POST {ME_CONSOLE_URL}/api/internal/tts/bridge
# 头 X-Internal-Token = ME_INTERNAL_TOKEN(未配置 token 时仅放行环回地址)

def _internal_ok(request: Request, ctx) -> None:
    token = ctx.settings.internal_token()
    supplied = request.headers.get("x-internal-token", "")
    if token:
        if supplied != token:
            raise HTTPException(status_code=401, detail="bad internal token")
    else:
        if request.client and request.client.host not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(status_code=401, detail="internal token not configured; loopback only")


@router.post("/internal/tts/bridge")
async def tts_bridge(request: Request, body: TTSBridgeIn,
                     x_internal_token: str = Header(default="")):
    ctx = request.app.state.ctx
    _internal_ok(request, ctx)
    out = await ctx.tts.speak_and_send(body.chat_id, body.chat_type, body.text, body.voice)
    if out.get("ok"):
        return out
    return {"ok": False, "error": out.get("error", "tts failed"), "detail": out.get("send_result", "")}
