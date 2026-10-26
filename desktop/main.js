/* ============================================================
   main.js — QQ Agent 桌面壳（Electron 主进程）
   ------------------------------------------------------------
   契约对齐 v1（shared/ME_Agent桌面壳对接契约_v1.md，基线 8140412）：
   1) 原生窗口加载控制台：
      · Live（§二）：优先直接 loadURL {backendUrl}/console/index.html ——
        console 随 ME_Agent 仓库走（后端更新 git pull 即新），壳与后端解耦；
        回退（consoleFromBackend=false）：app:// 协议本地静态 + 注入。
      · 演示模式（v0.2.0 入库适配）：优先引用**仓库根 console/**（repoDir 已知
        时经 getConsoleSource() 解析，仓库内单一副本随 git pull 更新，杜绝双份
        漂移）；仓库无 console/ 或独立分发场景回退内置 console/ 副本。
      控制台零改动：live 态经 preload 在页面脚本前写 localStorage（既有配置通道）
      + 自定义端口时 defineProperty 校准 API.baseUrl。
   2) 后端生命周期（backend-manager，§一/§三/§四）：双探（health 就绪 + status
      ME_Agent 特征判别，防误附加）→ 附加 / 拉起（repoDir 推导 .venv python -m app
      --host 127.0.0.1，cwd=仓库根）/ 锁占（InstanceLockError 明确提示勿重试）/
      端口占（非 ME_Agent 服务）/ 演示降级。关壳默认保持后端运行。
   3) 首启引导（v0.2.0）：repoDir 解析链 = 显式配置（config.json / local / setup
      窗落盘，手动覆盖恒优先）> 仓库根自动推导（壳位于仓库 desktop/ 子目录时按
      .venv / app 等特征识别，成功则跳过手填）。推导到仓库但缺 .venv（首次部署
      未完成）→ 引导窗预填路径 + .bat 部署指引；完全未配置 → 引导窗手填；后端
      已运行 → 直接附加，不出引导窗。一次配好写 config.local.json。
   4) 托盘常驻 + 关闭到托盘；开机自启（可选，托盘切换）；原生通知。
   5) --smoke：无头冒烟（xvfb 可跑）——JSON 汇总 + 退出码门禁。
   6) v0.3.0 双击即达 + 联动启动：仓库根「启动桌面客户端.bat」双击入口（打包产物
      优先 → Electron dev 运行时回退，start /b 无窗口）；后端就绪后 boot-orchestrator
      POST /api/v1/system/boot 触发第三方程序自动编排，端点 404/未实现 → 静默降级为
      控制台程序页手动按钮（不报错刷屏）；托盘 tooltip/原生通知经 program.* 主题
      同形态事件体现编排进度；全链路（bat→壳→后端 spawn）无 cmd 窗口暴露。
   ============================================================ */
"use strict";

const { app, BrowserWindow, Tray, Menu, Notification, nativeImage, shell, protocol, dialog, ipcMain } = require("electron");
const path = require("path");
const fs = require("fs");
const config = require("./config");
const { BackendManager } = require("./backend-manager");
const { BootOrchestrator } = require("./boot-orchestrator");

const ROOT = config.ROOT;
const BUILTIN_CONSOLE_DIR = path.join(ROOT, "console");   // 独立分发/入库排除同步时的回退副本
const IS_SMOKE = process.argv.includes("--smoke");
const MODE_ARG = (process.argv.find(a => a.startsWith("--mode=")) || "").split("=")[1] || "";   // demo|live

/* 容器/无头环境兼容：no-zygote 关键——沙箱 zygote 在受限容器无法创建共享内存
   （现象：渲染进程 render-process-gone:crashed，/dev/shm 报 ENOENT）；真实桌面
   （Windows/macOS/普通 Linux）不受影响、不开启。ELECTRON_CONTAINER_SAFE=1 供
   CI/容器内的非冒烟运行显式开启同组开关。 */
const CONTAINER_SAFE = IS_SMOKE || process.env.ELECTRON_CONTAINER_SAFE === "1";
if (CONTAINER_SAFE || process.env.ELECTRON_NO_SANDBOX) {
  app.commandLine.appendSwitch("no-sandbox");
  app.commandLine.appendSwitch("no-zygote");
}
if (CONTAINER_SAFE) {
  /* 无头/容器环境：禁 GPU 合成与 dev-shm（容器 /dev/shm 过小与无显卡场景防崩） */
  app.commandLine.appendSwitch("disable-gpu");
  app.commandLine.appendSwitch("disable-gpu-compositing");
  app.commandLine.appendSwitch("disable-dev-shm-usage");
  app.commandLine.appendSwitch("disable-software-rasterizer");
}

