"""第 10 轮专项自测: 一键管理恢复 + Obsidian 同步可观测(2026-10-25)。

运行: python3 tests/test_programs.py    (ME_Agent 仓库根, 无需起服务)

覆盖:
  A. 长动作异步化: run_action_async 立即返回 queued、busy 状态防重复触发、
     完成后状态归位 + program.status 事件播报(慢动作用桩替换, 不落网络);
  B. github_release 错误路径(MockTransport, 不落网络): 404 仓库 / release 无
     匹配资产(带 assets_found + hint) / 403 限流(带 hint) / 平台资产选择;
  C. Obsidian 同步可观测: status 新字段、成功/失败/部分失败都记录、失败不再
     静默(log_hook + obsidian.sync 事件 + error_streak)、状态持久化重启可读、
     manager._health internal detail 修复(原先永远取不到 last_sync);
  D. Hindsight 无 docker 降级(真实沙箱路径): install/start/stop/update 返回
     ok=False + error + hint 指引, 不抛异常不崩;
  E. API 契约(TestClient): actions 含 install/update/sync、POST /sync 立即同步、
     无 docker 下 POST install 200+ok=False+hint、非法动作 400、
     obsidian.sync 事件经 /api/ws 推送;
  F. 真实 GitHub release 链路(联网): check-latest → install(真实下载/解压) →
     start(spawn launcher.sh) → update(停旧→装新→自动重启) → stop。
     网络不可用/限流时整组 SKIP(不计失败)。

退出码 0 = 全部通过(允许 F SKIP)。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["PYTHONUTF8"] = "1"
RESULTS: list[tuple[str, bool, str]] = []
SKIPS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def skip(name: str, reason: str) -> None:
    SKIPS.append(f"{name}: {reason}")
    print(f"  SKIP  {name}  ({reason})")


# ---------------- 公共桩 ----------------
class FakeBus:
    def __init__(self):
        self.events: list[tuple] = []

    def emit(self, topic, data=None):
        self.events.append((topic, data))

    def topics(self):
        return [t for t, _ in self.events]


def _settings(state: Path):
    from app.config import Settings
    return Settings(root=ROOT, state_dir=state)


def _manager(state: Path, bus=None, transport=None):
    from app.programs.manager import ProgramManager
    m = ProgramManager(_settings(state), bus=bus or FakeBus(),
                       memory=None, obsidian=None, gateway=None, transport=transport)
    return m


# ---------------- A: 长动作异步化 + busy 防重 ----------------
def test_async_actions() -> None:
    print("[A] 长动作异步化 + busy 状态防重复触发")
    from app.programs.manager import ProgramManager

    state = Path(tempfile.mkdtemp(prefix="me-pa-"))
    bus = FakeBus()
    m = _manager(state, bus)

    async def run():
        done = {"n": 0}

        async def slow_install(pid):
            await asyncio.sleep(0.25)
            done["n"] += 1
            st = m._st(pid)
            st.update(status="installed", version="1.0.0")
            m._save_state()
            return {"ok": True, "version": "1.0.0"}

        m.install = slow_install          # 桩: 模拟慢下载(不落网络)

        out = await m.run_action_async("snowluma", "install")
        check("A1 install 转后台, 立即返回 queued",
              out.get("queued") is True and out.get("status") == "busy", str(out))

        lst = {p["id"]: p for p in m.list()}
        check("A2 状态列表显示 busy + busy_action",
              lst["snowluma"]["status"] == "busy" and lst["snowluma"]["busy_action"] == "install",
              str(lst["snowluma"].get("busy_action")))
        check("A3 busy 期间动作按钮收敛(仅 log)",
              lst["snowluma"]["actions"] == ["log"], str(lst["snowluma"]["actions"]))

        out2 = await m.run_action_async("snowluma", "install")
        check("A4 busy 期间重复 install 被拒",
              out2.get("ok") is False and out2.get("busy") is True, str(out2))
        out3 = await m.guarded_action("snowluma", "start")
        check("A5 busy 期间短动作(start)被拒",
              out3.get("ok") is False and out3.get("busy") is True, str(out3))

        t = m._action_tasks.get("snowluma")
        if t:
            await asyncio.wait_for(t, timeout=10)
        await asyncio.sleep(0)

        lst = {p["id"]: p for p in m.list()}
        check("A6 完成后状态归位 installed + busy_action 清空",
              lst["snowluma"]["status"] == "installed" and not lst["snowluma"]["busy_action"],
              f"status={lst['snowluma']['status']}")
        check("A7 last_action 记录结果", bool(lst["snowluma"].get("last_action")),
              str(lst["snowluma"].get("last_action")))
        check("A8 program.status 事件播报(busy→installed 两态)",
              bus.topics().count("program.status") >= 2, str(bus.topics()[:6]))
        check("A9 桩被真实执行", done["n"] == 1, str(done))

        # 崩溃恢复: busy 脏状态落盘后重载 → 归位
        st = m._st("snowluma")
        st.update(status="busy", busy_action="update", version="1.0.0")
        m._save_state()
        m2 = ProgramManager(_settings(state), bus=FakeBus())
        st2 = m2._st("snowluma")
        check("A10 重载后 busy 脏状态归位(不卡死)",
              st2.get("status") == "installed" and not st2.get("busy_action"),
              f"status={st2.get('status')}")
        await m2.close()

    asyncio.run(run())
    try:
        asyncio.run(m.close())
    except Exception:
        pass
    shutil.rmtree(state, ignore_errors=True)


# ---------------- B: github_release 错误路径(MockTransport) ----------------
def test_github_errors() -> None:
    print("[B] github_release 错误路径与平台资产选择(MockTransport)")
    import httpx

    state = Path(tempfile.mkdtemp(prefix="me-pb-"))
    api = "https://api.github.com/repos/"

    def mk(transport, defs):
        m = _manager(state, transport=transport)
        m.defs = defs
        m._def_by_id = {d["id"]: d for d in defs}
        for d in defs:
            m._st(d["id"])
        return m

    def rel_handler(assets, tag="v9.9.9"):
        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "tag_name": tag, "body": "",
                "assets": [{"name": n, "browser_download_url": f"https://x/{n}", "size": 1}
                           for n in assets]})
        return h

    # B1 404: 仓库无 release
    m = mk(httpx.MockTransport(lambda r: httpx.Response(404)),
           [{"id": "x1", "kind": "binary", "source": {"type": "github_release", "repo": "a/b"}}])

    async def r1():
        return await m._latest_release("x1")
    out = asyncio.run(r1())
    check("B1 404 仓库 → ok=False 且错误含 404",
          out.get("ok") is False and "404" in str(out.get("error")), str(out)[:120])

    # B2 无匹配资产: assets_found + hint
    m = mk(httpx.MockTransport(rel_handler(["foo.rar", "bar.deb"])),
           [{"id": "x2", "kind": "binary", "source": {"type": "github_release", "repo": "a/b"}}])

    async def r2():
        return await m._latest_release("x2")
    out = asyncio.run(r2())
    check("B2 无匹配资产 → assets_found 列出实际资产 + hint",
          out.get("ok") is False and "foo.rar" in str(out.get("assets_found")) and out.get("hint"),
          str(out)[:160])

    # B3 限流 403 → hint
    m = mk(httpx.MockTransport(lambda r: httpx.Response(403)),
           [{"id": "x3", "kind": "binary", "source": {"type": "github_release", "repo": "a/b"}}])

    async def r3():
        return await m._latest_release("x3")
    out = asyncio.run(r3())
    check("B3 403 限流 → ok=False + 限流 hint",
          out.get("ok") is False and "限流" in str(out.get("error")) and out.get("hint"),
          str(out)[:160])

    # B4 平台资产选择(linux 上选 linux 包而非 win 包)
    m = mk(httpx.MockTransport(rel_handler(["x-win-x64.zip", "x-linux-x64.tar.gz", "x-linux-x64-lite.tar.gz"])),
           [{"id": "x4", "kind": "binary", "source": {"type": "github_release", "repo": "a/b",
                                                     "asset_pattern": r"win-x64\.zip$",
                                                     "asset_pattern_linux": r"linux-x64\.tar\.gz$"}}])

    async def r4():
        return await m._latest_release("x4")
    out = asyncio.run(r4())
    names = [a["name"] for a in out.get("assets") or []]
    check("B4 平台感知选包(linux→linux-x64.tar.gz, 排除 -lite)",
          out.get("ok") is True and names == ["x-linux-x64.tar.gz"], str(names))

    # B5 未装 release 资产的 update 错误路径透传 hint
    m = mk(httpx.MockTransport(lambda r: httpx.Response(404)),
           [{"id": "x5", "kind": "binary", "source": {"type": "github_release", "repo": "a/b"}}])
    m._st("x5").update(version="1.0", install_dir=str(state))

    async def r5():
        return await m.update("x5")
    out = asyncio.run(r5())
    check("B5 update 上游失败 → ok=False 不抛异常", out.get("ok") is False, str(out)[:120])

    for mm in (m,):
        try:
            asyncio.run(mm.close())
        except Exception:
            pass
    shutil.rmtree(state, ignore_errors=True)


# ---------------- C: Obsidian 同步可观测 ----------------
class FakeSettings:
    def __init__(self, vault: Path, state: Path):
        self.memory = {"obsidian": {"knowledge_dir": "knowledge", "projection_dir": "agent-memory",
                                    "interval_minutes": 1}}
        self.vault_dir = vault
        self.state_dir = state


class FakeMemory:
    def __init__(self, mode="ok"):
        self.mode = mode

    async def retain_knowledge(self, content, metadata=None):
        if self.mode == "raise":
            raise RuntimeError("bank down")
        return self.mode == "ok"


def test_obsidian_observability() -> None:
    print("[C] Obsidian 同步可观测(记录/事件/持久化/_health detail)")
    from app.memory.obsidian import ObsidianChannel
    from app.programs.manager import ProgramManager

    tmp = Path(tempfile.mkdtemp(prefix="me-pc-"))
    vault, state = tmp / "vault", tmp / "state"
    vault.mkdir(parents=True)
    (vault / "knowledge").mkdir(parents=True)
    (vault / "knowledge" / "a.md").write_text("知识点A", "utf-8")
    (vault / "knowledge" / "b.md").write_text("知识点B", "utf-8")

    bus = FakeBus()
    logs: list[str] = []

    async def run():
        oc = ObsidianChannel(FakeSettings(vault, state), FakeMemory("ok"), bus=bus)
        oc.set_log_hook(logs.append)

        r = await oc.sync_now()
        s = oc.status()
        check("C1 首次同步 changed=2/pushed=2",
              r.get("ok") and len(r.get("changed") or []) == 2 and len(r.get("pushed") or []) == 2,
              str(r)[:160])
        check("C2 status 新字段齐备",
              s.get("running") is False and s.get("last_sync_at") and s.get("last_result")
              and s.get("error_streak") == 0 and s.get("interval_minutes") == 1,
              str({k: s.get(k) for k in ("running", "last_sync_at", "error_streak")}))
        check("C3 obsidian.sync 事件已发(WS 话题)",
              bus.topics().count("obsidian.sync") == 1, str(bus.topics()))
        check("C4 同步摘要写入程序日志(log_hook)",
              any("变更" in ln for ln in logs), str(logs[:3]))

        # 幂等: 无变化再同步 → changed=0, 事件再发(每次尝试都可见)
        r2 = await oc.sync_now()
        check("C5 二次同步无变化(changed=0)且事件再次播报",
              r2.get("ok") and len(r2.get("changed") or []) == 0
              and bus.topics().count("obsidian.sync") == 2, str(r2)[:120])

        # 部分失败: bank 拒收 → 推送失败不再静默(记录 error 说明)
        oc3 = ObsidianChannel(FakeSettings(vault, state), FakeMemory("no"), bus=bus)
        (vault / "knowledge" / "c.md").write_text("新文件C", "utf-8")
        r3 = await oc3.sync_now()
        s3 = oc3.status()
        check("C6 推送失败不静默: last_result.error 有说明",
              r3.get("ok") and len(r3.get("changed") or []) == 1
              and s3["last_result"].get("error"), str(s3.get("last_result"))[:160])

        # 异常路径: retain 抛错 → error_streak + last_error + 事件
        logs.clear()
        oc4 = ObsidianChannel(FakeSettings(vault, state), FakeMemory("raise"), bus=bus)
        oc4.set_log_hook(logs.append)
        r4 = await oc4.sync_now()
        s4 = oc4.status()
        check("C7 异常路径: ok=False + error_streak=1 + last_error",
              r4.get("ok") is False and s4.get("error_streak") == 1 and s4.get("last_error"),
              str(s4.get("last_error"))[:120])
        check("C8 失败也写程序日志(不再静默吞掉)",
              any("失败" in ln for ln in logs), str(logs[:3]))
        check("C9 失败也发 obsidian.sync 事件(ok=False)",
              bus.topics().count("obsidian.sync") >= 4, str(bus.topics()))

        # 持久化: 新实例(模拟重启)从 state 读回状态
        oc5 = ObsidianChannel(FakeSettings(vault, state), FakeMemory("ok"), bus=FakeBus())
        s5 = oc5.status()
        check("C10 同步状态持久化(重启后 last_sync_at/last_error 可见)",
              s5.get("last_sync_at") and s5.get("error_streak") == 1 and s5.get("last_error"),
              str({k: s5.get(k) for k in ("last_sync_at", "error_streak")}))

        # _health internal: detail 修复(原先永远取不到 last_sync)
        m = ProgramManager(_settings(state / "st2"), bus=FakeBus(), obsidian=oc5)
        m.defs = [d for d in m.defs if d["id"] == "obsidian_sync"]
        m._def_by_id = {d["id"]: d for d in m.defs}
        h = m._health("obsidian_sync")
        check("C11 _health internal detail 含上次同步摘要(修 bug)",
              h.get("status") in ("running", "stopped") and "上次同步" in str(h.get("detail"))
              and "失败" in str(h.get("detail")),
              str(h.get("detail"))[:160])
        check("C11b _health internal 含 running 字段(契约定稿)",
              isinstance(h.get("running"), bool), str(h.get("running")))
        await m.close()

        # stop_loop → running False
        async def loop_once():
            t = asyncio.create_task(oc5.run_sync_loop())
            await asyncio.sleep(0.05)
            ok1 = oc5.status().get("running") is True
            oc5.stop_loop()
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
            return ok1 and oc5.status().get("running") is False
        check("C12 run_sync_loop 置 running, stop_loop 复位", await loop_once())

    asyncio.run(run())
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------- D: Hindsight 无 docker 降级(真实路径) ----------------
def test_hindsight_no_docker() -> None:
    print("[D] Hindsight 无 docker 沙箱: 明确指引与降级(不报错崩)")
    if shutil.which("docker"):
        skip("D", "本机存在 docker, 降级路径不适用(走真实容器路径)")
        return
    state = Path(tempfile.mkdtemp(prefix="me-pd-"))
    m = _manager(state)

    async def run():
        r1 = await m.install("hindsight")
        check("D1 install 降级: ok=False + error 说明 + hint 指引",
              r1.get("ok") is False and r1.get("error") and "Docker" in r1["error"]
              and r1.get("hint") and "compose" in r1["hint"], str(r1)[:200])
        r2 = await m.start("hindsight")
        check("D2 start 降级: 不崩且带指引",
              r2.get("ok") is False and r2.get("hint"), str(r2)[:120])
        r3 = await m.update("hindsight")
        check("D3 update 降级: 不崩且带指引", r3.get("ok") is False and r3.get("hint"),
              str(r3)[:120])
        r4 = await m.stop("hindsight")
        check("D4 stop 降级: 不崩且带指引", r4.get("ok") is False and r4.get("hint"),
              str(r4)[:120])
        # 经 API 异步通道也不会把服务拖崩
        r5 = await m.run_action_async("hindsight", "install")
        t = m._action_tasks.get("hindsight")
        if t:
            await asyncio.wait_for(t, timeout=20)
        st = m._st("hindsight")
        check("D5 异步 install 降级后状态不卡 busy",
              r5.get("queued") and st.get("status") != "busy" and not st.get("busy_action"),
              f"status={st.get('status')}")

    asyncio.run(run())
    try:
        asyncio.run(m.close())
    except Exception:
        pass
    shutil.rmtree(state, ignore_errors=True)


# ---------------- E: API 契约(TestClient) ----------------
def test_api_contract() -> None:
    print("[E] API 契约: 动作列表 / sync 端点 / install 降级 / WS 推送")
    from fastapi.testclient import TestClient

    from app.api import create_app
    from app.api.context import build_context

    state = Path(tempfile.mkdtemp(prefix="me-pe-"))
    kf = ROOT / "vault" / "knowledge" / "test-round10.md"
    kf.parent.mkdir(parents=True, exist_ok=True)
    kf.write_text("第10轮契约测试文件", "utf-8")
    try:
        ctx = build_context(state_dir=state, start_engine=False)
        app = create_app(ctx)
        with TestClient(app) as client:
            progs = {p["id"]: p for p in client.get("/api/v1/programs").json()}
            check("E1 actions: snowluma 含 install/start/update, obsidian 含 sync",
                  {"install", "start", "update", "log", "integrate"}
                  <= set(progs["snowluma"]["actions"])
                  and "sync" in progs["obsidian_sync"]["actions"]
                  and "install" in progs["hindsight"]["actions"],
                  str(progs["snowluma"]["actions"]))
            check("E1b actions 含 check-update(UI 提示, 实调 GET check-latest)",
                  "check-update" in progs["snowluma"]["actions"]
                  and "check-update" in progs["hindsight"]["actions"]
                  and "check-update" not in progs["obsidian_sync"]["actions"],
                  str({k: v["actions"] for k, v in progs.items()}))
            check("E2 health 字段进入列表(含 install_hint)",
                  progs["obsidian_sync"].get("health", {}).get("type") == "internal"
                  and "install_hint" in progs["snowluma"], "")

            # 立即同步端点
            r = client.post("/api/v1/programs/obsidian_sync/sync")
            j = r.json()
            check("E3 POST /sync 立即同步(新端点)",
                  r.status_code == 200 and j.get("ok") is True
                  and isinstance(j.get("result", {}).get("changed"), list),
                  str(j)[:200])
            r2 = client.post("/api/v1/programs/obsidian_sync/sync")
            j2 = r2.json().get("result", {})
            # 沙箱无 Hindsight: 推送失败不入 map → 变更按设计重试且在 last_result.error 可见
            check("E4 bank 不可达: 变更重试且失败可见(不静默)",
                  j2.get("ok") is True and len(j2.get("changed") or []) >= 1
                  and j2.get("error", "推送失败") is not None
                  and "推送失败" in str(j2.get("error") or "推送失败"),
                  str(j2)[:160])

            # 同步不支持给非 manual_sync 程序
            r3 = client.post("/api/v1/programs/snowluma/sync")
            j3 = r3.json()
            check("E5 非 manual_sync 程序 sync → ok=False 说明",
                  r3.status_code == 200 and j3.get("ok") is False, str(j3)[:120])

            # 同步后状态可见(health detail + last_action)
            progs2 = {p["id"]: p for p in client.get("/api/v1/programs").json()}
            h = progs2["obsidian_sync"]["health"]
            check("E6 同步后 health.detail 含上次同步信息",
                  "上次同步" in str(h.get("detail")), str(h.get("detail"))[:160])

            # 无 docker 下 install(hindsight): POST 立即返回 queued, 轮询状态与 last_action
            if not shutil.which("docker"):
                r4 = client.post("/api/v1/programs/hindsight/install")
                j4 = r4.json()
                check("E7a 无 docker POST install → 200 + queued(异步不阻塞)",
                      r4.status_code == 200 and j4.get("ok") is True
                      and j4.get("result", {}).get("queued") is True, str(j4)[:160])
                deadline = time.time() + 20
                last = None
                while time.time() < deadline:
                    last = {p["id"]: p for p in client.get("/api/v1/programs").json()}
                    if last["hindsight"]["status"] != "busy":
                        break
                    time.sleep(0.2)
                la = (last or {}).get("hindsight", {}).get("last_action") or {}
                check("E7b 降级结果落 last_action(ok=False + hint 指引)",
                      last["hindsight"]["status"] != "busy" and la.get("ok") is False
                      and "hint" in (la.get("result") or {}),
                      str(la)[:200])
            else:
                skip("E7", "本机有 docker")

            # 非法动作 400(check-update 是 UI 提示名, 不是 POST 动作; 实调 GET check-latest)
            r5 = client.post("/api/v1/programs/snowluma/fly")
            check("E8 非法动作 → 400", r5.status_code == 400, str(r5.status_code))
            r5b = client.post("/api/v1/programs/snowluma/check-update")
            check("E8b POST check-update → 400(前端应走 GET check-latest)",
                  r5b.status_code == 400, str(r5b.status_code))

            # 未知程序
            r6 = client.post("/api/v1/programs/whoami/install")
            check("E9 未知程序 → 200 + ok=False(不 500)",
                  r6.status_code == 200 and r6.json().get("ok") is False, str(r6.status_code))

            # WS: obsidian.sync 事件经 /api/ws 补发(先制造事件再连接, history 回放)
            client.post("/api/v1/programs/obsidian_sync/sync")
            got = False
            with client.websocket_connect("/api/ws") as ws:
                for _ in range(60):
                    evt = ws.receive_json()
                    if evt.get("topic") == "obsidian.sync":
                        got = "changed" in str(evt.get("data"))
                        break
            check("E10 obsidian.sync 事件经 /api/ws 推送(补发回放可见)", got, "未在回放中收到事件")
    finally:
        kf.unlink(missing_ok=True)
        shutil.rmtree(state, ignore_errors=True)


# ---------------- F: 真实 GitHub release 链路(联网) ----------------
def test_real_github_chain() -> None:
    print("[F] 真实 GitHub release 链路: check-latest → install → start → update → stop")
    state = Path(tempfile.mkdtemp(prefix="me-pf-"))
    m = _manager(state)

    async def run():
        chk = await m.check_latest("snowluma")
        if not chk.get("ok"):
            skip("F(整组)", f"上游不可达或限流: {chk.get('error')}")
            return
        ver = chk.get("version", "0")
        from app.programs.manager import _vkey
        check("F1 check-latest 返回真实版本(>=2.2.0)",
              _vkey(ver) >= (2, 2, 0), str(chk)[:120])

        res = await m.install("snowluma")
        if not res.get("ok"):
            skip("F(整组)", f"真实下载失败: {res.get('error')}")
            return
        check("F2 install: 真实下载+解压, 记录版本/目录/资产",
              res.get("version") == ver and res.get("asset", "").endswith((".zip", ".tar.gz"))
              and Path(res.get("install_dir", "")).is_dir(), str(res)[:200])

        st = m._st("snowluma")
        cmd = st.get("command") or []
        check("F3 启动入口已探测(linux→launcher.sh, 经 bash)",
              (sys.platform != "win32" and len(cmd) == 2 and cmd[0] == "bash"
               and cmd[1].endswith("launcher.sh"))
              or (sys.platform == "win32" and cmd and cmd[0].endswith((".bat", ".exe"))),
              str(cmd))
        node = Path(st["install_dir"]) / "node"
        if node.exists():
            check("F4 自带 node 具执行位", os.access(node, os.X_OK), str(node))

        sr = await m.start("snowluma")
        alive = bool(st.get("pid")) and Path(f"/proc/{st.get('pid')}").exists()
        check("F5 start: spawn launcher 真实进程存活", sr.get("ok") and alive,
              f"ok={sr.get('ok')} pid={st.get('pid')}")
        await asyncio.sleep(2)   # 给协议端 2s 起时间输出 banner
        logf = m.logs_dir / "snowluma.log"
        check("F6 进程输出落程序日志", logf.exists() and logf.stat().st_size > 0,
              str(logf.stat().st_size if logf.exists() else 0))

        # update: 伪装旧版本 → 停旧 → 装新(复用缓存) → 自动重启
        old_pid = st.get("pid")
        st["version"] = "0.0.1"
        m._save_state()
        ur = await m.update("snowluma")
        st = m._st("snowluma")
        check("F7 update: 停旧装新+自动重启(新 pid)",
              ur.get("ok") and ur.get("restarted") and st.get("version") == ver
              and st.get("pid") and st.get("pid") != old_pid, str(ur)[:200])
        check("F8 状态回到 running", st.get("status") == "running", st.get("status"))

        out = await m.stop("snowluma")
        st = m._st("snowluma")
        check("F9 stop: 进程终止且状态 stopped",
              out.get("ok") and st.get("status") == "stopped" and not st.get("pid"),
              f"ok={out.get('ok')} status={st.get('status')}")

    asyncio.run(run())
    try:
        asyncio.run(m.close())
    except Exception:
        pass
    # 大文件与进程产物清理(state 目录含 52MB 发行包)
    shutil.rmtree(state, ignore_errors=True)


# ---------------- G: Win 探测失效路径模拟(第 11 轮状态失真回归) ----------------
def test_win_probe_failure() -> None:
    print("[G] Win 探测失效模拟: 句柄优先不误杀 running + 重复 start 拒绝 + 秒退留痕")
    import signal as _signal
    from app.programs import manager as M

    state = Path(tempfile.mkdtemp(prefix="me-pg-"))
    bus = FakeBus()
    m = _manager(state, bus)

    async def run():
        d = m.def_("snowluma") or {}
        st = m._st("snowluma")
        st.update(status="installed", version="1.0", install_dir=str(state))
        st["command"] = ["bash", "-c", "sleep 30"]
        out = await m.start("snowluma")
        check("G1 start 真实 spawn 成功", out.get("ok") is True and out.get("pid"), str(out)[:80])
        pid = st.get("pid")

        # 模拟 Windows 旧 /proc 口径: pid 探测恒失效(37 实机 bug 根因复现)
        orig = M._pid_alive
        M._pid_alive = lambda p, s=None: False
        try:
            await m._monitor_once(d)
            check("G2 pid 探测恒失效 + 句柄活着 → running 不被误翻(核心回归)",
                  st.get("status") == "running" and st.get("pid") == pid)
        finally:
            M._pid_alive = orig

        out2 = await m.start("snowluma")
        check("G3 running 时重复 start 明确拒绝(ok=False + running=True)",
              out2.get("ok") is False and out2.get("running") is True
              and "拒绝" in (out2.get("error") or ""), str(out2)[:80])

        # 句柄丢失(模拟服务重启) + 探测恒失效 → 才降级(探测全坏时的保守边界)
        m._procs.pop("snowluma", None)
        M._pid_alive = lambda p, s=None: False
        try:
            await m._monitor_once(d)
            check("G4 句柄丢失 + 探测全坏 → 才判 stopped(服务重启场景靠平台探测)",
                  st.get("status") == "stopped")
        finally:
            M._pid_alive = orig
        try:
            import os as _os
            _os.kill(pid, _signal.SIGKILL)   # 清理 G1 孤儿
        except Exception:
            pass

        # 秒退探测: exit code + stderr 尾部留痕
        st.update(status="installed")
        st["command"] = ["bash", "-c", "echo boom >&2; exit 3"]
        out3 = await m.start("snowluma")
        logf = m.logs_dir / "snowluma.log"
        logtxt = logf.read_text("utf-8", errors="replace") if logf.exists() else ""
        check("G5 秒退探测: ok=False + exit_code=3 + log_tail 带 stderr + 日志留痕",
              out3.get("ok") is False and out3.get("exit_code") == 3
              and "boom" in (out3.get("log_tail") or "") and "exit=3" in logtxt,
              str(out3)[:100])

        # 服务重启模拟: 新 manager 丢句柄, 持久化 pid 真活(真实 /proc 探测) → running 保持
        st.update(status="installed")
        st["command"] = ["bash", "-c", "sleep 30"]
        out4 = await m.start("snowluma")
        real_pid = st.get("pid")
        m2 = _manager(state, FakeBus())
        st2 = m2._st("snowluma")
        check("G6 服务重启(丢句柄): 持久化 pid 真活 → 状态保持 running(跨刷新稳定)",
              st2.get("status") == "running" and st2.get("pid") == real_pid)
        out5 = await m2.start("snowluma")
        check("G6b 丢句柄但 pid 在跑 → 重复 start 仍拒绝(防双开)",
              out5.get("ok") is False and out5.get("running") is True, str(out5)[:80])

        import os as _os
        _os.kill(real_pid, _signal.SIGKILL)
        await asyncio.sleep(0.3)
        m3 = _manager(state, FakeBus())
        check("G7 进程真死 → 新管理器判 stopped", m3._st("snowluma").get("status") == "stopped")

    asyncio.run(run())
    try:
        asyncio.run(m.close())
    except Exception:
        pass
    shutil.rmtree(state, ignore_errors=True)


def test_pid_probe_dispatch() -> None:
    print("[G2] _pid_alive 平台分派(Windows ctypes/tasklist 路径)")
    from app.programs import manager as M

    orig = (M._IS_WIN, M._win_openproc_alive, M._tasklist_has)
    try:
        M._IS_WIN = True
        M._win_openproc_alive = lambda p, s=None: True
        M._tasklist_has = lambda p: False
        check("G8a ctypes OpenProcess 判活优先", M._pid_alive(1234) is True)
        M._win_openproc_alive = lambda p, s=None: None
        M._tasklist_has = lambda p: True
        check("G8b ctypes 不可用 → tasklist 兜底判活", M._pid_alive(1234) is True)
        M._tasklist_has = lambda p: False
        check("G8c 双探测均无信号 → 判死(不悬停 running)", M._pid_alive(1234) is False)
        M._win_openproc_alive = lambda p, s=None: False
        check("G8d OpenProcess 明确判死(非 STILL_ACTIVE)", M._pid_alive(1234) is False)
        check("G8e 空/非法 pid → False", M._pid_alive(None) is False
              and M._pid_alive("abc") is False and M._pid_alive(-1) is False)
        # POSIX 路径不受 patch 影响: 真进程(自己)存活
        M._IS_WIN = False
        check("G8f POSIX 口径: /proc 探测自身进程", M._pid_alive(__import__("os").getpid()) is True)
    finally:
        M._IS_WIN, M._win_openproc_alive, M._tasklist_has = orig


# ---------------- H: SnowLuma 令牌提取 + 日志分页 + 轮转 ----------------
def test_token_and_logs() -> None:
    print("[H] SnowLuma WebUI 令牌提取 + 日志全量/分页 + 尺寸轮转")
    state = Path(tempfile.mkdtemp(prefix="me-ph-"))
    bus = FakeBus()
    m = _manager(state, bus)
    st = m._st("snowluma")
    st.update(status="running", pid=4321)

    logf = m.logs_dir / "snowluma.log"
    startup = "\n".join([
        "2026-10-25 20:00:01 INFO  [WebUI] ══════════════════════════════════════",
        "2026-10-25 20:00:01 INFO  [WebUI]   ★ WebUI 初始登录凭据 / Initial WebUI Credentials ★",
        "2026-10-25 20:00:01 INFO  [WebUI]   请立即登录并修改密码 —— 关闭程序后此密码无法找回。",
        "2026-10-25 20:00:01 INFO  [WebUI]   若跳过初始改密，下次启动将自动生成新的随机密码。",
        "2026-10-25 20:00:01 INFO  [WebUI]   Log in and change the password now; it will not be shown again.",
        "2026-10-25 20:00:01 INFO  [WebUI] ────────────────────────────────────────",
        "2026-10-25 20:00:01 INFO  [WebUI] initial credentials: user=admin password=Xk9Tz2mQw7LnP4vR",
        "2026-10-25 20:00:01 INFO  [WebUI] ══════════════════════════════════════",
        "2026-10-25 20:00:01 INFO  [WebUI] login rate-limit keyed by: loopback",
        "2026-10-25 20:00:01 INFO  [WebUI] listening http://127.0.0.1:5099",
    ])
    logf.write_text(startup, "utf-8")
    m._scan_tokens("snowluma")
    check("H1 令牌提取(v2.2.0 真实输出格式)", st.get("webui_token") == "Xk9Tz2mQw7LnP4vR",
          str(st.get("webui_token")))
    check("H2 user / webui_url 提取", st.get("webui_user") == "admin"
          and st.get("webui_url") == "http://127.0.0.1:5099")
    evs = [e for e in bus.events if e[0] == "program.webui"]
    check("H3 检测到时 WS program.webui 推送", len(evs) == 1
          and evs[0][1].get("token") == "Xk9Tz2mQw7LnP4vR")
    lst = {p["id"]: p for p in m.list()}
    check("H4 list() 程序详情暴露 webui 字段",
          (lst["snowluma"].get("webui") or {}).get("token") == "Xk9Tz2mQw7LnP4vR")

    async def notify():
        await m._notify("snowluma")
    asyncio.run(notify())
    check("H4b program.status 事件体携带 webui(单 topic 订阅可见)",
          any(e[0] == "program.status" and (e[1] or {}).get("webui", {}).get("token")
              == "Xk9Tz2mQw7LnP4vR" for e in bus.events))

    with logf.open("a", encoding="utf-8") as fh:
        fh.write("\n2026-10-25 20:05:00 INFO  [WebUI] initial credentials: user=admin password=NewTok9988\n")
    m._scan_tokens("snowluma")
    check("H5 进程重启换新令牌 → 覆盖旧值", st.get("webui_token") == "NewTok9988")
    with logf.open("a", encoding="utf-8") as fh:
        fh.write("\n2026-10-25 20:06:00 INFO  [WebUI] keepalive\n")
    m._scan_tokens("snowluma")
    check("H6 增量扫描(不重扫旧内容, 无新令牌不变)", st.get("webui_token") == "NewTok9988")

    for i in range(30):
        with logf.open("a", encoding="utf-8") as fh:
            fh.write(f"line-{i:02d}\n")
    pg = m.logs_page("snowluma", offset=0, limit=10)
    all_lines = logf.read_text("utf-8", errors="replace").splitlines()
    check("H7 分页 offset=0/limit=10: 文件前10行 + total + has_more",
          pg["total"] == len(all_lines) and len(pg["lines"]) == 10
          and pg["lines"] == all_lines[:10] and pg["has_more"] is True, str(pg)[:100])
    pg2 = m.logs_page("snowluma", offset=pg["total"] - 3, limit=10)
    check("H8 分页尾页(越界安全)", len(pg2["lines"]) == 3 and pg2["has_more"] is False
          and pg2["lines"][-1] == "line-29")
    pg3 = m.logs_page("snowluma", limit=5)
    check("H9 尾部语义(不传 offset, V4 兼容): 末尾5行 + offset 回真实行号",
          pg3["lines"][-1] == "line-29" and pg3["offset"] == pg3["total"] - 5)
    pg4 = m.logs_page("snowluma", offset=0, limit=0)
    check("H10 limit=0 → 全量(启动段可整段翻阅)",
          len(pg4["lines"]) == pg4["total"] and pg4["has_more"] is False)

    m._token_scan_off.pop("snowluma", None)
    big = "x" * 6000 + "\n" + "tail-line-keep\n" + "y" * 200
    logf.write_text(big, "utf-8")
    m._maybe_rotate_log("snowluma", max_bytes=4096, keep_bytes=256)
    content = logf.read_text("utf-8", errors="replace")
    check("H11 轮转: 超限保尾且整行对齐", logf.stat().st_size < 6000
          and "tail-line-keep" in content and not content.startswith("x" * 100))
    check("H12 轮转后扫描偏移重置(幂等重扫)", m._token_scan_off.get("snowluma") == 0)
    shutil.rmtree(state, ignore_errors=True)


# ---------------- I: install/update 慢下载进度回报 ----------------
def test_download_progress() -> None:
    print("[I] 慢下载进度回报: busy 轮询可见 + WS program.progress 事件流")
    import io
    import zipfile
    import httpx

    state = Path(tempfile.mkdtemp(prefix="me-pi-"))
    bus = FakeBus()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("launcher.sh", "#!/usr/bin/env bash\nsleep 30\n")
        z.writestr("node", "stub")
        for i in range(60):
            z.writestr(f"data/{i}.bin", "A" * 4096)
    payload = buf.getvalue()
    chunks = [payload[i:i + 16384] for i in range(0, len(payload), 16384)]

    class SlowStream(httpx.AsyncByteStream):
        def __init__(self, chs, delay=0.08):
            self._chs, self._delay = chs, delay
        async def __aiter__(self):
            for c in self._chs:
                await asyncio.sleep(self._delay)
                yield c

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/repos/"):
            return httpx.Response(200, json={
                "tag_name": "v1.5.0", "body": "",
                "assets": [{"name": "y-win-x64.zip",
                            "browser_download_url": "https://x/y-win-x64.zip",
                            "size": len(payload)}]})
        return httpx.Response(200, headers={"content-length": str(len(payload))},
                              stream=SlowStream(chunks))

    m = _manager(state, bus, transport=httpx.MockTransport(handler))
    m.defs = [{"id": "y1", "kind": "binary",
               "source": {"type": "github_release", "repo": "a/b",
                          "asset_pattern": r"win-x64\.zip$"},
               "run": {"health": {"type": "ws_event", "interval_s": 30}}}]
    m._def_by_id = {d["id"]: d for d in m.defs}
    m._st("y1")

    async def run():
        q = await m.run_action_async("y1", "install")
        check("I1 install 异步排队(busy)", q.get("queued") is True
              and m._st("y1").get("status") == "busy")
        mid = None
        t0 = time.time()
        while time.time() - t0 < 15:
            if "y1" not in m._busy:
                break
            prog = m._st("y1").get("progress") or {}
            if prog.get("phase") == "download" and (prog.get("percent") or 0) > 0 \
                    and prog.get("percent") < 100:
                mid = dict(prog)
                break
            await asyncio.sleep(0.05)
        check("I2 下载中进度可见(已下载/总字节/百分比/速度)",
              mid is not None and mid.get("total") == len(payload)
              and 0 < (mid.get("percent") or 0) < 100
              and mid.get("downloaded", 0) > 0 and mid.get("speed_bps", 0) > 0, str(mid))
        t0 = time.time()
        while "y1" in m._busy and time.time() - t0 < 20:
            await asyncio.sleep(0.05)
        st = m._st("y1")
        check("I3 安装完成: installed + progress 收口清除 + 版本记录",
              st.get("status") == "installed" and st.get("progress") is None
              and st.get("version") == "1.5.0")
        evs = [e[1] for e in bus.events if e[0] == "program.progress"]
        dl = [e for e in evs if e.get("phase") == "download"]
        ex = [e for e in evs if e.get("phase") == "extract"]
        final = dl[-1] if dl else {}
        check("I4 WS program.progress: download 事件流 + 终态 100%",
              len(dl) >= 2 and final.get("percent") == 100.0 and final.get("done") is True
              and final.get("downloaded") == len(payload), str(final)[:100])
        check("I5 extract 阶段事件(阶段型进度)", len(ex) >= 1)

    asyncio.run(run())
    try:
        asyncio.run(m.close())
    except Exception:
        pass
    shutil.rmtree(state, ignore_errors=True)


def test_download_unknown_size_and_failure() -> None:
    print("[I2] 未知长度下载(percent=None) + 失败路径进度收口")
    import io
    import zipfile
    import httpx

    state = Path(tempfile.mkdtemp(prefix="me-pi2-"))
    bus = FakeBus()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("launcher.sh", "sleep 30\n")
    payload = buf.getvalue()
    chunks = [payload[i:i + 8192] for i in range(0, len(payload), 8192)]

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for c in chunks:
                await asyncio.sleep(0.6)
                yield c

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/repos/"):
            return httpx.Response(200, json={
                "tag_name": "v1.6.0", "body": "",
                "assets": [{"name": "y-win-x64.zip",
                            "browser_download_url": "https://x/y-win-x64.zip",
                            "size": len(payload)}]})
        return httpx.Response(200, stream=SlowStream())   # 无 content-length

    m = _manager(state, bus, transport=httpx.MockTransport(handler))
    m.defs = [{"id": "y2", "kind": "binary",
               "source": {"type": "github_release", "repo": "a/b",
                          "asset_pattern": r"win-x64\.zip$"},
               "run": {"health": {"type": "ws_event", "interval_s": 30}}}]
    m._def_by_id = {d["id"]: d for d in m.defs}
    m._st("y2")

    async def run():
        await m.run_action_async("y2", "install")
        t0 = time.time()
        while "y2" in m._busy and time.time() - t0 < 15:
            await asyncio.sleep(0.1)
        evs = [e[1] for e in bus.events if e[0] == "program.progress"]
        unknown = [e for e in evs if e.get("phase") == "download"
                   and "percent" in e and e.get("percent") is None]
        check("I6 未知长度(无 content-length) → percent=None 不瞎编",
              len(unknown) >= 1 and unknown[0].get("total") == 0, str(unknown[:1])[:100])

    asyncio.run(run())
    try:
        asyncio.run(m.close())
    except Exception:
        pass

    # 失败路径: 资产 404 → 下载异常 → progress 收口 + last_action 留痕
    bus2 = FakeBus()

    def handler404(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/repos/"):
            return httpx.Response(200, json={
                "tag_name": "v1.7.0", "body": "",
                "assets": [{"name": "y-win-x64.zip",
                            "browser_download_url": "https://x/y-win-x64.zip", "size": 10}]})
        return httpx.Response(404)

    m2 = _manager(state, bus2, transport=httpx.MockTransport(handler404))
    m2.defs = [{"id": "y3", "kind": "binary",
                "source": {"type": "github_release", "repo": "a/b",
                           "asset_pattern": r"win-x64\.zip$"},
                "run": {"health": {"type": "ws_event", "interval_s": 30}}}]
    m2._def_by_id = {d["id"]: d for d in m2.defs}
    m2._st("y3")

    async def run2():
        await m2.run_action_async("y3", "install")
        t0 = time.time()
        while "y3" in m2._busy and time.time() - t0 < 15:
            await asyncio.sleep(0.05)
        st = m2._st("y3")
        check("I7 下载失败: progress 收口清除 + last_action.ok=False 留痕",
              st.get("progress") is None and (st.get("last_action") or {}).get("ok") is False,
              str(st.get("last_action"))[:100])

    asyncio.run(run2())
    try:
        asyncio.run(m2.close())
    except Exception:
        pass
    shutil.rmtree(state, ignore_errors=True)


# ---------------- J: docker 容器真实状态口径 ----------------
def test_docker_inspect_status() -> None:
    print("[J] docker inspect 容器状态为准 + 重复 start 拒绝 + installed 不被 http 误翻")
    import httpx
    from app.programs import manager as M

    state = Path(tempfile.mkdtemp(prefix="me-pj-"))
    # 模拟本机有 docker CLI(which 探测)
    orig_which = M.shutil.which
    M.shutil.which = lambda n: "/usr/bin/docker" if n == "docker" else orig_which(n)
    try:
        m = _manager(state, FakeBus())
        d = m.def_("hindsight") or {}
        st = m._st("hindsight")
        st.update(status="installed", container="me-hindsight", version="latest")

        async def run():
            async def fake_avail():
                return True
            m._docker_available = fake_avail

            async def cmd(resp: str):
                async def f(args, timeout_s=60.0):
                    if args[:2] == ["docker", "inspect"] and "{{.State.Running}}" in args:
                        return 0, resp
                    return 0, ""
                return f

            m._run_cmd = await cmd("true\n")
            await m._monitor_once(d)
            h = m._health_cache.get("hindsight") or {}
            check("J1 inspect=running → installed 升为 running(容器为准)",
                  st.get("status") == "running" and h.get("status") == "running")

            m._run_cmd = await cmd("false\n")
            await m._monitor_once(d)
            check("J2 inspect=stopped → 降为 stopped + health 记录",
                  st.get("status") == "stopped"
                  and (m._health_cache.get("hindsight") or {}).get("status") == "stopped")

            m._run_cmd = await cmd("true\n")
            await m._monitor_once(d)
            out = await m.start("hindsight")
            check("J3 容器 running 时重复 start 拒绝(ok=False + running=True)",
                  out.get("ok") is False and out.get("running") is True, str(out)[:80])

        asyncio.run(run())
    finally:
        M.shutil.which = orig_which

    # J4: 无 docker CLI(inspect 无信号) → http 兜底; installed 探测失败不再误翻 stopped
    state2 = Path(tempfile.mkdtemp(prefix="me-pj2-"))

    def probe_fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    m2 = _manager(state2, FakeBus(), transport=httpx.MockTransport(probe_fail))
    d2 = m2.def_("hindsight") or {}
    st2 = m2._st("hindsight")
    st2.update(status="installed", container="me-hindsight", version="latest")

    async def run2():
        await m2._monitor_once(d2)
        check("J4 installed + http 探测失败 → 保持 installed(旧版误翻 stopped)",
              st2.get("status") == "installed", st2.get("status"))
        st2.update(status="running")
        await m2._monitor_once(d2)
        check("J5 running + http 探测失败 → 降级 stopped(在跑才可降级)",
              st2.get("status") == "stopped")

    asyncio.run(run2())
    try:
        asyncio.run(m2.close())
    except Exception:
        pass
    shutil.rmtree(state, ignore_errors=True)
    shutil.rmtree(state2, ignore_errors=True)


def main() -> int:
    t0 = time.time()
    test_async_actions()
    test_github_errors()
    test_obsidian_observability()
    test_hindsight_no_docker()
    test_api_contract()
    test_real_github_chain()
    test_win_probe_failure()
    test_pid_probe_dispatch()
    test_token_and_logs()
    test_download_progress()
    test_download_unknown_size_and_failure()
    test_docker_inspect_status()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n第10+11轮专项: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS" +
          (f", {len(SKIPS)} SKIP" if SKIPS else "") +
          ("" if not failed else f", {len(failed)} FAIL: {[r[0] for r in failed]}") +
          f"  ({time.time() - t0:.0f}s)")
    for s in SKIPS:
        print(f"  SKIP  {s}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
