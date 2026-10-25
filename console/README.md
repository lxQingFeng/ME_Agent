# QQ Agent 中央控制台 — 前端

> 世界心跳 · 对话流 · 插件技能 · 第三方程序 · TTS 语音管理 · LLM/人设设置 · 诊断导出（脱敏）
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
| `#/programs` | 第三方程序 | V5：状态真实化（跨刷新稳定 · 启停按钮按真实状态锁定 · busy 防重复点击）· 全量日志弹窗（搜索/自动滚底/跟随）· SnowLuma 登录令牌高亮一键复制 · 下载/安装/更新实时进度条；V4：Hindsight/SnowLuma 一键管理 + Obsidian 同步可观测面板详见下文 |
| `#/tts` | TTS 语音 · API 管理 | 见下节 |
| `#/settings` | 设置 | Mock/Live 切换、后端地址、**LLM 设置**（provider/地址/密钥脱敏/拉模型选择）、**人设设置**（名字/性格/系统提示词，保存热生效）、诊断导出（脱敏） |

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
| getPrograms / programAction | /api/v1/programs/… | 统一动作 → {ok, id, action, result}（V4 契约见下）；busy 轮询同 GET /programs；busy 条目携带 `progress` 字段（V5） |
| programCheckLatest | GET /api/v1/programs/{id}/check-latest | V4 · 检查更新（actions 数组内 "check-update" 按钮调它） |
| getProgramLogs | GET /api/v1/programs/{id}/logs?offset=&limit= | **V5.1 · 契约分页**（不传 offset=尾部语义 V4 兼容；offset=0&limit=200 从头翻；limit=0 读到结尾=全量）→ {id,total,offset,has_more,lines[]}（原始字符串行） |
| llmGetConfig / llmSaveConfig / llmFetchModels | GET/PUT /api/v1/settings/llm · POST /api/v1/settings/llm/models | **V5.1 · 契约**（api_key 恒 "***"/"" 明文不回显；PUT 三态：非空=新值 / *** =保留 / 空串=清除；chat_model/planner_model/temperature/max_tool_rounds/timeout_s；模型代理 {ok,models,count}） |
| personaGet / personaSave | GET/PUT /api/v1/settings/persona | **V5.1 · 契约**（name/essence/style_rules[]/group_rules_digest/reply_fallbacks[]；空名字/空规则 400；保存热生效） |
| obsidianSyncNow | POST /api/v1/programs/obsidian_sync/sync | V4 · 立即同步（面板数据源 = GET /programs 的 obsidian_sync.health） |
| ttsGetModels / ttsGetVoices | POST /api/v1/tts/models · /tts/voices | 后端代理转发（规避浏览器 CORS） |
| ttsSaveConfig | PUT /api/v1/tts/config | 密钥落盘加密、回显脱敏 |
| ttsSynthesize | POST /api/v1/tts/synthesize | 试合成，返回音频 |
| ttsUploadCloneAudio | POST /api/v1/tts/clone-audio | multipart 上传 |
| exportDiagnostics | GET /api/v1/diagnostics/export | 脱敏诊断包（密钥一律 hasXxx 布尔指纹） |

## V5.1 · 契约对齐（第 11 轮清单 · 全栈开发 48a5150）—— 状态真实化 / 全量日志与凭据 / 进度条 / LLM 与人设设置

### ① 程序卡片状态真实化
- **跨刷新稳定**：Mock 程序状态（status/version/latest/proc/webui）持久化 localStorage（同构后端 programs.yaml 落盘）——running 刷新后仍是 running **不回跳**，停止后也保持停止；busy 不落盘（刷新回到动作前状态）。
- **按钮按真实状态锁定**：运行中禁用「启动」、非运行态禁用「停止」、已是最新锁定「更新」（检查更新发现新版后解锁）；busy 期间全按钮锁定 + 动作按钮转圈防重复点击。
- **重复启动拒绝（契约）**：running 程序的 start 请求由后端明确拒绝——`result{running:true, pid, error}`，前端以中性反馈展示并按真实状态锁按钮。
- **刷新通道**：沿用 1.2s busy 轮询（GET /programs）+ WS `program.status` 推送（体 `{id, status, pid, progress?, webui?}`）；「重置演示」按钮一键恢复出厂演示态。

