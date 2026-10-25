"""CoherenceGuard:世界状态唯一合法写入点(方案 §2.6)。
规则 R1-R9 + 兜底链(①修正 ②寻路[锚点优先] ③替换)。任何绕过 guard 的状态写入都是 bug。
"""
from __future__ import annotations
from datetime import datetime
from collections import deque
from app.heartbeat.models import WorldState, TransitionProposal, GuardResult, ActivityState
from app.heartbeat.activities import ActivityLibrary

class LocationGraph:
    def __init__(self, cfg: dict):
        self.nodes = set(cfg.get("nodes", []))
        self.adj: dict[str, list[tuple[str, int]]] = {}
        self.default_minutes = int(cfg.get("default_travel_minutes", 30))
        for e in cfg.get("edges", []):
            self.adj.setdefault(e["a"], []).append((e["b"], int(e["minutes"])))
            self.adj.setdefault(e["b"], []).append((e["a"], int(e["minutes"])))

    def travel_minutes(self, a: str, b: str) -> int:
        if a == b:
            return 0
        if a not in self.adj or b not in self.adj:
            return self.default_minutes
        q: deque[tuple[str, int]] = deque([(a, 0)])
        seen = {a}
        while q:
            node, d = q.popleft()
            for nxt, w in self.adj.get(node, []):
                if nxt == b:
                    return d + w
                if nxt not in seen:
                    seen.add(nxt)
                    q.append((nxt, d + w))
        return self.default_minutes

