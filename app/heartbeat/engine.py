"""世界心跳引擎:每 5 分钟一拍,世界向前走一步(方案 §2 总装)。
P2 级事件(会话闲聊不给引擎让路), P0 抢占由 arbiter 调用 attend()。
"""
from __future__ import annotations
import asyncio
import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from app.config import Settings
from app.clock import Clock
from app.bus import EventBus
from app.heartbeat.models import (WorldState, TimeState, ActivityState, TransitionProposal, TickReport)
from app.heartbeat.activities import ActivityLibrary
from app.heartbeat.schedule import Schedule
from app.heartbeat.dynamics import Dynamics
from app.heartbeat.guard import CoherenceGuard, LocationGraph
from app.heartbeat.planner import DayPlanner
from app.heartbeat.renderer import Renderer
from app.heartbeat.eventlog import EventLog

class HeartbeatEngine:
    def __init__(self, settings: Settings, clock: Clock, eventlog: EventLog,
                 bus: EventBus | None = None, llm=None):
        self.settings = settings
        self.clock = clock
        self.eventlog = eventlog
        self.bus = bus
        self.llm = llm
        self.rng = random.Random()
        self.activities = ActivityLibrary(settings.activities_cfg["activities"])
        self.locations = LocationGraph(settings.locations_cfg)
        self.schedule = Schedule(settings.routine, settings.tz)
        self.dynamics = Dynamics(settings.dynamics_cfg)
        self.guard = CoherenceGuard(self.activities, self.locations, self.schedule,
                                    settings.dynamics_cfg)
        self.planner = DayPlanner(self.schedule, self.activities, settings.dynamics_cfg,
                                  settings.state_dir / "plans", llm=llm)
        self.renderer = Renderer(self.activities)
        self.state_path = settings.state_dir / "world.json"
        self.state = self._load_or_init()
        self.on_retain = None       # async (text, kind, metadata) -> None
        self.on_broadcast = None    # async (candidate, ctx) -> None
        self._last_retain_id = self.eventlog.last_id()
        self._last_retain_ts: datetime | None = None
        self._p2_ring: list[dict] = []
        self._rejected_cooldown: dict[tuple, datetime] = {}
        self._running = False
        self.tick_count = 0

    # ---------- 初始化/持久化 ----------
    def _load_or_init(self) -> WorldState:
        if self.state_path.exists():
            data = json.loads(self.state_path.read_text("utf-8"))
            return WorldState.model_validate(data)
        now = self.clock.now()
        default = self.schedule.default_activity_for(now)
        spec = self.activities.get(default)
        loc = spec.locations[0] if spec else "家/床"
        state = WorldState(
            ts=now,
            current=ActivityState(id=default, started_at=now),
            location=loc,
            sleep_state="asleep" if default == "sleep" else "awake",
            time=TimeState(date=now.strftime("%Y-%m-%d"), hhmm=now.strftime("%H:%M"), weekday=now.weekday()),
            body=__import__("app.heartbeat.models", fromlist=["BodyState"]).BodyState(
                energy=float(self.settings.dynamics_cfg.get("baseline", {}).get("energy", 100))),
            misc={"coffee_today": 0, "loc_since": now.isoformat(), "anchors_done": {}},
        )
        self._save(state)
        self.eventlog.append(ts=now, type="init", to_act=default, reason="init",
                             meta={"location": loc}, version=0)
        return state

    def _save(self, state: WorldState | None = None) -> None:
        st = state or self.state
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(st.model_dump_json(indent=2), "utf-8")
        tmp.replace(self.state_path)

    # ---------- 主循环 ----------
    async def run(self) -> None:
        self._running = True
        tick_s = int(self.settings.console.get("engine", {}).get("tick_seconds", 300))
        while self._running:
            try:
                await self.tick()
            except Exception as e:  # 引擎永不死
                self.eventlog.append(ts=self.clock.now(), type="error", reason="tick",
                                     meta={"error": repr(e)})
            await self.clock.sleep(tick_s)

    def stop(self) -> None:
        self._running = False

    async def tick(self) -> TickReport:
        now = self.clock.now()
        report = TickReport(ts=now)
        date = now.strftime("%Y-%m-%d")

        # 1) 日期翻页
        if self.state.time.date != date:
            self.state.time = TimeState(date=date, hhmm=now.strftime("%H:%M"), weekday=now.weekday())
            self.state.misc["coffee_today"] = 0

        # 2) 日计划(无则生成,LLM 可用则走提议)
        try:
            plan = await self.planner.ensure_plan(self.state, now)
        except Exception as e:
            report.errors.append(f"planner: {e!r}")
            plan = {"date": date, "blocks": []}

        # 3) 动力学
        self.state, dyn_events, forced = self.dynamics.step(self.state, now, self.activities)
        self._log(dyn_events, report)

        # 4) 想法衰减(整点)
        if now.minute == 0:
            gone = self.dynamics.decay_wants(self.state, now)
            if gone:
                self._log([{"ts": now.isoformat(), "type": "want", "kind": "decay",
                            "meta": {"gone": gone}}], report)

        # 5) 强制提议(能量闸门)
        for fp in forced:
            self._apply(fp, now, report)

        # 6) 锚点保真(R7) —— 按锚点"归属日"记录尝试, 软锚点睡时不扰
        for anchor in self.schedule.anchors_for(now):
            due = self.schedule.anchor_datetime(anchor, now)
            a_date = due.strftime("%Y-%m-%d")
            anchors_done = self.state.misc.setdefault("anchors_done", {}).setdefault(a_date, [])
            if anchor.id in anchors_done:
                continue
            if not self.schedule.anchor_pending(anchor, now):
                continue
            if self.schedule.anchor_satisfied(anchor, self.state.current.id, self.state.sleep_state):
                anchors_done.append(anchor.id)
                self._log([{"ts": now.isoformat(), "type": "anchor", "kind": "hit",
                            "to": anchor.activity, "reason": anchor.id, "meta": {"anchor": anchor.model_dump()}}], report)
                if anchor.id == "wake":
                    self._emit("engine.wake", {"date": date})
                continue
            if not anchor.hard and self.state.sleep_state == "asleep":
                anchors_done.append(anchor.id)   # 软锚点睡过即过, 不叫醒
                continue
            p = TransitionProposal(from_activity=self.state.current.id, to_activity=anchor.activity,
                                   to_location=None, task=anchor.want, intent=anchor.id,
                                   reason="anchor")
            res = self._apply(p, now, report)
            # 无论成败都记录"已尝试", 防止每拍重试刷屏(硬锚点 miss 已在事件里留痕)
            anchors_done.append(anchor.id)
            if res and self.schedule.anchor_satisfied(anchor, self.state.current.id, self.state.sleep_state):
                self._log([{"ts": now.isoformat(), "type": "anchor", "kind": "hit_late",
                            "to": anchor.activity, "reason": anchor.id, "meta": {}}], report)
                if anchor.id == "wake":
                    self._emit("engine.wake", {"date": date})

        # 锚点记录剪枝(只留最近 3 天)
        try:
            keep = {(datetime.now().strftime("%Y-%m-%d")) if False else date,
                    (datetime.fromisoformat(date + "T00:00") - timedelta(days=1)).strftime("%Y-%m-%d"),
                    (datetime.fromisoformat(date + "T00:00") - timedelta(days=2)).strftime("%Y-%m-%d")}
            self.state.misc["anchors_done"] = {k: v for k, v in self.state.misc.get("anchors_done", {}).items() if k in keep}
        except Exception:
            pass

        # 7) 计划块执行
        block = self.planner.block_at(plan, now)
        if block and self.state.current.id != block.activity and not self.state.current.id.startswith("paused_"):
            if (self.state.sleep_state == "awake" or block.activity == "sleep") and self.activities.window_ok(block.activity, now):
                p = TransitionProposal(from_activity=self.state.current.id, to_activity=block.activity,
                                       to_location=None, task=block.task, intent="daily_plan",
                                       reason="plan")
                self._apply(p, now, report)

        # 8) 超时续接(当前活动超过 max 时长 → 计划下一块或兜底)
        dur = self.activities.duration_of(self.state.current.id)
        elapsed = (now - self.state.current.started_at).total_seconds() / 60.0
        if elapsed > dur.max and self.state.sleep_state == "awake":
            nxt = self.planner.next_block(plan, now)
            target = (nxt.activity if nxt else self.schedule.default_activity_for(now))
            if not self.activities.window_ok(target, now):
                target = self.schedule.default_activity_for(now)
                if not self.activities.window_ok(target, now):
                    target = None
            if target and target != self.state.current.id:
                p = TransitionProposal(from_activity=self.state.current.id, to_activity=target,
                                       to_location=None, task=(nxt.task if nxt else None),
                                       intent="overrun", reason="plan")
                self._apply(p, now, report)

        # 9) 恢复被打断的活动(暂停 >15min)
        if self.state.current.id.startswith("paused_"):
            orig = self.state.current.id[7:]
            paused_elapsed = (now - self.state.current.started_at).total_seconds() / 60.0
            if paused_elapsed > 15 and self.activities.exists(orig):
                p = TransitionProposal(from_activity=self.state.current.id, to_activity=orig,
                                       to_location=None, reason="resume")
                self._apply(p, now, report)

        # 10) 记忆批量 retain(diff-filter:30min 且有新事件)
        last_id = self.eventlog.last_id()
        if self._should_retain(now, last_id):
            await self._retain_batch(now, last_id, report)

        # 11) 主动冒泡候选(P3, 配额由 arbiter/outbound 收敛)
        await self._maybe_broadcast(now, report)

        self.state.ts = now
        self.state.time = TimeState(date=date, hhmm=now.strftime("%H:%M"), weekday=now.weekday())
        self._save()
        self._emit("engine.state", self.snapshot_public())
        self.tick_count += 1
        return report

    # ---------- 中断矩阵(§2.5) ----------
    def attend(self, env: dict) -> dict:
        """P0/P1 消息到达时的世界侧动作。返回 {respond, delay_s, emergency, paused}"""
        now = self.clock.now()
        emergency_words = self.settings.channels.get("onebot", {}).get("emergency_words", [])
        text = env.get("text", "") or ""
        p_level = env.get("priority", "P0")
        if self.state.sleep_state == "asleep":
            emergency = p_level == "P0" and any(w in text for w in emergency_words)
            if emergency:
                p = TransitionProposal(from_activity=self.state.current.id, to_activity="wake_routine",
                                       to_location=None, task="被急事叫醒", reason="interrupt",
                                       meta={"emergency": True})
                self._apply(p, now, TickReport(ts=now))
                return {"respond": "now", "delay_s": 0, "emergency": True, "paused": False}
            return {"respond": "catchup", "delay_s": None, "emergency": False, "paused": False}
        spec = self.activities.get(self.state.current.id)
        level = spec.interruptible if spec else "medium"
        if self.state.current.id.startswith("paused_"):
            level = "high"
        paused = False
        if p_level == "P0" and level in ("high", "medium") and not self.state.current.id.startswith("paused_"):
            p = TransitionProposal(from_activity=self.state.current.id,
                                   to_activity=f"paused_{self.state.current.id}",
                                   to_location=None, task="去回消息", reason="interrupt")
            res = self._apply(p, now, TickReport(ts=now))
            paused = bool(res and res.ok)
        if p_level != "P0":
            return {"respond": "defer", "delay_s": 120, "emergency": False, "paused": paused}
        if level == "high":
            return {"respond": "now", "delay_s": self.rng.uniform(0.5, 2.0), "emergency": False, "paused": paused}
        if level == "medium":
            return {"respond": "delayed", "delay_s": self.rng.uniform(120, 600), "emergency": False, "paused": paused}
        if level == "low":
            return {"respond": "delayed", "delay_s": self.rng.uniform(600, 1800), "emergency": False, "paused": paused}
        return {"respond": "delayed", "delay_s": self.rng.uniform(900, 1800), "emergency": False, "paused": paused}

    def observe(self, env: dict) -> None:
        """P2 观测:密度+1, 留原料(不惊动世界)。"""
        self.state.social.density += 1
        self._p2_ring.append({"ts": env.get("ts"), "sender": env.get("sender_name"),
                              "text": (env.get("text") or "")[:200]})
        if len(self._p2_ring) > 50:
            del self._p2_ring[: len(self._p2_ring) - 50]

    def note_reply_done(self) -> None:
        """回复完成后:社交电池扣减 + 尝试恢复原活动。"""
        self.state.social.battery = max(0, self.state.social.battery - self.dynamics.reply_cost)
        self.state.social.total_exchanges += 1
        if self.state.current.id.startswith("paused_"):
            orig = self.state.current.id[7:]
            if self.activities.exists(orig):
                now = self.clock.now()
                p = TransitionProposal(from_activity=self.state.current.id, to_activity=orig,
                                       to_location=None, reason="resume")
                self._apply(p, now, TickReport(ts=now))

    # ---------- 内部 ----------
    def _apply(self, p: TransitionProposal, now: datetime, report: TickReport):
        # 拒绝冷却: 同一(from,to,reason) 30 分钟内不重复提议
        key = (p.from_activity, p.to_activity, p.reason)
        last = self._rejected_cooldown.get(key)
        if last is not None and (now - last).total_seconds() < 1800:
            return None
        res = self.guard.apply(self.state, p, now)
        if res.ok and res.state is not None:
            self.state = res.state
            self._log(res.events, report)
            report.transitions.append(f"{p.from_activity}→{p.to_activity}")
            for n in res.notes:
                report.events.append({"ts": now.isoformat(), "type": "note", "meta": {"note": n}})
            self._emit("engine.transition", {"from": p.from_activity, "to": p.to_activity,
                                             "reason": p.reason, "fallback": res.fallback})
        else:
            self._rejected_cooldown[key] = now
            report.errors.append(f"rejected: {p.from_activity}→{p.to_activity}({p.reason}): {res.rejected_reason}")
            self.eventlog.append(ts=now, type="rejected", from_act=p.from_activity,
                                 to_act=p.to_activity, reason=p.reason,
                                 meta={"detail": res.rejected_reason, "proposal": p.model_dump()})
        return res

    def _log(self, events: list[dict], report: TickReport) -> None:
        for e in events:
            self.eventlog.append(ts=e.get("ts", self.clock.now().isoformat() if isinstance(self.clock.now(), datetime) else str(e.get("ts"))),
                                 type=e.get("type", "note"), from_act=e.get("from"),
                                 to_act=e.get("to"), location=e.get("location"),
                                 reason=e.get("reason"), meta=e.get("meta", {}),
                                 version=e.get("version"))
            report.events.append(e)

    def _should_retain(self, now: datetime, last_id: int) -> bool:
        batch_min = int(self.settings.memory.get("retain", {}).get("heartbeat_batch_minutes", 30))
        if self._last_retain_ts is None:
            self._last_retain_ts = now
            return False
        minutes = (now - self._last_retain_ts).total_seconds() / 60.0
        return minutes >= batch_min and last_id > self._last_retain_id

    async def _retain_batch(self, now: datetime, last_id: int, report: TickReport) -> None:
        events = self.eventlog.since(self._last_retain_id, limit=100)
        transitions = [e for e in events if e["type"] == "transition"]
        text = self.renderer.render(self.state, transitions)
        if transitions or self._p2_ring:
            payload = {"text": text, "observed": self._p2_ring[-10:]}
            if self.on_retain:
                try:
                    await self.on_retain(json.dumps(payload, ensure_ascii=False), "heartbeat", {})
                except Exception as e:
                    report.errors.append(f"retain: {e!r}")
        self._last_retain_id = last_id
        self._last_retain_ts = now
        self._p2_ring.clear()

    async def _maybe_broadcast(self, now: datetime, report: TickReport) -> None:
        if self.state.sleep_state != "awake":
            return
        if not (9 <= now.hour < 23):
            return
        p = 0.05
        spec = self.activities.get(self.state.current.id)
        if spec:
            p += spec.share_worth * 0.004
        if self.state.wants:
            p += 0.02 * sum(w.strength for w in self.state.wants) / 10
        if self.rng.random() < min(p, 0.15) and self.on_broadcast:
            ctx = {"text": self.renderer.render(self.state), "activity": self.state.current.id,
                   "wants": [w.content for w in self.state.wants], "ts": now.isoformat()}
            try:
                await self.on_broadcast(self.renderer.render(self.state), ctx)
            except Exception as e:
                report.errors.append(f"broadcast: {e!r}")

    def _emit(self, topic: str, data) -> None:
        if self.bus:
            self.bus.emit(topic, data)

    # ---------- 对外 ----------
    def snapshot_public(self) -> dict:
        return {
            "state": self.state.model_dump(mode="json"),
            "rendered": self.renderer.render(self.state, self.eventlog.tail(20)),
            "guard_stats": self.guard.stats,
            "tick_count": self.tick_count,
        }

    def snapshot(self) -> dict:
        snap = self.snapshot_public()
        anchors = []
        now = self.clock.now()
        all_done = self.state.misc.get("anchors_done", {})
        for a in self.schedule.anchors_for(now):
            a_date = self.schedule.anchor_datetime(a, now).strftime("%Y-%m-%d")
            done = a.id in all_done.get(a_date, [])
            anchors.append({"id": a.id, "at": a.at, "activity": a.activity, "hard": a.hard,
                            "done": done,
                            "satisfied": self.schedule.anchor_satisfied(a, self.state.current.id, self.state.sleep_state)})
        snap["anchors_today"] = anchors
        snap["recent_events"] = self.eventlog.tail(30)
        return snap