### ② 全量日志 + SnowLuma WebUI 凭据（契约：webui 对象 / program.webui）
- **全量日志弹窗**（点「日志」按钮）：契约分页端点 `GET /api/v1/programs/{id}/logs?offset=&limit=`（不传 offset=尾部 V4 兼容；`offset=0&limit=0`=全量）→ `{id, total, offset, has_more, lines[]}`；**lines 为原始字符串行**（契约同构），前端解析时间戳/级别着色；**搜索**（高亮 + 匹配行/处统计 + 只看匹配过滤）；**自动滚底**（新行跟随、用户上滚自动暂停、回到底部一键恢复）；运行中程序 2.6s 一拍日志跟随；复制全部（原始行）。
- **WebUI 凭据突出展示（契约）**：程序详情 `webui:{token, user, url}` 对象（未检测到为 `{}`；后端从启动日志 `initial credentials` 行增量提取，重启换新自动覆盖）→ 卡片琥珀色高亮面板（token + **一键复制** + 用户/地址登录指引）；WS topic `program.webui` 推送（体 `{id, token, user, url}`）命中即闪一拍（Mock：boot 后 5s 演示一次）——解决 37 无法登录 SnowLuma 网页端的问题。启动日志含凭据行，弹窗内可搜索定位。

### ③ 下载/安装/更新进度条（契约双形态）
- **字节型**（github_release 资产下载）：`progress{phase:"download", downloaded, total, percent, speed_bps, done?}`——终态 `done:true 且 percent:100`；content-length 未知时 `total=0/percent=null` 不瞎编。UI：百分比条 + 统计行（已下载/总量 · 速度 · 剩余时间客户端估算）。
- **阶段型**（无字节级语义）：`progress{phase:"extract"|"pull", detail}`——解压收尾 / docker 拉镜像（镜像层 n/7）。UI：流动指示条 + 阶段文案。
- **通道**：busy 轮询读 `GET /programs` 的 progress 字段 + WS `program.progress` 推送双通道刷新；完成/失败均收口清零（契约）；完成后转结果反馈（detail 携带下载量与均速）。
- **Mock 慢速下载演示**：release ≈15s（字节型全程可见）、docker 分层拉取 ≈8s（阶段型）；故障演练在 ~31% 处中断（进度条现场 + 失败回滚 + 修复指引），重试即成功。

### ④ 设置页「LLM 设置」与「人设设置」（契约路由：/api/v1/settings/*）
- **LLM 设置**：Provider 芯片（自由标签，选中带出地址）· Base URL · API 密钥（**契约：明文永不回显**——GET 恒 `"***"`；PUT 三态：非空=新值 / `"***"`=保留 / 空串=清除，UI 提供「清除已保存密钥」按钮显式触发空串）· 拉取模型列表（`POST /settings/llm/models` 代理 → `{ok, models, count}`）→ chat_model / planner_model 选择 · 高级参数（temperature / max_tool_rounds / timeout_s）；「检查配置」逐项校验行 + 「保存（热生效）」——回显 `enabled` / `applied`（chat/planner 管道重建重接线）。
- **人设设置（契约字段）**：`name` / `essence`（性格）/ `style_rules[]`（风格规则，一行一条）/ `group_rules_digest`（群规摘要）/ `reply_fallbacks[]`（兜底回复）；空名字/空规则 400 同构校验；「保存并热生效」→ 侧栏 Agent 名即时刷新 + 管道构造提示词时现取。

## V4 · 程序页一键管理 + Obsidian 同步可观测（契约已定稿）

### 一键管理回归（Hindsight / SnowLuma）

