"""通道双模集成自测(2026-10-25 第 5 轮): forward 正向外连 + 断线重连自愈 + reverse 回归。

运行: python3 tests/test_gateway_dual.py     (ME_Agent 仓库根; 会拉起真实 python -m app 子进程)
覆盖:
  [forward]
    F1 连接建立: ME_Agent 主动外连模拟协议端, status.gateway.connected=true
    F2 self_id 学习(lifecycle 事件)
    F3 事件→回复全链路: 模拟端投私聊消息 → 管道(LLM 缺席降级占位回复) → send_private_msg 动作帧回模拟端
    F4 断线感知: 模拟端关闭 → connected=false
    F5 自动重连自愈: 模拟端重启 → connected=true 且动作帧链路恢复
  [reverse 回归]
    R1 mock_onebot_client 反向连入 + 私聊消息 → 期望收到文本回包(exit 0)

退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import websockets

PY = sys.executable
# 本测试进程及其所有子进程(python -m app / mock client)统一 UTF-8 模式:
# 与部署启动脚本同口径, 规避 GBK locale Windows 上管道编解码不一致。
os.environ["PYTHONUTF8"] = "1"
HOST = "127.0.0.1"
MOCK_PORT = 9911
APP_PORT_FORWARD = 8821
APP_PORT_REVERSE = 8822
SELF_ID = "10088"
USER_ID = "20001"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def http_get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.loads(r.read().decode())


def gateway_status(port: int) -> dict:
    return http_get(f"http://{HOST}:{port}/api/v1/system/status").get("gateway", {})


async def wait_for(port: int, pred, timeout_s: float, what: str) -> bool:
    """异步等待网关状态满足谓词(阻塞版会卡死本进程事件循环, 导致模拟端握手超时)。"""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            last = gateway_status(port)
            if pred(last):
                return True
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        await asyncio.sleep(0.3)
    print(f"    (等待超时: {what}; last={last})")
    return False


def seed_asleep_state(state_dir: Path) -> None:
    """预置 sleep_state=asleep 的 world.json(引擎启动时装载)。

    仲裁器 attend() 对「睡着 + 含急救词的 P0 私聊」走 emergency 立即回复分支,
    绕开拟人延迟(依赖当前活动的 interruptible 等级, 午休时段会延迟 2-18 分钟,
    测试不可依赖时刻) → 回复链路成为确定性断言。
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%S") + time.strftime("%z")[:3] + ":" + time.strftime("%z")[3:]
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "world.json").write_text(json.dumps({
        "version": 4,
        "ts": now,
        "current": {"id": "sleep", "started_at": now},
        "location": "家/床",
        "sleep_state": "asleep",
        "time": {"date": time.strftime("%Y-%m-%d"), "hhmm": time.strftime("%H:%M"),
                 "weekday": time.localtime().tm_wday},
        "body": {"energy": 100.0, "stress": 5.0, "hunger": 100.0},
        "misc": {"coffee_today": 0, "loc_since": None, "paused_of": None},
    }, ensure_ascii=False), "utf-8")


