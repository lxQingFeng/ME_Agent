/* ============================================================
   page-heartbeat.js — 总览 · 世界心跳实时监控
   数据源：API.getWorldState() / API.getWorldTimeline()
   ============================================================ */
"use strict";

(function () {
  const NS = "hb";
  let lastState = null;

  function circadianChip(c) {
    const map = { morning: ["清晨", "green"], afternoon: ["午后", "blue"], evening: ["傍晚", "amber"], night: ["深夜", "purple"] };
    const [label, tone] = map[c] || ["—", "gray"];
    return `<span class="chip ${tone}">${label}</span>`;
  }
  function intensityChip(i) {
    const map = { light: ["低强度", "cyan"], medium: ["中强度", "blue"], high: ["高强度", "amber"] };
    const [label, tone] = map[i] || ["—", "gray"];
    return `<span class="chip ${tone}">${label}</span>`;
  }
  function interruptChip(v) {
    const map = { high: ["易打断", "green"], medium: ["中", "amber"], low: ["勿扰", "red"] };
    const [label, tone] = map[v] || ["—", "gray"];
    return `<span class="chip ${tone}">${label}</span>`;
  }
  function sleepChip(s) {
    const map = { awake: ["清醒", "green"], drowsy: ["困倦", "amber"], asleep: ["已入睡·静默", "purple"] };
    const [label, tone] = map[s] || [s, "gray"];
    return `<span class="chip ${tone}">${label}</span>`;
  }

  /* 心情二维图：valence(x) × arousal(y)，带象限标签 */
  function drawMood(canvas, mood) {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 320, h = 190;
    canvas.width = w * dpr; canvas.height = h * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    const pad = 26;
    const x0 = pad, x1 = w - 8, y0 = 8, y1 = h - 24;
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;

    /* 网格 + 轴 */
    ctx.strokeStyle = "#27334F"; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const gx = x0 + ((x1 - x0) * i) / 4;
      ctx.beginPath(); ctx.moveTo(gx, y0); ctx.lineTo(gx, y1); ctx.stroke();
    }
    for (let i = 0; i <= 4; i++) {
      const gy = y0 + ((y1 - y0) * i) / 4;
      ctx.beginPath(); ctx.moveTo(x0, gy); ctx.lineTo(x1, gy); ctx.stroke();
    }
    ctx.strokeStyle = "#3A4A6E";
    ctx.beginPath(); ctx.moveTo(cx, y0); ctx.lineTo(cx, y1); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x0, cy); ctx.lineTo(x1, cy); ctx.stroke();

    /* 象限标签 */
    ctx.fillStyle = "#8B99B5"; ctx.font = "600 11.5px " + getComputedStyle(document.body).getPropertyValue("--font-sans");
    ctx.textAlign = "center";
    ctx.fillText("兴奋", cx + 36, y0 + 14); ctx.fillText("平静", cx - 36, y0 + 14);
    ctx.fillText("低落", cx - 36, y1 - 6); ctx.fillText("烦躁", cx + 36, y1 - 6);
    ctx.fillText("valence →", x1 - 40, cy - 6);
    ctx.save(); ctx.translate(x0 + 8, cy + 40); ctx.rotate(-Math.PI / 2); ctx.fillText("arousal →", 0, 0); ctx.restore();

    /* 轨迹（近几个 tick 抖动，演示） */
    const pts = [];
    for (let i = 6; i >= 1; i--) {
      const dv = (Math.sin(Date.now() / 30000 + i) * 0.08) - (i * 0.015);
      pts.push([mood.valence + dv, mood.arousal - dv * 0.6]);
    }
    pts.push([mood.valence, mood.arousal]);
    const toXY = (v, a) => [cx + v * ((x1 - x0) / 2 - 6), cy - a * ((y1 - y0) / 2 - 6)];
    ctx.strokeStyle = "rgba(167,139,250,.5)"; ctx.lineWidth = 1.5;
    ctx.beginPath();
    pts.forEach((p, i) => { const [x, y] = toXY(p[0], p[1]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke();

    /* 当前点 */
    const [px, py] = toXY(mood.valence, mood.arousal);
    const glow = ctx.createRadialGradient(px, py, 0, px, py, 16);
    glow.addColorStop(0, "rgba(34,197,94,.5)"); glow.addColorStop(1, "rgba(34,197,94,0)");
    ctx.fillStyle = glow;
    ctx.beginPath(); ctx.arc(px, py, 16, 0, 7); ctx.fill();
    ctx.fillStyle = "#22C55E";
    ctx.beginPath(); ctx.arc(px, py, 5, 0, 7); ctx.fill();
    ctx.strokeStyle = "#0B1120"; ctx.lineWidth = 2; ctx.stroke();

    /* 标签 */
    ctx.fillStyle = "#F1F5F9"; ctx.font = "600 12px " + getComputedStyle(document.body).getPropertyValue("--font-sans");
    ctx.fillText(mood.label, px, py - 12);
  }

  function meterHTML(name, val, tone) {
    return `<div class="meter-row" data-ns="${NS}">
      <span class="m-name">${name}</span>
      <div class="meter" data-tone="${tone}" role="progressbar" aria-valuenow="${Math.round(val)}" aria-valuemin="0" aria-valuemax="100" aria-label="${name}">
        <div class="meter-fill" style="width:${Math.round(val)}%"></div>
      </div>
      <span class="m-val">${Math.round(val)}</span>
    </div>`;
  }

  function renderNarrative(w) {
    const t = new Date(w.time.wall_clock);
    const env = w.environment.weather;
    const cur = w.current;
    return `【当前状态 ${Fmt.date(t)} ${Fmt.weekday(t)} ${Fmt.hm(t)}｜${({ morning: "晨", afternoon: "午后", evening: "傍晚", night: "夜" })[w.time.circadian]}｜${env.condition} ${env.temp_c}℃】我在${cur.location}${cur.activity.name}（${cur.task}，${cur.activity.start} 开始，预计 ${cur.activity.planned_end} 结束）。精力 ${w.body.energy}/100${w.body.energy < 25 ? "，很累了" : w.body.energy > 70 ? "，状态不错" : ""}；心情${w.mind.mood.label}；社交电量 ${w.social.battery}。我想要：${w.mind.wants.join("、")}。接下来打算${cur.intent}。`;
  }

  function render(main) {
    main.innerHTML = `
      <div class="page-head">
        <div>
          <div class="page-title">${Icons.activity}<span class="p-ico-wrap" hidden></span>总览 · 世界心跳</div>
          <div class="page-desc">Agent 的实时生命状态 —— 由心跳引擎纯规则推进（每 5 分钟一拍，无 LLM），LLM 只读不写。演示模式已加速心跳。</div>
        </div>
        <div class="pulse-row"><span class="pulse-dot"></span><span style="color:var(--text-dim);font-size:12.5px">实时刷新中</span></div>
      </div>

      <div class="banner info" id="hbBanner">
        ${Icons.info}<div class="bn-body">当前为 <b>Mock 演示数据</b>（对齐方案 §2.3 WorldState 字段模型）。全栈开发后端契约定稿后，在「设置」页切换 Live 模式即可无缝对接。</div>
      </div>

      <div id="hbBody"><div class="empty">${Icons.clock} 正在加载世界状态…</div></div>
    `;

    const body = $("#hbBody", main);
    API.getWorldState().then(w => {
      lastState = w;
      API.getWorldTimeline().then(tl => {
        body.innerHTML = buildBody(w, tl);
        const canvas = $(`[data-mood-canvas="${NS}"]`, main);
        if (canvas) { drawMood(canvas, w.mind.mood); window.addEventListener("resize", () => lastState && drawMood(canvas, lastState.mind.mood)); }
        bindActions(main, w);
      });
    }).catch(err => {
      body.innerHTML = `<div class="banner warn">${Icons.alert}<div class="bn-body">加载失败：${esc(err.message)}</div></div>`;
    });

    /* 心跳 tick → 就地刷新数值（不整页重渲染） */
    document.addEventListener("world:tick", function onTick() {
      if (App.current !== "overview" || !lastState) return;
      const w = MOCK.worldState; // 演示引擎直接持有最新态
      refreshLive(w);
    });
  }

  function buildBody(w, tl) {
    const cur = w.current, env = w.environment.weather;
    const t = new Date(w.time.wall_clock);
    return `
    <div class="grid cols-3" style="margin-top:16px">

      <!-- 当前活动大卡 -->
      <div class="card span-2" data-activity-card>
        <div class="card-head">
          <div>
            <div class="card-title">${Icons.mapPin} 我在 · ${esc(cur.location)}</div>
            <div class="card-sub">位置图节点 · 计划块 ${esc(cur.activity.start)} → ${esc(cur.activity.planned_end)}</div>
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            ${intensityChip(cur.activity.intensity)} ${interruptChip(cur.activity.interruptible)} ${sleepChip(w.body.sleep_state)}
          </div>
        </div>
        <div style="display:flex;align-items:baseline;gap:14px;flex-wrap:wrap">
          <span style="font-size:26px;font-weight:800">${esc(cur.activity.name)}</span>
          <span class="chip blue"><span class="chip-dot"></span>${esc(cur.activity.id)}</span>
        </div>
        <div style="margin-top:10px;color:var(--text-dim);line-height:1.7">
          <div><b style="color:var(--text)">目的任务：</b>${esc(cur.task)}</div>
          <div><b style="color:var(--text)">当下意图：</b>${esc(cur.intent)}</div>
        </div>
        <div class="card" style="margin-top:14px;background:var(--bg-elev);border-style:dashed">
          <div class="card-title" style="font-size:12px;color:var(--text-faint)">${Icons.doc} 状态渲染器输出（喂给 LLM 的就是这段人话）</div>
          <div class="mono" style="font-size:12px;color:var(--text-dim);line-height:1.8;margin-top:6px" data-narrative>${esc(renderNarrative(w))}</div>
        </div>
      </div>

      <!-- 时间 & 天气 -->
      <div class="card">
        <div class="card-head">
          <div class="card-title">${Icons.clock} 时间与天气</div>
          ${circadianChip(w.time.circadian)}
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
          <div class="stat">
            <span class="stat-label">${Fmt.weekday(t)} 虚拟时钟</span>
            <span class="stat-value" data-wall-clock>${Fmt.hm(t)}</span>
            <span class="stat-hint">= 真实时间 Asia/Shanghai</span>
          </div>
          <div class="stat">
            <span class="stat-label">天气 · ${esc(env.city)}</span>
            <span class="stat-value">${env.temp_c}℃</span>
            <span class="stat-hint">${esc(env.condition)} · Open-Meteo 30min</span>
          </div>
        </div>
        <div style="margin-top:12px" class="banner ok" style="padding:8px 12px">
          <span style="font-size:12.5px;color:var(--text-dim)">事件溯源版本 <b class="mono" style="color:var(--accent)" data-ws-version>v${w.meta.version}</b> · 快照每日 00:10 导出进 git 审计</span>
        </div>
      </div>
    </div>

    <div class="grid cols-3" style="margin-top:14px">
      <!-- 身体 & 社交计量 -->
      <div class="card">
        <div class="card-head">
          <div class="card-title">${Icons.battery} 身体与社交动力学</div>
          <span class="chip gray">规则推进 · 每 tick</span>
        </div>
        ${meterHTML("精力余量", w.body.energy, "energy")}
        ${meterHTML("饥饿感", w.body.hunger, "hunger")}
        ${meterHTML("社交电量", w.social.battery, "social")}
        ${meterHTML("压力", w.mind.stress, "stress")}
        <div class="field-help" style="margin-top:12px">社交电量 &lt;20 自动潜水（只被 @ 时简短回应）；精力 &lt;25 回复 ≤2 句。</div>
      </div>

      <!-- 心情二维图 -->
      <div class="card">
        <div class="card-head">
          <div class="card-title">${Icons.heart} 心情 · 二维情绪空间</div>
          <span class="chip pink" data-mood-label>${esc(w.mind.mood.label)}</span>
        </div>
        <canvas class="mood-canvas" data-mood-canvas="${NS}" height="190" role="img" aria-label="情绪二维图：效价 ${w.mind.mood.valence}，唤醒度 ${w.mind.mood.arousal}，标签 ${w.mind.mood.label}"></canvas>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-faint);margin-top:4px">
          <span>valence（好↔坏）<b class="mono" style="color:var(--text-dim)" data-mood-v>${w.mind.mood.valence}</b></span>
          <span>arousal（激↔静）<b class="mono" style="color:var(--text-dim)" data-mood-a>${w.mind.mood.arousal}</b></span>
        </div>
      </div>

      <!-- 我想要的 & 打断队列 -->
      <div class="card">
        <div class="card-head">
          <div class="card-title">${Icons.spark} 想要 & 待处理</div>
          <span class="chip gray">wants ≤3 · 24h TTL</span>
        </div>
        <ul style="list-style:none;display:flex;flex-direction:column;gap:8px">
          ${w.mind.wants.map(x => `<li style="display:flex;gap:8px;align-items:baseline;font-size:13.5px;color:var(--text-dim)"><span style="color:var(--pink)">◆</span>${esc(x)}</li>`).join("")}
        </ul>
        <div class="card-title" style="margin-top:14px;font-size:12px;color:var(--text-faint)">${Icons.zap} 打断队列（interrupt_queue）</div>
        <div style="display:flex;flex-direction:column;gap:7px;margin-top:6px">
          ${(cur.interrupt_queue || []).map(q => `
            <div style="display:flex;align-items:center;gap:8px;font-size:12.5px;background:var(--bg-elev);border:1px solid var(--border);border-radius:8px;padding:7px 10px">
              <span class="chip ${q.priority === "P0" ? "red" : "gray"}" style="font-size:10px;padding:1px 7px">${q.priority}</span>
              <span style="color:var(--text-dim);flex:1">${esc(q.desc)}</span>
              <span class="mono" style="color:var(--text-faint);font-size:11px">${esc(q.arrived)}</span>
            </div>`).join("") || `<span style="color:var(--text-faint);font-size:12.5px">队列为空，岁月静好</span>`}
        </div>
      </div>
    </div>

    <div class="grid cols-2" style="margin-top:14px">
      <!-- 转移时间线 -->
      <div class="card">
        <div class="card-head">
          <div class="card-title">${Icons.refresh} 最近状态转移</div>
          <span class="chip gray">events.db 尾部</span>
        </div>
        <div class="timeline" data-timeline>
          ${tl.map(x => `
            <div class="tl-item ${x.hot ? "hot" : ""}">
              <div class="tl-time">${esc(x.at)} · ${esc(x.reason)}</div>
              <div class="tl-text"><b>${esc(x.from)}</b> → <b>${esc(x.to)}</b></div>
            </div>`).join("")}
        </div>
      </div>

      <!-- 转移图约束 -->
      <div class="card">
        <div class="card-head">
          <div class="card-title">${Icons.gauge} 转移图（guard 约束）</div>
          <span class="chip green">零非法转移</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:8px">
          ${MOCK.activityEdges.map(e => `<div class="mono" style="font-size:12px;background:var(--bg-elev);border:1px solid var(--border);border-radius:8px;padding:8px 12px;color:var(--text-dim)">${esc(e)}</div>`).join("")}
        </div>
        <div class="field-help" style="margin-top:10px">跑步 → 睡觉 这类跳变无边，guard 直接拒绝并落审计（llm_proposal_rejected:edge_missing）。</div>
      </div>
    </div>`;
  }

  /* tick 驱动的就地刷新 */
  function refreshLive(w) {
    const q = sel => document.querySelector(`[data-ns="${NS}"] ${sel}`) || document.querySelector(sel);
    const cur = w.current;
    const activityCard = $("[data-activity-card]");
    if (activityCard) {
      const nameEl = activityCard.querySelector("span[style*='26px']");
      if (nameEl) nameEl.textContent = cur.activity.name;
      const meta = activityCard.querySelectorAll("div[color\\:var(----text)] b, b");
      const bs = activityCard.querySelectorAll("b");
      if (bs.length >= 2) { bs[0].nextSibling; }
      const taskDiv = activityCard.children[2];
      if (taskDiv) {
        taskDiv.innerHTML = `<div><b style="color:var(--text)">目的任务：</b>${esc(cur.task)}</div><div><b style="color:var(--text)">当下意图：</b>${esc(cur.intent)}</div>`;
      }
      const chipId = activityCard.querySelector(".chip.blue");
      if (chipId) chipId.innerHTML = `<span class="chip-dot"></span>${esc(cur.activity.id)}`;
    }
    const narrative = $("[data-narrative]");
    if (narrative) narrative.textContent = renderNarrative(w);
    const wallClock = $("[data-wall-clock]");
    if (wallClock) wallClock.textContent = Fmt.hm(new Date(w.time.wall_clock));
    const wsVer = $("[data-ws-version]");
    if (wsVer) wsVer.textContent = "v" + w.meta.version;

    /* meters */
    const meters = document.querySelectorAll(`.meter-row[progressbar], .meter-row`);
    meters.forEach(row => {
      const name = row.querySelector(".m-name").textContent;
      const val = name === "精力余量" ? w.body.energy : name === "饥饿感" ? w.body.hunger : name === "社交电量" ? w.social.battery : w.mind.stress;
      const fill = row.querySelector(".meter-fill");
      const valEl = row.querySelector(".m-val");
      if (fill) fill.style.width = Math.round(val) + "%";
      if (valEl) valEl.textContent = Math.round(val);
      const bar = row.querySelector(".meter");
      if (bar) bar.setAttribute("aria-valuenow", Math.round(val));
    });

    const moodLabel = $("[data-mood-label]");
    if (moodLabel) moodLabel.textContent = w.mind.mood.label;
    const mv = $("[data-mood-v]"), ma = $("[data-mood-a]");
    if (mv) mv.textContent = w.mind.mood.valence;
    if (ma) ma.textContent = w.mind.mood.arousal;
    const canvas = $(`[data-mood-canvas="${NS}"]`);
    if (canvas) drawMood(canvas, w.mind.mood);

    const tl = $("[data-timeline]");
    if (tl) {
      tl.innerHTML = MOCK.transitions.map(x => `
        <div class="tl-item ${x.hot ? "hot" : ""}">
          <div class="tl-time">${esc(x.at)} · ${esc(x.reason)}</div>
          <div class="tl-text"><b>${esc(x.from)}</b> → <b>${esc(x.to)}</b></div>
        </div>`).join("");
    }
  }

  function bindActions(main, w) {
    /* 「追问 Agent」→ 对话流 */
    main.querySelectorAll("[data-goto-chat]").forEach(b => b.addEventListener("click", () => { location.hash = "#/chat"; }));
  }

  App.registerPage("overview", {
    title: "总览",
    icon: Icons.activity,
    render,
  });
})();
