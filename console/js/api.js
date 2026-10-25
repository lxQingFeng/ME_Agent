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
    programAction:   "/api/v1/programs/{id}/{action}", // POST 统一动作 → {ok, id, action, result}；重复 start 拒绝（result.running）
    programLogs:     "/api/v1/programs/{id}/logs",     // GET  V4 尾部语义；V5 分页 ?offset=&limit=（0=读到结尾）→ {id, total, offset, has_more, lines[]}
    programCheckLatest: "/api/v1/programs/{id}/check-latest", // GET 检查更新（actions 数组内 "check-update" 按钮调它）
    obsidianSyncNow: "/api/v1/programs/obsidian_sync/sync",   // POST Obsidian 立即同步 → {ok, id, action, result}
    /* ---- V5.1（对齐第 11 轮契约清单 · 全栈开发 48a5150）----
       程序详情 webui:{token,user,url}（WS topic program.webui）；progress{phase, downloaded?, total?, percent?, speed_bps?, done?}（WS program.progress） */
    llmConfigGet:    "/api/v1/settings/llm",           // GET  LLM 配置（api_key 恒 "***"/""，明文不回显；含 chat_model/planner_model/temperature 等）
    llmConfigSave:   "/api/v1/settings/llm",           // PUT  保存 + 热生效（字段可选；api_key 三态：***=保留 / ""=清除 / 非空=新值）
    llmModels:       "/api/v1/settings/llm/models",    // POST 模型列表代理 → {ok, models:[id], count}
    personaGet:      "/api/v1/settings/persona",       // GET  人设 {name, essence, style_rules[], group_rules_digest, reply_fallbacks[]}
    personaSave:     "/api/v1/settings/persona",       // PUT  保存 + 热生效（空名字/空规则 400）
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
    // 完成演化终态；一次性故障演练演示 ok:false + result{code,error,hint} 契约。
    const p = MOCK.programs.find(x => x.id === id);
    if (!p) return this._mockify(() => { throw new Error("程序不存在"); }, 200);
    /* 契约（第 11 轮 §二.3）：running 程序重复 start → 明确拒绝（不进 busy），
       result = {running:true, pid, error}——防误启动/误重启（37 实机反馈状态失真的一部分） */
    if (action === "start" && p.status === "running") {
      const pid = p.proc && p.proc.pid;
      return this._mockify(() => ({
        ok: false, id, action,
        result: { running: true, pid, error: `已在运行(pid=${pid})，重复启动被拒绝` },
      }), 300);
    }
    const prevStatus = p.status;
    p.status = "busy";
    p.busy_action = action;

    /* V5：下载/安装/更新为长动作 —— 慢速进度驱动（进度写入 busy 条目 progress 字段，
       1.2s 轮询 + document 事件 program:progress 双通道刷新），完成后转结果反馈。
       Mock 同构演示：release 形态约 15s 慢速下载；docker 形态约 8s 分层拉取；
       故障演练在 ~31% 处下载中断（result 回滚 + 修复指引）。 */
    const isLong = action === "install" || action === "one-click" || action === "update";
    if (isLong) {
      const drill = MOCK.drillFailures && MOCK.drillFailures[id + ":" + action];
      const docker = p.form === "docker";
      const cfg = docker
        ? { total: 40265318, speed: 5.4e6, kind: "docker", layers: 7, tickMs: 600 }   // ≈8s 镜像分层拉取
        : { total: 40361472, speed: 2.6e6, kind: "release", tickMs: 700 };            // ≈15s 慢速下载演示
      if (drill) cfg.abortAt = 0.31;                                                   // 故障演练：~31% 中断
      return MOCK.driveProgress(p, action, cfg).then(out => {
        if (out.aborted) {
          delete MOCK.drillFailures[id + ":" + action];
          p.status = prevStatus; p.busy_action = null;                                 // 可修复失败：状态回滚
          document.dispatchEvent(new CustomEvent("program:status", { detail: { id, status: prevStatus } }));
          return { ok: false, id, action, result: drill };
        }
        return this._mockEvolve(p, id, action, out.avg_bps);
      });
    }
    return this._mockify(() => {
      const drill = MOCK.drillFailures && MOCK.drillFailures[id + ":" + action];
      if (drill) {
        delete MOCK.drillFailures[id + ":" + action];
        p.status = prevStatus; p.busy_action = null; // 可修复失败：状态回滚
        return { ok: false, id, action, result: drill };
      }
      return this._mockEvolve(p, id, action, null);
    }, 850);
  },
  /* mock 终态演化（原 V4 逻辑提取）+ V5：落盘持久化（跨刷新稳定）+ program:status 推送 */
  _mockEvolve(p, id, action, avgBps) {
    const ver = p.upstream || p.latest || p.version;
    const freshProc = (mem) => ({ pid: 1000 + Math.floor(Math.random() * 4000), mem_mb: mem, cpu: (0.8 + Math.random() * 2.4).toFixed(1) + "%", uptime: "刚刚", restarts: p.proc ? p.proc.restarts : 0 });
    const dlBytes = p.form === "docker" ? 40265318 : 40361472;                          // 与 driveProgress cfg.total 一致
    const dlNote = avgBps ? `（${(dlBytes / 1048576).toFixed(1)} MB · 平均 ${(avgBps / 1048576).toFixed(1)} MB/s）` : "";
    let detail = "";
    if (action === "install") {
      p.status = "running"; p.version = ver; p.latest = ver; p.proc = freshProc(p.form === "docker" ? 420 : 170);
      detail = p.form === "docker"
        ? `docker compose up -d 完成：容器 ${p.container} 已启动（镜像 ${p.image}:${ver}）`
        : `release v${ver} 下载并解压完成${dlNote}，已写入运行目录 ${p.dir || "~/.me-agent/programs/"} 并启动`;
    } else if (action === "one-click") {
      p.status = "running"; p.version = ver; p.latest = ver; p.proc = freshProc(p.form === "docker" ? 420 : 170);
      detail = `拉取 → 配置 → 启动 → 注册完成${dlNote}：${p.name} 已接入 Console（健康探测与事件通道已建立，v${ver}）`;
    } else if (action === "update") {
      p.version = ver; p.latest = ver; p.status = "running";
      detail = p.form === "docker"
        ? `已拉取 ${p.image}:${ver} 并重建容器，vault 挂载已恢复`
        : `已更新到 v${ver}${dlNote}（下载 → 替换 → 平滑重启，反向 WS 已重连）`;
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
    if (MOCK.persistPrograms) MOCK.persistPrograms();   // 状态跨刷新稳定（running 不回跳）
    /* WS program.status 推送体（契约 §二.3）：{id, status, pid, progress?, webui?} */
    document.dispatchEvent(new CustomEvent("program:status", {
      detail: { id, status: p.status, pid: p.proc && p.proc.pid, webui: p.webui || {} },
    }));
    return { ok: true, id, action, result: { status: p.status, version: p.version, detail } };
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
      if (MOCK.persistPrograms) MOCK.persistPrograms();
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

  /* ================= 全量日志（V5.1 · 契约对齐） =================
     端点：GET /api/v1/programs/{id}/logs?offset=&limit=
       · 不传 offset = 尾部语义（V4 兼容，limit 默认 200）
       · offset=0&limit=200 = 从文件头翻页；limit=0 = 从 offset 读到结尾（全量）
     响应：{id, total, offset, has_more, lines[]}——lines 为原始字符串行（前端解析着色）。
     mock：程序日志由 mock-data.js v5Init 生成（含启动段的字符串行，契约同构）。 */
  getProgramLogs(id, opts = {}) {
    if (this.mode === "live") {
      const q = new URLSearchParams();
      if (opts.limit != null) q.set("limit", String(opts.limit));
      if (opts.offset != null) q.set("offset", String(opts.offset));
      return this._fetch(this.LIVE_ROUTES.programLogs.replace("{id}", id) + (q.toString() ? "?" + q.toString() : ""));
    }
    return this._mockify(() => {
      const all = MOCK.programLogFull[id] || [];
      const hasOffset = opts.offset != null;
      const offset = hasOffset
        ? Math.max(0, Math.min(opts.offset, all.length))
        : Math.max(0, all.length - (opts.limit != null ? opts.limit : 200));   // 不传 offset = 尾部语义
      const limit = opts.limit != null ? opts.limit : 200;
      const end = limit === 0 ? all.length : Math.min(all.length, offset + limit);
      return { id, total: all.length, offset, has_more: end < all.length, lines: all.slice(offset, end) };
    }, 320);
  },
  /* 日志跟随：live 由 WS 日志流 topic 驱动（此处返回空），mock 定时生成新行（字符串行） */
  programLogFollow(id) {
    if (this.mode === "live") return Promise.resolve({ lines: [] });
    return this._mockify(() => ({ lines: MOCK.programLogFollow(id) }), 1100);
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

  /* ================= LLM 设置（V5.1 · 契约：/api/v1/settings/llm） =================
     GET：{ok, provider, base_url, api_key("***"|""), has_api_key, api_key_source,
           chat_model, planner_model, temperature, max_tool_rounds, timeout_s,
           enabled, applied:{...}}——明文密钥永不回显。
     PUT：字段可选（省略=不动）；api_key 三态（"***"=保留 / ""=清除 / 非空=新值）→ 保存热生效。
     模型列表代理：POST /api/v1/settings/llm/models → {ok, models:[id], count}。
     mock：保存写 localStorage（同构后端 state 落盘），GET 转契约形态。 */
  llmGetConfig() {
    if (this.mode === "live") return this._fetch(this.LIVE_ROUTES.llmConfigGet);
    return this._mockify(() => {
      const saved = U.lsGet("console.llm.config.v1", null);
      const cfg = Object.assign({}, MOCK.llmConfig, saved ? JSON.parse(saved) : {});
      return this._llmContractView(cfg, "state");
    }, 350);
  },
  llmSaveConfig(patch) {
    if (this.mode === "live") {
      return this._fetchResult(this.LIVE_ROUTES.llmConfigSave, { method: "PUT", body: JSON.stringify(patch) });
    }
    return this._mockify(() => {
      const saved = U.lsGet("console.llm.config.v1", null);
      const cur = Object.assign({}, MOCK.llmConfig, saved ? JSON.parse(saved) : {});
      /* api_key 三态（契约）：非空=新值 / "***"=保留 / ""=清除 */
      if (patch.api_key != null) {
        if (typeof patch.api_key === "string" && patch.api_key.trim() && patch.api_key !== "***") cur.api_key = patch.api_key.trim();
        else if (patch.api_key === "") cur.api_key = "";
      }
      ["provider", "base_url", "chat_model", "planner_model"].forEach(k => { if (patch[k] != null) cur[k] = patch[k]; });
      if (patch.temperature != null) cur.temperature = +patch.temperature;
      if (patch.max_tool_rounds != null) cur.max_tool_rounds = +patch.max_tool_rounds;
      if (patch.timeout_s != null) cur.timeout_s = +patch.timeout_s;
      U.lsSet("console.llm.config.v1", JSON.stringify(cur));
      const view = this._llmContractView(cur, "state");
      return Object.assign({ ok: true, saved: true, hot_reloaded: true }, view);
    }, 600);
  },
  /* 契约形态视图：api_key 恒为 "***"/""（明文不回显），附 has_api_key/api_key_source/enabled/applied */
  _llmContractView(cfg, source) {
    const has = !!(cfg.api_key && String(cfg.api_key).trim());
    return {
      ok: true,
      provider: cfg.provider || "",
      base_url: cfg.base_url || "",
      api_key: has ? "***" : "",
      has_api_key: has,
      api_key_source: has ? (source || "state") : "none",
      chat_model: cfg.chat_model || "",
      planner_model: cfg.planner_model || cfg.chat_model || "",
      temperature: cfg.temperature != null ? cfg.temperature : 0.7,
      max_tool_rounds: cfg.max_tool_rounds != null ? cfg.max_tool_rounds : 2,
      timeout_s: cfg.timeout_s != null ? cfg.timeout_s : 30,
      enabled: !!(has && cfg.base_url && cfg.chat_model),
      applied: {
        chat: has && cfg.chat_model ? `chat=${cfg.chat_model}（${cfg.provider || "?"}）` : "chat 管道未接通（缺 key/模型）",
        planner: has && (cfg.planner_model || cfg.chat_model) ? `planner=${cfg.planner_model || cfg.chat_model}` : "planner 未配置",
        hot: "已热生效（对话管道构造请求时现取）",
      },
    };
  },
  llmFetchModels(provider) {
    if (this.mode === "live") {
      return this._fetchResult(this.LIVE_ROUTES.llmModels, { method: "POST", body: JSON.stringify({ provider }) });
    }
    return this._mockify(() => {
      const pv = (MOCK.llmProviders || []).find(x => x.id === provider) || {};
      const models = (pv.models || []).slice();
      return { ok: true, models, count: models.length };
    }, 750);
  },

  /* ================= 人设设置（V5.1 · 契约：/api/v1/settings/persona） =================
     字段：name / essence / style_rules[] / group_rules_digest / reply_fallbacks[]；
     GET 回读 · PUT 保存热生效（空名字/空 style_rules 400——mock 同构校验）。
     mock：保存写 localStorage，并派发 persona:updated（app.js 侧栏 Agent 名即时刷新）。 */
  personaGet() {
    if (this.mode === "live") return this._fetch(this.LIVE_ROUTES.personaGet);
    return this._mockify(() => {
      const saved = U.lsGet("console.persona.v1", null);
      return Object.assign({ ok: true }, Object.assign({}, MOCK.persona, saved ? JSON.parse(saved) : {}));
    }, 300);
  },
  personaSave(cfg) {
    if (this.mode === "live") {
      return this._fetchResult(this.LIVE_ROUTES.personaSave, { method: "PUT", body: JSON.stringify(cfg) });
    }
    return this._mockify(() => {
      const bad = (o) => o instanceof Error ? o : new Error(o);
      if (!cfg || !cfg.name || !String(cfg.name).trim()) throw bad("名字不能为空（后端 400 同构）");
      const rules = (cfg.style_rules || []).map(x => String(x || "").trim()).filter(Boolean);
      if (!rules.length) throw bad("style_rules 不能为空（后端 400 同构）");
      const next = {
        name: String(cfg.name).trim().slice(0, 16),
        essence: String(cfg.essence || "").trim().slice(0, 120),
        style_rules: rules,
        group_rules_digest: String(cfg.group_rules_digest || "").trim().slice(0, 300),
        reply_fallbacks: (cfg.reply_fallbacks || []).map(x => String(x || "").trim()).filter(Boolean).slice(0, 8),
      };
      U.lsSet("console.persona.v1", JSON.stringify(next));
      document.dispatchEvent(new CustomEvent("persona:updated", { detail: { name: next.name } }));
      return { ok: true, saved: true, hot_reloaded: true, applied: { prompt: "管道构造系统提示词时现取（热生效）" } };
    }, 550);
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
