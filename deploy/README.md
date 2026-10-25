# 部署说明

## 依赖

- Python 3.11+（开发环境 3.12）
- `pip install -r requirements.txt`（fastapi / httpx / pydantic / PyYAML）
- 外部服务（按需）：LLM 网关（OpenAI 兼容 `/chat/completions`）、Hindsight（记忆）、Fish Audio 或兼容 TTS

## 配置

1. `config/*.yaml`：人设、作息、心跳、通道、LLM、TTS 等（见 `config/` 12 份）。
2. 敏感项走 `.env`（不入库）：至少 `ME_INTERNAL_TOKEN`（插件回连控制台令牌）、LLM API Key、OneBot access_token。

## 接入链路

```
SnowLuma(OneBot v11) ──反向WS──> Console(gateway) ──> 心跳/对话/记忆 ──> QQ
```

- SnowLuma 以反向 WebSocket 连入本服务（地址与 access_token 见 `config/channels.yaml`）。
- 22 端口受限环境下 SSH 走 `ssh.github.com:443`（仅影响本沙箱运维，不影响部署）。

## 控制台前端

`console/` 为纯静态单页：直接开放 `console/index.html`，或任意静态服务器托管
（`python3 -m http.server 8000`）。默认 Mock 演示模式；Live 模式需 `app/api/` 就绪后在
设置页填后端地址。

## 现状提醒（2026-10-25）

`app/api/` HTTP 服务层与总装入口尚未实现（见 `docs/实施进度.md` 缺口清单），
当前形态为「模块就绪、待总装」：各子系统可单测运行（如 `python -m app.heartbeat.replay`），
一键启动命令将在 API 层落地后补充到本文档。
