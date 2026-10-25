/* ============================================================
   page-settings.js — 设置：API 对接模式 / 后端地址 / LLM 设置 / 人设设置 / 诊断 / 关于
   ------------------------------------------------------------
   V5.1（契约对齐第 11 轮清单 · 全栈开发 48a5150）：
   · LLM 设置：GET/PUT /api/v1/settings/llm——provider(自由标签)/base_url/
     chat_model/planner_model/temperature/max_tool_rounds/timeout_s；api_key 明文
     永不回显（GET 恒 "***"/""），PUT 三态（非空=新值 / "***"=保留 / 空串=清除，
     UI 提供「清除已保存密钥」显式触发空串）；保存热生效（enabled/applied 回显）。
     模型列表代理：POST /api/v1/settings/llm/models → {ok, models:[id], count}。
   · 人设设置：GET/PUT /api/v1/settings/persona——name/essence/style_rules[]/
     group_rules_digest/reply_fallbacks[]（一行一条编辑）；空名字/空规则 400 同构校验；
     保存热生效（persona:updated → app.js 侧栏 Agent 名即时刷新）。
   交互参考 TTS 设置页模式（字段 → 检查行 → 保存反馈）。
   ============================================================ */
"use strict";

(function () {

  function render(main) {
    main.innerHTML = `
      <div class="page-head">
        <div>
          <div class="page-title">${Icons.settings} 设置</div>
          <div class="page-desc">后端对接总开关 · LLM 设置 · 人设设置（保存即热生效）—— 全栈开发契约清单定稿后，在此切换 Live 模式即可</div>
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

      <!-- ============ V5.1 · LLM 设置（契约：GET/PUT /api/v1/settings/llm） ============ -->
      <div class="card" style="margin-top:14px" id="llmCard">
        <div class="card-head">
          <div class="card-title">${Icons.globe} LLM 设置（对话管道模型）</div>
          <span class="chip gray" id="llmState">读取中…</span>
        </div>
        <div style="color:var(--text-dim);font-size:13px;line-height:1.7;margin-top:2px">
          Agent 对话所用的 LLM 接入配置。契约：<b style="color:var(--text)">api_key 明文永不回显</b>（GET 恒为 <span class="mono">***</span>；PUT 三态——非空=新值 / <span class="mono">***</span>=保留 / 空串=清除）；保存热生效，chat 与 planner 管道构造请求时现取。
        </div>
        <div class="grid cols-2" style="margin-top:12px;gap:14px">
          <div>
            <div class="field">
              <div class="field-label">接口提供方（Provider，自由标签）<span class="req">*</span></div>
              <div class="chip-row" id="llmProviders" role="radiogroup" aria-label="LLM 提供方"></div>
            </div>
            <div class="field">
              <label class="field-label" for="llmBaseUrl">接口地址（Base URL）<span class="req">*</span></label>
              <input class="input mono" id="llmBaseUrl" placeholder="https://api.deepseek.com/v1" spellcheck="false">
              <div class="field-help">OpenAI 兼容端点；对话管道经后端代理调用。</div>
            </div>
            <div class="field">
              <label class="field-label" for="llmKey">API 密钥<span class="req" id="llmKeyReq">*</span></label>
              <input class="input mono" type="password" id="llmKey" autocomplete="off" spellcheck="false" aria-describedby="llmKeyHint">
              <div class="field-help" id="llmKeyHint">留空 = 保留已保存密钥（PUT 发 "***"）。</div>
              <div style="display:flex;gap:8px;margin-top:2px">
                <button class="btn ghost sm" id="llmKeyClear">${Icons.trash}<span>清除已保存密钥</span></button>
              </div>
            </div>
          </div>
          <div>
            <div class="field">
              <div class="field-label">对话模型（chat_model）<span class="req">*</span>
                <button class="btn ghost sm" id="llmFetch" style="margin-left:auto">${Icons.refresh}<span>拉取模型列表</span></button>
              </div>
              <select class="input mono" id="llmModel" aria-describedby="llmModelHint"><option value="">先拉取或选择提供方</option></select>
            </div>
            <div class="field">
              <label class="field-label" for="llmPlanner">规划模型（planner_model）<span class="opt">可选 · 留同 chat</span></label>
              <select class="input mono" id="llmPlanner"><option value="">（同对话模型）</option></select>
              <div class="field-help" id="llmModelHint">POST /api/v1/settings/llm/models 后端代理 → {ok, models:[id], count}；两模型共用该列表。</div>
            </div>
            <div class="field">
              <div class="field-label">高级参数<span class="opt">契约字段 · 可默认</span></div>
              <div class="adv-grid">
                <label>temperature<input type="number" id="llmTemp" min="0" max="2" step="0.1" value="0.7"></label>
                <label>max_tool_rounds<input type="number" id="llmRounds" min="1" max="8" step="1" value="2"></label>
                <label>timeout_s<input type="number" id="llmTimeout" min="5" max="300" step="5" value="30"></label>
              </div>
            </div>
            <div class="field">
              <div class="field-label" style="margin-bottom:2px">校验与保存</div>
              <div class="check-rows" id="llmCheck"></div>
              <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
                <button class="btn ghost" id="llmCheckBtn">${Icons.search}<span>检查配置</span></button>
                <button class="btn primary" id="llmSave">${Icons.save}<span>保存（热生效）</span></button>
              </div>
              <div class="llm-saved mono" id="llmSaved" hidden></div>
            </div>
          </div>
        </div>
      </div>

      <!-- ============ V5.1 · 人设设置（契约：GET/PUT /api/v1/settings/persona） ============ -->
      <div class="card" style="margin-top:14px" id="personaCard">
        <div class="card-head">
          <div class="card-title">${Icons.user} 人设设置（保存即热生效）</div>
          <span class="chip gray" id="personaState">读取中…</span>
        </div>
        <div style="color:var(--text-dim);font-size:13px;line-height:1.7;margin-top:2px">
          Agent 的名字、性格与风格规则——<b style="color:var(--text)">管道构造系统提示词时现取</b>（保存热生效，无需重启；侧栏 Agent 名即时刷新）。字段：name / essence / style_rules[] / group_rules_digest / reply_fallbacks[]。
        </div>
        <div class="grid cols-2" style="margin-top:12px;gap:14px">
          <div>
            <div class="field">
              <label class="field-label" for="psName">Agent 名字（name）<span class="req">*</span></label>
              <input class="input" id="psName" maxlength="16" placeholder="小满">
              <div class="field-help">对话/群聊中的自称，也将刷新侧栏显示。</div>
            </div>
            <div class="field">
              <label class="field-label" for="psEssence">性格（essence）</label>
              <input class="input" id="psEssence" maxlength="120" placeholder="温和、好奇、偶尔吐槽但懂分寸">
            </div>
            <div class="field">
              <label class="field-label" for="psRules">风格规则（style_rules）<span class="req">*</span><span class="opt">一行一条</span></label>
              <textarea class="input" id="psRules" rows="6" spellcheck="false" style="resize:vertical" placeholder="语气自然克制，不用客套话"></textarea>
              <div class="field-help">空规则后端返回 400（契约同构校验）。</div>
            </div>
          </div>
          <div>
            <div class="field">
              <label class="field-label" for="psGroup">群规摘要（group_rules_digest）</label>
              <textarea class="input" id="psGroup" rows="2" maxlength="300" style="resize:vertical" placeholder="群聊默认潜水，被 @ 或话题相关时简短回应"></textarea>
            </div>
            <div class="field">
              <label class="field-label" for="psFallbacks">兜底回复（reply_fallbacks）<span class="opt">一行一条 · 最多 8 条</span></label>
              <textarea class="input" id="psFallbacks" rows="3" style="resize:vertical" placeholder="（这条我先记下了，想清楚再回你）"></textarea>
            </div>
            <div class="check-rows" id="psCheck"></div>
            <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
              <button class="btn ghost" id="psReset">${Icons.refresh}<span>填入默认人设</span></button>
              <button class="btn primary" id="psSave">${Icons.save}<span>保存并热生效</span></button>
            </div>
          </div>
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
          QQ Agent 中央控制台 · 前端 V5 —— 世界心跳监控 / 对话流 / 插件技能 / 第三方程序（一键管理 + 状态真实化 + 全量日志 + 进度条）/ TTS API 管理 / LLM 设置 / 人设设置（热生效）/ 诊断导出（脱敏）。<br>
          纯静态零依赖（原生 JS），双击 index.html 即可运行；对接后端后在设置页切 Live。<br>
          依据《QQ Agent 实现方案 v1.0（2026-10-25）》与 37 需求实现：V3 诊断脱敏 · V4 一键管理回归与 Obsidian 可观测 · V5 状态真实化 / 令牌展示 / 进度条 / LLM 与人设设置。
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

    /* ============ V5.1 · LLM 设置（契约：/api/v1/settings/llm） ============ */
    const llmState = { provider: "", baseUrl: "", models: [], hasSavedKey: false, keySource: "none", enabled: false, applied: null };
    const llmStateChip = $("#llmState", main);
    const llmProvidersEl = $("#llmProviders", main);
    const llmBaseUrlEl = $("#llmBaseUrl", main);
    const llmKeyEl = $("#llmKey", main);
    const llmKeyHint = $("#llmKeyHint", main);
    const llmKeyReq = $("#llmKeyReq", main);
    const llmModelEl = $("#llmModel", main);
    const llmPlannerEl = $("#llmPlanner", main);
    const llmTempEl = $("#llmTemp", main);
    const llmRoundsEl = $("#llmRounds", main);
    const llmTimeoutEl = $("#llmTimeout", main);
    const llmCheck = $("#llmCheck", main);
    const llmSaved = $("#llmSaved", main);
    const PROVIDERS = (typeof MOCK !== "undefined" && MOCK.llmProviders) ? MOCK.llmProviders : [];
    let llmKeyClearPending = false;   // 点过「清除已保存密钥」→ 本次保存显式发空串（契约三态）

    function llmFieldInvalid(el, bad) { const f = el.closest(".field"); if (f) f.classList.toggle("invalid", !!bad); }
    function llmCheckRow(pass, label, hint) {
      return `<div class="check-row ${pass ? "pass" : "fail"}"><span class="check-ico">${pass ? Icons.check : Icons.alert}</span><span class="check-label">${esc(label)}</span><span class="check-hint">${esc(hint || "")}</span></div>`;
    }
    function llmSavedSummary(view) {
      const v = view || llmState.applied;
      llmSaved.hidden = !v;
      llmSaved.innerHTML = v ? `已保存 · provider <b>${esc(llmState.provider || "—")}</b> · chat <b>${esc(llmModelEl.value || "—")}</b> · 密钥 <b class="mono">${llmState.hasSavedKey ? "***（" + esc(llmState.keySource) + "）" : "未保存"}</b> · 管道 <b>${llmState.enabled ? "enabled" : "未接通"}</b> · ${esc(v.chat || "")}` : "";
    }
    function llmRenderProviders() {
      llmProvidersEl.innerHTML = PROVIDERS.map(pv => `
        <button class="chip ${pv.id === llmState.provider ? "blue" : "gray"}" data-pv="${esc(pv.id)}" role="radio" aria-checked="${pv.id === llmState.provider}" style="cursor:pointer">${esc(pv.name)}</button>`).join("");
      llmProvidersEl.querySelectorAll("[data-pv]").forEach(chip => {
        chip.addEventListener("click", () => {
          llmState.provider = chip.dataset.pv;
          const pv = PROVIDERS.find(x => x.id === llmState.provider);
          if (pv) {
            if (pv.base_url) llmBaseUrlEl.value = pv.base_url;
            llmFillModels(pv.models || [], "");
          }
          llmRenderProviders();
        });
      });
    }
    function llmFillModels(models, chatSel, plannerSel) {
      llmState.models = models || [];
      const opts = llmState.models.map(m => `<option value="${esc(m)}" ${m === chatSel ? "selected" : ""}>${esc(m)}</option>`).join("");
      llmModelEl.innerHTML = llmState.models.length ? opts : `<option value="">（该提供方需拉取模型列表）</option>`;
      llmPlannerEl.innerHTML = `<option value="">（同对话模型）</option>` + llmState.models.map(m => `<option value="${esc(m)}" ${m === plannerSel ? "selected" : ""}>${esc(m)}</option>`).join("");
    }

    API.llmGetConfig().then(res => {
      const c = res || {};   // 契约：扁平返回（非 config 包裹）
      llmState.provider = c.provider || "deepseek";
      llmState.baseUrl = c.base_url || "";
      llmState.hasSavedKey = !!c.has_api_key;
      llmState.keySource = c.api_key_source || "none";
      llmState.enabled = !!c.enabled;
      llmState.applied = c.applied || null;
      llmBaseUrlEl.value = llmState.baseUrl;
      llmKeyEl.value = "";
      llmKeyEl.placeholder = llmState.hasSavedKey ? "已保存 ***（输入新密钥覆盖）" : "sk-…";
      llmKeyHint.innerHTML = llmState.hasSavedKey
        ? `已保存密钥：<span class="mono" style="color:var(--blue)">***</span>（api_key 恒不回显 · 来源 ${esc(llmState.keySource)}）· 留空 = 保留`
        : "尚未保存密钥（PUT 非空=新值 / *** =保留 / 空串=清除）。";
      llmKeyReq.style.display = llmState.hasSavedKey ? "none" : "";
      if (c.temperature != null) llmTempEl.value = c.temperature;
      if (c.max_tool_rounds != null) llmRoundsEl.value = c.max_tool_rounds;
      if (c.timeout_s != null) llmTimeoutEl.value = c.timeout_s;
      llmRenderProviders();
      const pv = PROVIDERS.find(x => x.id === llmState.provider);
      llmFillModels(pv ? pv.models : [], c.chat_model || "", c.planner_model || "");
      llmStateChip.textContent = c.enabled ? "已保存 · 管道已接通" : (llmState.hasSavedKey ? "已保存（部分字段待补）" : "未保存");
      llmStateChip.className = "chip " + (c.enabled ? "green" : llmState.hasSavedKey ? "amber" : "gray");
      llmSavedSummary();
    }).catch(() => {
      llmStateChip.textContent = "读取失败";
      llmStateChip.className = "chip red";
      llmRenderProviders();
    });

    /* 清除密钥：本次保存显式发空串（契约三态之「清除」） */
    $("#llmKeyClear", main).addEventListener("click", () => {
      if (!llmState.hasSavedKey && !llmKeyEl.value.trim()) { Toast.show("当前无已保存密钥", "warn"); return; }
      llmKeyClearPending = true;
      llmKeyEl.value = "";
      llmKeyEl.placeholder = "将清除已保存密钥（保存后生效）";
      llmKeyHint.innerHTML = `<span style="color:var(--red)">已标记清除：保存时 PUT api_key=""（契约空串=清除）</span>`;
      Toast.show("已标记清除密钥", "warn");
    });

    /* 拉取模型列表：POST /api/v1/settings/llm/models → {ok, models:[id], count} */
    $("#llmFetch", main).addEventListener("click", async () => {
      const btn = $("#llmFetch", main);
      const orig = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = `${Icons.loader.replace('class="i"', 'class="i spin"')}<span>拉取中…</span>`;
      try {
        const res = await API.llmFetchModels(llmState.provider);
        const models = ((res && res.models) || []).map(m => typeof m === "string" ? m : m.id).filter(Boolean);
        if (!models.length) throw new Error("返回列表为空");
        llmFillModels(models, llmModelEl.value && models.includes(llmModelEl.value) ? llmModelEl.value : models[0], llmPlannerEl.value);
        Toast.ok("模型列表已拉取", `${(res && res.count) || models.length} 个模型（POST /api/v1/settings/llm/models 后端代理）`);
      } catch (err) {
        Toast.err("拉取模型列表失败", err.message);
      } finally {
        btn.disabled = false;
        btn.innerHTML = orig;
      }
    });

    function llmCollectCheck() {
      const items = [];
      const urlOk = /^https?:\/\/.+\..+/.test(llmBaseUrlEl.value.trim());
      const keyOk = !!llmKeyEl.value.trim() || (llmState.hasSavedKey && !llmKeyClearPending);
      items.push({ pass: !!llmState.provider, label: "已选择提供方", hint: llmState.provider || "点选 Provider（自由标签）" });
      items.push({ pass: urlOk, label: "接口地址格式", hint: urlOk ? llmBaseUrlEl.value.trim() : "需 http(s):// 开头的完整地址" });
      items.push({ pass: keyOk, label: "API 密钥", hint: llmKeyClearPending ? "已标记清除（保存后置空）" : (llmKeyEl.value.trim() ? "已输入（保存后明文不回显）" : (llmState.hasSavedKey ? `保留已保存 ***（${llmState.keySource}）` : "未填写")) });
      items.push({ pass: !!llmModelEl.value, label: "已选择对话模型", hint: llmModelEl.value || "拉取列表或选预设" });
      llmFieldInvalid(llmBaseUrlEl, !urlOk);
      llmFieldInvalid(llmKeyEl, !keyOk);
      return items;
    }

    $("#llmCheckBtn", main).addEventListener("click", async () => {
      const items = llmCollectCheck();
      let conn = { pass: false, label: "模型列表连通（后端代理）", hint: "" };
      try {
        const res = await API.llmFetchModels(llmState.provider);
        const n = (res && res.count) || ((res && res.models) || []).length;
        conn.pass = n > 0;
        conn.hint = `代理返回 ${n} 个模型`;
      } catch (err) { conn.hint = err.message; }
      items.push(conn);
      llmCheck.innerHTML = items.map(it => llmCheckRow(it.pass, it.label, it.hint)).join("");
      const fail = items.filter(i => !i.pass).length;
      Toast.show(fail ? `检查完成：${fail} 项未通过` : "检查完成：全部通过 ✓", fail ? "warn" : "ok");
    });

    $("#llmSave", main).addEventListener("click", async () => {
      const items = llmCollectCheck();
      const blocking = items.filter(i => !i.pass);
      llmCheck.innerHTML = items.map(it => llmCheckRow(it.pass, it.label, it.hint)).join("");
      if (blocking.length) {
        Toast.err("保存前请先通过校验", `${blocking.length} 项未通过（见上方检查清单）`);
        return;
      }
      const btn = $("#llmSave", main);
      const orig = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = `${Icons.loader.replace('class="i"', 'class="i spin"')}<span>保存中…</span>`;
      try {
        /* api_key 三态（契约）：非空=新值 / "***"=保留 / ""=清除（标记清除时显式发空串） */
        const apiKey = llmKeyClearPending ? "" : (llmKeyEl.value.trim() || "***");
        const res = await API.llmSaveConfig({
          provider: llmState.provider,
          base_url: llmBaseUrlEl.value.trim().replace(/\/$/, ""),
          api_key: apiKey,
          chat_model: llmModelEl.value,
          planner_model: llmPlannerEl.value || llmModelEl.value,
          temperature: +llmTempEl.value,
          max_tool_rounds: +llmRoundsEl.value,
          timeout_s: +llmTimeoutEl.value,
        });
        const c = res || {};
        llmState.hasSavedKey = !!c.has_api_key;
        llmState.keySource = c.api_key_source || (llmState.hasSavedKey ? "state" : "none");
        llmState.enabled = !!c.enabled;
        llmState.applied = c.applied || llmState.applied;
        llmKeyClearPending = false;
        llmKeyEl.value = "";
        llmKeyEl.placeholder = llmState.hasSavedKey ? "已保存 ***（输入新密钥覆盖）" : "sk-…";
        llmKeyHint.innerHTML = llmState.hasSavedKey
          ? `已保存密钥：<span class="mono" style="color:var(--blue)">***</span>（api_key 恒不回显 · 来源 ${esc(llmState.keySource)}）· 留空 = 保留`
          : "尚未保存密钥（PUT 非空=新值 / *** =保留 / 空串=清除）。";
        llmKeyReq.style.display = llmState.hasSavedKey ? "none" : "";
        llmStateChip.textContent = llmState.enabled ? "已保存 · 管道已接通" : (llmState.hasSavedKey ? "已保存（部分字段待补）" : "未保存");
        llmStateChip.className = "chip " + (llmState.enabled ? "green" : llmState.hasSavedKey ? "amber" : "gray");
        llmSavedSummary();
        Toast.ok("LLM 配置已保存（热生效）", `api_key ${llmState.hasSavedKey ? "***" : "已清除"} · chat=${llmModelEl.value} · 管道已重建重接线`);
      } catch (err) {
        Toast.err("保存失败", err.message);
      } finally {
        btn.disabled = false;
        btn.innerHTML = orig;
      }
    });

    /* ============ V5.1 · 人设设置（契约：/api/v1/settings/persona） ============ */
    const personaStateChip = $("#personaState", main);
    const psNameEl = $("#psName", main);
    const psEssenceEl = $("#psEssence", main);
    const psRulesEl = $("#psRules", main);
    const psGroupEl = $("#psGroup", main);
    const psFallbacksEl = $("#psFallbacks", main);
    const psCheckEl = $("#psCheck", main);

    function psFill(p) {
      psNameEl.value = p.name || "";
      psEssenceEl.value = p.essence || "";
      psRulesEl.value = (p.style_rules || []).join("\n");
      psGroupEl.value = p.group_rules_digest || "";
      psFallbacksEl.value = (p.reply_fallbacks || []).join("\n");
    }
    API.personaGet().then(res => {
      psFill(res || {});   // 契约：扁平返回（非 persona 包裹）
      const dirty = U.lsGet("console.persona.v1", null);
      personaStateChip.textContent = dirty ? "已保存 · 热生效中" : "默认人设 · 未改";
      personaStateChip.className = "chip " + (dirty ? "green" : "gray");
    }).catch(() => {
      personaStateChip.textContent = "读取失败";
      personaStateChip.className = "chip red";
    });

    $("#psReset", main).addEventListener("click", () => {
      if (typeof MOCK !== "undefined" && MOCK.persona) psFill(MOCK.persona);
      Toast.show("已填入默认人设（未保存，确认后点「保存并热生效」）", "warn");
    });

    $("#psSave", main).addEventListener("click", async () => {
      const name = psNameEl.value.trim();
      const rules = psRulesEl.value.split("\n").map(s => s.trim()).filter(Boolean);
      const fallbacks = psFallbacksEl.value.split("\n").map(s => s.trim()).filter(Boolean).slice(0, 8);
      const rows = [];
      rows.push({ pass: !!name, label: "名字非空", hint: name || "未填写" });
      rows.push({ pass: rules.length >= 1, label: "风格规则 ≥ 1 条", hint: `${rules.length} 条（空规则后端 400）` });
      psCheckEl.innerHTML = rows.map(it => llmCheckRow(it.pass, it.label, it.hint)).join("");
      const fail = rows.filter(r => !r.pass).length;
      if (fail) { Toast.err("人设有必填项未通过校验", `${fail} 项未通过（见上方清单）`); return; }
      const btn = $("#psSave", main);
      const orig = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = `${Icons.loader.replace('class="i"', 'class="i spin"')}<span>保存中…</span>`;
      try {
        await API.personaSave({
          name,
          essence: psEssenceEl.value.trim(),
          style_rules: rules,
          group_rules_digest: psGroupEl.value.trim(),
          reply_fallbacks: fallbacks,
        });
        personaStateChip.textContent = "已保存 · 热生效中";
        personaStateChip.className = "chip green";
        Toast.ok(`人设已保存并热生效（${name}）`, "管道构造系统提示词时现取 · 侧栏 Agent 名已刷新");
      } catch (err) {
        Toast.err("人设保存失败", err.message);
      } finally {
        btn.disabled = false;
        btn.innerHTML = orig;
      }
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
