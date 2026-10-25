"""世界心跳路由:WorldState 快照 + 转移时间线(console LIVE_ROUTES 对接)。"""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/v1/world", tags=["world"])


def _circadian(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    return "night"


def world_state_view(ctx) -> dict:
    """把引擎 WorldState 映射成 console 前端期望的展示形态(字段对齐方案 §2.3)。"""
    eng = ctx.engine
    st = eng.state
    spec = eng.activities.get(st.current.id)
    snap = eng.snapshot()
    loc_since = st.misc.get("loc_since")
    # 计划时段: duration.typ 分钟
    try:
        typ = int(spec.duration.typ) if spec else 20
    except Exception:
        typ = 20
    planned_end = st.current.started_at.timestamp() + typ * 60
    misc = st.misc or {}
    weather = misc.get("weather") or {"condition": "未接入", "temp_c": None, "city": "—"}
    return {
        "ok": True,
        "time": {
            "wall_clock": st.ts.isoformat(),
            "circadian": _circadian(st.ts.hour),
        },
        "environment": {"weather": weather, "special_day": misc.get("special_day")},
        "body": {
            "energy": round(st.body.energy, 1),
            "sleep_state": st.sleep_state,
            "hunger": round(st.body.hunger, 1),
        },
        "mind": {
            "mood": {
                "valence": round(st.mood.valence, 2),
                "arousal": round(st.mood.arousal, 2),
                "label": eng.renderer._mood_label(st),
            },
            "stress": round(st.body.stress, 1),
            "wants": [{"text": w.content, "strength": w.strength} for w in st.wants],
        },
        "social": {
            "battery": round(st.social.battery, 1),
            "recent_density": st.social.density,
        },
        "current": {
            "location": st.location,
            "activity": {
                "id": st.current.id,
                "name": spec.display if spec else st.current.id,
                "start": st.current.started_at.isoformat(),
                "planned_end": planned_end,
                "intensity": spec.intensity if spec else "light",
                "interruptible": spec.interruptible if spec else "medium",
            },
            "task": st.current.task,
            "intent": misc.get("intent"),
            "interrupt_queue": misc.get("interrupt_queue", []),
        },
        "meta": {
            "version": st.version,
            "tick_count": snap.get("tick_count"),
            "guard_stats": snap.get("guard_stats"),
            "loc_since": loc_since,
        },
        "narrative": snap.get("rendered"),
        "anchors_today": snap.get("anchors_today", []),
        "recent_events": snap.get("recent_events", []),
    }


def transitions_view(ctx, limit: int = 60) -> list[dict]:
    import json as _json
    evts = ctx.engine.eventlog.tail(limit)
    out = []
    for e in evts:
        if e.get("type") not in ("transition", "init", "wake", "sleep"):
            continue
        meta = e.get("meta")
        try:
            meta = _json.loads(meta) if isinstance(meta, str) else (meta or {})
        except Exception:
            meta = {}
        out.append({
            "at": e.get("ts"),
            "from": e.get("from_act"),
            "to": e.get("to_act") or e.get("type"),
            "location": e.get("location"),
            "reason": e.get("reason") or e.get("type"),
            "meta": meta,
        })
    return out


@router.get("/state")
def world_state(request: Request):
    ctx = request.app.state.ctx
    return world_state_view(ctx)


@router.get("/transitions")
def world_transitions(request: Request, limit: int = 60):
    ctx = request.app.state.ctx
    return {"ok": True, "transitions": transitions_view(ctx, limit)}


@router.get("/activities")
def activity_graph(request: Request):
    """活动库 + 转移边(page-heartbeat 的 activityEdges/锚点数据源)。"""
    ctx = request.app.state.ctx
    lib = ctx.engine.activities
    nodes = []
    for aid, spec in lib.specs.items():
        nodes.append({
            "id": aid, "name": spec.display, "intensity": spec.intensity,
            "interruptible": spec.interruptible, "next": spec.next,
            "share_worth": spec.share_worth,
            "locations": spec.locations, "time_window": spec.time_window,
        })
    edges = []
    for a, spec in lib.specs.items():
        for b in spec.next:
            if lib.check_edge(a, b):
                edges.append(f"{a} → {b}")
    anchors = []
    now = ctx.engine.clock.now()
    for an in ctx.engine.schedule.anchors_for(now):
        anchors.append({"id": an.id, "at": an.at, "activity": an.activity, "hard": an.hard})
    return {"ok": True, "nodes": nodes, "edges": edges, "anchors": anchors}
