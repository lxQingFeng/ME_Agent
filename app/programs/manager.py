"""第三方程序管理器:健康探测/状态监控/一键接入为主(2026-10-25 收缩)。

部署哲学(参照 K0nd1us/QQ-agent 0.5 的回撤结论):协议端与外部程序「归用户,
app 只管应用层」——主路径定位「外部自管 + 健康探测」:
- external(主路径): 用户侧自行安装启动, 本管理器只做健康探测(http/ws_event)
  与状态播报, 不提供启停/安装动作;
- internal: 进程内服务(Obsidian 同步器), start/stop 控制其 asyncio 任务;
- binary/manual + run.command: 少数自管进程可 spawn(保留);
- github_release 下载安装与 docker spawn 降级为「可选增强」: 配置里保留
  source/docker 字段才启用, 默认注册表不再依赖(见 config/programs.yaml 注释)。

运行规则:
- run.command 存在 → 由本管理器 spawn 子进程, 日志落 state/programs/logs/<id>.log;
- run.command 为空 → 外部自管, 健康由 run.health 探测(http / ws_event / internal)。

监控: monitor 循环每 N 秒刷新进程状态, 状态变化时向 bus 发 program.status 事件(WS 推送)。
"""
from __future__ import annotations

import asyncio
import json
import re
import os
import shutil
import tarfile
import time
import zipfile
from pathlib import Path

import httpx

GITHUB_API = "https://api.github.com"

LOGO = {"hindsight": "H", "snowluma": "S", "napcat": "N", "obsidian_sync": "O"}
COLOR = {"hindsight": "var(--purple)", "snowluma": "var(--green)",
         "napcat": "var(--blue)", "obsidian_sync": "var(--pink)"}


