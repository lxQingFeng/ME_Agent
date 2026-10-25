"""仲裁器自测(2026-10-25 第 6 轮, 缺口 5-②)。

运行: python3 tests/test_arbiter.py   (ME_Agent 仓库根, 无需起服务)
覆盖:
  A. 分级 classify: 私聊/被@→P0; 点名或热议(density≥6)→P1; 其余→P2。
  B. P2 流向: observe 计数 + 原料入会话库, 不触发管道。
  C. P1 迟滞: 30min 内 ≥2 次点名才插话; 睡眠/低电量不插话。
  D. P0 突发合并: 400ms 滚动窗同 chat 合并(" / " 拼接, merged_count);
     满仓(8 条)换仓时先放行积压消息 —— 9 连发不丢消息(修复回归)。
  E. 睡眠 catchup: 睡着且非急救 → 入补看队列, 不即时回复。
  F. 延迟决策(中断矩阵): 急救词立即回+叫醒; 可中断性 high/medium/low
     分别对应 now(0.5-2s)/delayed(120-600s)/delayed(600-1800s); 非 P0 defer。
退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.clock import VirtualClock
from app.bus import EventBus
from app.core.arbiter import Arbiter
from app.core.store import Store
from app.heartbeat.engine import HeartbeatEngine
from app.heartbeat.eventlog import EventLog
from app.heartbeat.models import ActivityState

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  | {detail}" if detail and not ok else ""))


class PipelineStub:
    def __init__(self):
        self.handled: list[tuple[dict, dict]] = []
        self.catchups: list[str] = []

    async def handle(self, env: dict, attend: dict) -> None:
        self.handled.append((dict(env), dict(attend)))

    async def handle_catchup(self, chat: str) -> None:
        self.catchups.append(chat)


class OutboundStub:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.broadcasts: list[tuple[str, str]] = []

    def broadcast_allowed(self):
        return (self.allowed, "" if self.allowed else "quota")

    async def broadcast(self, chat, text):
        self.broadcasts.append((chat, text))


def make_env(chat="g1", text="在吗", chat_type="group", at_me=False, sender="u1"):
    return {"chat_id": chat, "chat_type": chat_type, "sender_id": sender,
            "sender_name": sender, "text": text, "at_me": at_me,
            "segments": [], "ts": 1774000000.0}


def build(tmp: Path):
    settings = Settings(root=ROOT, state_dir=tmp)
    clock = VirtualClock(datetime(2026, 10, 24, 10, 0, tzinfo=ZoneInfo(settings.tz)))
    log = EventLog(tmp / "world.db")
    bus = EventBus()
    engine = HeartbeatEngine(settings, clock, log, bus=bus)
    store = Store(tmp / "console.db")
    pipe = PipelineStub()
    outbound = OutboundStub()
    arb = Arbiter(settings, engine, pipe, store, bus, outbound)
    return arb, engine, store, pipe, outbound


async def settle(ms: float = 0.05) -> None:
    await asyncio.sleep(ms)


async def run() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="arb_"))

    # ---------- A. 分级 ----------
    print("== A. 事件分级 classify ==")
    arb, engine, store, pipe, outbound = build(tmp / "a")
    engine.state.social.density = 0
    check("A1 私聊 → P0", arb.classify(make_env(chat_type="private")) == "P0")
    check("A2 群内被@ → P0", arb.classify(make_env(at_me=True)) == "P0")
    check("A3 群内点名(小默) → P1", arb.classify(make_env(text="小默你怎么看")) == "P1")
    engine.state.social.density = 6
    check("A4 热议(density≥6) → P1", arb.classify(make_env(text="随便聊")) == "P1")
    engine.state.social.density = 2
    check("A5 普通群聊 → P2", arb.classify(make_env(text="随便聊")) == "P2")

    # ---------- B. P2 流向 ----------
    print("== B. P2 观察不惊动世界 ==")
    arb, engine, store, pipe, outbound = build(tmp / "b")
    engine.state.social.density = 0
    await arb.on_message(make_env(chat="b1", text="今天天气不错"))
    await settle()
    rows = store.query("SELECT content FROM messages WHERE chat_id='b1'")
    check("B1 observe 计数(密度+1)", engine.state.social.density == 1,
          f"density={engine.state.social.density}")
    check("B2 P2 原料入会话库", len(rows) == 1 and rows[0]["content"] == "今天天气不错")
    check("B3 不触发对话管道", not pipe.handled and not pipe.catchups)

    # ---------- C. P1 迟滞 ----------
    print("== C. P1 迟滞插话 ==")
    arb, engine, store, pipe, outbound = build(tmp / "c")
    engine.state.social.density = 0
    engine.state.sleep_state = "awake"
    engine.state.social.battery = 80
    await arb.on_message(make_env(chat="c1", text="小默在干嘛"))
    await settle()
    check("C1 首次点名不插话", not pipe.handled)
    await arb.on_message(make_env(chat="c1", text="小默快出来"))
    await settle()
    check("C2 30min 内二次点名 → 插话(at_me 注入)", len(pipe.handled) == 1
          and pipe.handled[0][0].get("at_me") is True)
    # 睡着不插话
    pipe.handled.clear()
    engine.state.sleep_state = "asleep"
    await arb.on_message(make_env(chat="c2", text="小默在吗"))
    await arb.on_message(make_env(chat="c2", text="小默睡了吗"))
    await settle()
    check("C3 睡眠中不插话", not pipe.handled)
    # 低电量不插话
    engine.state.sleep_state = "awake"
    engine.state.social.battery = 10
    await arb.on_message(make_env(chat="c3", text="小默呢"))
    await arb.on_message(make_env(chat="c3", text="小默出来"))
    await settle()
    check("C4 社交电池<25 不插话", not pipe.handled)
    engine.state.social.battery = 80

    # ---------- D. P0 突发合并 ----------
    print("== D. P0 突发合并(400ms 滚动窗) ==")
    arb, engine, store, pipe, outbound = build(tmp / "d")
    engine.state.sleep_state = "awake"
    engine.state.social.battery = 80
    engine.state.current = ActivityState(id="light_scroll", started_at=engine.clock.now())
    for i in range(3):
        await arb.on_message(make_env(chat="d1", chat_type="private", text=f"消息{i+1}"))
    await settle()
    check("D1 3 连发 → 合并为 1 次管道调用", len(pipe.handled) == 1,
          f"handles={len(pipe.handled)}")
    if pipe.handled:
        env, attend = pipe.handled[0]
        check("D2 合并文本按 ' / ' 拼接", env.get("text") == "消息1 / 消息2 / 消息3",
              f"text={env.get('text')!r}")
        check("D3 merged_count=3", env.get("merged_count") == 3, f"mc={env.get('merged_count')}")
        check("D4 attend 注入中断决策", attend.get("respond") in ("now", "delayed"), f"attend={attend}")
    else:
        check("D2 合并文本按 ' / ' 拼接", False); check("D3 merged_count=3", False); check("D4 attend 注入", False)

    # 满仓放行(修复回归): 9 连发不丢
    pipe.handled.clear()
    for i in range(9):
        await arb.on_message(make_env(chat="d2", chat_type="private", text=f"第{i+1}条"))
    await settle()
    sent = "".join(env.get("text", "") for env, _ in pipe.handled)
    check("D5 9 连发全部送达(满仓先放行, 无丢失)",
          all(f"第{i+1}条" in sent for i in range(9)), f"handles={len(pipe.handled)} got={sent!r}")
    check("D6 9 连发拆为 8+1 两次合并", len(pipe.handled) == 2,
          f"handles={len(pipe.handled)}")
    if len(pipe.handled) == 2:
        check("D7 满仓批次 merged_count=8", pipe.handled[0][0].get("merged_count") == 8)

    # ---------- E. 睡眠 catchup ----------
    print("== E. 睡眠补看队列 ==")
    arb, engine, store, pipe, outbound = build(tmp / "e")
    engine.state.sleep_state = "asleep"
    engine.state.current = ActivityState(id="sleep", started_at=engine.clock.now())
    await arb.on_message(make_env(chat="e1", chat_type="private", text="明早叫我"))
    await settle()
    q = store.kv_get("catchup:e1", [])
    check("E1 入 catchup 队列", len(q) == 1 and q[0]["text"] == "明早叫我", f"q={q}")
    check("E2 睡眠中不即时回复", not pipe.handled)

    # ---------- F. 延迟决策(中断矩阵) ----------
    print("== F. attend 延迟决策 ==")
    # F1 急救词: 睡着也立即叫醒
    arb, engine, store, pipe, outbound = build(tmp / "f")
    engine.state.sleep_state = "asleep"
    engine.state.current = ActivityState(id="sleep", started_at=engine.clock.now())
    a = engine.attend({"priority": "P0", "text": "出事了快来帮忙"})
    check("F1 急救词 → now+emergency+世界叫醒", a["respond"] == "now" and a["emergency"] is True
          and engine.state.current.id == "wake_routine", f"a={a} act={engine.state.current.id}")
    # F2 睡着非急救 → catchup
    engine.state.sleep_state = "asleep"
    engine.state.current = ActivityState(id="sleep", started_at=engine.clock.now())
    a = engine.attend({"priority": "P0", "text": "晚安"})
    check("F2 睡着非急救 → catchup", a["respond"] == "catchup", f"a={a}")
    # F3 high → now(0.5-2s)
    engine.state.sleep_state = "awake"
    engine.state.current = ActivityState(id="light_scroll", started_at=engine.clock.now())
    a = engine.attend({"priority": "P0", "text": "在吗"})
    check("F3 high 可中断 → now(0.5-2s)+暂停活动", a["respond"] == "now" and 0.5 <= a["delay_s"] <= 2.0
          and a["paused"] is True, f"a={a}")
    check("F3b 活动被暂停(paused_*)", engine.state.current.id.startswith("paused_"),
          f"act={engine.state.current.id}")
    # F4 medium → delayed(120-600s)
    engine.state.current = ActivityState(id="reading", started_at=engine.clock.now())
    a = engine.attend({"priority": "P0", "text": "在吗"})
    check("F4 medium 可中断 → delayed(120-600s)", a["respond"] == "delayed"
          and 120 <= a["delay_s"] <= 600, f"a={a}")
    # F5 low → delayed(600-1800s) 不暂停
    engine.state.current = ActivityState(id="coding", started_at=engine.clock.now())
    a = engine.attend({"priority": "P0", "text": "在吗"})
    check("F5 low 可中断 → delayed(600-1800s)不打断", a["respond"] == "delayed"
          and 600 <= a["delay_s"] <= 1800 and a["paused"] is False, f"a={a}")
    # F6 非 P0 → defer
    engine.state.current = ActivityState(id="light_scroll", started_at=engine.clock.now())
    a = engine.attend({"priority": "P1", "text": "小默在吗"})
    check("F6 非 P0 → defer(120s)", a["respond"] == "defer" and a["delay_s"] == 120, f"a={a}")


def main() -> int:
    print("== 仲裁器分级/突发合并/延迟决策 ==")
    asyncio.run(run())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n仲裁器自测: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS" +
          ("" if not failed else f", {len(failed)} FAIL: {[r[0] for r in failed]}"))
    import shutil
    for d in Path(tempfile.gettempdir()).glob("arb_*"):
        shutil.rmtree(d, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
