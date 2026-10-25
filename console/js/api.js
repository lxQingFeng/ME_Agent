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
    programs:        "/api/v1/programs",               // GET  第三方程序状态（obsidian_sync 条目 health 含同步可观测数据）
    programAction:   "/api/v1/programs/{id}/{action}", // POST 统一动作 → {ok, id, action, result}
    programLogs:     "/api/v1/programs/{id}/logs",     // GET  进程日志尾部
    programCheckLatest: "/api/v1/programs/{id}/check-latest", // GET 检查更新（actions 数组内 "check-update" 按钮调它）
    obsidianSyncNow: "/api/v1/programs/obsidian_sync/sync",   // POST Obsidian 立即同步 → {ok, id, action, result}
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
  /* 动作类接口专用（V4）：业务失败（ok:false + error{code,message,hint}）也 resolve，
     仅网络/超时/非 JSON 才 reject —— 保证后端修复指引文案（hint）能完整回流到页面。 */
  async _fetchResult(route, options = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), this.timeout);
    try {
      const res = await fetch(this.baseUrl + route, {
        headers: { "Content-Type": "application/json" },
        ...options,
        signal: ctrl.signal,
      });
      const body = await res.json().catch(() => null);
      if (body) return body;
      throw new Error(`HTTP ${res.status}（响应非 JSON）`);
    } finally { clearTimeout(timer); }
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

  /* ================= 第三方程序（V4 · 契约已定稿） =================
     统一动作响应：POST /api/v1/programs/{id}/{action} → {ok, id, action, result}
       成功 → result = {status, version, detail…}（detail = 人话结果描述）
       失败 → result = {code?, error, hint}（扁平字段：error 失败原因 / hint 修复指引）
     busy 语义：动作执行中该程序条目 status="busy" + busy_action —— 前端轮询
     GET /api/v1/programs 即可看到；完成后后端写终态并经 WS 推送
     （topics：program.status / obsidian.sync，前端桥接为 document 事件）。 */
  getPrograms() {
    if (this.mode === "live") return this._fetch(this.LIVE_ROUTES.programs);
    return this._mockify(() => JSON.parse(JSON.stringify(MOCK.programs)));
  },
  programAction(id, action) {
    if (this.mode === "live") {
      return this._fetchResult(this.LIVE_ROUTES.programAction.replace("{id}", id).replace("{action}", action), { method: "POST" });
    }
    // mock：动作开始同步置 busy（status="busy" + busy_action，轮询 /programs 可见），
    // 完成演化终态；一次性故障演练演示 ok:false + result{code,error,hint} 契约
    const p = MOCK.programs.find(x => x.id === id);
    if (!p) return this._mockify(() => { throw new Error("程序不存在"); }, 200);
    const prevStatus = p.status;
    p.status = "busy";
    p.busy_action = action;
    return this._mockify(() => {
      const drill = MOCK.drillFailures && MOCK.drillFailures[id + ":" + action];
      if (drill) {
        delete MOCK.drillFailures[id + ":" + action];
        p.status = prevStatus; p.busy_action = null; // 可修复失败：状态回滚
        return { ok: false, id, action, result: drill };
      }

      const ver = p.upstream || p.latest || p.version;
      const freshProc = (mem) => ({ pid: 1000 + Math.floor(Math.random() * 4000), mem_mb: mem, cpu: (0.8 + Math.random() * 2.4).toFixed(1) + "%", uptime: "刚刚", restarts: p.proc ? p.proc.restarts : 0 });
      let detail = "";
      if (action === "install") {
        p.status = "running"; p.version = ver; p.latest = ver; p.proc = freshProc(p.form === "docker" ? 420 : 170);
        detail = p.form === "docker"
          ? `docker compose up -d 完成：容器 ${p.container} 已启动（镜像 ${p.image}:${ver}）`
          : `release v${ver} 下载并解压完成，已写入运行目录 ${p.dir || "~/.me-agent/programs/"} 并启动`;
      } else if (action === "one-click") {
        p.status = "running"; p.version = ver; p.latest = ver; p.proc = freshProc(p.form === "docker" ? 420 : 170);
        detail = `拉取 → 配置 → 启动 → 注册完成：${p.name} 已接入 Console（健康探测与事件通道已建立，v${ver}）`;
      } else if (action === "update") {
        p.version = ver; p.latest = ver; p.status = "running";
        detail = p.form === "docker"
          ? `已拉取 ${p.image}:${ver} 并重建容器，vault 挂载已恢复`
          : `已更新到 v${ver}（下载 → 替换 → 平滑重启，反向 WS 已重连）`;
      } else if (action === "start" || action === "restart") {
        p.status = "running";
        p.proc = freshProc(120 + Math.floor(Math.random() * 300));
        if (action === "restart" && p.proc) p.proc.restarts = (p.proc.restarts || 0) + 1;
        detail = action === "start" ? `进程已启动（pid ${p.proc.pid}）` : `已重启（第 ${p.proc.restarts} 次）`;
      } else if (action === "stop") {
        p.status = "stopped";
        p.proc = { pid: null, mem_mb: 0, cpu: "—", uptime: "—", restarts: p.proc ? p.proc.restarts : 0 };
        detail = p.form === "docker" ? `容器 ${p.container} 已停止` : "进程已停止";
      }
      p.busy_action = null;
      return { ok: true, id, action, result: { status: p.status, version: p.version, detail } };
    }, action === "install" || action === "one-click" ? 1500 : (action === "update" ? 1300 : 850));
  },
  /* 检查更新（契约定稿）：GET /api/v1/programs/{id}/check-latest
     —— 按钮名 "check-update" 来自后端 actions 数组，点击时前端调 check-latest。
     响应 {ok, id, action, result:{current, latest, has_update, notes, checked_at}}；
     发现新版后端同步刷新 GET /programs 的 latest 字段（卡片徽标点亮、更新按钮解锁）。 */
  programCheckLatest(id) {
    if (this.mode === "live") {
      return this._fetchResult(this.LIVE_ROUTES.programCheckLatest.replace("{id}", id), { method: "GET" });
    }
    return this._mockify(() => {
      const p = MOCK.programs.find(x => x.id === id);
      if (!p) throw new Error("程序不存在");
      if (p.upstream && p.upstream !== p.latest) p.latest = p.upstream; // 模拟后端查询上游发现新版本
      const has = p.latest !== p.version;
      return {
        ok: true, id, action: "check-update",
        result: {
          current: p.version, latest: p.latest, has_update: has,
          checked_at: new Date().toISOString(),
          notes: has ? `上游已发布 v${p.latest}（当前 v${p.version}）` : "上游无新版本，已是最新",
        },
      };
    }, 700);
  },

  /* ================= Obsidian 同步器（V4 · 契约已定稿） =================
     可观测面板数据源 = GET /api/v1/programs 中 obsidian_sync 条目的 health 字段：
       {running, last_sync_at, last_result{changed, pushed, error}, error_streak, detail}
     （不再调用独立 sync 状态接口；Mock 在 health 上附加演示增强字段，面板自动降级）
     WS 实时推送 topic = obsidian.sync（前端桥接为 document 事件 obssync:push）。 */
  /* 立即同步：POST /api/v1/programs/obsidian_sync/sync → {ok, id, action, result}；
     成功 result = {changed, pushed, commits, duration_ms, at…}，失败 result = {code?, error, hint} */
  obsidianSyncNow() {
    if (this.mode === "live") return this._fetchResult(this.LIVE_ROUTES.obsidianSyncNow, { method: "POST" });
    const entry = MOCK.programs.find(x => x.id === "obsidian_sync");
    if (!entry || !entry.health) return this._mockify(() => { throw new Error("同步器状态不可用"); }, 200);
    const prevStatus = entry.status;
    entry.status = "busy"; entry.busy_action = "sync";   // busy 期间轮询 /programs 可见
    return this._mockify(() => {
      const s = entry.health;
      const changed = 2 + Math.floor(Math.random() * 5);
      const rec = {
        at: new Date().toISOString(), outcome: "success", trigger: "manual",
        changed_files: changed, pushed_files: changed, commits: 1,
        commit: Math.random().toString(16).slice(2, 9),
        duration_ms: 1600 + Math.floor(Math.random() * 1400),
      };
      entry.status = "running"; entry.busy_action = null;
      s.last_sync_at = rec.at;
      s.last_result = { changed: rec.changed_files, pushed: rec.pushed_files, error: null };
      s.error_streak = 0;
      s.detail = `手动同步成功 · 注入 ${rec.changed_files} · 推送 ${rec.pushed_files} 文件`;
      s.last_sync = rec;
      s.history.unshift(rec);
      if (s.history.length > 6) s.history.length = 6;
      s.projection.files += 1;
      s.projection.latest_file_age_min = 0;
      s.vault.synced_entries = Math.min(s.vault.knowledge_files, s.vault.synced_entries + changed);
      s.vault.last_commit = rec.commit;
      s.ws.last_push = rec.at;
      return { ok: true, id: "obsidian_sync", action: "sync", result: rec };
    }, 1900);
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
