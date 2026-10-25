"""端到端联调 mock 上游:mock LLM(OpenAI 兼容) + mock TTS。

用途: ME_Agent 全系统联调时无需真实 key, 验证
  私聊消息 → 仲裁器 → 对话管道 → LLM(tool_calls) → speak 工具 →
  TTS bridge → 合成 → record 段下发 全链路。

用法:
  python3 scripts/e2e_mock_upstream.py                 # LLM=9902, TTS=9903
  python3 scripts/e2e_mock_upstream.py --llm-port 9902 --tts-port 9903

mock LLM 行为(按请求 messages 内容路由, 便于多轮工具循环):
  - 含 "发个语音"/"念一句" 且尚无 "工具结果:" → 返回 tool_calls 调 speak
  - 已含 "工具结果:" → 返回最终文本(工具循环结束)
  - 其他 → 返回占位文本回复

mock TTS 行为:
  - POST 任意路径 → {"audio": <base64 假 mp3(ID3 头, 256 字节)>}
  - GET  /v1/models → {"data": [{"id": "mock-tts-1"}]}

Ctrl-C 退出。
"""
from __future__ import annotations

import argparse
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAKE_MP3 = b"ID3" + b"\x00" * 253          # 256 字节, ID3 头过 looks_audio 检测
FAKE_MP3_B64 = base64.b64encode(FAKE_MP3).decode()


def _openai_payload(content: str = "", tool_calls: list | None = None) -> bytes:
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
        msg["content"] = content or ""
    body = {"id": f"chatcmpl-mock{int(time.time())}", "object": "chat.completion",
            "created": int(time.time()), "model": "mock-llm",
            "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


class LLMHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 精简日志
        print(f"[mock-llm] {self.command} {self.path}", flush=True)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception:
            req = {}
        messages = req.get("messages") or []
        joined = "\n".join(str(m.get("content", "")) for m in messages)
        has_tool_result = "工具结果:" in joined
        wants_voice = ("发个语音" in joined) or ("念一句" in joined)

        if wants_voice and not has_tool_result:
            # 第一轮: 请求 speak 工具
            tc = [{"id": "call_e2e_1", "type": "function",
                   "function": {"name": "speak",
                                "arguments": json.dumps({"text": "端到端测试语音:小默在测试链路"},
                                                         ensure_ascii=False)}}]
            payload = _openai_payload("", tool_calls=tc)
        elif has_tool_result:
            payload = _openai_payload("语音已经发出去啦,你听听看~")
        else:
            payload = _openai_payload("你好呀,我是小默(联调占位回复)")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.endswith("/models"):
            payload = json.dumps({"data": [{"id": "mock-llm"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()


class TTSHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[mock-tts] {self.command} {self.path}", flush=True)

    def _reply_audio(self):
        # 铁律验证: 回显收到的原始请求体(占位符替换结果), 供日志比对
        payload = json.dumps({"audio": FAKE_MP3_B64, "format": "mp3",
                              "received_path": self.path}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        print(f"[mock-tts] body={raw[:200].decode('utf-8', 'replace')}", flush=True)
        self._reply_audio()

    def do_GET(self):
        if self.path.endswith("/models"):
            payload = json.dumps({"data": [{"id": "mock-tts-1", "owned_by": "mock"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            # 直接回二进制音频(content-type audio/*)
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(FAKE_MP3)))
            self.end_headers()
            self.wfile.write(FAKE_MP3)


def main() -> int:
    ap = argparse.ArgumentParser(description="E2E mock upstream (LLM + TTS)")
    ap.add_argument("--llm-port", type=int, default=9902)
    ap.add_argument("--tts-port", type=int, default=9903)
    args = ap.parse_args()

    llm_srv = ThreadingHTTPServer(("127.0.0.1", args.llm_port), LLMHandler)
    tts_srv = ThreadingHTTPServer(("127.0.0.1", args.tts_port), TTSHandler)
    threading.Thread(target=llm_srv.serve_forever, daemon=True).start()
    print(f"[mock-llm] listening on http://127.0.0.1:{args.llm_port}/v1/chat/completions", flush=True)
    print(f"[mock-tts] listening on http://127.0.0.1:{args.tts_port}", flush=True)
    try:
        tts_srv.serve_forever()
    except KeyboardInterrupt:
        print("mock upstream exit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
