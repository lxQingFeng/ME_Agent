#!/usr/bin/env python3
"""git SSH 传输包装器(纯 Python, paramiko over ssh.github.com:443)。

用途: 本沙箱无 openssh 二进制(装不上), git push 无法走原生 ssh;
本脚本作为 GIT_SSH_COMMAND 被 git 调用, 用 paramiko 完成 SSH 传输,
git 侧的 pack 计算/协议/引用更新全部仍由 git 自身处理。

用法:
    GIT_SSH_COMMAND="/path/to/git-ssh-paramiko.py" git push origin master
    GIT_SSH_COMMAND="/path/to/git-ssh-paramiko.py" git ls-remote git@github.com:owner/repo.git
    # 建议同时: git -c ssh.variant=ssh ... 或环境变量 GIT_SSH_VARIANT=ssh

安全设计:
- 出站 22 端口被沙箱拦截 → 固定走 GitHub 官方备用入口 ssh.github.com:443;
- 主机密钥钉扎(pinning): 期望密钥来自官方 https://api.github.com/meta 的
  ssh_keys(全量公钥, 非指纹), 缓存于 ~/.ssh/github_hostkeys.json, 超过 7 天
  自动刷新; 连接握手后与远端主机密钥做全量比对, 不匹配立即断开;
- 用户认证仅使用 ~/.ssh/github_deploy_key (GitHub Deploy key), 不读代理/agent;
- 服务器 stderr 与 stdout 分离转发(git 协议正确性要求)。

退出码: 远端命令的退出状态; 本地异常返回 255。
"""
from __future__ import annotations

import base64
import json
import os
import select
import sys
import time
from pathlib import Path

import paramiko

GATEWAY_HOST = "ssh.github.com"   # GitHub 官方 443 备用入口, 等效 github.com
GATEWAY_PORT = 443
DEPLOY_KEY = Path.home() / ".ssh" / "github_deploy_key"
HOSTKEYS_CACHE = Path.home() / ".ssh" / "github_hostkeys.json"
HOSTKEYS_TTL = 7 * 24 * 3600     # 缓存 7 天
META_URL = "https://api.github.com/meta"
IO_TIMEOUT = 300.0               # 无 I/O 活动的看门狗(秒)

LOG_PREFIX = "git-ssh-paramiko:"


def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", file=sys.stderr, flush=True)


# ---------- 主机密钥钉扎 ----------
def _key_from_line(line: str) -> paramiko.pkey.PKey | None:
    parts = line.split()
    if len(parts) < 2:
        return None
    ktype, b64 = parts[0], parts[1]
    data = base64.b64decode(b64)
    try:
        if ktype == "ssh-rsa":
            return paramiko.RSAKey(data=data)
        if ktype == "ssh-ed25519":
            return paramiko.Ed25519Key(data=data)
        if ktype.startswith("ecdsa-sha2-nistp"):
            return paramiko.ECDSAKey(data=data)
    except Exception:
        return None
    return None


