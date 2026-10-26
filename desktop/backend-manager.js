/* ============================================================
   backend-manager.js — 后端生命周期管理（纯 Node，可脱离 Electron 单测）
   ------------------------------------------------------------
   契约对齐 v1（shared/ME_Agent桌面壳对接契约_v1.md）：
   1) 双探（§三）：
      · 就绪：GET /api/v1/system/health → {"ok":true, ts}
      · 判别：GET /api/v1/system/status → 含 ME_Agent 特征字段
        （memory/gateway/programs/tts/llm_enabled/started_at，命中 ≥2 视为 ME_Agent）
        —— 防止无关程序占端口被误附加（端口≠锁，§四.3）。
   2) 拉起（§一）：原始命令 <仓库根>\.venv\Scripts\python.exe -m app --host 127.0.0.1
      （参数透传 --host/--port/--no-engine；cwd 必须显式 = 仓库根）。
      命令推导：repoDir 已配置且未显式给 startCommand 时自动构造
      （win32: .venv\Scripts\python.exe；posix: .venv/bin/python）。
   3) 实例锁（§四）：spawn 秒退且 stderr 含 InstanceLockError → mode="lock-held"
      （锁被占：实例在别的端口/host），明确提示、勿重试循环；已有实例（health ok
      且 status 判别通过）直接附加不拉新。
   4) 关壳（§四.4）：默认「保持后端运行」（takeDown=false，子进程随壳退出自然存活）；
      takeDown=true 时 terminate 本壳拉起的实例（SIGTERM → SIGKILL 兜底），
      附加实例一律不带走。claim 残留由后端下次启动自动回收（ABA 安全）。
   5) 仓库根自动推导（v0.2.0 入库适配）：壳位于仓库 desktop/ 子目录时，
      以壳目录上级为 ME_Agent 仓库根候选——按「.venv / app/__main__.py 等仓库特征
      ≥2 且至少一个强特征（app 入口或 venv）」判别；推导成功供 config 层跳过首启
      手填，显式配置（config.json / config.local.json / setup 窗）恒优先。
   ============================================================ */
"use strict";

const http = require("http");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");

/* ME_Agent 特征字段（§三 status 响应）——命中 ≥2 视为本尊 */
const ME_AGENT_FIELDS = ["memory", "gateway", "programs", "tts", "llm_enabled", "started_at"];

/* ---------- venv python 路径（§一：win32 .venv\Scripts\python.exe / posix .venv/bin/python） ---------- */
function venvPythonPath(repoRoot) {
  return process.platform === "win32"
    ? path.join(repoRoot, ".venv", "Scripts", "python.exe")
    : path.join(repoRoot, ".venv", "bin", "python");
}

/* ---------- 仓库根自动推导（v0.2.0：壳位于仓库 desktop/ 子目录场景） ----------
   候选 = 壳目录上级。特征清单（强特征 ∩ 阈值判别，防把任意父目录误认成仓库）：
   · app/__main__.py   强（后端入口，契约 §一）
   · .venv python      强（部署产物，admin 口径「检测到 .venv / app/ 等仓库特征」）
   · console/index.html（仓库控制台）
   · requirements.txt
   · deploy/scripts/   （启动ME_Agent.bat 所在）
   规则：命中 ≥2 且至少含一个强特征 → 推导成功。返回值供 config 层使用，
   显式 repoDir（config.json / config.local.json / setup 窗落盘）恒优先于推导。 */
function repoFeatureHits(repoRoot) {
  const hits = [];
  if (fs.existsSync(path.join(repoRoot, "app", "__main__.py"))) hits.push("app-entry");
  const venv = venvPythonPath(repoRoot);
  if (fs.existsSync(venv)) hits.push("venv-python");
  if (fs.existsSync(path.join(repoRoot, "console", "index.html"))) hits.push("console");
  if (fs.existsSync(path.join(repoRoot, "requirements.txt"))) hits.push("requirements");
  if (fs.existsSync(path.join(repoRoot, "deploy", "scripts"))) hits.push("deploy-scripts");
  return hits;
}

function deriveRepoDir(shellDir) {
  if (!shellDir) return { ok: false, reason: "no-shell-dir" };
  const parent = path.resolve(shellDir, "..");
  if (!parent || parent === path.resolve(shellDir)) return { ok: false, reason: "no-parent" };
  const hits = repoFeatureHits(parent);
  const strong = hits.includes("app-entry") || hits.includes("venv-python");
  if (hits.length >= 2 && strong) {
    return { ok: true, repoDir: parent, hits, venvReady: hits.includes("venv-python") };
  }
  return { ok: false, repoDir: parent, hits, reason: hits.length === 0 ? "no-features" : "below-threshold" };
}

