/* ============================================================
   mock-data.js — 演示数据（对齐《QQ Agent 实现方案》§2.3 WorldState 字段模型）
   live 模式接入后端后，本文件所有数据将被 API 返回值替代。
   ============================================================ */
"use strict";

const MOCK = {

  /* ---------- 世界心跳：WorldState 快照 ---------- */
  worldState: {
    time: {
      wall_clock: "2026-10-25T10:15:00+08:00",
      circadian: "morning",             // morning/afternoon/evening/night
    },
    environment: {
      weather: { condition: "晴间多云", temp_c: 26, city: "深圳" },
      special_day: null,
    },
    body: {
      energy: 78,
      sleep_state: "awake",
      hunger: 42,
    },
    mind: {
      mood: { valence: 0.35, arousal: 0.30, label: "清爽" },
      stress: 18,
      wants: ["想喝杯手冲", "想把昨天的代码重构收尾", "想看看群友在聊什么"],
    },
    social: {
      battery: 68,
      recent_density: 23,
    },
    current: {
      location: "家/书桌",
      activity: { id: "coding", name: "写代码", start: "09:40", planned_end: "11:30", intensity: "medium", interruptible: "medium" },
      task: "把 Agent 记忆模块重构收尾",
      intent: "写完这段就泡咖啡休息一下",
      interrupt_queue: [
        { id: "iq1", type: "message", desc: "群友 @小满 提问 Hindsight 配置", arrived: "10:07", priority: "P0" },
        { id: "iq2", type: "reminder", desc: "11:00 Open-Meteo 天气缓存刷新", arrived: "—", priority: "P2" },
      ],
    },
    meta: {
      plan_ref: "plans/2026-10-25.yaml",
      last_transition: { from: "breakfast", to: "coding", at: "09:40", reason: "plan" },
      version: 312,
    },
  },

  /* 状态转移时间线（事件溯源 events.db 尾部） */
  transitions: [
    { at: "09:40", from: "breakfast", to: "coding", reason: "plan", hot: true },
    { at: "09:05", from: "wake_routine", to: "breakfast", reason: "anchor" },
    { at: "08:12", from: "sleep", to: "wake_routine", reason: "anchor" },
    { at: "00:31", from: "sleep_prep", to: "sleep", reason: "anchor" },
    { at: "23:02", from: "reading", to: "sleep_prep", reason: "plan" },
  ],

  /* 活动转移图（guard 约束，展示用） */
  activityEdges: [
    "sleep → wake_routine → breakfast → coding ↔ rest",
    "coding → lunch → nap ↔ coding",
    "running → rest → coding",
    "reading → sleep_prep → sleep",
    "scrolling → reading / coding / rest",
  ],

  /* ---------- 对话流 ---------- */
  conversations: [
    {
      id: "g_1", type: "group", name: "AI 玩机群", unread: 2,
      last: { from: "37", text: "这个 WorldState 的字段我补了几个", ts: Date.now() - 3 * 60000 },
      stats: { msgs_1h: 23, p0: 1, p1: 2, burst: 1 },
    },
    {
      id: "g_2", type: "group", name: "摸鱼下午茶", unread: 0,
      last: { from: "群友·小K", text: "（语音 12″）", ts: Date.now() - 42 * 60000 },
      stats: { msgs_1h: 6, p0: 0, p1: 0, burst: 0 },
    },
    {
      id: "p_1", type: "private", name: "37", unread: 1,
      last: { from: "37", text: "TTS 模块的原生需求你看了吗", ts: Date.now() - 9 * 60000 },
      stats: { msgs_1h: 4, p0: 1, p1: 0, burst: 0 },
    },
    {
      id: "p_2", type: "private", name: "群友·阿泽", unread: 0,
      last: { from: "小满", text: "嗯嗯，我看完啦", ts: Date.now() - 5 * 3600000 },
      stats: { msgs_1h: 0, p0: 0, p1: 0, burst: 0 },
    },
  ],

  messages: {
    g_1: [
      { id: "m1", from: "群友·小K", role: "user", text: "你们那个 agent 真的会自己冒泡吗", ts: Date.now() - 25 * 60000, pr: "P2" },
      { id: "m2", from: "群友·阿泽", role: "user", text: "会的，昨天晚上它还说要去跑步了晚点聊", ts: Date.now() - 24 * 60000, pr: "P2" },
      { id: "m3", from: "37", role: "user", text: "@小满 你现在状态怎么样，说说 WorldState 都有哪些字段", ts: Date.now() - 12 * 60000, pr: "P0", at: true },
      { id: "m4", from: "小满", role: "agent", text: "早上好呀～刚吃完早饭在写代码（记忆模块重构，9:40 开始）。现在字段有 10 项：时间/天气/心情(二维+标签)/精力/睡眠态/饥饿/社交电量/压力/我在干嘛/我想要的，外加当下意图和打断队列。情绪现在是清爽但不兴奋，精力 78。", ts: Date.now() - 11 * 60000, pr: "P0", latency_ms: 1450, llm: "deepseek-chat" },
      { id: "m5", from: "群友·小K", role: "user", text: "这个回复是状态机直出的？", ts: Date.now() - 8 * 60000, pr: "P1" },
      { id: "m6", from: "小满", role: "agent", text: "状态渲染器先出一段事实描述，再轻量润色，两小时前后的回答保证连续～", ts: Date.now() - 7 * 60000, pr: "P1", latency_ms: 980, burst: 1, llm: "deepseek-chat" },
      { id: "m7", from: "37", role: "user", text: "这个 WorldState 的字段我补了几个", ts: Date.now() - 3 * 60000, pr: "P2" },
    ],
    p_1: [
      { id: "p1m1", from: "37", role: "user", text: "TTS 模块的原生需求你看了吗", ts: Date.now() - 9 * 60000, pr: "P0" },
      { id: "p1m2", from: "小满", role: "agent", text: "看了！接口默认小米 MiMo completionsts，支持 OpenAI 兼容；文字提示语音模型要自定义请求头/请求体、字节保真发送；预设音色模型有中文音色列表+试听；克隆模型要音频上传。保存检查和试合成都安排上了～", ts: Date.now() - 8 * 60000, pr: "P0", latency_ms: 2100, llm: "deepseek-chat" },
    ],
    g_2: [
      { id: "g2m1", from: "群友·小K", role: "user", text: "（语音 12″）", ts: Date.now() - 42 * 60000, pr: "P2", audio: 12 },
      { id: "g2m2", from: "群友·阿泽", role: "user", text: "周末愉快各位～", ts: Date.now() - 40 * 60000, pr: "P2" },
    ],
    p_2: [
      { id: "p2m1", from: "群友·阿泽", role: "user", text: "那个插件怎么装", ts: Date.now() - 5.2 * 3600000, pr: "P0" },
      { id: "p2m2", from: "小满", role: "agent", text: "嗯嗯，我看完啦", ts: Date.now() - 5 * 3600000, pr: "P0", latency_ms: 800, llm: "deepseek-chat" },
    ],
  },

  /* ---------- 插件技能 ---------- */
  plugins: [
    {
      id: "weather", name: "天气感知", type: "passive", version: "1.2.0",
      desc: "Open-Meteo 免 key 拉取，30min 缓存，写入 WorldState.environment.weather",
      enabled: true, perms: ["net:api.open-meteo.com"], trigger: "每 30 分钟",
      manifest: `id: weather\ntype: passive\nversion: 1.2.0\ntrigger:\n  every: 30m\nwrites:\n  - environment.weather\nnet_whitelist:\n  - api.open-meteo.com`,
    },
    {
      id: "hindsight_retain", name: "记忆沉淀", type: "passive", version: "0.9.3",
      desc: "心跳事件差值过滤后转写文本，提交 Hindsight retain（一天约 10-20 条）",
      enabled: true, perms: ["fs:./memories", "svc:hindsight"], trigger: "每心跳 tick",
      manifest: `id: hindsight_retain\ntype: passive\ntrigger:\n  on: tick_delta\nfilter:\n  - block_switch\n  - anchor\n  - interrupted\n  - mood_impulse >= 0.2\n  - broadcast\nsink: hindsight://retain`,
    },
    {
      id: "tts_bridge", name: "语音桥接", type: "active", version: "0.3.1",
      desc: "Agent 决策发语音时调用 TTS API 合成，转 OneBot record 消息发出",
      enabled: true, perms: ["svc:tts", "onebot:send_group_record"], trigger: "被动调用",
      manifest: `id: tts_bridge\ntype: active\non_call:\n  - speak(text, voice)\napi: tts://current\nout: onebot://record`,
    },
    {
      id: "mood_report", name: "晚间播报", type: "active", version: "1.0.4",
      desc: "22:00 左右主动冒泡一条今日心情小结（受频率整形约束）",
      enabled: false, perms: ["onebot:send_group_msg"], trigger: "cron 22:00 ±20m",
      manifest: `id: mood_report\ntype: active\nevery:\n  cron: "0 22 * * *"\ncondition:\n  social.battery > 15\n  silent_hours: false\nout: p3_queue`,
    },
    {
      id: "birthday_greeter", name: "特别日问候", type: "active", version: "0.8.0",
      desc: "special_day 命中时（群友生日/节日）主动问候，走 P3 播报整形",
      enabled: true, perms: ["fs:./knowledge_bank", "onebot:send_group_msg"], trigger: "每日 08:30 检查",
      manifest: `id: birthday_greeter\ntype: active\nevery:\n  cron: "30 8 * * *"\nreads:\n  - environment.special_day\nout: p3_queue`,
    },
  ],

  /* ---------- 第三方程序（V4：一键管理回归） ----------
     form 字段决定一键形态："docker" = docker compose 一键拉取/启停/更新；
     "release" = 从 GitHub Release 拉取安装/更新；缺省 = 外部自管（仅健康探测）。
     upstream = 后端「检查更新」才能发现的最新版本（发现后写入 latest 并点亮徽标）。 */
  programs: [
    {
      id: "snowluma", name: "SnowLuma", tag: "通道层底座", form: "release",
      desc: "SnowLuma + OneBot 组合承载 QQ 消息通道，反向 WS 接入，事件总线分发。",
      status: "running", version: "0.7.2", latest: "0.7.2", upstream: "0.7.3",
      source: "github.com/SnowLuma/SnowLuma/releases", dir: "~/.me-agent/programs/snowluma",
      proc: { pid: 1024, mem_mb: 186, cpu: "3.2%", uptime: "2天14小时", restarts: 1 },
      actions: ["install", "start", "stop", "update", "one-click", "check-update", "log"],
      color: "var(--blue)",
      logo: "SL", logs: [
        "[10:02:11] onebot:reverse-ws connected (NapCat 4.8.1)",
        "[10:02:12] event-bus: subscribed message.group / message.private",
        "[10:07:44] p0 queue: 1 item dispatched → dialog pipeline",
      ],
    },
    {
      id: "hindsight", name: "Hindsight", tag: "记忆系统", form: "docker",
      desc: "情景记忆抽取+Observations 固化，与 Obsidian vault 单向同步（vault 只读审计）。",
      status: "running", version: "1.4.0", latest: "1.4.0", upstream: "1.4.1",
      image: "ghcr.io/me-agent/hindsight", container: "hindsight",
      proc: { pid: 1288, mem_mb: 412, cpu: "1.8%", uptime: "2天14小时", restarts: 0 },
      actions: ["install", "start", "stop", "update", "one-click", "check-update", "log"],
      color: "var(--purple)",
      logo: "H", logs: [
        "[09:41:02] retain: 3 events accepted (tick delta filter)",
        "[09:41:03] observation: upserted '37 周末晚上习惯安静'",
        "[10:07:50] recall: query 'WorldState 字段' → 4 chunks",
      ],
    },
    {
      /* 程序列表保留 obsidian_sync 条目；页面将其渲染为「Obsidian 同步器 · 可观测面板」，
         数据源 = 本条目的 health 字段（V4 契约已定稿：/programs 内 obsidian_sync.health） */
      id: "obsidian_sync", name: "Obsidian 同步器", tag: "人类可审计", form: "sync",
      desc: "每日 00:10 全量快照 + 事件触发增量：vault git commit 推送远端，人类可随时翻阅 Agent 大脑。",
      status: "running", version: "1.0.1", latest: "1.0.1", upstream: "1.0.2",
      proc: { pid: 1104, mem_mb: 38, cpu: "0.3%", uptime: "2天14小时", restarts: 0 },
      actions: ["sync-now", "update", "start", "log"],
      color: "var(--cyan)",
      logo: "Ob", logs: [
        "[00:10:03] snapshot: state/snapshots/2026-10-25.yaml committed (3.4 KB)",
        "[00:10:04] projection: 12 files changed → git push origin main (a1b2c3d)",
      ],
      /* 契约核心：running / last_sync_at / last_result{changed,pushed,error} / error_streak / detail；
         schedule / next_run / last_sync(富记录) / history / projection / vault / ws 为 Mock 演示
         增强字段 —— live 无这些字段时面板自动降级，不影响核心可观测。 */
      health: {
        running: true,
        last_sync_at: "2026-10-25T00:10:03+08:00",
        last_result: { changed: 12, pushed: 12, error: null },
        error_streak: 0,
        detail: "定时任务运行中 · 最近成功 00:10（注入 12 · 推送 12 文件）",
        /* ---- 演示增强（live 可缺省） ---- */
        schedule: "每日 00:10 全量快照 · retain 事件后 +5min 增量",
        next_run: "明日 00:10",
        last_sync: {
          at: "2026-10-25T00:10:03+08:00", outcome: "success", trigger: "cron",
          changed_files: 12, pushed_files: 12, commits: 1,
          commit: "a1b2c3d", duration_ms: 4210,
        },
        history: [
          { at: "2026-10-25T00:10:03+08:00", outcome: "success", trigger: "cron",
            changed_files: 12, pushed_files: 12, commits: 1, commit: "a1b2c3d", duration_ms: 4210 },
          { at: "2026-10-24T00:10:02+08:00", outcome: "failure", trigger: "cron",
            stage: "vault git push", code: "GIT_AUTH_FAILED",
            changed_files: 9, pushed_files: 0, commits: 0, duration_ms: 980,
            error: "fatal: Authentication failed for 'https://github.com/37/me-agent-vault.git'",
            hint: "检查 vault 远端凭据（gh auth status / 更新 PAT），或临时切换 SSH 远端后点「立即同步」重试。" },
          { at: "2026-10-23T00:10:05+08:00", outcome: "success", trigger: "cron",
            changed_files: 7, pushed_files: 7, commits: 1, commit: "9f8e7d6", duration_ms: 3980 },
        ],
        projection: {
          mounted: true, path: "~/ME_Agent/vault-projection",
          files: 128, latest_file_age_min: 36,
        },
        vault: {
          knowledge_files: 462, synced_entries: 397,
          last_commit: "a1b2c3d", remote: "github.com/37/me-agent-vault",
        },
        ws: { connected: true, channel: "obsidian.sync", last_push: "2026-10-25T00:10:04+08:00" },
      },
    },
    {
      id: "mimo_tts", name: "MiMo TTS 服务", tag: "语音合成",
      desc: "小米 MiMo completionsts 接口代理（内置去重/重试），为 Agent 语音消息提供合成。",
      status: "running", version: "0.3.1", latest: "0.3.1",
      proc: { pid: 1566, mem_mb: 96, cpu: "0.9%", uptime: "8小时", restarts: 2 },
      actions: ["start", "update", "stop", "log"],
      color: "var(--pink)",
      logo: "T", logs: [
        "[09:12:30] synthesize: voice=沁瑶 len=42chars dur=1.8s cache=miss",
        "[09:44:10] synthesize: voice=沁瑶 len=88chars dur=2.4s cache=hit",
      ],
    },
    {
      id: "napcat", name: "NapCat", tag: "QQ 协议端",
      desc: "OneBot v11 实现，承载 QQ 账号登录与消息收发，需配合签名服务器控风控。",
      status: "degraded", version: "4.8.1", latest: "4.8.3",
      proc: { pid: 932, mem_mb: 640, cpu: "6.5%", uptime: "2天14小时", restarts: 3 },
      actions: ["update", "restart", "log"],
      color: "var(--amber)",
      logo: "NC", logs: [
        "[08:00:05] login: qrcode refresh ok",
        "[09:58:12] warn: risk-control ping 812ms (threshold 800ms)",
        "[10:02:11] ws: connected to SnowLuma (reverse)",
      ],
    },
  ],

  /* ---------- 一键动作故障演练（一次性，触发后自动清除） ----------
     用途：演示后端统一动作失败契约 {ok:false, id, action, result:{code, error, hint}}
     —— error 为失败原因、hint 为修复指引文案（扁平字段），前端红条展示。
     首次触发，重试即成功，与真实可修复故障同构。 */
  drillFailures: {
    "hindsight:install": {
      code: "DOCKER_UNAVAILABLE",
      error: "Docker 环境不可用：docker info 探测失败（daemon 未响应或未安装）",
      hint: "先确认 Docker 已安装并运行（systemctl start docker 或打开 Docker Desktop，`docker info` 可用后重试）；一键拉取将执行 docker compose up -d 拉取镜像并启动容器。",
    },
    "hindsight:one-click": {
      code: "DOCKER_UNAVAILABLE",
      error: "Docker 环境不可用：docker info 探测失败（daemon 未响应或未安装）",
      hint: "先确认 Docker 已安装并运行（systemctl start docker 或打开 Docker Desktop，`docker info` 可用后重试）；一键接入将依次执行拉取 → 配置 → 启动 → 注册。",
    },
    "snowluma:update": {
      code: "RELEASE_DOWNLOAD_FAILED",
      error: "下载 release 超时：github.com 连接被重置（3 次重试均失败）",
      hint: "检查网络或代理（可为 Console 后端配置 HTTPS_PROXY 环境变量）；也可手动下载 release 包放入 ~/.me-agent/downloads/ 缓存目录后重试。",
    },
  },

  /* ---------- 诊断导出：GET /api/v1/diagnostics/export 的同构演示数据 ----------
     脱敏铁律：所有密钥仅以 hasXxx:true 布尔指纹出现，绝不出现原文。 */
  diagnostics: {
    generated_at: "2026-10-25T13:05:41+08:00",
    agent: { name: "小满", version: "0.5.0", mode: "headless" },
    uptime: "2天14小时6分",
    uptime_sec: 224766,
    components: [
      { id: "kernel",          name: "内核装配",    status: "ok",      detail: "composition 完成 · tick #1042" },
      { id: "onebot_gateway",  name: "OneBot 网关", status: "ok",      detail: "反向 WS 已连接 · NapCat 4.8.1" },
      { id: "memory",          name: "记忆系统",    status: "ok",      detail: "retain / recall 正常 · 最近 recall 10:07" },
      { id: "tts_bridge",      name: "TTS 桥接",    status: "warn",    detail: "上游延迟 2.4s（阈值 2s）" },
      { id: "obsidian_sync",   name: "Obsidian 同步", status: "ok",      detail: "定时任务运行中 · 最近成功 00:10（12 文件）" },
    ],
    recent_errors: [
      { ts: "2026-10-25T09:58:12+08:00", source: "programs/napcat",     message: "risk-control ping 812ms (threshold 800ms)" },
      { ts: "2026-10-25T08:12:44+08:00", source: "tts/synthesize",     message: "upstream timeout，重试 1/2 后成功" },
      { ts: "2026-10-24T22:00:03+08:00", source: "plugins/mood_report", message: "frequency shaping 推迟冒泡（social.battery=12）" },
    ],
    config_fingerprint: {
      has_deepseek_api_key: true,
      has_tts_api_key: true,
      has_onebot_access_token: true,
      has_snowluma_shared_secret: true,
      has_smtp_password: false,
    },
  },

  /* ---------- TTS：模型列表（依据接口地址 GET /models 的演示返回） ---------- */
  ttsModels: [
    {
      id: "mimo-v2.5-tts", object: "model", owned_by: "xiaomi-mimo",
      _meta: { type: "preset", label: "预设音色", desc: "官方 9 音色（冰糖/茉莉/苏打…），支持唱歌与低延迟流式；voice 填 audio.voice 字段。" },
    },
    {
      id: "mimo-v2.5-tts-voicedesign", object: "model", owned_by: "xiaomi-mimo",
      _meta: { type: "prompt", label: "文字提示语音", desc: "用自然语言描述音色与风格（user 消息），合成文本放 assistant 消息。" },
    },
    {
      id: "mimo-v2.5-tts-voiceclone", object: "model", owned_by: "xiaomi-mimo",
      _meta: { type: "clone", label: "语音克隆", desc: "音频样本以 data:audio/mpeg;base64,… 内联在 audio.voice，≤10MB，mp3/wav。" },
    },
    {
      id: "gpt-4o-mini-tts", object: "model", owned_by: "openai-compatible",
      _meta: { type: "prompt", label: "OpenAI 兼容", desc: "/v1/audio/speech 风格：input/voice/instructions（风格指令）字段，返回音频二进制。" },
    },
    {
      id: "mimo-v2.6", object: "model", owned_by: "xiaomi-mimo",
      _meta: { type: "none", label: "非语音模型", desc: "普通对话模型（出现在列表里可忽略；模型类型可手动指定）。" },
    },
  ],

  /* 官方音色列表（预设音色模型用）— MiMo 真实 9 音色，中文名 */
  ttsVoices: [
    { id: "mimo_default", name: "MiMo-默认", tags: ["默认", "女声"], desc: "中国集群=冰糖，其他集群=Mia" },
    { id: "冰糖",  name: "冰糖", tags: ["中文", "女声"], desc: "清甜灵动，日常对话" },
    { id: "茉莉",  name: "茉莉", tags: ["中文", "女声"], desc: "温润柔和，叙事感" },
    { id: "苏打",  name: "苏打", tags: ["中文", "男声"], desc: "清爽少年感" },
    { id: "白桦",  name: "白桦", tags: ["中文", "男声"], desc: "沉稳低语" },
    { id: "Mia",   name: "Mia",  tags: ["英文", "女声"], desc: "English female, natural" },
    { id: "Chloe", name: "Chloe", tags: ["英文", "女声"], desc: "English female, warm" },
    { id: "Milo",  name: "Milo", tags: ["英文", "男声"], desc: "English male, casual" },
    { id: "Dean",  name: "Dean", tags: ["英文", "男声"], desc: "English male, deep" },
  ],

  /* 文字提示语音模型：默认请求头/请求体模板（原文，仅占位符替换后发送）
     依据信息哨兵《TTS接口规格调研报告》：MiMo 为 chat/completions 形态，
     合成文本必须放 role=assistant，user 放风格指令；音频在响应 choices[0].message.audio.data */
  ttsDefaults: {
    endpoint: "https://api.xiaomimimo.com/v1/chat/completions",
    headerTemplate:
`Authorization: Bearer {{api_key}}
Content-Type: application/json`,
    bodyTemplate:
`{"model":"{{model}}","messages":[{"role":"user","content":"{{style}}"},{"role":"assistant","content":"{{text}}"}],"audio":{"format":"wav","voice":"{{voice}}"}}`,
  },

  /* 试合成示例文案 */
  ttsSampleTexts: [
    "周六的上午好呀～我刚吃完早饭，正在给记忆模块做重构，泡了杯手冲，等你来聊天。",
    "晚上好，今天跑完五公里啦，现在在沙发上看《三体III》最后两章，群里明天见～",
    "这道题的答案是四十二，不过更重要的是提问的方式。",
  ],
};