class CoherenceGuard:
    def __init__(self, activities: ActivityLibrary, locations: LocationGraph,
                 schedule, dynamics_cfg: dict, admin_bypass: bool = False):
        self.activities = activities
        self.locations = locations
        self.schedule = schedule
        self.gates = dynamics_cfg.get("gates", {})
        self.admin_bypass = admin_bypass
        self.stats = {"proposals": 0, "rejected": 0,
                      "fallbacks": {"fix": 0, "pathfind": 0, "replace": 0}}

    # ---------- 校验:返回 [(code, blocking, detail)] ----------
    def validate(self, state: WorldState, p: TransitionProposal, now: datetime):
        v: list[tuple[str, bool, str]] = []
        act = self.activities
        compressed = bool(p.meta.get("compressed"))

        if p.from_activity != state.current.id:
            v.append(("R1", True, f"from={p.from_activity} != current={state.current.id}"))
        if not act.exists(p.to_activity):
            v.append(("R2", True, f"unknown target {p.to_activity}"))
            return v
        # R1 时长(软规则: 只记注记, 由 plan/interrupt 语义决定)
        elapsed = (now - state.current.started_at).total_seconds() / 60.0
        dur = act.duration_of(p.from_activity)
        if not compressed and elapsed < dur.min and p.reason not in ("interrupt", "admin", "fast_forward", "energy_gate"):
            v.append(("R1", False, f"too_short {elapsed:.0f}<{dur.min}min"))
        # R2 白名单边
        if not act.check_edge(p.from_activity, p.to_activity):
            v.append(("R2", True, f"no edge {p.from_activity} -> {p.to_activity}"))
        # R3 时间窗
        if not act.window_ok(p.to_activity, now) and not compressed:
            v.append(("R3", True, f"window closed for {p.to_activity}"))
        # R4 位置
        spec = act.get(p.to_activity)
        if p.to_location is None:
            p.to_location = state.location if state.location in spec.locations else spec.locations[0]
        if p.to_location not in spec.locations:
            v.append(("R4", True, f"location {p.to_location} invalid for {p.to_activity}"))
        elif not compressed:
            travel = self.locations.travel_minutes(state.location, p.to_location)
            if travel > 0 and p.reason not in ("admin", "fast_forward", "energy_gate"):
                loc_since_raw = state.misc.get("loc_since")
                loc_since = datetime.fromisoformat(loc_since_raw) if loc_since_raw else state.current.started_at
                moved = (now - loc_since).total_seconds() / 60.0
                if moved < travel:
                    v.append(("R4", False, f"travel {travel}min > at_location {moved:.0f}min"))
        # R5 能量门槛
        ok, why = act.energy_ok(p.to_activity, state.body.energy, self.gates)
        if not ok:
            v.append(("R5", True, why))
        # R6 睡眠门
        if state.sleep_state == "asleep" and p.to_activity not in ("wake_routine",):
            v.append(("R6", True, "asleep: only wake_routine allowed"))
        # R8 结构
        if p.to_activity.startswith("paused_") and not act.exists(p.to_activity[7:]):
            v.append(("R8", True, f"paused of unknown {p.to_activity}"))
        return v

    # ---------- 提交 ----------
    def _commit(self, state: WorldState, p: TransitionProposal, now: datetime) -> WorldState:
        new = state.model_copy(deep=True)
        new.version += 1
        to = p.to_activity
        new.current = ActivityState(id=to, started_at=now, task=p.task)
        if to.startswith("paused_"):
            new.current.interrupted = True
            new.misc["paused_of"] = to[7:]
        else:
            new.misc["paused_of"] = None
        if to == "sleep":
            new.sleep_state = "asleep"
        if to == "wake_routine":
            new.sleep_state = "awake"
        if p.to_location and p.to_location != state.location and self.locations.travel_minutes(state.location, p.to_location) > 0:
            new.location = p.to_location
            new.misc["loc_since"] = now.isoformat()
        new.ts = now
        new.time = _timestate(now)
        if to in ("breakfast", "lunch", "dinner", "snack"):
            new.body.hunger = 5.0
        return new

    # ---------- 入口 ----------
    def apply(self, state: WorldState, proposal: TransitionProposal, now: datetime) -> GuardResult:
        self.stats["proposals"] += 1
        violations = self.validate(state, proposal, now)
        blocking = [x for x in violations if x[1]]
        soft = [x for x in violations if not x[1]]
        if self.admin_bypass and proposal.reason == "admin" and not _hard_block(blocking):
            blocking = []
        if not blocking:
            new = self._commit(state, proposal, now)
            ev = _event(now, new.version, state.current.id, proposal)
            return GuardResult(ok=True, state=new, events=[ev],
                               notes=[f"{c}:{d}" for c, _, d in soft])
        self.stats["rejected"] += 1
        notes = [f"rejected: " + "; ".join(f"{c}:{d}" for c, _, d in blocking)]
        # 压缩跳(寻路内部提案)不得再触发兜底, 否则递归
        if proposal.meta.get("compressed") or proposal.reason == "fast_forward":
            return GuardResult(ok=False, rejected_reason="; ".join(f"{c}:{d}" for c, _, d in blocking),
                               state=state, notes=notes)
        # ---- 兜底① fix:位置就近(仅 R4 类阻挡) ----
        fixed = self._try_fix(state, proposal, now)
        if fixed is not None:
            self.stats["fallbacks"]["fix"] += 1
            return fixed
        # ---- 兜底② pathfind:锚点/计划/管理/心愿类提议优先寻路 ----
        if proposal.reason in ("anchor", "plan", "admin", "want", "energy_gate"):
            pf = self._try_pathfind(state, proposal, now)
            if pf is not None:
                self.stats["fallbacks"]["pathfind"] += 1
                return pf
        # ---- 兜底③ replace:时段默认活动 ----
        replaced = self._try_replace(state, proposal, now)
        if replaced is not None:
            self.stats["fallbacks"]["replace"] += 1
            return replaced
        # 寻路再试一次(replace 不可用时)
        if proposal.reason not in ("anchor", "plan", "admin", "want", "energy_gate"):
            pf = self._try_pathfind(state, proposal, now)
            if pf is not None:
                self.stats["fallbacks"]["pathfind"] += 1
                return pf
        return GuardResult(ok=False, rejected_reason="; ".join(f"{c}:{d}" for c, _, d in blocking),
                           state=state, notes=notes)

    def _try_fix(self, state, p, now) -> GuardResult | None:
        spec = self.activities.get(p.to_activity)
        if not spec or state.location not in spec.locations:
            return None
        p2 = p.model_copy(deep=True)
        p2.to_location = state.location
        p2.meta["fallback"] = "fix_location"
        remaining = self.validate(state, p2, now)
        if any(x[1] for x in remaining):
            return None
        new = self._commit(state, p2, now)
        ev = _event(now, new.version, state.current.id, p2)
        return GuardResult(ok=True, state=new, events=[ev], fallback="fix",
                           notes=[f"{c}:{d}" for c, _, d in remaining] + ["R4 fixed: keep current location"])

    def _try_replace(self, state, p, now) -> GuardResult | None:
        default_id = self.schedule.default_activity_for(now)
        if not self.activities.check_edge(state.current.id, default_id):
            for cand in self.activities.neighbors(state.current.id):
                spec = self.activities.get(cand)
                if (spec and spec.intensity in ("none", "light")
                        and self.activities.window_ok(cand, now)
                        and self.activities.check_edge(state.current.id, cand)):
                    default_id = cand
                    break
            else:
                return None
        if not self.activities.check_edge(state.current.id, default_id):
            return None
        spec = self.activities.get(default_id)
        if not self.activities.window_ok(default_id, now):
            return None
        ok, _ = self.activities.energy_ok(default_id, state.body.energy, self.gates)
        if not ok:
            return None
        p2 = TransitionProposal(
            from_activity=state.current.id, to_activity=default_id,
            to_location=state.location if state.location in spec.locations else spec.locations[0],
            task=f"兜底:{p.to_activity} 被拒,改做{spec.display}",
            reason=p.reason, intent=p.intent,
            meta={"fallback": "replace", "original_target": p.to_activity})
        new = self._commit(state, p2, now)
        ev = _event(now, new.version, state.current.id, p2)
        return GuardResult(ok=True, state=new, events=[ev], fallback="replace")

    def _try_pathfind(self, state, p, now) -> GuardResult | None:
        path = self.activities.bfs_path(state.current.id, p.to_activity, now)
        if len(path) < 2:
            return None
        cur = state
        events: list[dict] = []
        for a, b in zip(path, path[1:]):
            spec = self.activities.get(b)
            p2 = TransitionProposal(
                from_activity=a, to_activity=b,
                to_location=(spec.locations[0] if spec else state.location),
                task=p.task if b == path[-1] else None,
                reason="fast_forward", intent=p.intent,
                meta={"compressed": True, "fallback_path": path, "original_reason": p.reason})
            res = self.apply(cur, p2, now)
            if not res.ok:
                return None
            cur = res.state
            events.extend(res.events)
        return GuardResult(ok=True, state=cur, events=events, fallback="pathfind",
                           notes=[f"fallback pathfind: {' → '.join(path)}"])

def _hard_block(blocking) -> bool:
    return bool(blocking)

def _timestate(now: datetime):
    from app.heartbeat.models import TimeState
    return TimeState(date=now.strftime("%Y-%m-%d"), hhmm=now.strftime("%H:%M"), weekday=now.weekday())

def _event(now: datetime, version: int, from_act: str, p: TransitionProposal) -> dict:
    return {
        "ts": now.isoformat(), "type": "transition", "kind": p.reason,
        "from": from_act, "to": p.to_activity,
        "location": p.to_location, "reason": p.reason,
        "meta": {**p.meta, "task": p.task, "version": version},
        "version": version,
    }
