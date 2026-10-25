"""Obsidian 单向双通道:
A) 注入: vault/knowledge/*.md → Hindsight knowledge bank(headless 增量同步,15min 周期)
B) 投影: Hindsight fs mount → vault/agent-memory/(宿主机侧 CLI, 我们提供配置/状态监控/命令)
快照: 每日 00:10 导出 world 状态到 vault, vault 若为 git 仓库则自动 commit。"""
from __future__ import annotations
import asyncio
import hashlib
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

    # ---- 通道 A: knowledge → bank ----
    async def run_sync_loop(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.sync_knowledge()
            except Exception:
                pass
            await asyncio.sleep(self.interval * 60)

    def _load_map(self) -> dict:
        import json
        if self.map_path.exists():
            return json.loads(self.map_path.read_text("utf-8"))
        return {}

    def _save_map(self, m: dict) -> None:
        import json
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
            if self.bus:
                self.bus.emit("obsidian.sync", {"changed": changed, "pushed": pushed})
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
            "sync_map_entries": len(self._load_map()),
            "projection": self.projection_status(),
        }
