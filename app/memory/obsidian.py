"""Obsidian 单向双通道:
A) 注入: vault/knowledge/*.md → Hindsight knowledge bank(headless 增量同步,15min 周期)
B) 投影: Hindsight fs mount → vault/agent-memory/(宿主机侧 CLI, 我们提供配置/状态监控/命令)
快照: 每日 00:10 导出 world 状态到 vault, vault 若为 git 仓库则自动 commit。

同步可观测(2026-10-25 第 10 轮): 每次同步尝试(成功/失败)都记录
running / last_sync_at / last_result(changed/pushed/error) / last_error / error_streak,
失败不再静默——写入程序日志(log_hook 由 ProgramManager 注入 state/programs/logs)并
发 obsidian.sync 事件(WS 推给控制台); 状态持久化在 state/obsidian-sync.json 的
_status 键, 重启后仍可见。"""
from __future__ import annotations
import asyncio
import hashlib
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

class ObsidianChannel:
    def __init__(self, settings, memory, bus=None):
        self.settings = settings
        self.memory = memory
        self.bus = bus
        oc = settings.memory.get("obsidian", {})
        self.vault = settings.vault_dir
        self.knowledge_dir = self.vault / oc.get("knowledge_dir", "knowledge")
        self.projection_dir = self.vault / oc.get("projection_dir", "agent-memory")
        self.interval = int(oc.get("interval_minutes", 15))
        self.map_path = settings.state_dir / "obsidian-sync.json"
        self.mount_cmd = oc.get("mount_command", "")
        self._running = False
        # --- 同步状态(第 10 轮: 可观测 + 持久化) ---
        self._sync_lock = asyncio.Lock()
        self._log_hook = None          # manager 注入: 写入程序日志(state/programs/logs)
        self._last_sync_at: float | None = None
        self._last_result: dict | None = None    # {"changed": [...], "pushed": [...], "error": str|None}
        self._last_error: str | None = None
        self._error_streak = 0
        self._load_status()

    # ---- 状态记录(成功/失败都留痕) ----
    def set_log_hook(self, fn) -> None:
        self._log_hook = fn

    def _load_status(self) -> None:
        try:
            s = (self._load_map().get("_status") or {})
            self._last_sync_at = s.get("last_sync_at")
            self._last_result = s.get("last_result")
            self._last_error = s.get("last_error")
            self._error_streak = int(s.get("error_streak") or 0)
        except Exception:
            pass

    def _persist_status(self) -> None:
        try:
            m = self._load_map()
            m["_status"] = {"last_sync_at": self._last_sync_at,
                            "last_result": self._last_result,
                            "last_error": self._last_error,
                            "error_streak": self._error_streak}
            self._save_map(m)
        except Exception:
            pass

    def _log_line(self, line: str) -> None:
        if self._log_hook:
            try:
                self._log_hook(line)
            except Exception:
                pass

    def _record_success(self, res: dict) -> None:
        ch = list(res.get("changed") or [])
        pu = list(res.get("pushed") or [])
        partial = None
        if ch and len(pu) < len(ch):
            # 有变更但没推成(典型: Hindsight 未启动/bank 拒收)——不再静默, 记入 last_result.error
            partial = f"{len(ch) - len(pu)} 个文件推送失败(Hindsight 未启动/不可达或 bank 拒收)"
        self._last_sync_at = time.time()
        self._last_result = {"changed": ch, "pushed": pu, "error": partial}
        self._last_error = None
        self._error_streak = 0
        self._persist_status()
        self._log_line(f"[sync] 成功: 变更 {len(ch)} 个, 推送 {len(pu)} 个"
                       + (f"({', '.join(ch[:3])}…)" if ch else "")
                       + (f"; {partial}" if partial else ""))
        if self.bus:
            self.bus.emit("obsidian.sync", {"ok": True, "changed": ch, "pushed": pu,
                                            "error": partial, "error_streak": 0,
                                            "at": _now_iso()})

    def _record_error(self, e: BaseException) -> None:
        msg = f"{type(e).__name__}: {e}"
        self._last_sync_at = time.time()
        self._last_error = msg
        self._error_streak += 1
        self._last_result = {"changed": [], "pushed": [], "error": msg}
        self._persist_status()
        self._log_line(f"[sync] 失败(连续第 {self._error_streak} 次): {msg}")
        if self.bus:
            self.bus.emit("obsidian.sync", {"ok": False, "error": msg,
                                            "error_streak": self._error_streak, "at": _now_iso()})

    # ---- 通道 A: knowledge → bank ----
    async def sync_now(self) -> dict:
        """立即同步一次(循环与控制台「立即同步」共用): 失败不静默, 记录并返回。"""
        if self._sync_lock.locked():
            return {"ok": False, "busy": True, "error": "一次同步正在进行中，请稍候"}
        async with self._sync_lock:
            try:
                res = await self.sync_knowledge()
            except Exception as e:
                self._record_error(e)
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self._record_success(res)
            return {"ok": True, **res}

    async def run_sync_loop(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.sync_now()
            except asyncio.CancelledError:
                raise
            except Exception as e:      # 双保险: sync_now 已捕获, 这里兜底也留痕
                self._record_error(e)
            await asyncio.sleep(self.interval * 60)

    def stop_loop(self) -> None:
        self._running = False

    def _load_map(self) -> dict:
        if self.map_path.exists():
            try:
                return json.loads(self.map_path.read_text("utf-8"))
            except Exception:
                return {}
        return {}

    def _save_map(self, m: dict) -> None:
        self.map_path.parent.mkdir(parents=True, exist_ok=True)
        self.map_path.write_text(json.dumps(m, ensure_ascii=False, indent=1), "utf-8")

    async def sync_knowledge(self) -> dict:
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        m = self._load_map()
        changed, pushed = [], []
        for p in sorted(self.knowledge_dir.rglob("*.md")):
            rel = str(p.relative_to(self.knowledge_dir))
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            if m.get(rel) == h:
                continue
            changed.append(rel)
            content = p.read_text("utf-8")
            ok = await self.memory.retain_knowledge(content, {"source": rel})
            if ok:
                m[rel] = h
                pushed.append(rel)
        if changed:
            self._save_map(m)
        return {"changed": changed, "pushed": pushed}

    # ---- 通道 B: 投影状态 ----
    def projection_status(self) -> dict:
        exists = self.projection_dir.exists()
        files = 0
        freshest = None
        if exists:
            fs = list(self.projection_dir.rglob("*.md"))
            files = len(fs)
            if fs:
                freshest = max(f.stat().st_mtime for f in fs)
        return {
            "mounted": exists and files > 0,
            "dir": str(self.projection_dir),
            "files": files,
            "freshest_age_s": (time.time() - freshest) if freshest else None,
            "mount_command": self.mount_cmd,
            "note": "投影目录由宿主机执行 hindsight fs mount 生成; 已从注入源排除(防回声)",
        }

    # ---- 每日快照 ----
    def export_snapshot(self, state_dump: dict) -> Path:
        snap_dir = self.vault / "agent-snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        out = snap_dir / f"{datetime.now().strftime('%Y-%m-%d')}.md"
        lines = ["---", "type: agent-snapshot",
                 f"date: {datetime.now().strftime('%Y-%m-%d')}", "---", ""]
        st = state_dump.get("state", state_dump)
        lines.append(f"# 小默的世界快照 {st.get('time',{}).get('date','')}")
        lines.append("")
        lines.append(f"- 当前活动: {st.get('current',{}).get('id')}")
        lines.append(f"- 位置: {st.get('location')}")
        lines.append(f"- 睡眠: {st.get('sleep_state')}")
        body = st.get('body') or {}
        lines.append(f"- 能量/压力/饥饿: {body.get('energy',0):.0f}/{body.get('stress',0):.0f}/{body.get('hunger',0):.0f}")
        rendered = state_dump.get("rendered")
        if rendered:
            lines.append("")
            lines.append("## 叙述")
            lines.append("")
            lines.append(rendered)
        out.write_text("\n".join(lines), "utf-8")
        return out

    def git_commit_vault(self) -> dict:
        if not (self.vault / ".git").exists():
            return {"committed": False, "reason": "vault 不是 git 仓库"}
        try:
            subprocess.run(["git", "add", "-A"], cwd=self.vault, check=True, timeout=30)
            r = subprocess.run(["git", "commit", "-m", f"agent snapshot {datetime.now().isoformat(timespec='minutes')}"],
                               cwd=self.vault, capture_output=True, timeout=30)
            return {"committed": r.returncode == 0, "detail": r.stdout.decode()[-200:] if r.stdout else ""}
        except Exception as e:
            return {"committed": False, "reason": repr(e)}

    def status(self) -> dict:
        kd = list(self.knowledge_dir.rglob("*.md")) if self.knowledge_dir.exists() else []
        return {
            "vault": str(self.vault),
            "knowledge_files": len(kd),
            "sync_map_entries": len([k for k in self._load_map() if k != "_status"]),
            "projection": self.projection_status(),
            # --- 同步可观测(第 10 轮) ---
            "running": self._running,
            "interval_minutes": self.interval,
            "last_sync_at": self._last_sync_at,
            "last_result": self._last_result,          # {"changed": [...], "pushed": [...], "error": str|None}
            "last_error": self._last_error,
            "error_streak": self._error_streak,
        }


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
