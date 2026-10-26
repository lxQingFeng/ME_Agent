/* ============================================================
   boot-orchestrator.js — v0.3.0 联动启动（后端就绪后触发第三方程序自动编排）
   ------------------------------------------------------------
   1) trigger()：后端就绪（attached / spawned）后 POST /api/v1/system/boot
      （端点由 ME_Agent 后端同轮实现——触发 SnowLuma 等第三方程序编排）。
      分类（全部走「静默降级」路径，不报错不刷屏）：
      · 2xx（JSON body.ok !== false）→ triggered → 进入 watching 进度观测；
      · 404 / 405 → manual（端点未实现：正常旧后端形态，控制台程序页手动按钮照常）；
      · 网络错误 / 非 2xx / ok:false → manual（一次性日志，不再重试）。
   2) watching 进度观测：轮询 GET /api/v1/programs（默认 1.2s/拍，与控制台
      busy 语义轮询同拍），快照差分派发**既有 program.* 推送主题同形态**事件：
      · {type:"status",  id, name, status, pid, webui, prevStatus} ← topic program.status
      · {type:"progress", id, name, ...progress}                   ← topic program.progress
      壳侧复用主题 payload 形态而非另造协议；纯 HTTP 快照差分（主进程无 WS 客户端
      依赖、零新增 npm 依赖、可脱离 Electron 单测）。事件附 {running, total} 计数。
   3) done 收口：连续 settlePolls 拍无暂态条目（status=busy/starting/pending/queued
      或 busy_action 非空或 progress.done===false）且（观测到过变化 或 满 minWatchMs
      观测窗——防 POST /boot 后端尚未翻转状态时提前收口）；watchTimeoutMs 兜底。
   4) 全 HTTP，无子进程 —— 全链路窗口自查（v0.3.0 §3）零新增 spawn 路径。
   ============================================================ */
"use strict";

const http = require("http");

const TRANSIENT_STATUSES = ["busy", "starting", "pending", "queued"];
const FAILING_STATUSES = ["error", "failed", "crash", "crashed"];

class BootOrchestrator {
  constructor(opts = {}) {
    this.url = opts.url || "http://127.0.0.1:8000";
    this.bootPath = opts.bootPath || "/api/v1/system/boot";
    this.programsPath = opts.programsPath || "/api/v1/programs";
    this.enabled = opts.enabled !== false;
    this.timeoutMs = opts.timeoutMs || 6000;            // POST /boot 超时
    this.pollIntervalMs = opts.pollIntervalMs || 1200;  // 与控制台 busy 轮询同拍
    this.pollTimeoutMs = opts.pollTimeoutMs || 2500;
    this.watchTimeoutMs = opts.watchTimeoutMs || 240000; // 进度观测兜底（含下载类长动作）
    this.minWatchMs = opts.minWatchMs != null ? opts.minWatchMs : 8000;   // 无变化时的最短观测窗（防提前收口；0 合法 = 不设下限）
    this.settlePolls = opts.settlePolls || 3;           // 连续 N 拍无暂态 → settled
    this.log = opts.log || (() => {});
    this.onEvent = opts.onEvent || (() => {});

    this.state = "idle";        // idle → triggering → watching → done | manual
    this.note = "";
    this.httpStatus = null;
    this.events = [];           // 事件留痕（单测/冒烟断言用，截尾 40 条）
    this.statusEvents = 0;
    this.progressEvents = 0;
    this.runningCount = 0;
    this.totalCount = 0;

    this._timer = null;
    this._last = null;          // Map(id → 快照 {status,pid,name,webui,progressJson,progressObj})
    this._settle = 0;
    this._pollFails = 0;
    this._sawChange = false;
    this._watchT0 = 0;
  }

