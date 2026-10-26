/* ============================================================
   scripts/sync-console.js — 重建内置 console/ 副本（演示/回退用，勿改源仓库文件）
   ------------------------------------------------------------
   v0.2.0 入库适配：壳位于仓库 desktop/ 子目录时，演示模式运行时直接引用
   仓库根 console/（单一副本，见 main.js getConsoleSource）——内置 console/
   仅为独立分发 / 打包 exe 场景的回退副本，来源自动探测：
     1) 壳位于仓库内（<repo>/desktop）→ 同步自 <repo>/console（推荐入口：
        仓库 console 更新后随手刷新，防内置副本陈旧）；
     2) 开发工作台（shared/qq-agent-console 源仓库）→ 同步自该源；
     3) 命令行手动指定：node scripts/sync-console.js <console源目录>。
   排除 .git / node_modules；同步后提交本仓库（仅独立分发需要）。
   ============================================================ */
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const DST = path.join(ROOT, "console");

const CANDIDATES = [
  { label: "仓库根 console/（壳位于仓库 desktop/ 内）", dir: path.join(ROOT, "..", "console") },
  { label: "工作台源仓库 qq-agent-console", dir: path.join(ROOT, "..", "qq-agent-console") },
];

const argSrc = process.argv[2];
const SRC = argSrc
  ? path.resolve(argSrc)
  : (CANDIDATES.find(c => fs.existsSync(path.join(c.dir, "index.html"))) || {}).dir;

if (!SRC || !fs.existsSync(path.join(SRC, "index.html"))) {
  console.error("未找到控制台源目录（含 index.html）。");
  console.error("用法：node scripts/sync-console.js [console源目录]");
  console.error("自动探测顺序：");
  CANDIDATES.forEach(c => console.error(`  · ${c.label}：${c.dir}`));
  process.exit(1);
}
if (path.resolve(SRC) === path.resolve(DST)) {
  console.error("源目录与目标相同，无需同步：" + SRC);
  process.exit(0);
}

fs.rmSync(DST, { recursive: true, force: true });
fs.cpSync(SRC, DST, {
  recursive: true,
  filter: p => {
    const rel = path.relative(SRC, p);
    if (rel.startsWith(".git") || rel.split(path.sep).includes("node_modules")) return false;
    return true;
  },
});
const files = [];
(function walk(d) {
  fs.readdirSync(d).forEach(f => {
    const p = path.join(d, f);
    fs.statSync(p).isDirectory() ? walk(p) : files.push(path.relative(DST, p));
  });
})(DST);
console.log(`source: ${SRC}`);
console.log(`synced ${files.length} files → console/`);
console.log(files.slice(0, 8).join("\n") + (files.length > 8 ? "\n…" : ""));
