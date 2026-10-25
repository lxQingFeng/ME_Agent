"""事件重放:从 world.db 重建世界状态(审计/测试一致性)。
用法: python -m app.heartbeat.replay [db路径]
"""
from __future__ import annotations
import sys
import json
from datetime import datetime
from pathlib import Path
from app.heartbeat.models import WorldState, TimeState, ActivityState

def _meta(e: dict) -> dict:
    m = e.get("meta")
    if isinstance(m, str):
        try:
            return json.loads(m)
        except Exception:
            return {}
    return m or {}

def replay(eventlog) -> WorldState | None:
    events = eventlog.all()
    state: WorldState | None = None
    for e in events:
        ts = datetime.fromisoformat(e["ts"])
        if e["type"] == "init":
            state = WorldState(
                ts=ts, current=ActivityState(id=e["to_act"] or "sleep", started_at=ts),
                location=_meta(e).get("location", "家/床"),
                sleep_state="asleep" if (e["to_act"] == "sleep") else "awake",
                time=TimeState(date=ts.strftime("%Y-%m-%d"), hhmm=ts.strftime("%H:%M"), weekday=ts.weekday()),
                misc={"coffee_today": 0, "loc_since": ts.isoformat(), "anchors_done": {}},
            )
            state.version = 0
        elif e["type"] == "transition" and state is not None:
            to = e["to_act"]
            meta = _meta(e)
            state.version = e.get("version") or state.version + 1
            state.current = ActivityState(id=to, started_at=ts, task=meta.get("task"))
            if to.startswith("paused_"):
                state.current.interrupted = True
            if to == "sleep":
                state.sleep_state = "asleep"
            if to == "wake_routine":
                state.sleep_state = "awake"
            if e.get("location") and e["location"] != state.location:
                state.location = e["location"]
                state.misc["loc_since"] = ts.isoformat()
            state.ts = ts
            state.time = TimeState(date=ts.strftime("%Y-%m-%d"), hhmm=ts.strftime("%H:%M"), weekday=ts.weekday())
            # 锚点完成重放
            reason = e.get("reason") or ""
            meta_done = state.misc.setdefault("anchors_done", {})
            if reason == "anchor" and e.get("kind") == "anchor":
                pass
    # anchors_done 从 anchor 事件恢复
    for e in events:
        if e["type"] in ("anchor",) and state is not None:
            ts = datetime.fromisoformat(e["ts"])
            d = ts.strftime("%Y-%m-%d")
            lst = state.misc.setdefault("anchors_done", {}).setdefault(d, [])
            if e.get("reason") and e["reason"] not in lst:
                lst.append(e["reason"])
    return state

def main():
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("state/world.db")
    from app.heartbeat.eventlog import EventLog
    log = EventLog(db)
    st = replay(log)
    if st is None:
        print("no init event; empty world")
        return
    print(json.dumps(st.model_dump(mode="json"), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