class ProgramManager:
    def __init__(self, settings, bus=None, memory=None, obsidian=None,
                 gateway=None, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.bus = bus
        self.memory = memory            # MemoryService(一键建 bank)
        self.obsidian = obsidian        # ObsidianChannel(internal 形态启停)
        self.gateway = gateway          # OneBotGateway(ws_event 健康探测)
        self.defs: list[dict] = list((settings.programs_cfg or {}).get('programs') or [])
        self._def_by_id = {d["id"]: d for d in self.defs}
        self.root: Path = settings.state_dir / "programs"
        self.opt_dir = self.root / "opt"
        self.logs_dir = self.root / "logs"
        self.dl_dir = self.root / "downloads"
        for d in (self.opt_dir, self.logs_dir, self.dl_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._state_file = self.root / "state.json"
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._cpu_prev: dict[str, tuple[float, float, float]] = {}   # pid -> (utime, stime, ts)
        self._monitor_task: asyncio.Task | None = None
        self._internal_tasks: dict[str, asyncio.Task] = {}
        self._log_buffers: dict[str, list[str]] = {}
        self._health_cache: dict[str, dict] = {}
        self._load_state()

    # ---------- 基础 ----------
    def _load_state(self) -> None:
        self.state = {"programs": {}}
        if self._state_file.exists():
            try:
                self.state = json.loads(self._state_file.read_text("utf-8"))
            except Exception:
                pass
        # 补齐 + 探活: 持久化 pid 失效则归为 stopped
        for d in self.defs:
            st = self.state["programs"].setdefault(d["id"], {})
            st.setdefault("status", "not_installed")
            st.setdefault("pid", None)
            st.setdefault("started_at", None)
            st.setdefault("restarts", 0)
            st.setdefault("version", None)
            st.setdefault("command", None)
            pid = st.get("pid")
            if pid and not _pid_alive(pid):
                if st["status"] == "running":
                    st["status"] = "stopped"
                st["pid"] = None
        self._save_state()

    def _save_state(self) -> None:
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self._state_file)

    def _st(self, pid: str) -> dict:
        return self.state["programs"].setdefault(pid, {})

    def def_(self, pid: str) -> dict | None:
        return self._def_by_id.get(pid)

    def _client_ensure(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0, transport=self._transport,
                                             headers={"User-Agent": "ME-Agent-ProgramManager"})
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
        await self.stop_monitor()

    # ---------- GitHub release ----------
    async def _latest_release(self, pid: str) -> dict:
        d = self.def_(pid) or {}
        repo = (d.get("source") or {}).get("repo", "")
        if not repo:
            return {"ok": False, "error": "no repo"}
        url = f"{GITHUB_API}/repos/{repo}/releases/latest"
        resp = await self._client_ensure().get(url)
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:150]}"}
        data = resp.json()
        ver = (data.get("tag_name") or "unknown").lstrip("vV")
        assets = []
        pat = re.compile((d.get("source") or {}).get("asset_pattern", r"\.(zip|tar\.gz|tgz)$"))
        for a in data.get("assets") or []:
            if pat.search(a.get("name") or ""):
                assets.append({"name": a["name"], "url": a["browser_download_url"],
                               "size": a.get("size", 0)})
        return {"ok": True, "version": ver, "tag": data.get("tag_name"),
                "notes": (data.get("body") or "")[:400], "assets": assets}

    async def check_latest(self, pid: str) -> dict:
        rel = await self._latest_release(pid)
        st = self._st(pid)
        cur = st.get("version")
        if rel.get("ok"):
            rel["current_version"] = cur
            rel["has_update"] = bool(cur) and _vkey(rel["version"]) > _vkey(cur)
            st["latest"] = rel["version"]
            self._save_state()
        return rel

    async def _download(self, url: str, dest: Path) -> None:
        async with self._client_ensure().stream("GET", url) as resp:
            resp.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes(65536):
                    f.write(chunk)

    def _extract(self, archive: Path, target: Path) -> int:
        target.mkdir(parents=True, exist_ok=True)
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as z:
                z.extractall(target)
                return len(z.namelist())
        if tarfile.is_tarfile(archive):
            with tarfile.open(archive) as t:
                t.extractall(target, filter="data")
                return len(t.getnames())
        return 0

    def _guess_command(self, install_dir: Path, pid: str) -> list[str] | None:
        """启发式找启动入口(launcher.bat / *.exe / run.sh), 供 binary 形态使用。"""
        cands = sorted(install_dir.rglob("launcher.bat")) + \
                sorted(install_dir.rglob("run.bat")) + \
                sorted(install_dir.rglob("*.exe"))
        if not cands:
            sh = sorted(install_dir.rglob("run.sh")) + sorted(install_dir.rglob("*.sh"))
            if sh:
                return ["bash", str(sh[0])]
            return None
        return [str(cands[0])]

    async def install(self, pid: str) -> dict:
        d = self.def_(pid)
        if not d:
            return {"ok": False, "error": "unknown program"}
        st = self._st(pid)
        kind = d.get("kind", "binary")
        if kind == "external":
            # 主路径: 外部自管, 不代管安装(可选增强未配置 source/docker 时到不了别处)
            return {"ok": False, "external": True,
                    "error": "外部自管程序不由本管理器安装",
                    "hint": d.get("install_hint", "请参照项目 homepage 手动安装")}
        src = d.get("source") or {}
        if src.get("type") == "github_release":
            rel = await self._latest_release(pid)
            if not rel.get("ok") or not rel.get("assets"):
                return {"ok": False, "error": rel.get("error") or "release 里没有匹配资产"}
            asset = rel["assets"][0]
            archive = self.dl_dir / f"{pid}-{rel['version']}-{asset['name']}"
            try:
                await self._download(asset["url"], archive)
                n = self._extract(archive, self.opt_dir / pid / rel["version"])
            except Exception as e:
                return {"ok": False, "error": f"下载/解压失败: {type(e).__name__}: {e}"}
            st["version"] = rel["version"]
            st["install_dir"] = str(self.opt_dir / pid / rel["version"])
            if kind == "binary" and not st.get("command"):
                cmd = self._guess_command(Path(st["install_dir"]), pid)
                if cmd:
                    st["command"] = cmd
            if st.get("status") in ("not_installed", "installed"):
                st["status"] = "installed"
            self._save_state()
            return {"ok": True, "version": rel["version"], "files": n,
                    "install_dir": st["install_dir"], "asset": asset["name"]}
        if kind == "docker":
            docker = await self._docker_available()
            if not docker:
                return {"ok": False, "error": "本机无 docker, 请走手动安装(见指引)"}
            code, out = await self._run_cmd(["docker", "pull", d["docker"]["image"]])
            return {"ok": code == 0, "detail": out[-300:]}
        return {"ok": False, "error": "manual 形态: 请按指引手动安装"}

    async def update(self, pid: str) -> dict:
        chk = await self.check_latest(pid)
        if not chk.get("ok"):
            return {"ok": False, "error": chk.get("error")}
        st = self._st(pid)
        if st.get("status") == "running":
            await self.stop(pid)
        res = await self.install(pid)
        if res.get("ok") and (d := self.def_(pid)) and d.get("kind") == "docker":
            code, out = await self._run_cmd(["docker", "pull", d["docker"]["image"]])
            res = {"ok": code == 0, "detail": out[-300:], "version": chk.get("version")}
        if res.get("ok"):
            self._log(pid, f"[update] → {chk.get('version')} 完成")
            await self._notify(pid)
        return res

    # ---------- 启停 ----------
    async def start(self, pid: str) -> dict:
        d = self.def_(pid)
        if not d:
            return {"ok": False, "error": "unknown program"}
        st = self._st(pid)
        kind = d.get("kind", "binary")
        if kind == "external":
            return {"ok": False, "external": True,
                    "error": "外部自管程序不由本管理器启动",
                    "hint": d.get("install_hint", "请手动启动; 启动后本管理器会通过 run.health 自动探测")}
        if kind == "internal":
            return self._internal_start(pid)
        if kind == "docker":
            docker = await self._docker_available()
            if not docker:
                return {"ok": False, "error": "本机无 docker"}
            dd = d.get("docker", {})
            name = dd.get("container_name", f"me-agent-{pid}")
            code, _ = await self._run_cmd(["docker", "inspect", name])
            args = (["docker", "start", name] if code == 0 else
                    ["docker", "run", "-d", "--name", name] +
                    sum([["-p", p] for p in dd.get("ports", [])], []) +
                    sum([["-v", v] for v in dd.get("volumes", [])], []) +
                    [dd.get("image", "")])
            code, out = await self._run_cmd(args)
            if code == 0:
                st.update(status="running", container=name, started_at=time.time())
                self._save_state()
                self._log(pid, f"[start] container {name} 已启动")
                await self._notify(pid)
            return {"ok": code == 0, "detail": out[-300:]}
        cmd = st.get("command") or (d.get("run") or {}).get("command")
        if not cmd:
            return {"ok": False, "error": "未配置启动命令(请先 install 或在 run.command 填写)"}
        if pid in self._procs and self._procs[pid].returncode is None:
            return {"ok": True, "note": "已在运行"}
        logf = self.logs_dir / f"{pid}.log"
        logfh = logf.open("ab")
        proc = await asyncio.create_subprocess_exec(
            *[str(c) for c in cmd], stdout=logfh, stderr=asyncio.subprocess.STDOUT,
            cwd=str(Path(cmd[1]).parent) if len(cmd) > 1 and Path(str(cmd[1])).exists() else None)
        self._procs[pid] = proc
        st.update(status="running", pid=proc.pid, started_at=time.time())
        self._save_state()
        self._log(pid, f"[start] pid={proc.pid} cmd={cmd}")
        await self._notify(pid)
        return {"ok": True, "pid": proc.pid}

    async def stop(self, pid: str) -> dict:
        d = self.def_(pid)
        st = self._st(pid)
        if not d:
            return {"ok": False, "error": "unknown program"}
        kind = d.get("kind", "binary")
        if kind == "external":
            return {"ok": False, "external": True,
                    "error": "外部自管程序不由本管理器停止",
                    "hint": "请在其自身控制台/服务管理器中停止"}
        if kind == "internal":
            return self._internal_stop(pid)
        if kind == "docker":
            name = st.get("container") or (d.get("docker") or {}).get("container_name", f"me-agent-{pid}")
            code, out = await self._run_cmd(["docker", "stop", name])
            if code == 0:
                st.update(status="stopped", pid=None)
                self._save_state()
                await self._notify(pid)
            return {"ok": code == 0, "detail": out[-300:]}
        proc = self._procs.get(pid)
        if proc is None or proc.returncode is not None:
            st.update(status="stopped", pid=None)
            self._save_state()
            return {"ok": True, "note": "本管理器未持有其进程(外部自管请手动停止)"}
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=8)
        except asyncio.TimeoutError:
            proc.kill()
        st.update(status="stopped", pid=None)
        self._procs.pop(pid, None)
        self._cpu_prev.pop(pid, None)
        self._save_state()
        self._log(pid, "[stop] 已终止")
        await self._notify(pid)
        return {"ok": True}

    async def restart(self, pid: str) -> dict:
        await self.stop(pid)
        await asyncio.sleep(0.3)
        res = await self.start(pid)
        if res.get("ok"):
            self._st(pid)["restarts"] = int(self._st(pid).get("restarts", 0)) + 1
            self._save_state()
        return res

    # ---------- 一键接入 ----------
    async def integrate(self, pid: str) -> dict:
        d = self.def_(pid) or {}
        mode = d.get("integrate", "")
        if mode == "create_banks":
            if self.memory is None:
                return {"ok": False, "error": "记忆服务未初始化"}
            out = await self.memory.ensure_banks()
            self._log(pid, f"[integrate] create_banks: {out}")
            await self._notify(pid)
            return {"ok": True, "mode": "create_banks", "banks": out,
                    "guide": "已确保 qq-agent / knowledge 两个记忆库存在; recall/retain 走 memory.yaml 配置。"}
        if mode == "write_onebot_config":
            ob = self.settings.channels.get("onebot", {})
            gw_mode = self.settings.onebot_mode()
            snippet = {
                "mode": gw_mode,
                "listen_host": ob.get("listen_host", "0.0.0.0"),
                "listen_port": ob.get("listen_port", 8000),
                "ws_path": ob.get("ws_path", "/onebot"),
                "access_token_env": ob.get("access_token_env", "ONEBOT_ACCESS_TOKEN"),
                "reverse_ws_url": f"ws://127.0.0.1:{ob.get('listen_port', 8000)}{ob.get('ws_path', '/onebot')}",
                "forward_ws_url": self.settings.onebot_forward_url(),
            }
            f = self.root / f"onebot-{pid}.yaml"
            import yaml
            f.write_text(yaml.safe_dump({"onebot_connection": snippet}, allow_unicode=True), "utf-8")
            self._log(pid, f"[integrate] 对接参数已写入 {f}")
            await self._notify(pid)
            if gw_mode == "forward":
                guide = (f"当前为 forward 正向外连模式: 在 {d.get('name', pid)} 开启正向 WS 服务后, "
                         f"把地址填到 config/channels.yaml 的 ws_url(当前 {snippet['forward_ws_url']}), "
                         f"并设置相同 access token(环境变量 {snippet['access_token_env']})。")
            else:
                guide = (f"当前为 reverse 反向模式: 在 {d.get('name', pid)} 配置页填反向 WS 地址 "
                         f"{snippet['reverse_ws_url']}, 并设置相同 access token"
                         f"(环境变量 {snippet['access_token_env']}); 连接后 self_qq 会自动学习。")
            return {"ok": True, "mode": "write_onebot_config", "snippet_file": str(f), "guide": guide}
        if mode == "open_vault":
            vd = self.settings.vault_dir
            self._log(pid, f"[integrate] vault 目录: {vd}")
            return {"ok": True, "mode": "open_vault", "vault_dir": str(vd),
                    "guide": f"用 Obsidian 打开 {vd} 即可审计 Agent 记忆(agent-memory/ 投影)。"}
        return {"ok": False, "error": "该程序没有接入动作"}

    # ---------- 状态/健康/指标 ----------
    def _health(self, pid: str) -> dict:
        d = self.def_(pid) or {}
        h = (d.get("run") or {}).get("health") or {}
        st = self._st(pid)
        t = h.get("type", "")
        if t == "http":
            cached = self._health_cache.get(pid) or {}
            return {"type": "http", "status": cached.get("status") or st.get("status", "stopped"),
                    "detail": cached.get("detail", "")}
        if t == "ws_event":
            gw = self.gateway
            if gw is not None:
                s = gw.status()
                ok = s.get("connected") and s.get("heartbeat_ok")
                detail = "onebot WS 在线" if s.get("connected") else "未连接"
                if s.get("mode") == "forward":
                    detail = f"forward 外连({s.get('forward_url', '')})" if s.get("connected") else \
                             f"forward 外连断开{s.get('last_error') or ''}"
                return {"type": "ws_event", "status": "running" if ok else "stopped",
                        "detail": detail}
        if t == "internal":
            if self.obsidian is not None:
                s = self.obsidian.status()
                return {"type": "internal", "status": "running" if s.get("running") else "stopped",
                        "detail": s.get("last_sync", "")}
        proc = self._procs.get(pid)
        if proc is not None and proc.returncode is None:
            return {"type": "process", "status": "running"}
        if st.get("pid") and _pid_alive(st["pid"]):
            return {"type": "external", "status": "running", "detail": f"pid {st['pid']}(外部自管)"}
        return {"type": "none", "status": "stopped"}

    def _proc_info(self, pid: str) -> dict:
        st = self._st(pid)
        proc_pid = st.get("pid")
        info = {"pid": proc_pid, "mem_mb": None, "cpu": None,
                "uptime": None, "restarts": int(st.get("restarts", 0))}
        if not proc_pid or not _pid_alive(proc_pid):
            return info
        try:
            stat = Path(f"/proc/{proc_pid}/stat").read_text("utf-8")
            fields = stat.rsplit(")", 1)[1].split()
            utime, stime = int(fields[11]), int(fields[12])
            started_at = int(fields[19])
            rss = int(Path(f"/proc/{proc_pid}/statm").read_text("utf-8").split()[1]) * 4096
            info["mem_mb"] = round(rss / 1048576, 1)
            if st.get("started_at"):
                up = max(0, int(time.time() - st["started_at"]))
                info["uptime"] = _fmt_uptime(up)
            prev = self._cpu_prev.get(pid)
            now = time.time()
            self._cpu_prev[pid] = (utime, stime, now)
            if prev:
                dt = now - prev[2]
                if dt > 0:
                    info["cpu"] = f"{(utime + stime - prev[0] - prev[1]) / os_ticks() / dt * 100:.1f}%"
        except Exception:
            pass
        return info

    def list(self) -> list[dict]:
        out = []
        for d in self.defs:
            pid = d["id"]
            st = self._st(pid)
            h = self._health(pid)
            hstat = h.get("status")
            base = st.get("status", "not_installed")
            status = hstat if (hstat == "running" and base != "not_installed") else base
            if hstat == "running" and base in ("installed", "error", "stopped"):
                st["status"] = "running"
                self._save_state()
            if d.get("kind") == "external":
                # 外部自管: 状态即健康探测结果, 不存在「未安装」生命周期位
                status = hstat if hstat in ("running", "stopped") else "stopped"
            out.append({
                "id": pid,
                "name": d.get("name", pid),
                "tag": d.get("tag", d.get("kind", "")),
                "desc": d.get("description", ""),
                "kind": d.get("kind", ""),
                "status": status,
                "version": st.get("version") or "-",
                "latest": st.get("latest") or "-",
                "proc": self._proc_info(pid),
                "actions": self._actions(d, st, status),
                "color": COLOR.get(pid, "var(--blue)"),
                "logo": LOGO.get(pid, pid[:1].upper()),
                "logs": self.logs(pid, 6),
                "integrate": d.get("integrate", ""),
            })
        return out

    def _actions(self, d: dict, st: dict, status: str) -> list[str]:
        acts: list[str] = []
        if d.get("kind") == "external":
            # 外部自管: 只保留日志与一键接入, 不代管安装/启停
            acts.append("log")
            if d.get("integrate"):
                acts.append("integrate")
            return acts
        if d.get("source"):
            acts.append("update")
        acts.append("stop" if status == "running" else "start")
        acts.append("log")
        if d.get("integrate"):
            acts.append("integrate")
        return acts

    def logs(self, pid: str, limit: int = 80) -> list[str]:
        bufs = self._log_buffers.setdefault(pid, [])
        f = self.logs_dir / f"{pid}.log"
        if f.exists():
            try:
                lines = f.read_text("utf-8", errors="replace").splitlines()[-limit:]
            except Exception:
                lines = []
            return lines
        return bufs[-limit:]

    def _log(self, pid: str, line: str) -> None:
        ts = time.strftime("%H:%M:%S")
        f = self.logs_dir / f"{pid}.log"
        with f.open("a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] {line}\n")

    async def _notify(self, pid: str) -> None:
        if self.bus:
            st = self._st(pid)
            self.bus.emit("program.status", {"id": pid, "status": st.get("status"),
                                             "pid": st.get("pid")})

    # ---------- 监控循环 ----------
    async def start_monitor(self) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop_monitor(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

    async def _monitor_loop(self) -> None:
        while True:
            try:
                for d in self.defs:
                    pid = d["id"]
                    st = self._st(pid)
                    h = (d.get("run") or {}).get("health") or {}
                    if h.get("type") == "http" and st.get("status") != "not_installed":
                        try:
                            resp = await self._client_ensure().get(h.get("url", ""), timeout=5.0)
                            self._health_cache[pid] = {"status": "running" if resp.status_code < 500 else "error",
                                                       "detail": f"HTTP {resp.status_code}"}
                        except Exception as e:
                            self._health_cache[pid] = {"status": "stopped", "detail": type(e).__name__}
                        if self._health_cache[pid]["status"] != st.get("status"):
                            st["status"] = self._health_cache[pid]["status"]
                            self._save_state()
                            await self._notify(pid)
                    proc = self._procs.get(pid)
                    if proc is not None and proc.returncode is not None and st.get("status") == "running":
                        st["status"] = "stopped"
                        st["pid"] = None
                        st["exit_code"] = proc.returncode
                        self._log(pid, f"[monitor] 进程退出 code={proc.returncode}")
                        self._procs.pop(pid, None)
                        self._save_state()
                        await self._notify(pid)
                    elif st.get("pid") and st.get("status") == "running" and not _pid_alive(st["pid"]):
                        st["status"] = "stopped"
                        st["pid"] = None
                        self._log(pid, "[monitor] 外部进程消失")
                        self._save_state()
                        await self._notify(pid)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(10)

    # ---------- internal 形态 ----------
    async def autostart(self) -> dict:
        """进程启动时恢复: internal 形态且持久化状态为 running 的程序(如 obsidian_sync)。"""
        out = {}
        for d in self.defs:
            pid = d["id"]
            if d.get("kind") == "internal" and self._st(pid).get("status") == "running":
                out[pid] = self._internal_start(pid)
        return out

    def _internal_start(self, pid: str) -> dict:
        if pid == "obsidian_sync" and self.obsidian is not None:
            if pid in self._internal_tasks and not self._internal_tasks[pid].done():
                return {"ok": True, "note": "已在运行"}
            self._internal_tasks[pid] = asyncio.create_task(self._obsidian_loop(pid))
            self._st(pid).update(status="running", started_at=time.time())
            self._save_state()
            self._log(pid, "[start] 内部同步循环已启动")
            return {"ok": True}
        return {"ok": False, "error": "内部服务不可用"}

    async def _obsidian_loop(self, pid: str) -> None:
        try:
            await self.obsidian.run_sync_loop()
        except asyncio.CancelledError:
            pass

    def _internal_stop(self, pid: str) -> dict:
        t = self._internal_tasks.pop(pid, None)
        if t:
            t.cancel()
            self._st(pid).update(status="stopped", pid=None)
            self._save_state()
            self._log(pid, "[stop] 内部同步循环已停止")
            return {"ok": True}
        return {"ok": True, "note": "未在运行"}

    # ---------- docker 助手 ----------
    async def _docker_available(self) -> bool:
        if not shutil.which("docker"):
            return False
        code, _ = await self._run_cmd(["docker", "info", "--format", "{{.ServerVersion}}"])
        return code == 0

    async def _run_cmd(self, args: list[str]) -> tuple[int, str]:
        try:
            p = await asyncio.create_subprocess_exec(
                *[str(a) for a in args],
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out = (await p.communicate())[0].decode("utf-8", "replace")
            return p.returncode or 0, out
        except FileNotFoundError:
            return 127, "command not found"
        except Exception as e:
            return 1, f"{type(e).__name__}: {e}"


# ---------- 工具函数 ----------
def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        return Path(f"/proc/{pid}").exists()
    except Exception:
        return False


def _os_ticks() -> float:
    try:
        import os
        return float(os.sysconf("SC_CLK_TCK"))
    except Exception:
        return 100.0


def _vkey(v: str | None) -> tuple:
    if not v:
        return (0, 0, 0)
    parts = re.findall(r"\d+", str(v))[:3] + ["0", "0", "0"]
    return tuple(int(x) for x in parts[:3])


def _fmt_uptime(s: int) -> str:
    if s < 3600:
        return f"{s // 60}分钟"
    if s < 86400:
        return f"{s // 3600}小时{s % 3600 // 60}分"
    return f"{s // 86400}天{s % 86400 // 3600}小时"
