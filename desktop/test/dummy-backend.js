/* test/dummy-backend.js — 供 test-backend-manager / 冒烟使用的假后端
   契约 v1 形态：
   · /api/v1/system/health → {"ok":true,"ts":...}（就绪轮询）
   · /api/v1/system/status → ME_Agent 特征字段（memory/gateway/programs/tts/llm_enabled/started_at）
   · /console/* → 静态服务项目内 vendored 控制台（live 冒烟 loadURL 用）
   · /api/v1/world/state|transitions → 合法最小 WorldState（控制台总览可渲染）
   v0.3.0 联动启动形态：
   · POST /api/v1/system/boot → {ok:true, accepted:true, programs:[…]}（编排触发；
     触发后 0-1.2s snowluma busy 30% → 1.2-2.4s busy 70% → running——供壳侧
     boot-orchestrator 快照差分/进度事件/收口判定全链路观测）
   · GET /api/v1/programs → 程序条目数组（boot 触发前后状态演化）
   环境开关：
   · DUMMY_FOREIGN=1  → 健康返回 ok 但 status 无 ME_Agent 特征字段（模拟无关程序占端口）
   · DUMMY_NO_BOOT=1  → /system/boot 返回 404（模拟旧后端：端点未实现→壳静默降级）
   · DUMMY_PORT       → 监听端口（默认 8791）
   */
"use strict";

const http = require("http");
const fs = require("fs");
const path = require("path");
const PORT = Number(process.env.DUMMY_PORT || 8791);
const FOREIGN = process.env.DUMMY_FOREIGN === "1";
const NO_BOOT = process.env.DUMMY_NO_BOOT === "1";
const CONSOLE_DIR = path.join(__dirname, "..", "console");
let bootAt = 0;   // POST /system/boot 时间戳（0 = 未触发）

const WORLD_STATE = {
  time: { wall_clock: new Date().toISOString(), circadian: "evening" },
  environment: { weather: { condition: "晴", temp_c: 24, city: "深圳" }, special_day: null },
  body: { energy: 72, sleep_state: "awake", hunger: 40 },
  mind: { mood: { valence: 0.3, arousal: 0.2, label: "平静" }, stress: 15, wants: ["想看看群友在聊什么"] },
  social: { battery: 64, social_streak: 3, last_event: null },
  current: {
    location: "工作室",
    activity: { id: "evening_wind_down", name: "晚间收尾", start: "21:00", planned_end: "22:30", intensity: "low", interruptible: "medium" },
    task: "整理今天的记忆沉淀", intent: "收尾与轻社交",
  },
  meta: { version: 1042, last_transition: { from: "dinner", to: "evening_wind_down", at: "21:00", reason: "plan" } },
};

const STATUS = FOREIGN
  ? { app: "some-other-service", uptime_s: 99 }                     // 无 ME_Agent 特征字段
  : {
      memory: { enabled: true, entries: 1234 },
      gateway: { type: "onebot", connected: true },
      programs: { snowluma: "running", hindsight: "running" },
      tts: { provider: "mimo", voices: 9 },
      llm_enabled: true,
      started_at: new Date().toISOString(),
    };

const MIME = { ".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml", ".png": "image/png" };

/* v0.3.0：/programs 程序条目（boot 编排仿真——触发前 stopped，触发后 busy+progress → running） */
function programsSnapshot() {
  const t = bootAt ? Date.now() - bootAt : -1;
  let snow;
  if (!bootAt) snow = { id: "snowluma", name: "SnowLuma", status: "stopped" };
  else if (t < 1200) snow = { id: "snowluma", name: "SnowLuma", status: "busy", busy_action: "start", progress: { phase: "launch", percent: 30, done: false } };
  else if (t < 2400) snow = { id: "snowluma", name: "SnowLuma", status: "busy", busy_action: "start", progress: { phase: "handshake", percent: 70, done: false } };
  else snow = { id: "snowluma", name: "SnowLuma", status: "running", pid: 4242 };
  const obs = { id: "obsidian_sync", name: "Obsidian 同步", status: "running", pid: 4243 };
  return [snow, obs];
}

const srv = http.createServer((req, res) => {
  const send = obj => { res.writeHead(200, { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }); res.end(JSON.stringify(obj)); };
  const notFound = obj => { res.writeHead(404, { "Content-Type": "application/json" }); res.end(JSON.stringify(obj || { ok: false, error: "not found" })); };
  if (req.url === "/health" || req.url === "/api/v1/system/health") {
    return send({ ok: true, ts: Date.now(), service: FOREIGN ? "foreign" : "dummy-me-agent" });
  }
  if (req.url === "/api/v1/system/status") return send(STATUS);
  /* v0.3.0 联动启动：boot 触发 + programs 快照（含 404 降级仿真开关） */
  if (req.method === "POST" && req.url === "/api/v1/system/boot") {
    if (NO_BOOT) return notFound({ ok: false, error: "endpoint not implemented" });
    let body = "";
    req.on("data", d => { body += d; });
    req.on("end", () => {
      bootAt = Date.now();
      let src = null;
      try { src = JSON.parse(body).source; } catch (e) {}
      send({ ok: true, accepted: true, source: src, programs: ["snowluma", "obsidian_sync"], started_at: new Date().toISOString() });
    });
    return;
  }
  if (req.method === "GET" && req.url.startsWith("/api/v1/programs")) return send(programsSnapshot());
  if (req.url.startsWith("/api/v1/world/state")) return send(WORLD_STATE);
  if (req.url.startsWith("/api/v1/world/transitions")) return send([]);
  if (req.url.startsWith("/api/v1/")) return send({ ok: true, note: "dummy", path: req.url });
  if (req.url.startsWith("/console/")) {
    const rel = decodeURIComponent(req.url.slice("/console/".length).split("?")[0]) || "index.html";
    const p = path.normalize(path.join(CONSOLE_DIR, rel));
    if (!p.startsWith(CONSOLE_DIR)) { res.writeHead(403); return res.end("forbidden"); }
    try {
      const body = fs.readFileSync(p);
      res.writeHead(200, { "Content-Type": MIME[path.extname(p).toLowerCase()] || "application/octet-stream" });
      return res.end(body);
    } catch (e) { res.writeHead(404); return res.end("not found"); }
  }
  if (req.url === "/" || req.url === "/console" || req.url === "/console/") {
    res.writeHead(302, { Location: "/console/index.html" });
    return res.end();
  }
  return send({ ok: true, path: req.url });
});
srv.listen(PORT, "127.0.0.1", () => console.log(`[dummy-backend] listening on ${PORT} foreign=${FOREIGN} pid=${process.pid}`));
process.on("SIGTERM", () => process.exit(0));
process.on("SIGINT", () => process.exit(0));
