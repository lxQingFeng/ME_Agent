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
程序状态置 busy 防重复触发, 进度写 st["progress"] 并经 WS program.progress 推送
(第 11 轮: 已下载/总字节/百分比/速度; docker 形态为阶段级 phase=pull/extract)。

第 11 轮(37 实机反馈修复):
- 进程存活探测平台感知: Windows OpenProcess(ctypes)+tasklist 兜底, macOS 无 /proc
  退 os.kill; spawn 句柄 poll 恒优先(修「刷新后变已停止」根因);
- docker 形态状态以 docker inspect 容器真实状态为准, http 只补 detail(容器在跑
  而 REST 未就绪 ≠ 已停止); installed 不再被 http 探测失败误翻为 stopped;
- 重复 start 明确拒绝(ok=False+running=True); spawn 后 1.5s 秒退探测(exit code
  + 日志尾部 + 修复指引); Windows 直连自带 node.exe+index.mjs(避开 .bat 的
  pause 挂起与孤儿进程);
- 日志全量/分页读取(logs_page: offset/limit/total/has_more); 按程序 webui 配置
  的正则从日志增量提取登录凭据(st.webui_token, WS program.webui 推送),
  解决 37 拿不到 SnowLuma WebUI 初始密码的问题; 日志超 50MB 自动保尾轮转。

程序状态置 busy(防重复触发), 完成后状态落回 installed/running 并发 program.status
事件; 调用方立即拿到 {queued: true} 响应, 经 GET /api/v1/programs 轮询进度。

