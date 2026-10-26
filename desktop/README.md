# QQ Agent 桌面壳（Electron）· ME_Agent 仓库 `desktop/` 子目录

> 原生窗口加载 QQ Agent 中央控制台 · 托盘常驻（关闭到托盘）· 仓库根**自动推导** · **仓库根「启动桌面客户端.bat」双击即达（v0.3.0）** · **后端就绪联动启动第三方程序编排（POST /system/boot，404 静默降级）** · 全链路无 cmd 窗口 · 后端生命周期（双探附加/拉起/锁占提示/可选带走）· 无后端演示模式 · 首启引导 · 原生通知 · 开机自启（可选）

契约对齐《ME_Agent桌面壳对接契约_v1.md》（基线 8140412）。自 **v0.2.0（入库适配）** 起本壳以 **ME_Agent 仓库 `desktop/` 子目录**为标准形态：启动时自动识别上级为仓库根（免手填）；演示模式**优先引用仓库根 `console/`**（仓库内单一副本，杜绝双份漂移）。也支持脱离仓库独立运行（手动配置仓库根 + 内置 console 副本回退）。

## 目录定位

```
ME_Agent/                        ← 仓库根（自动推导目标：含 app/、.venv/、console/、deploy/ 等特征）
├── 启动桌面客户端.bat            ← v0.3.0 双击即达入口（落在仓库根，勿放 desktop\ 内——路径按 .\desktop\… 相对引用）
├── app/                         ← 后端（.venv\Scripts\python.exe -m app）
├── console/                     ← 控制台 v5.1（Live 由后端服务；演示模式由壳直接引用）
├── .venv/                       ← 首次部署产物（启动ME_Agent.bat 自动创建）
└── desktop/                     ← 本壳（即本目录）
    ├── main.js / preload.js / backend-manager.js / boot-orchestrator.js / config.js / setup.html
    ├── 启动桌面客户端.bat         ← 交付暂存位置（入库同步时上移到仓库根，见下节）
    ├── console/                 ← 内置副本（独立分发回退用；入库同步可排除，见下文）
    └── test/ scripts/ icons/
```

## 双击即达：启动桌面客户端.bat（v0.3.0 · 落仓库根）

