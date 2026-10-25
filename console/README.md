# QQ Agent 中央控制台 — 前端

> 世界心跳 · 对话流 · 插件技能 · 第三方程序 · TTS 语音管理 · 诊断导出（脱敏）
> 纯静态单页应用（零构建 / 零运行时依赖），`index.html` 双击即用。

## 运行

- **直接打开**：浏览器打开 `index.html`（file:// 协议即可，无外部依赖；Google Fonts 加载失败时自动回退系统字体）。
- **本地服务**：`python3 -m http.server 8000` → http://localhost:8000
- 默认 **Mock 演示模式**：内置演示数据 + 世界心跳模拟引擎（演示 30 秒/拍，真实系统 5 分钟/拍）。

## 页面模块

| 路由 | 模块 | 说明 |
|---|---|---|
| `#/` | 总览 · 世界心跳 | WorldState 实时卡片（时间/天气/心情二维图/疲劳/所在地/活动/意图/想要），状态叙事预览、转移时间线，字段完全对齐《实现方案》§2.3 |
| `#/chat` | 对话流 | 会话列表 + 消息流（P0–P4 事件分级、burst 合并、语音消息标识），支持发送测试消息 |
| `#/plugins` | 插件技能 | passive/active 插件卡、启停开关、触发器与权限、skill.yaml manifest 预览 |
| `#/programs` | 第三方程序 | 外部自管为主（健康探测/进程监控/日志尾部），一键接入为可选增强（卡片按 actions 自动推导模式徽标）；下载/更新/启停/重启、进程状态（PID/内存/CPU/运行时长） |
| `#/tts` | TTS 语音 · API 管理 | 见下节 |
| `#/settings` | 设置 | Mock/Live 切换、后端地址（对接总开关）、诊断导出（脱敏） |

## TTS 模块（按 37 需求 10-25 10:11 原文实现）

**五步流程**：①接口地址+密钥（默认小米 `POST /v1/chat/completions`，兼容 OpenAI `/v1/audio/speech` 风格）→ ②拉取模型列表（同源 `GET /v1/models`）并选择 → ③按模型类型差异化配置 → ④保存与检查 → ⑤试合成试听。

**三类模型差异化**：

1. **文字提示语音**（如 `mimo-v2.5-tts-voicedesign`、`gpt-4o-mini-tts`）
   → 请求头/请求体**双编辑器**：两段原文**独立编辑、独立保存**；发送时**仅占位符替换**，其余一字不动。
   - **字节保真承诺**：不自动补请求头（如 Content-Type）、不做 JSON 重序列化、不做 pretty-print、不加任何前后缀；落盘与编辑往返字节一致（「往返校验」按钮 UTF-8 逐字节比对）。
   - 占位符：`{{api_key}}` `{{model}}` `{{text}}` `{{voice}}` `{{style}}` `{{endpoint}}`（未知占位符原样发送）。
   - MiMo 协议要点已内置默认模板：合成文本放 `role=assistant`，user 放风格指令；音频在响应 `choices[0].message.audio.data`（base64）。
2. **预设音色**（如 `mimo-v2.5-tts`）→ 官方音色列表（**中文名**：冰糖/茉莉/苏打/白桦/Mia… 共 9 个）+ 逐个试听。
3. **语音克隆**（如 `mimo-v2.5-tts-voiceclone`）→ 音频上传模块（mp3/wav ≤10MB；MiMo 无需预建音色，发送时样本以 `data:audio/mpeg;base64,…` 内联进 `audio.voice`）。

## API 对接层（给全栈开发）

唯一后端耦合点：`js/api.js`。页面只调 `API.*`，契约变更只改这一个文件。

```js
API.mode        // "mock" | "live"（设置页切换，localStorage 记忆）
API.baseUrl     // 默认 http://127.0.0.1:8000（设置页可改）
API.LIVE_ROUTES // 全部 live 路由表，逐条对照实现即可
```

| 方法 | Live 路由（草案，以群内契约清单为准） | 说明 |
|---|---|---|
| getWorldState / getWorldTimeline | GET /api/v1/world/state · /world/transitions | WorldState 快照 / 转移 |
| getConversations / getMessages / sendMessage | /api/v1/chat/… | 对话流 |
| getPlugins / togglePlugin | /api/v1/plugins/… | 插件启停 |
| getPrograms / programAction | /api/v1/programs/… | install/update/start/stop/restart/one-click |
| ttsGetModels / ttsGetVoices | POST /api/v1/tts/models · /tts/voices | 后端代理转发（规避浏览器 CORS） |
| ttsSaveConfig | PUT /api/v1/tts/config | 密钥落盘加密、回显脱敏 |
| ttsSynthesize | POST /api/v1/tts/synthesize | 试合成，返回音频 |
| ttsUploadCloneAudio | POST /api/v1/tts/clone-audio | multipart 上传 |
| exportDiagnostics | GET /api/v1/diagnostics/export | 脱敏诊断包（密钥一律 hasXxx 布尔指纹） |

## 诊断导出模块（V3 · 诊断输出脱敏）

- **入口**：设置页「诊断导出（脱敏）」卡 →「导出诊断包 .json」按钮，触发浏览器下载（文件名 `me-agent-diagnostics-YYYYMMDD-HHMM.json`）。
- **内容**：`generated_at` / `uptime` / 组件健康 / 近期错误摘要 / 配置密钥指纹——**密键一律 `hasXxx:true` 布尔指纹形态，绝不包含原文**。
- **脱敏形态预览**：导出后（Mock 模式自动载入同构演示数据）在卡片内直接展示高亮 JSON，`hasXxx` 指纹键绿色高亮，直观核对脱敏形态。
- **客户端密钥自检（兜底防线）**：导出前扫描 JSON 原文形态（sk- / Bearer / AKIA / gh token / 密文键值对），命中即红条警告「请勿分发」，未命中绿条通过。

**TTS 发送保真责任边界**：前端负责模板原文的编辑/落盘/占位符替换预览；实际 HTTP 发送由后端执行——后端拿到两段原文后**同样只做占位符替换，不得重序列化/改写**，才能保证端到端字节一致。

## 设计

深空监控台主题（`#0B1120` 底 / `#22C55E` 状态绿 / 轻玻璃层次），全内联 SVG 图标，`prefers-reduced-motion` 支持，响应式断点 900/640px。Fira Sans/Fira Code + 中文系统字体栈。
