"""输出整形器自测(2026-10-25 第 6 轮, 缺口 5-③)。

运行: python3 tests/test_shaper.py   (ME_Agent 仓库根, 无需起服务)
覆盖:
  A. 拆条 split: 短文单条; 长文按句切(≤阈值); max_parts 收口(尾段合并截断);
     无标点长文不丢内容; 拆条保序。
  B. 令牌桶: 满额 8 取 8 成、第 9 拒; 60s 恢复满额; 虚拟时钟直通(防忙等)。
  C. 延迟: 虚拟时钟下多段发送零真实延迟; 真实时钟小延迟区间确实生效。
  D. 发送路径: 多段逐条出网关(reply_to 仅首段)/逐条入库/逐条上总线;
     segments 原样透传; 空内容不出门; 语音段单独通道。
  E. 广播限额与睡眠静默: 日配额/时配额/最小间隔/静默窗(含跨午夜窗口与通配)。
退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.bus import EventBus
from app.clock import VirtualClock
from app.outbound.shaper import OutboundShaper, _in_window
from app.core.store import Store

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  | {detail}" if detail and not ok else ""))


class GatewayStub:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, chat_type, chat_id, segments, reply_to=None):
        self.sent.append({"chat_type": chat_type, "chat_id": chat_id,
                          "segments": segments, "reply_to": reply_to})
        return {"message_id": len(self.sent)}


def build(tmp: Path, clock=None, shaping_patch: dict | None = None):
    settings = Settings(root=ROOT, state_dir=tmp)
    if shaping_patch:
        settings.shaping.update(shaping_patch)
    store = Store(tmp / "console.db")
    bus = EventBus()
    gw = GatewayStub()
    out: list[dict] = []
    bus.subscribe("message.out", lambda evt: out.append(evt["data"]))
    sh = OutboundShaper(settings, gw, store, bus, clock=clock)
    return sh, gw, store, out


def long_text(n: int = 6) -> str:
    sents = [f"第{i}句这是一段用来测试拆条逻辑的长句子要超过阈值才能触发切分。" for i in range(1, n + 1)]
    return "".join(sents)


async def run() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="shp_"))

    # ---------- A. 拆条 ----------
    print("== A. 拆条 split ==")
    sh, gw, store, out = build(tmp / "a")
    th = int(sh.shaping.get("split_threshold", 120))
    check("A1 短文单条", sh.split("你好呀") == ["你好呀"])
    parts = sh.split(long_text(6))
    check("A2 长文多段且每段≤阈值(尾段合并除外)",
          len(parts) > 1 and all(len(p) <= th for p in parts[:-1]) and len(parts[-1]) <= th * 2,
          f"parts={[len(p) for p in parts]} th={th}")
    joined = "".join(p for p in parts)
    check("A3 拆条不丢字且保序", joined == long_text(6))
    many = sh.split(long_text(20))
    max_parts = int(sh.shaping.get("max_parts", 3))
    check("A4 max_parts 收口", len(many) <= max_parts and len(many[-1]) <= th * 2,
          f"n={len(many)}")
    nop = "无标点的超长文本" * 30
    check("A5 无标点长文整条送出不丢", "".join(sh.split(nop)) == nop)

    # ---------- B. 令牌桶 ----------
    print("== B. 群发令牌桶 ==")
    sh, gw, store, out = build(tmp / "b")     # 真实时钟
    rate = int(sh.shaping.get("group_rate_per_min", 8))
    oks = [sh._bucket_take("c1") for _ in range(rate)]
    check("B1 满额取尽全部成功", all(oks))
    check("B2 超额第 9 次拒绝", sh._bucket_take("c1") is False)
    sh._tokens["c1"] = (time.time() - 60, 0)  # 模拟 60s 前桶空
    check("B3 60s 后恢复满额", sh._bucket_take("c1") is True)
    # 虚拟时钟直通
    settings = Settings(root=ROOT, state_dir=tmp / "b2")
    vsh, _, _, _ = build(tmp / "b2", clock=VirtualClock(datetime(2026, 10, 24, 10, 0, tzinfo=ZoneInfo(settings.tz))))
    vsh._tokens["c1"] = (time.time(), 0)
    check("B4 虚拟时钟令牌桶直通(防忙等)", vsh._bucket_take("c1") is True)

    # ---------- C. 延迟 ----------
    print("== C. 打字延迟与段间隔 ==")
    # 虚拟: 3 段消息零真实延迟
    vsh, gw, store, out = build(tmp / "c1", clock=VirtualClock(datetime(2026, 10, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))))
    t0 = time.monotonic()
    await vsh.send("c1", long_text(4))
    dt = time.monotonic() - t0
    check("C1 虚拟时钟多段发送零延迟(<0.5s)", dt < 0.5, f"dt={dt:.3f}s")
    # 真实: 小区间延迟生效
    sh, gw, store, out = build(tmp / "c2", shaping_patch={"typing_delay_ms": [15, 20], "inter_message_gap_ms": [15, 20]})
    t0 = time.monotonic()
    await sh.send("c1", long_text(5))   # ≥2 段: 打字+间隔+打字
    dt = time.monotonic() - t0
    check("C2 真实时钟延迟确实生效(≥30ms)", dt >= 0.03, f"dt={dt:.3f}s")

    # ---------- D. 发送路径 ----------
    print("== D. 发送路径 ==")
    vsh, gw, store, out = build(tmp / "d1", clock=VirtualClock(datetime(2026, 10, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))))
    await vsh.send("g9", long_text(4), reply_to=12345)
    texts = [s["segments"][0]["data"]["text"] for s in gw.sent]
    check("D1 多段逐条出网关且保序", len(gw.sent) == len(texts) > 1
          and "".join(texts) == long_text(4), f"n={len(gw.sent)}")
    check("D2 reply_to 仅首段携带", gw.sent[0]["reply_to"] == 12345
          and all(s["reply_to"] is None for s in gw.sent[1:]))
    rows = store.query("SELECT content, role, origin FROM messages WHERE chat_id='g9' ORDER BY id")
    check("D3 每段落库为 assistant 消息", len(rows) == len(gw.sent)
          and all(r["role"] == "assistant" for r in rows), f"n={len(rows)}")
    check("D4 message.out 逐段上总线", len(out) == len(gw.sent))
    # segments 透传
    gw.sent.clear(); out.clear()
    segs = [{"type": "text", "data": {"text": "透传"}}, {"type": "at", "data": {"qq": "all"}}]
    await vsh.send("g9", "", segments=segs)
    check("D5 segments 原样透传(单次整包)", len(gw.sent) == 1 and gw.sent[0]["segments"] == segs)
    # 空发送
    gw.sent.clear()
    await vsh.send("g9", "")
    check("D6 空内容不出门", len(gw.sent) == 0)
    # 语音
    res = await vsh.send_record("g9", "AAAA")
    check("D7 语音段走 record 通道并回执", res.get("message_id") is not None
          and gw.sent[-1]["segments"][0]["type"] == "record"
          and gw.sent[-1]["segments"][0]["data"]["file"].startswith("base64://"))

    # ---------- E. 广播限额与静默窗 ----------
    print("== E. 广播限额/睡眠静默 ==")
    sh, gw, store, out = build(tmp / "e")
    ok, why = sh.broadcast_allowed()
    check("E1 初始放行", ok is True)
    # 日配额
    for _ in range(8):
        sh._broadcast_log.append(time.time() - 7200)   # 2h 前, 只占日配额
    ok, why = sh.broadcast_allowed()
    check("E2 日配额(8)拒绝", ok is False and why == "daily quota", f"why={why}")
    sh._broadcast_log = [time.time() - 3000] * 2       # 2 条 50min 前: 只占时配额(避开 min_gap)
    ok, why = sh.broadcast_allowed()
    check("E3 小时配额(2)满拒绝", ok is False and why == "hourly quota", f"why={why}")
    sh._broadcast_log = [time.time() - 90]             # 90s 前 1 条
    ok, why = sh.broadcast_allowed()
    check("E4 最小间隔(25min)拒绝", ok is False and why == "min gap", f"why={why}")
    sh._broadcast_log = []
    import time as _t
    real_localtime = _t.localtime
    class LT:  # 可控的 localtime 替身
        def __init__(self, h, m=0): self.tm_hour, self.tm_min = h, m
    _t.localtime = lambda *a, **k: LT(3, 30)          # 凌晨 03:30
    ok, why = sh.broadcast_allowed()
    check("E5 睡眠静默窗(00:00-08:00)拒绝", ok is False and why == "silence window", f"why={why}")
    _t.localtime = lambda *a, **k: LT(12, 0)          # 中午
    ok, why = sh.broadcast_allowed()
    check("E6 白天窗口外放行", ok is True, f"why={why}")
    _t.localtime = real_localtime
    # 跨午夜窗口 + 通配
    lt23 = LT(23, 0); lt12 = LT(12, 0); lt2 = LT(2, 0)
    check("E7 跨午夜静默窗(22:00-06:00)覆盖 23:00",
          _in_window("22:00-06:00", lt23) and _in_window("22:00-06:00", lt2)
          and not _in_window("22:00-06:00", lt12))
    check("E8 通配 * 全天静默", _in_window("*", lt12))
    # broadcast 落账
    sh._broadcast_log = []
    await sh.broadcast("g9", "冒泡")
    check("E9 broadcast 记账且 origin=broadcast", len(sh._broadcast_log) == 1
          and any(o.get("origin") == "broadcast" for o in out), f"log={len(sh._broadcast_log)}")


def main() -> int:
    print("== 输出整形器: 延迟/拆条/令牌桶/广播静默 ==")
    asyncio.run(run())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n整形器自测: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS" +
          ("" if not failed else f", {len(failed)} FAIL: {[r[0] for r in failed]}"))
    import shutil
    for d in Path(tempfile.gettempdir()).glob("shp_*"):
        shutil.rmtree(d, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
