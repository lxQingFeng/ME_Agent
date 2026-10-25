/* ============================================================
   page-chat.js — 对话流监控（OneBot 通道）
   数据源：API.getConversations() / API.getMessages() / API.sendMessage()
   ============================================================ */
"use strict";

(function () {
  let activeConv = null;

  const AVATAR_COLORS = {};
  function avatarColor(name) {
    if (!AVATAR_COLORS[name]) {
      const palette = ["#22C55E", "#38BDF8", "#A78BFA", "#F5A524", "#EC4899", "#2DD4BF", "#FB7185"];
      let h = 0; for (const c of name) h = (h * 31 + c.charCodeAt(0)) % 997;
      AVATAR_COLORS[name] = palette[h % palette.length];
    }
    return AVATAR_COLORS[name];
  }

  function prChip(pr) {
    if (!pr) return "";
    const tone = pr === "P0" ? "red" : pr === "P1" ? "amber" : "gray";
    return `<span class="chip ${tone}" style="font-size:10px;padding:1px 7px" title="事件分级：${pr}">${pr}</span>`;
  }

  function convItem(c) {
    const icon = c.type === "group" ? Icons.users : Icons.user;
    return `
    <button class="conv-item ${activeConv === c.id ? "active" : ""}" data-conv="${c.id}" role="button">
      <span class="conv-ico" style="color:${avatarColor(c.name)}">${icon}</span>
      <span class="conv-body">
        <span class="conv-name">${esc(c.name)}${c.type === "group" ? '<span class="chip blue" style="font-size:10px;padding:0 6px;margin-left:6px">群</span>' : '<span class="chip purple" style="font-size:10px;padding:0 6px;margin-left:6px">私聊</span>'}</span>
        <span class="conv-last">${esc(c.last.from)}：${esc(c.last.text)}</span>
      </span>
      ${c.unread ? `<span class="conv-unread">${c.unread}</span>` : ""}
    </button>`;
  }

  function msgHTML(m) {
    const mine = m.role === "agent";
    const av = `<span class="msg-avatar" style="background:${avatarColor(m.from)}22;color:${avatarColor(m.from)}">${esc(m.from[0])}</span>`;
    const meta = [];
    if (m.pr) meta.push(prChip(m.pr));
    if (m.at) meta.push(`<span class="chip green" style="font-size:10px;padding:1px 7px">@我</span>`);
    if (m.burst) meta.push(`<span class="chip amber" style="font-size:10px;padding:1px 7px" title="连续 2 拍相关，合并为一次回复">burst 合并</span>`);
    if (m.audio) meta.push(`<span class="chip purple" style="font-size:10px;padding:1px 7px">语音 ${m.audio}″ · TTS</span>`);
    return `
    <div class="msg ${mine ? "mine" : ""}">
      ${av}
      <div class="msg-body">
        <div class="msg-head">
          <b>${esc(m.from)}</b>
          <span class="msg-time">${Fmt.time(new Date(m.ts))}</span>
          ${meta.join("")}
        </div>
        <div class="msg-bubble">${esc(m.text)}${m.llm ? `<div class="msg-llm">${Icons.spark}<span>${esc(m.llm)} · ${m.latency_ms ? (m.latency_ms / 1000).toFixed(1) + "s" : "—"}</span></div>` : ""}</div>
      </div>
    </div>`;
  }

  function render(main) {
    main.innerHTML = `
      <div class="page-head">
        <div>
          <div class="page-title">${Icons.chat} 对话流</div>
          <div class="page-desc">OneBot 通道消息监控 —— 事件分级路由（P0 即时回 / P1 下一拍评估 / P2 仅计数入记忆原料）</div>
        </div>
      </div>

      <div class="chat-layout">
        <aside class="conv-list card" aria-label="会话列表">
          <div class="conv-list-head">
            <span>会话</span>
            <span class="chip gray" id="convCount">—</span>
          </div>
          <div id="convListBody" style="display:flex;flex-direction:column;gap:2px"><div class="empty" style="padding:20px">${Icons.clock} 加载中…</div></div>
        </aside>

        <section class="card chat-pane" aria-label="消息流">
          <div class="chat-pane-head">
            <div>
              <div style="font-weight:700" id="chatTitle">选择一个会话</div>
              <div style="font-size:12px;color:var(--text-faint);margin-top:2px" id="chatSub">对话管道：OneBot → arbiter → LLM → 整形 → 发送</div>
            </div>
            <div style="display:flex;gap:8px" id="chatStats"></div>
          </div>
          <div class="msg-flow" id="msgFlow" role="log" aria-live="polite" aria-label="消息流">
            <div class="empty" style="padding:48px 20px">${Icons.users}<div style="margin-top:10px;color:var(--text-faint);font-size:13px">← 从左侧选择会话查看消息流</div></div>
          </div>
          <div class="chat-input" id="chatInputWrap">
            <textarea id="chatInput" rows="1" placeholder="以管理员身份发送测试消息（live 模式经 OneBot 真实下发；mock 仅本地演示）…" aria-label="发送测试消息"></textarea>
            <button class="btn primary" id="chatSend">${Icons.send}<span>发送</span></button>
          </div>
        </section>
      </div>
    `;

    const listBody = $("#convListBody", main);
    API.getConversations().then(convs => {
      activeConv = convs[0] ? convs[0].id : null;
      listBody.innerHTML = convs.map(convItem).join("") || '<div class="empty" style="padding:20px">暂无会话</div>';
      $("#convCount", main).textContent = convs.length + " 个";
      listBody.querySelectorAll("[data-conv]").forEach(btn => {
        btn.addEventListener("click", () => {
          activeConv = btn.dataset.conv;
          listBody.querySelectorAll(".conv-item").forEach(x => x.classList.toggle("active", x.dataset.conv === activeConv));
          loadMessages(convs);
        });
      });
      loadMessages(convs);
    });

    function loadMessages(convs) {
      const c = convs.find(x => x.id === activeConv);
      if (!c) return;
      $("#chatTitle", main).textContent = (c.type === "group" ? "群聊 · " : "私聊 · ") + c.name;
      $("#chatStats", main).innerHTML = `
        <span class="chip gray" title="最近 1 小时消息数">${Icons.msgSquare}1h ${c.stats.msgs_1h} 条</span>
        <span class="chip red" title="P0 即时回复事件">P0 ${c.stats.p0}</span>
        <span class="chip amber" title="P1 下一拍决策事件">P1 ${c.stats.p1}</span>
        <span class="chip blue" title="burst 合并回复">burst ${c.stats.burst}</span>`;
      const flow = $("#msgFlow", main);
      flow.innerHTML = `<div class="empty" style="padding:30px">${Icons.clock} 加载消息…</div>`;
      API.getMessages(c.id).then(msgs => {
        flow.innerHTML = msgs.map(msgHTML).join("") || '<div class="empty" style="padding:30px">无消息</div>';
        flow.scrollTop = flow.scrollHeight;
        /* live 模式预留：后续由 WS world/chat 事件推送增量 append */
      });
    }

    /* 发送测试消息 */
    $("#chatSend", main).addEventListener("click", async () => {
      const input = $("#chatInput", main);
      const text = input.value.trim();
      if (!text || !activeConv) return Toast.show("先写点什么吧", "warn");
      const flow = $("#msgFlow", main);
      const sending = document.createElement("div");
      sending.className = "msg mine";
      sending.innerHTML = `<span class="msg-avatar" style="background:var(--blue-soft);color:var(--blue)">管</span>
        <div class="msg-body"><div class="msg-head"><b>管理员·我</b><span class="msg-time">${Fmt.time(new Date())}</span><span class="chip blue" style="font-size:10px;padding:1px 7px">console</span></div>
        <div class="msg-bubble" style="opacity:.6">${esc(text)} <span style="color:var(--text-faint)">发送中…</span></div></div>`;
      flow.appendChild(sending);
      flow.scrollTop = flow.scrollHeight;
      input.value = ""; input.style.height = "auto";
      try {
        const res = await API.sendMessage(activeConv, text);
        sending.querySelector(".msg-bubble").style.opacity = "1";
        sending.querySelector(".msg-bubble").innerHTML = esc(text) + (res.note ? ` <span class="mono" style="color:var(--text-faint);font-size:11px">（${esc(res.note)}）</span>` : "");
        Toast.show("已提交发送", "ok");
      } catch (err) {
        sending.querySelector(".msg-bubble").innerHTML = esc(text) + ` <span style="color:var(--red)">发送失败：${esc(err.message)}</span>`;
        Toast.show("发送失败", "err");
      }
    });

    const ta = $("#chatInput", main);
    ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 120) + "px"; });
    ta.addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chatSend", main).click(); }
    });
  }

  App.registerPage("chat", { title: "对话流", icon: Icons.chat, render });
})();
