/* ============================================================
   test/test-boot-orchestrator.js — v0.3.0 联动启动单测（纯 Node，无需 Electron）
   ------------------------------------------------------------
   用例：
   1) 404 → manual（端点未实现：静默降级，不 throw、不进观测）
   2) 网络不可达（无服务监听）→ manual
   3) 200 + {ok:false} → manual（编排被拒）
   4) 200 + programs 演化 busy(30%)→busy(70%)→running →
      triggered→watching；status/progress 事件 program.* 主题同形态；
      收口 done(settled)；计数 2/2
   5) 旧 dummy 形态（/programs 返回非数组 {ok:true}）→ 静默收口 done，不崩
   6) watching 态 stop() → state=stopped、计时器清
   7) enabled:false → manual(disabled)
   断言全过输出 ALL PASS，退出码 0；任一失败退出码 1。
   ============================================================ */
"use strict";

const http = require("http");
const { BootOrchestrator } = require("../boot-orchestrator");

let failed = 0;
function assert(name, cond, detail) {
  if (cond) return console.log(`  ok - ${name}`);
  failed += 1;
  console.log(`  FAIL - ${name}${detail !== undefined ? " :: " + JSON.stringify(detail).slice(0, 300) : ""}`);
}

function listen(handler) {
  return new Promise(res => {
    const srv = http.createServer(handler);
    srv.listen(0, "127.0.0.1", () => res({ srv, url: `http://127.0.0.1:${srv.address().port}` }));
  });
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  /* ---------- 1) 404 → manual ---------- */
  {
    const { srv, url } = await listen((req, res) => { res.writeHead(404, { "Content-Type": "application/json" }); res.end(JSON.stringify({ ok: false, error: "not found" })); });
    const o = new BootOrchestrator({ url, log: () => {} });
    const r = await o.trigger();
    assert("404 → manual（端点未实现）", r.state === "manual" && o.note === "endpoint-missing" && o.httpStatus === 404, r);
    assert("404 manual：未进观测（无计时器）", !o._timer, o._timer);
    const r2 = await o.trigger();
    assert("manual 态二次 trigger 不重试", r2.already === true, r2);
    srv.close();
  }

  /* ---------- 2) 网络不可达 → manual ---------- */
  {
    const o = new BootOrchestrator({ url: "http://127.0.0.1:9", timeoutMs: 500, log: () => {} });   // 9 = discard 端口，无监听
    const r = await o.trigger();
    assert("不可达 → manual(unreachable)", r.state === "manual" && o.note === "unreachable", r);
  }

  /* ---------- 3) ok:false → manual ---------- */
  {
    const { srv, url } = await listen((req, res) => { res.writeHead(200, { "Content-Type": "application/json" }); res.end(JSON.stringify({ ok: false, error: "orchestrator disabled" })); });
    const o = new BootOrchestrator({ url, log: () => {} });
    const r = await o.trigger();
    assert("ok:false → manual(refused)", r.state === "manual" && o.note === "refused", r);
    srv.close();
  }

  /* ---------- 4) 触发成功 + 进度观测全链路 ---------- */
  {
    const prog = [
      [{ id: "snowluma", name: "SnowLuma", status: "stopped" }, { id: "obsidian_sync", name: "Obsidian 同步", status: "running", pid: 4243 }],
      [{ id: "snowluma", name: "SnowLuma", status: "busy", busy_action: "start", progress: { phase: "launch", percent: 30, done: false } }, { id: "obsidian_sync", name: "Obsidian 同步", status: "running", pid: 4243 }],
      [{ id: "snowluma", name: "SnowLuma", status: "busy", busy_action: "start", progress: { phase: "handshake", percent: 70, done: false } }, { id: "obsidian_sync", name: "Obsidian 同步", status: "running", pid: 4243 }],
      [{ id: "snowluma", name: "SnowLuma", status: "running", pid: 4242 }, { id: "obsidian_sync", name: "Obsidian 同步", status: "running", pid: 4243 }],
    ];
    let i = 0;
    const { srv, url } = await listen((req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      if (req.method === "POST" && req.url === "/api/v1/system/boot") {
        let body = ""; req.on("data", d => body += d); req.on("end", () => res.end(JSON.stringify({ ok: true, accepted: true, programs: ["snowluma", "obsidian_sync"] })));
        return;
      }
      if (req.url === "/api/v1/programs") return res.end(JSON.stringify(prog[Math.min(i, prog.length - 1)]));
      res.end(JSON.stringify({ ok: true }));
    });
    const seen = [];
    const o = new BootOrchestrator({ url, pollIntervalMs: 90, pollTimeoutMs: 1500, minWatchMs: 500, settlePolls: 2, log: () => {}, onEvent: e => seen.push(e) });
    const r = await o.trigger();
    assert("200 → watching", r.state === "watching" && o.httpStatus === 200, r);
    /* 演化：busy → running（i 每拍推进一档） */
    const adv = setInterval(() => { i = Math.min(i + 1, prog.length - 1); }, 180);
    await sleep(1400);
    clearInterval(adv);
    const sum = o.summary();
    assert("收口 done(settled)", o.state === "done" && o.note === "settled", sum);
    assert("status 事件 ≥1（busy→running）", sum.statusEvents >= 1, sum);
    assert("progress 事件 ≥1（30→70%）", sum.progressEvents >= 1, sum);
    assert("计数 2/2 运行", sum.running === 2 && sum.total === 2, sum);
    const statusEv = seen.find(e => e.type === "status" && e.status === "running" && e.id === "snowluma");
    assert("status 事件 program.status 主题同形态（id/name/status/pid/prevStatus）",
      !!statusEv && statusEv.name === "SnowLuma" && statusEv.pid === 4242 && statusEv.prevStatus === "busy", statusEv);
    const progEv = seen.find(e => e.type === "progress" && e.id === "snowluma");
    assert("progress 事件 program.progress 主题同形态（phase/percent）",
      !!progEv && typeof progEv.percent === "number" && typeof progEv.phase === "string", progEv);
    const doneEv = seen.find(e => e.type === "done");
    assert("done 事件含汇总（changed/running/total）", !!doneEv && doneEv.running === 2 && doneEv.total === 2 && doneEv.changed >= 1, doneEv);
    assert("收口后计时器已清", !o._timer, o._timer);
    srv.close();
  }

  /* ---------- 5) 旧 dummy 形态（/programs 非数组）→ 静默收口 ---------- */
  {
    const { srv, url } = await listen((req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      if (req.method === "POST") return res.end(JSON.stringify({ ok: true, accepted: true }));
      res.end(JSON.stringify({ ok: true, note: "dummy", path: req.url }));    // 非数组
    });
    const o = new BootOrchestrator({ url, pollIntervalMs: 80, pollTimeoutMs: 1000, settlePolls: 3, minWatchMs: 0, log: () => {} });
    const r = await o.trigger();
    await sleep(700);
    assert("旧 dummy：watching 进入后静默收口 done（不崩不刷屏）", r.state === "watching" && o.state === "done", o.summary());
    srv.close();
  }

  /* ---------- 6) watching 态 stop() ---------- */
  {
    const { srv, url } = await listen((req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      if (req.method === "POST") return res.end(JSON.stringify({ ok: true }));
      res.end(JSON.stringify([{ id: "x", name: "X", status: "busy", busy_action: "start", progress: { phase: "p", percent: 10, done: false } }]));
    });
    const o = new BootOrchestrator({ url, pollIntervalMs: 100, watchTimeoutMs: 60000, log: () => {} });
    await o.trigger();
    assert("stop 前 watching（busy 常驻不收口）", o.state === "watching", o.state);
    o.stop();
    assert("stop() → stopped + 计时器清", o.state === "stopped" && !o._timer, o.state);
    await sleep(250);
    assert("stop 后无新事件", o.events.filter(e => e.at > 0).length === o.events.length, o.events.length);
    srv.close();
  }

  /* ---------- 7) enabled:false ---------- */
  {
    const o = new BootOrchestrator({ url: "http://127.0.0.1:1", enabled: false, log: () => {} });
    const r = await o.trigger();
    assert("enabled:false → manual(disabled)（不发请求）", r.state === "manual" && o.note === "disabled", r);
  }

  console.log(failed ? `\n${failed} FAILED` : "\nALL PASS");
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
