/* ============================================================
   api.js — API 对接层（唯一后端耦合点）
   ------------------------------------------------------------
   设计原则：
   1. 页面代码只调 API.*，永远不直接 fetch —— 后端契约变更时只改本文件。
   2. mode = "mock"：内置演示数据（当前默认）。
      mode = "live"：请求 Console 后端（FastAPI，契约待全栈开发清单定稿）。
   3. 所有 live 路径集中在 LIVE_ROUTES，便于按最终契约批量调整。
   4. live 不可用/超时 → 统一 reject，页面自行降级提示。
   ============================================================ */
"use strict";

const API = {
  mode: "mock",                 // "mock" | "live"
  baseUrl: "http://127.0.0.1:8000", // 后端 Console 地址（对接时改这里或用设置页）
  wsUrl: "ws://127.0.0.1:8000/ws", // 预留：实时推送（世界心跳/消息）
  timeout: 8000,

  /* live 路由表 —— 对接清单定稿后以此为准逐条核对 */
  LIVE_ROUTES: {
    worldState:      "/api/v1/world/state",            // GET  WorldState 快照
    worldTimeline:   "/api/v1/world/transitions",      // GET  最近状态转移
    conversations:   "/api/v1/chat/conversations",     // GET  会话列表
    messages:        "/api/v1/chat/conversations/{id}/messages", // GET 消息流
    sendMessage:     "/api/v1/chat/conversations/{id}/send",     // POST 测试发送（管理口）
    plugins:         "/api/v1/plugins",                // GET  插件列表
    pluginToggle:    "/api/v1/plugins/{id}/enabled",   // PUT  {enabled:bool}
    programs:        "/api/v1/programs",               // GET  第三方程序状态
    programAction:   "/api/v1/programs/{id}/{action}", // POST start|stop|restart|update|install
    programLogs:     "/api/v1/programs/{id}/logs",     // GET  进程日志尾部
    ttsConfigGet:    "/api/v1/tts/config",             // GET  当前 TTS 配置（密钥脱敏）
    ttsConfigSave:   "/api/v1/tts/config",             // PUT  保存（头/体原文落盘）
    ttsModels:       "/api/v1/tts/models",             // POST {endpoint,key} → 代理拉取模型列表
    ttsVoices:       "/api/v1/tts/voices",             // POST {endpoint,key,model} → 官方音色列表
    ttsSynthesize:   "/api/v1/tts/synthesize",         // POST {text,voice} → {audio_url}
    ttsCloneUpload:  "/api/v1/tts/clone/audio",        // POST multipart 克隆音频上传
    diagnosticsExport: "/api/v1/diagnostics/export",   // GET  脱敏诊断包（密钥一律 hasXxx 布尔指纹，无原文）
  },

  /* ---------- 基础设施 ---------- */
  _live: true, // 占位以兼容未来标志
  async _fetch(route, options = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), this.timeout);
    try {
      const res = await fetch(this.baseUrl + route, {
        headers: { "Content-Type": "application/json" },
        ...options,
        signal: ctrl.signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } finally { clearTimeout(timer); }
  },
  _mockify(fn, delay = 260) {
    // mock 统一注入小延迟，让加载态真实可感；立即执行并返回 Promise
    return U.sleep(delay + Math.random() * 240).then(() => Promise.resolve(typeof fn === "function" ? fn() : fn));
  },
  setMode(mode) {
    this.mode = mode === "live" ? "live" : "mock";
    const modeName = $("#modeName"), modeDot = $("#svcMode .svc-dot");
    if (modeName) modeName.textContent = this.mode === "live" ? "Live" : "Mock";
    if (modeDot) modeDot.dataset.state = this.mode === "live" ? "ok" : "mock";
    localStorage.setItem("console.api.mode", this.mode);
  },

  /* ================= 世界心跳 ================= */
  getWorldState() {
    if (this.mode === "live") return this._fetch(this.LIVE_ROUTES.worldState);
    return this._mockify(() => JSON.parse(JSON.stringify(MOCK.worldState)));
  },
  getWorldTimeline() {
    if (this.mode === "live") return this._fetch(this.LIVE_ROUTES.worldTimeline);
    return this._mockify(() => JSON.parse(JSON.stringify(MOCK.transitions)));
  },

  /* ================= 对话流 ================= */
  getConversations() {
    if (this.mode === "live") return this._fetch(this.LIVE_ROUTES.conversations);
    return this._mockify(() => JSON.parse(JSON.stringify(MOCK.conversations)));
  },
  getMessages(convId) {
    if (this.mode === "live") return this._fetch(this.LIVE_ROUTES.messages.replace("{id}", convId));
    return this._mockify(() => (MOCK.messages[convId] || []).slice());
  },
  sendMessage(convId, text) {
    if (this.mode === "live") {
      return this._fetch(this.LIVE_ROUTES.sendMessage.replace("{id}", convId), {
        method: "POST", body: JSON.stringify({ text }),
      });
    }
    return this._mockify(() => ({ ok: true, echo: text, note: "mock 模式：未真正下发（对接后端后经 OneBot 通道发出）" }), 500);
  },

  /* ================= 插件技能 ================= */
  getPlugins() {
    if (this.mode === "live") return this._fetch(this.LIVE_ROUTES.plugins);
    return this._mockify(() => JSON.parse(JSON.stringify(MOCK.plugins)));
  },
  togglePlugin(id, enabled) {
    if (this.mode === "live") {
      return this._fetch(this.LIVE_ROUTES.pluginToggle.replace("{id}", id), {
        method: "PUT", body: JSON.stringify({ enabled }),
      });
    }
    return this._mockify(() => ({ ok: true, id, enabled }));
  },

  /* ================= 第三方程序 ================= */
  getPrograms() {
    if (this.mode === "live") return this._fetch(this.LIVE_ROUTES.programs);
    return this._mockify(() => JSON.parse(JSON.stringify(MOCK.programs)));
  },
  programAction(id, action) {
    if (this.mode === "live") {
      return this._fetch(this.LIVE_ROUTES.programAction.replace("{id}", id).replace("{action}", action), { method: "POST" });
    }
    // mock：本地演化状态，让操作可感
    return this._mockify(() => {
      const p = MOCK.programs.find(x => x.id === id);
      if (!p) throw new Error("程序不存在");
      if (action === "start" || action === "restart") {
        p.status = "running";
        p.proc = { pid: 1000 + Math.floor(Math.random() * 4000), mem_mb: 120 + Math.floor(Math.random() * 300), cpu: "2.1%", uptime: "刚刚", restarts: p.proc.restarts + (action === "restart" ? 1 : 0) };
      } else if (action === "stop") {
        p.status = "stopped";
        p.proc = { pid: null, mem_mb: 0, cpu: "—", uptime: "—", restarts: p.proc.restarts };
      } else if (action === "update") {
        p.version = p.latest;
        p.status = "running";
      } else if (action === "install") {
        p.status = "running"; p.version = p.latest;
        p.proc = { pid: 1000 + Math.floor(Math.random() * 4000), mem_mb: 150, cpu: "1.5%", uptime: "刚刚", restarts: 0 };
      } else if (action === "one-click") {
        // 一键接入（可选增强）：下载安装 + 配置 + 启动一次完成
        p.status = "running"; p.version = p.latest;
        p.proc = { pid: 1000 + Math.floor(Math.random() * 4000), mem_mb: 120, cpu: "1.2%", uptime: "刚刚", restarts: 0 };
      }
      return { ok: true, id, action, status: p.status };
    }, 900);
  },

  /* ================= 诊断导出（脱敏） ================= */
  /* 诊断包 JSON：generated_at / uptime / 组件健康 / 近期错误摘要 / 配置密钥指纹。
     密钥一律为 hasXxx:true 布尔指纹形态；live 由后端按 SECRET_KEY_PATTERN 脱敏后返回。 */
  exportDiagnostics() {
    if (this.mode === "live") return this._fetch(this.LIVE_ROUTES.diagnosticsExport);
    return this._mockify(() => JSON.parse(JSON.stringify(MOCK.diagnostics)), 600);
  },

  /* ================= TTS ================= */
  /* 拉取模型列表：live 由后端代理（规避 CORS），mock 返回演示列表 */
  ttsGetModels(endpoint, apiKey) {
    if (this.mode === "live") {
      return this._fetch(this.LIVE_ROUTES.ttsModels, {
        method: "POST", body: JSON.stringify({ endpoint, key: apiKey }),
      });
    }
    return this._mockify(() => ({ object: "list", data: JSON.parse(JSON.stringify(MOCK.ttsModels)) }), 700);
  },
  ttsGetVoices(endpoint, apiKey, model) {
    if (this.mode === "live") {
      return this._fetch(this.LIVE_ROUTES.ttsVoices, {
        method: "POST", body: JSON.stringify({ endpoint, key: apiKey, model }),
      });
    }
    return this._mockify(() => JSON.parse(JSON.stringify(MOCK.ttsVoices)), 500);
  },
  /* 合成：live 返回 {audio_url,...}；mock 由前端用 speechSynthesis 本地演示 */
  ttsSynthesize(payload) {
    if (this.mode === "live") {
      return this._fetch(this.LIVE_ROUTES.ttsSynthesize, {
        method: "POST", body: JSON.stringify(payload),
      });
    }
    return this._mockify(() => ({ ok: true, engine: "browser-speech-synthesis", note: "演示模式：浏览器本地语音（实际以服务端返回音频为准）" }), 1400);
  },
  ttsSaveConfig(config) {
    if (this.mode === "live") {
      return this._fetch(this.LIVE_ROUTES.ttsConfigSave, {
        method: "PUT", body: JSON.stringify(config),
      });
    }
    return this._mockify(() => ({ ok: true, saved_at: new Date().toISOString() }), 450);
  },
  ttsUploadCloneAudio(file, meta) {
    if (this.mode === "live") {
      const fd = new FormData();
      fd.append("file", file);
      Object.entries(meta || {}).forEach(([k, v]) => fd.append(k, v));
      return fetch(this.baseUrl + this.LIVE_ROUTES.ttsCloneUpload, { method: "POST", body: fd }).then(r => r.json());
    }
    return this._mockify(() => ({ ok: true, id: U.uid("clone"), name: file.name, size: file.size }), 1200);
  },
};

window.API = API;

/* 启动时恢复模式选择 */
(function restoreMode() {
  const saved = localStorage.getItem("console.api.mode");
  if (saved === "live") API.mode = "live";
})();
