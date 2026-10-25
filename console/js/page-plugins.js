/* ============================================================
   page-plugins.js — 插件技能管理
   数据源：API.getPlugins() / API.togglePlugin()
   ============================================================ */
"use strict";

(function () {

  function typeChip(t) {
    return t === "active"
      ? `<span class="chip blue" title="主动型：定时/事件触发，产出播报提案，不绕过频率整形">${Icons.zap}主动型</span>`
      : `<span class="chip cyan" title="被动型：被 Agent 调用或后台定时写状态，不直接发消息">${Icons.eye}被动型</span>`;
  }

  function pluginCard(p) {
    return `
    <div class="card plugin-card ${p.enabled ? "" : "off"}" data-plugin="${p.id}">
      <div class="card-head">
        <div style="display:flex;align-items:center;gap:10px;min-width:0">
          <span class="plugin-logo" data-logo>${esc(p.name[0])}</span>
          <div style="min-width:0">
            <div class="card-title" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(p.name)} <span class="mono" style="color:var(--text-faint);font-size:11px">v${esc(p.version)}</span></div>
            <div class="card-sub">${typeChip(p.type)} <span class="chip gray" style="font-size:10px;padding:1px 7px">${esc(p.trigger)}</span></div>
          </div>
        </div>
        <label class="switch" title="${p.enabled ? "点击停用" : "点击启用"}">
          <input type="checkbox" data-toggle ${p.enabled ? "checked" : ""} aria-label="${esc(p.name)} 启用开关">
          <span class="switch-slider"></span>
        </label>
      </div>
      <div style="color:var(--text-dim);font-size:13px;line-height:1.65;margin-top:8px">${esc(p.desc)}</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:10px">
        ${(p.perms || []).map(x => `<span class="mono chip gray" style="font-size:10.5px">${esc(x)}</span>`).join("")}
      </div>
      <details style="margin-top:12px">
        <summary class="mono" style="cursor:pointer;font-size:12px;color:var(--blue);user-select:none">skill.yaml manifest ▾</summary>
        <pre class="code-block mono" style="margin-top:8px;max-height:200px;overflow:auto">${esc(p.manifest)}</pre>
      </details>
    </div>`;
  }

  function render(main) {
    main.innerHTML = `
      <div class="page-head">
        <div>
          <div class="page-title">${Icons.plugin} 插件技能</div>
          <div class="page-desc">热插拔技能系统 —— active 型产出播报提案必过频率整形；passive 型只能写状态/被调用，无法绕过守卫直接发消息</div>
        </div>
        <button class="btn ghost" id="plgRefresh">${Icons.refresh}<span>刷新</span></button>
      </div>
      <div id="plgBody" class="grid cols-2" style="margin-top:16px">
        <div class="empty" style="grid-column:1/-1">${Icons.clock} 加载插件清单…</div>
      </div>`;

    const body = $("#plgBody", main);

    async function load() {
      body.innerHTML = `<div class="empty" style="grid-column:1/-1">${Icons.clock} 加载插件清单…</div>`;
      try {
        const plugins = await API.getPlugins();
        const enabled = plugins.filter(p => p.enabled).length;
        body.innerHTML = `
          <div style="grid-column:1/-1;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            <span class="chip green">${enabled} 启用</span>
            <span class="chip gray">${plugins.length - enabled} 停用</span>
            <span style="color:var(--text-faint);font-size:12.5px">插件目录 ./plugins · 每插件独立 manifest + 权限白名单</span>
          </div>
          ${plugins.map(pluginCard).join("")}`;
        bindToggles(body);
      } catch (err) {
        body.innerHTML = `<div class="banner warn" style="grid-column:1/-1">${Icons.alert}<div class="bn-body">加载失败：${esc(err.message)}</div></div>`;
      }
    }

    function bindToggles(scope) {
      scope.querySelectorAll("[data-toggle]").forEach(sw => {
        sw.addEventListener("change", async () => {
          const id = sw.closest("[data-plugin]").dataset.plugin;
          const card = sw.closest("[data-plugin]");
          const name = card.querySelector(".card-title").textContent.split(" v")[0];
          sw.disabled = true;
          try {
            await API.togglePlugin(id, sw.checked);
            card.classList.toggle("off", !sw.checked);
            Toast.show(`${name} 已${sw.checked ? "启用" : "停用"}`, sw.checked ? "ok" : "warn");
          } catch (err) {
            sw.checked = !sw.checked;
            Toast.show("操作失败：" + err.message, "err");
          } finally { sw.disabled = false; }
        });
      });
      /* logo 色 */
      scope.querySelectorAll("[data-plugin]").forEach((card, i) => {
        const palette = ["green", "blue", "purple", "amber", "pink", "cyan"];
        card.querySelector("[data-logo]").dataset.tone = palette[i % palette.length];
      });
    }

    $("#plgRefresh", main).addEventListener("click", load);
    load();
  }

  App.registerPage("plugins", { title: "插件技能", icon: Icons.plugin, render });
})();