def _refresh_cache() -> list[str]:
    """从 api.github.com/meta 拉取 ssh_keys 并写缓存; 失败返回空列表。"""
    try:
        import urllib.request
        req = urllib.request.Request(META_URL, headers={"User-Agent": "git-ssh-paramiko"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        keys = data.get("ssh_keys", [])
        HOSTKEYS_CACHE.write_text(json.dumps({"fetched_at": time.time(), "ssh_keys": keys}), "utf-8")
        try:
            HOSTKEYS_CACHE.chmod(0o600)
        except OSError:
            pass
        return keys
    except Exception as e:  # 网络失败时退回缓存(由调用方兜底)
        log(f"meta 刷新失败(将用缓存): {e}")
        return []


def expected_host_keys() -> list[paramiko.pkey.PKey]:
    lines: list[str] = []
    fresh = False
    if HOSTKEYS_CACHE.exists():
        try:
            cached = json.loads(HOSTKEYS_CACHE.read_text("utf-8"))
            if time.time() - cached.get("fetched_at", 0) < HOSTKEYS_TTL:
                lines, fresh = cached.get("ssh_keys", []), True
        except Exception:
            pass
    if not fresh:
        lines = _refresh_cache() or lines
    keys = [k for k in (_key_from_line(l) for l in lines) if k is not None]
    return keys


def verify_host_key(server_key: paramiko.pkey.PKey, expected: list[paramiko.pkey.PKey]) -> None:
    if not any(server_key == k for k in expected):
        got = server_key.get_fingerprint()
        raise SystemExit(
            f"{LOG_PREFIX} 主机密钥不匹配, 疑似中间人, 已中止! "
            f"got={server_key.get_name()} {base64.b64encode(got).decode()}")


# ---------- 参数解析(git 以 ssh 方式调用本脚本) ----------
def parse_git_ssh_args(argv: list[str]) -> tuple[str, str] | None:
    """返回 (user@host, command)。git 调用形态: [opts...] user@host command。"""
    if "-G" in argv:               # git 的 ssh 变体探测: 无害应答
        print(f"hostname {GATEWAY_HOST}\nport {GATEWAY_PORT}")
        raise SystemExit(0)
    opts_with_value = {"-p", "-o", "-i", "-l", "-S", "-b", "-F", "-W", "-D", "-L"}
    pos: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            pos.extend(argv[i + 1:])
            break
        if a.startswith("-") and a != "-":
            i += 2 if a in opts_with_value else 1
            continue
        pos.append(a)
        i += 1
    if len(pos) < 2:
        return None
    return pos[0], pos[-1]


# ---------- 传输主体 ----------
def _load_identity() -> paramiko.pkey.PKey:
    """加载 deploy key 私钥(支持 ed25519/rsa/ecdsa 自动识别)。"""
    last_err: Exception | None = None
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return cls.from_private_key_file(str(DEPLOY_KEY))
        except Exception as e:
            last_err = e
    raise SystemExit(f"{LOG_PREFIX} 私钥加载失败 {DEPLOY_KEY}: {last_err}")


def run(user: str, command: str) -> int:
    if not DEPLOY_KEY.exists():
        raise SystemExit(f"{LOG_PREFIX} 缺少私钥 {DEPLOY_KEY}")
    expected = expected_host_keys()
    if not expected:
        raise SystemExit(f"{LOG_PREFIX} 无可用的钉扎主机密钥(缓存与 meta 均失败), 拒绝连接")

    import socket
    try:
        sock = socket.create_connection((GATEWAY_HOST, GATEWAY_PORT), timeout=30)
    except OSError as e:
        raise SystemExit(f"{LOG_PREFIX} 无法连接 {GATEWAY_HOST}:{GATEWAY_PORT}: {e}")

    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=30)          # SSH 握手
        verify_host_key(transport.get_remote_server_key(), expected)
        transport.auth_publickey(user, _load_identity())
        if not transport.is_authenticated():
            raise SystemExit(f"{LOG_PREFIX} 认证未通过(deploy key 被拒?)")
        chan = transport.open_session(timeout=30)
        transport.set_keepalive(15)
        chan.exec_command(command)
    except paramiko.AuthenticationException as e:
        transport.close()
        raise SystemExit(f"{LOG_PREFIX} SSH 认证失败: {e}")
    except paramiko.SSHException as e:
        transport.close()
        raise SystemExit(f"{LOG_PREFIX} SSH 协议错误: {e}")

    # 双工泵: 独立写线程(stdin→channel, 阻塞 sendall) + 主循环持续读
    # (只有持续排空对端输出, SSH 流控窗口才会推进, sendall 才不饿死)
    import threading

    def writer() -> None:
        stdin = sys.stdin.buffer
        try:
            while True:
                data = stdin.read1(65536)
                if not data:
                    break
                chan.sendall(data)
        except Exception as e:
            log(f"写线程异常: {e}")
        finally:
            try:
                chan.shutdown_write()
            except Exception:
                pass

    wt = threading.Thread(target=writer, daemon=True)
    wt.start()

    stdout, stderr = sys.stdout.buffer, sys.stderr.buffer
    last_activity = time.monotonic()
    rc: int | None = None

    while True:
        if chan.exit_status_ready() and chan.eof_received:
            while chan.recv_ready():
                stdout.write(chan.recv(65536))
            while chan.recv_stderr_ready():
                stderr.write(chan.recv_stderr(65536))
            stdout.flush()
            stderr.flush()
            rc = chan.recv_exit_status()
            break

        rl, _, _ = select.select([chan], [], [], 5.0)
        if not rl:
            if time.monotonic() - last_activity > IO_TIMEOUT:
                log(f"超过 {IO_TIMEOUT:.0f}s 无 I/O 活动, 看门狗中止")
                rc = 255
                break
            continue

        try:
            while chan.recv_ready():
                d = chan.recv(65536)
                if not d:
                    break
                stdout.write(d)
                last_activity = time.monotonic()
            stdout.flush()
            while chan.recv_stderr_ready():
                d = chan.recv_stderr(65536)
                if not d:
                    break
                stderr.write(d)
                last_activity = time.monotonic()
            stderr.flush()
        except (EOFError, OSError, paramiko.SSHException):
            pass   # 远端先关通道: 交由 exit_status 分支收尾

    wt.join(timeout=10)
    transport.close()
    return rc if rc is not None else 255


def main() -> int:
    parsed = parse_git_ssh_args(sys.argv[1:])
    if parsed is None:
        log(f"无法解析 git 的 ssh 调用参数: {sys.argv[1:]!r}")
        return 255
    userhost, command = parsed
    user, _, _host = userhost.rpartition("@")
    user = user or "git"
    return run(user, command)


if __name__ == "__main__":
    sys.exit(main())