监控: monitor 循环每 10s 刷新进程/容器状态并做令牌增量扫描, 状态变化时向 bus 发
program.status 事件(WS 推送)。
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
        self._token_scan_off: dict[str, int] = {}   # pid → 令牌扫描已读字节偏移(增量)
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
            if pid and not _pid_alive(pid, st.get("started_at")):
                # 平台感知探测(第 11 轮): Windows 下句柄丢失(服务重启)但进程真在跑
                # → 保持 running(此前 /proc 恒判死导致 37 实机刷新即回跳 stopped)
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

    async def _download(self, url: str, dest: Path, pid: str | None = None) -> None:
        """流式下载; pid 给定时回报进度(第 11 轮):
        st["progress"] = {phase, downloaded, total, percent, speed_bps} 供 busy 轮询,
        并经 WS topic program.progress 推送(0.5s 节流)。"""
        st = self._st(pid) if pid else None
        last_emit = 0.0
        async with self._client_ensure().stream("GET", url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)   # 0 = 未知(分块)
            done = 0
            t0 = time.monotonic()
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes(65536):
                    f.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if st is not None and now - last_emit >= 0.5:
                        last_emit = now
                        self._report_progress(pid, "download", done, total, t0)
        if st is not None:
            self._report_progress(pid, "download", done, total or done, t0, final=True)

    def _report_progress(self, pid: str, phase: str, done: int, total: int,
                         t0: float, final: bool = False) -> None:
        """写程序状态(busy 轮询可见) + WS 推送 program.progress。"""
        st = self._st(pid)
        elapsed = max(1e-6, time.monotonic() - t0)
        pct = round(done / total * 100, 1) if total else None
        prog = {"phase": phase, "downloaded": done, "total": total,
                "percent": pct, "speed_bps": int(done / elapsed)}
        if final:
            prog["percent"] = 100.0
            prog["done"] = True
        st["progress"] = prog
        self._save_state()
        if self.bus:
            self.bus.emit("program.progress", {"id": pid, **prog})

    def _set_phase(self, pid: str, phase: str, detail: str = "") -> None:
        """无字节语义的阶段进度(docker pull/extract 等): 阶段名 + 说明。"""
        st = self._st(pid)
        prog = {"phase": phase, "detail": detail}
        st["progress"] = prog
        self._save_state()
        if self.bus:
            self.bus.emit("program.progress", {"id": pid, **prog})

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
        Windows: ① 自带 node.exe + index.mjs → 直连 spawn(优先, 见下);
                 ② launcher.bat/launcher.exe/run.bat/*.exe;
        POSIX:   launcher.sh/run.sh/*.sh(经 bash 执行), 无脚本时保留 .exe 便于诊断。

        Windows 直连 node 的理由(第 11 轮): .bat 包装器(如 SnowLuma win 包)末尾有
        pause —— node 退出后 cmd 停在「请按任意键」, 进程句柄恒活、exit code 丢失,
        且 terminate 只杀 cmd 会孤儿化 node(端口仍被占)。直连 node.exe 则
        生命周期/退出码/停止语义全部真实。
        """
        if sys.platform == "win32":
            node = install_dir / "node.exe"
            entry = install_dir / "index.mjs"
            if node.is_file() and entry.is_file():
                return [str(node), str(entry)]
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
                    self._set_phase(pid, "download", f"下载 {asset['name']}")
                    await self._download(asset["url"], archive, pid=pid)   # 已有同版本同大小缓存则复用
                self._set_phase(pid, "extract", f"解压 → opt/{pid}/{rel['version']}")
                n = self._extract(archive, self.opt_dir / pid / rel["version"])
            except Exception as e:
                self._st(pid).pop("progress", None)
                self._save_state()
                return {"ok": False, "error": f"下载/解压失败: {type(e).__name__}: {e}"}
            if n == 0:
                self._st(pid).pop("progress", None)
                self._save_state()
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
            st.pop("progress", None)   # 进度收口: 完成(终态与日志已带结果)
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
            self._set_phase(pid, "pull", "拉取镜像中(docker 无字节级进度, 输出完成后记入日志)")
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
                st.pop("progress", None)
                self._save_state()
            else:
                st.pop("progress", None)
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
                res["start_result"] = await self.start(pid, force=True)
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
                res["start_result"] = await self.start(pid, force=True)   # compose up -d 会按新镜像重建
            if res.get("ok"):
                self._log(pid, "[update] 镜像已更新" + ("(容器已重建)" if was_running else ""))
                await self._notify(pid)
            return res
        return {"ok": False, "error": "该程序形态不支持更新(external/external自管请手动升级)"}

    # ---------- 启停 ----------
    async def start(self, pid: str, force: bool = False) -> dict:
        """启动程序; force=True 供 update 自动拉起绕过「重复启动拒绝」(docker 重建场景)。"""
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
            # 重复启动明确拒绝(第 11 轮): 容器已在跑 → ok=False + running=True
            # (force=True 供 update 的「拉新镜像后 compose up -d 重建」路径绕过)
            if not force:
                cstate = await self._docker_container_state(pid)
                if cstate == "running":
                    return {"ok": False, "running": True, "error": f"容器 {name} 已在运行, 重复启动被拒绝",
                            "container": name}
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
        # 重复启动明确拒绝(第 11 轮): 句柄活着或 pid 探测活着 → ok=False + running=True
        # (旧口径 ok=True+note 不算「拒绝」, 前端无法锁定按钮; update 自动拉起传 force)
        held = self._procs.get(pid)
        if not force and held is not None and held.returncode is None:
            return {"ok": False, "running": True, "pid": held.pid,
                    "error": f"已在运行(pid={held.pid}), 重复启动被拒绝"}
        if (not force and st.get("status") == "running"
                and _pid_alive(st.get("pid"), st.get("started_at"))):
            return {"ok": False, "running": True, "pid": st.get("pid"),
                    "error": f"状态为运行中(pid={st.get('pid')}), 重复启动被拒绝; "
                             f"若确认进程已死请先 stop 再 start"}
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
        st.update(status="running", pid=proc.pid, started_at=time.time(), exit_code=None)
        self._save_state()
        self._log(pid, f"[start] pid={proc.pid} cmd={cmd} cwd={cwd}")
        # 秒退探测(第 11 轮): spawn 后 1.5s 观察窗, 立即退出则带回 exit code 与日志尾部,
        # 不再让用户面对「启动成功→10 秒后变已停止」的谜之回跳
        await asyncio.sleep(1.5)
        if proc.returncode is not None:
            st.update(status="stopped", pid=None, exit_code=proc.returncode)
            self._procs.pop(pid, None)
            self._save_state()
            tail = "\n".join(self.logs(pid, limit=8))
            self._log(pid, f"[start] 进程 1.5s 内退出 exit={proc.returncode} —— 尾部日志:\n{tail}")
            await self._notify(pid)
            return {"ok": False, "error": f"进程启动后立即退出(exit={proc.returncode})",
                    "exit_code": proc.returncode, "log_tail": tail,
                    "hint": "查看日志尾部定位原因(常见: 依赖缺失/端口占用/配置错误/被杀软拦截)"}
        self._scan_tokens(pid)   # 启动段可能已打出 WebUI 凭据
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
            st.pop("progress", None)   # 进度收口(异常路径兜底; 成功路径 install 内已收)
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
            return {"type": "process", "status": "running", "detail": f"pid {proc.pid}(句柄持有)"}
        if st.get("pid") and _pid_alive(st["pid"], st.get("started_at")):
            how = "句柄丢失(服务重启), pid 探测在跑" if st.get("status") == "running" else "pid 在跑"
            return {"type": "external", "status": "running", "detail": f"pid {st['pid']}({how})"}
        return {"type": "none", "status": "stopped"}

    def _proc_info(self, pid: str) -> dict:
        st = self._st(pid)
        proc_pid = st.get("pid")
        info = {"pid": proc_pid, "mem_mb": None, "cpu": None,
                "uptime": None, "restarts": int(st.get("restarts", 0))}
        # uptime 跨平台口径(第 11 轮): 一律由 started_at 推导, Windows 也有值
        if st.get("started_at"):
            info["uptime"] = _fmt_uptime(max(0, int(time.time() - float(st["started_at"]))))
        if not proc_pid or not _pid_alive(proc_pid):
            return info
        try:
            stat = Path(f"/proc/{proc_pid}/stat").read_text("utf-8")
            fields = stat.rsplit(")", 1)[1].split()
            utime, stime = int(fields[11]), int(fields[12])
            rss = int(Path(f"/proc/{proc_pid}/statm").read_text("utf-8").split()[1]) * 4096
            info["mem_mb"] = round(rss / 1048576, 1)
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
                "progress": st.get("progress"),        # 第 11 轮: 下载/拉取进度(busy 轮询)
                "webui": self.webui_info(pid),          # 第 11 轮: SnowLuma 登录凭据(检测到时)
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
        """尾部 N 行(旧契约, V4 console 兼容); 全量/分页走 logs_page()。"""
        bufs = self._log_buffers.setdefault(pid, [])
        f = self.logs_dir / f"{pid}.log"
        if f.exists():
            try:
                lines = f.read_text("utf-8", errors="replace").splitlines()[-limit:]
            except Exception:
                lines = []
            return lines
        return bufs[-limit:]

    def logs_page(self, pid: str, offset: int | None = None, limit: int = 200) -> dict:
        """日志分页读取(第 11 轮, 令 37 能翻到启动段拿 WebUI 凭据)。

        - offset=None: 尾部语义(兼容旧调用) — 返回末尾 limit 行, offset 字段回真实起始行号;
        - offset>=0: 行号分页 — lines[offset : offset+limit], has_more 标记是否还有下页;
        - limit<=0: 从 offset(缺省 0)读到结尾(全量);
        - 全部含 total 总行数。启动段(含 SnowLuma WebUI 凭据)从 offset=0 可全量翻阅。
        """
        bufs = self._log_buffers.setdefault(pid, [])
        f = self.logs_dir / f"{pid}.log"
        all_lines: list[str] = bufs
        if f.exists():
            try:
                all_lines = f.read_text("utf-8", errors="replace").splitlines()
            except Exception:
                all_lines = bufs
        total = len(all_lines)
        if offset is None:   # 尾部语义
            n = limit if limit and limit > 0 else total
            lines = all_lines[-n:] if n < total else all_lines
            off = max(0, total - n)
        else:                # 行号分页语义
            off = max(0, int(offset))
            n = limit if limit and limit > 0 else max(0, total - off)
            lines = all_lines[off: off + n]
        return {"total": total, "offset": off, "limit": len(lines),
                "lines": lines, "has_more": off + len(lines) < total}

    def _log(self, pid: str, line: str) -> None:
        ts = time.strftime("%H:%M:%S")
        f = self.logs_dir / f"{pid}.log"
        with f.open("a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] {line}\n")

    # ---------- WebUI 凭据提取(第 11 轮: 37 拿不到 SnowLuma 登录令牌) ----------
    def _scan_tokens(self, pid: str) -> None:
        """增量扫描程序日志, 按 config/programs.yaml 的 webui.{token,url}_pattern 提取凭据。

        SnowLuma 真实输出(v2.2.0 核实):
          <ts> INFO  [WebUI] initial credentials: user=admin password=<随机>
          <ts> INFO  [WebUI] listening http://127.0.0.1:5099
        命中 → st.webui_token/webui_user/webui_url(经 GET /programs 与 WS 暴露),
        且检测到变化时推送 program.webui 事件。进程重启换新令牌会覆盖旧值。
        """
        d = self.def_(pid) or {}
        w = d.get("webui") or {}
        tok_pat, url_pat = w.get("token_pattern"), w.get("url_pattern")
        if not (tok_pat or url_pat):
            return
        f = self.logs_dir / f"{pid}.log"
        if not f.exists():
            return
        try:
            data = f.read_bytes()
            start = self._token_scan_off.get(pid, 0)
            if start < 0 or start > len(data):   # 文件被轮转/重置 → 从头重扫
                start = 0
            new = data[start:].decode("utf-8", "replace")
            self._token_scan_off[pid] = len(data)
            st = self._st(pid)
            changed = False
            if tok_pat:
                m = re.search(tok_pat, new)
                if m:
                    gd = m.groupdict()
                    tok = gd.get("token") or (m.groups()[-1] if m.groups() else None)
                    user = gd.get("user") or w.get("username") or ""
                    if tok and st.get("webui_token") != tok:
                        st["webui_token"] = tok
                        st["webui_user"] = user
                        changed = True
                        self._log(pid, f"[webui] 检测到登录凭据(user={user}); "
                                       f"已写入程序详情 webui 字段并推送 program.webui")
            if url_pat:
                m = re.search(url_pat, new)
                if m and st.get("webui_url") != m.group(1):
                    st["webui_url"] = m.group(1)
                    changed = True
            if changed:
                self._save_state()
                if self.bus:
                    self.bus.emit("program.webui", {"id": pid, **self.webui_info(pid)})
        except Exception:
            pass

    def webui_info(self, pid: str) -> dict:
        """程序详情里的 webui 字段(令牌仅本机控制台可见, 端口勿暴露公网)。"""
        st = self._st(pid)
        tok = st.get("webui_token")
        if not tok:
            return {}
        return {"token": tok, "user": st.get("webui_user") or "", "url": st.get("webui_url") or ""}

    def _maybe_rotate_log(self, pid: str, max_bytes: int = 50 * 1024 * 1024,
                          keep_bytes: int = 10 * 1024 * 1024) -> None:
        """程序日志尺寸轮转: 超过 50MB 保留尾部 10MB(协议端长期运行防撑爆磁盘)。

        轮转后行号语义变化, 同步重置该 pid 的令牌扫描偏移(下次从头重扫一次, 幂等)。
        """
        f = self.logs_dir / f"{pid}.log"
        try:
            if not f.exists() or f.stat().st_size <= max_bytes:
                return
            with f.open("rb") as fh:
                fh.seek(-keep_bytes, 2)
                tail = fh.read()
            nl = tail.find(b"\n")
            tail = tail[nl + 1:] if nl >= 0 else tail   # 对齐到整行
            tmp = f.with_suffix(".log.tmp")
            tmp.write_bytes(tail)
            tmp.replace(f)
            self._token_scan_off[pid] = 0
            self._log(pid, f"[rotate] 日志超过 {max_bytes // 1048576}MB, 已保留尾部 "
                           f"{len(tail) // 1048576}MB(全量翻阅从新文件行号 0 开始)")
        except Exception:
            pass

    async def _notify(self, pid: str) -> None:
        if self.bus:
            st = self._st(pid)
            evt = {"id": pid, "status": st.get("status"), "pid": st.get("pid")}
            if st.get("progress"):
                evt["progress"] = st["progress"]
            w = self.webui_info(pid)
            if w:
                evt["webui"] = w
            self.bus.emit("program.status", evt)

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

    async def _probe_http_authoritative(self, pid: str, st: dict, h: dict) -> None:
        """http 健康探测(权威口径, 供远程形态/非 docker http 程序)。

        修正(第 11 轮): 只有「此前在跑」的程序才允许探测失败降级为 stopped;
        installed/error 状态探测失败保持原状态(未启动≠已停止, 不再误翻)。
        """
        try:
            resp = await self._client_ensure().get(h.get("url", ""), timeout=5.0)
            self._health_cache[pid] = {"status": "running" if resp.status_code < 500 else "error",
                                       "detail": f"HTTP {resp.status_code}"}
        except Exception as e:
            self._health_cache[pid] = {"status": "stopped", "detail": type(e).__name__}
        cstat = self._health_cache[pid]["status"]
        cur = st.get("status")
        if cstat == "running" and cur != "running":
            st["status"] = "running"    # 外部拉起 → 自动识别
            self._save_state()
            await self._notify(pid)
        elif cstat != "running" and cur == "running":
            st["status"] = "stopped"    # 在跑但探测失败 → 降级
            self._save_state()
            await self._notify(pid)

    async def _docker_container_state(self, pid: str) -> str | None:
        """容器真实状态(compose ps/inspect 口径): 'running'/'stopped'/None(探测不可用)。

        docker inspect -f {{.State.Running}} <name>; 无 docker CLI/容器不存在/出错 → None,
        None 表示「本探测无信号」(调用方应沿用其他口径, 如远程 http 探测)。
        """
        d = self.def_(pid) or {}
        dd = d.get("docker") or {}
        st = self._st(pid)
        name = st.get("container") or dd.get("container_name", f"me-agent-{pid}")
        if not shutil.which("docker"):
            return None
        code, out = await self._run_cmd(
            ["docker", "inspect", "-f", "{{.State.Running}}", name], timeout_s=15)
        if code == 0:
            return "running" if out.strip().lower() == "true" else "stopped"
        return None

    async def _monitor_loop(self) -> None:
        while True:
            try:
                for d in self.defs:
                    await self._monitor_once(d)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(10)

    async def _monitor_once(self, d: dict) -> None:
        """单个程序的一轮监控(容器/进程探测 + 令牌扫描 + 轮转); 供测试直接驱动。"""
        try:
            pid = d["id"]
            st = self._st(pid)
            h = (d.get("run") or {}).get("health") or {}
            # ---------- docker 形态: 容器真实状态为准(inspect 口径, 第 11 轮) ----------
            # http 探测只补 detail(容器在跑而 REST 未就绪是「启动中」不是「停止」);
            # 本机无 docker(inspect 不可用)时沿用 http 权威口径(远程部署形态)。
            if (d.get("kind") == "docker" and st.get("status") != "not_installed"
                    and not st.get("busy_action")):
                cstate = await self._docker_container_state(pid)
                if cstate is not None:
                    detail = "容器运行中"
                    if h.get("type") == "http":
                        try:
                            resp = await self._client_ensure().get(h.get("url", ""), timeout=5.0)
                            detail = (f"容器运行中; REST API HTTP {resp.status_code}"
                                      if resp.status_code < 500
                                      else f"容器运行中; REST API 异常 HTTP {resp.status_code}")
                        except Exception as e:
                            detail = f"容器运行中; REST API 未就绪({type(e).__name__})"
                    self._health_cache[pid] = {"status": cstate, "detail": detail}
                    if st.get("status") != cstate:
                        st["status"] = cstate
                        self._log(pid, f"[monitor] 容器状态 → {cstate}({detail})")
                        self._save_state()
                        await self._notify(pid)
                elif h.get("type") == "http":   # inspect 不可用 → http 兜底(远程形态)
                    await self._probe_http_authoritative(pid, st, h)
            # ---------- 非 docker 的 http 健康探测(仅 running 可降级, 不误伤 installed) ----------
            elif (h.get("type") == "http" and st.get("status") != "not_installed"
                    and not st.get("busy_action")):   # busy(拉镜像等)期间不做健康探测
                await self._probe_http_authoritative(pid, st, h)
            # ---------- spawn 进程: 句柄优先, pid 探测兜底(第 11 轮修状态失真) ----------
            # 37 实机反馈根因: Windows 无 /proc, 旧探测恒判死 → 10 秒内 running 被
            # 误翻 stopped。现句柄 poll 恒准且优先; 句柄丢失(服务重启)才退到
            # 平台感知 pid 探测(OpenProcess/tasklist, 见 _pid_alive)。
            proc = self._procs.get(pid)
            if proc is not None and proc.returncode is not None and st.get("status") == "running":
                st["status"] = "stopped"
                st["pid"] = None
                st["exit_code"] = proc.returncode
                self._log(pid, f"[monitor] 进程退出 code={proc.returncode}(stderr 已随 stdout 落日志)")
                self._procs.pop(pid, None)
                self._save_state()
                await self._notify(pid)
            elif proc is not None and proc.returncode is None:
                pass   # 句柄活着: 不做 pid 兜底探测(探测失真不再误杀 running)
            elif st.get("pid") and st.get("status") == "running" and not _pid_alive(st["pid"]):
                st["status"] = "stopped"
                st["exit_code"] = None
                st["pid"] = None
                self._log(pid, "[monitor] 外部进程消失(pid 探测失效)")
                self._save_state()
                await self._notify(pid)
            # ---------- 令牌扫描 + 日志尺寸轮转(轻量, 只处理增量字节) ----------
            if st.get("status") == "running":
                self._scan_tokens(pid)
            self._maybe_rotate_log(pid)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

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
                # 重复启动明确拒绝(第 11 轮, 口径与 binary/docker 一致; autostart 幂等无害)
                return {"ok": False, "running": True, "error": "内部同步循环已在运行, 重复启动被拒绝",
                        "note": "已在运行"}
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
_IS_WIN = sys.platform == "win32"


def _win_openproc_alive(pid: int, started_at: float | None = None) -> bool | None:
    """Windows ctypes 探测: OpenProcess + GetExitCodeProcess(微秒级, 无子进程)。

    返回 True/False/None(None=ctypes 探测不可用, 调用方走 tasklist 兜底)。
    pid 重用防护: 进程创建时间早于 started_at - 120s → 视为 pid 被复用 → 判死。
    """
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32   # 非 Windows 会 AttributeError → None
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return None   # 拒绝访问/句柄失败: 有权限不确定性, 交 tasklist 兜底
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                return None
            if code.value != STILL_ACTIVE:
                return False
            # STILL_ACTIVE: 再比对创建时间排除 pid 复用(尽力而为, 失败不拦判定)
            try:
                creation = ctypes.c_ulonglong()
                _exit = ctypes.c_ulonglong()
                _kern = ctypes.c_ulonglong()
                _user = ctypes.c_ulonglong()
                if (kernel32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(_exit),
                                             ctypes.byref(_kern), ctypes.byref(_user))
                        and creation.value and started_at):
                    created_epoch = creation.value / 1e7 - 11644473600.0   # FILETIME(100ns) → epoch s
                    if created_epoch < float(started_at) - 120:
                        return False   # 进程创建早于记录的启动 → pid 被复用
            except Exception:
                pass
            return True
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return None


def _tasklist_has(pid: int) -> bool | None:
    """Windows tasklist 兜底探测(OpenProcess 不可用/被拒时)。

    返回 True/False/None(None=tasklist 也不可用)。编码按系统 locale(mbcs/GBK),
    只判断输出中是否含该 PID 且非「INFO: 没有任务」提示行。
    """
    try:
        import subprocess
        r = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=10, text=True,
                           encoding="mbcs", errors="replace")
        out = r.stdout or ""
        if "INFO:" in out or "信息:" in out:
            return False
        return f'"{int(pid)}"' in out
    except Exception:
        return None


def _pid_alive(pid: int | None, started_at: float | None = None) -> bool:
    """平台感知的进程存活探测(第 11 轮修「Windows 恒判死」根因)。

    - POSIX: /proc 存在则查 /proc(Linux); 无 /proc(macOS/BSD)退 os.kill(pid, 0);
    - Windows: ctypes OpenProcess(快, started_at 参与 pid 复用防护) → tasklist 兜底;
    - 全部探测手段异常时返回 False(仅剩句柄/容器口径兜底, 见 monitor/_load_state)。
    """
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if _IS_WIN:
            r = _win_openproc_alive(pid, started_at)
            if r is not None:
                return r
            r = _tasklist_has(pid)
            return bool(r) if r is not None else False
        if Path("/proc").is_dir():
            return Path(f"/proc/{pid}").exists()
        try:
            os.kill(pid, 0)   # macOS/BSD: 无 /proc 的 POSIX 探测
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True    # 存在但非本用户进程
        except OSError:
            return False
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