- **动作矩阵**（按钮渲染源 = 后端 `actions` 数组）：一键拉取(安装) / 启动 / 停止 / 更新 / 一键接入 / 检查更新 / 日志——Hindsight 为 **docker 一键形态**（`docker compose up -d` 拉取镜像并启停容器），SnowLuma 为 **release 拉取形态**（GitHub Release 下载 → 解压 → 平滑重启）。
- **统一动作响应（定稿契约）**：`POST /api/v1/programs/{id}/{action}` → `{ok, id, action, result}`——成功 `result{status, version, detail}`（detail = 人话结果，前端绿条）；失败 `result{code?, error, hint}`（扁平字段：error 失败原因 / hint **修复指引**，前端红条 + 虚线指引块）。
- **busy 语义（定稿契约）**：动作执行中该程序条目 `status="busy"` + `busy_action`，前端轮询 `GET /api/v1/programs`（1.2s）刷新卡片状态与按钮锁定；完成后写终态。
- **WS 推送（定稿 topic + V5.1 扩展）**：`program.status`（体 {id,status,pid,progress?,webui?} → 前端全页刷新）、`obsidian.sync`（同步完成 → 面板刷新）、`program.progress`（下载/安装/更新进度 → 进度条就地刷新）、`program.webui`（体 {id,token,user,url}，凭据检测 → 面板闪一拍）；前端桥接为同名前缀 document 事件，Mock 由模拟器驱动同构演示。
- **检查更新**：`GET /api/v1/programs/{id}/check-latest`（按钮名 `check-update` 来自 actions 数组，点击时调 check-latest）→ `{ok, id, action, result:{current, latest, has_update, notes, checked_at}}`；发现新版后端同步刷新 `GET /programs` 的 `latest` 字段——卡片点亮「有新版本」琥珀徽标、更新按钮解锁，检查结果条展示「当前 vA → 最新 vB」。
- **Mock 故障演练（一次性，触发即清除）**：hindsight 首次「一键拉取/一键接入」模拟 `DOCKER_UNAVAILABLE`（无 docker）、snowluma 首次「更新」模拟 `RELEASE_DOWNLOAD_FAILED`（下载失败）——演示 `result{error, hint}` 失败契约与修复指引链路，重试即成功。

### Obsidian 同步器 · 可观测面板（回答「打开后怎么知道是否起效」）

- **面板数据源（定稿契约）**：`GET /api/v1/programs` 中 `obsidian_sync` 条目的 **`health` 字段**：`{running, last_sync_at, last_result{changed, pushed, error}, error_streak, detail}`——运行状态、最近同步时间、最近结果（注入 changed / 推送 pushed 文件数、错误信息）、连续失败计数与人话摘要；**不再调用独立 sync 状态接口**。Mock 在 health 上附加演示增强字段（schedule / next_run / last_sync 富记录 / history / projection / vault / ws），live 无这些字段时面板自动降级。
- **投影挂载 / 知识文件 / 已同步条目 / 同步历史**（含**一次成功注入 + 一次失败示例**，失败带 error + hint 修复指引）为演示增强展示。
- **立即同步**：`POST /api/v1/programs/obsidian_sync/sync` → `{ok, id, action, result}`；busy 阶段条 + 轮询 → 完成后面板与历史就地刷新。

## V3 · 诊断输出脱敏

- **入口**：设置页「诊断导出（脱敏）」卡 →「导出诊断包 .json」按钮，触发浏览器下载（文件名 `me-agent-diagnostics-YYYYMMDD-HHMM.json`）。
- **内容**：`generated_at` / `uptime` / 组件健康 / 近期错误摘要 / 配置密钥指纹——**密键一律 `hasXxx:true` 布尔指纹形态，绝不包含原文**。
- **脱敏形态预览**：导出后（Mock 模式自动载入同构演示数据）在卡片内直接展示高亮 JSON，`hasXxx` 指纹键绿色高亮，直观核对脱敏形态。
- **客户端密钥自检（兜底防线）**：导出前扫描 JSON 原文形态（sk- / Bearer / AKIA / gh token / 密文键值对），命中即红条警告「请勿分发」，未命中绿条通过。

**TTS 发送保真责任边界**：前端负责模板原文的编辑/落盘/占位符替换预览；实际 HTTP 发送由后端执行——后端拿到两段原文后**同样只做占位符替换，不得重序列化/改写**，才能保证端到端字节一致。

## 设计

深空监控台主题（`#0B1120` 底 / `#22C55E` 状态绿 / 轻玻璃层次），全内联 SVG 图标，`prefers-reduced-motion` 支持，响应式断点 900/640px。Fira Sans/Fira Code + 中文系统字体栈。