**落位说明**：本仓库（qq-agent-desktop）内交付的 `启动桌面客户端.bat` 是按 **ME_Agent 仓库根目录落位形态**编写的——同步进 ME_Agent 时放到**仓库根**（`desktop\` 的上级），**勿放 `desktop\` 内**（bat 内所有路径按 `.\desktop\…` 相对引用，位置错了找不到运行时）。内容 ASCII-only + CRLF（cmd 兼容三件套，静态门禁测试把关）。

双击行为（成功路径全程无 cmd 窗口、无 pause）：

1. **优先找打包产物**：`desktop\dist\QQAgentDesktop.exe`（直接路径），同时匹配 `desktop\dist\*-win32-x64\QQAgentDesktop.exe`（electron-packager 默认子目录形态）→ `start "" /b` 直启（自包含 exe，清除 `ELECTRON_RUN_AS_NODE` 防宿主泄漏致 Node 模式）；
2. **回退 dev 运行时**：`desktop\node_modules\electron\dist\electron.exe` + app 目录 `desktop\.` → 同样 `start "" /b`；
3. **两者皆缺** → 打印修复指引（`cd desktop && npm install` 装 Electron dev 运行时，或 `npm run package-win` 打 exe）后 `pause`（唯一 pause 点，方便看指引）→ `exit /b 1`；成功路径 `exit /b 0` 即时退出，不留窗口。

## 快速开始（37 · Windows 实机 · 仓库内形态）

前置：安装 [Node.js LTS](https://nodejs.org/)（≥18）；ME_Agent 已做过**首次部署**（跑过一次 `deploy\scripts\启动ME_Agent.bat`，仓库根存在 `.venv`）。

```bat
cd desktop
npm install        :: 首次（下载 Electron，约 2-5 分钟）
npm start          :: 启动桌面壳
```

### 首次启动行为（自动推导 → 引导 → 演示降级）

1. **仓库根自动推导**（v0.2.0）：壳位于仓库 `desktop/` 子目录 → 自动以 `desktop\..` 为仓库根候选，按 `.venv\Scripts\python.exe`（Win）/ `.venv/bin/python`（posix）、`app\__main__.py` 等**仓库特征**判别（≥2 项且含强特征才认定，防误认任意父目录）；
   - 推导成功且 `.venv` 就绪 → **跳过引导窗，直接拉起/附加后端进 Live**（后端已运行则直接附加，全程无感）；
   - 推导成功但缺 `.venv`（未跑首次部署）→ 引导窗**预填仓库路径** + `.bat` 部署指引；
   - 推导未命中且无配置（如独立目录运行）→ 引导窗手填仓库根；
2. **引导窗**：填仓库根 → 「保存并拉起后端」；或「先进入演示模式（Mock）」；
3. **演示降级**：锁占/端口占等场景明确提示后进演示模式（见下文决策表）。

**手动覆盖**：`config.local.json` 的 `repoDir`（或引导窗 / 托盘「仓库与后端配置…」）**恒优先**于自动推导——改动路径后重启壳即生效；删除该字段即恢复自动推导。配置落 `config.local.json`（gitignore，不影响仓库默认值）。

## Windows 打包（免装 Node 的分发形态）

```bat
npm run package-win
```

产物：`dist\QQAgentDesktop-win32-x64\QQAgentDesktop.exe`（整个文件夹拷走即用；`config.json`/`config.local.json` 在同级目录按机器配置）。

- 打包产物**独立运行**（不依赖仓库）：Live 直连后端，演示走内置 console 副本；
- 打包产物内 `resources\app` 的上级无仓库特征 → 自动推导**不适用**，首启走引导窗手填（预期行为）；
- 若入库同步时排除了 `desktop/console/`，打包前先 `npm run sync-console`（自动从仓库根 `console/` 重建内置副本，见下节）。

## 演示模式与控制台来源（v0.2.0）

| 优先级 | 来源 | 触发条件与说明 |
|---|---|---|
| 1 | **仓库根 `console/`** | repoDir 已知（自动推导/显式配置）且仓库存在 `console/` → 演示模式与 app:// 回退态直接引用（**仓库单一副本随 git pull 更新，杜绝 desktop/ 与仓库双份漂移**） |
| 2 | 内置 `desktop/console/` 副本 | 独立分发 / 打包 exe / 仓库无 `console/` 时回退；v5.1 纯静态页，与仓库版同一 CSP 口径注入 |
| 3 | 均不存在 | 演示模式展示重建指引页（`npm run sync-console`） |

- **副本刷新**：`node scripts/sync-console.js` 自动探测源（仓库根 `console/` → 开发工作台 `../qq-agent-console` → 命令行指定目录），排除 `.git`/`node_modules`；
- `preferRepoConsole: false`（config.json）可禁用仓库引用强制走内置副本（仓库 console 异常时逃生舱）；
- **Live 模式不受影响**：始终直接加载 `{backendUrl}/console/index.html`（契约 §二，console 随仓库走，壳与后端解耦）。

## 入库同步说明（面向仓库维护者）

- 本目录整体同步进 ME_Agent 仓库 `desktop/` 即可运行，无额外配置（`.gitignore` 已含 `node_modules/`、`dist/`、`config.local.json`——用户本地状态与依赖不入库）；
- **建议同步时排除 `desktop/console/`**：演示模式运行时引用仓库根 `console/`，仓库内无需第二份；排除后需要独立分发/打包时由 `npm run sync-console` 一键重建；
- 后端契约变更 → 只改 `config.json` 接缝（见下表）；壳自身升级与后端升级解耦（壳只依赖契约）。

## 启动行为（契约 §三/§四 · 托盘与生命周期要点）

双探决策：
- **就绪**：轮询 `GET /api/v1/system/health`（`{"ok":true}`）；
- **判别**：`GET /api/v1/system/status` 含 ME_Agent 特征字段（memory/gateway/programs/tts/llm_enabled/started_at，命中 ≥2）——**防无关程序占端口误附加**；
- 均通过 → **附加现有实例**（不拉新）→ 主窗口直接 `loadURL {backendUrl}/console/index.html` 进入 **Live 模式**；
- 未就绪且仓库根已知 → **拉起**：`<仓库根>\.venv\Scripts\python.exe -m app --host 127.0.0.1`（cwd 显式 = 仓库根；等待转健康 60s 覆盖首次启动余量）；
- health ok 但判别不过 → **端口被无关程序占用**：明确提示 + 演示模式降级，不误附加；
- 拉起秒退且 stderr 含 `InstanceLockError` → **实例锁被占**（已有实例在别的端口/host）：明确提示勿重试，壳不再拉新；
- 仓库根已知但缺 `.venv` → 引导窗预填 + `.bat` 部署指引；无后端无配置 → 引导窗手填 → 演示模式。

**关壳默认保持后端运行**（契约 §四.4）；托盘「退出时带走后端进程」可切换（仅带走本壳拉起的实例，附加实例一律保留）。

## 联动启动（v0.3.0 · 第三方程序自动编排触发）

后端就绪（附加或拉起）后，壳**自动** `POST /api/v1/system/boot` 触发后端对第三方程序（SnowLuma 等）的自动编排，并在托盘/原生通知中体现进度：

- **触发时机**：`bootFromDecision` 判定 Live（attached/spawned）后触发一次（每会话一次，重连/引导不重复编排）；
- **静默降级**：端点 404/405（后端尚未实现——正常旧后端形态）、网络错误、`ok:false` → **完全静默**（一次性日志，不报错不刷屏），控制台「第三方程序」页**手动按钮照常可用**——降级即回到 v0.2.0 行为；
- **进度观测**：轮询 `GET /api/v1/programs`（1.2s/拍，与控制台 busy 语义轮询同拍）做快照差分，派发**既有 `program.*` 推送主题同形态**事件（`program.status`：`{id,status,pid,webui}`；`program.progress`：`{id,phase,percent,…}`）——复用主题 payload 形态而非另造协议；纯 HTTP 快照差分（主进程无 WS 客户端依赖、零新增 npm 依赖）；
- **托盘/通知**：程序进入 `running` → 原生通知「联动启动：X 已运行」；`error/failed/crash` → 异常通知；progress 更新**只刷 tooltip**（防刷屏）；收口（连续拍无 busy/暂态或 watchdog）→ 有变化才发汇总通知 + tooltip 复位；托盘菜单出现「联动启动：进行中/完成」状态行（manual 降级时不占菜单）；
- **收口防早退**：连续 3 拍无暂态 **且**（观测到过变化 或 已观测 ≥8s）才收口——防后端尚未翻转状态时提前判定完成；watchdog 240s 兜底。

## 全链路窗口自查（v0.3.0 · 无 cmd 窗口）

| 链路环节 | 自查结论 |
|---|---|
| bat → 运行时 | `start "" /b`（无新控制台）；成功路径不 `pause`、`exit /b 0` 即时退出（机制参考 K0nd1us `启动QQ机器人.bat`） |
| Electron 本体 | `electron.exe` / `QQAgentDesktop.exe` 均为 GUI 子系统进程，自身不分配控制台 |
| 壳 → 后端 spawn | `backend-manager.js`：`spawn(cmd, args, { windowsHide: true })`，`shell:false`（不经 cmd 包装）、stdio 全 pipe——Windows 上 python.exe 无窗口闪现 |
| 壳 → 联动启动 | `boot-orchestrator.js` **全 HTTP，零 spawn**——窗口自查链路零新增暴露点 |
| 后端 → SnowLuma spawn | 后端侧实现（随 `/system/boot` 端点同轮交付）：Python 侧须 `creationflags=CREATE_NO_WINDOW`（Windows）——已列为全栈开发端点验收项 |

## 后端对接配置（config.json，契约 v1 校准值）

| 键 | 默认 | 说明 |
|---|---|---|
| `backendUrl` | `http://127.0.0.1:8000` | 本机后端地址（契约 §三） |
| `healthPath` / `statusPath` | `/api/v1/system/health` / `/api/v1/system/status` | 就绪轮询 / ME_Agent 特征判别端点 |
| `repoDir` | 空 | ME_Agent 仓库根。**解析链**：显式配置（config.json/config.local.json/引导窗，恒优先）> **自动推导**（desktop/ 上级特征判别）> 空（弹引导窗） |
| `preferRepoConsole` | `true` | 演示/回退态优先引用仓库根 `console/`（false 强制内置副本） |
| `backendStartCommand` | 空 | 显式拉起命令（优先于 repoDir 推导；契约 §一：`.venv\Scripts\python.exe -m app --host 127.0.0.1`） |
| `backendStartCwd` | 空 | 拉起工作目录（默认 = repoDir，契约要求显式仓库根） |
| `spawnTimeoutMs` | `60000` | 就绪等待（覆盖首次 venv/依赖安装余量） |
| `spawnArgs` | `["--host","127.0.0.1"]` | 拉起附加参数（§六：收缩暴露面仅本机监听） |
| `consoleFromBackend` | `true` | Live 直连后端 `/console/index.html`（§二；false 回退 app:// 本地 + 注入） |
| `bootEnabled` | `true` | v0.3.0 联动启动开关（false = 完全回到 v0.2.0 行为） |
| `bootPath` | `/api/v1/system/boot` | 编排触发端点（404/未实现 → 静默降级控制台手动按钮） |
| `bootProgramsPath` | `/api/v1/programs` | 进度观测快照端点（program.* 主题同形态差分） |
| `bootPollIntervalMs` / `bootWatchTimeoutMs` | `1200` / `240000` | 观测轮询拍频（与控制台同拍）/ watchdog 兜底 |
| `takeBackendDownOnExit` | `false` | 退出带走后端（§四.4 默认保持运行；托盘可切换） |
| `openAtLogin` / `startMinimized` | `false` | 开机自启 / 启动到托盘（托盘可切换） |

