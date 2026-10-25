# 部署说明

## 依赖

- Python 3.11+（开发环境 3.12）
- `pip install -r requirements.txt`（fastapi / uvicorn / httpx / pydantic / PyYAML / websockets）
- 外部服务（按需）：LLM 网关（OpenAI 兼容 `/chat/completions`）、Hindsight（记忆）、Fish Audio 或兼容 TTS

## 快速启动

```bash
# Linux / macOS(仓库根)
./scripts/launch                 # 等价 python -m app, 自动优先 .venv
python -m app                    # 前台, 8000 端口(REST/WS/控制台/OneBot 同端口)
python -m app --port 8080        # 自定义端口
python -m app --no-engine        # 只起 API 服务(前端联调/冒烟)
```

### Windows（双击可用）

| 脚本 | 行为 |
|---|---|
| `deploy\scripts\启动ME_Agent.bat` | 纯 ASCII（防 cmd 编码坑）。启动前检查 `.venv` 与六个关键依赖（fastapi/uvicorn/httpx/yaml/websockets/pydantic）存在性；**缺失时打印明确修复指引后暂停退出**（不双击装死）：`python -m venv .venv` + `.venv\Scripts\pip.exe install -r requirements.txt`；检查通过后 `start /b` 静默拉起，窗口保留展示日志（关窗即停止，Ctrl+C 亦可） |
| `deploy\scripts\启动ME_Agent.ps1` | PowerShell 版。默认**后台隐藏启动**：PID 写 `logs\agent.pid`、日志重定向 `logs\agent.log` / `agent.err.log`；`-Foreground` 切前台交互；`-Port 8080` / `-ExtraArgs @('--no-engine')` 透传；停止：`taskkill /PID <logs\agent.pid 里的数字> /F` |

首次安装（项目根执行一次；第 2 行让 pip 全程 UTF-8，中文 Windows 双保险——requirements.txt 已纯 ASCII 化，不设也能装）：

```bat
python -m venv .venv
set PYTHONUTF8=1
.venv\Scripts\pip.exe install -r requirements.txt
```

（PowerShell 用 `$env:PYTHONUTF8='1'` 代替 `set` 行。）

## 单实例锁

同一 `state/` 目录默认只允许一个实例运行（claim 票据文件 + `kill(pid,0)` 失效进程回收，uvicorn SIGTERM 兜底已处理）。双开第二个进程会**直接拒绝**：exit code 2 + 提示持锁 pid 与修复指引（`换 ME_STATE_DIR` 或删除 `state/me-agent.lock.claim-*` 残留）。多实例请各自设独立状态目录：

```bash
ME_STATE_DIR=/data/agent2 python -m app --port 8100
```

## 通道双模（OneBot v11）

由 `config/channels.yaml` 的 `onebot.mode` 切换（环境变量 `ME_ONEBOT_MODE` 可覆盖）：

| 模式 | 连接方向 | 适用 |
|---|---|---|
| `reverse`（默认） | 协议端（SnowLuma 等）反向 WS 连入本服务 `listen_port + ws_path` | 桌面同机/局域网 |
| `forward` | 本服务主动外连协议端的正向 WS 服务（`onebot.ws_url`），**断线自动重连自愈**（`reconnect_interval_s`） | 云部署（无需公网监听端口）、协议端为 NapCat/Lagrange 正向服务 |

```bash
ME_ONEBOT_MODE=forward ME_ONEBOT_WS_URL=ws://127.0.0.1:3001 python -m app
```

两种模式下 token 均走 `ONEBOT_ACCESS_TOKEN`（forward 时以 query 参数 + Authorization 头双通道携带）；网关状态含 `mode / forward_url / reconnect_attempts / last_error`（`/api/v1/system/status` 的 gateway 段）。

## 第三方程序口径（一键代管 + 健康探测，2026-10-25 第 10 轮）

`config/programs.yaml` 注册表按 kind 分层代管：

| 程序 | 形态 | 控制台可做 |
|---|---|---|
| SnowLuma（QQ 协议端） | `binary + source.github_release` | 一键拉取（真实 release 资产，平台自动选包 win-x64.zip/linux-x64.tar.gz，内含 launcher+自带 node）→ 启动（spawn launcher）→ 更新（check-latest→停旧→装新→自动重启）。QQ 登录在协议端自身侧完成 |
| Hindsight（记忆引擎） | `docker` | 一键拉取（`docker compose -f deploy/hindsight/docker-compose.yml pull`，缺 compose 时 `docker pull`）→ 启动（`compose up -d` 或 `docker run` 参数对齐 compose）→ 停止/更新。**本机无 docker 时返回明确指引与降级建议**（装 Docker Desktop / 远程部署后改 health.url / 退回 external 口径），不报错崩 |
| NapCat（备选协议端） | `external` | 外部自管：只做健康探测（ws_event）与状态播报；动作接口返回明确指引 |
| Obsidian 同步器 | `internal + manual_sync` | 启停同步循环 + 「立即同步」；每次同步的成败摘要落程序日志并发 `obsidian.sync` WS 事件 |

长动作（install/update）由 API 转后台任务：程序状态置 `busy` 防重复触发，`POST /api/v1/programs/{pid}/{action}` 立即返回 `{queued: true}`，完成经 `program.status` WS 事件播报、`GET /api/v1/programs` 轮询与程序日志观察。动作响应统一 `{ok, id, action, result}`，失败原因与修复指引在 `result.error` / `result.hint`（扁平字段）。

给程序加 `source:`（github_release）/`docker:` 字段并去掉 `external` 即恢复对应代管；反之加 `kind: external` 即收缩为纯健康探测。

## 诊断导出（排障不泄密）