  /* ---------- HTTP POST（读 JSON 响应；失败 resolve 而非 reject——降级语义收口） ---------- */
  _postJson(p, body, timeoutMs) {
    return new Promise(resolve => {
      let data;
      try { data = Buffer.from(JSON.stringify(body || {})); } catch (e) { data = Buffer.from("{}"); }
      let u;
      try { u = new URL(this.url); } catch (e) { return resolve({ status: 0, error: "bad-url" }); }
      const req = http.request({
        protocol: u.protocol, hostname: u.hostname, port: u.port, path: p, method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": data.length },
        timeout: timeoutMs,
      }, res => {
        let out = "";
        res.setEncoding("utf8");
        res.on("data", d => { out += d; if (out.length > 65536) req.destroy(); });
        res.on("end", () => {
          let json = null;
          try { json = JSON.parse(out); } catch (e) { /* 非 JSON：按状态码分类 */ }
          resolve({ status: res.statusCode, json, body: out.slice(0, 200) });
        });
      });
      req.on("timeout", () => req.destroy(new Error("timeout")));
      req.on("error", err => resolve({ status: 0, error: err.message }));
      req.end(data);
    });
  }

  /* ---------- HTTP GET（读 JSON；与 backend-manager._getJson 同口径） ---------- */
  _getJson(p, timeoutMs) {
    return new Promise(resolve => {
      const req = http.get(`${this.url}${p}`, { timeout: timeoutMs }, res => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", d => { body += d; if (body.length > 65536) req.destroy(); });
        res.on("end", () => {
          let json = null;
          try { json = JSON.parse(body); } catch (e) { /* 非 JSON（HTML 页等） */ }
          resolve({ status: res.statusCode, json, body: body.slice(0, 200) });
        });
      });
      req.on("timeout", () => { req.destroy(new Error("timeout")); });
      req.on("error", err => resolve({ status: 0, error: err.message }));
    });
  }

  /* ---------- 触发（每会话一次：idle 之外的态直接返回，重连不重复编排） ---------- */
  async trigger() {
    if (!this.enabled) {
      this.state = "manual"; this.note = "disabled";
      return { state: this.state, note: this.note };
    }
    if (this.state !== "idle") return { state: this.state, note: this.note, already: true };

    this.state = "triggering";
    const res = await this._postJson(this.bootPath, { source: "qq-agent-desktop", ts: new Date().toISOString() }, this.timeoutMs);
    this.httpStatus = res.status;

    if (res.error || res.status === 0) return this._manual("unreachable");          // 后端没起来/网络问题
    if (res.status === 404 || res.status === 405) return this._manual("endpoint-missing"); // 旧后端：端点未实现
    if (!(res.status >= 200 && res.status < 300)) return this._manual(`http-${res.status}`);
    if (res.json && typeof res.json === "object" && res.json.ok === false) return this._manual("refused");

    /* 触发成功 → 进度观测 */
    this.state = "watching";
    this.note = "triggered";
    this.log(`[boot] 已触发后端联动启动（POST ${this.bootPath} → ${res.status}）——轮询 ${this.programsPath} 观测编排进度`);
    this._emit({ type: "triggered", httpStatus: res.status, running: 0, total: 0 });
    this._startWatching();
    return { state: this.state, note: this.note, httpStatus: res.status };
  }

  _manual(note) {
    this.state = "manual";
    this.note = note;
    this.log(`[boot] 联动启动未启用（${note}${this.httpStatus ? ` · HTTP ${this.httpStatus}` : ""}）——静默降级为控制台程序页手动按钮`);
    this._emit({ type: "manual", note, httpStatus: this.httpStatus, running: 0, total: 0 });
    return { state: this.state, note, httpStatus: this.httpStatus };
  }

  /* ---------- 进度观测（GET /programs 快照差分 → program.* 主题同形态事件） ---------- */
  _startWatching() {
    this._watchT0 = Date.now();
    this._settle = 0;
    this._pollFails = 0;
    const tick = async () => {
      if (this.state !== "watching") return;
      if (Date.now() - this._watchT0 >= this.watchTimeoutMs) return this._finish("watchdog");
      const res = await this._getJson(this.programsPath, this.pollTimeoutMs);
      if (this.state !== "watching") return;
      const list = this._programsFrom(res);
      if (res.error || res.status === 0) {
        /* 网络层失败（后端掉线）→ 连续 pollFails 拍后静默收口（后端巡检另有通知，不刷屏） */
        this._pollFails += 1;
        if (this._pollFails >= 5) return this._finish("backend-unreachable");
      } else if (list === null) {
        /* 404 / 非 JSON / 形态不符 → 按「空观测」处理（settle 递增静默收口，不报错） */
        this._pollFails = 0;
        this._settle += 1;
      } else {
        this._pollFails = 0;
        this._diffList(list);
        const transient = list.some(x => this._isTransient(x));
        this._settle = transient ? 0 : this._settle + 1;
        this._refreshCounts(list);
      }
      if (this._settle >= this.settlePolls && (this._sawChange || Date.now() - this._watchT0 >= this.minWatchMs)) {
        return this._finish("settled");
      }
    };
    this._timer = setInterval(() => { tick().catch(e => this.log(`[boot] 轮询异常（忽略）: ${e.message}`)); }, this.pollIntervalMs);
    tick().catch(() => {});
  }

  /* /programs 响应形态：数组 | {programs:[…]} | {ok:…,programs:[…]}；其余（含网络层失败）→ null */
  _programsFrom(res) {
    if (!res || res.error || res.status === 0) return null;
    if (res.json && Array.isArray(res.json)) return res.json;
    if (res.json && Array.isArray(res.json.programs)) return res.json.programs;
    return null;   // 404 / 非 JSON / 形态不符：按「空观测」处理（settle 递增），不报错
  }

  _isTransient(x) {
    if (!x || typeof x !== "object") return false;
    const st = String(x.status || "").toLowerCase();
    if (TRANSIENT_STATUSES.includes(st)) return true;
    if (x.busy_action != null && x.busy_action !== "") return true;
    if (x.progress && x.progress.done === false) return true;
    return false;
  }

  _snap(x) {
    return {
      status: x.status == null ? "" : String(x.status),
      pid: x.pid == null ? null : x.pid,
      name: x.name == null ? "" : String(x.name),
      webui: x.webui == null ? null : x.webui,
      progressObj: x.progress && typeof x.progress === "object" ? x.progress : null,
      progressJson: JSON.stringify(x.progress || null),
    };
  }

  _diffList(list) {
    const now = new Map();
    for (const x of list) {
      if (!x || typeof x !== "object" || x.id == null) continue;
      now.set(String(x.id), this._snap(x));
    }
    if (this._last === null) { this._last = now; return; }   // 首拍建基线（不派发）
    for (const [id, cur] of now) {
      const prev = this._last.get(id);
      const statusChanged = !prev || prev.status !== cur.status || prev.pid !== cur.pid;
      if (statusChanged) {
        this._sawChange = true;
        this.statusEvents += 1;
        this._emit({ type: "status", id, name: cur.name, status: cur.status, pid: cur.pid, webui: cur.webui,
          prevStatus: prev ? prev.status : "(new)", running: this.runningCount, total: this.totalCount });   // topic program.status 形态
      }
      if (!prev || prev.progressJson !== cur.progressJson) {
        if (prev && prev.progressObj || cur.progressObj) {
          this._sawChange = true;
          this.progressEvents += 1;
          this._emit(Object.assign({ type: "progress", id, name: cur.name || (prev && prev.name) || "",
            running: this.runningCount, total: this.totalCount }, cur.progressObj || { cleared: true }));     // topic program.progress 形态
        }
      }
    }
    this._last = now;
  }

  _refreshCounts(list) {
    this.totalCount = list.length;
    this.runningCount = list.filter(x => x && String(x.status || "").toLowerCase() === "running").length;
  }

  _finish(note) {
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
    this.state = "done";
    this.note = note;
    const changed = this.statusEvents;
    this._emit({ type: "done", note, changed, running: this.runningCount, total: this.totalCount });
    this.log(`[boot] 联动启动观测收口（${note}）：变化 ${changed} 条 / 运行 ${this.runningCount}/${this.totalCount}`);
  }

  _emit(e) {
    try { this.onEvent(e); } catch (err) { /* 观察者异常不阻断观测 */ }
    this.events.push(Object.assign({ at: Date.now() }, e));
    if (this.events.length > 40) this.events.shift();
  }

  /* ---------- 停止（退出/冒烟收口：只清计时器，state 留痕） ---------- */
  stop() {
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
    if (this.state === "watching") this.state = "stopped";
  }

  /* 冒烟/单测摘要 */
  summary() {
    return {
      state: this.state, note: this.note, httpStatus: this.httpStatus,
      statusEvents: this.statusEvents, progressEvents: this.progressEvents,
      running: this.runningCount, total: this.totalCount,
      recent: this.events.slice(-6),
    };
  }
}

module.exports = { BootOrchestrator, TRANSIENT_STATUSES, FAILING_STATUSES };
