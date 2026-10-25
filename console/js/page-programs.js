/* ============================================================
   page-programs.js — 第三方程序管理（V5.1 · 契约对齐第 11 轮清单）
   ------------------------------------------------------------
   V5 新增（对应 37 实机反馈 4 问题）：
   1) 程序卡片状态真实化：running 持续显示、刷新不回跳（mock 持久化同构后端
      programs.yaml 落盘，见 mock-data.js v5Init）；启停按钮按真实状态锁定
      （运行中禁启动、非运行态禁停止、最新版锁更新）；沿用 1.2s busy 轮询 +
      WS program.status 推送刷新（体 {id,status,pid,progress?,webui?}）；
      running 程序重复 start 由后端明确拒绝（result{running:true,pid}）。
   2) 日志查看升级：全量日志弹窗（滚动 + 搜索高亮/过滤 + 自动滚底 + 运行中
      日志跟随），GET /api/v1/programs/{id}/logs?offset=&limit=（分页契约，
      limit=0 读到结尾；lines 为原始字符串行，前端解析着色）；
      SnowLuma WebUI 凭据高亮展示 + 一键复制（契约：程序详情 webui:{token,
      user,url} 对象 / WS topic program.webui 推送，命中闪一拍）。
   3) 下载/安装/更新进度条（契约双形态）：
      字节型 progress{phase:"download", downloaded, total, percent, speed_bps,
      done?} → 百分比条 + 统计；阶段型 {phase:"extract"|"pull", detail} →
      流动条 + 阶段文案；经 1.2s 轮询 + WS program.progress 双通道实时刷新，
      完成后转结果反馈；Mock 慢速下载演示（release ≈15s / docker 分层 ≈8s，
      故障演练 ~31% 处中断）。
   V4 保留：
   1) 一键管理回归：Hindsight（docker 一键形态）/ SnowLuma（release 拉取形态）
      「一键拉取(安装) / 启动 / 停止 / 更新 / 一键接入 / 检查更新」——
      按钮渲染源 = 后端 actions 数组（含 "check-update"）。
   2) 统一动作契约：POST /api/v1/programs/{id}/{action} → {ok, id, action, result}；
      成功 result{status,version,detail}，失败 result{code?,error,hint}（扁平修复指引）。
      busy 语义：程序条目 status="busy" + busy_action，前端轮询 GET /programs 刷新。
   3) 检查更新：GET /api/v1/programs/{id}/check-latest → result{current,latest,has_update}，
      发现新版点亮「有新版本」徽标并解锁更新按钮。
   4) Obsidian 同步器可观测面板：数据源 = GET /api/v1/programs 中 obsidian_sync 条目的
      health 字段（running/last_sync_at/last_result{changed,pushed,error}/error_streak/detail）；
      「立即同步」= POST /api/v1/programs/obsidian_sync/sync。
      WS topics：program.status / obsidian.sync / program.progress / program.webui
      （桥接 document 事件 program:status / obssync:push / program:progress /
      program:webui，面板/卡片/进度条自动刷新；Mock 由模拟器驱动）。
   ============================================================ */
"use strict";