托盘菜单：显示窗口 / 后端状态 / **联动启动状态行（v0.3.0，仅实际编排时出现）** / 重连后端（Live）/ 切换演示模式 / 打开后端页面 / **仓库与后端配置…**（手动覆盖入口）/ 开机自启 / 退出时带走后端 / 退出。窗口关闭按钮 = 最小化到托盘（真正退出走托盘「退出」）。

## 开发与验证（本仓库已做的沙箱验证）

```bash
node scripts/gen-icon.js                    # 重新生成图标（纯 Node PNG 编码）
node scripts/sync-console.js                # 重建/刷新内置 console 副本（源自动探测）
npm test                                    # 单测全跑：51 断言（backend-manager 16 · boot-orchestrator 18/7 场景 · bat 静态门禁 17）
node test/test-backend-manager.js           # 生命周期单测 16 断言（附加/端口占/演示/拉起/锁占/带走 + v0.2.0 推导/优先级链/命令构造）
node test/test-boot-orchestrator.js         # v0.3.0 联动启动单测 7 场景 18 断言（404/不可达降级 · 触发观测 · 主题同形态 · 旧后端收口 · stop · 开关）
node test/test-launcher-bat.js              # v0.3.0 双击入口静态门禁 17 断言（ASCII/CRLF/无 BOM · 仓库根相对路径 · start /b · pause 唯一 · README 落位同步）
npm run smoke:demo                          # 无头冒烟：演示模式（自动推导 + console 来源落位快照）
npm run smoke                               # 无头冒烟：真实双探 + 拉起 + Live 直连
npm run smoke:live                          # 无头冒烟：强制附加态（配合 dummy 后端）
```