const cfg = config.load();
const log = (...a) => console.log(...a);

/* v0.2.0：repoDir 解析链提示（推导成功/显式配置/未识别——首启引导依据） */
if (cfg.repoDirSource === "auto") {
  log(`[desktop] 仓库根自动推导成功（壳位于仓库 desktop/ 子目录）：${cfg.repoDir}`);
} else if (cfg.repoDirSource === "config") {
  log(`[desktop] 使用已配置仓库根：${cfg.repoDir}`);
} else {
  log("[desktop] 未识别仓库根（自动推导未命中且无显式配置）——后端未运行时将弹首启引导窗");
}

const backend = new BackendManager({
  url: cfg.backendUrl,
  healthPath: cfg.healthPath,
  statusPath: cfg.statusPath,
  repoDir: cfg.repoDir,
  startCommand: cfg.backendStartCommand,
  startCwd: cfg.backendStartCwd,
  spawnArgs: cfg.spawnArgs,
  takeBackendDownOnExit: cfg.takeBackendDownOnExit,
  log,
});

/* ---------- v0.3.0 联动启动：后端就绪后 POST /system/boot 触发第三方程序编排 ----------
   端点未实现（404）等一切失败形态 → boot-orchestrator 内部静默降级为控制台程序页
   手动按钮（不报错不刷屏、本会话不再重试）；进度经 program.* 主题同形态事件驱动
   托盘 tooltip / 原生通知。契约接缝收敛在 config.json（bootPath 等）。 */
const bootOrch = new BootOrchestrator({
  url: cfg.backendUrl,
  bootPath: cfg.bootPath,
  programsPath: cfg.bootProgramsPath,
  enabled: cfg.bootEnabled,
  pollIntervalMs: cfg.bootPollIntervalMs,
  watchTimeoutMs: cfg.bootWatchTimeoutMs,
  log,
  onEvent: e => onBootEvent(e),
});

function maybeTriggerBoot() {
  if (bootOrch.state !== "idle") return;   // 每会话一次：重连/引导不重复编排
  bootOrch.trigger().catch(e => log(`[boot] 触发异常（静默降级）：${e.message}`));
}

let win = null;             // 主窗口（控制台）
let setupWin = null;        // 首启引导窗
let tray = null;
let quitting = false;
let BOOT_MODE = "demo";     // live | demo
let lastDecision = null;    // 最近一次后端决策（托盘状态/提示用）

/* ---------- 单实例锁（壳自身） ---------- */
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  log("[desktop] 已有桌面壳实例运行，本次启动转为唤起既有窗口后退出");
  app.quit();
}
app.on("second-instance", () => {
  if (win && !win.isDestroyed()) { if (win.isMinimized()) win.restore(); win.show(); win.focus(); }
  else if (setupWin && !setupWin.isDestroyed()) { setupWin.show(); setupWin.focus(); }
});

/* ---------- app:// 协议：console 静态文件 + 注入（演示/回退用）；setup 引导页 ---------- */
const MIME = { ".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json", ".ico": "image/x-icon" };

/* ---------- console 来源解析（v0.2.0：演示/回退态优先引用仓库根 console/） ----------
   repoDir 已知（显式配置或自动推导）且仓库存在 console/ → 引用之（仓库单一副本，
   随 git pull 更新，避免 desktop/ 内置副本与仓库双份漂移）；
   否则回退内置 console/ 副本（独立分发 / 打包 exe 场景）；
   两者皆无 → null（协议层给出重建指引页）。运行时每请求解析（repoDir 可经
   首启引导窗热更新），preferRepoConsole=false 可强制回退内置副本。 */
function getConsoleSource() {
  if (cfg.preferRepoConsole && cfg.repoDir) {
    const repoConsole = path.join(cfg.repoDir, "console");
    if (fs.existsSync(path.join(repoConsole, "index.html"))) return { dir: repoConsole, source: "repo" };
  }
  if (fs.existsSync(path.join(BUILTIN_CONSOLE_DIR, "index.html"))) return { dir: BUILTIN_CONSOLE_DIR, source: "builtin" };
  return null;
}

