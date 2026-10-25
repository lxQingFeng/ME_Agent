/* ============================================================
   app.js — 应用骨架：路由 / 导航 / 时钟 / 世界心跳模拟引擎
   ============================================================ */
"use strict";

const App = {
  pages: {},          // name → {title, desc, icon, render}
  current: null,
  hbTimer: null,
  hbTickSec: 0,       // 距下一拍秒数（演示：30s/拍，真实：300s/拍）
  HB_DEMO_SECONDS: 30,

  registerPage(name, def) { this.pages[name] = def; },

  boot() {
    this.bindNav();
    this.startClock();
    this.restoreModeBadge();
    this.startHeartbeat();
    window.addEventListener("hashchange", () => this.route());
    this.route();
    console.log("%cQQ Agent Console ready", "color:#22C55E;font-weight:bold");
  },

  bindNav() {
    $$("#navList a").forEach(a => {
      a.addEventListener("click", e => { e.preventDefault(); location.hash = a.getAttribute("href"); });
    });
  },

  restoreModeBadge() {
    const saved = localStorage.getItem("console.api.mode");
    API.setMode(saved === "live" ? "live" : "mock");
  },

  route() {
    const name = (location.hash.replace(/^#\//, "") || "overview").split("?")[0];
    const page = this.pages[name] || this.pages.overview;
    this.current = name;
    $$("#navList a").forEach(a => a.classList.toggle("active", a.dataset.page === name));
    const main = $("#main");
    main.innerHTML = "";
    main.setAttribute("aria-live", "polite");
    try {
      page.render(main);
    } catch (err) {
      console.error(err);
      main.innerHTML = `<div class="banner warn">${Icons.alert}<div class="bn-body">页面渲染出错：<b>${esc(err.message)}</b>（演示数据异常，刷新重试）</div></div>`;
    }
    main.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
  },

  /* ---------- 顶部时钟 ---------- */
  startClock() {
    const clockEl = $("#topbarClock");
    const tick = () => {
      const now = new Date();
      clockEl.textContent = Fmt.time(now);
      clockEl.setAttribute("datetime", now.toISOString());
    };
    tick();
    setInterval(tick, 1000);
  },

  /* ---------- 世界心跳模拟引擎（演示加速：30s = 1 tick，真实 5min） ----------
     live 模式接入后：本引擎停用，改由 WS /world/state 推送驱动同样的 UI。 */
  startHeartbeat() {
    const cdEl = $("#hbCountdown");
    const tickEl = $("#hbTickCount");
    this.hbTickSec = this.HB_DEMO_SECONDS;
    clearInterval(this.hbTimer);

    const onSecond = () => {
      this.hbTickSec--;
      if (this.hbTickSec <= 0) { this.advanceWorld(); this.hbTickSec = this.HB_DEMO_SECONDS; }
      const s = this.hbTickSec;
      cdEl.textContent = `0:${Fmt.two(s)}`;
    };
    this.hbTimer = setInterval(onSecond, 1000);
    cdEl.textContent = `0:${Fmt.two(this.HB_DEMO_SECONDS)}`;
    tickEl.textContent = `tick #${MOCK.worldState.meta.version}`;
    cdEl.parentElement.title = "世界心跳：真实系统 5 分钟/拍（纯规则推进）。演示模式已加速为 30 秒/拍";
  },

  /* 演示用：世界状态演化（对动力学公式的极简模拟） */
  advanceWorld() {
    const w = MOCK.worldState;
    const now = new Date(w.time.wall_clock);
    now.setMinutes(now.getMinutes() + 5);
    w.time.wall_clock = now.toISOString().replace("Z", "+08:00");
    const h = now.getHours();
    w.time.circadian = h < 6 ? "night" : h < 12 ? "morning" : h < 18 ? "afternoon" : h < 23 ? "evening" : "night";
    w.body.energy = U.clamp(w.body.energy - (w.current.activity.intensity === "high" ? 2 : w.current.activity.intensity === "medium" ? 1.2 : 0.6), 0, 100);
    w.body.hunger = U.clamp(w.body.hunger + 0.8, 0, 100);
    w.mind.mood.valence = U.clamp(+(w.mind.mood.valence + (Math.random() - 0.48) * 0.06).toFixed(2), -1, 1);
    w.mind.mood.arousal = U.clamp(+(w.mind.mood.arousal + (Math.random() - 0.5) * 0.05).toFixed(2), 0, 1);
    w.mind.stress = U.clamp(w.mind.stress + (Math.random() - 0.5) * 3, 0, 100);
    w.social.battery = U.clamp(w.social.battery - w.social.recent_density / 600, 0, 100);
    w.meta.version += 1;

    /* 计划块到期 → 合法转移（演示序列） */
    const [ph, pm] = w.current.activity.planned_end.split(":").map(Number);
    if (h * 60 + now.getMinutes() >= ph * 60 + pm) {
      const nexts = [
        { id: "rest", name: "休息", dur: 25, intensity: "light", task: "歇一会儿，泡杯咖啡", intent: "休息完继续下一个番茄钟" },
        { id: "coding", name: "写代码", dur: 50, intensity: "medium", task: "Agent 记忆模块重构收尾", intent: "写完这段去吃午饭" },
        { id: "lunch", name: "午饭", dur: 40, intensity: "light", task: "吃午饭", intent: "吃完小睡二十分钟" },
        { id: "reading", name: "看书", dur: 50, intensity: "light", task: "把《三体III》最后两章看完", intent: "看完就洗漱睡觉" },
      ];
      const next = nexts[Math.floor(Math.random() * nexts.length)];
      const st = now;
      const et = new Date(st.getTime() + next.dur * 60000);
      MOCK.transitions.unshift({
        at: Fmt.hm(now), from: w.current.activity.id, to: next.id, reason: "plan", hot: true,
      });
      if (MOCK.transitions.length > 8) MOCK.transitions.length = 8;
      w.current.activity = { id: next.id, name: next.name, start: Fmt.hm(st), planned_end: Fmt.hm(et), intensity: next.intensity, interruptible: next.intensity === "high" ? "low" : "medium" };
      w.current.task = next.task;
      w.current.intent = next.intent;
      w.meta.last_transition = { from: w.current.activity.id, to: next.id, at: Fmt.hm(now), reason: "plan" };
    }

    $("#hbTickCount").textContent = `tick #${w.meta.version}`;
    document.dispatchEvent(new CustomEvent("world:tick", { detail: { version: w.meta.version } }));
  },
};

window.App = App;
