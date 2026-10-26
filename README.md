# ME_Agent · QQ Agent

> 一个住在 QQ 里的数字生命体：世界心跳、记忆、插件技能与语音，由中央控制台统一管理。
> 前身 37QAG 的全部遗留内容已于 2026-10-25 清除，本仓库是其全新起点（详见 `docs/`）。

## 架构（五层）

```
L0  SnowLuma        OneBot v11 协议端（QQ 号容器），反向 WebSocket 接入
L1  Console 核心    Python 单进程：gateway / dialogue / outbound / bus
L2  世界心跳 + 记忆  每 5 分钟一拍推进 WorldState；Hindsight 双 bank 记忆
L3  插件技能        plugins/*/skill.yaml manifest 声明 + 沙箱执行
L4  Obsidian        单向双通道导出（人类可读的记忆档案）
```

核心纪律：**CoherenceGuard 是世界状态的唯一合法写入者**，LLM 永不直写状态；
事件溯源 + 虚拟时钟压缩测试保证心跳可回放、可审计。

## 目录结构

| 目录 | 说明 |
|---|---|
| `app/` | 后端核心（Python）：bus / clock / config / core / dialogue / gateway / heartbeat / memory / outbound / plugins / programs / tts |
| `plugins/` | 插件技能：dice / qq_actions / reminder / translate / voice / weather / web_search |
| `config/` | YAML 配置 12 份：persona / routine / memory / console / shaping / programs / channels / dynamics / activities / locations / tts / llm |
| `console/` | 中央控制台前端 V2：纯静态单页（零构建、零运行时依赖），内置 Mock 演示模式 |
| `desktop/` | 桌面客户端（Electron 桌面壳 v0.2.0）：原生窗口加载控制台 · 托盘常驻 · 后端生命周期 |
| `tests/` | 测试（建设中） |
| `docs/` | 《QQ-Agent 实现方案》（928 行）+ 实施进度盘点 |
| `deploy/` | 部署说明：依赖安装、SnowLuma 接入、控制台托管 |

运行时产物（不入库）：`state/`（世界状态与事件溯源）、`vault/`（TTS Provider 档案等）。

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 填写 config/*.yaml（LLM、人设、通道等），敏感项走 .env 环境变量
# SnowLuma 以 OneBot v11 反向 WS 连入，access_token 见 config/channels.yaml
```

控制台前端可独立体验：浏览器直接打开 `console/index.html`（Mock 演示模式，
心跳 30 秒/拍演示；Live 模式待 `app/api/` HTTP 层就绪后接线）。

## 桌面客户端（desktop/，Electron 桌面壳 v0.2.0）

`desktop/` 是 ME_Agent 的原生桌面客户端（设计匠人开发，第 12 轮入库）：壳本身零业务逻辑——UI 完全复用 `console/`（Live 模式直连后端，演示模式引用仓库根 `console/`），数据全走后端 HTTP/WS，与《ME_Agent桌面壳对接契约_v1》对齐；契约接缝收敛在 `desktop/config.json`。

前置：安装 [Node.js LTS](https://nodejs.org/)（≥18）；仓库已做过首次部署（跑过一次 `deploy\scripts\启动ME_Agent.bat`，仓库根存在 `.venv`）。

```bat
cd desktop
npm install        :: 首次（下载 Electron，约 2-5 分钟）
npm start          :: 启动桌面壳
```

- **首启行为链（自动推导 → 引导 → 演示降级）**：壳位于仓库 `desktop/` 子目录，启动时按 `.venv`、`app\__main__.py` 等特征**自动识别上级为仓库根**（免手填）；推导成功且 venv 就绪 → 跳过引导直接拉起/附加后端进 Live；推导成功但缺 venv → 引导窗预填路径 + `.bat` 部署指引；无仓库特征 → 引导窗手填；不想连后端可进演示模式（Mock）。手动覆盖能力保留（托盘「仓库与后端配置…」写 `config.local.json`，恒优先于自动推导）。
- **托盘与后端生命周期**：窗口关闭 = 缩到托盘（后端保持运行），真正退出走托盘「退出」（可选带走后端，仅本壳拉起的实例）；后端双探（health 就绪 + status 特征判别，防无关程序占端口误附加）；实例锁占用（InstanceLockError）明确提示不重试。
- **Windows 免装 Node 分发**：`npm run package-win` → `dist\QQAgentDesktop-win32-x64\QQAgentDesktop.exe`（整文件夹拷走即用；产物独立运行，首启走引导窗手填仓库根，属预期）。
- 入库形态不含 `desktop/console/`（演示运行时引用仓库根 `console/`，单一副本杜绝漂移）；`node_modules/`、`dist/`、`config.local.json`（用户本地状态）不入库，由自带 `desktop/.gitignore` 覆盖。

细节（console 来源优先级、配置项表、冒烟口径）见 `desktop/README.md`。

## 当前状态（2026-10-25）

- ✅ 已实现：`app/` 后端 13 个子系统约 4355 行；7 个插件技能；12 份配置；控制台前端 V2（13 文件约 3100 行）。
- ⏳ 未实现：`app/api/` HTTP 服务层（控制台 Live 数据源）、总装启动入口、`tests/` 用例、SnowLuma 实机联调。

模块级明细见 [docs/实施进度.md](docs/实施进度.md)。

## License

MIT