const CONSOLE_MISSING_HTML = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>控制台缺失</title>
<style>body{background:#0B1120;color:#F1F5F9;font-family:"Segoe UI","Microsoft YaHei",sans-serif;display:flex;align-items:center;justify-content:center;margin:0}
.p{max-width:560px;padding:28px;border:1px solid #27334F;border-radius:12px;background:#131E36;line-height:1.8}
code{background:#0A101F;border:1px solid #27334F;border-radius:4px;padding:2px 6px;font-size:13px}</style></head>
<body><div class="p"><b>未找到控制台文件</b><br>
本壳未识别到 ME_Agent 仓库的 <code>console/</code> 目录，内置副本也不存在。<br><br>
· 在仓库内运行：把壳放在仓库 <code>desktop/</code> 子目录（自动识别仓库根）；<br>
· 独立分发场景：在壳目录执行 <code>npm run sync-console</code> 重建内置副本后重启。<br><br>
（详见 README「演示模式与控制台来源」）</div></body></html>`;

function bootScript(mode) {
  /* 注入点：App.boot() 之前执行——api.js 已装载（可校准 API.baseUrl）、restoreMode 可读 localStorage */
  const base = JSON.stringify(cfg.backendUrl);
  const ws = JSON.stringify(cfg.backendUrl.replace(/^http/, "ws") + "/ws");
  return `<script>/* qq-agent-desktop 注入（勿改控制台）*/try{localStorage.setItem("console.api.mode","${mode}");localStorage.setItem("console.api.baseUrl",${base});if(window.API){API.baseUrl=${base};API.wsUrl=${ws};}}catch(e){}</script>`;
}

protocol.registerSchemesAsPrivileged([
  { scheme: "app", privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true } },
]);

function registerAppProtocol() {
  /* CSP：壳层注入（作用于 app:// 本地 console——演示/回退态，含「引用仓库 console/」
     场景；后端直连 Live 页面的 CSP 由后端决定）。仓库 console 与内置副本同为
     控制台 v5.1 纯静态页，同一 CSP 口径（v0.2.0 已随入库冒烟回归验证）。 */
  const CSP = [
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",                       // 控制台含内联启动脚本（App.boot/壳注入）
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    "img-src 'self' data:",
    "connect-src 'self' http://127.0.0.1:* ws://127.0.0.1:* http://localhost:* ws://localhost:*",
  ].join("; ");
  protocol.handle("app", async request => {
    try {
      const u = new URL(request.url);
      /* app://setup/index.html → 首启引导页（项目根 setup.html） */
      if (u.hostname === "setup") {
        const p = path.join(ROOT, "setup.html");
        const html = fs.readFileSync(p, "utf8");
        return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8" } });
      }
      let p = decodeURIComponent(u.pathname || "/");
      if (p === "/" || p === "") p = "/index.html";
      /* v0.2.0：console 目录动态解析（仓库 console/ 优先，内置副本回退）——
         每请求解析以覆盖 repoDir 热更新；防目录穿越按当前解析基线收敛 */
      const src = getConsoleSource();
      if (!src) return new Response(CONSOLE_MISSING_HTML, { status: 200, headers: { "Content-Type": "text/html; charset=utf-8" } });
      const consoleDir = src.dir;
      const fsPath = path.normalize(path.join(consoleDir, p));
      const normDir = path.normalize(consoleDir) + path.sep;
      if (!fsPath.startsWith(normDir)) return new Response("forbidden", { status: 403 });   // 防目录穿越（按当前解析基线）
      if (fsPath === path.join(consoleDir, "index.html")) {
        let html = fs.readFileSync(fsPath, "utf8");
        const anchor = "<script>App.boot();</script>";
        if (html.includes(anchor)) html = html.replace(anchor, `${bootScript(BOOT_MODE)}\n${anchor}`);
        else html = html.replace("</body>", `${bootScript(BOOT_MODE)}\n</body>`);
        /* 文档级 CSP（meta）：Electron 安全告警读文档 CSP；响应头兜底 */
        const metaCsp = `<meta http-equiv="Content-Security-Policy" content="${CSP.replace(/"/g, "&quot;")}">`;
        html = html.includes("<head>") ? html.replace("<head>", `<head>${metaCsp}`) : html;
        if (process.env.DESKTOP_DEBUG_SERVE) { try { fs.writeFileSync("/tmp/served.html", html); } catch (e) {} }
        return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": CSP } });
      }
      const body = fs.readFileSync(fsPath);
      return new Response(body, { headers: { "Content-Type": MIME[path.extname(fsPath).toLowerCase()] || "application/octet-stream" } });
    } catch (e) {
      return new Response("not found", { status: 404 });
    }
  });
}