/* 演示用：模拟一次「事件触发」的增量同步成功（由 WS 推送模拟器调用，对应
   后端 obsidian.sync topic，驱动可观测面板自动刷新）。同步更新契约核心字段
   （last_sync_at / last_result / error_streak / detail）与演示增强字段。 */
MOCK.advanceObsidianSync = function () {
  const entry = (this.programs || []).find(p => p.id === "obsidian_sync");
  const s = entry && entry.health;
  if (!s) return;
  const now = new Date();
  const changed = 1 + Math.floor(Math.random() * 3);
  const rec = {
    at: now.toISOString(), outcome: "success", trigger: "event",
    changed_files: changed, pushed_files: changed, commits: 1,
    commit: Math.random().toString(16).slice(2, 9),
    duration_ms: 700 + Math.floor(Math.random() * 900),
  };
  s.last_sync_at = rec.at;
  s.last_result = { changed: rec.changed_files, pushed: rec.pushed_files, error: null };
  s.error_streak = 0;
  s.detail = `事件触发同步成功 · 注入 ${rec.changed_files} · 推送 ${rec.pushed_files} 文件`;
  s.last_sync = rec;
  s.history.unshift(rec);
  if (s.history.length > 6) s.history.length = 6;
  s.projection.files += 1;
  s.projection.latest_file_age_min = 0;
  s.vault.synced_entries = Math.min(s.vault.knowledge_files, s.vault.synced_entries + changed);
  s.vault.last_commit = rec.commit;
  s.ws.last_push = now.toISOString();
};

window.MOCK = MOCK;
