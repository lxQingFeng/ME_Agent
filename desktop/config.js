/* ============================================================
   config.js — 桌面壳配置（契约接缝：全栈开发「桌面壳对接契约」发群后校准此处）
   ------------------------------------------------------------
   读取顺序：defaults ← config.json（随仓库，提交默认值）← config.local.json（托盘
   用户操作落盘，gitignore）。改配置不需要改任何代码。
   repoDir 解析优先级（v0.2.0 入库适配）：
     1) 显式配置（config.json / config.local.json / 首启引导窗落盘）——恒优先；
     2) 自动推导：壳位于 ME_Agent 仓库 desktop/ 子目录时，以壳目录上级为仓库根
        候选（.venv / app/__main__.py 等仓库特征 ≥2 且含强特征，见 backend-manager
        deriveRepoDir）；推导成功 repoDirSource="auto"，首启引导跳过手填；
     3) 都没有 → 空值，后端未运行时弹首启引导窗手填。
   待校准项（对接契约定稿后只改 config.json）：
   - backendUrl / backendHealthPath：监听地址、端口、健康检查端点
   - backendStartCommand / backendStartCwd：Windows 后端启动方式（脚本/命令/工作目录）
   - 实例锁/单例语义：壳侧已做应用级单实例锁；后端「已运行则附加」由健康探测实现，
     探测不到且配置了拉起命令才 spawn（不抢已有实例）。
   ============================================================ */
"use strict";

const fs = require("fs");
const path = require("path");
const { deriveRepoDir } = require("./backend-manager");

/* 项目根 = 本文件所在目录（config.js 位于 qq-agent-desktop/ 根；
   入库后即 ME_Agent 仓库 desktop/ 子目录） */
const ROOT = __dirname;

const DEFAULTS = {
  /* 后端连接（已按「桌面壳对接契约 v1」校准：默认 8000，双探端点 §三） */
  backendUrl: "http://127.0.0.1:8000",
  healthPath: "/api/v1/system/health",
  statusPath: "/api/v1/system/status",

  /* ME_Agent 仓库根（拉起命令推导 + 演示模式 console 引用）。
     空 = 未配置：入库场景自动推导（desktop/ 上级），推导不到则首启引导手填 */
  repoDir: "",
  backendStartCommand: "",
  backendStartCwd: "",

  /* 演示模式 console 来源（v0.2.0）：true = 优先引用仓库根 console/（入库后单一
     副本，随仓库 git pull 更新）；仓库无 console/ 或独立分发时回退内置副本 */
  preferRepoConsole: true,

  /* 拉起（§一）：命令留空 = 由 repoDir 推导 .venv python -m app */
  spawnTimeoutMs: 60000,
  spawnArgs: ["--host", "127.0.0.1"],

  /* Live 控制台来源（§二）：true = 直接加载 {backendUrl}/console/index.html */
  consoleFromBackend: true,

  /* 退出（§四.4）：默认保持后端运行（false）；true = 仅带走本壳拉起的实例 */
  takeBackendDownOnExit: false,

  /* 托盘/窗口 */
  openAtLogin: false,
  startMinimized: false,
  windowWidth: 1280,
  windowHeight: 860,
  windowMinWidth: 940,
  windowMinHeight: 640,

  /* 演示 */
  demoNoBackendNotify: true,              // 无后端进演示模式时发原生通知
};

function readJsonSafe(p) {
  try { return JSON.parse(fs.readFileSync(p, "utf8")); } catch (e) { return null; }
}

/* load(root)：root 供单测注入项目根（默认 ROOT）。
   返回 cfg 追加派生字段 repoDirSource（"config" 显式 / "auto" 自动推导 / "" 无）。 */
function load(root) {
  const dir = root || ROOT;
  const local = readJsonSafe(path.join(dir, "config.local.json")) || {};
  const committed = readJsonSafe(path.join(dir, "config.json")) || {};
  const cfg = Object.assign({}, DEFAULTS, committed, local);
  if (typeof cfg.spawnArgs === "string") cfg.spawnArgs = cfg.spawnArgs.split(/\s+/).filter(Boolean);

  cfg.repoDirSource = "";
  if (cfg.repoDir) {
    cfg.repoDirSource = "config";                 // 显式配置恒优先（手动覆盖能力）
  } else {
    const d = deriveRepoDir(dir);                 // 入库场景：壳目录上级 = 仓库根候选
    if (d.ok) {
      cfg.repoDir = d.repoDir;
      cfg.repoDirSource = "auto";
    }
  }
  return cfg;
}

/* 托盘切换项落 config.local.json（不动仓库默认） */
function saveLocal(patch) {
  const p = path.join(ROOT, "config.local.json");
  const cur = readJsonSafe(p) || {};
  Object.assign(cur, patch);
  try { fs.writeFileSync(p, JSON.stringify(cur, null, 2)); } catch (e) { /* 只读介质等场景静默 */ }
  return cur;
}

module.exports = { load, saveLocal, DEFAULTS, ROOT };
