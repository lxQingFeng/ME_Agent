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
        <div class="card-title">${Icons.info} 关于</div>
        <div style="color:var(--text-dim);font-size:13px;line-height:1.8;margin-top:6px">
          QQ Agent 中央控制台 · 前端 V1 —— 世界心跳监控 / 对话流 / 插件技能 / 第三方程序 / TTS API 管理。<br>
          纯静态零依赖（原生 JS），双击 index.html 即可运行；对接后端后在设置页切 Live。<br>
          依据《QQ Agent 实现方案 v1.0（2026-10-25）》与 37 的 TTS 需求原文（10:11）实现。
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
  }

  App.registerPage("settings", { title: "设置", icon: Icons.settings, render });
})();
