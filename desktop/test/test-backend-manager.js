/* ============================================================
   test/test-backend-manager.js — 后端生命周期单测（纯 Node，无需 Electron）
   ------------------------------------------------------------
   契约 v1 用例：
   1) 附加：ME_Agent dummy（health + status 特征字段）→ mode=attached（不 spawn）
   2) 端口占：health ok 但 status 无特征字段（FOREIGN dummy）→ mode=port-occupied（不误附加）
   3) 演示：无服务且无命令 → mode=demo
   4) 拉起：无服务 + startCommand 起 dummy → spawned → stop() 带走
   5) 锁占：startCommand 秒退 stderr 含 InstanceLockError → mode=lock-held（勿重试）
   6) 关壳默认保持：attached 实例 shutdown() 不带走
   7) v0.2.0 入库适配：仓库根自动推导（desktop/ 上级特征判别，强特征阈值防误认）
      + config 解析优先级链（显式 repoDir 恒优先 > 自动推导）+ 推导→契约命令构造
   断言全过输出 ALL PASS，退出码 0；任一失败退出码 1。
   ============================================================ */
"use strict";

const http = require("http");
const { spawn } = require("child_process");
const path = require("path");

const { BackendManager } = require("../backend-manager");

function dummyServer({ foreign = false } = {}) {
  return new Promise(resolve => {
    const srv = http.createServer((req, res) => {
      const send = obj => { res.writeHead(200, { "Content-Type": "application/json" }); res.end(JSON.stringify(obj)); };
      if (req.url === "/api/v1/system/health") return send({ ok: true, ts: Date.now() });
      if (req.url === "/api/v1/system/status") return send(foreign ? { app: "other" } : { memory: {}, gateway: {}, programs: {}, tts: {}, llm_enabled: false, started_at: new Date().toISOString() });
      return send({ ok: true, path: req.url });
    });
    srv.listen(0, "127.0.0.1", () => resolve({ srv, port: srv.address().port }));
  });
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

let failed = 0;
function assert(name, ok, extra) {
  console.log((ok ? "PASS  " : "FAIL  ") + name + (ok ? "" : " → " + JSON.stringify(extra)));
  if (!ok) failed++;
}

(async () => {
  /* 1) 附加：ME_Agent 特征判别通过 */
  {
    const { srv, port } = await dummyServer();
    const bm = new BackendManager({ url: `http://127.0.0.1:${port}`, healthPath: "/api/v1/system/health", statusPath: "/api/v1/system/status", log: () => {} });
    const r = await bm.ensureRunning();
    assert("attached：health+status 判别通过 → 直接附加", r.mode === "attached" && r.probe.isMeAgent === true, r);
    const took = await bm.shutdown();
    assert("attached：关壳默认保持（不带走附加实例）", took === false);
    srv.close();
  }

  /* 2) 端口占：无关程序响应（health ok，status 无特征字段） */
  {
    const { srv, port } = await dummyServer({ foreign: true });
    const bm = new BackendManager({ url: `http://127.0.0.1:${port}`, healthPath: "/api/v1/system/health", statusPath: "/api/v1/system/status", startCommand: "echo should-not-spawn", log: () => {} });
    const r = await bm.ensureRunning();
    assert("port-occupied：非 ME_Agent 服务 → 不附加不拉起", r.mode === "port-occupied" && !!r.message, r);
    assert("port-occupied：未 spawn", !bm.ownsProcess());
    srv.close();
  }

  /* 3) 演示：无服务无命令 */
  {
    const bm = new BackendManager({ url: "http://127.0.0.1:59999", healthPath: "/api/v1/system/health", statusPath: "/api/v1/system/status", log: () => {} });
    const r = await bm.ensureRunning({ spawnTimeoutMs: 2000 });
    assert("demo：无后端无命令 → 演示模式", r.mode === "demo", r);
  }

  /* 4) 拉起 + 带走 */
  {
    const script = path.join(__dirname, "dummy-backend.js");
    const bm = new BackendManager({
      url: "http://127.0.0.1:59998",
      healthPath: "/api/v1/system/health", statusPath: "/api/v1/system/status",
      startCommand: `node ${JSON.stringify(script)}`,
      startCwd: __dirname,
      spawnEnv: { DUMMY_PORT: "59998" },
      takeBackendDownOnExit: true,
      log: () => {},
    });
    const r = await bm.ensureRunning({ spawnTimeoutMs: 12000 });
    assert("spawned：探测失败 → 按命令拉起并转健康+判别通过", r.mode === "spawned" && r.probe.ok === true, r);
    assert("spawned：实例归属本壳", bm.ownsProcess() === true);
    const took = await bm.shutdown();
    await sleep(300);
    const after = await bm.probe(800);
    assert("shutdown：带走后端进程（端口失联）", took === true && after.ok === false, after);
  }

  /* 5) 锁占：秒退 + stderr 含 InstanceLockError */
  {
    const script = path.join(__dirname, "lock-exit.js");
    const bm = new BackendManager({
      url: "http://127.0.0.1:59997",
      healthPath: "/api/v1/system/health", statusPath: "/api/v1/system/status",
      startCommand: `node ${JSON.stringify(script)}`,
      log: () => {},
    });
    const r = await bm.ensureRunning({ spawnTimeoutMs: 8000 });
    assert("lock-held：InstanceLockError 秒退 → 明确锁占提示", r.mode === "lock-held" && /InstanceLockError/.test(r.message || ""), r);
    assert("lock-held：未留孤儿进程", !bm.ownsProcess());
  }

  /* 6) v0.2.0 入库适配：仓库根自动推导 + repoDir 解析优先级链 */
  {
    const os = require("os");
    const fs = require("fs");
    const { deriveRepoDir, venvPythonPath } = require("../backend-manager");
    const config = require("../config");

    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "meagent-desktop-"));

    /* 6a) 完整仓库特征（.venv + app/__main__.py + console 等）→ 推导成功 */
    const repo = path.join(tmp, "ME_Agent");
    const desktop = path.join(repo, "desktop");
    fs.mkdirSync(path.join(repo, "app"), { recursive: true });
    fs.writeFileSync(path.join(repo, "app", "__main__.py"), "# dummy entry\n");
    fs.mkdirSync(path.dirname(venvPythonPath(repo)), { recursive: true });
    fs.writeFileSync(venvPythonPath(repo), "#!/bin/sh\n");
    fs.mkdirSync(path.join(repo, "console"), { recursive: true });
    fs.writeFileSync(path.join(repo, "console", "index.html"), "<html></html>");
    fs.writeFileSync(path.join(repo, "requirements.txt"), "");
    fs.mkdirSync(desktop, { recursive: true });
    const d = deriveRepoDir(desktop);
    assert("derive：desktop/ 上级含 .venv+app 特征 ≥2 → 推导为仓库根", d.ok === true && d.repoDir === repo && d.venvReady === true, d);

    /* 6b) 仅弱特征（console+requirements，无 app/.venv）→ 不误认任意父目录 */
    const weak = path.join(tmp, "weak-parent", "desktop");
    fs.mkdirSync(path.join(tmp, "weak-parent", "console"), { recursive: true });
    fs.writeFileSync(path.join(tmp, "weak-parent", "console", "index.html"), "");
    fs.writeFileSync(path.join(tmp, "weak-parent", "requirements.txt"), "");
    fs.mkdirSync(weak, { recursive: true });
    assert("derive：仅弱特征（无 app/.venv 强特征）→ 拒绝推导", deriveRepoDir(weak).ok === false);

    /* 6c) 上级零特征 → 不推导 */
    const empty = path.join(tmp, "empty-parent", "desktop");
    fs.mkdirSync(empty, { recursive: true });
    assert("derive：上级无仓库特征 → 不推导", deriveRepoDir(empty).ok === false);

    /* 6d) 显式 repoDir（config.local.json）恒优先于自动推导（手动覆盖能力） */
    fs.writeFileSync(path.join(desktop, "config.local.json"), JSON.stringify({ repoDir: "/explicit/repo" }));
    const cExplicit = config.load(desktop);
    assert("config：显式 repoDir 恒优先（repoDirSource=config）", cExplicit.repoDir === "/explicit/repo" && cExplicit.repoDirSource === "config", cExplicit);

    /* 6e) 无显式配置 → load() 自动推导（首启引导跳过手填） */
    fs.unlinkSync(path.join(desktop, "config.local.json"));
    const cAuto = config.load(desktop);
    assert("config：无显式配置 → 自动推导仓库根（repoDirSource=auto）", cAuto.repoDir === repo && cAuto.repoDirSource === "auto", cAuto);

    /* 6f) 推导出的 repoDir → 拉起命令按契约 §一 构造（cwd=仓库根） */
    const bm = new BackendManager({ repoDir: cAuto.repoDir, spawnArgs: ["--host", "127.0.0.1"], log: () => {} });
    const built = bm.buildStartCommand();
    assert("derive→命令：.venv python -m app --host 127.0.0.1（cwd=仓库根）",
      built.command === `"${venvPythonPath(repo)}" -m app --host 127.0.0.1` && built.cwd === repo, built);

    fs.rmSync(tmp, { recursive: true, force: true });
  }

  console.log(failed ? `\n${failed} FAILED` : "\nALL PASS");
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
