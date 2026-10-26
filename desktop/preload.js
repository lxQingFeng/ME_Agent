/* ============================================================
   preload.js — 渲染层桌面能力接缝（contextIsolation 安全注入）
   ------------------------------------------------------------
   1) window.desktop：桌面能力 API（控制台零依赖；后续控制台如需桌面能力
      （原生通知/打开外链/窗口控制），统一经此调用，走「壳配合需求」流程发群提出）。
   2) live 注入（契约 §二，控制台零改动）：页面由后端服务（origin === backendUrl）
      时，在页面脚本执行前写 localStorage「console.api.mode=live」（控制台
      restoreMode 既有通道）；自定义端口（≠默认 8000）时再校准 API.baseUrl/
      wsUrl——控制台 api.js 装载赋值 window.API 时经 defineProperty 拦截注入。
   ============================================================ */
"use strict";

const { contextBridge, ipcRenderer } = require("electron");

/* ---- live 注入：读取主进程经 additionalArguments 传入的后端地址 ---- */
const BACKEND_ARG = "--desktop-backend=";
const backendUrl = (() => {
  const a = process.argv.find(x => x.startsWith(BACKEND_ARG));
  return a ? a.slice(BACKEND_ARG.length) : "";
})();
try {
  if (backendUrl && location.origin === new URL(backendUrl).origin) {
    localStorage.setItem("console.api.mode", "live");        // 控制台既有配置通道（restoreMode）
    if (backendUrl !== "http://127.0.0.1:8000") {
      /* 自定义端口：控制台默认 8000 → 拦截 window.API 赋值校准 baseUrl/wsUrl */
      localStorage.setItem("console.api.baseUrl", backendUrl);
      const wsUrl = backendUrl.replace(/^http/, "ws") + "/ws";
      let _api = undefined;
      Object.defineProperty(window, "API", {
        configurable: true, enumerable: true,
        get() { return _api; },
        set(v) {
          _api = v;
          try { if (v && typeof v === "object") { v.baseUrl = backendUrl; v.wsUrl = wsUrl; } } catch (e) {}
        },
      });
    }
  }
} catch (e) { /* localStorage 不可用等场景静默：控制台自行降级 */ }

contextBridge.exposeInMainWorld("desktop", {
  available: true,
  platform: process.platform,          // win32 / darwin / linux
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
  /* 首启引导页专用（app://setup）：保存配置并拉起 / 进入演示 */
  setup: {
    save: data => ipcRenderer.invoke("desktop:setup-save", data),
    demo: () => ipcRenderer.send("desktop:setup-demo"),
  },
});