def spawn_app(port: int, env_extra: dict, asleep: bool = False) -> subprocess.Popen:
    env = dict(os.environ)
    state = Path(tempfile.mkdtemp(prefix=f"me-gw-{port}-"))
    if asleep:
        seed_asleep_state(state)
    env["ME_STATE_DIR"] = str(state)
    env.update(env_extra)
    return subprocess.Popen(
        [PY, "-m", "app", "--no-engine", "--port", str(port)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_app(p: subprocess.Popen) -> None:
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill()


# ============================================================
# 模拟协议端(正向 WS 服务)
# ============================================================
class MockProtoServer:
    """模拟 OneBot 协议端正向 WS 服务: 上报 lifecycle/心跳, 应答动作帧, 可推送私聊事件。"""

    def __init__(self):
        self.conns: set = set()
        self.actions: list[dict] = []
        self.server = None

    def _event(self, post_type: str, **kw) -> str:
        evt = {"time": int(time.time()), "self_id": int(SELF_ID), "post_type": post_type}
        evt.update(kw)
        return json.dumps(evt, ensure_ascii=False)

    async def handler(self, ws):
        self.conns.add(ws)
        await ws.send(self._event("meta_event", meta_event_type="lifecycle", sub_type="connect"))
        hb = self._event("meta_event", meta_event_type="heartbeat", status={}, interval=30000)
        await ws.send(hb)
        try:
            async for raw in ws:
                frame = json.loads(raw)
                if "action" in frame:
                    self.actions.append(frame)
                    await ws.send(json.dumps({
                        "status": "ok", "retcode": 0, "echo": frame.get("echo"),
                        "data": {"message_id": 4242},
                    }))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.conns.discard(ws)

    async def push_private(self, text: str):
        evt = self._event(
            "message", message_type="private", sub_type="friend", user_id=int(USER_ID),
            message=[{"type": "text", "data": {"text": text}}],
            raw_message=text,
            sender={"user_id": int(USER_ID), "nickname": "联调用户", "card": ""},
        )
        for ws in list(self.conns):
            await ws.send(evt)

    async def start(self):
        self.server = await websockets.serve(self.handler, HOST, MOCK_PORT)

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for ws in list(self.conns):
            await ws.close()
        self.conns.clear()

    async def wait_action(self, action: str, timeout_s: float = 20) -> dict | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for a in self.actions:
                if a.get("action") == action:
                    return a
            await asyncio.sleep(0.2)
        return None


async def run_forward_mode() -> None:
    print("[forward] 正向外连模式")
    mock = MockProtoServer()
    await mock.start()
    app = None
    try:
        app = spawn_app(APP_PORT_FORWARD, {
            "ME_ONEBOT_MODE": "forward",
            "ME_ONEBOT_WS_URL": f"ws://{HOST}:{MOCK_PORT}",
            "ME_ONEBOT_RECONNECT_S": "1",
        }, asleep=True)
        ok = await wait_for(APP_PORT_FORWARD, lambda s: s.get("connected") is True, 30, "首次连接")
        check("F1 forward 主动外连建立", ok)
        st = gateway_status(APP_PORT_FORWARD)
        check("F1b status 暴露 mode=forward + ws_url",
              st.get("mode") == "forward" and f":{MOCK_PORT}" in str(st.get("forward_url", "")),
              json.dumps(st)[:160])
        check("F2 self_id 自动学习", st.get("self_id") == SELF_ID, str(st.get("self_id")))

        # F3 事件→回复全链路(预置 asleep + 急救词 → emergency 立即回复;
        # LLM 缺席 → 占位回复经 send_private_msg 回到模拟端)
        mock.actions.clear()
        await mock.push_private("急！双模联调冒烟")
        act = await mock.wait_action("send_private_msg", 25)
        ok = act is not None and str(act.get("params", {}).get("user_id")) == USER_ID
        segs = (act or {}).get("params", {}).get("message", [])
        check("F3 私聊事件→动作帧回包链路", ok, json.dumps(act)[:160])
        check("F3b 占位回复为非空文本", any(s.get("type") == "text" and s.get("data", {}).get("text", "").strip() for s in segs),
              json.dumps(segs)[:160])

        # F4/F5 断线感知 + 自动重连自愈
        before = gateway_status(APP_PORT_FORWARD)
        await mock.stop()
        ok = await wait_for(APP_PORT_FORWARD, lambda s: s.get("connected") is False, 20, "断线感知")
        check("F4 断线感知(connected=false)", ok)

        await mock.start()   # 同端口重启模拟端
        ok = await wait_for(APP_PORT_FORWARD, lambda s: s.get("connected") is True, 25, "重连")
        after = gateway_status(APP_PORT_FORWARD)
        check("F5 断线自动重连自愈", ok,
              f"attempts {before.get('reconnect_attempts')}→{after.get('reconnect_attempts')}")
        hb_ok = await wait_for(APP_PORT_FORWARD,
                               lambda s: bool(s.get("connected") and s.get("heartbeat_ok")),
                               15, "重连后心跳")
        check("F5b 重连后心跳恢复", hb_ok)
        mock.actions.clear()
        await mock.push_private("急！重连后再发一条")
        act = await mock.wait_action("send_private_msg", 25)
        check("F5c 重连后事件→回包链路恢复", act is not None, "25s 内未收到动作帧")
    finally:
        if app:
            await asyncio.to_thread(stop_app, app)
        await mock.stop()


def run_reverse_regression() -> None:
    print("[reverse] 反向模式回归")
    app = spawn_app(APP_PORT_REVERSE, {"ME_ONEBOT_MODE": "reverse"}, asleep=True)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                http_get(f"http://{HOST}:{APP_PORT_REVERSE}/api/v1/system/health")
                break
            except Exception:
                time.sleep(0.4)
        r = subprocess.run(
            [PY, str(ROOT / "scripts" / "mock_onebot_client.py"),
             "--url", f"ws://{HOST}:{APP_PORT_REVERSE}/onebot",
             "--send-private", "急！双模回归冒烟", "--expect", "text", "--wait-s", "25"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120)
        check("R1 反向连入+私聊→文本回包(客户端 exit 0)",
              r.returncode == 0, f"rc={r.returncode} {r.stdout[-300:]} {r.stderr[-300:]}")
    finally:
        stop_app(app)


async def amain() -> int:
    await run_forward_mode()
    run_reverse_regression()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n双模自测: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS" +
          ("" if not failed else f", {len(failed)} FAIL: {[r[0] for r in failed]}"))
    return 0 if not failed else 1


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
