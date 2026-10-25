/* ============================================================
   page-settings.js — 设置：API 对接模式 / 后端地址 / 关于
   ============================================================ */
"use strict";

(function () {

  function render(main) {
    main.innerHTML = `
      <div class="page-head">
        <div>
          <div class="page-title">${Icons.settings} 设置</div>
          <div class="page-desc">后端对接总开关 —— 全栈开发契约清单定稿后，在此切换 Live 模式即可</div>
        </div>
      </div>

      <div class="grid cols-2" style="margin-top:16px">
        <div class="card">
          <div class="card-head">
            <div class="card-title">${Icons.globe} API 对接模式</div>
            <div style="display:flex;gap:8px" id="modeBtns" role="radiogroup" aria-label="API 模式">
              <button class="chip ${API.mode === "mock" ? "amber" : "gray"}" data-mode="mock" role="radio" aria-checked="${API.mode === "mock"}" style="cursor:pointer">Mock 演示</button>
              <button class="chip ${API.mode === "live" ? "green" : "gray"}" data-mode="live" role="radio" aria-checked="${API.mode === "live"}" style="cursor:pointer">Live 对接</button>
            </div>
          </div>
          <div class="field">
            <label class="field-label" for="baseUrl">后端 Console 地址</label>
            <input class="input mono" id="baseUrl" value="${esc(API.baseUrl)}" spellcheck="false" aria-describedby="baseUrlHint">
            <div class="field-help" id="baseUrlHint">FastAPI（全栈开发）。默认 <span class="mono">http://127.0.0.1:8000</span>，WS 推送 <span class="mono">ws://127.0.0.1:8000/ws</span>。</div>
          </div>
          <div class="field">
            <label class="field-label" for="wsUrl">实时推送 WS 地址（世界心跳/消息流）</label>
            <input class="input mono" id="wsUrl" value="${esc(API.wsUrl)}" spellcheck="false">
          </div>
          <div class="field-help">当前模式：<b id="modeNow">${API.mode === "mock" ? "Mock（内置演示数据，心跳加速）" : "Live（请求后端）"}</b></div>
        </div>

        <div class="card">
          <div class="card-head">
            <div class="card-title">${Icons.doc} 路由表（对接清单核对用）</div>
            <span class="chip gray">api.js · LIVE_ROUTES</span>
          </div>
          <div class="mono" style="font-size:12px;color:var(--text-dim);line-height:2.1">
            ${Object.entries(API.LIVE_ROUTES).map(([k, v]) => `<div style="display:flex;gap:10px"><span style="color:var(--blue);min-width:130px">${esc(k)}</span><span>${esc(v)}</span></div>`).join("")}
          </div>
          <div class="field-help" style="margin-top:10px">契约以全栈开发发群的清单为准；不一致时只改 <span class="mono">js/api.js</span> 一处。</div>
        </div>
      </div>

      <div class="card" style="margin-top:14px">
        <div class="card-head">
          <div class="card-title">${Icons.shield} 诊断导出（脱敏）</div>
          <span class="chip ${API.mode === "mock" ? "purple" : "green"}">${API.mode === "mock" ? "Mock · 同构演示数据" : "Live · GET /api/v1/diagnostics/export"}</span>
        </div>
        <div style="color:var(--text-dim);font-size:13px;line-height:1.7;margin-top:2px">
          排障 / 反馈工具——导出脱敏诊断包 .json，可直接发给维护者分析。<b style="color:var(--text)">密钥一律为 hasXxx:true 布尔指纹形态，绝不包含原文。</b>
        </div>
        <div class="diag-tags">
          <span class="chip gray"><span class="chip-dot"></span>生成时间</span>
          <span class="chip gray"><span class="chip-dot"></span>运行时长</span>
          <span class="chip gray"><span class="chip-dot"></span>组件健康</span>
          <span class="chip gray"><span class="chip-dot"></span>近期错误摘要</span>
          <span class="chip green"><span class="chip-dot"></span>密钥指纹 hasXxx:true</span>
        </div>
        <div class="diag-row">
          <button class="btn primary" id="diagExport">${Icons.download}<span>导出诊断包 .json</span></button>
          <span class="diag-meta mono" id="diagMeta">尚未导出</span>
        </div>
        <div class="diag-preview" id="diagPreviewBox" hidden>
          <div class="diag-preview-head">
            <span class="mono" id="diagPreviewTitle">脱敏形态预览</span>
            <span class="diag-fp-legend"><span class="j-fp">"has_xxx"</span> 密钥指纹（布尔，无原文）</span>
          </div>
          <pre class="mono" id="diagJson" tabindex="0" aria-label="诊断包 JSON 预览"></pre>
          <div class="diag-scan" id="diagScan"></div>
        </div>
      </div>

      <div class="card" style="margin-top:14px">
        <div class="card-title">${Icons.info} 关于</div>
        <div style="color:var(--text-dim);font-size:13px;line-height:1.8;margin-top:6px">
          QQ Agent 中央控制台 · 前端 V3 —— 世界心跳监控 / 对话流 / 插件技能 / 第三方程序（外部自管为主）/ TTS API 管理 / 诊断导出（脱敏）。<br>
          纯静态零依赖（原生 JS），双击 index.html 即可运行；对接后端后在设置页切 Live。<br>
          依据《QQ Agent 实现方案 v1.0（2026-10-25）》与 37 的 TTS 需求原文（10:11）实现；V3 增补诊断输出脱敏（契约：GET /api/v1/diagnostics/export）。
        </div>
      </div>
    `;

    const base = $("#baseUrl", main);
    const ws = $("#wsUrl", main);
    base.addEventListener("change", () => { API.baseUrl = base.value.trim().replace(/\/$/, ""); localStorage.setItem("console.api.baseUrl", API.baseUrl); Toast.show("后端地址已更新", "ok"); });
    ws.addEventListener("change", () => { API.wsUrl = ws.value.trim(); localStorage.setItem("console.api.wsUrl", API.wsUrl); Toast.show("WS 地址已更新", "ok"); });

    main.querySelectorAll("#modeBtns [data-mode]").forEach(btn => {
      btn.addEventListener("click", () => {
        API.setMode(btn.dataset.mode);
        main.querySelectorAll("#modeBtns [data-mode]").forEach(b => {
          b.classList.toggle("gray", b.dataset.mode !== API.mode);
          b.classList.toggle(API.mode === "live" ? "green" : "amber", b.dataset.mode === API.mode);
          b.setAttribute("aria-checked", String(b.dataset.mode === API.mode));
        });
        $("#modeNow", main).textContent = API.mode === "mock" ? "Mock（内置演示数据，心跳加速）" : "Live（请求后端）";
        Toast.show(`已切换 ${API.mode === "live" ? "Live 模式：将请求 " + API.baseUrl : "Mock 模式"}，页面即将刷新`, API.mode === "live" ? "ok" : "warn");
        setTimeout(() => location.reload(), 900);
      });
    });

    /* ---------- 诊断导出（脱敏） ---------- */
    const diagBtn = $("#diagExport", main);
    const diagMeta = $("#diagMeta", main);
    const diagBox = $("#diagPreviewBox", main);
    const diagJson = $("#diagJson", main);
    const diagScan = $("#diagScan", main);

    /* JSON 语法高亮：键/值/布尔/数字/null 分色；hasXxx 指纹键绿色高亮（脱敏形态一目了然） */
    function prettyJSON(obj) {
      return JSON.stringify(obj, null, 2).replace(
        /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false)\b|\bnull\b|(-?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)/g,
        (m, str, colon) => {
          if (str !== undefined) {
            const isKey = colon !== undefined;
            const isFp = isKey && /^"has[A-Za-z0-9_]*"$/.test(str);
            const cls = isFp ? "j-fp" : (isKey ? "j-key" : "j-str");
            return `<span class="${cls}">${esc(str)}</span>${colon || ""}`;
          }
          if (m === "true" || m === "false") return `<span class="j-bool">${m}</span>`;
          if (m === "null") return `<span class="j-null">null</span>`;
          return `<span class="j-num">${esc(m)}</span>`;
        }
      );
    }

    /* 客户端密钥原文自检（兜底防线：即便后端脱敏失效也不外发） */
    const SECRET_PATTERNS = [
      { re: /\bsk-[A-Za-z0-9_-]{16,}/,                 label: "sk- 密钥" },
      { re: /\bBearer\s+[A-Za-z0-9._-]{16,}/i,          label: "Bearer 令牌" },
      { re: /\bAKIA[0-9A-Z]{16}\b/,                     label: "AWS AccessKey" },
      { re: /\bgh[pousr]_[A-Za-z0-9]{20,}/,             label: "GitHub token" },
      { re: /(?:api[_-]?key|secret|token|password|passwd)[^,\n]{0,4}[:=]\s*"[^"\n]{8,}"/i, label: "密文键值对" },
    ];
    function scanSecrets(text) {
      return SECRET_PATTERNS.filter(p => p.re.test(text)).map(p => p.label);
    }

    function diagStamp(d) {
      let t = d && d.generated_at ? new Date(d.generated_at) : new Date();
      if (isNaN(t.getTime())) t = new Date();
      const p = n => String(n).padStart(2, "0");
      return `${t.getFullYear()}${p(t.getMonth() + 1)}${p(t.getDate())}-${p(t.getHours())}${p(t.getMinutes())}`;
    }

    function downloadDiag(data) {
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `me-agent-diagnostics-${diagStamp(data)}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    }

    function renderDiag(data, srcNote) {
      diagBox.hidden = false;
      $("#diagPreviewTitle", main).textContent = `脱敏形态预览 · ${srcNote}`;
      diagJson.innerHTML = prettyJSON(data);
      const raw = JSON.stringify(data, null, 2);
      const kb = (new Blob([raw]).size / 1024).toFixed(1);
      const gen = data && data.generated_at ? data.generated_at : "";
      diagMeta.textContent = `${gen ? `生成于 ${gen}` : "已生成"} · ${kb} KB · ${srcNote}`;
      const hits = scanSecrets(raw);
      diagScan.innerHTML = hits.length
        ? `<span class="scan-bad">${Icons.alert} 检出疑似密钥原文（${hits.join("、")}）——请勿分发此文件</span>`
        : `<span class="scan-ok">${Icons.check} 密钥自检通过：未检出原文形态，密钥仅以 hasXxx 布尔指纹出现</span>`;
    }

    diagBtn.addEventListener("click", async () => {
      const origHTML = diagBtn.innerHTML;
      diagBtn.disabled = true;
      diagBtn.innerHTML = `${Icons.loader.replace('class="i"', 'class="i spin"')}<span>生成中…</span>`;
      try {
        const data = await API.exportDiagnostics();
        downloadDiag(data);
        renderDiag(data, API.mode === "mock" ? "Mock 演示数据" : "Live 接口返回");
        Toast.show("诊断包已导出（已脱敏）", "ok");
      } catch (err) {
        Toast.show(`导出失败：${err.message}`, "err");
      } finally {
        diagBtn.disabled = false;
        diagBtn.innerHTML = origHTML;
      }
    });

    /* Mock 模式自动载入演示数据——直观展示脱敏形态 */
    if (API.mode === "mock") {
      API.exportDiagnostics().then(d => renderDiag(d, "Mock 演示数据")).catch(() => {});
    }
  }

  App.registerPage("settings", { title: "设置", icon: Icons.settings, render });
})();