冒烟输出 `SMOKE_RESULT {…}`（JSON）：页面加载/JS 错误/后端决策/页面内 `API.mode`·`baseUrl` 落位 + **`env` 快照（repoDir 推导链 + consoleSource 落位）** + **`boot` 快照（v0.3.0：联动启动 state/httpStatus/事件计数——trigger→watch→done 或 manual 降级形态）**；非零退出码 = 有错误。Linux 无显示环境需 Xvfb（容器内注意：xvfb-run 缺 xauth 时直接 `Xvfb :99 & DISPLAY=:99`；Electron 需 `--no-sandbox --no-zygote`，`--smoke` 已自动附带）。

v0.2.0 回归口径：单测 16/16 + xvfb 六场景冒烟（独立 demo/live + 仓库内 demo / 全流程拉起 live / 仅仓库 console / 仓库无 console 回退内置）零 JS 错误零退出码非零。

v0.3.0 回归口径：单测 51 断言全过 + 同六场景冒烟全绿，live 场景 `boot` 快照为 `done(settled)` 且含 ≥1 个 status 事件（dummy 后端 /system/boot + /programs 编排仿真；`DUMMY_NO_BOOT=1` 可仿真 404 降级为 manual）。

## 架构

```
main.js               主进程：窗口（Live loadURL 直连 / demo app://）/ console 来源动态解析（仓库优先）/ 首启引导窗/托盘/通知/单实例锁/联动启动事件→托盘·通知/冒烟
preload.js            渲染层接缝：live 注入（localStorage 通道 + 自定义端口 API 校准）+ window.desktop / setup IPC
backend-manager.js    生命周期：双探（health+status 判别）→ attach/spawn（repoDir 推导命令，windowsHide）/lock-held/port-occupied + deriveRepoDir 仓库根推导；纯 Node 可单测
boot-orchestrator.js  v0.3.0 联动启动：POST /system/boot 触发编排（404 等全形态静默降级）+ /programs 快照差分派发 program.* 主题同形态事件 + 收口判定；全 HTTP 零 spawn，纯 Node 可单测
config.js             配置装载：defaults ← config.json ← config.local.json（引导窗/托盘落盘）+ repoDir 解析优先级链
setup.html            引导页（仓库根配置 → 拉起 / 演示；自动推导预填 + 手动覆盖口径）
console/              控制台 v5.1 内置副本（独立分发回退；入库后演示运行时引用仓库根 console/，见「演示模式与控制台来源」）
scripts/              gen-icon / sync-console（源自动探测）
test/                 test-backend-manager（16 用例）· test-boot-orchestrator（v0.3.0 联动启动 7 用例）· test-launcher-bat（v0.3.0 双击入口静态门禁）· dummy-backend（契约 v1 + v0.3.0 boot/programs 编排仿真假后端）· lock-exit（锁占模拟）
```

**契约接缝**：后端地址/双探端点/拉起命令全部收敛在 `config.json`——后续契约变更改配置即可，无需改码。控制台侧如需配合改动（window.desktop API 等），走「发群提需求 → 统一处理」流程，本轮控制台零改动。

**安全说明**：`contextIsolation` 始终开启、`nodeIntegration` 始终关闭、`webSecurity` 始终保持开启（Live 与后端同源、后端 CORS 全开，无需关闭）；app:// 本地 console（含仓库 console 引用态）由壳层注入 CSP（响应头+meta）；锁占/端口占仅提示不自动干预（防误杀实例）。
