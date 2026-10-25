"""第三方程序管理器:一键拉取/启停/更新/一键接入 + 健康探测(2026-10-25 第 10 轮)。

代管深度由 config/programs.yaml 的 kind + source/docker 字段决定:
- docker(hindsight): install=compose pull/docker pull, start=compose up -d 或
  docker run(参数对齐 deploy/hindsight/docker-compose.yml), stop/update 同理;
  本机无 docker 时返回 ok=False + hint 明确指引与降级建议, 不抛错;
- binary + source.github_release(snowluma): install=下载真实 release 资产并解压
  (平台自动选包: asset_pattern_{linux/darwin}/asset_pattern), start=spawn 解出的
  launcher, update=check-latest→停旧→装新(原先在跑则装完自动拉起);
- external(napcat): 用户自行安装启动, 只做健康探测与状态播报;
- internal(obsidian_sync): 进程内 asyncio 任务, start/stop 控制其同步循环,
  manual_sync=true 时支持「立即同步」动作(sync)。

运行规则:
- run.command 存在 → 由本管理器 spawn 子进程, 日志落 state/programs/logs/<id>.log;
- run.command 为空 → 外部自管, 健康由 run.health 探测(http / ws_event / internal)。

长动作异步化: install/update 由 API 层经 run_action_async() 转后台任务执行——
程序状态置 busy(防重复触发), 完成后状态落回 installed/running 并发 program.status
事件; 调用方立即拿到 {queued: true} 响应, 经 GET /api/v1/programs 轮询进度。

监控: monitor 循环每 N 秒刷新进程状态, 状态变化时向 bus 发 program.status 事件(WS 推送)。
"""
from __future__ import annotations