/* ---------- 图标（scripts/gen-icon.js 生成；缺文件时兜底空图） ---------- */
function iconFile(name) {
  const p = path.join(ROOT, "icons", name);
  return fs.existsSync(p) ? p : null;
}

/* ---------- 主窗口：Live 直接 loadURL 后端 console（§二），演示走 app:// ---------- */
function createWindow(mode) {
  const win2 = new BrowserWindow({
    width: cfg.windowWidth, height: cfg.windowHeight,
    minWidth: cfg.windowMinWidth, minHeight: cfg.windowMinHeight,
    icon: iconFile("icon.png") || undefined,
    autoHideMenuBar: true,
    backgroundColor: "#0B1120",
    title: "QQ Agent 桌面控制台",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
      /* live 页面与后端同源（后端 CORS 全开，§二）——保持默认安全配置 */
      additionalArguments: ["--desktop-backend=" + cfg.backendUrl, "--desktop-mode=" + mode],
    },
  });
  if (mode === "live" && cfg.consoleFromBackend) {
    win2.loadURL(cfg.backendUrl.replace(/\/$/, "") + "/console/index.html");
  } else {
    win2.loadURL("app://console/index.html");
  }
  win2.on("close", e => {
    /* 关闭到托盘（真正退出走托盘「退出」） */
    if (!quitting && tray) { e.preventDefault(); win2.hide(); }
  });
  win2.once("ready-to-show", () => { if (!cfg.startMinimized && !IS_SMOKE) win2.show(); });
  win2.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);            // 外链走系统浏览器
    return { action: "deny" };
  });
  return win2;
}

/* ---------- 首启引导窗（repoDir 未配置且无后端时） ---------- */
function showSetupWindow(prefill) {
  if (setupWin && !setupWin.isDestroyed()) { setupWin.show(); return; }
  setupWin = new BrowserWindow({
    width: 680, height: 520, resizable: false, modal: false,
    icon: iconFile("icon.png") || undefined,
    autoHideMenuBar: true,
    backgroundColor: "#0B1120",
    title: "QQ Agent 桌面壳 · 仓库与后端配置",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true, nodeIntegration: false, spellcheck: false,
    },
  });
  setupWin.loadURL("app://setup/index.html" + (prefill ? `?repo=${encodeURIComponent(prefill)}` : ""));
  setupWin.once("ready-to-show", () => setupWin.show());
  setupWin.on("closed", () => { setupWin = null; });
}

/* ---------- 原生通知（不可用环境静默） ---------- */
function notify(title, body) {
  try {
    if (!Notification.isSupported()) return;
    new Notification({ title, body, icon: iconFile("icon.png") || undefined }).show();
  } catch (e) { /* 环境缺 dbus 等：静默 */ }
}

/* ---------- v0.3.0 联动启动事件 → 托盘 tooltip / 原生通知 ----------
   program.status 主题同形态事件：进入 running → 原生通知；异常态 → 提醒；
   program.progress 主题同形态事件：只更新 tooltip（防通知刷屏）；
   done 收口：有变化才通知汇总，tooltip 复位；manual：完全静默（一次性日志）。 */
function setTrayTooltip(text) { try { if (tray) tray.setToolTip(text); } catch (e) {} }

