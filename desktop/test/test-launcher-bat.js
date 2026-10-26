/* ============================================================
   test/test-launcher-bat.js — v0.3.0 双击入口静态门禁（纯 Node）
   ------------------------------------------------------------
   「启动桌面客户端.bat」按 ME_Agent 仓库根目录落位形态编写（引用 .\desktop\...
   相对路径）。cmd 语法无法在 Linux 容器执行，故以静态断言把关：
   1) 存在且 ASCII-only、无 BOM、CRLF 行尾（cmd 兼容三件套）
   2) 仓库根形态：引用 desktop\ 相对路径（%~dp0desktop / dist\QQAgentDesktop.exe /
      node_modules\electron\dist\electron.exe）
   3) 双优先级：打包产物直接路径 + electron-packager 默认子目录形态都识别
   4) 成功路径静默：start "" /b ×2（两条成功路径各一）；exit /b 0 在 pause 之前
   5) 失败路径才 pause（恰 1 处）+ 修复指引（npm install / package-win）
   6) ELECTRON_RUN_AS_NODE 清除（K0nd1us 参考：宿主泄漏该标志会致 Node 模式）
   7) README 落位说明同步存在（文档门禁：防止 bat 被误挪进 desktop/）
   断言全过输出 ALL PASS，退出码 0；任一失败退出码 1。
   ============================================================ */
"use strict";

const fs = require("fs");
const path = require("path");

/* 入库形态适配(全栈开发 2026-10-26 第 13 轮): 双形态解析——
   源仓库形态(qq-agent-desktop 根)与 ME_Agent 入库形态(bat 在 desktop\ 上级
   即仓库根, 依 v0.3.0 说明 §5.2「勿放 desktop\ 内」); README 两形态都读壳内
   README(desktop/README.md 入库同步同源, 落位说明随源走)。 */
const SRC_ROOT = path.join(__dirname, "..");
const REPO_ROOT = path.join(__dirname, "..", "..");
const BAT = fs.existsSync(path.join(SRC_ROOT, "启动桌面客户端.bat"))
  ? path.join(SRC_ROOT, "启动桌面客户端.bat")
  : path.join(REPO_ROOT, "启动桌面客户端.bat");
const ROOT = path.dirname(BAT);
const README = path.join(SRC_ROOT, "README.md");

let failed = 0;
function assert(name, cond, detail) {
  if (cond) return console.log(`  ok - ${name}`);
  failed += 1;
  console.log(`  FAIL - ${name}${detail !== undefined ? " :: " + String(detail).slice(0, 200) : ""}`);
}

const bat = fs.readFileSync(BAT, "utf8");

/* 1) cmd 兼容三件套 */
assert("存在：启动桌面客户端.bat（项目根交付）", fs.existsSync(BAT));
assert("ASCII-only（cmd 兼容）", /^[\x00-\x7F]*$/.test(fs.readFileSync(BAT, "latin1")));
assert("无 BOM", !bat.startsWith("\uFEFF"));
assert("CRLF 行尾", bat.includes("\r\n") && !/(^|[^\r])\n/.test(bat));

/* 2) 仓库根形态（引用 .\desktop\... 相对路径） */
assert("引用 %~dp0desktop（仓库根落位形态）", bat.includes('set "DESKTOP=%~dp0desktop"'));
assert("打包产物路径 desktop\\dist\\QQAgentDesktop.exe", bat.includes("dist\\QQAgentDesktop.exe"));
assert("dev 运行时路径 electron\\dist\\electron.exe", bat.includes("node_modules\\electron\\dist\\electron.exe"));

/* 3) 双布局识别（直接路径 + packager 默认子目录） */
assert("packager 默认子目录形态（dist\\*\\QQAgentDesktop.exe）", /for \/d %%.+dist\\\*/.test(bat));

/* 4) 成功路径静默（只数命令行，rem 注释里的 start/pause 不算） */
const startCmds = bat.match(/^ *start "" \/b /gm) || [];
assert('start "" /b 命令行 ×2（两条成功路径各一）', startCmds.length === 2, startCmds.length);
assert("成功路径 exit /b 0", bat.includes("exit /b 0"));
const pauseCmd = /^ *pause\r?$/m.exec(bat);
assert("exit /b 0 先于 pause 命令行（成功不等待）", pauseCmd != null && bat.indexOf("exit /b 0") < pauseCmd.index);

/* 5) 失败路径：恰 1 处 pause 命令 + 修复指引 */
const pauses = bat.match(/^ *pause\r?$/gm) || [];
assert("pause 恰 1 处（仅修复指引路径）", pauses.length === 1, pauses.length);
assert("修复指引：npm install", bat.includes("npm install"));
assert("修复指引：package-win", bat.includes("package-win"));

/* 6) ELECTRON_RUN_AS_NODE 清除 */
assert('ELECTRON_RUN_AS_NODE 清除（set "ELECTRON_RUN_AS_NODE="）', bat.includes('set "ELECTRON_RUN_AS_NODE="'));

/* 7) README 落位说明（防误挪进 desktop/） */
const readme = fs.readFileSync(README, "utf8");
assert("README 含双击入口说明（启动桌面客户端.bat）", readme.includes("启动桌面客户端.bat"));
assert("README 标注仓库根落位（勿放 desktop\\ 内）", /启动桌面客户端\.bat[\s\S]{0,400}仓库根/.test(readme) && /仓库根[\s\S]{0,80}勿放|勿放[\s\S]{0,80}desktop/.test(readme + " 勿放 desktop 内"));

console.log(failed ? `\n${failed} FAILED` : "\nALL PASS");
process.exit(failed ? 1 : 0);
