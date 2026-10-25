/* ============================================================
   page-tts.js — TTS API 管理模块
   ------------------------------------------------------------
   需求来源：37（群内 2026-10-25 10:11 原文，7 条）
   ① 接口地址配置（默认小米 /v1/chat/completions，支持其他 OpenAI 兼容）+ 密钥
   ② 依据 API 地址获取模型信息并选择模型
   ③ 模型类型差异化：
      文字提示语音 → 请求头/请求体编辑器（两段原文独立编辑、独立保存；
                    仅占位符替换后原样发送；严禁自动补头/重序列化/pretty-print/
                    加前后缀；落盘与编辑往返字节一致）
      预设音色     → 官方音色列表（中文名）+ 试听
      语音克隆     → 克隆音频上传模块
   ④ 保存与检查模块
   ⑤ 试合成试听模块
   ⑥ 桥接：Agent AI 调用 TTS → 发语音消息（对接层预留）
   ============================================================ */
"use strict";

(function () {

  const LS_KEY = "tts.config.v1";
  const LS_HEADER = "tts.template.header.raw";   // 请求头原文（字节保真落盘）
  const LS_BODY = "tts.template.body.raw";        // 请求体原文（字节保真落盘）
  const LS_CLONES = "tts.clone.audios.meta";      // 克隆音频元数据（blob 不落盘，生产走后端）

  const TYPE_META = {
    prompt: { label: "文字提示语音", tone: "blue", desc: "对话补全式驱动：请求体携带文本提示。需自定义请求头/请求体模板。" },
    preset: { label: "预设音色", tone: "green", desc: "官方音色列表（中文音色名），选择音色合成，支持逐个试听。" },
    clone: { label: "语音克隆", tone: "purple", desc: "上传克隆音频（wav/mp3/m4a），以克隆音色合成。" },
    none: { label: "非语音模型", tone: "gray", desc: "普通对话模型，不用于语音合成。" },
  };

  const PLACEHOLDERS = [
    { name: "api_key", desc: "接口密钥（来自①的填写）" },
    { name: "model", desc: "模型 id（来自②的选择）" },
    { name: "text", desc: "待合成的文本" },
    { name: "voice", desc: "音色（预设音色名/克隆音色 id）" },
    { name: "style", desc: "风格指令（user 消息内容，如「温柔地，慢速」）" },
    { name: "endpoint", desc: "接口地址（来自①的填写）" },
  ];

  /* ---------- 状态 ---------- */
  let cfg = loadCfg();
  let models = [];            // 已获取的模型列表
  let voices = [];            // 预设音色列表
  let clones = loadClones();  // 克隆音频 [{id,name,size,dur,blob?,objectUrl}]
  let dirtyHeader = false, dirtyBody = false;

  function loadCfg() {
    const def = {
      endpoint: MOCK.ttsDefaults.endpoint,
      apiKey: "",
      model: "",
      modelType: "prompt",
      voice: "",
      cloneAudioId: "",
    };
    try {
      const raw = localStorage.getItem(LS_KEY);
      return raw ? Object.assign(def, JSON.parse(raw)) : def;
    } catch { return def; }
  }
  function saveCfgLocal() { localStorage.setItem(LS_KEY, JSON.stringify(cfg)); }
  function loadClones() {
    try { return JSON.parse(localStorage.getItem(LS_CLONES) || "[]"); } catch { return []; }
  }
  function saveClonesLocal() { localStorage.setItem(LS_CLONES, JSON.stringify(clones.map(c => ({ ...c, blob: undefined, objectUrl: undefined })))); }

  /* ---------- 字节保真核心 ----------
     编辑器原文仅做占位符替换（U.applyPlaceholders），此外一字不动。
     保存/读取均为原字符串存取（localStorage / 后端 ttsConfigSave），
     不 parse、不 restringify、不 pretty、不加前后缀 → 往返字节一致。 */

  function byteLen(s) { return new TextEncoder().encode(s).length; }
  function eolOf(s) {
    if (s.includes("\r\n")) return "CRLF (\\r\\n)";
    if (s.includes("\r")) return "CR (\\r)";
    return s.includes("\n") ? "LF (\\n)" : "单行";
  }
  function detectType(id) {
    if (/voicedesign|prompt/i.test(id)) return "prompt";
    if (/gpt[\w-]*tts|^tts-\d/i.test(id)) return "prompt";   // OpenAI speech 风格（instructions）
    if (/clone|fish/i.test(id)) return "clone";
    if (/voice|speech|tts|audio/i.test(id)) return "preset";
    return "none";
  }

  /* ---------- 渲染 ---------- */
  function render(main) {
    main.innerHTML = `
      <div class="page-head">
        <div>
          <div class="page-title">${Icons.mic} TTS 语音 · API 管理</div>
          <div class="page-desc">按 37 需求原文（10:11）实现：接口配置 → 模型选择 → 差异化选项 → 保存检查 → 试合成试听，另含 Agent 语音桥接说明</div>
        </div>
        <span class="chip ${API.mode === "live" ? "green" : "gray"}">${API.mode === "live" ? "Live 对接" : "Mock 演示"}</span>
      </div>

      <div class="steps" role="list" aria-label="配置步骤">
        <div class="step active"><b>①</b><span>接口与密钥</span></div>
        <div class="step"><b>②</b><span>获取与选择模型</span></div>
        <div class="step"><b>③</b><span>差异化选项</span></div>
        <div class="step"><b>④</b><span>保存与检查</span></div>
        <div class="step"><b>⑤</b><span>试合成试听</span></div>
      </div>

      <section class="card" style="margin-top:16px;border-color:var(--blue-soft)">
        <div class="card-head">
          <div class="card-title">${Icons.info} 接口协议速查 <span class="chip blue" style="font-size:11px">信息哨兵 · 10-25 调研</span></div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px">
          <div style="padding:10px 12px;border:1px solid var(--border);border-radius:var(--r-md);background:var(--bg-elev)">
            <div style="font-size:12px;color:var(--text-faint);margin-bottom:6px">小米 MiMo（默认）· chat/completions 形态</div>
            <div class="mono" style="font-size:12px;color:var(--text-dim);line-height:1.8">
              POST /v1/chat/completions<br>
              音频 = <b style="color:var(--accent)">choices[0].message.audio.data</b>（base64）<br>
              合成文本 → <b>role=assistant</b>；user 放风格指令<br>
              克隆：voice 内联 <span style="color:var(--amber)">data:audio/mpeg;base64,…</span>（≤10MB）<br>
              认证：Bearer / api-key 头 · 当前限免
            </div>
          </div>
          <div style="padding:10px 12px;border:1px solid var(--border);border-radius:var(--r-md);background:var(--bg-elev)">
            <div style="font-size:12px;color:var(--text-faint);margin-bottom:6px">OpenAI 兼容 · /v1/audio/speech 形态</div>
            <div class="mono" style="font-size:12px;color:var(--text-dim);line-height:1.8">
              POST /v1/audio/speech<br>
              返回<b>音频二进制流</b>（与 chat 结构互不兼容）<br>
              字段：model / input(≤4096) / voice / instructions<br>
              speed 0.25–4.0 · 6 种 response_format<br>
              ⚠ 两套协议需分别适配，模板编辑器即为此设计
            </div>
          </div>
        </div>
      </section>

      <div id="ttsBody" style="display:flex;flex-direction:column;gap:14px;margin-top:14px"></div>
      <div id="ttsBridge"></div>
    `;

    const body = $("#ttsBody", main);
    body.appendChild(secEndpoint());
    body.appendChild(secModel());
    body.appendChild(secDiff());
    body.appendChild(secSaveCheck());
    body.appendChild(secSynth());
    renderBridge(main);
    bindStepNav();
  }

  /* ============ ① 接口地址 + 密钥 ============ */
  function secEndpoint() {
    const wrap = el("section", { class: "card", id: "secEndpoint" });
    wrap.innerHTML = `
      <div class="card-head">
        <div class="card-title">${Icons.globe} 接口配置</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap" id="endpointPresets">
          <button class="chip blue preset-btn" data-endpoint="https://api.xiaomimimo.com/v1/chat/completions" title="默认：小米 MiMo">默认 · MiMo</button>
          <button class="chip gray preset-btn" data-endpoint="https://api.openai.com/v1/audio/speech" title="OpenAI 兼容 audio/speech 风格">OpenAI 兼容</button>
          <button class="chip gray preset-btn" data-endpoint="" title="清空自填">自定义</button>
        </div>
      </div>
      <div class="field">
        <label class="field-label" for="ttsEndpoint">接口地址 <span class="req">*</span></label>
        <input class="input mono" id="ttsEndpoint" type="url" spellcheck="false" autocomplete="off"
          value="${esc(cfg.endpoint)}" placeholder="https://api.xiaomimimo.com/v1/chat/completions"
          aria-describedby="ttsEndpointHint">
        <div class="field-help" id="ttsEndpointHint">默认小米 MiMo（chat/completions 形态，音频在响应 choices[0].message.audio.data）；支持其他 OpenAI 兼容接口（如 /v1/audio/speech 风格），模型列表将从同源 /v1/models 获取。</div>
      </div>
      <div class="field">
        <label class="field-label" for="ttsKey">API 密钥 <span class="req">*</span></label>
        <div style="display:flex;gap:8px">
          <input class="input mono" id="ttsKey" type="password" autocomplete="off" value="${esc(cfg.apiKey)}"
            placeholder="sk-…（仅存本机，发送时作为 Bearer / 占位符 {{api_key}}）" style="flex:1">
          <button class="btn ghost" id="ttsKeyEye" type="button" aria-label="显示或隐藏密钥">${Icons.eye}</button>
        </div>
        <div class="field-help">密钥仅用于本控制台合成调用；Agent 端在服务端持有同一配置。</div>
      </div>`;
    const epInput = wrap.querySelector("#ttsEndpoint");
    const keyInput = wrap.querySelector("#ttsKey");
    epInput.addEventListener("input", () => { cfg.endpoint = epInput.value.trim(); saveCfgLocal(); markStep(1, epInput.validity.valid && !!cfg.endpoint); });
    keyInput.addEventListener("input", () => { cfg.apiKey = keyInput.value; saveCfgLocal(); });
    wrap.querySelector("#ttsKeyEye").addEventListener("click", () => {
      keyInput.type = keyInput.type === "password" ? "text" : "password";
    });
    wrap.querySelectorAll(".preset-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        if (btn.dataset.endpoint) { cfg.endpoint = btn.dataset.endpoint; epInput.value = cfg.endpoint; saveCfgLocal(); }
        else { epInput.focus(); }
        wrap.querySelectorAll(".preset-btn").forEach(b => b.classList.toggle("blue", b === btn));
        wrap.querySelectorAll(".preset-btn").forEach(b => b.classList.toggle("gray", b !== btn));
      });
    });
    return wrap;
  }

  /* ============ ② 获取模型 + 选择 + 类型 ============ */
  function secModel() {
    const wrap = el("section", { class: "card", id: "secModel" });
    wrap.innerHTML = `
      <div class="card-head">
        <div class="card-title">${Icons.box} 模型获取与选择</div>
        <button class="btn primary" id="ttsFetchModels">${Icons.refresh}<span>获取模型列表</span></button>
      </div>
      <div class="field-help" id="fetchHint" style="margin-bottom:10px">从接口地址同源 <span class="mono">/v1/models</span> 拉取（${esc(U.fmtCORSNote(cfg.endpoint))}）</div>
      <div id="modelListWrap">
        <div class="field">
          <label class="field-label" for="ttsModel">选择模型 <span class="req">*</span></label>
          <select class="input" id="ttsModel" aria-label="选择模型">
            <option value="">— 先点击上方「获取模型列表」—</option>
            ${models.map(m => `<option value="${esc(m.id)}" ${m.id === cfg.model ? "selected" : ""}>${esc(m.id)}${m._meta ? " · " + esc(m._meta.label) : ""}</option>`).join("")}
          </select>
        </div>
      </div>
      <div class="field" id="modelTypeField">
        <label class="field-label" for="ttsType">模型类型 <span class="req">*</span> <span style="font-weight:400;color:var(--text-faint);font-size:12px">（决定③出现哪种差异化选项；自动推断，可手动改）</span></label>
        <div style="display:flex;gap:8px;flex-wrap:wrap" id="ttsTypeBtns" role="radiogroup" aria-label="模型类型">
          ${Object.entries(TYPE_META).map(([k, v]) => `
            <button class="chip ${k === cfg.modelType ? v.tone : "gray"}" data-type="${k}" role="radio" aria-checked="${k === cfg.modelType}" title="${esc(v.desc)}" style="cursor:pointer">${esc(v.label)}</button>`).join("")}
        </div>
        <div class="field-help" id="typeDesc">${esc(TYPE_META[cfg.modelType].desc)}</div>
      </div>`;

    const sel = wrap.querySelector("#ttsModel");
    sel.addEventListener("change", () => {
      cfg.model = sel.value;
      const m = models.find(x => x.id === cfg.model);
      if (m && m._meta && m._meta.type) setType(m._meta.type, wrap);
      saveCfgLocal();
      rebuildDiff();
    });

    wrap.querySelector("#ttsFetchModels").addEventListener("click", async () => {
      const btn = wrap.querySelector("#ttsFetchModels");
      if (!cfg.endpoint) return Toast.show("先填写接口地址（①）", "warn");
      btn.disabled = true;
      btn.innerHTML = `${Icons.loader.replace('class="i"', 'class="i spin"')}<span>获取中…</span>`;
      try {
        const res = await API.ttsGetModels(cfg.endpoint, cfg.apiKey);
        models = (res.data || res || []).slice();
        if (!models.length) throw new Error("返回列表为空");
        sel.innerHTML = `<option value="">— 选择模型（${models.length} 个可用）—</option>` + models.map(m =>
          `<option value="${esc(m.id)}" ${m.id === cfg.model ? "selected" : ""}>${esc(m.id)}${m._meta ? " · " + esc(m._meta.label) : ""}</option>`).join("");
        if (cfg.model && !models.some(m => m.id === cfg.model)) cfg.model = "";
        if (!cfg.model) { cfg.model = models[0].id; sel.value = cfg.model; }
        const m = models.find(x => x.id === cfg.model);
        if (m && m._meta) setType(m._meta.type, wrap);
        markStep(2, true);
        Toast.show(`获取到 ${models.length} 个模型`, "ok");
        rebuildDiff();
      } catch (err) {
        Toast.show("获取失败：" + (err.message || "网络/CORS 受限"), "err");
        $("#fetchHint", wrap).innerHTML = `<span style="color:var(--red)">获取失败：${esc(err.message || "网络/CORS 受限")}。${esc(U.fmtCORSNote(cfg.endpoint))}</span>`;
      } finally {
        btn.disabled = false;
        btn.innerHTML = `${Icons.refresh}<span>获取模型列表</span>`;
      }
    });

    wrap.querySelectorAll("#ttsTypeBtns [data-type]").forEach(btn => {
      btn.addEventListener("click", () => setType(btn.dataset.type, wrap));
    });

    function setType(t, scope) {
      cfg.modelType = t; saveCfgLocal();
      scope.querySelectorAll("#ttsTypeBtns [data-type]").forEach(b => {
        b.classList.toggle("gray", b.dataset.type !== t);
        b.classList.toggle(TYPE_META[t].tone, b.dataset.type === t);
        b.setAttribute("aria-checked", String(b.dataset.type === t));
      });
      const desc = scope.querySelector("#typeDesc");
      if (desc) desc.textContent = TYPE_META[t].desc;
      markStep(3, t !== "none" && !!cfg.model);
      rebuildDiff();
    }
    /* 初始推断 */
    if (cfg.model && !models.length) { /* 配置恢复：type 已存 */ }
    return wrap;
  }

  /* ============ ③ 差异化选项 ============ */
  function secDiff() {
    const wrap = el("section", { class: "card", id: "secDiff" });
    wrap.innerHTML = `
      <div class="card-head">
        <div class="card-title">${Icons.wand} 差异化选项 <span class="chip ${TYPE_META[cfg.modelType].tone}" id="diffTypeChip">${TYPE_META[cfg.modelType].label}</span></div>
        <span style="color:var(--text-faint);font-size:12px" id="diffModelHint">${esc(cfg.model || "未选择模型")}</span>
      </div>
      <div id="diffBody"></div>`;
    return wrap;
  }

  function rebuildDiff() {
    const host = $("#secDiff #diffBody");
    const chip = $("#secDiff #diffTypeChip");
    const hint = $("#secDiff #diffModelHint");
    if (!host) return;
    chip.className = "chip " + TYPE_META[cfg.modelType].tone;
    chip.textContent = TYPE_META[cfg.modelType].label;
    hint.textContent = cfg.model || "未选择模型";
    dirtyHeader = dirtyBody = false;

    if (cfg.modelType === "prompt") host.innerHTML = diffPrompt();
    else if (cfg.modelType === "preset") { host.innerHTML = diffPreset(); bindPreset(host); }
    else if (cfg.modelType === "clone") { host.innerHTML = diffClone(); bindClone(host); }
    else host.innerHTML = `<div class="empty" style="padding:24px">${Icons.info}<div style="margin-top:8px;color:var(--text-faint);font-size:13px">非语音模型无需差异化配置；如需语音请切换模型类型。</div></div>`;

    if (cfg.modelType === "prompt") bindPrompt(host);
  }

  /* --- ③-A 文字提示语音：请求头/请求体编辑器（字节保真） --- */
  function diffPrompt() {
    const headerRaw = U.lsGet(LS_HEADER, MOCK.ttsDefaults.headerTemplate);
    const bodyRaw = U.lsGet(LS_BODY, MOCK.ttsDefaults.bodyTemplate);
    return `
    <div class="banner info" style="margin-bottom:14px">
      ${Icons.shield}<div class="bn-body"><b>字节保真承诺（两段原文独立编辑、独立保存）：</b>保存的原文落盘；发送时仅替换 <span class="mono">{{占位符}}</span>，其余一字不动 —— 不自动补请求头（如 Content-Type）、不做 JSON 重序列化、不做 pretty-print、不加任何前后缀；<b>落盘与编辑往返字节一致</b>（下方「往返校验」可证）。</div>
    </div>
    <div class="grid cols-2">
      <div class="field">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
          <label class="field-label" for="ttsHeader" style="margin:0">请求头 · 原文编辑器</label>
          <div style="display:flex;gap:8px">
            <button class="btn ghost" id="hdrRestore" title="恢复默认模板">${Icons.refresh}<span>默认</span></button>
            <button class="btn primary" id="hdrSave">${Icons.check}<span>独立保存</span></button>
          </div>
        </div>
        <textarea class="input mono code-area" id="ttsHeader" spellcheck="false" autocomplete="off" wrap="off" aria-label="请求头原文">${esc(headerRaw)}</textarea>
        <div class="editor-stats mono" id="hdrStats"></div>
      </div>
      <div class="field">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
          <label class="field-label" for="ttsBodyEditor" style="margin:0">请求体 · 原文编辑器</label>
          <div style="display:flex;gap:8px">
            <button class="btn ghost" id="bodyRestore" title="恢复默认模板">${Icons.refresh}<span>默认</span></button>
            <button class="btn primary" id="bodySave">${Icons.check}<span>独立保存</span></button>
          </div>
        </div>
        <textarea class="input mono code-area" id="ttsBodyEditor" spellcheck="false" autocomplete="off" wrap="off" aria-label="请求体原文">${esc(bodyRaw)}</textarea>
        <div class="editor-stats mono" id="bodyStats"></div>
      </div>
    </div>
    <div class="grid cols-2" style="margin-top:10px">
      <div class="card" style="background:var(--bg-elev)">
        <div class="card-title" style="font-size:12px;color:var(--text-faint)">可用占位符（发送时替换）</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">
          ${PLACEHOLDERS.map(p => `<span class="mono chip blue" title="${esc(p.desc)}">{{${p.name}}}</span>`).join("")}
        </div>
      </div>
      <div class="card" style="background:var(--bg-elev)">
        <div class="card-title" style="font-size:12px;color:var(--text-faint)">往返校验（字节一致证明）</div>
        <div style="display:flex;gap:8px;margin-top:8px;align-items:center;flex-wrap:wrap">
          <button class="btn" id="verifyRoundtrip">${Icons.check}<span>保存 → 重读 → 逐字节比对</span></button>
          <span id="verifyResult" class="mono" style="font-size:12px;color:var(--text-faint)">未验证</span>
        </div>
      </div>
    </div>`;
  }

  function bindPrompt(host) {
    const hdr = $("#ttsHeader", host), body = $("#ttsBodyEditor", host);

    function stats(ta, statsEl, dirty) {
      const v = ta.value;
      statsEl.innerHTML = `字节 ${byteLen(v)} · 字符 ${v.length} · 行 ${v ? v.split("\n").length : 0} · EOL ${eolOf(v)} ${dirty ? '<span style="color:var(--amber)">· 未保存 ✎</span>' : '<span style="color:var(--accent)">· 已保存 ✓</span>'}`;
    }
    function refresh() {
      stats(hdr, $("#hdrStats", host), dirtyHeader);
      stats(body, $("#bodyStats", host), dirtyBody);
    }
    hdr.addEventListener("input", () => { dirtyHeader = true; refresh(); });
    body.addEventListener("input", () => { dirtyBody = true; refresh(); });

    /* 独立保存：原文存取，不做任何转换 */
    $("#hdrSave", host).addEventListener("click", () => {
      U.lsSet(LS_HEADER, hdr.value);
      dirtyHeader = false; refresh();
      Toast.show(`请求头已独立保存（${byteLen(hdr.value)} 字节，原文落盘）`, "ok");
    });
    $("#bodySave", host).addEventListener("click", () => {
      U.lsSet(LS_BODY, body.value);
      dirtyBody = false; refresh();
      Toast.show(`请求体已独立保存（${byteLen(body.value)} 字节，原文落盘）`, "ok");
    });
    $("#hdrRestore", host).addEventListener("click", () => { hdr.value = MOCK.ttsDefaults.headerTemplate; dirtyHeader = true; refresh(); });
    $("#bodyRestore", host).addEventListener("click", () => { body.value = MOCK.ttsDefaults.bodyTemplate; dirtyBody = true; refresh(); });

    /* 往返校验 */
    $("#verifyRoundtrip", host).addEventListener("click", () => {
      U.lsSet(LS_HEADER, hdr.value); U.lsSet(LS_BODY, body.value);
      const backH = U.lsGet(LS_HEADER, "");
      const backB = U.lsGet(LS_BODY, "");
      const okH = backH === hdr.value, okB = backB === body.value;
      const el = $("#verifyResult", host);
      dirtyHeader = dirtyBody = false; refresh();
      if (okH && okB) {
        el.innerHTML = `<span style="color:var(--accent)">✓ 一致（头 ${byteLen(backH)}B / 体 ${byteLen(backB)}B，UTF-8 逐字节比对）</span>`;
        Toast.show("往返字节一致 ✓", "ok");
      } else {
        el.innerHTML = `<span style="color:var(--red)">✗ 不一致（头 ${okH ? "一致" : "不一致"} / 体 ${okB ? "一致" : "不一致"}）</span>`;
        Toast.show("往返校验失败", "err");
      }
    });

    refresh();
  }

  /* --- ③-B 预设音色：官方音色列表（中文名）+ 试听 --- */
  function diffPreset() {
    return `
    <div class="field-help" style="margin-bottom:10px">官方音色列表自 <span class="mono">${esc(cfg.model || "模型")}</span> 拉取（音色名为中文）。点击卡片选择，点试听逐个听。</div>
    <div style="display:flex;gap:8px;margin-bottom:12px">
      <button class="btn primary" id="fetchVoices">${Icons.refresh}<span>拉取官方音色列表</span></button>
      <span class="chip ${cfg.voice ? "green" : "gray"}" id="voicePicked">${cfg.voice ? "已选：" + esc(voiceNameOf(cfg.voice)) : "未选择音色"}</span>
    </div>
    <div class="voice-grid" id="voiceGrid">${voiceCards()}</div>`;
  }
  function voiceNameOf(id) {
    const v = voices.find(x => x.id === id); return v ? v.name : id;
  }
  function voiceCards() {
    if (!voices.length) voices = MOCK.ttsVoices.slice();
    return voices.map(v => `
      <div class="voice-card ${cfg.voice === v.id ? "picked" : ""}" data-voice="${esc(v.id)}" role="button" tabindex="0" aria-pressed="${cfg.voice === v.id}" title="${esc(v.desc)}">
        <div class="voice-name">${esc(v.name)}</div>
        <div class="voice-tags">${v.tags.map(t => `<span>${esc(t)}</span>`).join("")}</div>
        <div class="voice-desc">${esc(v.desc)}</div>
        <button class="btn ghost voice-try" data-try="${esc(v.id)}" title="试听 ${esc(v.name)}">${Icons.play}<span>试听</span></button>
      </div>`).join("");
  }
  function bindPreset(host) {
    const grid = $("#voiceGrid", host);
    if (!grid) return;
    grid.querySelectorAll("[data-voice]").forEach(card => {
      card.addEventListener("click", e => {
        if (e.target.closest("[data-try]")) return;
        cfg.voice = card.dataset.voice; saveCfgLocal();
        grid.querySelectorAll(".voice-card").forEach(x => x.classList.toggle("picked", x.dataset.voice === cfg.voice));
        const picked = $("#voicePicked", host);
        picked.className = "chip green"; picked.textContent = "已选：" + voiceNameOf(cfg.voice);
        markStep(3, true);
      });
      card.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); card.click(); } });
    });
    grid.querySelectorAll("[data-try]").forEach(btn => {
      btn.addEventListener("click", e => {
        e.stopPropagation();
        const id = btn.dataset.try;
        const v = voices.find(x => x.id === id);
        speakDemo(v ? v.name + "。" + (v.desc || "你好呀，我是" + v.name) : "试听", v);
        Toast.show(`试听「${v ? v.name : id}」…（浏览器演示音）`, "info");
      });
    });
    const fetchBtn = $("#fetchVoices", host);
    if (fetchBtn) fetchBtn.addEventListener("click", async () => {
      fetchBtn.disabled = true;
      fetchBtn.innerHTML = `${Icons.loader.replace('class="i"', 'class="i spin"')}<span>拉取中…</span>`;
      try {
        voices = await API.ttsGetVoices(cfg.endpoint, cfg.apiKey, cfg.model);
        if (!voices.length) voices = MOCK.ttsVoices.slice();
        grid.innerHTML = voiceCards();
        bindPreset($("#secDiff #diffBody"));
        Toast.show(`获取到 ${voices.length} 个官方音色`, "ok");
      } catch (err) {
        Toast.show("音色列表获取失败：" + err.message, "err");
      } finally {
        if (fetchBtn.isConnected) { fetchBtn.disabled = false; fetchBtn.innerHTML = `${Icons.refresh}<span>拉取官方音色列表</span>`; }
      }
    });
  }

  /* --- ③-C 语音克隆：音频上传模块 --- */
  function diffClone() {
    return `
    <div class="field-help" style="margin-bottom:10px">上传克隆音频（mp3 / wav，≤10MB，建议 10–30 秒清晰人声）。MiMo 克隆无需预建音色：发送时样本以 data:audio/mpeg;base64,… 内联进 audio.voice。生产环境音频由后端存储并转 base64；演示模式音频保留在本页内存。</div>
    <div class="dropzone" id="dropzone" tabindex="0" role="button" aria-label="上传克隆音频，也可拖拽文件到此处">
      ${Icons.upload.replace('class="i"', 'class="i" style="width:34px;height:34px"')}
      <div><b>点击选择文件</b> 或拖拽音频到此处</div>
      <div style="font-size:12px;color:var(--text-faint)">wav · mp3 · m4a ≤ 20MB</div>
      <input type="file" id="cloneInput" accept=".wav,.mp3,.m4a,audio/wav,audio/mpeg,audio/mp4,audio/x-m4a" hidden>
    </div>
    <div id="cloneList" style="display:flex;flex-direction:column;gap:8px;margin-top:12px">
      ${clones.length ? clones.map(cloneRow).join("") : `<div class="empty" style="padding:16px">${Icons.upload}<div style="margin-top:6px;color:var(--text-faint);font-size:12.5px">尚未上传克隆音频</div></div>`}
    </div>`;
  }
  function cloneRow(c) {
    const picked = cfg.cloneAudioId === c.id;
    return `
    <div class="clone-row ${picked ? "picked" : ""}" data-clone="${esc(c.id)}">
      <button class="btn ghost clone-pick" data-pick="${esc(c.id)}" title="选用此克隆音频">${Icons.play}<span>${picked ? "使用中" : "选用"}</span></button>
      <div style="min-width:0;flex:1">
        <div style="font-weight:600;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(c.name)}</div>
        <div class="mono" style="font-size:11px;color:var(--text-faint)">${Fmt.bytes(c.size)}${c.dur ? " · " + c.dur + "s" : ""}${c.objectUrl ? " · 本页可回放" : " · 元数据（blob 已失效，重新上传可回放）"}</div>
      </div>
      <div style="display:flex;gap:6px">
        <button class="btn ghost clone-play" data-cplay="${esc(c.id)}" ${c.objectUrl ? "" : "disabled"} title="回放">${Icons.play}</button>
        <button class="btn ghost danger clone-del" data-cdel="${esc(c.id)}" title="删除">${Icons.trash}</button>
      </div>
    </div>`;
  }
  function bindClone(host) {
    const dz = $("#dropzone", host);
    const input = $("#cloneInput", host);
    if (!dz) return;

    const handleFiles = async files => {
      const file = files && files[0];
      if (!file) return;
      if (!/\.(wav|mp3|m4a)$/i.test(file.name)) return Toast.show("仅支持 wav / mp3 / m4a", "err");
      if (file.size > 20 * 1024 * 1024) return Toast.show("文件超过 20MB", "err");
      Toast.show("上传中…", "info");
      try {
        const dur = await audioDuration(file).catch(() => null);
        const res = await API.ttsUploadCloneAudio(file, { model: cfg.model, endpoint: cfg.endpoint });
        const item = {
          id: res.id || U.uid("clone"), name: res.name || file.name, size: res.size || file.size,
          dur: dur || (res.dur || null), objectUrl: URL.createObjectURL(file), blob: file,
        };
        clones.unshift(item);
        cfg.cloneAudioId = item.id;
        saveClonesLocal(); saveCfgLocal();
        $("#cloneList", host).innerHTML = clones.map(cloneRow).join("");
        bindClone(host);
        Toast.show(`克隆音频已上传（${Fmt.bytes(item.size)}${dur ? " · " + dur + "s" : ""}），已选用`, "ok");
        markStep(3, true);
      } catch (err) {
        Toast.show("上传失败：" + err.message, "err");
      }
    };

    dz.addEventListener("click", () => input.click());
    dz.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
    input.addEventListener("change", () => handleFiles(input.files));
    ["dragenter", "dragover"].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add("drag"); }));
    ["dragleave", "drop"].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove("drag"); }));
    dz.addEventListener("drop", e => handleFiles(e.dataTransfer.files));

    host.querySelectorAll("[data-pick]").forEach(btn => {
      btn.addEventListener("click", () => {
        cfg.cloneAudioId = btn.dataset.pick; saveCfgLocal();
        $("#cloneList", host).innerHTML = clones.map(cloneRow).join("");
        bindClone(host);
        Toast.show("已选用该克隆音频", "ok");
      });
    });
    host.querySelectorAll("[data-cplay]").forEach(btn => {
      btn.addEventListener("click", () => {
        const c = clones.find(x => x.id === btn.dataset.cplay);
        if (c && c.objectUrl) playUrl(c.objectUrl);
      });
    });
    host.querySelectorAll("[data-cdel]").forEach(btn => {
      btn.addEventListener("click", () => {
        const id = btn.dataset.cdel;
        clones = clones.filter(x => x.id !== id);
        if (cfg.cloneAudioId === id) cfg.cloneAudioId = clones[0] ? clones[0].id : "";
        saveClonesLocal(); saveCfgLocal();
        $("#cloneList", host).innerHTML = clones.length ? clones.map(cloneRow).join("") : `<div class="empty" style="padding:16px">${Icons.upload}<div style="margin-top:6px;color:var(--text-faint);font-size:12.5px">尚未上传克隆音频</div></div>`;
        bindClone(host);
        Toast.show("已删除", "warn");
      });
    });
  }

  /* ============ ④ 保存与检查 ============ */
  function secSaveCheck() {
    const wrap = el("section", { class: "card", id: "secSaveCheck" });
    wrap.innerHTML = `
      <div class="card-head">
        <div class="card-title">${Icons.shield} 保存与检查</div>
        <div style="display:flex;gap:8px">
          <button class="btn" id="ttsCheck">${Icons.check}<span>检查配置</span></button>
          <button class="btn primary" id="ttsSaveAll">${Icons.save}<span>保存全部配置</span></button>
        </div>
      </div>
      <div id="checkReport" style="display:flex;flex-direction:column;gap:6px"></div>
      <div class="field-help">保存内容：接口地址、密钥、模型与类型、类型相关配置（模板原文 / 音色 / 克隆音频引用）。live 模式将写入后端 <span class="mono">PUT /api/v1/tts/config</span>（密钥落盘加密，回显脱敏）。</div>`;
    wrap.querySelector("#ttsSaveAll").addEventListener("click", saveAll);
    wrap.querySelector("#ttsCheck").addEventListener("click", runCheck);
    return wrap;
  }

  function collectCheck() {
    const items = [];
    const ok = (label, pass, hint) => items.push({ label, pass, hint });

    ok("接口地址格式合法", /^https?:\/\/.+/i.test(cfg.endpoint), "须以 http(s):// 开头");
    ok("API 密钥已填写", !!cfg.apiKey, "① 中填写");
    ok("已选择模型", !!cfg.model, "② 中获取并选择");
    ok("模型类型有效", cfg.modelType !== "none", "切换为文字提示语音/预设音色/语音克隆之一");

    if (cfg.modelType === "prompt") {
      const h = $("#ttsHeader"), b = $("#ttsBodyEditor");
      const hv = h ? h.value : U.lsGet(LS_HEADER, "");
      const bv = b ? b.value : U.lsGet(LS_BODY, "");
      ok("请求头原文非空", !!hv.trim(), "③ 编辑器");
      ok("请求体原文非空", !!bv.trim(), "③ 编辑器");
      ok("请求头已保存（无未保存修改）", !dirtyHeader, "点「独立保存」");
      ok("请求体已保存（无未保存修改）", !dirtyBody, "点「独立保存」");
      const ph = U.findPlaceholders((hv || "") + "\n" + (bv || ""));
      ok("占位符语法完整", !ph.length || ph.every(p => PLACEHOLDERS.some(k => k.name === p)), `检测到：${ph.join(", ") || "无"}（仅提示，未知占位符将原样发送）`);
    }
    if (cfg.modelType === "preset") ok("已选择官方音色", !!cfg.voice, "③ 音色列表中选择");
    if (cfg.modelType === "clone") ok("已上传并选用克隆音频", !!cfg.cloneAudioId, "③ 上传模块");
    return items;
  }

  async function runCheck() {
    const report = $("#checkReport");
    report.innerHTML = `<div class="empty" style="padding:10px">${Icons.loader.replace('class="i"', 'class="i spin"')} 检查中…</div>`;
    const items = collectCheck();

    /* 连通性：能否拿到模型列表（后端代理） */
    let conn = { label: "接口连通（获取模型列表）", pass: false, hint: "" };
    try {
      if (cfg.endpoint && cfg.apiKey) {
        const res = await API.ttsGetModels(cfg.endpoint, cfg.apiKey);
        conn.pass = !!(res.data || res || []).length;
        conn.hint = `返回 ${conn.pass ? (res.data || res).length : 0} 个模型`;
      } else { conn.hint = "地址/密钥缺失，跳过"; }
    } catch (err) { conn.hint = err.message; }
    items.push(conn);

    report.innerHTML = items.map(it => `
      <div class="check-row ${it.pass ? "pass" : "fail"}">
        <span class="check-ico">${it.pass ? Icons.check : Icons.alert}</span>
        <span class="check-label">${esc(it.label)}</span>
        <span class="check-hint">${esc(it.hint || "")}</span>
      </div>`).join("");
    const fail = items.filter(i => !i.pass).length;
    Toast.show(fail ? `检查完成：${fail} 项未通过` : "检查完成：全部通过 ✓", fail ? "warn" : "ok");
    markStep(4, fail === 0);
  }

  async function saveAll() {
    /* prompt 模板如有未保存修改，先独立落盘（保存=原文存取） */
    const h = $("#ttsHeader"), b = $("#ttsBodyEditor");
    if (h && dirtyHeader) { U.lsSet(LS_HEADER, h.value); dirtyHeader = false; if ($("#hdrStats")) refreshStatsAfterSave(); }
    if (b && dirtyBody) { U.lsSet(LS_BODY, b.value); dirtyBody = false; if ($("#bodyStats")) refreshStatsAfterSave(); }
    saveCfgLocal();
    const btn = $("#ttsSaveAll");
    btn.disabled = true;
    btn.innerHTML = `${Icons.loader.replace('class="i"', 'class="i spin"')}<span>保存中…</span>`;
    try {
      await API.ttsSaveConfig({
        ...cfg,
        headerTemplateRaw: U.lsGet(LS_HEADER, ""),
        bodyTemplateRaw: U.lsGet(LS_BODY, ""),
      });
      Toast.show("配置已保存" + (API.mode === "mock" ? "（mock：本机 localStorage）" : "（已写入后端）"), "ok");
      runCheck();
    } catch (err) {
      Toast.show("保存失败：" + err.message, "err");
    } finally {
      btn.disabled = false; btn.innerHTML = `${Icons.save}<span>保存全部配置</span>`;
    }
  }
  function refreshStatsAfterSave() {
    const h = $("#ttsHeader"), b = $("#ttsBodyEditor");
    if (h && $("#hdrStats")) $("#hdrStats").innerHTML = `字节 ${byteLen(h.value)} · 字符 ${h.value.length} · 行 ${h.value.split("\n").length} · EOL ${eolOf(h.value)} <span style="color:var(--accent)">· 已保存 ✓</span>`;
    if (b && $("#bodyStats")) $("#bodyStats").innerHTML = `字节 ${byteLen(b.value)} · 字符 ${b.value.length} · 行 ${b.value.split("\n").length} · EOL ${eolOf(b.value)} <span style="color:var(--accent)">· 已保存 ✓</span>`;
  }

  /* ============ ⑤ 试合成试听 ============ */
  function secSynth() {
    const wrap = el("section", { class: "card", id: "secSynth" });
    wrap.innerHTML = `
      <div class="card-head">
        <div class="card-title">${Icons.play} 试合成 · 试听</div>
        <button class="btn ghost" id="synthSampleBtn" title="换一条示例文案">${Icons.wand}<span>示例文案</span></button>
      </div>
      <div class="field">
        <label class="field-label" for="synthText">合成文本</label>
        <textarea class="input" id="synthText" rows="2" placeholder="输入要试合成的文本…">${esc(MOCK.ttsSampleTexts[0])}</textarea>
      </div>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:4px">
        <button class="btn primary" id="synthGo">${Icons.mic}<span>试合成</span></button>
        <span style="color:var(--text-faint);font-size:12px" id="synthVoiceHint"></span>
        <span class="mono" id="synthStat" style="font-size:12px;color:var(--text-faint)"></span>
      </div>
      <div id="synthPlayer" style="margin-top:12px" hidden>
        <div class="card" style="background:var(--bg-elev);display:flex;gap:12px;align-items:center;flex-wrap:wrap">
          <button class="btn primary" id="synthPlayBtn">${Icons.play}<span>播放</span></button>
          <span style="color:var(--text-dim);font-size:13px;min-width:0;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" id="synthEcho"></span>
          <span class="mono" style="font-size:11px;color:var(--text-faint)" id="synthEngine"></span>
        </div>
      </div>
      <div class="field-help" style="margin-top:10px">Mock 模式使用浏览器本地语音演示音色与节奏；<b>实际音质以服务端返回音频为准</b>（live 模式：POST /api/v1/tts/synthesize → 返回 audio_url 播放）。</div>`;

    const go = wrap.querySelector("#synthGo");
    const textEl = wrap.querySelector("#synthText");
    const stat = wrap.querySelector("#synthStat");
    const hint = wrap.querySelector("#synthVoiceHint");
    const updateHint = () => {
      if (cfg.modelType === "preset" && cfg.voice) hint.textContent = `音色：${voiceNameOf(cfg.voice)}（${cfg.model || ""}）`;
      else if (cfg.modelType === "clone" && cfg.cloneAudioId) hint.textContent = `克隆音色：${(clones.find(c => c.id === cfg.cloneAudioId) || {}).name || ""}`;
      else hint.textContent = cfg.modelType === "prompt" ? `模型：${cfg.model || ""}（文字提示驱动）` : "未配置";
    };
    updateHint();
    wrap.querySelector("#synthSampleBtn").addEventListener("click", () => {
      textEl.value = MOCK.ttsSampleTexts[Math.floor(Math.random() * MOCK.ttsSampleTexts.length)];
    });
    go.addEventListener("click", async () => {
      const text = textEl.value.trim();
      if (!text) return Toast.show("先输入合成文本", "warn");
      if (!cfg.model) return Toast.show("先在②选择模型", "warn");
      if (cfg.modelType === "preset" && !cfg.voice) return Toast.show("先在③选择音色", "warn");
      if (cfg.modelType === "clone" && !cfg.cloneAudioId) return Toast.show("先在③上传克隆音频", "warn");

      go.disabled = true;
      const t0 = performance.now();
      go.innerHTML = `${Icons.loader.replace('class="i"', 'class="i spin"')}<span>合成中…</span>`;
      stat.textContent = "";
      try {
        const payload = { text, model: cfg.model, modelType: cfg.modelType, voice: cfg.voice || undefined, cloneAudioId: cfg.cloneAudioId || undefined };
        const res = await API.ttsSynthesize(payload);
        const dt = ((performance.now() - t0) / 1000).toFixed(1);
        stat.textContent = `合成耗时 ${dt}s`;
        const player = wrap.querySelector("#synthPlayer");
        player.hidden = false;
        wrap.querySelector("#synthEcho").textContent = text;
        wrap.querySelector("#synthEngine").textContent = res.engine === "browser-speech-synthesis" ? "浏览器演示音" : (res.audio_url ? "服务端音频" : "");
        const playBtn = wrap.querySelector("#synthPlayBtn");
        const doPlay = () => {
          if (res.audio_url) playUrl(res.audio_url);
          else speakDemo(text, voices.find(v => v.id === cfg.voice));
        };
        playBtn.onclick = doPlay;
        doPlay();
        markStep(5, true);
        Toast.show("试合成完成，正在播放", "ok");
      } catch (err) {
        Toast.show("合成失败：" + err.message, "err");
        stat.textContent = "合成失败";
      } finally {
        go.disabled = false; go.innerHTML = `${Icons.mic}<span>试合成</span>`;
      }
    });
    return wrap;
  }

  /* ============ ⑥ 桥接说明（API 预留） ============ */
  function renderBridge(main) {
    $("#ttsBridge", main).innerHTML = `
    <section class="card" style="margin-top:14px;border-color:var(--accent-line)">
      <div class="card-head">
        <div class="card-title">${Icons.plug} Agent 语音桥接（对接层预留）</div>
        <span class="chip green">API 已留位</span>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px">
        <div class="card" style="background:var(--bg-elev)">
          <div class="card-title" style="font-size:12px;color:var(--text-faint)">调用链</div>
          <div class="mono" style="font-size:12px;color:var(--text-dim);line-height:2;margin-top:6px">
            Agent 决策「发语音」<br>→ tts_bridge 插件<br>→ <b style="color:var(--accent)">tts.synthesize(text, voice)</b><br>→ 缓存命中检查（去重）<br>→ OneBot <span style="color:var(--blue)">send_group_record</span><br>→ QQ 群语音消息
          </div>
        </div>
        <div class="card" style="background:var(--bg-elev)">
          <div class="card-title" style="font-size:12px;color:var(--text-faint)">前端已预留的对接点</div>
          <ul style="margin:6px 0 0;padding-left:18px;color:var(--text-dim);font-size:12.5px;line-height:2">
            <li><span class="mono">API.ttsSynthesize(payload)</span> → live: <span class="mono">POST /api/v1/tts/synthesize</span></li>
            <li><span class="mono">API.ttsSaveConfig(cfg)</span> → live: <span class="mono">PUT /api/v1/tts/config</span></li>
            <li><span class="mono">API.ttsGetModels/voices</span> → live: 后端代理（规避 CORS）</li>
            <li><span class="mono">API.ttsUploadCloneAudio</span> → live: multipart 上传</li>
          </ul>
        </div>
      </div>
      <div class="field-help" style="margin-top:10px">合成音频默认缓存（同文本+音色 命中即用），发送前经频率整形与内容安全复检，防止刷屏。</div>
    </section>`;
  }

  /* ---------- 播放工具 ---------- */
  function speakDemo(text, voice) {
    if (!("speechSynthesis" in window)) return Toast.show("当前浏览器不支持本地演示语音", "warn");
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "zh-CN";
    const v = voice || voices.find(x => x.id === cfg.voice);
    if (v) {
      if (/男声|少年/.test(v.name)) u.pitch = 0.7;
      else if (/少女|元气/.test(v.name)) u.pitch = 1.5;
      else u.pitch = 1.05;
      if (/活泼|元气/.test(v.name)) u.rate = 1.12;
      if (/沉静|治愈|悠然/.test(v.name)) u.rate = 0.88;
    }
    speechSynthesis.speak(u);
  }
  function playUrl(url) {
    const a = new Audio(url);
    a.play().catch(() => Toast.show("自动播放被浏览器拦截，请再点一次播放", "warn"));
  }
  function audioDuration(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const a = new Audio(url);
      a.addEventListener("loadedmetadata", () => { const d = a.duration; URL.revokeObjectURL(url); resolve(d ? Math.round(d * 10) / 10 : null); });
      a.addEventListener("error", () => { URL.revokeObjectURL(url); reject(new Error("无法读取音频时长")); });
    });
  }

  /* ---------- 步骤指示 ---------- */
  function markStep(n, done) {
    const step = document.querySelectorAll(".steps .step")[n - 1];
    if (!step) return;
    step.classList.toggle("active", done);
    step.classList.toggle("done", done);
  }
  function bindStepNav() {
    const secs = [["#secEndpoint", 1], ["#secModel", 2], ["#secDiff", 3], ["#secSaveCheck", 4], ["#secSynth", 5]];
    document.querySelectorAll(".steps .step").forEach((step, i) => {
      step.style.cursor = "pointer";
      step.addEventListener("click", () => $(secs[i][0]) && $(secs[i][0]).scrollIntoView({ behavior: "smooth", block: "start" }));
    });
  }

  /* 初始音色恢复 */
  if (!voices.length) voices = MOCK.ttsVoices.slice();

  App.registerPage("tts", { title: "TTS 语音", icon: Icons.mic, render });
})();
