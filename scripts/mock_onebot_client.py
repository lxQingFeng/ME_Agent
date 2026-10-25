"""模拟 OneBot 对端(反向 WS 客户端): SnowLuma 缺席时的联调工具。

连接 ME_Agent 网关(默认 ws://127.0.0.1:8000/onebot), 之后:
  1. 上报 lifecycle connect(self_id 学习) + 周期心跳;
  2. 自动应答网关下发的所有动作帧(send_private_msg / send_group_msg / ...),
     回 {"status":"ok","retcode":0,"data":{"message_id":N}};
  3. --send-private <text> 投递一条私聊消息(OneBot v11 事件帧);
  4. 等待并打印网关回包(文本/语音段), 供断言。

用法:
  python3 scripts/mock_onebot_client.py --url ws://127.0.0.1:8000/onebot
  python3 scripts/mock_onebot_client.py --url ws://127.0.0.1:8000/onebot \
      --token <ONEBOT_ACCESS_TOKEN> \
      --send-private "你好" --expect text \
      --send-private "给我发个语音" --expect record

输出: 每个动作帧一行 JSON; 退出码 0=全部 expect 满足, 1=失败。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid

import websockets


def lifecycle_frame(self_id: str) -> dict:
    return {"post_type": "meta_event", "meta_event_type": "lifecycle",
            "sub_type": "connect", "self_id": int(self_id), "time": int(time.time())}


def heartbeat_frame(self_id: str) -> dict:
    return {"post_type": "meta_event", "meta_event_type": "heartbeat",
            "status": {"online": True}, "interval": 30000,
            "self_id": int(self_id), "time": int(time.time())}


def private_msg_event(self_id: str, user_id: str, text: str, msg_id: int) -> dict:
    return {
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "message_id": msg_id, "user_id": int(user_id), "self_id": int(self_id),
        "time": int(time.time()),
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        "sender": {"user_id": int(user_id), "nickname": "E2E测试号", "card": ""},
    }


async def run(args: argparse.Namespace) -> int:
    url = args.url
    if args.token:
        url += (("?access_token=" + args.token) if "?" not in url else ("&access_token=" + args.token))
    print(f"[client] connecting {url} self_id={args.self_id}", flush=True)
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps(lifecycle_frame(args.self_id)))
        print("[client] lifecycle connect sent", flush=True)

        sent_ids: set[str] = set()
        results: list[dict] = []      # 收到的动作帧记录
        msg_seq = 10000
        stop_at: float | None = None

        async def heartbeat():
            while True:
                await asyncio.sleep(20)
                try:
                    await ws.send(json.dumps(heartbeat_frame(args.self_id)))
                except Exception:
                    return

        hb = asyncio.create_task(heartbeat())

        async def sender():
            nonlocal stop_at, msg_seq
            # 依次投递私聊消息(间隔 1s, 留出处理时间)
            for text in args.send_private:
                await asyncio.sleep(1.0)
                msg_seq += 1
                evt = private_msg_event(args.self_id, args.user_id, text, msg_seq)
                await ws.send(json.dumps(evt))
                print(f"[client] >>> private message sent: {text!r}", flush=True)
            # 全部发完后等待响应窗口
            stop_at = time.time() + args.wait_s

        send_task = asyncio.create_task(sender())

        async def receiver():
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    if stop_at and time.time() > stop_at:
                        return
                    continue
                except Exception:
                    return
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(frame, dict) or "action" not in frame:
                    continue
                # 网关下发的动作帧: 记录 + 自动应答
                results.append(frame)
                summary = {
                    "action": frame.get("action"),
                    "params_keys": sorted((frame.get("params") or {}).keys()),
                    "message": frame.get("params", {}).get("message"),
                }
                print("[client] <<< action:", json.dumps(summary, ensure_ascii=False), flush=True)
                ack = {"status": "ok", "retcode": 0,
                       "data": {"message_id": 90000 + len(results)},
                       "echo": frame.get("echo", "")}
                await ws.send(json.dumps(ack))

        recv_task = asyncio.create_task(receiver())
        done, _ = await asyncio.wait({recv_task}, timeout=args.wait_s + len(args.send_private) * 2 + 15)
        hb.cancel()
        send_task.cancel()

        # ---- 断言 ----
        ok = True
        expect_kinds: list[str] = [e.strip().lower() for e in args.expect if e.strip()]
        if not args.send_private:
            print("[client] (dry-run: 未投递消息, 仅打印动作帧)", flush=True)
        for i, kind in enumerate(expect_kinds):
            # 按顺序匹配: 第 i 个 expect 对应第 i 个 send_private 之后应出现的回复
            # 宽松匹配: 从已收动作帧里找含文本段(或语音段)的回包
            seg_type = "record" if kind in ("record", "voice", "语音") else "text"
            hit = None
            for f in results:
                segs = (f.get("params") or {}).get("message") or []
                if any(s.get("type") == seg_type for s in segs):
                    hit = f
                    break
            if hit:
                print(f"[PASS] expect[{i}] {seg_type} 段已收到", flush=True)
            else:
                print(f"[FAIL] expect[{i}] 未收到含 {seg_type} 段的动作帧", flush=True)
                ok = False
        if expect_kinds and all("record" not in k and "voice" not in k and "语音" not in k
                                for k in expect_kinds):
            pass  # 纯文本期望
        print(f"[client] done: actions={len(results)} pass={ok}", flush=True)
        return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟 OneBot 对端(反向 WS 客户端)")
    ap.add_argument("--url", default="ws://127.0.0.1:8000/onebot")
    ap.add_argument("--token", default="", help="OneBot access token(与网关一致)")
    ap.add_argument("--self-id", default="10088", help="模拟 QQ 机器人号(网关学习)")
    ap.add_argument("--user-id", default="20001", help="模拟私聊用户 QQ")
    ap.add_argument("--send-private", action="append", default=[],
                    help="投递私聊消息文本(可多次)")
    ap.add_argument("--expect", action="append", default=[],
                    help="期望收到的回复段类型: text / record(可多次, 与 send 顺序对应)")
    ap.add_argument("--wait-s", type=float, default=25.0, help="发完后等回包秒数")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
