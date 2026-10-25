/* ============================================================
   page-programs.js — 第三方程序管理（下载/更新/一键接入/启停/进程状态）
   数据源：API.getPrograms() / API.programAction()
   ============================================================ */
"use strict";

(function () {

  const STATUS = {
    running:   { label: "运行中", tone: "ok",   pulse: true },
    stopped:   { label: "已停止", tone: "off",  pulse: false },
    degraded:  { label: "告警",   tone: "warn", pulse: true },
    notinstalled: { label: "未安装", tone: "off", pulse: false },
  };
  const ACTION_META = {
    install:  { label: "下载安装", icon: "download", cls: "primary" },
    update:   { label: "更新",     icon: "refresh", cls: "" },
    start:    { label: "启动",     icon: "play",    cls: "primary" },
    stop:     { label: "停止",     icon: "pause",   cls: "danger ghost" },
    restart:  { label: "重启",     icon: "refresh", cls: "" },
    "one-click": { label: "一键接入", icon: "plug", cls: "" },
    log:      { label: "日志",     icon: "doc",     cls: "ghost" },
  };

  function statusChip(st) {
    const s = STATUS[st] || STATUS.stopped;
    return `<span class="svc-tag"><span class="svc-dot" data-state="${s.tone}"></span>${s.label}</span>`;
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
      <div><span class="proc-k">重启次数</span><span class="mono proc-v">${pr.restarts ?? 0}</span></div>
      <div><span class="proc-k">版本</span><span class="mono proc-v">${esc(p.version)}${p.latest !== p.version ? ` <span class="chip amber" style="font-size:10px;padding:0 6px">可更新 ${esc(p.latest)}</span>` : ""}</span></div>
    </div>`;
  }

  function programCard(p) {
    const acts = (p.actions || []).filter(a => ACTION_META[a]);
    return `
    <div class="card program-card" data-program="${p.id}" data-status="${p.status}">
      <div class="card-head">
        <div style="display:flex;gap:12px;align-items:center;min-width:0">
          <span class="program-logo" style="--logo-c:${p.color}">${esc(p.logo)}</span>
          <div style="min-width:0">
            <div class="card-title">${esc(p.name)} <span class="chip gray" style="font-size:10px;padding:1px 7px;margin-left:4px">${esc(p.tag)}</span></div>
            ${statusChip(p.status)}
          </div>
        </div>
      </div>
      <div style="color:var(--text-dim);font-size:13px;line-height:1.65;margin-top:6px">${esc(p.desc)}</div>
      ${procTable(p)}
      <div class="program-actions">
        ${acts.map(a => {
          const m = ACTION_META[a];
          return `<button class="btn ${m.cls}" data-action="${a}" ${a === "update" && p.latest === p.version ? "disabled" : ""}>${Icons[m.icon] || ""}<span>${m.label}</span></button>`;
        }).join("")}
        <button class="btn ghost" data-action="log" aria-expanded="false">${Icons.doc}<span>日志</span></button>
      </div>
      <div class="prog-log" hidden>
        <pre class="mono code-block">${(p.logs || []).map(esc).join("\n")}</pre>
      </div>
    </div>`;
  }

  function render(main) {
    main.innerHTML = `
      <div class="page-head">
        <div>
          <div class="page-title">${Icons.server} 第三方程序</div>
          <div class="page-desc">控制台托管的独立组件 —— Docker 自托管，统一启停/更新/进程监控，日志尾部实时可查</div>
        </div>
        <button class="btn ghost" id="pgmRefresh">${Icons.refresh}<span>刷新</span></button>
      </div>
      <div id="pgmBody" class="grid cols-2" style="margin-top:16px">
        <div class="empty" style="grid-column:1/-1">${Icons.clock} 正在读取进程状态…</div>
      </div>`;

    const body = $("#pgmBody", main);

    async function load() {
      body.innerHTML = `<div class="empty" style="grid-column:1/-1">${Icons.clock} 正在读取进程状态…</div>`;
      try {
        const programs = await API.getPrograms();
        const run = programs.filter(p => p.status === "running").length;
        body.innerHTML = `
          <div style="grid-column:1/-1;display:flex;gap:10px;flex-wrap:wrap;align-items:center">
            <span class="chip green">${run} 运行中</span>
            <span class="chip amber">${programs.filter(p => p.status === "degraded").length} 告警</span>
            <span class="chip gray">${programs.filter(p => p.status === "stopped").length} 已停止</span>
            <span class="chip amber" id="pgmUpdates">${programs.filter(p => p.latest !== p.version).length} 个可更新</span>
            <span style="color:var(--text-faint);font-size:12.5px">编排：docker compose（./deploy）· 健康检查 30s</span>
          </div>
          ${programs.map(programCard).join("")}`;
        bindActions(body);
      } catch (err) {
        body.innerHTML = `<div class="banner warn" style="grid-column:1/-1">${Icons.alert}<div class="bn-body">加载失败：${esc(err.message)}</div></div>`;
      }
    }

    function bindActions(scope) {
      scope.querySelectorAll("[data-action]").forEach(btn => {
        btn.addEventListener("click", async () => {
          const action = btn.dataset.action;
          const card = btn.closest("[data-program]");
          const id = card.dataset.program;
          const name = card.querySelector(".card-title").textContent.trim().split(" ")[0];

          if (action === "log") {
            const logBox = card.querySelector(".prog-log");
            const open = !logBox.hidden;
            logBox.hidden = open;
            btn.setAttribute("aria-expanded", String(!open));
            if (!open) logBox.scrollIntoView({ behavior: "smooth", block: "nearest" });
            return;
          }

          const label = ACTION_META[action].label;
          btn.disabled = true;
          const origHTML = btn.innerHTML;
          btn.innerHTML = `${Icons.loader.replace('class="i"', 'class="i spin"')}<span>${label}中…</span>`;
          try {
            const res = await API.programAction(id, action);
            Toast.show(`${name} ${label}完成 → ${STATUS[res.status] ? STATUS[res.status].label : res.status}`, "ok");
            await load();
          } catch (err) {
            Toast.show(`${name} ${label}失败：${err.message}`, "err");
            btn.disabled = false;
            btn.innerHTML = origHTML;
          }
        });
      });
    }

    $("#pgmRefresh", main).addEventListener("click", load);
    load();
  }

  App.registerPage("programs", { title: "第三方程序", icon: Icons.server, render });
})();