function onBootEvent(e) {
  switch (e.type) {
    case "triggered":
      log(`[boot] 已触发后端联动启动（POST ${cfg.bootPath} → HTTP ${e.httpStatus}），进度观测中`);
      setTrayTooltip("QQ Agent 桌面控制台 · 联动启动中…");
      refreshTray();
      break;
    case "manual":
      /* 静默降级：控制台程序页手动按钮照常可用，不打扰用户 */
      log(`[boot] 联动启动未启用（${e.note}）——控制台「第三方程序」页手动按钮照常可用`);
      break;
    case "status": {
      const name = e.name || e.id;
      if (e.status === "running" && e.prevStatus !== "running") {
        notify("联动启动", `${name} 已运行${e.pid ? `（pid ${e.pid}）` : ""}`);
      } else if (typeof e.status === "string" && /^(error|failed|crash)/.test(e.status)) {
        notify("联动启动", `${name} 启动异常（${e.status}）`);
      }
      setTrayTooltip(`QQ Agent 桌面控制台 · 联动启动中（${e.running}/${e.total} 运行）`);
      refreshTray();
      break;
    }
    case "progress": {
      const pct = e.percent != null ? ` ${e.percent}%` : "";
      const phase = e.phase || "";
      setTrayTooltip(`QQ Agent 桌面控制台 · 联动启动中：${e.name || e.id} ${phase}${pct}`.trim());
      break;
    }
    case "done":
      if (e.changed > 0) {
        notify("联动启动完成", `${e.running}/${e.total} 个程序运行中${e.note === "watchdog" ? "（观测超时收口）" : ""}`);
      }
      setTrayTooltip("QQ Agent 桌面控制台");
      refreshTray();
      break;
  }
}

/* ---------- 后端模式装载（首启/托盘重连共用） ---------- */
function loadMode(mode) {
  BOOT_MODE = mode === "live" ? "live" : "demo";
  if (!win || win.isDestroyed()) {
    win = createWindow(BOOT_MODE);
    if (IS_SMOKE) wireSmoke(win);
  } else {
    if (BOOT_MODE === "live" && cfg.consoleFromBackend) win.loadURL(cfg.backendUrl.replace(/\/$/, "") + "/console/index.html");
    else win.loadURL("app://console/index.html");
    win.show();
  }
  if (tray) refreshTray();
}

/* ---------- 生命周期决策 → 窗口装载 ---------- */
async function bootFromDecision(decision, opts = {}) {
  lastDecision = decision;
  const mode = decision.mode === "attached" || decision.mode === "spawned" ? "live" : "demo";
  log(`[desktop] 后端决策：${decision.mode}`);

  if (mode === "live") {
    if (decision.mode === "spawned") notify("QQ Agent 桌面控制台", "后端已拉起并就绪（Live 模式）");
    else log("[desktop] 附加运行中的后端（Live 模式）");
    startWatcher();
    maybeTriggerBoot();            // v0.3.0：后端就绪 → 触发第三方程序自动编排（失败静默降级）
  } else if (!opts.fromSetup) {
    /* 降级路径的明确提示（§四：锁占/端口占勿含糊） */
    if (decision.mode === "lock-held") {
      notify("后端实例锁被占", decision.message || "已有 ME_Agent 实例在别的端口/host 运行，壳进入演示模式。");
      if (!IS_SMOKE) dialog.showMessageBox({ type: "warning", title: "后端实例锁被占（InstanceLockError）",
        message: decision.message || "已有 ME_Agent 实例在运行（可能在别的端口/host）。",
        detail: "壳已进入演示模式，不再重试拉起。可找到现有实例后点托盘「重连后端（Live）」附加。",
        buttons: ["知道了，先进入演示模式"] }).catch(() => {});
    } else if (decision.mode === "port-occupied") {
      notify("端口被非 ME_Agent 服务占用", decision.message || "");
      if (!IS_SMOKE) dialog.showMessageBox({ type: "warning", title: "端口被占用（非 ME_Agent）",
        message: decision.message || `端口 ${cfg.backendUrl} 上有别的服务响应。`,
        detail: "为避免误附加，壳不会拉起后端。请检查端口或修改 config.json 的 backendUrl 后重启壳。",
        buttons: ["知道了，先进入演示模式"] }).catch(() => {});
    } else if (decision.mode === "spawn-failed") {
      notify("后端拉起失败", (decision.message || "") + (decision.tail ? "" : ""));
    } else if (decision.reason === "missing-venv") {
      /* v0.2.0：已识别仓库（推导/配置）但 .venv 未就绪（首次部署未跑过 .bat）——
         首启引导窗已预填路径并给出部署指引；此处通知补口径说明 */
      notify("已识别 ME_Agent 仓库，但 .venv 未就绪", "首次部署尚未完成：请先运行仓库内 deploy/scripts/启动ME_Agent.bat（自动建 .venv 装依赖），完成后在引导窗点「保存并拉起后端」。已先进入演示模式。");
    } else if (cfg.demoNoBackendNotify) {
      notify("QQ Agent 桌面控制台", "未检测到后端，已进入演示模式（Mock 数据）");
    }
  }
  loadMode(mode);
}

