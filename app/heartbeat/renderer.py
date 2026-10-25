"""状态渲染器:把 WorldState 渲染成给 LLM/控制台看的自然语言(方案附录 C-1)。"""
from __future__ import annotations
from datetime import datetime
from app.heartbeat.models import WorldState
from app.heartbeat.activities import ActivityLibrary

WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]

class Renderer:
    def __init__(self, activities: ActivityLibrary):
        self.activities = activities

    def render(self, state: WorldState, recent_events: list[dict] | None = None) -> str:
        disp = self.activities.display(state.current.id)
        paused = state.current.id.startswith("paused_")
        mood = self._mood_label(state)
        energy = int(state.body.energy)
        lines = []
        base = (f"现在是{state.time.date}(周{WEEKDAY_ZH[state.time.weekday]}) {state.time.hhmm}。"
                f"我在{'' if not paused else '(被打断)'}{disp}"
                + (f"——{state.current.task}" if state.current.task else "")
                + f",位于{state.location}。"
                f"心情{mood},精力{energy}%"
                + ("(有点撑不住了)" if energy < 25 else "")
                + f",社交欲{int(state.social.battery)}%")
        lines.append(base)
        if state.sleep_state == "asleep":
            lines.append("我正在睡觉(非紧急消息会攒着,醒来会看)。")
        if recent_events:
            trans = [e for e in recent_events if e.get("type") == "transition"][-4:]
            if trans:
                seq = " → ".join(
                    f"{self.activities.display(e.get('to') or e.get('to_act'))}" for e in trans)
                lines.append(f"今天轨迹:{seq}。")
        if state.wants:
            lines.append("心里想的:" + ";".join(
                f"{w.content}({w.strength})" for w in state.wants) + "。")
        return "\n".join(lines)

    def _mood_label(self, state: WorldState) -> str:
        v, a = state.mood.valence, state.mood.arousal
        if v > 0.3 and a > 0.3: return "开心"
        if v > 0.3: return "满足"
        if v < -0.3 and a > 0.4: return "烦躁"
        if v < -0.3: return "低落"
        if a > 0.5: return "兴奋"
        if state.body.energy < 25: return "疲惫"
        return "平静"
