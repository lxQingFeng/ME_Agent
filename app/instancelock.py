"""单实例锁: state/ 下 claim 票据文件, 双实例启动直接拒绝(2026-10-25 补强)。

设计参照 K0nd1us/QQ-agent 0.5 instance-lock 的「claim 文件事务」移植为 Python:
- 每次加锁在 state/ 落一个 claim 文件 me-agent.lock.claim-<pid>-<uuid4>;
  文件名含 pid, 内容为票据号(ticket = 现存最大 ticket + 1), uuid 保证文件名
  永不复用 → 回收失效 claim 时不可能误删后来者的新 claim(ABA 防护);
- 事务窗口内扫描全部 claim: 死进程(pid 失效)的 claim 直接回收;
- 阻塞判定: 存在其他存活 claim 且 (其 ticket=0 写入未完成 / ticket 小于我 /
  ticket 相等但名字字典序小于我) → 等待, 超时即抛 InstanceLockError;
- 同 pid 的 claim 视为同一持有者(可重入), 跨进程才构成冲突;
- 持锁期间 claim 文件一直在(锁文件即心跳), finally 只删自己的 claim;
  进程被 kill -9 后残留 claim 由下一次启动回收(文件名唯一, ABA 安全)。

提示口径: 报错信息必须带阻塞 pid 与修复指引, 不得让用户面对无字面量的失败。
"""
from __future__ import annotations

import contextlib
import os
import re
import time
import uuid
from pathlib import Path
from typing import Iterator

CLAIM_PREFIX = "me-agent.lock.claim-"
CLAIM_RE = re.compile(
    r"^me-agent\.lock\.claim-([1-9]\d*)-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


class InstanceLockError(RuntimeError):
    """已有实例正在运行(或启动事务未收敛)。message 含阻塞 pid 与修复指引。"""

    def __init__(self, message: str, holders: list[int] | None = None):
        super().__init__(message)
        self.holders = holders or []


def _alive(pid: int) -> bool:
    """kill(pid, 0) 存活探测: ESRCH=死, EPERM=活(他人进程), 其余按活处理(保守)。"""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Windows 下无效 pid 走此分支; 参数错/权限错均按"无法判定"保守处理
        return True


def _scan(lock_dir: Path, own_name: str) -> list[dict]:
    """扫描 claim 文件; 回收死 pid 的 claim(文件名唯一 → 无 ABA)。"""
    rows: list[dict] = []
    for p in sorted(lock_dir.glob(CLAIM_PREFIX + "*")):
        m = CLAIM_RE.match(p.name)
        if not m:
            continue
        pid = int(m.group(1))
        if p.name == own_name or pid == os.getpid():
            continue          # 自己/本 pid(可重入)
        if not _alive(pid):   # 失效进程: 回收残留 claim
            with contextlib.suppress(OSError):
                p.unlink()
            continue
        ticket = 0            # 空或半写: 视为"对方正在选票"(阻塞后来者)
        try:
            ticket = int(p.read_text("utf-8").strip() or "0")
        except (OSError, ValueError):
            pass
        rows.append({"pid": pid, "name": p.name, "ticket": ticket})
    return rows


@contextlib.contextmanager
def instance_lock(state_dir: Path, timeout_s: float = 3.0,
                  poll_s: float = 0.05) -> Iterator[dict]:
    """进程级单实例锁(整个应用生命周期持有)。

    用法:
        with instance_lock(settings.state_dir) as lock:
            uvicorn.run(...)
    抛 InstanceLockError: 已有实例在跑(含阻塞 pid 与指引)。

    SIGTERM 兜底(实测发现): uvicorn 优雅停机后会恢复原 SIGTERM 处理器并
    re-raise 该信号(默认处置=杀进程), 上下文管理器的 finally 会被跳过导致
    claim 残留。这里持锁期间安装 SIGTERM 处理器: 先删自己的 claim 再以
    SystemExit(0) 退栈, 保证任意路径下锁文件都被清理(残留也能被下次回收)。
    """
    import signal

    lock_dir = Path(state_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    own_name = f"{CLAIM_PREFIX}{os.getpid()}-{uuid.uuid4()}"
    own_file = lock_dir / own_name
    fd = os.open(own_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)

    prev_term = None
    installed = False

    def _term_cleanup(signum, frame) -> None:
        with contextlib.suppress(OSError):
            own_file.unlink()
        raise SystemExit(0)

    try:
        try:
            prev_term = signal.signal(signal.SIGTERM, _term_cleanup)
            installed = True
        except (ValueError, OSError):
            pass    # 非主线程等场景: 跳过兜底, 依赖下次启动回收
        ticket = 0
        for row in _scan(lock_dir, own_name):
            ticket = max(ticket, row["ticket"])
        ticket += 1
        os.write(fd, str(ticket).encode())
        os.close(fd)
        fd = -1

        deadline = time.monotonic() + timeout_s
        while True:
            rows = _scan(lock_dir, own_name)
            blocked = [r for r in rows
                       if r["ticket"] == 0 or r["ticket"] < ticket
                       or (r["ticket"] == ticket and r["name"] < own_name)]
            if not blocked:
                break
            if time.monotonic() >= deadline:
                pids = sorted({r["pid"] for r in blocked})
                raise InstanceLockError(
                    "ME_Agent 已有实例正在运行(state 目录被 pid {} 持锁)。"
                    "请先停止旧实例(或换 ME_STATE_DIR); 若确认无实例运行, "
                    "删除 state/ 下 me-agent.lock.claim-* 残留文件后重试。".format(
                        ", ".join(str(x) for x in pids)),
                    holders=pids,
                )
            time.sleep(poll_s)
        yield {"pid": os.getpid(), "ticket": ticket, "claim": own_name}
    finally:
        if installed:
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(signal.SIGTERM, prev_term)
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            own_file.unlink()