```bash
curl http://127.0.0.1:8000/api/v1/diagnostics/export
```

返回诊断包 JSON：运行状态、组件健康、近期错误摘要（WARNING+ 环形缓冲）、配置密钥指纹。**全部密钥按 SECRET_KEY_PATTERN 脱敏为 `hasXxx` 布尔指纹**（如 `hasOnebotAccessToken: true`），严禁明文；TTS 两段请求原文仅输出存在性指纹。现有含密钥的 API 输出（TTS 配置回显）统一走 `app/api/sanitizer.py`（严格 `***`，不带首尾明文片段）。

## 配置

1. `config/*.yaml`：人设、作息、心跳、通道、LLM、TTS 等（见 `config/` 12 份）。
2. 敏感项走 `.env`（不入库）：至少 `ME_INTERNAL_TOKEN`（插件回连控制台令牌）、LLM API Key、`ONEBOT_ACCESS_TOKEN`。

## Hindsight 记忆服务（Compose，37 一键部署）

编排文件 `deploy/hindsight/docker-compose.yml`（镜像/端口/卷/环境变量名均经官方
openapi.json + README 核实 2026-10-25）。**双 bank（qq-agent/knowledge）与 webhook 回连
无需手工建：ME_Agent 启动时 `ensure_banks()` 幂等下发**（建库 PUT → Memory Defense
redact 策略 → webhook 注册到 `http://host.docker.internal:8000/webhooks/hindsight`）。

```bash
# 1) 装好 Docker 后, 在仓库根执行(首次拉镜像约 1-2 分钟):
docker compose -f deploy/hindsight/docker-compose.yml up -d

# 2) .env 里给 Hindsight 配 LLM key(与主链路 DeepSeek 复用同一把):
#    DEEPSEEK_API_KEY=sk-xxx          # 可选: HINDSIGHT_LLM_PROVIDER=deepseek(默认)
#    HINDSIGHT_WEBHOOK_SECRET=xxx     # 可选: webhook HMAC 签名密钥(不配则不验签)

# 3) 验证服务就绪(REST 8888 / Web UI 9999):
curl http://127.0.0.1:8888/health
# 浏览器打开 http://127.0.0.1:9999 可直接看记忆库

# 4) 正常启动 ME_Agent(python -m app), 启动即自动:
#    建 qq-agent/knowledge 双 bank → 开 Memory Defense → 注册 webhook 回连
#    控制台「记忆」页 status 应显示 fail_streak=0、banks 双库就位

# 排障: docker compose -f deploy/hindsight/docker-compose.yml logs -f hindsight
# 静态校验(无 docker 也能跑): python3 deploy/hindsight/check_compose.py
```

说明：①Windows Docker Desktop 与 macOS 自带 `host.docker.internal`；Linux 由 compose
`extra_hosts: host-gateway` 自动补；②数据在具名卷 `hindsight-data`（内嵌 Postgres），
`docker compose down` 不丢、`down -v` 才清库；③端点/签名已对齐官方 API 1.1
（`/v1/default/banks/{bank}/memories` 体系、`X-Hindsight-Signature: sha256=<hex>`）。

## 已验证 / 未验证边界（2026-10-25 沙箱实测）

| 项 | 状态 |
|---|---|
| `python -m app` 全系统、reverse/forward 双模、断线自动重连、实例锁双开拒绝与回收、诊断导出零明文、冒烟 17/17 | ✅ 沙箱实测（tests/ 三套共 47 断言） |
| 心跳回放/仲裁器/整形器单测（19+28+27=74 断言） | ✅ 沙箱实测（2026-10-25 第 6 轮，六套合计 121 断言全绿） |
| Hindsight 编排（compose + 端点/签名对齐官方 API 1.1 + 静态校验 28/28） | ✅ 静态校验；容器拉起待 37 实机（见上「Hindsight 记忆服务」一键步骤） |
| Windows .bat/.ps1 双击行为 | ⏳ 静态校验（bat 纯 ASCII、ps1 UTF-8 BOM），待实机确认 |
| SnowLuma/NapCat 真机联调、真实上游 key | ⏳ 未实测（见 docs/实施进度.md 缺口清单，均需 37 实机操作） |

## 接入链路

```
reverse 模式: SnowLuma(OneBot v11) ──反向WS──> Console(gateway) ──> 心跳/对话/记忆 ──> QQ
forward 模式: Console(gateway) ──正向WS外连──> NapCat/Lagrange(OneBot v11 正向 WS 服务)
```

- 22 端口受限环境下 SSH 走 `ssh.github.com:443`（仅影响开发沙箱运维，不影响部署）。

## 控制台前端

`console/` 为纯静态单页：直接开放 `console/index.html`，或任意静态服务器托管
（`python3 -m http.server 8000`）。默认 Mock 演示模式；Live 模式需后端就绪后在
设置页填后端地址。

## 自测

```bash
python3 tests/test_hardening.py       # 实例锁/双开拒绝/脱敏/诊断导出(20 断言)
python3 tests/test_gateway_dual.py    # 通道双模+断线重连+reverse 回归(10 断言)
python3 tests/test_heartbeat_replay.py  # 心跳回放/事件溯源一致性(19 断言)
python3 tests/test_arbiter.py           # 仲裁器分级/突发合并/延迟决策(28 断言)
python3 tests/test_shaper.py            # 整形器延迟/拆条/令牌桶/睡眠静默(27 断言)
python3 -m app --no-engine &          # 冒烟目标(独立 ME_STATE_DIR 更佳)
python3 tests/smoke_api.py http://127.0.0.1:8000  # API 冒烟(17 断言)
python3 deploy/hindsight/check_compose.py  # Hindsight 编排静态校验(28 断言)
```