/* ---------- 托盘 ---------- */
function statusLabel() {
  if (!lastDecision) return "状态：启动中…";
  switch (lastDecision.mode) {
    case "attached": return "后端：运行中（附加，共享实例）";
    case "spawned": return "后端：运行中（本壳拉起）";
    case "lock-held": return "后端：实例锁被占（勿重试拉起）";
    case "port-occupied": return "后端：端口被非 ME_Agent 服务占用";
    case "spawn-failed": return "后端：拉起失败（详见日志）";
    default: return "后端：未运行（演示模式）";
  }
}

function bootLabel() {
  /* v0.3.0 联动启动托盘状态行：仅在实际编排时出现（manual 静默降级不占菜单） */
  if (!bootOrch || !cfg.bootEnabled) return null;
  const s = bootOrch.summary();
  if (bootOrch.state === "watching" || bootOrch.state === "stopped") {
    return `联动启动：进行中（${s.running}/${s.total} 运行）`;
  }
  if (bootOrch.state === "done" && s.note !== "disabled") {
    return `联动启动：${s.statusEvents > 0 ? `完成（${s.running}/${s.total} 运行）` : "完成（程序均已运行）"}`;
  }
  return null;
}

function createTray() {
  const iconPath = iconFile("tray.png");
  if (!iconPath) return null;
  try {
    const t = new Tray(nativeImage.createFromPath(iconPath));
    t.setToolTip("QQ Agent 桌面控制台");
    const rebuild = () => {
      const items = [
        { label: "显示窗口", click: () => { if (win && !win.isDestroyed()) { win.show(); win.focus(); } else loadMode(BOOT_MODE); } },
        { type: "separator" },
        { label: statusLabel(), enabled: false },
      ];
      const bl = bootLabel();
      if (bl) items.push({ label: bl, enabled: false });
      items.push(
        { label: "重连后端（Live）", click: () => reconnectBackend() },
        { label: "切换演示模式（Mock）", click: () => loadMode("demo") },
        { label: "打开后端页面", click: () => shell.openExternal(cfg.backendUrl), enabled: lastDecision && (lastDecision.mode === "attached" || lastDecision.mode === "spawned") },
        { type: "separator" },
        { label: "仓库与后端配置…", click: () => showSetupWindow(cfg.repoDir) },
        { label: "开机自启", type: "checkbox", checked: cfg.openAtLogin, click: mi => setOpenAtLogin(mi.checked) },
        { label: "退出时带走后端进程", type: "checkbox", checked: cfg.takeBackendDownOnExit, click: mi => {
          cfg.takeBackendDownOnExit = mi.checked;
          backend.takeDown = mi.checked;
          config.saveLocal({ takeBackendDownOnExit: mi.checked });
        } },
        { type: "separator" },
        { label: "退出", click: () => { quitting = true; app.quit(); } },
      );
      t.setContextMenu(Menu.buildFromTemplate(items));
    };
    rebuild();
    t.on("click", () => { if (win && !win.isDestroyed()) { win.isVisible() ? win.focus() : win.show(); } });
    t._rebuild = rebuild;
    return t;
  } catch (e) {
    log("[desktop] 托盘不可用（无系统托盘环境），继续无托盘运行：" + e.message);
    return null;
  }
}
function refreshTray() { if (tray && tray._rebuild) tray._rebuild(); }

function setOpenAtLogin(on) {
  cfg.openAtLogin = !!on;
  try { app.setLoginItemSettings({ openAtLogin: cfg.openAtLogin }); } catch (e) {}
  config.saveLocal({ openAtLogin: cfg.openAtLogin });
}

/* ---------- 重连（托盘）：双探 → 附加或按配置拉起 ---------- */
async function reconnectBackend() {
  log("[desktop] 手动重连后端…");
  const decision = await backend.ensureRunning({ spawnTimeoutMs: cfg.spawnTimeoutMs });
  await bootFromDecision(decision);
}

