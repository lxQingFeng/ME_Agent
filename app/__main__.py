"""一键启动入口: python -m app

拉起全系统(心跳引擎 + 仲裁器 + OneBot 网关 + 记忆服务 + API 服务):
  python -m app                       # 前台运行, 8000 端口(REST/WS/控制台/OneBot 同端口)
  python -m app --port 8080           # 自定义端口
  python -m app --tick-seconds 5      # 心拍加速(联调; 生产 300)
  python -m app --no-engine           # 只起 API 服务(前端联调/冒烟)

环境变量: .env 自动加载(config/.env → 项目根 .env); 关键项:
  ME_INTERNAL_TOKEN   插件回连令牌
  ONEBOT_ACCESS_TOKEN OneBot 鉴权(与 SnowLuma 侧一致)
  DEEPSEEK_API_KEY    LLM(不配则对话降级为占位回复)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

log = logging.getLogger("app")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m app", description="ME_Agent 一键启动")
    ap.add_argument("--host", default="", help="监听地址(默认取 config/console.yaml host)")
    ap.add_argument("--port", type=int, default=0, help="端口(默认取 config/console.yaml port)")
    ap.add_argument("--tick-seconds", type=int, default=0,
                    help="世界心跳节拍秒数(覆盖配置; 联调可加速)")
    ap.add_argument("--no-engine", action="store_true", help="不起心跳引擎(仅 API 服务)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    import uvicorn

    from app.config import Settings
    from app.api.context import AppContext
    from app.api import create_app

    settings = Settings()
    host = args.host or settings.console.get("host", "0.0.0.0")
    port = args.port or int(settings.console.get("port", 8000))
    if args.tick_seconds > 0:
        settings.console.setdefault("engine", {})
        settings.console["engine"]["tick_seconds"] = args.tick_seconds

    ctx = AppContext(settings, start_engine=not args.no_engine)
    app = create_app(ctx)

    log.info("ME_Agent 启动: http://%s:%s (console=%s onebot_ws=%s push_ws=/api/ws)",
             host, port, f"http://{host}:{port}/console/index.html",
             settings.channels.get("onebot", {}).get("ws_path", "/onebot"))
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    except KeyboardInterrupt:
        log.info("收到 Ctrl-C, 退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
