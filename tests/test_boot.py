"""第 13 轮专项自测: 开机联动 boot 编排(2026-10-26, 契约定稿)。

运行: python3 tests/test_boot.py    (ME_Agent 仓库根, 无需起服务)

覆盖(慢动作用桩替换, 不落网络):
  A. 编排配置: programs.yaml autostart 解析(3 步顺序 hindsight→snowluma→
     obsidian_sync + 每步动作开关); boot_steps 规范化(internal 不代管安装);
     开关口径(env ME_AUTOSTART 优先于 yaml enabled; 关闭只影响启动自联动);
  B. 幂等 + 跳过已运行: 全部在跑 → 整链 skip 不触碰 install/start/integrate;
     busy 让路(控制台长动作进行中 → 该步让路跳过);
  C. 全链顺序: 未安装 → install→start→integrate 按步内序与步间序执行;
     二次联动全 skip(整链幂等); busy/进度字段收口不残留;
  D. 失败降级不阻塞: 安装失败(无 docker 口径 ok=False+hint)→ 降级记录并
     继续后续步骤(ok 仍 True); 启动失败 → 同降级且不再 integrate;
  E. 异常不阻塞: 步骤抛异常 → failed 记录, 链继续, ok=False 但不外溢;
     未知程序 id → failed 报错(config 错误可见);
  F. 并发互斥: 联动执行中重复触发(boot/boot_async)→ busy 拒绝;
  G. API 契约(TestClient, ME_AUTOSTART=0 起服): POST /system/boot 后台形态
     (queued+plan+started/skipped) / GET /system/boot 完成报告 / wait=true
     同步完整报告 / 幂等二次全 skip / system.boot 事件经 bus 播报;
  H. 启动自联动开关: ME_AUTOSTART=0 → startup 不建 autoboot 任务(状态 idle);
     开关放开 → startup 自动以 trigger=startup 执行联动。
  I. Windows 无窗口 spawn 验收: _WIN_NO_WINDOW 平台口径(Win=CREATE_NO_WINDOW)
     + 带标志真实 spawn 可用(桌面壳拉起 SnowLuma/compose 不闪黑窗)。

退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["PYTHONUTF8"] = "1"
os.environ["ME_AUTOSTART"] = "0"   # 本测试进程关闭启动自联动(H 节单独验开关行为)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


# ---------------- 公共桩 ----------------
class FakeBus:
    def __init__(self):
        self.events: list[tuple] = []

    def emit(self, topic, data=None):
        self.events.append((topic, data))

    def topics(self):
        return [t for t, _ in self.events]

    def of(self, topic):
        return [d for t, d in self.events if t == topic]


def _settings(state: Path):
    from app.config import Settings
    return Settings(root=ROOT, state_dir=state)


def _manager(state: Path, bus=None):
    from app.programs.manager import ProgramManager
    m = ProgramManager(_settings(state), bus=bus or FakeBus(),
                       memory=None, obsidian=None, gateway=None, transport=None)
    return m


def _patch_all_stubs(m, calls, install_impl=None, start_impl=None):
    """给 manager 装上全链桩: install/start/integrate/_boot_wait_http(记录调用序)。"""
    async def _install(pid):
        calls.append(("install", pid))
        if install_impl:
            return await install_impl(pid)
        st = m._st(pid)
        st.update(status="installed", version="9.9.9",
                  install_dir=str(m.opt_dir / pid))
        m._save_state()
        return {"ok": True, "version": "9.9.9"}

    async def _start(pid):
        calls.append(("start", pid))
        if start_impl:
            return await start_impl(pid)
        st = m._st(pid)
        if pid == "hindsight":
            st.update(status="running", container="me-hindsight", version="9.9.9")
        elif pid == "obsidian_sync":
            m._internal_tasks[pid] = asyncio.create_task(asyncio.sleep(30))
            st.update(status="running", started_at=time.time())
        else:
            st.update(status="running", pid=os.getpid(), started_at=time.time())
        m._save_state()
        return {"ok": True, "pid": os.getpid()}

    async def _integrate(pid):
        calls.append(("integrate", pid))
        return {"ok": True, "mode": "stub"}

    async def _wait_http(d, timeout_s):
        calls.append(("wait_http", d["id"]))
        return None

    m.install, m.start, m.integrate = _install, _start, _integrate
    m._boot_wait_http = _wait_http


async def _fake_container_state(m, value):
    async def _cs(pid):
        return value
    m._docker_container_state = _cs


def _cleanup_internal(m):
    for t in m._internal_tasks.values():
        t.cancel()
    m._internal_tasks.clear()


# ---------------- A: 编排配置与开关 ----------------
def test_boot_config() -> None:
    print("[A] 编排配置: autostart 解析 / 步骤规范化 / 开关口径")
    state = Path(tempfile.mkdtemp(prefix="me-boot-a-"))
    m = _manager(state)
    try:
        cfg = m.boot_config()
        check("A1 autostart 配置存在且默认启用", cfg.get("enabled") is True, str(cfg)[:120])
        ids = [s.get("id") for s in cfg.get("steps", [])]
        check("A2 步骤顺序 = hindsight → snowluma → obsidian_sync",
              ids == ["hindsight", "snowluma", "obsidian_sync"], str(ids))

        steps = {s["id"]: s for s in m.boot_steps()}
        check("A3 hindsight: install/start/integrate 全开 + health_wait_s",
              steps["hindsight"]["install"] and steps["hindsight"]["start"]
              and steps["hindsight"]["integrate"]
              and steps["hindsight"].get("health_wait_s") == 20, str(steps.get("hindsight")))
        check("A4 snowluma: install/start/integrate 全开",
              steps["snowluma"]["install"] and steps["snowluma"]["start"]
              and steps["snowluma"]["integrate"], str(steps.get("snowluma")))
        check("A5 obsidian_sync: start 开 / install+integrate 关(internal 规范化)",
              steps["obsidian_sync"]["start"] and not steps["obsidian_sync"]["install"]
              and not steps["obsidian_sync"]["integrate"], str(steps.get("obsidian_sync")))

        # 开关口径: env 优先, yaml 兜底
        for env, expect, name in (("0", False, "A6 env ME_AUTOSTART=0 → 关"),
                                  ("1", True, "A7 env ME_AUTOSTART=1 → 开"),
                                  ("false", False, "A8 env ME_AUTOSTART=false → 关")):
            os.environ["ME_AUTOSTART"] = env
            check(name, m.boot_enabled() is expect, f"env={env}")
        os.environ.pop("ME_AUTOSTART", None)
        check("A9 env 缺省 → yaml enabled(默认 true)", m.boot_enabled() is True)
        m.settings.programs_cfg = dict(m.settings.programs_cfg or {})
        m.settings.programs_cfg["autostart"] = {"enabled": False, "steps": []}
        check("A10 yaml enabled=false → 关", m.boot_enabled() is False)
        m.settings.programs_cfg["autostart"] = {"enabled": True, "steps": []}
        check("A11 steps 空 → boot_steps 规范为空(联动空转有报告)", m.boot_steps() == [])
    finally:
        os.environ["ME_AUTOSTART"] = "0"
        try:
            asyncio.run(m.close())
        except Exception:
            pass
        shutil.rmtree(state, ignore_errors=True)


# ---------------- B: 幂等 + 跳过已运行 + busy 让路 ----------------
def test_skip_running() -> None:
    print("[B] 幂等跳过: 全部在跑 → 不触发任何动作; busy 让路")
    state = Path(tempfile.mkdtemp(prefix="me-boot-b-"))
    bus = FakeBus()
    m = _manager(state, bus)
    calls: list[tuple] = []
    try:
        async def run():
            _patch_all_stubs(m, calls)
            await _fake_container_state(m, "running")     # hindsight 容器在跑
            st = m._st("snowluma")
            st.update(status="running", pid=os.getpid(), started_at=time.time())
            m._internal_tasks["obsidian_sync"] = asyncio.create_task(asyncio.sleep(30))
            m._st("obsidian_sync").update(status="running")

            rep = await m.boot(trigger="manual")
            check("B1 全部在跑 → 报告 ok 且 started 空",
                  rep.get("ok") is True and rep.get("started") == [], str(rep)[:160])
            check("B2 skipped 含三个程序", sorted(rep.get("skipped", []))
                  == ["hindsight", "obsidian_sync", "snowluma"], str(rep.get("skipped")))
            check("B3 未触碰 install/start/integrate(幂等核心)", calls == [], str(calls))
            check("B4 每步 outcome=skipped",
                  all(x.get("outcome") == "skipped" for x in rep.get("steps", [])),
                  str(rep.get("steps"))[:160])
            check("B5 状态机收口 done", m.boot_status().get("status") == "done")

            # busy 让路: 控制台正在给 snowluma 装更新 → 该步让路, 其余照常
            calls.clear()
            m._busy["snowluma"] = "install"
            t_obs = m._internal_tasks.pop("obsidian_sync", None)
            if t_obs:
                t_obs.cancel()
            await _fake_container_state(m, None)          # hindsight 容器没起
            st.update(status="installed")                 # snowluma 已装但停着
            rep2 = await m.boot(trigger="manual")
            step_by = {x["id"]: x for x in rep2.get("steps", [])}
            check("B6 busy 程序让路(outcome=busy, 不装不启)",
                  step_by.get("snowluma", {}).get("outcome") == "busy"
                  and ("install", "snowluma") not in calls and ("start", "snowluma") not in calls,
                  str(step_by.get("snowluma")))
            check("B7 其余步骤照常执行不被牵连",
                  ("install", "hindsight") in calls and ("start", "hindsight") in calls
                  and ("start", "obsidian_sync") in calls, str(calls))
            check("B8 busy 归属聚合到 skipped",
                  "snowluma" in rep2.get("skipped", []) and "hindsight" in rep2.get("started", []),
                  f"skipped={rep2.get('skipped')} started={rep2.get('started')}")
            check("B9 让路不吞别人的 _busy 登记", m._busy.get("snowluma") == "install")
            _cleanup_internal(m)

        asyncio.run(run())
    finally:
        _cleanup_internal(m)
        try:
            asyncio.run(m.close())
        except Exception:
            pass
        shutil.rmtree(state, ignore_errors=True)


# ---------------- C: 全链顺序 + 二次幂等 ----------------
def test_full_chain_order() -> None:
    print("[C] 全链: install→start→integrate 顺序 + 二次联动全 skip")
    state = Path(tempfile.mkdtemp(prefix="me-boot-c-"))
    bus = FakeBus()
    m = _manager(state, bus)
    calls: list[tuple] = []
    try:
        async def run():
            _patch_all_stubs(m, calls)
            await _fake_container_state(m, None)

            rep = await m.boot(trigger="startup")
            check("C1 报告 ok=True 且三步全 started",
                  rep.get("ok") is True and sorted(rep.get("started", []))
                  == ["hindsight", "obsidian_sync", "snowluma"], str(rep)[:180])
            expect = [("install", "hindsight"), ("start", "hindsight"),
                      ("wait_http", "hindsight"), ("integrate", "hindsight"),
                      ("install", "snowluma"), ("start", "snowluma"), ("integrate", "snowluma"),
                      ("start", "obsidian_sync")]
            check("C2 动作按编排序执行(hindsight→snowluma→obsidian_sync, 步内 install→start→integrate)",
                  calls == expect, str(calls))
            check("C3 obsidian 只 start 不 install/integrate(internal 规范)",
                  ("install", "obsidian_sync") not in calls
                  and ("integrate", "obsidian_sync") not in calls, str(calls))
            check("C4 健康等待只发生在 docker 形态(hindsight)",
                  calls.count(("wait_http", "hindsight")) == 1
                  and ("wait_http", "snowluma") not in calls, str(calls))
            st = m._st("hindsight")
            check("C5 安装阶段 busy 收口(不残留 busy/progress/busy_action)",
                  st.get("status") == "running" and st.get("busy_action") is None
                  and st.get("progress") is None and not m._busy,
                  f"status={st.get('status')} busy={m._busy}")
            check("C6 system.boot 事件 running→done 两拍(bus 播报)",
                  [e.get("status") for e in bus.of("system.boot")] == ["running", "done"],
                  str(bus.of("system.boot"))[:160])
            check("C7 program.status 事件照常推送(复用既有通道)",
                  bus.topics().count("program.status") >= 4, str(bus.topics()[:10]))
            check("C8 状态机 done + 报告可回读",
                  m.boot_status().get("status") == "done"
                  and m.boot_status().get("report", {}).get("ok") is True)

            # 二次联动: 整链幂等, 全 skip
            calls.clear()
            bus.events.clear()
            rep2 = await m.boot(trigger="manual")
            check("C9 二次联动全 skip(整链幂等)", rep2.get("started") == []
                  and sorted(rep2.get("skipped", [])) == ["hindsight", "obsidian_sync", "snowluma"]
                  and calls == [], f"started={rep2.get('started')} calls={calls}")
            _cleanup_internal(m)

        asyncio.run(run())
    finally:
        _cleanup_internal(m)
        try:
            asyncio.run(m.close())
        except Exception:
            pass
        shutil.rmtree(state, ignore_errors=True)


# ---------------- D: 失败降级不阻塞 ----------------
def test_degrade_not_block() -> None:
    print("[D] 降级: 安装失败(无 docker 口径)/启动失败 → 记录后继续, 不阻塞")
    state = Path(tempfile.mkdtemp(prefix="me-boot-d-"))
    m = _manager(state)
    calls: list[tuple] = []
    try:
        async def run():
            # 场景一: hindsight 无 docker → install ok=False + error + hint(真实降级口径)
            async def no_docker_install(pid):
                if pid != "hindsight":        # 其余程序安装正常, 验证降级不阻塞后续
                    st = m._st(pid)
                    st.update(status="installed", version="9.9.9",
                              install_dir=str(m.opt_dir / pid))
                    m._save_state()
                    return {"ok": True, "version": "9.9.9"}
                return {"ok": False, "error": "本机未检测到 Docker",
                        "hint": "三条路任选: ①安装 Docker Desktop…"}
            _patch_all_stubs(m, calls, install_impl=no_docker_install)
            await _fake_container_state(m, None)

            rep = await m.boot(trigger="manual")
            step_by = {x["id"]: x for x in rep.get("steps", [])}
            hs = step_by.get("hindsight", {})
            check("D1 无 docker → 该步 degraded + error/hint 透传",
                  hs.get("outcome") == "degraded" and hs.get("error") == "本机未检测到 Docker"
                  and bool(hs.get("hint")), str(hs)[:200])
            check("D2 降级步骤不再 start/integrate",
                  ("start", "hindsight") not in calls and ("integrate", "hindsight") not in calls,
                  str(calls))
            check("D3 后续步骤照常(snowluma/obsidian 联动不被阻塞)",
                  ("install", "snowluma") in calls and ("start", "snowluma") in calls
                  and ("start", "obsidian_sync") in calls, str(calls))
            check("D4 报告 ok=True(degraded 不算失败口径)",
                  rep.get("ok") is True and rep.get("degraded") == ["hindsight"]
                  and rep.get("started") == ["snowluma", "obsidian_sync"],
                  f"ok={rep.get('ok')} degraded={rep.get('degraded')}")
            st = m._st("hindsight")
            check("D5 失败后状态收口(不卡 busy、不残留 busy_action)",
                  st.get("status") != "busy" and st.get("busy_action") is None
                  and not m._busy, f"status={st.get('status')}")
            _cleanup_internal(m)

            # 场景二: snowluma 启动失败 → 该步降级, hindsight 照常
            # (重载同 state 目录: 先把场景一遗留的 running 复位, 独立验证启动失败路径)
            calls.clear()
            m2 = _manager(state, FakeBus())
            m2._st("snowluma").update(status="installed", pid=None)
            m2._st("obsidian_sync").update(status="stopped")
            m2._st("hindsight").update(status="not_installed")
            m2._save_state()
            async def start_fail(pid):
                if pid == "snowluma":
                    return {"ok": False, "error": "进程启动后立即退出(exit=1)"}
                st = m2._st(pid)
                st.update(status="running", pid=os.getpid(), started_at=time.time())
                m2._save_state()
                return {"ok": True, "pid": os.getpid()}
            _patch_all_stubs(m2, calls, start_impl=start_fail)
            await _fake_container_state(m2, None)
            rep2 = await m2.boot(trigger="manual")
            step_by2 = {x["id"]: x for x in rep2.get("steps", [])}
            check("D6 启动失败 → degraded + 不再 integrate 该程序",
                  step_by2.get("snowluma", {}).get("outcome") == "degraded"
                  and ("integrate", "snowluma") not in calls, str(step_by2.get("snowluma")))
            check("D7 启动失败链继续(ok=True, hindsight/obsidian started)",
                  rep2.get("ok") is True and rep2.get("degraded") == ["snowluma"]
                  and "hindsight" in rep2.get("started", []), str(rep2)[:180])
            _cleanup_internal(m2)
            try:
                await m2.close()
            except Exception:
                pass

        asyncio.run(run())
    finally:
        _cleanup_internal(m)
        try:
            asyncio.run(m.close())
        except Exception:
            pass
        shutil.rmtree(state, ignore_errors=True)


# ---------------- E: 异常不阻塞 + 未知程序 ----------------
def test_exception_not_block() -> None:
    print("[E] 异常不阻塞: 步骤抛异常 → failed 记录链继续; 未知 id → failed 可见")
    state = Path(tempfile.mkdtemp(prefix="me-boot-e-"))
    m = _manager(state)
    calls: list[tuple] = []
    try:
        async def run():
            async def boom_install(pid):
                if pid == "snowluma":
                    raise RuntimeError("boom")
                st = m._st(pid)
                st.update(status="installed", version="9.9.9", install_dir=str(m.opt_dir / pid))
                m._save_state()
                return {"ok": True, "version": "9.9.9"}
            _patch_all_stubs(m, calls, install_impl=boom_install)
            await _fake_container_state(m, None)

            rep = await m.boot(trigger="manual")
            check("E1 异常被收口为 failed(不外溢, boot 正常返回)",
                  isinstance(rep, dict) and rep.get("failed") == ["snowluma"], str(rep)[:160])
            step_by = {x["id"]: x for x in rep.get("steps", [])}
            check("E2 failed 步骤带异常信息",
                  "RuntimeError" in str(step_by.get("snowluma", {}).get("error", "")),
                  str(step_by.get("snowluma")))
            check("E3 链继续: obsidian_sync 仍被拉起",
                  ("start", "obsidian_sync") in calls and "obsidian_sync" in rep.get("started", []),
                  str(calls))
            check("E4 ok=False(failed 存在)但整链完成",
                  rep.get("ok") is False and m.boot_status().get("status") == "done")
            st = m._st("snowluma")
            check("E5 异常后状态收口(不卡 busy)",
                  st.get("status") != "busy" and not m._busy, f"status={st.get('status')}")
            _cleanup_internal(m)

            # 未知程序 id: config 错误 → failed 可见(不静默吞)
            calls.clear()
            m.settings.programs_cfg = dict(m.settings.programs_cfg or {})
            m.settings.programs_cfg["autostart"] = {
                "enabled": True,
                "steps": [{"id": "ghost"}, {"id": "obsidian_sync", "start": True}]}
            rep2 = await m.boot(trigger="manual")
            step_by2 = {x["id"]: x for x in rep2.get("steps", [])}
            check("E6 未知 id → failed + 明确报错",
                  step_by2.get("ghost", {}).get("outcome") == "failed"
                  and "未知程序" in str(step_by2.get("ghost", {}).get("error", "")),
                  str(step_by2.get("ghost")))
            check("E7 未知步骤不拖垮其余步骤",
                  ("start", "obsidian_sync") in calls and rep2.get("ok") is False, str(calls))

        asyncio.run(run())
    finally:
        _cleanup_internal(m)
        try:
            asyncio.run(m.close())
        except Exception:
            pass
        shutil.rmtree(state, ignore_errors=True)


# ---------------- F: 并发互斥 ----------------
def test_concurrent_guard() -> None:
    print("[F] 并发互斥: 联动执行中重复触发被拒")
    state = Path(tempfile.mkdtemp(prefix="me-boot-f-"))
    m = _manager(state)
    try:
        async def run():
            m._boot_state.update(status="running", trigger="manual")
            out = await m.boot(trigger="manual")
            check("F1 boot 执行中再触发 → busy 拒绝",
                  out.get("ok") is False and out.get("busy") is True, str(out)[:120])
            out2 = await m.boot_async(trigger="manual")
            check("F2 boot_async 执行中再触发 → busy 拒绝(带 state)",
                  out2.get("ok") is False and out2.get("busy") is True
                  and isinstance(out2.get("state"), dict), str(out2)[:120])
            m._boot_state.update(status="done")
            out3 = await m.boot_async(trigger="manual")
            check("F3 完成后可再次触发(幂等口径)",
                  out3.get("ok") is True, str(out3)[:120])
            if m._boot_task:
                await asyncio.wait_for(m._boot_task, timeout=15)
        asyncio.run(run())
    finally:
        _cleanup_internal(m)
        try:
            asyncio.run(m.close())
        except Exception:
            pass
        shutil.rmtree(state, ignore_errors=True)


# ---------------- G: API 契约(TestClient) ----------------
def test_api_contract() -> None:
    print("[G] API 契约: POST/GET /api/v1/system/boot(后台/wait/幂等/事件)")
    from fastapi.testclient import TestClient
    from app.api import create_app
    from app.api.context import build_context

    state = Path(tempfile.mkdtemp(prefix="me-boot-g-"))
    try:
        ctx = build_context(state_dir=state, start_engine=False)
        app = create_app(ctx)
        calls: list[tuple] = []
        _patch_all_stubs(ctx.programs, calls)

        async def _fake_cs(pid):
            return None
        ctx.programs._docker_container_state = _fake_cs

        with TestClient(app) as c:
            r = c.post("/api/v1/system/boot")
            out = r.json()
            check("G1 POST /system/boot → 200 后台形态(queued+plan)",
                  r.status_code == 200 and out.get("ok") is True and out.get("queued") is True
                  and isinstance(out.get("plan"), list) and len(out["plan"]) == 3,
                  f"status={r.status_code} body={str(out)[:160]}")
            plan_ids = [p.get("id") for p in out.get("plan", [])]
            check("G2 plan 清单顺序 = 编排顺序",
                  plan_ids == ["hindsight", "snowluma", "obsidian_sync"], str(plan_ids))
            plan_keys = {p.get("id"): p.get("actions") for p in out.get("plan", [])}
            check("G3 plan 动作口径(未装 → install+start+integrate; internal → 仅 start)",
                  plan_keys.get("hindsight") == ["install", "start", "integrate"]
                  and plan_keys.get("obsidian_sync") == ["start"], str(plan_keys))
            check("G4 started/skipped 清单在响应中",
                  sorted(out.get("started", [])) == ["hindsight", "obsidian_sync", "snowluma"]
                  and out.get("skipped") == [], f"started={out.get('started')}")

            # 等后台完成 → 完成报告
            deadline = time.time() + 10
            bs = {}
            while time.time() < deadline:
                bs = c.get("/api/v1/system/boot").json()
                if bs.get("status") == "done":
                    break
                time.sleep(0.1)
            check("G5 GET /system/boot → done + 完整报告",
                  bs.get("status") == "done" and bs.get("report", {}).get("ok") is True
                  and len(bs.get("report", {}).get("steps", [])) == 3, str(bs)[:160])

            progs = {p["id"]: p for p in c.get("/api/v1/programs").json()}
            check("G6 联动后程序状态可经既有 /programs 轮询",
                  progs.get("hindsight", {}).get("status") == "running"
                  and progs.get("snowluma", {}).get("status") == "running",
                  f"hindsight={progs.get('hindsight', {}).get('status')} "
                  f"snowluma={progs.get('snowluma', {}).get('status')}")

            # wait=true 同步完整报告 + 幂等
            n_calls_before = len(calls)
            r2 = c.post("/api/v1/system/boot?wait=true")
            rep = r2.json()
            check("G7 ?wait=true → 同步返回完整报告",
                  r2.status_code == 200 and rep.get("ok") is True and rep.get("trigger") == "manual"
                  and isinstance(rep.get("steps"), list) and len(rep.get("steps", [])) == 3,
                  f"status={r2.status_code} body={str(rep)[:160]}")
            check("G8 二次联动幂等(全 skip, 不再装/启)",
                  rep.get("started") == [] and len(rep.get("skipped", [])) == 3
                  and len(calls) == n_calls_before,
                  f"started={rep.get('started')} new_calls={calls[n_calls_before:]}")
            sys_evt = [e for e in ctx.bus.history if e.get("topic") == "system.boot"]
            check("G9 system.boot 事件经 bus 播报(WS /api/ws 可订阅)",
                  len(sys_evt) >= 4 and any(e["data"].get("status") == "done" for e in sys_evt),
                  f"n={len(sys_evt)}")
        _cleanup_internal(ctx.programs)
    finally:
        _cleanup_internal(ctx.programs)
        try:
            asyncio.run(ctx.programs.close())
        except Exception:
            pass
        shutil.rmtree(state, ignore_errors=True)


# ---------------- H: 启动自联动开关(AppContext.startup) ----------------
def test_startup_autoboot() -> None:
    print("[H] 启动自联动: ME_AUTOSTART 关/开口径(startup 自动触发)")
    from fastapi.testclient import TestClient
    from app.api import create_app
    from app.api.context import build_context

    # H1: ME_AUTOSTART=0(本文件顶部已置) → startup 不建 autoboot 任务
    state = Path(tempfile.mkdtemp(prefix="me-boot-h1-"))
    try:
        ctx = build_context(state_dir=state, start_engine=False)
        app = create_app(ctx)
        with TestClient(app) as c:
            names = [t.get_name() for t in ctx.tasks]
            check("H1 ME_AUTOSTART=0 → 无 autoboot 任务", "autoboot" not in names, str(names))
            bs = c.get("/api/v1/system/boot").json()
            check("H2 联动状态保持 idle(启动不自跑)",
                  bs.get("status") == "idle", str(bs)[:120])
        try:
            asyncio.run(ctx.programs.close())
        except Exception:
            pass
    finally:
        shutil.rmtree(state, ignore_errors=True)

    # H2: 开关放开 → startup 自动以 trigger=startup 执行联动
    state2 = Path(tempfile.mkdtemp(prefix="me-boot-h2-"))
    os.environ.pop("ME_AUTOSTART", None)
    try:
        ctx2 = build_context(state_dir=state2, start_engine=False)
        app2 = create_app(ctx2)
        fired: list[str] = []

        async def _boot_stub(trigger="manual"):
            fired.append(trigger)
            return {"ok": True, "trigger": trigger, "steps": [], "started": [],
                    "skipped": [], "degraded": [], "failed": []}
        ctx2.programs.boot = _boot_stub
        with TestClient(app2):
            deadline = time.time() + 8
            while not fired and time.time() < deadline:
                time.sleep(0.05)
            check("H3 开关放开 → startup 自动触发联动(trigger=startup)",
                  fired == ["startup"], str(fired))
            names2 = [t.get_name() for t in ctx2.tasks]
            check("H4 autoboot 任务纳入 ctx.tasks(shutdown 一并取消)",
                  "autoboot" in names2, str(names2))
        try:
            asyncio.run(ctx2.programs.close())
        except Exception:
            pass
    finally:
        os.environ["ME_AUTOSTART"] = "0"
        shutil.rmtree(state2, ignore_errors=True)


# ---------------- I: Windows 无窗口 spawn 验收(桌面壳场景) ----------------
def test_spawn_no_window() -> None:
    print("[I] SnowLuma/compose spawn 无窗口验收(CREATE_NO_WINDOW, 第 13 轮桌面壳)")
    import subprocess
    import app.programs.manager as pm
    on_win = os.name == "nt"
    check("I1 _WIN_NO_WINDOW 平台口径(Win=CREATE_NO_WINDOW, 其他=0)",
          pm._WIN_NO_WINDOW == (getattr(subprocess, "CREATE_NO_WINDOW", 0) if on_win else 0),
          f"val={pm._WIN_NO_WINDOW} os={os.name}")

    async def run():
        # 与 start()/_run_cmd 同参形态真实 spawn 一次: 标志在本平台可接受、进程正常退出
        p = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "pass",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            creationflags=pm._WIN_NO_WINDOW)
        out = await asyncio.wait_for(p.communicate(), timeout=15)
        return p.returncode

    rc = asyncio.run(run())
    check("I2 带标志真实 spawn 可用(退出码 0, 不抛不挂)", rc == 0, f"rc={rc}")


def main() -> int:
    t0 = time.time()
    test_boot_config()
    test_skip_running()
    test_full_chain_order()
    test_degrade_not_block()
    test_exception_not_block()
    test_concurrent_guard()
    test_api_contract()
    test_startup_autoboot()
    test_spawn_no_window()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n第13轮开机联动专项: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS" +
          ("" if not failed else f", {len(failed)} FAIL: {[r[0] for r in failed]}") +
          f"  ({time.time() - t0:.0f}s)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