class BackendManager {
  constructor(opts = {}) {
    this.url = opts.url || "http://127.0.0.1:8000";
    this.healthPath = opts.healthPath || "/api/v1/system/health";
    this.statusPath = opts.statusPath || "/api/v1/system/status";
    this.repoDir = opts.repoDir || "";                       // ME_Agent 仓库根（拉起命令推导用）
    this.startCommand = opts.startCommand || "";             // 显式命令优先；空则由 repoDir 推导
    this.startCwd = opts.startCwd || "";
    this.spawnArgs = opts.spawnArgs || ["--host", "127.0.0.1"];   // §六：收缩暴露面，仅本机
    this.spawnEnv = opts.spawnEnv || null;                   // 附加环境变量（合并 process.env；不设 ME_STATE_DIR）
    this.takeDown = !!opts.takeBackendDownOnExit;
    this.log = opts.log || (() => {});
    this.child = null;            // 本壳 spawn 的后端进程（附加模式为 null）
    this.tail = [];               // 后端输出尾部（排障用，最多 60 行）
  }

  /* ---------- HTTP GET（读 JSON body） ---------- */
  _getJson(p, timeoutMs) {
    return new Promise(resolve => {
      const req = http.get(`${this.url}${p}`, { timeout: timeoutMs }, res => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", d => { body += d; if (body.length > 65536) req.destroy(); });
        res.on("end", () => {
          let json = null;
          try { json = JSON.parse(body); } catch (e) { /* 非 JSON（如别的程序的 HTML 页） */ }
          resolve({ httpOk: res.statusCode >= 200 && res.statusCode < 400, status: res.statusCode, json, body: body.slice(0, 200) });
        });
      });
      req.on("timeout", () => { req.destroy(new Error("timeout")); });
      req.on("error", err => resolve({ httpOk: false, status: 0, error: err.message }));
    });
  }

  /* ---------- 双探（§三）：health 就绪 → status 判别 ---------- */
  async probe(timeoutMs = 1500) {
    const t0 = Date.now();
    const h = await this._getJson(this.healthPath, timeoutMs);
    const out = {
      ok: false, isMeAgent: false, statusInfo: null,
      httpStatus: h.status, error: h.error || null,
      latencyMs: Date.now() - t0, url: this.url,
    };
    /* 就绪判据：health 返回 2xx 且 JSON.ok === true */
    if (!(h.httpOk && h.json && h.json.ok === true)) return out;
    out.ok = true;
    /* 判别（§四.3 端口≠锁）：status 必须含 ME_Agent 特征字段，防误附加 */
    const s = await this._getJson(this.statusPath, timeoutMs);
    if (s.httpOk && s.json && typeof s.json === "object" && !Array.isArray(s.json)) {
      out.statusInfo = s.json;
      const hits = ME_AGENT_FIELDS.filter(k => k in s.json).length;
      out.isMeAgent = hits >= 2;
    }
    return out;
  }

  /* ---------- 拉起命令构造（§一：仓库根 + .venv python -m app） ---------- */
  buildStartCommand() {
    if (this.startCommand) return { command: this.startCommand, cwd: this.startCwd || this.repoDir || undefined };
    if (!this.repoDir) return null;
    const venvPython = venvPythonPath(this.repoDir);
    if (!fs.existsSync(venvPython)) return { command: null, missingVenv: venvPython, cwd: this.repoDir };
    return { command: `"${venvPython}" -m app ${this.spawnArgs.join(" ")}`, cwd: this.repoDir };
  }

  /* ---------- 仓库根自检（首启引导用；repoDir 可能为自动推导值） ---------- */
  inspectRepo() {
    if (!this.repoDir) return { ok: false, reason: "未配置 ME_Agent 仓库根目录" };
    if (!fs.existsSync(this.repoDir)) return { ok: false, reason: `目录不存在：${this.repoDir}` };
    const venvPython = venvPythonPath(this.repoDir);
    if (!fs.existsSync(venvPython)) return {
      ok: false, reason: "未找到 .venv（首次部署未完成）",
      hint: "先双击仓库内 deploy/scripts/启动ME_Agent.bat 完成首次部署（自动建 .venv 并安装依赖），完成后回到壳重试。",
    };
    return { ok: true, venvPython };
  }

  /* ---------- 命令行解析：支持双引号/单引号包裹的含空格路径（Windows 常见形态） ---------- */
  _parseCmd(cmd) {
    const parts = [];
    const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
    let m;
    while ((m = re.exec(cmd))) parts.push(m[1] != null ? m[1] : (m[2] != null ? m[2] : m[3]));
    return parts;
  }

  _spawn() {
    const built = this.buildStartCommand();
    if (!built || !built.command) return { spawned: false, reason: built && built.missingVenv ? "missing-venv" : "no-command" };
    const parts = this._parseCmd(built.command);
    const child = spawn(parts[0], parts.slice(1), {
      cwd: built.cwd,                        // §一：cwd 必须显式 = 仓库根（state/config/console 按其解析）
      env: Object.assign({}, process.env, this.spawnEnv || {}),   // §一：不设 ME_STATE_DIR（除非刻意独立实例）
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,                     // Windows 下不弹控制台窗口
    });
    this.child = child;
    this.spawnedAt = Date.now();
    this._quickExitDetected = false;
    const onOut = (buf, isStderr) => {
      const s = String(buf).trim();
      if (!s) return;
      this.tail.push(s);
      if (this.tail.length > 60) this.tail.shift();
      if (isStderr && /InstanceLockError/i.test(s)) this._lockSignaled = true;   // §四：锁拒绝留痕
      this.log(`[backend] ${s.split("\n").slice(-2).join(" | ").slice(0, 200)}`);
    };
    child.stdout.on("data", b => onOut(b, false));
    child.stderr.on("data", b => onOut(b, true));
    child.on("exit", (code, sig) => {
      const aliveMs = Date.now() - (this.spawnedAt || Date.now());
      this.log(`[backend] 进程退出 code=${code} sig=${sig}（存活 ${aliveMs}ms）`);
      if (aliveMs < 5000) this._quickExitDetected = true;                         // 秒退（§四）
      if (this.child === child) this.child = null;
    });
    this.log(`[backend] 已拉起：${built.command}（cwd=${built.cwd || "<inherit>"} pid=${child.pid}）`);
    return { spawned: true, pid: child.pid };
  }

  /* ---------- 启动决策（§四 正确姿势）：附加 / 拉起 / 锁占 / 端口占 / 演示 ---------- */
  async ensureRunning(o = {}) {
    const first = await this.probe(o.probeTimeoutMs || 1500);
    if (first.ok && first.isMeAgent) {
      return { mode: "attached", probe: first };                 // 已有实例 → 直接附加，不拉新（多壳共享）
    }
    if (first.ok && !first.isMeAgent) {
      /* health ok 但 status 判别不是 ME_Agent：端口被无关程序占用 → 不误附加、不 spawn */
      return { mode: "port-occupied", probe: first,
        message: `端口上有服务响应健康检查，但不是 ME_Agent（status 缺特征字段）。可能被其他程序占用，请检查 ${this.url} 或更换端口。` };
    }

    const sp = this._spawn();
    if (!sp.spawned) {
      return { mode: "demo", probe: first, reason: sp.reason };  // 未配置命令/仓库 → 演示模式（首启引导另行提示）
    }

    /* 轮询等待就绪（§三：0.5~1s 一拍；60s 超时覆盖首次 venv 安装余量） */
    const deadline = Date.now() + (o.spawnTimeoutMs || 60000);
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, o.probeIntervalMs || 800));
      if (!this.child && this._quickExitDetected) {
        /* §四：秒退诊断——锁被占（实例在别的端口/host）→ 明确提示，勿重试循环 */
        if (this._lockSignaled) {
          return { mode: "lock-held", probe: await this.probe(1200), tail: this.tail.slice(-12),
            message: "后端实例锁被占（InstanceLockError，退出码 2）：已有 ME_Agent 实例在运行（可能在别的端口/host）。请找到现有实例直接附加，或先关闭它。壳不再重试拉起。" };
        }
        return { mode: "spawn-failed", probe: await this.probe(1200), tail: this.tail.slice(-12),
          message: "后端进程拉起后很快退出（非实例锁）——多为端口被占或依赖缺失，详见尾部日志。" };
      }
      if (!this.child) break;                                     // 其他形态退出：按失败收口
      const p = await this.probe(1200);
      if (p.ok && p.isMeAgent) return { mode: "spawned", probe: p, pid: this.child ? this.child.pid : null };
      if (p.ok && !p.isMeAgent) return { mode: "port-occupied", probe: p,
        message: "后端已监听但 status 判别未通过（可能仍在启动中或非 ME_Agent）。" };
    }
    return { mode: "spawn-failed", probe: await this.probe(1200), tail: this.tail.slice(-12),
      message: "等待后端就绪超时（60s）。若为首次部署，请先运行仓库内 deploy/scripts/启动ME_Agent.bat 完成依赖安装。" };
  }

  /* ---------- 退出（带走仅限本壳 spawn 的实例；附加实例不带走） ---------- */
  ownsProcess() { return !!this.child; }

  stop() {
    return new Promise(resolve => {
      const c = this.child;
      if (!c) return resolve(false);
      const hard = setTimeout(() => { try { c.kill("SIGKILL"); } catch (e) {} }, 3000);
      c.once("exit", () => { clearTimeout(hard); this.child = null; resolve(true); });
      try { c.kill("SIGTERM"); } catch (e) { clearTimeout(hard); resolve(false); }
    });
  }

  async shutdown() {
    if (!this.takeDown || !this.ownsProcess()) return false;   // 默认保持后端运行（§四.4）
    this.log("[backend] 退出时带走后端进程（配置 takeBackendDownOnExit）");
    return this.stop();
  }
}

module.exports = { BackendManager, ME_AGENT_FIELDS, venvPythonPath, repoFeatureHits, deriveRepoDir };