(function () {

  const STATUS = {
    running:   { label: "运行中", tone: "ok",   pulse: true },
    stopped:   { label: "已停止", tone: "off",  pulse: false },
    degraded:  { label: "告警",   tone: "warn", pulse: true },
    notinstalled: { label: "未安装", tone: "off", pulse: false },
    busy:      { label: "执行中", tone: "busy", pulse: true },
  };
  const ACTION_META = {
    install:        { label: "一键拉取", icon: "download", cls: "primary" },
    start:          { label: "启动",     icon: "play",    cls: "primary" },
    stop:           { label: "停止",     icon: "pause",   cls: "danger ghost" },
    restart:        { label: "重启",     icon: "refresh", cls: "" },
    update:         { label: "更新",     icon: "refresh", cls: "" },
    "one-click":    { label: "一键接入", icon: "plug",    cls: "" },
    "check-update": { label: "检查更新", icon: "search",  cls: "ghost" },
    log:            { label: "日志",     icon: "doc",     cls: "ghost" },
  };
  const TRIGGER_LABEL = { cron: "定时", event: "事件", manual: "手动" };

  /* busy 态阶段文案（轮播演示；live 模式统一显示等待后端返回） */
  const STAGES = {
    docker: {
      install:      ["检查 Docker 环境…", "拉取镜像…", "创建并启动容器…"],
      update:       ["拉取新版本镜像…", "重建容器并恢复挂载…"],
      "one-click":  ["检查 Docker 环境…", "拉取镜像…", "创建容器并启动…", "写入接入配置并注册到 Console…"],
      start: ["下发容器启动指令…"], stop: ["下发容器停止指令…"], restart: ["停止旧容器…", "启动新容器…"],
    },
    release: {
      install:      ["解析最新 Release…", "下载安装包…", "解压到运行目录…", "生成配置并启动…"],
      update:       ["下载新版本 Release…", "替换程序文件…", "平滑重启并重连反向 WS…"],
      "one-click":  ["解析最新 Release…", "下载安装包…", "安装并启动…", "注册到 Console 通道…"],
      start: ["下发启动指令…"], stop: ["下发停止指令…"], restart: ["重启进程…"],
    },
  };
  const SYNC_STAGES = ["拉取增量记忆…", "渲染投影文件…", "vault git commit & push…"];
  const POLL_MS = 1200;   // busy 轮询周期（GET /api/v1/programs）

  /* ---------- 页面级状态（跨重渲染保留） ---------- */
  let mainEl = null;            // 当前渲染容器
  let programsCache = [];       // GET /programs 最新快照（obsidian_sync.health 也在其中）
  const feedback = {};          // id → {type:busy|ok|err|upd, ...} 最近一次动作反馈
  let wsBound = false;          // WS 桥接事件监听只绑一次

  /* ---------- 小工具 ---------- */
  function statusChip(st, busyAction) {
    const s = STATUS[st] || STATUS.stopped;
    const busyTxt = st === "busy" && busyAction && ACTION_META[busyAction]
      ? ` · ${ACTION_META[busyAction].label}` : "";
    return `<span class="svc-tag" ${busyTxt ? `title="后端正在执行：${ACTION_META[busyAction].label}"` : ""}><span class="svc-dot" data-state="${s.tone}"></span>${s.label}${busyTxt}</span>`;
  }
  function fmtRel(iso) {
    if (!iso) return "—";
    const d = new Date(iso), now = new Date();
    const hm = Fmt.hm(d);
    if (d.toDateString() === now.toDateString()) return `今日 ${hm}`;
    const y = new Date(now); y.setDate(y.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return `昨日 ${hm}`;
    return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${hm}`;
  }
  function fmtAgo(min) {
    if (min == null) return "—";
    if (min < 60) return `${Math.max(0, Math.round(min))} 分钟前`;
    const h = min / 60;
    if (h < 24) return `${h >= 10 ? Math.round(h) : h.toFixed(1)} 小时前`;
    return `${(h / 24).toFixed(1)} 天前`;
  }
  function fmtDur(ms) { return ms == null ? "—" : (ms / 1000).toFixed(1) + "s"; }
  function spin(loader) { return loader.replace('class="i"', 'class="i spin"'); }
  /* V5 · 进度格式化：字节/速度/剩余时间 */
  function fmtBytes(b) { return b == null ? "—" : b >= 1048576 ? (b / 1048576).toFixed(1) + " MB" : (b / 1024).toFixed(0) + " KB"; }
  function fmtSpeed(bps) { return bps == null ? "—" : (bps / 1048576).toFixed(1) + " MB/s"; }
  function fmtEta(s) { return s == null ? "—" : s >= 60 ? Math.floor(s / 60) + "分" + (s % 60) + "秒" : s + " 秒"; }
  /* V5 · 剪贴板（navigator.clipboard 优先，降级 execCommand） */
  function copyText(text) {
    if (!text) return Promise.resolve(false);
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(() => true).catch(() => fallbackCopy(text));
    }
    return Promise.resolve(fallbackCopy(text));
  }
  function fallbackCopy(text) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;opacity:0;pointer-events:none";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch (e) { return false; }
  }
  function obsTrigger(h) { return TRIGGER_LABEL[h && h.trigger] || (h && h.trigger) || "—"; }
  function obsStat(k, v, hint) {
    return `<div class="obs-stat"><span class="os-k">${esc(k)}</span><span class="os-v">${v}</span><span class="os-h">${hint}</span></div>`;
  }

  /* ---------- 程序卡 ---------- */
  function formBadge(p) {
    if (p.form === "docker")
      return `<span class="chip purple form-badge" title="docker compose 一键拉取 / 启停 / 更新">Docker 一键形态</span>`;
    if (p.form === "release")
      return `<span class="chip cyan form-badge" title="从 GitHub Release 拉取安装并更新">Release 拉取形态</span>`;
    return `<span class="chip blue form-badge" title="由你自行部署与管理；控制台仅做健康探测（HTTP / WS 事件）与监控">外部自管</span>`;
  }
  function programMeta(p) {
    if (p.form === "docker")
      return `<div class="pmeta mono">${Icons.layers} 镜像 <b>${esc(p.image)}</b> · 容器 <b>${esc(p.container)}</b> · compose 拉取启动</div>`;
    if (p.form === "release")
      return `<div class="pmeta mono">${Icons.download} 源 <b>${esc(p.source)}</b> · 目录 <b>${esc(p.dir)}</b> · release 拉取安装</div>`;
    return "";
  }
  function versionRow(p) {
    const hasUpd = p.latest !== p.version;
    return `<div class="ver-row mono">${Icons.tag}<span>当前</span><span class="ver-v">v${esc(p.version || "—")}</span>` +
      (hasUpd
        ? `<span class="chip amber form-badge upd-badge">有新版本 v${esc(p.latest)}</span>`
        : `<span class="ver-latest">已是最新</span>`) +
      `</div>`;
  }
  function procTable(p) {
    const pr = p.proc || {};
    const running = pr.pid;
    return `
    <div class="proc-grid ${running ? "" : "dimmed"}">
      <div><span class="proc-k">PID</span><span class="mono proc-v">${running ? pr.pid : "—"}</span></div>
      <div><span class="proc-k">内存</span><span class="mono proc-v">${running ? pr.mem_mb + " MB" : "—"}</span></div>
      <div><span class="proc-k">CPU</span><span class="mono proc-v">${pr.cpu || "—"}</span></div>
      <div><span class="proc-k">运行时长</span><span class="mono proc-v">${pr.uptime || "—"}</span></div>
      <div><span class="proc-k">重启次数</span><span class="mono proc-v">${pr.restarts == null ? "—" : pr.restarts}</span></div>
    </div>`;
  }
  function actionBtn(p, a) {
    const m = ACTION_META[a];
    if (!m) return "";
    const fb = feedback[p.id];
    const busyAll = fb && fb.type === "busy";
    const isBusyBtn = busyAll && fb.action === a;
    let dis = false;
    if (a === "update" && p.latest === p.version) dis = true;
    if (a === "start" && p.status === "running") dis = true;
    if (a === "stop" && (p.status === "stopped" || p.status === "notinstalled")) dis = true;
    if (busyAll || p.status === "busy") dis = true;   // 本页动作 busy / 后端 busy（轮询所见）均锁定
    const icon = isBusyBtn ? spin(Icons.loader) : (Icons[m.icon] || "");
    const label = isBusyBtn ? `${m.label}中…` : m.label;
    return `<button class="btn ${m.cls}" data-action="${a}" ${dis ? "disabled" : ""}>${icon}<span>${label}</span></button>`;
  }
  /* V5.1 · 令牌高亮面板：WebUI 凭据（契约：程序详情 webui:{token,user,url} 对象，
     未检测到为 {}；后端从启动日志「initial credentials」提取，重启换新自动覆盖；WS topic
     program.webui 推送命中即闪一拍）——解决 37 无法登录 SnowLuma 网页端的问题 */
  function tokenPanel(p) {
    const w = p.webui;
    if (!w || !w.token) return "";
    return `
    <div class="token-box" data-token-box>
      <div class="token-head">
        ${Icons.key} <b>WebUI 登录凭据</b>
        <span class="chip amber form-badge" title="来源：程序详情 webui 对象（后端提取自启动日志 initial credentials 行）；WS program.webui 推送（命中即闪一拍）">网页端登录用</span>
      </div>
      <div class="token-row">
        <code class="token-value mono">${esc(w.token)}</code>
        <button class="btn primary sm" data-copy-token="${esc(w.token)}" data-copy-token-name="${esc(p.name)}">${Icons.copy}<span>一键复制</span></button>
      </div>
      <div class="token-hint">打开 <span class="mono">${esc(w.url || "WebUI")}</span> → 用户 <span class="mono">${esc(w.user || "—")}</span> · 密码粘贴此令牌（=初始凭据 password，仅首次启动可见，控制台已代为提取）——也可在「日志」弹窗搜索定位凭据行</div>
    </div>`;
  }
  /* V5.1 · busy 期间下载/安装/更新进度（契约双形态）：
     字节型 {phase:"download", downloaded, total, percent, speed_bps, done?} → 百分比条 + 统计行
     阶段型 {phase:"extract"|"pull", detail}（total=0/percent=null 同）→ 流动条 + 阶段文案
     通道：busy 轮询读 progress 字段 + document 事件 program:progress（WS 同名 topic 桥接） */
  function isByteProgress(pr) {
    return !!(pr && pr.phase === "download" && pr.total > 0 && pr.percent != null);
  }
  function progressArea(p) {
    const pr = p.progress;
    if (!pr) return "";
    if (isByteProgress(pr)) {
      const pct = Math.max(0, Math.min(100, pr.percent ?? 0));
      const eta = pr.speed_bps > 0 ? Math.max(0, Math.round((pr.total - (pr.downloaded || 0)) / pr.speed_bps)) : null;
      return `
    <div class="dl-progress" data-progress-box data-prog-form="byte">
      <div class="dl-bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100" aria-label="下载进度">
        <div class="dl-fill${pr.done ? "" : " byte"}" data-dl-fill style="width:${pct}%"></div>
      </div>
      <div class="dl-stats mono">
        <span class="dl-pct" data-dl-pct>${pct.toFixed(1)}%${pr.done ? " · 完成" : ""}</span>
        <span class="dl-dim">已下载 ${fmtBytes(pr.downloaded)} / ${fmtBytes(pr.total)}</span>
        <span class="dl-dim" data-dl-speed>${pr.speed_bps ? fmtSpeed(pr.speed_bps) : ""}</span>
        <span class="dl-dim" data-dl-eta>${!pr.done && eta != null && eta > 0 ? "剩余 " + fmtEta(eta) : ""}</span>
      </div>
    </div>`;
    }
    /* 阶段型：无字节级语义（docker 拉镜像 / 解压收尾）→ 流动指示条 */
    const detail = pr.detail || (pr.phase === "download" ? "下载中（未知总量）" : "执行中…");
    return `
    <div class="dl-progress" data-progress-box data-prog-form="stage">
      <div class="dl-bar indet" role="progressbar" aria-label="执行进度">
        <div class="dl-fill indet"></div>
      </div>
      <div class="dl-stats mono">
        <span class="dl-pct">${esc(pr.phase === "pull" ? "镜像拉取" : pr.phase === "extract" ? "安装收尾" : "执行中")}</span>
        <span class="dl-dim" data-dl-label>${esc(detail)}</span>
      </div>
    </div>`;
  }
  /* 最近一次动作反馈：busy（阶段文案 + 进度条）/ ok（detail）/ err（error + hint 修复指引）/ upd（版本对比） */
  function feedbackArea(p) {
    const id = p.id || p;
    const fb = feedback[id];
    if (!fb) return "";
    const prog = (fb.type === "busy" && typeof p === "object" && p.progress) ? progressArea(p) : "";
    if (fb.type === "busy")
      return `<div class="afb afb-busy">${spin(Icons.loader)}<span class="afb-stage">${esc(fb.stage || "已下发指令，等待后端执行…")}</span></div>${prog}`;
    if (fb.type === "ok")
      return `<div class="afb afb-ok">${Icons.check}<div class="bn-body"><b>${esc(fb.title)}</b>${fb.detail ? `<div class="afb-detail">${esc(fb.detail)}</div>` : ""}</div></div>`;
    if (fb.type === "err")
      return `<div class="afb afb-err">${Icons.alert}<div class="bn-body"><b>${esc(fb.title)}</b>` +
        (fb.code ? ` <span class="chip red mono" style="font-size:10px">${esc(fb.code)}</span>` : "") +
        `<div class="afb-detail">${esc(fb.message || "未知错误")}</div>` +
        (fb.hint ? `<div class="afb-hint"><span class="hint-k">修复指引</span>${esc(fb.hint)}</div>` : "") +
        `</div></div>`;
    if (fb.type === "upd")
      return `<div class="upd-check mono">${Icons.search}<span>当前 v${esc(fb.current || "—")} → 最新 v${esc(fb.latest || "—")}</span>` +
        (fb.has_update
          ? `<span class="chip amber form-badge">有新版本 · 可更新</span>`
          : `<span class="chip green form-badge">已是最新</span>`) +
        `<span class="upd-at">${esc(fb.checkedAt || "")}</span></div>`;
    return "";
  }
  function programCard(p) {
    const acts = (p.actions || []).filter(a => a !== "sync-now" && ACTION_META[a]);
    return `
    <div class="card program-card" data-program="${p.id}" data-status="${p.status}">
      <div class="prog-head">
        <span class="program-logo" style="--logo-c:${p.color}">${esc(p.logo)}</span>
        <div class="prog-title">
          <div class="card-title">${esc(p.name)} <span class="chip gray form-badge">${esc(p.tag)}</span>${formBadge(p)}</div>
          ${statusChip(p.status, p.busy_action)}
        </div>
      </div>
      <div class="prog-desc">${esc(p.desc)}</div>
      ${programMeta(p)}
      ${versionRow(p)}
      ${tokenPanel(p)}
      ${procTable(p)}
      <div class="program-actions">
        ${acts.map(a => actionBtn(p, a)).join("")}
      </div>
      ${feedbackArea(p)}
    </div>`;
  }

  /* ---------- Obsidian 同步器 · 可观测面板 ----------
     数据源 = /programs 中 obsidian_sync 条目：
       契约核心：entry.health{running, last_sync_at, last_result{changed,pushed,error},
                 error_streak, detail} + entry.status/busy_action（busy 轮询所见）
       演示增强（live 可缺省 → 面板自动降级）：health.last_sync / history /
                 projection / vault / ws / schedule / next_run */
  function obsEntry() { return programsCache.find(p => p.id === "obsidian_sync") || null; }
  function obsPanel(entry) {
    const h = entry.health || {};
    const lr = h.last_result || {};
    const rich = h.last_sync || {};
    const proj = h.projection || {};
    const vault = h.vault || {};
    const ws = h.ws || {};
    const last = {
      at: rich.at || h.last_sync_at,
      outcome: rich.outcome || (lr.error ? "failure" : "success"),
      trigger: rich.trigger,
      changed_files: rich.changed_files != null ? rich.changed_files : lr.changed,
      pushed_files: rich.pushed_files != null ? rich.pushed_files : lr.pushed,
      commits: rich.commits, commit: rich.commit, duration_ms: rich.duration_ms,
      stage: rich.stage, message: rich.message, hint: rich.hint,
    };
    const errObj = lr.error ? (typeof lr.error === "object" ? lr.error : { error: String(lr.error) }) : null;
    const fb = feedback["obsidian_sync"];
    const busyObs = fb && fb.type === "busy";
    const pending = Math.max(0, (vault.knowledge_files || 0) - (vault.synced_entries || 0));
    const lastStatVal = errObj ? "失败" : (h.last_sync_at ? "成功" : "—");
    return `
    <div class="card obs-panel" id="obsPanel" data-program="obsidian_sync" data-status="${entry.status || (h.running ? "running" : "stopped")}">
      <div class="prog-head">
        <span class="program-logo" style="--logo-c:var(--cyan)">Ob</span>
        <div class="prog-title">
          <div class="card-title">Obsidian 同步器
            <span class="chip gray form-badge">人类可审计</span>
            <span class="chip cyan form-badge" title="运行态 / 最近结果 / 投影挂载 / 条目计数 全程可观测">同步可观测</span>
          </div>
          <div class="svc-tag">
            <span class="svc-dot" data-state="${entry.status === "busy" ? "busy" : (h.running ? "ok" : "off")}"></span>
            ${entry.status === "busy" ? "同步中" : (h.running ? "运行中" : "已停止")}
            ${h.error_streak ? `<span class="chip red form-badge" title="连续同步失败次数">连续失败 ${h.error_streak} 次</span>` : ""}
            ${ws.connected != null ? `<span class="ws-badge" data-on="${!!ws.connected}"><span class="ws-dot"></span>WS 实时推送${ws.connected ? " · 已连接" : " · 离线"}</span>` : ""}
          </div>
        </div>
        <div class="obs-actions">
          <button class="btn primary" id="obsSyncNow" ${busyObs || entry.status === "busy" ? "disabled" : ""}>${busyObs ? spin(Icons.loader) : Icons.refresh}<span>${busyObs ? "同步中…" : "立即同步"}</span></button>
        </div>
      </div>
      <div class="prog-desc">${h.schedule ? esc(h.schedule) : "定时全量快照 + 事件触发增量"}：把 WorldState 与记忆库投影为 Obsidian vault，git commit 推送远端 —— 打开 vault 就能核对 Agent 是否真的起效。${h.next_run ? `下次全量：${esc(h.next_run)}。` : ""}</div>
      ${h.detail ? `<div class="obs-detail mono">${Icons.radio} ${esc(h.detail)}</div>` : ""}

      <div class="obs-stats">
        ${obsStat("最近同步", lastStatVal, `${fmtRel(last.at)} · ${obsTrigger(last)}`)}
        ${obsStat("注入 changed", `${last.changed_files ?? "—"}<i class="os-u">文件</i>`, `推送 pushed ${last.pushed_files ?? "—"} 文件`)}
        ${obsStat("投影挂载", proj.mounted != null ? (proj.mounted ? "已挂载" : "未挂载") : "—", proj.mounted != null ? `${proj.files ?? "—"} 文件 · 最新 ${fmtAgo(proj.latest_file_age_min)}` : (h.detail ? esc(h.detail) : "—"))}
        ${obsStat("知识文件", vault.knowledge_files != null ? `${vault.knowledge_files}<i class="os-u">个</i>` : "—", vault.synced_entries != null ? `已同步 ${vault.synced_entries} 条 · 待同步 ${pending} 条` : "—")}
        ${obsStat("vault 远端", vault.remote ? esc(vault.remote.split("/").pop() || "—") : "—", vault.last_commit ? `HEAD ${esc(vault.last_commit)}` : "—")}
        ${obsStat("下次全量", esc(h.next_run || "—"), esc((h.schedule || "").split(" · ")[0] || ""))}
      </div>

      ${obsLastResult(h, last)}
      ${obsLastFailure(h)}
      ${feedbackArea("obsidian_sync")}

      ${proj.mounted != null ? `
      <div class="obs-sec">${Icons.folder} 投影挂载</div>
      <div class="obs-h-row ok mono">
        <span class="obs-h-dot"></span>
        <span class="obs-h-at">${proj.mounted ? "mounted" : "unmounted"}</span>
        <span class="obs-h-summary">${esc(proj.path || "—")} · ${proj.files ?? "—"} 文件 · 最新 ${fmtAgo(proj.latest_file_age_min)}</span>
      </div>` : ""}

      ${(h.history || []).length ? `
      <div class="obs-sec">${Icons.git} 同步历史（最近 ${Math.min(6, h.history.length)} 次）</div>
      <div class="obs-hist">${h.history.slice(0, 6).map(obsHistoryRow).join("")}</div>` : ""}
    </div>`;
  }
  /* 最近一次同步结果：契约字段优先（last_result{changed,pushed,error}），
     富记录（duration/commit/trigger）为演示增强，有则附加。 */
  function obsLastResult(h, last) {
    if (!h.last_sync_at && !h.last_result) return "";
    const lr = h.last_result || {};
    const errObj = lr.error ? (typeof lr.error === "object" ? lr.error : { error: String(lr.error) }) : null;
    if (errObj) {
      return `
      <div class="obs-block bad">
        <div class="obs-block-head">${Icons.alert}<b>最近一次同步 · 失败</b>${errObj.code ? `<span class="chip red mono" style="font-size:10px">${esc(errObj.code)}</span>` : ""}<span class="mono">${esc(fmtRel(h.last_sync_at))}</span></div>
        <div class="obs-block-body">${esc(errObj.error || errObj.message || "同步失败")}${lr.changed != null ? ` · 注入 changed ${lr.changed} 文件后中断` : ""}</div>
        ${errObj.hint ? `<div class="afb-hint"><span class="hint-k">修复指引</span>${esc(errObj.hint)}</div>` : ""}
      </div>`;
    }
    return `
    <div class="obs-block ok">
      <div class="obs-block-head">${Icons.check}<b>最近一次同步 · 成功</b>
        <span class="mono">${esc(fmtRel(last.at))}${last.duration_ms != null ? ` · 耗时 ${fmtDur(last.duration_ms)}` : ""} · ${obsTrigger(last)}</span></div>
      <div class="obs-block-body">注入 changed <b>${lr.changed ?? last.changed_files ?? "—"}</b> 文件 → 推送 pushed <b>${lr.pushed ?? last.pushed_files ?? "—"}</b> 文件` +
      (last.commits != null ? ` · ${last.commits} commit` : "") +
      (last.commit ? ` · <span class="mono">${esc(last.commit)}</span>` : "") + `</div>
    </div>`;
  }
  /* 最近一次失败示例（来自 history，演示增强字段） */
  function obsLastFailure(h) {
    const f = (h.history || []).find(x => x.outcome === "failure");
    if (!f) return "";
    return `
    <div class="obs-block bad">
      <div class="obs-block-head">${Icons.alert}<b>最近一次失败（示例）</b>
        ${f.code ? `<span class="chip red mono" style="font-size:10px">${esc(f.code)}</span>` : ""}
        <span class="mono">${esc(fmtRel(f.at))}</span></div>
      <div class="obs-block-body">阶段「${esc(f.stage)}」：${esc(f.error || f.message || "同步失败")}</div>
      ${f.hint ? `<div class="afb-hint"><span class="hint-k">修复指引</span>${esc(f.hint)}</div>` : ""}
    </div>`;
  }
  function obsHistoryRow(x) {
    const okk = x.outcome === "success";
    return `
    <div class="obs-h-row ${okk ? "ok" : "bad"}">
      <span class="obs-h-dot"></span>
      <span class="mono obs-h-at">${esc(fmtRel(x.at))}</span>
      <span class="obs-h-summary">${okk
        ? `注入 ${x.changed_files} · 推送 ${x.pushed_files} 文件 · ${x.commits} commit`
        : esc(x.error || x.message || "同步失败")}</span>
      <span class="chip ${okk ? "green" : "red"} form-badge">${obsTrigger(x)}</span>
    </div>`;
  }
  function obsUnavailable() {
    return `<div class="banner warn obs-wrap" style="grid-column:1/-1">${Icons.alert}<div class="bn-body">Obsidian 可观测数据不可用：<b>GET /api/v1/programs</b> 返回中缺少 <b>obsidian_sync</b> 条目或其 <b>health</b> 字段（契约：running / last_sync_at / last_result{changed,pushed,error} / error_streak / detail）</div></div>`;
  }

  /* ---------- 渲染与绑定 ---------- */
  function summaryChips(programs) {
    const n = f => programs.filter(f).length;
    const oneClick = n(p => (p.actions || []).some(a => a === "install" || a === "one-click"));
    const busy = n(p => p.status === "busy");
    return `
      <div style="grid-column:1/-1;display:flex;gap:10px;flex-wrap:wrap;align-items:center">
        <span class="chip green">${n(p => p.status === "running")} 运行中</span>
        ${busy ? `<span class="chip blue">${busy} 执行中</span>` : ""}
        <span class="chip amber">${n(p => p.status === "degraded")} 告警</span>
        <span class="chip gray">${n(p => p.status === "stopped" || p.status === "notinstalled")} 停止/未安装</span>
        <span class="chip purple">${oneClick} 一键形态</span>
        <span class="chip blue">${n(p => (p.actions || []).every(a => a !== "install" && a !== "one-click"))} 外部自管</span>
        <span class="chip amber" id="pgmUpdates">${n(p => p.latest !== p.version)} 个可更新</span>
        <span style="color:var(--text-faint);font-size:12.5px">动作结果与失败修复指引就地反馈 · busy 轮询 + WS 推送自动刷新</span>
      </div>`;
  }
  function renderAll() {
    const programs = programsCache.filter(p => p.id !== "obsidian_sync");
    const entry = obsEntry();
    const body = $("#pgmBody", mainEl);
    if (!body) return;
    body.innerHTML = summaryChips(programs) +
      programs.map(programCard).join("") +
      `<div class="obs-wrap">${entry && entry.health ? obsPanel(entry) : obsUnavailable()}</div>`;
    bindCards(body);
  }
  function bindCards(scope) {
    $$("[data-program]", scope).forEach(card => {
      if (card.id === "obsPanel") { bindObsPanel(card); return; }
      bindCardActions(card);
    });
  }
  function bindCardActions(card) {
    card.querySelectorAll("[data-action]").forEach(btn => {
      btn.addEventListener("click", () => onAction(btn, card));
    });
    /* V5 · 令牌一键复制（navigator.clipboard，降级 execCommand） */
    card.querySelectorAll("[data-copy-token]").forEach(btn => {
      btn.addEventListener("click", () => {
        copyText(btn.dataset.copyToken).then(ok => {
          if (ok) Toast.ok(`${btn.dataset.copyTokenName || "程序"} 登录令牌已复制`, "打开 WebUI 登录页直接粘贴即可");
          else Toast.err("复制失败", "请手动选中文本复制");
        });
      });
    });
  }
  function bindObsPanel(panel) {
    const btn = $("#obsSyncNow", panel);
    if (btn) btn.addEventListener("click", onSyncNow);
  }
  /* 就地更新单卡（保留反馈区，避免全页重渲染闪烁） */
  function updateCard(id) {
    const body = mainEl ? $("#pgmBody", mainEl) : null;
    if (!body) return;
    if (id === "obsidian_sync") { refreshObsPanel(false); return; }
    const p = programsCache.find(x => x.id === id);
    const node = body.querySelector(`[data-program="${id}"]`);
    if (!p || !node) { renderAll(); return; }
    const fresh = el(programCard(p).trim());
    node.replaceWith(fresh);
    bindCardActions(fresh);
  }
  function refreshObsPanel(flash) {
    const body = mainEl ? $("#pgmBody", mainEl) : null;
    if (!body) return;
    const node = $("#obsPanel", body);
    const entry = obsEntry();
    if (!node || !entry || !entry.health) return;
    const fresh = el(obsPanel(entry).trim());
    if (flash) fresh.classList.add("obs-flash");
    node.replaceWith(fresh);
    bindObsPanel(fresh);
  }

  /* ---------- 动作执行 ----------
     统一契约解析：res.ok === true → res.result{status,version,detail…}
                  res.ok === false → res.result{code?, error, hint}（扁平修复指引） */
  function stagesFor(p, action) {
    if (API.mode === "live") return ["指令已下发，等待后端返回…"];
    const g = STAGES[p.form === "docker" ? "docker" : "release"];
    return (g && g[action]) || ["执行中…"];
  }
  /* busy 轮询：动作执行期间反复 GET /programs —— 后端会把该程序置
     status="busy" + busy_action，卡片状态/按钮随之刷新（契约第 3 条）。 */
  function pollProgram(id) {
    let last = "";
    const t = setInterval(async () => {
      try {
        programsCache = await API.getPrograms();
        const p = programsCache.find(x => x.id === id);
        const j = p ? JSON.stringify(p) : "";
        if (j !== last) { last = j; updateCard(id); }
      } catch { /* 轮询失败静默，动作结果以统一响应为准 */ }
    }, POLL_MS);
    return { stop() { clearInterval(t); } };
  }
  async function onAction(btn, card) {
    const action = btn.dataset.action;
    const id = card.dataset.program;
    const p = programsCache.find(x => x.id === id);
    if (!p || !ACTION_META[action]) return;

    if (action === "log") { await openLogModal(p); return; }
    if (action === "check-update") { await doCheckLatest(id, p); return; }

    const m = ACTION_META[action];
    feedback[id] = { type: "busy", action, stage: stagesFor(p, action)[0] };
    updateCard(id);
    const stages = stagesFor(p, action);
    let si = 0;
    const stageTimer = setInterval(() => {
      const cardNode = mainEl && mainEl.querySelector(`[data-program="${id}"]`);
      if (cardNode && cardNode.querySelector("[data-progress-box]")) return;   // V5：进度条接管阶段文案（program:progress）
      si = Math.min(si + 1, stages.length - 1);
      const node = mainEl && mainEl.querySelector(`[data-program="${id}"] .afb-stage`);
      if (node) node.textContent = stages[si];
    }, 640);
    const poll = pollProgram(id);

    try {
      const res = await API.programAction(id, action);
      const r = (res && res.result) || {};
      if (res && res.ok === false) {
        if (r.running) {
          /* 契约：running 程序重复 start → result{running:true, pid}（明确拒绝）——
             展示为中性反馈而非故障，随后按钮按真实状态锁定 */
          feedback[id] = { type: "err", action, title: `${p.name} 启动被拒绝（已在运行）`,
            message: r.error || "已在运行，重复启动被拒绝", hint: "状态已按 /programs 刷新，启动按钮已按真实状态锁定。" };
          Toast.info(`${p.name} 启动被拒绝`, r.error || "已在运行");
        } else {
          feedback[id] = { type: "err", action, title: `${p.name} ${m.label}失败`, code: r.code,
            message: r.error || r.message || "未知错误" + (r.exit_code != null ? `（exit_code=${r.exit_code}）` : ""),
            hint: r.hint || (r.log_tail ? `进程尾部日志：${r.log_tail}` : "") };
          Toast.err(`${p.name} ${m.label}失败`, r.hint || r.error || "查看卡片内修复指引");
        }
      } else {
        feedback[id] = { type: "ok", action, title: `${p.name} ${m.label}完成`, detail: r.detail || `状态 → ${(STATUS[r.status] || STATUS.stopped).label}` };
        Toast.ok(`${p.name} ${m.label}完成`, r.detail || "");
      }
      programsCache = await API.getPrograms();   // 状态以 /programs 为准刷新（版本/徽标/进程/终态）
    } catch (e) {
      feedback[id] = { type: "err", action, title: `${p.name} ${m.label}失败`, message: e.message, hint: "后端不可达：确认 Console 后端已运行（设置页可配置地址）后重试。" };
      Toast.err(`${p.name} ${m.label}失败`, e.message);
    } finally {
      clearInterval(stageTimer);
      poll.stop();
      updateCard(id);
    }
  }
  /* 检查更新：GET /programs/{id}/check-latest（按钮名 check-update 来自 actions 数组） */
  async function doCheckLatest(id, p) {
    feedback[id] = { type: "busy", action: "check-update", stage: "正在查询上游最新版本…" };
    updateCard(id);
    try {
      const res = await API.programCheckLatest(id);
      const r = (res && res.result) || {};
      programsCache = await API.getPrograms();   // 发现新版 → latest 刷新 → 徽标点亮、更新解锁
      feedback[id] = { type: "upd", current: r.current, latest: r.latest, has_update: !!r.has_update, checkedAt: "刚刚检查" };
      if (r.has_update) Toast.info(`${p.name} 有新版本`, `当前 v${r.current} → 最新 v${r.latest}，可执行更新`);
      else Toast.ok(`${p.name} 已是最新`, `当前 v${r.current}`);
    } catch (e) {
      feedback[id] = { type: "err", title: `${p.name} 检查更新失败`, message: e.message };
      Toast.err(`${p.name} 检查更新失败`, e.message);
    }
    updateCard(id);
  }

  /* ---------- Obsidian 立即同步：POST /api/v1/programs/obsidian_sync/sync ---------- */
  async function onSyncNow() {
    const fb = feedback["obsidian_sync"];
    if (fb && fb.type === "busy") return;
    feedback["obsidian_sync"] = { type: "busy", action: "sync", stage: SYNC_STAGES[0] };
    refreshObsPanel(false);
    let si = 0;
    const stageTimer = setInterval(() => {
      si = Math.min(si + 1, SYNC_STAGES.length - 1);
      const node = mainEl && mainEl.querySelector("#obsPanel .afb-stage");
      if (node) node.textContent = SYNC_STAGES[si];
    }, 620);
    const poll = pollProgram("obsidian_sync");
    try {
      const res = await API.obsidianSyncNow();
      const r = (res && res.result) || {};
      programsCache = await API.getPrograms();   // health 契约字段以 /programs 为准刷新
      if (res && res.ok === false) {
        feedback["obsidian_sync"] = { type: "err", title: "立即同步失败", code: r.code, message: r.error || "未知错误", hint: r.hint };
        Toast.err("Obsidian 立即同步失败", r.hint || r.error || "");
      } else {
        feedback["obsidian_sync"] = { type: "ok", title: "立即同步完成",
          detail: `注入 changed ${r.changed_files ?? r.changed ?? "—"} 文件 → 推送 pushed ${r.pushed_files ?? r.pushed ?? "—"} 文件` +
            (r.commits != null ? ` · ${r.commits} commit` : "") +
            (r.duration_ms != null ? ` · 耗时 ${fmtDur(r.duration_ms)}` : "") };
        Toast.ok("Obsidian 同步完成", `注入 ${r.changed_files ?? r.changed} · 推送 ${r.pushed_files ?? r.pushed} 文件`);
      }
    } catch (e) {
      feedback["obsidian_sync"] = { type: "err", title: "立即同步失败", message: e.message,
        hint: "后端不可达：确认 Console 后端已运行后重试。" };
      Toast.err("Obsidian 立即同步失败", e.message);
    } finally {
      clearInterval(stageTimer);
      poll.stop();
      refreshObsPanel(true);
    }
  }

  /* ---------- WS 实时推送 → 自动刷新（桥接 document 事件） ----------
     obssync:push     ← WS topic obsidian.sync：同步器面板刷新（数据源 /programs）。
     program:status   ← WS topic program.status（体 {id,status,pid,progress?,webui?}）：状态变化 → 全页重渲染。
     program:progress ← WS topic program.progress：下载/安装/更新进度 → 进度条就地刷新
                        （字节型：百分比/已下载/总量/速度；阶段型：extract/pull 阶段文案）。
     program:webui    ← WS topic program.webui（体 {id,token,user,url}）：WebUI 凭据检测推送 → 面板闪一拍。 */
  function bindWsEvents() {
    if (wsBound) return;
    wsBound = true;
    document.addEventListener("obssync:push", async () => {
      if (App.current !== "programs") return;
      try {
        programsCache = await API.getPrograms();   // 面板数据 = /programs 的 obsidian_sync.health
        refreshObsPanel(true);   // 绿光环闪一拍 = 实时推送已生效
      } catch { /* 静默：面板保持上次数据 */ }
    });
    document.addEventListener("program:status", async () => {
      if (App.current !== "programs") return;
      try {
        programsCache = await API.getPrograms();
        renderAll();
      } catch { /* 静默 */ }
    });
    /* V5.1 · 进度就地刷新（WS program.progress 桥接）：字节型原地更新数值，
       阶段型/形态切换整体替换进度盒；轮询竞态下进度盒未挂出时兜底单卡重渲染 */
    document.addEventListener("program:progress", e => {
      if (App.current !== "programs") return;
      const d = e.detail || {};
      const card = mainEl && mainEl.querySelector(`[data-program="${d.id}"]`);
      if (!card) return;
      let box = card.querySelector("[data-progress-box]");
      if (!box) { updateCard(d.id); return; }
      const wantForm = isByteProgress(d) ? "byte" : "stage";
      if (box.dataset.progForm !== wantForm) {
        box.outerHTML = progressArea({ id: d.id, progress: d });
        box = card.querySelector("[data-progress-box]");
        if (!box) return;
      }
      if (wantForm === "byte") {
        const pct = Math.max(0, Math.min(100, d.percent ?? 0));
        const fill = box.querySelector("[data-dl-fill]");
        if (fill) fill.style.width = pct + "%";
        const bar = box.querySelector(".dl-bar");
        if (bar) bar.setAttribute("aria-valuenow", pct);
        const eta = d.speed_bps > 0 ? Math.max(0, Math.round((d.total - (d.downloaded || 0)) / d.speed_bps)) : null;
        const set = (sel, txt) => { const n = box.querySelector(sel); if (n) n.textContent = txt; };
        set("[data-dl-pct]", pct.toFixed(1) + "%" + (d.done ? " · 完成" : ""));
        set("[data-dl-speed]", d.speed_bps ? fmtSpeed(d.speed_bps) : "");
        set("[data-dl-eta]", !d.done && eta != null && eta > 0 ? "剩余 " + fmtEta(eta) : "");
      } else {
        const lbl = box.querySelector("[data-dl-label]");
        if (lbl) lbl.textContent = d.detail || "";
      }
      /* 阶段文案同步为当前进度阶段（stage 轮播让位） */
      const stage = card.querySelector(".afb-stage");
      if (stage) {
        const am = ACTION_META[d.phase];
        const phaseText = isByteProgress(d)
          ? (d.done ? "下载完成" : "下载中")
          : (d.phase === "pull" ? `拉取镜像 · ${d.detail || ""}` : d.phase === "extract" ? `收尾 · ${d.detail || ""}` : "执行中");
        stage.textContent = phaseText;
        void am;
      }
    });
    /* V5.1 · webui 凭据推送（WS topic program.webui 桥接）：面板闪一拍 + toast
       （数据源以 GET /programs 的 webui 字段为准刷新） */
    document.addEventListener("program:webui", async e => {
      if (App.current !== "programs") return;
      const d = e.detail || {};
      try {
        programsCache = await API.getPrograms();
        renderAll();
      } catch { /* 静默 */ }
      const card = mainEl && mainEl.querySelector(`[data-program="${d.id}"]`);
      const box = card && card.querySelector("[data-token-box]");
      if (box) box.classList.add("flash");
      Toast.ok(`${d.name || "程序"} WebUI 凭据已检测到`, "WS 推送（program.webui）· 已在卡片高亮展示，一键复制即可登录网页端");
    });
  }

  /* ---------- V5 · 全量日志查看弹窗 ----------
     升级点（37 实机反馈「日志只有片段」）：全量日志（含启动段）+ 搜索高亮/过滤 +
     自动滚底（新行跟随）+ 复制全部；运行中程序日志跟随（2.6s 一拍，live 由 WS 日志流驱动）。
     数据源：GET /api/v1/programs/{id}/logs?full=1；SnowLuma 启动段含令牌行，可搜索定位。 */
  const LG_CLS = { info: "lg-info", ok: "lg-ok", warn: "lg-warn", err: "lg-err", dim: "lg-dim" };
  let logModal = null;   // { overlay, id, p, lines, follow, filterOnly, followTimer, lastFocus }
  let logPauseTimer = null;   // 用户上滚判定（延迟复核，防程序化滚动动画误判）

  /* 行解析（契约同构：lines 为原始字符串行 "[HH:MM:SS] LEVEL  message"，前端解析着色） */
  const LG_RE = /^\[?(\d{2}:\d{2}:\d{2})\]?\s+(INFO|OK|WARN|ERROR|ERR|DIM|DEBUG)?\s*(.*)$/;
  function parseLogLine(line) {
    if (typeof line !== "string") return { t: line.t || "", lvl: line.lvl || "info", msg: line.msg || "" };
    const m = LG_RE.exec(line.trim());
    if (!m) return { t: "", lvl: "info", msg: line };
    const raw = (m[2] || "").toUpperCase();
    const lvl = /WARN/.test(raw) ? "warn" : /ERROR|ERR/.test(raw) ? "err" : raw === "OK" ? "ok" : raw === "DIM" || raw === "DEBUG" ? "dim" : "info";
    return { t: m[1], lvl, msg: m[3] || "" };
  }

  function logLineHTML(line, q) {
    const { t, lvl, msg } = parseLogLine(line);
    const escd = esc(msg);
    let hi = escd;
    if (q) {
      const eq = esc(q).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      try { hi = escd.replace(new RegExp(eq, "gi"), m => `<mark>${m}</mark>`); } catch (e) { hi = escd; }
    }
    return `<div class="log-line ${LG_CLS[lvl] || "lg-info"}"><span class="lg-t">${esc(t)}</span><span class="lg-msg">${hi}</span></div>`;
  }

  async function openLogModal(p) {
    closeLogModal();   // 单实例
    logModal = { id: p.id, p, lines: [], follow: true, filterOnly: false, followTimer: null };
    logModal.lastFocus = document.activeElement;
    const overlay = el(`
      <div class="log-modal" role="dialog" aria-modal="true" aria-labelledby="logModalTitle">
        <div class="log-panel">
          <div class="log-head">
            <div class="log-title">
              <span class="program-logo" style="--logo-c:${p.color}">${esc(p.logo)}</span>
              <div>
                <div class="lt-name" id="logModalTitle">${esc(p.name)} · 全量日志</div>
                <div class="lt-sub mono" data-log-sub>正在读取…</div>
              </div>
            </div>
            <button class="btn ghost sm" data-log-close aria-label="关闭日志查看器">${Icons.x}<span>关闭</span></button>
          </div>
          <div class="log-tools">
            <div class="log-search">
              ${Icons.search}
              <input type="search" data-log-q placeholder="搜索日志（如：credentials / password / WARN）…" aria-label="搜索日志" />
              <span class="log-count mono" data-log-count>—</span>
            </div>
            <label class="log-toggle"><input type="checkbox" data-log-follow checked /><span>自动滚底</span></label>
            <label class="log-toggle"><input type="checkbox" data-log-filter /><span>只看匹配</span></label>
            <button class="btn ghost sm" data-log-copy>${Icons.copy}<span>复制全部</span></button>
            <button class="btn ghost sm" data-log-bottom>${Icons.arrowDown}<span>回到底部</span></button>
          </div>
          <div class="log-body mono" data-log-body tabindex="0" aria-label="日志内容（可滚动）"></div>
          <div class="log-foot">
            ${(p.webui && p.webui.token)
              ? `<div class="log-token"><span class="lt-k">WebUI 登录凭据（${esc(p.webui.user || "user")}）</span><code class="mono">${esc(p.webui.token)}</code><button class="btn primary sm" data-log-token-copy>一键复制</button></div>`
              : `<span class="lf-hint">日志全量落盘（含启动段）· 运行中程序自动跟随新行</span>`}
            <span class="lf-stats mono" data-log-stats></span>
          </div>
        </div>
      </div>`.trim());
    document.body.appendChild(overlay);
    logModal.overlay = overlay;

    /* 交互绑定 */
    overlay.addEventListener("mousedown", e => { if (e.target === overlay) closeLogModal(); });
    overlay.querySelector("[data-log-close]").addEventListener("click", closeLogModal);
    const qInput = overlay.querySelector("[data-log-q]");
    qInput.addEventListener("input", () => renderLogLines());
    qInput.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); qInput.value = ""; renderLogLines(); } });
    overlay.querySelector("[data-log-follow]").addEventListener("change", e => {
      logModal.follow = e.target.checked;
      if (logModal.follow) scrollLogBottom(true);
    });
    overlay.querySelector("[data-log-filter]").addEventListener("change", e => {
      logModal.filterOnly = e.target.checked;
      renderLogLines();
    });
    overlay.querySelector("[data-log-copy]").addEventListener("click", () => {
      copyText(logModal.lines.join("\n")).then(ok => {   // 契约：原始字符串行原样复制
        if (ok) Toast.ok("日志已复制", `${logModal.lines.length} 行原始日志已复制到剪贴板`);
        else Toast.err("复制失败", "请手动选择复制");
      });
    });
    overlay.querySelector("[data-log-bottom]").addEventListener("click", () => {
      const cb = overlay.querySelector("[data-log-follow]");
      cb.checked = true;
      logModal.follow = true;
      scrollLogBottom(true);
    });
    const tokenBtn = overlay.querySelector("[data-log-token-copy]");
    if (tokenBtn) tokenBtn.addEventListener("click", () => {
      const token = (p.webui && p.webui.token) || "";
      copyText(token).then(ok => {
        if (ok) Toast.ok(`${p.name} 登录凭据已复制`, `打开 ${esc((p.webui && p.webui.url) || "WebUI")} · 用户 ${esc((p.webui && p.webui.user) || "—")} · 密码粘贴此值`);
        else Toast.err("复制失败", "请手动选中文本复制");
      });
    });
    const body = overlay.querySelector("[data-log-body]");
    body.addEventListener("scroll", () => {
      if (!logModal) return;
      /* 暂停跟随判定延迟复核：程序化滚动（贴底/平滑动画）的中间态 dist 会瞬时 >60，
         260ms 后复核仍 >60 才判定为用户上滚（瞬态自愈，不误取消自动滚底） */
      clearTimeout(logPauseTimer);
      logPauseTimer = setTimeout(() => {
        if (!logModal) return;
        const dist = body.scrollHeight - body.scrollTop - body.clientHeight;
        if (dist > 60 && logModal.follow) {
          logModal.follow = false;
          const cb = overlay.querySelector("[data-log-follow]");
          if (cb) cb.checked = false;
        }
      }, 260);
    });
    document.addEventListener("keydown", logModalEsc);
    qInput.focus();

    /* 加载全量日志（契约分页参数：offset=0 从文件头 · limit=0 读到结尾 = 全量） */
    try {
      const res = await API.getProgramLogs(p.id, { offset: 0, limit: 0 });
      logModal.lines = (res && res.lines) || [];
      renderLogLines();
      scrollLogBottom(false);
      startLogFollow();
      /* 字体加载完成后行高会变化：贴底视图被撑开 → 重新贴底一次 */
      if (document.fonts && document.fonts.ready && document.fonts.ready.then) {
        document.fonts.ready.then(() => { if (logModal && logModal.follow) scrollLogBottom(false); }).catch(() => {});
      }
    } catch (e) {
      body.innerHTML = `<div class="log-empty">${Icons.alert} 日志加载失败：${esc(e.message)}</div>`;
    }
  }

  function logModalEsc(e) { if (e.key === "Escape") closeLogModal(); }

  function renderLogLines() {
    if (!logModal) return;
    const q = (logModal.overlay.querySelector("[data-log-q]").value || "").trim();
    const body = logModal.overlay.querySelector("[data-log-body]");
    const qlc = q.toLowerCase();
    const shown = (logModal.filterOnly && q)
      ? logModal.lines.filter(l => String(l).toLowerCase().includes(qlc))
      : logModal.lines;
    body.innerHTML = shown.length
      ? shown.map(l => logLineHTML(l, q)).join("")
      : `<div class="log-empty">${q ? `无匹配「${esc(q)}」的日志行` : "暂无日志"}</div>`;
    /* 匹配统计：行数 + 命中次数（原始字符串行全文匹配） */
    const cnt = logModal.overlay.querySelector("[data-log-count]");
    if (q) {
      let hits = 0, rows = 0;
      logModal.lines.forEach(l => {
        const s = String(l).toLowerCase();
        const n = qlc ? s.split(qlc).length - 1 : 0;
        if (n > 0) rows++;
        hits += n;
      });
      cnt.textContent = `${rows} 行 · ${hits} 处`;
    } else cnt.textContent = `${logModal.lines.length} 行`;
    /* 标题栏与底栏统计 */
    const sub = logModal.overlay.querySelector("[data-log-sub]");
    const running = logModal.p.status === "running";
    sub.textContent = `${logModal.lines.length} 行 · 含启动段 · GET /api/v1/programs/${logModal.id}/logs?offset=0&limit=0${running ? " · 跟随中" : ""}`;
    const stats = logModal.overlay.querySelector("[data-log-stats]");
    const last = logModal.lines[logModal.lines.length - 1];
    const lastT = last ? parseLogLine(last).t : "";
    stats.textContent = logModal.lines.length ? `尾行 ${lastT} · ${logModal.lines.length} 行` : "";
    if (logModal.follow) scrollLogBottom(false);
  }

  function scrollLogBottom(smooth) {
    if (!logModal) return;
    const body = logModal.overlay.querySelector("[data-log-body]");
    /* behavior 显式指定（auto 会遵循 CSS scroll-behavior）*/
    body.scrollTo({ top: body.scrollHeight, behavior: smooth ? "smooth" : "instant" });
  }

  /* 运行中程序日志跟随：2.6s 拉一拍新行（live 模式由 WS 日志流驱动，此处返回空） */
  function startLogFollow() {
    stopLogFollow();
    if (!logModal) return;
    logModal.followTimer = setInterval(async () => {
      if (!logModal) return stopLogFollow();
      const cur = programsCache.find(x => x.id === logModal.id);
      if (!cur || cur.status !== "running") return;    // 仅运行中程序有新行
      try {
        const res = await API.programLogFollow(logModal.id);
        const lines = (res && res.lines) || [];
        if (!lines.length || !logModal) return;
        logModal.lines.push(...lines);
        if (logModal.lines.length > 600) logModal.lines.splice(0, logModal.lines.length - 600);   // 视窗保护
        const q = (logModal.overlay.querySelector("[data-log-q]").value || "").trim();
        if (!q || !logModal.filterOnly) {
          const body = logModal.overlay.querySelector("[data-log-body]");
          body.insertAdjacentHTML("beforeend", lines.map(l => logLineHTML(l, q)).join(""));
          if (logModal.follow) scrollLogBottom(true);
          const stats = logModal.overlay.querySelector("[data-log-stats]");
          const last = logModal.lines[logModal.lines.length - 1];
          stats.textContent = `尾行 ${last.t} · ${logModal.lines.length} 行`;
        } else renderLogLines();   // 过滤模式下重渲染
      } catch { /* 跟随失败静默 */ }
    }, 2600);
  }
  function stopLogFollow() {
    if (logModal && logModal.followTimer) { clearInterval(logModal.followTimer); logModal.followTimer = null; }
  }

  function closeLogModal() {
    if (!logModal) return;
    stopLogFollow();
    clearTimeout(logPauseTimer);
    document.removeEventListener("keydown", logModalEsc);
    logModal.overlay.remove();
    if (logModal.lastFocus && logModal.lastFocus.focus) logModal.lastFocus.focus();
    logModal = null;
  }

  /* ---------- 页面入口 ---------- */
  function render(main) {
    mainEl = main;
    main.innerHTML = `
      <div class="page-head">
        <div>
          <div class="page-title">${Icons.server} 第三方程序</div>
          <div class="page-desc">V5：状态真实化（跨刷新稳定 · 按钮按真实状态锁定）· 全量日志弹窗（搜索/自动滚底/跟随）· SnowLuma 登录令牌高亮一键复制 · 下载/安装/更新实时进度条；busy 轮询 1.2s + WS program.status / progress / token 推送自动刷新。</div>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn ghost" id="pgmResetDemo" title="清空演示状态持久化，恢复出厂演示数据（running 初始态、令牌、可更新徽标）">${Icons.tag}<span>重置演示</span></button>
          <button class="btn ghost" id="pgmRefresh">${Icons.refresh}<span>刷新</span></button>
        </div>
      </div>
      <div id="pgmBody" class="grid cols-2" style="margin-top:16px">
        <div class="empty" style="grid-column:1/-1">${Icons.clock} 正在读取进程状态…</div>
      </div>`;
    $("#pgmRefresh", main).addEventListener("click", load);
    $("#pgmResetDemo", main).addEventListener("click", () => {
      if (MOCK.resetPrograms) MOCK.resetPrograms();
      Toast.ok("演示数据已重置", "程序状态/版本/令牌已恢复出厂演示态");
      load();
    });
    bindWsEvents();
    load();
  }

  async function load() {
    const body = mainEl && $("#pgmBody", mainEl);
    if (!body) return;
    body.innerHTML = `<div class="empty" style="grid-column:1/-1">${Icons.clock} 正在读取进程状态…</div>`;
    try {
      programsCache = await API.getPrograms();   // obsidian_sync.health 与程序状态同源
      renderAll();
    } catch (err) {
      body.innerHTML = `<div class="banner warn" style="grid-column:1/-1">${Icons.alert}<div class="bn-body">加载失败：${esc(err.message)}</div></div>`;
    }
  }

  App.registerPage("programs", { title: "第三方程序", icon: Icons.server, render });
})();
