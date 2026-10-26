"""API 层冒烟自测(零外部依赖, 标准库 urllib)。

用法: 先起服务(python3 -m app --no-engine --no-boot), 再运行:
    python3 tests/smoke_api.py [base_url]     # 默认 http://127.0.0.1:8000

覆盖:
1. TTS 配置 PUT 保存(请求头/请求体两段原文, 含 CRLF/CJK/尾随空白/占位符)
   → GET 读回 → 逐字节一致断言(文本相等 + sha256 相等);
2. 已验收四路由回归: /world/state /programs /chat/conversations /plugins;
3. 开机联动契约(第 13 轮): GET /system/boot 状态端点 200 且 status 口径合法
   (--no-boot 起服时应为 idle; 联动逻辑由 tests/test_boot.py 专项覆盖)。

退出码 0=全部通过, 1=有失败(失败项打印到 stdout, 前缀 FAIL)。
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

# 刁钻载荷: CRLF/纯LF混合、多字节UTF-8、行尾空格、无尾换行、占位符
HEADER_RAW = (
    "Authorization: Bearer {{key}}\r\n"
    "X-Custom: 中文响应头·★\r\n"
    "Content-Type: application/json\r\n"
    "X-Trailing-Space: 有行尾空格  \r\n"
    "X-Last: 行尾CRLF带尾随tab\t\r\n"
)
BODY_RAW = (
    '{"model":"{{model}}","input":"{{text}}","voice":"{{voice}}",'
    '"instructions":"指令含\\r\\n换行与\\t制表与\\\\反斜杠",'
    '"payload":"多字节中文Ω≈√…","no_trailing_newline": true}'
)

results: list[tuple[str, bool, str]] = []


def _req(method: str, path: str, body: dict | None = None) -> tuple[int, dict | bytes]:
    url = BASE + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            rc = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        rc = e.code
    try:
        return rc, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return rc, raw


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))


def wait_ready(timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/api/v1/world/state", timeout=3) as r:
                if r.status == 200:
                    return True
        except urllib.error.HTTPError:
            return True  # 服务已响应(哪怕 4xx/5xx 也算起来了)
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    if not wait_ready():
        print(f"FAIL  服务在 {BASE} 上未就绪(60s)")
        return 1

    # ---------- ① GET /api/v1/tts/config 不再 500 ----------
    rc, cfg = _req("GET", "/api/v1/tts/config")
    check("GET /tts/config 返回 200", rc == 200, f"status={rc}")

    # ---------- ② PUT 保存两段原文 ----------
    put_body = {
        "endpoint": "https://api.xiaomimimo.com/v1/chat/completions",
        "apiKey": "sk-smoke-1234567890abcdef",
        "model": "mimo-v2.5-tts",
        "modelType": "prompt",
        "voice": "",
        "headerTemplateRaw": HEADER_RAW,
        "bodyTemplateRaw": BODY_RAW,
    }
    rc, put_out = _req("PUT", "/api/v1/tts/config", put_body)
    check("PUT /tts/config 返回 200", rc == 200, f"status={rc}")
    if rc == 200:
        h, b = put_out.get("request_header", {}), put_out.get("request_body", {})
        check("PUT 返回 request_header.round_trip_identical", h.get("round_trip_identical") is True)
        check("PUT 返回 request_body.round_trip_identical", b.get("round_trip_identical") is True)
        check("PUT 返回 header sha256 与发送字节一致",
              h.get("sha256") == hashlib.sha256(HEADER_RAW.encode("utf-8")).hexdigest())
        check("PUT 返回 body sha256 与发送字节一致",
              b.get("sha256") == hashlib.sha256(BODY_RAW.encode("utf-8")).hexdigest())

    # ---------- ③ GET 读回 → 逐字节一致断言 ----------
    rc, back = _req("GET", "/api/v1/tts/config")
    check("GET(读回) 返回 200", rc == 200, f"status={rc}")
    if rc == 200:
        got_h = back.get("headerTemplateRaw")
        got_b = back.get("bodyTemplateRaw")
        check("请求头原文逐字节一致(读回==发送)", got_h == HEADER_RAW,
              f"len sent={len(HEADER_RAW.encode())} got={len((got_h or '').encode())}")
        check("请求体原文逐字节一致(读回==发送)", got_b == BODY_RAW,
              f"len sent={len(BODY_RAW.encode())} got={len((got_b or '').encode())}")
        rs = back.get("rawStatus", {})
        check("rawStatus.header sha256 与发送字节一致",
              rs.get("request_header", {}).get("sha256") == hashlib.sha256(HEADER_RAW.encode("utf-8")).hexdigest())
        check("rawStatus.body sha256 与发送字节一致",
              rs.get("request_body", {}).get("sha256") == hashlib.sha256(BODY_RAW.encode("utf-8")).hexdigest())
        check("rawStatus.body bytes 计数一致",
              rs.get("request_body", {}).get("bytes") == len(BODY_RAW.encode("utf-8")))

    # ---------- ④ 已验收四路由回归 ----------
    # world/state 返回 {ok:...} 对象; programs/chat/plugins 返回 JSON 数组, 200 即通过
    for path in ("/api/v1/world/state", "/api/v1/programs", "/api/v1/chat/conversations", "/api/v1/plugins"):
        rc, out = _req("GET", path)
        if path.endswith("/world/state"):
            ok = rc == 200 and isinstance(out, dict) and out.get("ok") is not False
        else:
            ok = rc == 200 and isinstance(out, list)
        check(f"回归 {path}", ok, f"status={rc} body={str(out)[:120]}")

    # ---------- ⑤ 密钥脱敏回显(不泄露明文) ----------
    rc, cfg = _req("GET", "/api/v1/tts/config")
    if rc == 200:
        key = cfg.get("apiKey", "")
        check("密钥脱敏回显(不含明文)", bool(key) and "***" in key and "1234567890" not in key,
              f"apiKey={key!r}")

    # ---------- ⑥ 开机联动契约(第 13 轮; 本冒烟用 --no-boot 起服, 只验状态端点) ----------
    rc, boot = _req("GET", "/api/v1/system/boot")
    check("GET /api/v1/system/boot → 200 且 status 口径合法",
          rc == 200 and isinstance(boot, dict) and boot.get("status") in ("idle", "running", "done"),
          f"status={rc} body={str(boot)[:120]}")

    failed = [r for r in results if not r[1]]
    print(f"\n冒烟结果: {len(results) - len(failed)}/{len(results)} PASS" +
          ("" if not failed else f", {len(failed)} FAIL: {[r[0] for r in failed]}"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
