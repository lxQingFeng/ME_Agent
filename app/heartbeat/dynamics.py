"""状态动力学(方案 §2.5):能量/情绪/社交电池/饥饿的每拍演化。"""
from __future__ import annotations
from datetime import datetime
from app.heartbeat.models import WorldState, Want, TransitionProposal

MEALS = {"breakfast", "lunch", "dinner", "snack"}

class Dynamics:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.drain = cfg.get("drain_per_tick", {"none": 0.05, "light": 0.25, "medium": 0.5, "high": 0.9, "extreme": 1.6})
        self.recharge = cfg.get("recharge_per_tick", {})
        b = cfg.get("baseline", {})
        self.baseline_valence = float(b.get("valence", 0.2))
        self.baseline_arousal = float(b.get("arousal", 0.1))
        soc = cfg.get("social", {})
        self.per_msg = float(soc.get("per_message_cost", 0.25))
        self.reply_cost = float(soc.get("reply_cost", 0.8))
        self.alone_recharge = float(soc.get("alone_recharge_per_tick", 0.8))
        hun = cfg.get("hunger", {})
        self.hunger_rise = float(hun.get("rise_per_tick", 0.45))
        self.hunger_threshold = float(hun.get("threshold", 55))
        self.wants_cfg = cfg.get("wants", {})

    def intensity_of(self, state: WorldState, library) -> str:
        if state.sleep_state == "asleep":
            return "none"
        spec = library.get(state.current.id)
        return spec.intensity if spec else "light"

    def step(self, state: WorldState, now: datetime, library) -> tuple[WorldState, list[dict], list[TransitionProposal]]:
        """每拍推进;返回(新状态, 阈值事件, 强制提议)。数值变更仍属"规则直写",
        但阈值跨越会记事件, 结构转移一律走 guard。"""
        new = state.model_copy(deep=True)
        events: list[dict] = []
        proposals: list[TransitionProposal] = []
        intensity = self.intensity_of(state, library)

        # ---- 能量 ----
        if state.sleep_state == "asleep":
            new.body.energy = min(100, new.body.energy + float(self.cfg.get("sleep_recharge_per_tick", 1.05)))
        else:
            d = self.drain.get(intensity, 0.25)
            stress_extra = (new.body.stress / 100.0) * float(self.cfg.get("stress_extra_factor", 0.003)) * 100 / 100 * (5 / 5)
            rec = float(self.recharge.get(state.current.id, 0.0)) * (0.0 if state.current.id.startswith("paused_") else 1.0)
            new.body.energy = max(0, min(100, new.body.energy - d + rec - stress_extra))
        # ---- 饥饿 ----
        new.body.hunger = min(100, new.body.hunger + self.hunger_rise)
        # ---- 压力向基线回归 ----
        new.body.stress = max(0, min(100, new.body.stress + (5 - new.body.stress) * 0.02))
        # ---- 情绪回归 + 唤醒跟随强度 ----
        reg = float(self.cfg.get("mood_regression_per_tick", 0.03))
        new.mood.valence = self.baseline_valence + (new.mood.valence - self.baseline_valence) * (1 - reg)
        target_arousal = {"none": 0.1, "light": 0.15, "medium": 0.3, "high": 0.5, "extreme": 0.6}[intensity]
        new.mood.arousal = target_arousal + (new.mood.arousal - target_arousal) * 0.7
        # ---- 社交电池 ----
        density = new.social.density
        if density > 0:
            new.social.battery = max(0, new.social.battery - self.per_msg * density)
        elif intensity != "light" or new.current.id != "chat_online":
            new.social.battery = min(100, new.social.battery + self.alone_recharge)

        # ---- 阈值事件(带状, 只记跨越) ----
        for band in (75, 50, 25, 10):
            if _crossed(state.body.energy, new.body.energy, band):
                events.append({"ts": now.isoformat(), "type": "dynamics", "kind": "energy_band",
                               "meta": {"band": band, "now": round(new.body.energy, 1)}})
        for band in (50, 25, 10):
            if _crossed(state.social.battery, new.social.battery, band):
                events.append({"ts": now.isoformat(), "type": "dynamics", "kind": "battery_band",
                               "meta": {"band": band, "now": round(new.social.battery, 1)}})
        if _crossed_up(state.body.hunger, new.body.hunger, self.hunger_threshold):
            events.append({"ts": now.isoformat(), "type": "dynamics", "kind": "hungry",
                           "meta": {"hunger": round(new.body.hunger, 1)}})
            self._add_want(new, "吃点东西", 5, now)

        # ---- 强制提议(能量闸门) ----
        gates = self.cfg.get("gates", {})
        cur_id = new.current.id
        if not cur_id.startswith("paused_") and state.sleep_state == "awake":
            spec = library.get(cur_id)
            cur_intensity = spec.intensity if spec else "light"
            if new.body.energy < float(gates.get("force_rest_energy", 8)) and cur_intensity in ("medium", "high", "extreme"):
                proposals.append(TransitionProposal(
                    from_activity=cur_id, to_activity="rest",
                    to_location=None, task="没电了,歇会儿",
                    reason="energy_gate", meta={"forced": True}))
            if new.body.energy < float(gates.get("force_sleep_energy", 3)) and self._night(now):
                path = library.bfs_path(cur_id, "sleep", now)
                if len(path) >= 2:
                    proposals.append(TransitionProposal(
                        from_activity=path[0], to_activity=path[1],
                        to_location=None, task="电量见底,睡了",
                        reason="energy_gate", meta={"forced": True, "sleep_path": path}))
        # 想吃东西 → want 已加;若当前可去厨房方向,交给引擎下一拍决策
        new.social.density = 0   # 本拍密度清零(由 observe 累计下一拍)
        return new, events, proposals

    def _night(self, now: datetime) -> bool:
        return now.hour >= 22 or now.hour <= 4

    def _add_want(self, state: WorldState, content: str, strength: int, now: datetime) -> None:
        if any(w.content == content for w in state.wants):
            return
        max_active = int(self.wants_cfg.get("max_active", 3))
        if len(state.wants) >= max_active:
            state.wants.pop(0)
        state.wants.append(Want(content=content, strength=strength, created_at=now,
                                 ttl_hours=float(self.wants_cfg.get("decay_hours", 24))))

    def decay_wants(self, state: WorldState, now: datetime) -> list[str]:
        before = [w.content for w in state.wants]
        state.wants = [w for w in state.wants
                       if (now - w.created_at).total_seconds() / 3600.0 < w.ttl_hours]
        return [c for c in before if c not in [w.content for w in state.wants]]

    def mood_label(self, state: WorldState) -> str:
        v, a = state.mood.valence, state.mood.arousal
        if v > 0.3 and a > 0.3: return "开心"
        if v > 0.3: return "满足"
        if v < -0.3 and a > 0.4: return "烦躁"
        if v < -0.3: return "低落"
        if a > 0.5: return "兴奋"
        if state.body.energy < 25: return "疲惫"
        return "平静"

def _crossed(a: float, b: float, band: float) -> bool:
    return (a > band >= b) or (a < band <= b)

def _crossed_up(a: float, b: float, band: float) -> bool:
    return a < band <= b