/* ---------- 后端巡检（30s：状态变化 → 通知 + 托盘刷新） ---------- */
function startWatcher() {
  clearInterval(backend._watchTimer);
  backend._watchTimer = setInterval(async () => {
    if (IS_SMOKE || quitting) return clearInterval(backend._watchTimer);
    const p = await backend.probe(1500);
    const healthy = p.ok && p.isMeAgent;
    if (healthy !== backend._lastHealthy) {
      backend._lastHealthy = healthy;
      refreshTray();
      notify("后端状态变化", healthy ? "后端恢复在线（program.status 巡检通过）" : "后端离线（health 探测失败）——页面数据可能停止刷新");
    }
  }, 30000);
}

/* ---------- 首启引导 IPC（setup 页 → 主进程） ---------- */
ipcMain.handle("desktop:setup-save", async (_e, data) => {
  const repoDir = String((data && data.repoDir) || "").trim();
  const backendUrl = String((data && data.backendUrl) || "").trim() || cfg.backendUrl;
  if (!repoDir) return { ok: false, message: "请填写 ME_Agent 仓库根目录（如 D:\\ME_Agent）" };
  const normalized = path.resolve(repoDir);
  /* 校验仓库（.venv/python -m app 就绪度） */
  const insp = (() => {
    const mgr = new BackendManager({ repoDir: normalized, log: () => {} });
    return mgr.inspectRepo();
  })();
  if (!insp.ok) return { ok: false, message: insp.reason, hint: insp.hint || "" };
  /* 写配置（config.local.json）并热应用到当前 manager */
  config.saveLocal({ repoDir: normalized, backendUrl });
  cfg.repoDir = normalized;
  cfg.repoDirSource = "config";          // 手动覆盖（显式配置自此恒优先于自动推导）
  if (backendUrl !== cfg.backendUrl) cfg.backendUrl = backendUrl;
  backend.repoDir = normalized;
  backend.url = cfg.backendUrl;
  log(`[desktop] 首启配置已保存：repoDir=${normalized} backendUrl=${cfg.backendUrl}`);
  /* 立即拉起/附加 */
  const decision = await backend.ensureRunning({ spawnTimeoutMs: cfg.spawnTimeoutMs });
  const mode = decision.mode === "attached" || decision.mode === "spawned" ? "live" : null;
  if (!mode) return { ok: false, mode: decision.mode, message: decision.message || `后端未就绪（${decision.mode}），请按提示处理后重试。`, tail: (decision.tail || []).slice(-6) };
  await bootFromDecision(decision, { fromSetup: true });
  if (setupWin && !setupWin.isDestroyed()) setupWin.close();
  return { ok: true, mode: decision.mode };
});

ipcMain.on("desktop:setup-demo", () => {
  if (setupWin && !setupWin.isDestroyed()) setupWin.close();
  loadMode("demo");
  if (cfg.demoNoBackendNotify) notify("QQ Agent 桌面控制台", "已进入演示模式（Mock 数据）——随时可从托盘「重连后端（Live）」");
});

/* ---------- 冒烟模式：收集页面 JS 错误 → JSON 汇总 → 退出码 ---------- */
const smoke = { startedAt: Date.now(), pageErrors: [], consoleErrors: [], electronAdvisories: 0, loaded: false, backend: null, finalMode: null, boot: null };
function wireSmoke(w) {
  w.webContents.on("console-message", (_e, level, message, line, sourceId) => {
    if (level < 2) return;
    const msg = String(message);
    const src = String(sourceId);
    /* Electron 沙箱预载的 CSP 告警：其文档级检查时点早于本壳注入的响应头/meta CSP 生效，
       为已知环境噪音（CSP 已就位）——计数但不计入 JS 错误门禁 */
    if (src.includes("electron/js2c/") && /Content-Security-Policy|Security Warning/i.test(msg)) {
      smoke.electronAdvisories += 1;
      return;
    }
    smoke.consoleErrors.push({ level, message: msg.slice(0, 300), line, source: src.slice(0, 120) });
  });
  w.webContents.on("did-finish-load", () => { smoke.loaded = true; });
  w.webContents.on("did-fail-load", (_e, code, desc, url) => {
    smoke.pageErrors.push({ type: "did-fail-load", code, desc: String(desc), url: String(url).slice(0, 160) });
  });
  w.webContents.on("render-process-gone", (_e, details) => {
    smoke.pageErrors.push({ type: "render-process-gone", reason: details.reason });
  });
}

