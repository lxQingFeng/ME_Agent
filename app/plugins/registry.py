"""插件技能框架:manifest 扫描/校验/启停 + 沙箱执行 + 工具清单(LLM function calling)。"""
from __future__ import annotations
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from pydantic import BaseModel, Field

class ToolParam(BaseModel):
    type: str = "string"
    description: str = ""

class PluginManifest(BaseModel):
    name: str
    version: str = "1.0.0"
    type: str = "passive"            # passive / active
    description: str = ""
    entry: str = "main.py"
    tool: dict = Field(default_factory=dict)   # {function, description, parameters, timeout_s}
    permissions: list[str] = Field(default_factory=list)
    active: dict = Field(default_factory=dict)  # {every_minutes, condition}

class PluginRegistry:
    def __init__(self, settings, bus=None):
        self.settings = settings
        self.bus = bus
        self.plugins_dir = settings.root / "plugins"
        self.state_path = settings.state_dir / "plugins.json"
        self.manifests: dict[str, PluginManifest] = {}
        self.enabled: dict[str, bool] = {}
        self.last_errors: dict[str, str] = {}
        self.last_invoked: dict[str, float] = {}
        self.reload()

    def reload(self) -> int:
        self.manifests = {}
        for d in sorted(self.plugins_dir.iterdir()) if self.plugins_dir.exists() else []:
            if not d.is_dir() or d.name.startswith(("_", ".")):
                continue
            y = d / "skill.yaml"
            if not y.exists():
                continue
            try:
                import yaml
                raw = yaml.safe_load(y.read_text("utf-8")) or {}
                raw.setdefault("entry", "main.py")
                m = PluginManifest(**raw)
                if not (d / m.entry).exists():
                    self.last_errors[m.name] = f"entry {m.entry} missing"
                    continue
                self.manifests[m.name] = m
            except Exception as e:
                self.last_errors[d.name] = repr(e)
        saved = self._load_state()
        self.enabled = {name: saved.get(name, True) for name in self.manifests}
        self._save_state()
        return len(self.manifests)

    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text("utf-8"))
            except Exception:
                return {}
        return {}

    def _save_state(self) -> None:
        self.state_path.write_text(json.dumps(self.enabled, ensure_ascii=False, indent=1), "utf-8")

    def set_enabled(self, name: str, on: bool) -> bool:
        if name not in self.manifests:
            return False
        self.enabled[name] = on
        self._save_state()
        return True

    # ---- LLM 工具清单 ----
    def tools(self) -> list[dict] | None:
        tools = []
        for name, m in self.manifests.items():
            if not self.enabled.get(name, False) or not m.tool:
                continue
            props = {}
            required = []
            for k, v in (m.tool.get("parameters") or {}).items():
                props[k] = {"type": v.get("type", "string"), "description": v.get("description", "")}
                if v.get("required"):
                    required.append(k)
            tools.append({
                "type": "function",
                "function": {
                    "name": m.tool.get("function", name),
                    "description": m.tool.get("description", m.description),
                    "parameters": {"type": "object", "properties": props, "required": required},
                },
            })
        return tools or None

    # ---- 沙箱执行 ----
    def _sandbox_env(self, m: PluginManifest) -> dict:
        import os
        env = dict(os.environ)
        # 强制子进程(插件)以 UTF-8 模式运行: 保证 stdin/stdout 管道与文件读写
        # 不受宿主机 locale 影响(中文 Windows 默认 GBK 会把管道双方编码打岔)。
        env["PYTHONUTF8"] = "1"
        if "net:internal" in m.permissions:
            env["ME_CONSOLE_URL"] = f"http://127.0.0.1:{self.settings.console.get('port', 8080)}"
            token = self.settings.internal_token()
            if token:
                env["ME_INTERNAL_TOKEN"] = token
        return env

    async def invoke(self, name: str, params: dict, context: dict) -> dict:
        """按 function 名或插件名调用;返回 {result, actions}。"""
        m = self.manifests.get(name)
        if m is None:
            for mm in self.manifests.values():
                if mm.tool.get("function") == name:
                    m = mm
                    break
        if m is None:
            raise KeyError(f"plugin/function {name} not found")
        if not self.enabled.get(m.name, False):
            return {"result": f"插件 {m.name} 已停用"}
        self.last_invoked[m.name] = time.time()
        payload = {"op": "invoke", "params": params or {}, "context": context or {}}
        timeout = float(m.tool.get("timeout_s", 15))
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(self.plugins_dir / m.name / m.entry),
                cwd=str(self.plugins_dir / m.name),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=self._sandbox_env(m))
            out, err = await asyncio.wait_for(proc.communicate(
                input=json.dumps(payload, ensure_ascii=False).encode()), timeout=timeout)
        except asyncio.TimeoutError:
            self.last_errors[m.name] = f"timeout {timeout}s"
            return {"result": f"插件 {m.name} 超时"}
        except Exception as e:
            self.last_errors[m.name] = repr(e)
            return {"result": f"插件 {m.name} 执行失败"}
        try:
            data = json.loads(out.decode("utf-8"))
            self.last_errors.pop(m.name, None)
            return {"result": data.get("result"), "actions": data.get("actions") or []}
        except Exception:
            errtail = (err or b"").decode("utf-8", "replace")[-300:]
            self.last_errors[m.name] = errtail or "bad json output"
            return {"result": f"插件 {m.name} 输出异常"}

    async def run_active(self, engine_state: dict) -> list[dict]:
        """运行所有 active 插件, 收集 actions(条件触发由插件自己判断)。"""
        actions_all: list[dict] = []
        for name, m in list(self.manifests.items()):
            if m.type != "active" or not self.enabled.get(name, False):
                continue
            every = int(m.active.get("every_minutes", 30))
            last = self.last_invoked.get(f"{name}:active", 0)
            if time.time() - last < every * 60:
                continue
            self.last_invoked[f"{name}:active"] = time.time()
            payload = {"op": "active", "params": {}, "context": {"engine_state": engine_state}}
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(self.plugins_dir / m.name / m.entry),
                    cwd=str(self.plugins_dir / m.name),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, env=self._sandbox_env(m))
                out, _ = await asyncio.wait_for(proc.communicate(
                    input=json.dumps(payload, ensure_ascii=False).encode()), timeout=30)
                data = json.loads(out.decode("utf-8"))
                if data.get("condition_met") and data.get("broadcast_text"):
                    actions_all.append({"type": "broadcast", "text": data["broadcast_text"]})
                for a in data.get("actions") or []:
                    actions_all.append(a)
            except Exception as e:
                self.last_errors[name] = repr(e)
        return actions_all

    def status(self) -> list[dict]:
        out = []
        for name, m in self.manifests.items():
            out.append({
                "name": name, "version": m.version, "type": m.type,
                "description": m.description, "enabled": self.enabled.get(name, True),
                "tool": m.tool.get("function") if m.tool else None,
                "permissions": m.permissions,
                "last_invoked_age_s": (time.time() - self.last_invoked[name]) if name in self.last_invoked else None,
                "last_error": self.last_errors.get(name),
            })
        return out
