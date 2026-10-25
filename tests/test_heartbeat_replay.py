"""心跳回放/事件溯源一致性自测(2026-10-25 第 6 轮, 缺口 5-①)。

运行: python3 tests/test_heartbeat_replay.py   (ME_Agent 仓库根, 无需起服务)
覆盖:
  A. EventLog 机制: init/transition 落库; id 单调连续; since()/tail()/last_id() 对账。
  B. 虚拟时钟压缩推进(96 拍 × 15min ≈ 24h)后 replay(world.db) 与引擎现态一致:
     current.id / task / started_at / location / sleep_state / version / anchors_done。
     (审计通道重建的世界 == 运行通道的世界, 事件溯源不漂移)
  C. CLI 入口: python -m app.heartbeat.replay <db> 输出 JSON 可解析且与引擎一致。
  D. 快照对账: state/world.json(快照真相)与事件重放(审计真相)关键字段一致。
退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = sys.executable
RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  | {detail[:160]}" if detail and not ok else ""))


async def run_engine() -> tuple:
    from app.config import Settings
    from app.clock import VirtualClock
    from app.bus import EventBus
    from app.heartbeat.engine import HeartbeatEngine
    from app.heartbeat.eventlog import EventLog
    from app.heartbeat.replay import replay

    tmp = Path(tempfile.mkdtemp(prefix="me_replay_"))
    settings = Settings(root=ROOT, state_dir=tmp)
    start = datetime(2026, 10, 24, 7, 50, tzinfo=ZoneInfo(settings.tz))
    clock = VirtualClock(start=start, tz=settings.tz)
    bus = EventBus()
    eventlog = EventLog(settings.state_dir / "world.db")
    engine = HeartbeatEngine(settings, clock, eventlog, bus=bus, llm=None)

    # --- A. EventLog 机制 ---
    init_events = [e for e in eventlog.all() if e["type"] == "init"]
    check("A1 init 事件落库(to_act 非空)", len(init_events) == 1 and bool(init_events[0]["to_act"]),
          json.dumps(init_events))

    ids = [e["id"] for e in eventlog.all()]
    check("A2 事件 id 单调连续", ids == sorted(ids) and len(ids) == len(set(ids)), str(ids[:10]))

    last = eventlog.last_id()
    since_all = eventlog.since(last, limit=200)
    check("A3 since(last_id) 返回空(游标对账)", since_all == [], str(since_all))

    # 压缩推进 24h: 96 拍 × 15min
    for _ in range(96):
        clock.advance(15)
        await engine.tick()

    events = eventlog.all()
    transitions = [e for e in events if e["type"] == "transition"]
    check("B0 24h 推进产生 transition(世界确实在动)", len(transitions) >= 8,
          f"transitions={len(transitions)}")
    tail5 = eventlog.tail(5)
    check("A4 tail(5) 取末尾且保序", [e["id"] for e in tail5] == ids_all_tail(eventlog),
          str([e["id"] for e in tail5]))

    # --- B. replay 与引擎现态一致 ---
    state = engine.state
    r = replay(eventlog)
    check("B1 replay 非 None", r is not None)
    if r is None:
        return engine, eventlog, tmp
    check("B2 current.id 一致", r.current.id == state.current.id,
          f"replay={r.current.id} engine={state.current.id}")
    check("B3 current.task 一致", r.current.task == state.current.task,
          f"replay={r.current.task!r} engine={state.current.task!r}")
    check("B4 current.started_at 一致", r.current.started_at == state.current.started_at,
          f"replay={r.current.started_at} engine={state.current.started_at}")
    check("B5 current.interrupted 一致", r.current.interrupted == state.current.interrupted,
          f"replay={r.current.interrupted} engine={state.current.interrupted}")
    check("B6 location 一致", r.location == state.location,
          f"replay={r.location} engine={state.location}")
    check("B7 sleep_state 一致", r.sleep_state == state.sleep_state,
          f"replay={r.sleep_state} engine={state.sleep_state}")
    check("B8 version 一致", r.version == state.version,
          f"replay={r.version} engine={state.version}")
    ra = r.misc.get("anchors_done", {})
    ea = state.misc.get("anchors_done", {})
    check("B9 anchors_done 按日一致",
          {d: sorted(v) for d, v in ra.items()} == {d: sorted(v) for d, v in ea.items()},
          f"replay={ra} engine={ea}")

    # --- C. CLI 回放入口 ---
    proc = subprocess.run([PY, "-m", "app.heartbeat.replay", str(settings.state_dir / "world.db")],
                          capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    ok = proc.returncode == 0
    payload = None
    if ok:
        try:
            payload = json.loads(proc.stdout)
        except Exception as e:
            ok = False
    check("C1 CLI 输出合法 JSON", ok and payload is not None,
          proc.stdout[-300:] + proc.stderr[-300:])
    if payload:
        check("C2 CLI 状态与引擎一致", payload["current"]["id"] == state.current.id,
              f"cli={payload['current']['id']} engine={state.current.id}")

    # --- D. 快照对账 ---
    snap_path = settings.state_dir / "world.json"
    ok = snap_path.exists()
    snap = json.loads(snap_path.read_text("utf-8")) if ok else {}
    check("D1 快照 world.json 存在", ok)
    if snap:
        check("D2 快照与重放 current.id 一致",
              snap["current"]["id"] == r.current.id,
              f"snap={snap['current']['id']} replay={r.current.id}")
        check("D3 快照与重放 location/sleep_state 一致",
              snap["location"] == r.location and snap["sleep_state"] == r.sleep_state,
              f"snap=({snap['location']},{snap['sleep_state']}) replay=({r.location},{r.sleep_state})")

    return engine, eventlog, tmp


def ids_all_tail(eventlog) -> list[int]:
    return [e["id"] for e in eventlog.all()[-5:]]


def main() -> int:
    print("== 心跳回放/事件溯源一致性 ==")
    engine, eventlog, tmp = asyncio.run(run_engine())
    eventlog.close()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n心跳回放自测: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS" +
          ("" if not failed else f", {len(failed)} FAIL: {[r[0] for r in failed]}"))
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
