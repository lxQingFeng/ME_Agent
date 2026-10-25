"""日计划器:LLM 提议(JSON mode) → CoherenceGuard 风格校验 → 无效块回退模板(方案 §2.4.6)。"""
from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path
import yaml
from app.heartbeat.activities import ActivityLibrary, hhmm_to_min

class Block:
    def __init__(self, activity: str, start: str, end: str, task: str | None = None):
        self.activity, self.start, self.end, self.task = activity, start, end, task
    def to_dict(self):
        return {"activity": self.activity, "start": self.start, "end": self.end, "task": self.task}
    def start_min(self): return hhmm_to_min(self.start)
    def end_min(self): return hhmm_to_min(self.end)
    def minutes(self): return self.end_min() - self.start_min()

class DayPlanner:
    def __init__(self, schedule, activities: ActivityLibrary, dynamics_cfg: dict,
                 plans_dir: Path, llm=None):
        self.schedule = schedule
        self.activities = activities
        self.dynamics_cfg = dynamics_cfg
        self.plans_dir = plans_dir
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        self.llm = llm

    def plan_path(self, date: str) -> Path:
        return self.plans_dir / f"{date}.yaml"

    async def ensure_plan(self, state, now: datetime) -> dict:
        date = now.strftime("%Y-%m-%d")
        p = self.plan_path(date)
        if p.exists():
            return yaml.safe_load(p.read_text("utf-8")) or {"date": date, "blocks": []}
        blocks, replaced = await self.generate(date, state, now)
        data = {"date": date, "blocks": [b.to_dict() for b in blocks], "replaced_blocks": replaced}
        p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8")
        return data

    async def generate(self, date: str, state, now: datetime) -> tuple[list[Block], int]:
        template = self.template_blocks(now, state)
        if self.llm is None:
            return template, 0
        try:
            proposal = await self._llm_propose(date, state, template)
            if not proposal:
                return template, 0
            merged, replaced = self._merge(proposal, template, now, state)
            return merged, replaced
        except Exception:
            return template, 0

    async def _llm_propose(self, date: str, state, template: list[Block]) -> list[dict]:
        anchors = self.schedule.anchors_for(datetime.strptime(date, "%Y-%m-%d"))
        prompt = (
            "基于以下输入生成今日活动块序列(JSON 数组):\n"
            f"周模板: {[b.to_dict() for b in template]}\n"
            f"当前状态: energy={state.body.energy:.0f} mood={state.mood.valence:.2f}\n"
            f"未完成: {state.current.id}\n想要: {[w.content for w in state.wants]}\n"
            f"硬锚点(不可删除只可微调±{self.schedule.drift}分钟): "
            f"{[a.model_dump() for a in anchors if a.hard]}\n"
            "约束: 块活动必须用活动库 id; 块时长在活动 min/max 内; 能量预算从当前值起按强度扣减不得为负。\n"
            '输出仅 JSON: [{"activity":"...","start":"HH:MM","end":"HH:MM","task":"..."}]'
        )
        raw = await self.llm.chat_json(prompt)
        import json
        data = json.loads(raw) if isinstance(raw, str) else raw
        return data if isinstance(data, list) else []

    def template_blocks(self, now: datetime, state) -> list[Block]:
        anchors, defaults = self.schedule._profile(now)
        blocks: list[Block] = []
        for d in defaults:
            blocks.append(Block(d.activity, d.frm, d.to))
        for a in anchors:
            if not a.hard:
                continue
            dur = self.activities.duration_of(a.activity)
            start = hhmm_to_min(a.at)
            # 覆盖包含锚点时刻的模板块
            for i, b in enumerate(blocks):
                if b.start_min() <= start < b.end_min():
                    blocks[i] = Block(a.activity, a.at, a.at.replace(":", ":")[:5],
                                      a.want) if False else blocks[i]
                    # 锚点块: 锚点时刻起 typ 时长,与相邻块拼接
                    end = start + dur.typ
                    blocks[i] = Block(a.activity, a.at, f"{end//60:02d}:{end%60:02d}", a.want)
                    break
        # 清理重叠(后块让位)
        out: list[Block] = []
        for b in sorted(blocks, key=lambda x: x.start_min()):
            if out and b.start_min() < out[-1].end_min():
                b.start = out[-1].end
                if b.start_min() >= b.end_min():
                    continue
            out.append(b)
        return out

    def _merge(self, proposal: list[dict], template: list[Block], now: datetime, state):
        hard = {a.id: a for a in self.schedule.anchors_for(now) if a.hard}
        valid: list[Block] = []
        replaced = 0
        for item in proposal:
            try:
                b = Block(item["activity"], item["start"], item["end"], item.get("task"))
            except Exception:
                replaced += 1
                continue
            if self._validate_block(b, state, hard):
                valid.append(b)
            else:
                replaced += 1
        # 硬锚点保真:模板锚点块必须保留
        anchor_blocks = [b for b in template if any(
            hhmm_to_min(a.at) <= b.start_min() < b.end_min() or b.activity == a.activity
            for a in hard.values())]
        for ab in anchor_blocks:
            if not any(abs(v.start_min() - ab.start_min()) <= self.schedule.drift and
                       v.activity == ab.activity for v in valid):
                valid.append(ab)
        valid.sort(key=lambda x: x.start_min())
        return valid, replaced

    def _validate_block(self, b: Block, state, hard: dict) -> bool:
        spec = self.activities.get(b.activity)
        if not spec:
            return False
        if not self.activities.window_ok(b.activity, datetime.strptime(
                f"{state.time.date} {b.start}", "%Y-%m-%d %H:%M")):
            return False
        if not (spec.duration.min <= b.minutes() <= spec.duration.max * 2):
            return False
        # 能量预算粗校验
        ticks = b.minutes() / 5
        drain = {"none": 0.05, "light": 0.3, "medium": 0.8, "high": 1.6, "extreme": 2.5}[spec.intensity]
        if ticks * drain > 100:
            return False
        return True

    def block_at(self, plan: dict, now: datetime) -> Block | None:
        t = now.hour * 60 + now.minute
        for raw in plan.get("blocks", []):
            b = Block(raw["activity"], raw["start"], raw["end"], raw.get("task"))
            if b.start_min() <= t < b.end_min():
                return b
        return None

    def next_block(self, plan: dict, now: datetime) -> Block | None:
        t = now.hour * 60 + now.minute
        best = None
        for raw in plan.get("blocks", []):
            b = Block(raw["activity"], raw["start"], raw["end"], raw.get("task"))
            if b.start_min() > t and (best is None or b.start_min() < best.start_min()):
                best = b
        return best