/* ---------- 启动 ---------- */
app.whenReady().then(async () => {
  registerAppProtocol();

  /* 1) 后端生命周期决策（冒烟 --mode 可跳过探测强制模式） */
  let decision;
  if (IS_SMOKE && MODE_ARG === "demo") decision = { mode: "demo", probe: { ok: false, error: "smoke-forced-demo" } };
  else if (IS_SMOKE && MODE_ARG === "live") decision = { mode: "attached", probe: { ok: true, forced: true } };
  else {
    decision = await backend.ensureRunning({ spawnTimeoutMs: cfg.spawnTimeoutMs });
  }
  smoke.backend = decision;

  /* 2) 首启引导（v0.2.0 口径）：后端未运行进入演示态时——
     · reason=no-command：仓库根完全未识别（推导未命中且无配置）→ 引导窗手填；
     · reason=missing-venv：仓库根已识别（多为自动推导）但 .venv 未建（首次部署
       未完成）→ 引导窗预填路径 + .bat 部署指引（跳过手填）；
     推导成功且 venv 就绪时不会走到这里（直接拉起/附加，跳过引导窗）。
     后端已运行（附加/本壳拉起）不出引导窗。 */
  const needSetup = !IS_SMOKE && decision.mode === "demo"
    && (decision.reason === "no-command" || decision.reason === "missing-venv");
  if (needSetup) {
    showSetupWindow(cfg.repoDir);
    await bootFromDecision(decision, { fromSetup: true });
  } else {
    await bootFromDecision(decision);
  }

  /* 3) 托盘 / 自启 */
  if (cfg.openAtLogin) { try { app.setLoginItemSettings({ openAtLogin: true }); } catch (e) {} }
  if (!IS_SMOKE) tray = createTray();

  /* 4) 冒烟收口：12s 后输出 JSON 并退出（0 = 零 JS 错误且完成加载；v0.3.0 +2s
     为联动启动观测留窗——dummy 编排仿真 4.8s 内收口，见 test/dummy-backend.js） */
  if (IS_SMOKE) {
    const mode = BOOT_MODE;
    setTimeout(async () => {
      smoke.elapsedMs = Date.now() - smoke.startedAt;
      smoke.finalMode = mode;
      /* v0.2.0：入库适配环境快照（repoDir 推导链 + 演示 console 来源落位验证） */
      {
        const src = getConsoleSource();
        smoke.env = { repoDir: cfg.repoDir, repoDirSource: cfg.repoDirSource, consoleSource: src && src.source, consoleDir: src && src.dir, platform: process.platform };
      }
      /* 注入生效性验证：页面内 API.mode / API.baseUrl（live 注入是否真正落位） */
      try {
        const w = BrowserWindow.getAllWindows().find(x => !x.isDestroyed() && x !== setupWin);
        if (w) {
          const ps = await w.webContents.executeJavaScript(
            "JSON.stringify({mode: window.API && API.mode, baseUrl: window.API && API.baseUrl, booted: !!(window.App && App.pages), href: location.href, scripts: document.querySelectorAll('script').length, mainKids: document.getElementById('main') ? document.getElementById('main').childElementCount : -1, hasDesktop: typeof window.desktop})"
          );
          smoke.pageState = JSON.parse(ps);
        }
      } catch (e) { smoke.pageStateError = String(e).slice(0, 120); }
      /* v0.3.0：联动启动观测留痕（trigger→watch→done / manual 降级形态） */
      smoke.boot = bootOrch.summary();
      const ok = smoke.loaded && smoke.pageErrors.length === 0 && smoke.consoleErrors.length === 0;
      console.log("SMOKE_RESULT " + JSON.stringify(smoke, null, 2));
      bootOrch.stop();
      await backend.shutdown();
      app.exit(ok ? 0 : 1);
    }, 12000);
  }
});

app.on("window-all-closed", () => {
  /* 关闭到托盘语义：有托盘时不退出；无托盘环境（纯窗口）遵循平台惯例退出 */
  if (!tray || process.platform === "darwin") return;
});

app.on("before-quit", async e => {
  if (quitting || IS_SMOKE) return;
  quitting = true;
});

app.on("will-quit", async e => {
  /* 退出时按配置带走本壳拉起的后端进程（默认保持运行 §四.4） */
  bootOrch.stop();                                   // v0.3.0：联动启动观测随退出收口
  if (backend.ownsProcess()) {
    e.preventDefault();
    await backend.shutdown();
    app.exit(0);
  }
});
