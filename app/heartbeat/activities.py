"""活动库:转移白名单图 + 时间窗 + 能量门槛 + BFS 寻路。"""
from __future__ import annotations
from datetime import datetime
from typing import Optional
from collections import deque
from pydantic import BaseModel, Field, field_validator

class ActivitySpec(BaseModel):
    id: str
    display: str
    intensity: str = "light"     # none/light/medium/high/extreme
    duration: "Duration" = Field(default_factory=lambda: Duration())
    time_window: list[str] = ["*"]
    locations: list[str] = ["家/客厅"]
    interruptible: str = "medium"  # none/low/medium/high
    requires: dict = Field(default_factory=dict)
    next: list[str] = Field(default_factory=list)
    share_worth: int = 3
    locks: list[str] = Field(default_factory=list)

from app.heartbeat.models import Duration  # noqa: E402

def hhmm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)

def in_window(window: str, now: datetime) -> bool:
    if window == "*":
        return True
    t = now.hour * 60 + now.minute
    a, _, b = window.partition("-")
    lo, hi = hhmm_to_min(a), hhmm_to_min(b)
    if lo <= hi:
        return lo <= t <= hi
    return t >= lo or t <= hi   # 跨午夜(如 22:30-02:00)

class ActivityLibrary:
    def __init__(self, cfg: list[dict]):
        self.specs: dict[str, ActivitySpec] = {}
        for raw in cfg:
            spec = ActivitySpec(**raw)
            self.specs[spec.id] = spec

    def get(self, activity_id: str) -> Optional[ActivitySpec]:
        if activity_id.startswith("paused_"):
            return self.specs.get(activity_id[7:])
        return self.specs.get(activity_id)

    def exists(self, activity_id: str) -> bool:
        return self.get(activity_id) is not None

    def check_edge(self, a: str, b: str) -> bool:
        """R2: 白名单边(含 paused 约定与 resume)。"""
        if a.startswith("paused_") and b == a[7:]:
            return True                       # 恢复
        if b.startswith("paused_") and a == b[7:]:
            return True                       # 被打断
        sa = self.specs.get(a)
        return bool(sa and b in sa.next)

    def window_ok(self, activity_id: str, now: datetime) -> bool:
        spec = self.get(activity_id)
        if not spec:
            return False
        return any(in_window(w, now) for w in spec.time_window)

    def neighbors(self, activity_id: str) -> list[str]:
        spec = self.specs.get(activity_id)
        return list(spec.next) if spec else []

    def bfs_path(self, start: str, goal: str, now: datetime) -> list[str]:
        """R7 兜底③: 白名单图 BFS 最短路径(中间节点须过时间窗)。"""
        if start == goal:
            return [start]
        if start.startswith("paused_"):
            # 先恢复再寻路
            resume = start[7:]
            rest = self.bfs_path(resume, goal, now)
            return [start, *rest] if rest else []
        q: deque[tuple[str, list[str]]] = deque([(start, [start])])
        seen = {start}
        while q:
            node, path = q.popleft()
            for nxt in self.neighbors(node):
                if nxt == goal:
                    return [*path, nxt]
                if nxt in seen or nxt.startswith("paused_"):
                    continue
                if not self.window_ok(nxt, now):
                    continue
                seen.add(nxt)
                q.append((nxt, [*path, nxt]))
        return []

    def energy_ok(self, activity_id: str, energy: float, gates: dict) -> tuple[bool, str]:
        spec = self.get(activity_id)
        if not spec:
            return False, "unknown_activity"
        floor = max(float(spec.requires.get("energy_min", 0)),
                    float(gates.get("high_activity_min_energy", 15)) if spec.intensity in ("medium", "high", "extreme") else 0)
        if energy < floor:
            return False, f"energy {energy:.0f} < floor {floor:.0f}"
        return True, ""

    def duration_of(self, activity_id: str) -> Duration:
        spec = self.get(activity_id)
        return spec.duration if spec else Duration()

    def display(self, activity_id: str) -> str:
        spec = self.get(activity_id)
        return spec.display if spec else activity_id