import asyncio
import json
import re
import os
import shutil
import sys
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
        self._busy: dict[str, str] = {}            # pid -> 执行中的长动作名(install/update)
        self._action_tasks: dict[str, asyncio.Task] = {}
        self._compose: list[str] | None = None     # 缓存 compose 命令(['docker','compose'] / ['docker-compose'] / [] 不可用)
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
            # 上次运行崩在长动作中 → busy 状态是脏数据, 归位
            if st.get("busy_action"):
                st["busy_action"] = None
                st["status"] = ("installed" if (st.get("install_dir") or st.get("version")
                                               or st.get("container")) else "not_installed")
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
                                             follow_redirects=True,   # release 资产 302 到 CDN
                                             headers={"User-Agent": "ME-Agent-ProgramManager"})
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
        await self.stop_monitor()

    # ---------- GitHub release ----------
    async def _latest_release(self, pid: str) -> dict:
        d = self.def_(pid) or {}
        src = d.get("source") or {}
        repo = src.get("repo", "")
        if not repo:
            return {"ok": False, "error": "no repo"}
        headers = {}
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"   # 提高未认证 60 次/小时的 API 限额
        url = f"{GITHUB_API}/repos/{repo}/releases/latest"
        resp = await self._client_ensure().get(url, headers=headers)
        if resp.status_code in (403, 429):
            return {"ok": False, "error": f"HTTP {resp.status_code}: GitHub API 限流",
                    "hint": "未认证限额 60 次/小时, 稍后再试; 或在 .env 配置 GITHUB_TOKEN"}
        if resp.status_code == 404:
            return {"ok": False, "error": f"HTTP 404: {repo} 无 release 或仓库不存在"}
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:150]}"}
        data = resp.json()
        ver = (data.get("tag_name") or "unknown").lstrip("vV")
        # 平台感知的资产选择: asset_pattern_{sys.platform}(win32/linux/darwin) → asset_pattern 兜底
        pat_src = src.get(f"asset_pattern_{sys.platform}") or src.get("asset_pattern") \
            or r"\.(zip|tar\.gz|tgz)$"
        pat = re.compile(pat_src)
        assets, all_names = [], []
        for a in data.get("assets") or []:
            all_names.append(a.get("name") or "")
            if pat.search(a.get("name") or ""):
                assets.append({"name": a["name"], "url": a["browser_download_url"],
                               "size": a.get("size", 0)})
        if not assets:
            return {"ok": False, "error": f"release {ver} 里没有匹配资产(pattern: {pat_src})",
                    "assets_found": all_names,
                    "hint": "检查 config/programs.yaml 的 asset_pattern 与上方实际资产列表"}
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
        """平台感知地找启动入口, 供 binary 形态使用。
        Windows: launcher.bat/launcher.exe/run.bat/*.exe;
        POSIX:   launcher.sh/run.sh/*.sh(经 bash 执行), 无脚本时保留 .exe 便于诊断。"""
        if sys.platform == "win32":
            for pat in ("launcher.bat", "launcher.exe", "run.bat", "run.cmd", "*.exe"):
                cands = sorted(install_dir.rglob(pat))
                if cands:
                    return [str(cands[0])]
            sh = sorted(install_dir.rglob("*.sh"))
            if sh:
                return ["bash", str(sh[0])]
            return None
        sh = (sorted(install_dir.rglob("launcher.sh")) + sorted(install_dir.rglob("run.sh"))
              + sorted(install_dir.rglob("*.sh")))
        if sh:
            return ["bash", str(sh[0])]
        exes = sorted(install_dir.rglob("*.exe")) + sorted(install_dir.rglob("*.bat"))
        if exes:
            return [str(exes[0])]   # POSIX 上无法直接执行, start 会给出明确错误
        return None

    def _ensure_exec_bits(self, install_dir: Path) -> None:
        """POSIX: zip 解压不带执行位; tar data filter 也可能收窄——统一补齐脚本/入口。"""
        if os.name != "posix":
            return
        try:
            for f in install_dir.rglob("*"):
                if f.is_file() and (f.suffix == ".sh" or f.name in ("node",)):
                    f.chmod(f.stat().st_mode | 0o111)
        except Exception:
            pass

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
                out = {"ok": False, "error": rel.get("error") or "release 里没有匹配资产"}
                for extra in ("hint", "assets_found"):
                    if rel.get(extra):
                        out[extra] = rel[extra]
                return out
            asset = rel["assets"][0]
            archive = self.dl_dir / f"{pid}-{rel['version']}-{asset['name']}"
            try:
                if not (archive.exists() and archive.stat().st_size == asset.get("size", -1)):
                    await self._download(asset["url"], archive)   # 已有同版本同大小缓存则复用
                n = self._extract(archive, self.opt_dir / pid / rel["version"])
            except Exception as e:
                return {"ok": False, "error": f"下载/解压失败: {type(e).__name__}: {e}"}
            if n == 0:
                return {"ok": False, "error": f"解压后无文件(archive: {asset['name']})"}
            install_dir = self.opt_dir / pid / rel["version"]
            self._ensure_exec_bits(install_dir)
            st["version"] = rel["version"]
            st["install_dir"] = str(install_dir)
            st["latest"] = rel["version"]
            # 每次安装(含更新)都按新目录重探启动入口, 避免指向旧版本目录
            if kind == "binary":
                cmd = self._guess_command(install_dir, pid)
                st["command"] = cmd          # 无入口时置 None, start 给出明确错误
                if not cmd:
                    self._log(pid, f"[install] {rel['version']} 解压完成但未找到启动入口(launcher/exe/sh)")
            if st.get("status") in ("not_installed", "installed", "stopped", "busy"):
                st["status"] = "installed"
            self._save_state()
            self._log(pid, f"[install] {asset['name']} → {st['install_dir']}")
            return {"ok": True, "version": rel["version"], "files": n,
                    "install_dir": st["install_dir"], "asset": asset["name"]}
        if kind == "docker":
            dd = d.get("docker") or {}
            if not await self._docker_available():
                return {"ok": False, "error": "本机未检测到 Docker(docker 命令缺失或 daemon 未运行)",
                        **self._docker_hint(pid)}
            cc = await self._compose_cmd()
            cf = self._compose_file(dd)
            if cc and cf:
                code, out = await self._run_cmd(cc + ["-f", str(cf), "pull"], timeout_s=900)
                how = f"{' '.join(cc)} -f {cf} pull"
            else:
                code, out = await self._run_cmd(["docker", "pull", dd.get("image", "")], timeout_s=900)
                how = f"docker pull {dd.get('image', '')}"
            if code == 0:
                st["version"] = (dd.get("image", "").rsplit(":", 1)[-1] or "unknown")
                if st.get("status") in ("not_installed", "installed", "stopped", "busy"):
                    st["status"] = "installed"
                self._save_state()
            self._log(pid, f"[install] {how} → {'ok' if code == 0 else 'fail: ' + out[-120:]}")
            return {"ok": code == 0, "detail": out[-300:], "how": how}
        return {"ok": False, "error": "manual 形态: 请按指引手动安装"}

    def _docker_hint(self, pid: str) -> dict:
        """无 docker 时的明确指引与降级建议(而非报错崩)。"""
        return {"hint": "三条路任选: ①安装 Docker Desktop(https://www.docker.com/products/docker-desktop/) "
                        "后重试一键拉取/启动; ②在装有 docker 的机器上执行 "
                        "deploy/hindsight/docker-compose.yml 的编排, 再把 config/programs.yaml "
                        "里 health.url 指向该机地址(远程外部自管); ③改回 kind: external 纯健康探测口径。"}

    async def update(self, pid: str) -> dict:
        d = self.def_(pid)
        if not d:
            return {"ok": False, "error": "unknown program"}
        st = self._st(pid)
        src = d.get("source") or {}
        if src.get("type") == "github_release":
            if not st.get("version"):
                # 从未安装: update 直接走安装
                res = await self.install(pid)
                res.setdefault("note", "此前未安装, 已按最新版直接安装")
                return res
            chk = await self.check_latest(pid)
            if not chk.get("ok"):
                out = {"ok": False, "error": chk.get("error")}
                if chk.get("hint"):
                    out["hint"] = chk["hint"]
                return out
            if not chk.get("has_update"):
                return {"ok": True, "up_to_date": True, "version": chk["version"]}
            was_running = st.get("status") == "running"
            if was_running:
                await self.stop(pid)          # 停旧
            res = await self.install(pid)     # 装新
            if res.get("ok") and was_running:
                res["restarted"] = True
                res["start_result"] = await self.start(pid)
            if res.get("ok"):
                self._log(pid, f"[update] → {chk['version']} 完成" +
                         ("(已自动重启)" if was_running else ""))
                await self._notify(pid)
            return res
        if d.get("kind") == "docker":
            # docker 形态: pull 新镜像 + (容器在跑则)重建容器
            if not await self._docker_available():
                return {"ok": False, "error": "本机未检测到 Docker", **self._docker_hint(pid)}
            was_running = st.get("status") == "running"
            res = await self.install(pid)     # compose pull / docker pull
            if res.get("ok") and was_running:
                res["restarted"] = True
                res["start_result"] = await self.start(pid)   # compose up -d 会按新镜像重建
            if res.get("ok"):
                self._log(pid, "[update] 镜像已更新" + ("(容器已重建)" if was_running else ""))
                await self._notify(pid)
            return res
        return {"ok": False, "error": "该程序形态不支持更新(external/external自管请手动升级)"}

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
            if not await self._docker_available():
                return {"ok": False, "error": "本机未检测到 Docker(docker 命令缺失或 daemon 未运行)",
                        **self._docker_hint(pid)}
            dd = d.get("docker") or {}
            name = dd.get("container_name", f"me-agent-{pid}")
            cc = await self._compose_cmd()
            cf = self._compose_file(dd)
            if cc and cf:
                # compose 优先(与 deploy/hindsight/docker-compose.yml 对齐, 含 webhook/卷/健康检查)
                args = cc + ["-f", str(cf), "up", "-d"]
                how = f"{' '.join(cc)} -f {cf} up -d"
            else:
                code, _ = await self._run_cmd(["docker", "inspect", name], timeout_s=15)
                if code == 0:
                    args = ["docker", "start", name]
                    how = f"docker start {name}"
                else:
                    args = (["docker", "run", "-d", "--name", name,
                             "--restart", str(dd.get("restart", "unless-stopped"))] +
                            sum([["-p", p] for p in dd.get("ports", [])], []) +
                            sum([["-v", v] for v in dd.get("volumes", [])], []) +
                            sum([["-e", f"{k}={self._env_expand(v)}"] for k, v in (dd.get("env") or {}).items()], []) +
                            sum([["--add-host", h] for h in dd.get("extra_hosts", [])], []) +
                            (["--memory", str(dd["mem_limit"])] if dd.get("mem_limit") else []) +
                            [dd.get("image", "")])
                    how = f"docker run -d {name}({dd.get('image', '')}, 参数对齐 compose)"
            code, out = await self._run_cmd(args, timeout_s=180)
            if code == 0:
                st.update(status="running", container=name, pid=None, started_at=time.time())
                self._save_state()
                self._log(pid, f"[start] {how}")
                await self._notify(pid)
            else:
                self._log(pid, f"[start] 失败: {out[-200:]}")
            return {"ok": code == 0, "detail": out[-300:], "how": how}
        cmd = st.get("command") or (d.get("run") or {}).get("command")
        if not cmd:
            return {"ok": False, "error": "未配置启动命令(请先 install 探测 launcher, 或在 run.command 填写)"}
        if pid in self._procs and self._procs[pid].returncode is None:
            return {"ok": True, "note": "已在运行"}
        logf = self.logs_dir / f"{pid}.log"
        logfh = logf.open("ab")
        # cwd 优先取安装目录(launcher 依赖相对路径调用同目录 node), 兼容旧命令形态
        cwd = None
        if st.get("install_dir") and Path(st["install_dir"]).is_dir():
            cwd = st["install_dir"]
        elif len(cmd) > 1 and Path(str(cmd[1])).exists():
            cwd = str(Path(cmd[1]).parent)
        proc = await asyncio.create_subprocess_exec(
            *[str(c) for c in cmd], stdout=logfh, stderr=asyncio.subprocess.STDOUT, cwd=cwd)
        self._procs[pid] = proc
        st.update(status="running", pid=proc.pid, started_at=time.time())
        self._save_state()
        self._log(pid, f"[start] pid={proc.pid} cmd={cmd} cwd={cwd}")
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
            if not await self._docker_available():
                return {"ok": False, "error": "本机未检测到 Docker", **self._docker_hint(pid)}
            dd = d.get("docker") or {}
            name = st.get("container") or dd.get("container_name", f"me-agent-{pid}")
            cc = await self._compose_cmd()
            cf = self._compose_file(dd)
            if cc and cf:
                args, how = cc + ["-f", str(cf), "stop"], f"{' '.join(cc)} -f {cf} stop"
            else:
                args, how = ["docker", "stop", name], f"docker stop {name}"
            code, out = await self._run_cmd(args, timeout_s=60)
            if code == 0:
                st.update(status="stopped", pid=None)
                self._save_state()
                self._log(pid, f"[stop] {how}")
                await self._notify(pid)
            return {"ok": code == 0, "detail": out[-300:], "how": how}
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

    # ---------- 动作调度(busy 防重 + 长动作后台化) ----------
    LONG_ACTIONS = ("install", "update")   # 下载/拉镜像类: 经 run_action_async 后台执行

    async def run_action_async(self, pid: str, action: str) -> dict:
        """长动作入口: 立即返回 queued, 状态置 busy 防重复触发, 完成后经 program.status 播报。"""
        if not self.def_(pid):
            return {"ok": False, "error": "unknown program"}
        if pid in self._busy:
            return {"ok": False, "busy": True,
                    "error": f"该程序正在执行 {self._busy[pid]}，请稍候(轮询 GET /api/v1/programs 观察状态)"}
        st = self._st(pid)
        self._busy[pid] = action
        st["status"] = "busy"
        st["busy_action"] = action
        self._save_state()
        self._log(pid, f"[{action}] 已开始(后台执行)")
        await self._notify(pid)
        self._action_tasks[pid] = asyncio.create_task(self._action_worker(pid, action))
        return {"ok": True, "queued": True, "action": action, "status": "busy",
                "note": "后台执行中: 轮询 GET /api/v1/programs(状态 busy→installed/running)或看日志接口"}

    async def _action_worker(self, pid: str, action: str) -> None:
        st = self._st(pid)
        res: dict = {}
        try:
            res = await getattr(self, action)(pid)
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self._log(pid, f"[{action}] 异常: {res['error']}")
        try:
            if res.get("ok"):
                summary = {k: res[k] for k in ("version", "up_to_date", "asset", "how",
                                               "restarted", "files", "note") if k in res}
                self._log(pid, f"[{action}] 完成: {json.dumps(summary, ensure_ascii=False, default=str)}")
            st["last_action"] = {"action": action, "ok": bool(res.get("ok")),
                                 "at": time.time(),
                                 "result": {k: v for k, v in res.items()
                                            if k in ("ok", "version", "up_to_date", "error",
                                                     "hint", "asset", "how", "restarted")}}
        finally:
            self._busy.pop(pid, None)
            self._action_tasks.pop(pid, None)
            st["busy_action"] = None
            if st.get("status") == "busy":   # 失败/异常时终态归位(成功路径 install/update 内部已写终态)
                st["status"] = ("installed" if (st.get("install_dir") or st.get("version")
                                               or st.get("container")) else "stopped")
            self._save_state()
            await self._notify(pid)

    async def guarded_action(self, pid: str, action: str) -> dict:
        """短动作(start/stop/restart/integrate/sync)统一入口: busy 期间拒绝。"""
        if pid in self._busy:
            return {"ok": False, "busy": True,
                    "error": f"该程序正在执行 {self._busy[pid]}，请稍候再试"}
        return await getattr(self, action)(pid)

    async def sync(self, pid: str) -> dict:
        """manual_sync 程序的「立即同步」(当前= obsidian_sync)。"""
        d = self.def_(pid) or {}
        if not d.get("manual_sync") or self.obsidian is None:
            return {"ok": False, "error": "该程序不支持手动同步"}
        return await self.obsidian.sync_now()

    def log_program(self, pid: str, line: str) -> None:
        """供其他组件(ObsidianChannel)写入程序日志。"""
        if pid in self._def_by_id:
            self._log(pid, line)

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
                running = bool(s.get("running"))
                ls = s.get("last_sync_at")
                lr = s.get("last_result") or {}
                if not ls:
                    detail = "尚未同步过"
                else:
                    detail = (f"上次同步 {_fmt_ts(ls)}: 变更{len(lr.get('changed') or [])}"
                              f"/推送{len(lr.get('pushed') or [])}")
                err = (lr.get("error") or s.get("last_error"))
                if err:
                    detail += f"; 失败: {str(err)[:120]}"
                streak = int(s.get("error_streak") or 0)
                if streak:
                    detail += f"(连续失败 {streak} 次)"
                return {"type": "internal", "running": running,
                        "status": "running" if running else "stopped",
                        "detail": detail, "last_sync_at": ls,
                        "last_result": lr, "error_streak": streak}
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
            if (hstat == "running" and base in ("installed", "error", "stopped")
                    and st.get("status") != "busy"):   # busy 期间不被健康探测覆盖
                st["status"] = "running"
                self._save_state()
            if d.get("kind") == "external":
                # 外部自管: 状态即健康探测结果, 不存在「未安装」生命周期位
                status = hstat if hstat in ("running", "stopped") else "stopped"
            if st.get("busy_action"):
                status = "busy"
            out.append({
                "id": pid,
                "name": d.get("name", pid),
                "tag": d.get("tag", d.get("kind", "")),
                "desc": d.get("description", ""),
                "kind": d.get("kind", ""),
                "status": status,
                "busy_action": st.get("busy_action"),
                "last_action": st.get("last_action"),
                "version": st.get("version") or "-",
                "latest": st.get("latest") or "-",
                "proc": self._proc_info(pid),
                "actions": self._actions(d, st, status),
                "color": COLOR.get(pid, "var(--blue)"),
                "logo": LOGO.get(pid, pid[:1].upper()),
                "logs": self.logs(pid, 6),
                "integrate": d.get("integrate", ""),
                "install_hint": d.get("install_hint", ""),
                "health": h,
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
        if status == "busy":
            return ["log"]              # 执行中: 禁用动作按钮(防重复触发)
        if d.get("source") or d.get("docker"):
            acts.append("install")      # 拉取(release 资产 / 镜像)
            if d.get("source"):
                acts.append("update")
            elif d.get("docker"):
                acts.append("update")   # docker 形态: pull + 重建
            acts.append("check-update")  # 仅作 UI 提示: 前端渲染「检查更新」按钮, 实调 GET /{pid}/check-latest
        acts.append("stop" if status == "running" else "start")
        acts.append("log")
        if d.get("manual_sync"):
            acts.append("sync")         # 立即同步(obsidian_sync)
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
                    if (h.get("type") == "http" and st.get("status") != "not_installed"
                            and not st.get("busy_action")):   # busy(拉镜像等)期间不做健康探测
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
        if pid == "obsidian_sync" and self.obsidian is not None:
            self.obsidian.stop_loop()      # 先置 _running=False, status() 即刻反映
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
        code, _ = await self._run_cmd(["docker", "info", "--format", "{{.ServerVersion}}"], timeout_s=15)
        return code == 0

    async def _compose_cmd(self) -> list[str]:
        """compose 命令探测(缓存): docker compose v2 → docker-compose v1 → [] 不可用。"""
        if self._compose is not None:
            return self._compose
        if not shutil.which("docker"):
            self._compose = []
            return self._compose
        for cand in (["docker", "compose"], ["docker-compose"]):
            code, _ = await self._run_cmd(cand + ["version"], timeout_s=15)
            if code == 0:
                self._compose = cand
                return self._compose
        self._compose = []
        return self._compose

    def _compose_file(self, dd: dict) -> Path | None:
        """yaml 里 docker.compose_file(相对仓库根 self.settings.root)存在则返回路径。"""
        raw = dd.get("compose_file")
        if not raw:
            return None
        p = Path(raw)
        if not p.is_absolute():
            repo_root = getattr(self.settings, "root", None)
            if repo_root is not None:
                p = Path(repo_root) / p
        return p if p.exists() else None

    @staticmethod
    def _env_expand(v) -> str:
        """docker env 值里的 ${VAR} 用进程环境展开(.env 已在 config 装载时注入 os.environ)。"""
        s = str(v)
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), s)

    async def _run_cmd(self, args: list[str], timeout_s: float = 60.0) -> tuple[int, str]:
        try:
            p = await asyncio.create_subprocess_exec(
                *[str(a) for a in args],
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                out = (await asyncio.wait_for(p.communicate(), timeout=timeout_s))[0] \
                    .decode("utf-8", "replace")
                return p.returncode or 0, out
            except asyncio.TimeoutError:
                try:
                    p.kill()
                except Exception:
                    pass
                return 124, f"超时(>{timeout_s}s): {' '.join(str(a) for a in args)}"
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


def _fmt_ts(epoch: float | None) -> str:
    if not epoch:
        return "-"
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(epoch)))
    except Exception:
        return "-"


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
