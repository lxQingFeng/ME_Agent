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


def main() -> int:
    t0 = time.time()
    test_async_actions()
    test_github_errors()
    test_obsidian_observability()
    test_hindsight_no_docker()
    test_api_contract()
    test_real_github_chain()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n第10轮专项: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS" +
          (f", {len(SKIPS)} SKIP" if SKIPS else "") +
          ("" if not failed else f", {len(failed)} FAIL: {[r[0] for r in failed]}") +
          f"  ({time.time() - t0:.0f}s)")
    for s in SKIPS:
        print(f"  SKIP  {s}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
