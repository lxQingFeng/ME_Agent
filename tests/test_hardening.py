"""补强项自测: 实例锁 / 密钥脱敏 / 诊断导出 / 双开拒绝(2026-10-25 第 5 轮)。

运行: python3 tests/test_hardening.py     (ME_Agent 仓库根, 无需起服务)
覆盖:
  A. 实例锁单元: 同 pid 重入 / 跨进程阻塞(报错含 pid+指引) / 死进程回收(ABA 安全)
  B. 双实例启动拒绝: python -m app 同 state 目录双开 → 第二个进程 exit 2 + 明确提示;
     旧实例退出后第三次启动恢复成功(锁回收)。
  C. 脱敏工具单元: SECRET_KEY_PATTERN 命中→hasXxx; *_env 指针排除; 无明文外漏。
  D. 诊断导出 + TTS 回显: TestClient 起 app(engine off) →
     /api/v1/diagnostics/export 结构与密钥指纹(明文值绝不出现);
     /api/v1/tts/config 密钥回显为 "***"(不含明文)。
退出码 0 = 全部通过。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = sys.executable
# 本测试进程及其所有子进程统一 UTF-8 模式(与部署启动脚本同口径,
# 规避 GBK locale Windows 上管道编解码不一致)。
os.environ["PYTHONUTF8"] = "1"
os.environ["ME_AUTOSTART"] = "0"   # 第 13 轮: 关闭启动自联动(测试起服只测 API, 不真拉第三方程序)
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def _wait_http(url: str, timeout_s: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def _get(url: str) -> tuple[int, dict | list | None]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return -1, None


# ============================================================
# A. 实例锁单元
# ============================================================
def test_instance_lock_unit() -> None:
    print("[A] 实例锁单元")
    from app.instancelock import InstanceLockError, instance_lock

    d = Path(tempfile.mkdtemp(prefix="me-lock-"))

    # A1 同 pid 重入(同进程 = 同持有者)
    with instance_lock(d, timeout_s=1.0):
        with instance_lock(d, timeout_s=1.0):
            check("A1 同 pid 可重入", len(list(d.iterdir())) == 2)
    check("A2 释放后 claim 清零", len(list(d.iterdir())) == 0)

    # A3 跨进程阻塞: 子进程持锁 30s
    holder = subprocess.Popen(
        [PY, "-c",
         'import sys, time; sys.path.insert(0, "."); '
         'from app.instancelock import instance_lock\n'
         'with instance_lock(sys.argv[1], timeout_s=30):\n'
         '    print("HELD", flush=True)\n'
         '    time.sleep(30)', str(d)],
        cwd=str(ROOT), stdout=subprocess.PIPE, text=True, encoding="utf-8")
    holder.stdout.readline()  # HELD
    try:
        with instance_lock(d, timeout_s=1.0):
            check("A3 双开应被拒绝", False)
    except InstanceLockError as e:
        ok = holder.pid in e.holders and "ME_STATE_DIR" in str(e) and str(holder.pid) in str(e)
        check("A3 双开拒绝 + 提示含 pid 与指引", ok, str(e)[:100])

    # A4 持锁进程死亡 → 回收(死 pid claim 被清, 新启动成功)
    holder.kill()
    holder.wait()
    time.sleep(0.1)
    dead_prefix = f"me-agent.lock.claim-{holder.pid}-"
    with instance_lock(d, timeout_s=2.0):
        check("A4 死进程 claim 回收",
              not any(p.name.startswith(dead_prefix) for p in d.iterdir()))

    # A5 伪造死 pid 残留 claim(ABA): uuid 唯一, 不会误伤
    fake = d / "me-agent.lock.claim-999999-00000000-0000-0000-0000-000000000001"
    fake.write_text("7", encoding="utf-8")
    with instance_lock(d, timeout_s=2.0):
        check("A5 残留死 pid claim 自动清理", not fake.exists())
    check("A5b ABA: 新 claim 名唯一不复用",
          not any(p.name.startswith("me-agent.lock.claim-999999-") for p in d.iterdir()))


# ============================================================
# B. 双实例启动拒绝(进程级, python -m app)
# ============================================================
def test_double_start_rejection() -> None:
    print("[B] 双实例启动拒绝(进程级)")
    env = dict(os.environ)
    env.pop("ME_STATE_DIR", None)
    state = Path(tempfile.mkdtemp(prefix="me-dbl-"))
    env["ME_STATE_DIR"] = str(state)
    env["ME_ONEBOT_MODE"] = "reverse"

    def spawn(port: int):
        return subprocess.Popen(
            [PY, "-m", "app", "--no-engine", "--port", str(port)],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace")

    p1 = spawn(8831)
    try:
        ready = _wait_http("http://127.0.0.1:8831/api/v1/system/health", 30)
        check("B1 第一实例就绪", ready)

        p2 = spawn(8832)
        try:
            rc = p2.wait(timeout=20)
            err = p2.stderr.read() or ""
            ok = rc == 2 and "已有实例正在运行" in err and "ME_STATE_DIR" in err
            check("B2 第二实例被拒(exit 2 + 明确提示)", ok, f"rc={rc} err={err[:200]}")
        except subprocess.TimeoutExpired:
            p2.kill()
            check("B2 第二实例被拒", False, "20s 未退出(装死)")
    finally:
        p1.send_signal(signal.SIGTERM)
        try:
            p1.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p1.kill()

    # B3: 旧实例退出后 state 无残留 claim, 第三次启动成功(锁回收)
    claims = [p.name for p in state.iterdir() if p.name.startswith("me-agent.lock.claim-")]
    check("B3 退出后 claim 释放干净", claims == [], str(claims))
    p3 = spawn(8833)
    try:
        ready = _wait_http("http://127.0.0.1:8833/api/v1/system/health", 30)
        check("B3b 旧实例退出后可再次启动", ready)
    finally:
        p3.send_signal(signal.SIGTERM)
        try:
            p3.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p3.kill()


# ============================================================
# C. 脱敏工具单元
# ============================================================
def test_sanitizer_unit() -> None:
    print("[C] 脱敏工具单元")
    from app.api.sanitizer import is_masked, is_secret_key, mask_display, sanitize_tree

    cfg = {
        "api_key": "sk-PLAINTEXT-123", "accessToken": "tok-secret", "password": "p@ss",
        "cookie": "a=b", "Authorization": "Bearer xyz", "api_key_env": "DEEPSEEK_API_KEY",
        "hasApiKey": True, "nested": {"secret_value": "x", "url": "http://ok"},
        "list": [{"token": "t", "note": "keep"}],
    }
    out = sanitize_tree(cfg)
    flat = json.dumps(out, ensure_ascii=False)
    ok = ("hasApiKey" in out and "hasAccessToken" in out and "hasPassword" in out
          and "hasCookie" in out and "hasAuthorization" in out
          and "sk-PLAINTEXT-123" not in flat and "tok-secret" not in flat
          and out.get("api_key_env") == "DEEPSEEK_API_KEY"   # *_env 指针保留
          and "hasHasApiKey" not in flat                       # 派生键不连锁
          and out["nested"]["url"] == "http://ok"
          and "hasToken" in out["list"][0] and out["list"][0]["note"] == "keep")
    check("C1 sanitize_tree 指纹/排除/嵌套", ok, flat[:200])
    check("C2 mask_display 不带明文片段", mask_display("1234567890abcdef") == "***" and mask_display("") == "")
    check("C3 is_masked 判定", is_masked("***") and not is_masked("sk-real"))
    check("C4 is_secret_key 口径", is_secret_key("API_KEY") and not is_secret_key("api_key_env"))


# ============================================================
# D. 诊断导出 + TTS 回显(TestClient)
# ============================================================
def test_diagnostics_export() -> None:
    print("[D] 诊断导出 + TTS 回显")
    os.environ["ONEBOT_ACCESS_TOKEN"] = "diag-plaintext-token-xyz"
    os.environ["ME_INTERNAL_TOKEN"] = "diag-plaintext-internal-xyz"
    state = Path(tempfile.mkdtemp(prefix="me-diag-"))
    try:
        from fastapi.testclient import TestClient

        from app.api import create_app
        from app.api.context import build_context

        ctx = build_context(state_dir=state, start_engine=False)
        app = create_app(ctx)
        with TestClient(app) as client:
            r = client.get("/api/v1/diagnostics/export")
            body = r.text
            data = r.json()
            ok = (r.status_code == 200 and data.get("ok") is True
                  and "runtime" in data and "components" in data
                  and "recent_errors" in data and "config" in data)
            check("D1 导出结构与 200", ok, f"status={r.status_code}")

            # D2 密钥明文绝不出现
            no_leak = ("diag-plaintext-token-xyz" not in body
                       and "diag-plaintext-internal-xyz" not in body)
            check("D2 环境密钥明文零泄露", no_leak)

            # D3 指纹正确(hasXxx 布尔, 驼峰)
            cfg = data.get("config", {})
            env_fp = cfg.get("env", {})
            expected = ("hasOnebotAccessToken", "hasMeInternalToken",
                        "hasDeepseekApiKey", "hasHindsightApiKey")
            ok = (env_fp.get("hasOnebotAccessToken") is True
                  and env_fp.get("hasMeInternalToken") is True
                  and all(isinstance(env_fp.get(k), bool) for k in expected)
                  and "files" in cfg and "channels.yaml" in cfg.get("files", {}))
            check("D3 hasXxx 布尔指纹", ok, json.dumps(env_fp)[:200])

            # D4 TTS 配置回显: 密钥只回 *** (不含明文/片段)
            r2 = client.get("/api/v1/tts/config")
            if r2.status_code == 200:
                k = r2.json().get("apiKey", "")
                check("D4 TTS 密钥回显为 ***", k in ("", "***"), repr(k))
            else:
                check("D4 TTS 密钥回显为 ***", False, f"status={r2.status_code}")

            # D5 组件健康含网关模式
            gw = data.get("components", {}).get("gateway", {})
            check("D5 网关组件含 mode 字段", gw.get("mode") in ("reverse", "forward"), str(gw)[:120])

            # D6 程序注册表收缩口径: snowluma/hindsight/napcat 为 external
            progs = {p.get("id"): p for p in data.get("components", {}).get("programs", [])}
            ok = (progs.get("snowluma", {}).get("kind") == "binary"
                  and progs.get("hindsight", {}).get("kind") == "docker"
                  and progs.get("napcat", {}).get("kind") == "external"
                  and progs.get("obsidian_sync", {}).get("kind") == "internal"
                  and "install" in progs.get("snowluma", {}).get("actions", [])
                  and "start" in progs.get("snowluma", {}).get("actions", [])
                  and "update" in progs.get("snowluma", {}).get("actions", [])
                  and "install" in progs.get("hindsight", {}).get("actions", [])
                  and "update" in progs.get("hindsight", {}).get("actions", [])
                  and "sync" in progs.get("obsidian_sync", {}).get("actions", [])
                  and "start" not in progs.get("napcat", {}).get("actions", []))
            check("D6 程序管理口径(第10轮: binary/docker 恢复一键, napcat 仍 external)",
                  ok, json.dumps({k: (v.get("kind"), v.get("actions")) for k, v in progs.items()})[:240])
    finally:
        os.environ.pop("ONEBOT_ACCESS_TOKEN", None)
        os.environ.pop("ME_INTERNAL_TOKEN", None)


def main() -> int:
    test_instance_lock_unit()
    test_double_start_rejection()
    test_sanitizer_unit()
    test_diagnostics_export()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n补强自测: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS" +
          ("" if not failed else f", {len(failed)} FAIL: {[r[0] for r in failed]}"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
