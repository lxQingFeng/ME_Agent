"""世界状态模型(方案 §2.3)——所有结构变更必须过 CoherenceGuard。"""
from __future__ import annotations
from datetime import datetime
from typing import Literal, Any
from pydantic import BaseModel, Field

class Duration(BaseModel):
    min: int = 5
    typ: int = 20
    max: int = 60

class ActivityState(BaseModel):
    id: str
    started_at: datetime
    task: str | None = None
    interrupted: bool = False

class TimeState(BaseModel):
    date: str                 # YYYY-MM-DD
    hhmm: str                 # HH:MM
    weekday: int = Field(ge=0, le=6)

class BodyState(BaseModel):
    energy: float = 100.0     # 0-100
    stress: float = 5.0       # 0-100
    hunger: float = 0.0       # 0-100

class MoodState(BaseModel):
    valence: float = 0.2      # -1..1
    arousal: float = 0.1      # 0..1

class SocialState(BaseModel):
    battery: float = 60.0     # 0-100
    density: int = 0          # 最近一拍内的消息数
    total_exchanges: int = 0

class Want(BaseModel):
    content: str
    strength: int = 5         # 1-10
    created_at: datetime
    ttl_hours: float = 24.0

class WorldState(BaseModel):
    version: int = 0
    ts: datetime
    current: ActivityState
    location: str = "家/床"
    sleep_state: Literal["awake", "asleep"] = "asleep"
    time: TimeState
    body: BodyState = BodyState()
    mood: MoodState = MoodState()
    social: SocialState = SocialState()
    wants: list[Want] = []
    misc: dict[str, Any] = Field(default_factory=lambda: {"coffee_today": 0, "loc_since": None, "paused_of": None})

class TransitionProposal(BaseModel):
    from_activity: str
    to_activity: str
    to_location: str | None = None
    task: str | None = None
    intent: str | None = None
    reason: Literal["anchor", "plan", "interrupt", "resume", "energy_gate",
                    "llm_proposal_accepted", "admin", "fast_forward", "want"] = "plan"
    meta: dict[str, Any] = {}

class GuardResult(BaseModel):
    ok: bool = False
    rejected_reason: str = ""
    fallback: Literal["none", "fix", "replace", "pathfind"] | None = None
    events: list[dict] = Field(default_factory=list)
    state: WorldState | None = None
    notes: list[str] = Field(default_factory=list)

class TickReport(BaseModel):
    ts: datetime
    transitions: list[str] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
