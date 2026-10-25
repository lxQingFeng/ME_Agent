"""周例行:硬/软锚点 + 兜底时段模板。"""
from __future__ import annotations
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pydantic import BaseModel

class Anchor(BaseModel):
    id: str
    day: str = "*"
    at: str
    activity: str
    hard: bool = True
    want: str | None = None

class SlotDefault(BaseModel):
    frm: str
    to: str
    activity: str

class Schedule:
    def __init__(self, routine_cfg: dict, tz: str = "Asia/Shanghai"):
        self.tz = ZoneInfo(tz)
        self.drift = int(routine_cfg.get("drift_minutes", 20))
        wt = routine_cfg.get("week_template", {})
        self.weekday = self._parse(wt.get("weekday", {}))
        self.weekend = self._parse(wt.get("weekend", {}))

    def _parse(self, d: dict) -> tuple[list[Anchor], list[SlotDefault]]:
        anchors = [Anchor(**a) for a in d.get("anchors", [])]
        defaults = [SlotDefault(frm=s["from"], to=s["to"], activity=s["activity"])
                    for s in d.get("defaults", [])]
        return anchors, defaults

    def _profile(self, now: datetime):
        return self.weekend if now.weekday() >= 5 else self.weekday

    def anchors_for(self, now: datetime) -> list[Anchor]:
        anchors, _ = self._profile(now)
        out = []
        for a in anchors:
            if a.day == "*" or a.day == now.strftime("%a"):
                out.append(a)
        return out

    def anchor_datetime(self, anchor: Anchor, now: datetime) -> datetime:
        """锚点时刻(挂到今天的日期;凌晨锚点属于"前一天夜里",如 sleep 00:30)。"""
        h, m = map(int, anchor.at.split(":"))
        d = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if h < 6 and now.hour >= 6:      # 凌晨锚点已过(今天凌晨) → 归到明天
            d = d + timedelta(days=1)
        elif h >= 21 and now.hour < 6:   # 深夜锚点还没到 → 归到昨天晚上
            d = d - timedelta(days=1)
        return d

    def anchor_pending(self, anchor: Anchor, now: datetime) -> bool:
        """该锚点在 now 时刻是否已到期且未满足(含 drift 宽限)。"""
        due = self.anchor_datetime(anchor, now)
        return due <= now

    def default_activity_for(self, now: datetime) -> str:
        _, defaults = self._profile(now)
        t = now.hour * 60 + now.minute
        for s in defaults:
            a, b = s.frm, s.to
            lo, hi = int(a[:2]) * 60 + int(a[3:5]), int(b[:2]) * 60 + int(b[3:5])
            if lo <= hi:
                if lo <= t <= hi:
                    return s.activity
            else:  # 跨午夜
                if t >= lo or t <= hi:
                    return s.activity
        return "rest"

    def anchor_satisfied(self, anchor: Anchor, current_activity: str, sleep_state: str) -> bool:
        if anchor.activity == "wake_routine":
            return sleep_state == "awake" and not current_activity.startswith("paused_")
        if anchor.activity == "sleep":
            return sleep_state == "asleep"
        return current_activity == anchor.activity
