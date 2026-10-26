"""一键启动入口: python -m app

拉起全系统(心跳引擎 + 仲裁器 + OneBot 网关 + 记忆服务 + API 服务):
  python -m app                       # 前台运行, 8000 端口(REST/WS/控制台/OneBot 同端口)
  python -m app --port 8080           # 自定义端口
  python -m app --tick-seconds 5      # 心拍加速(联调; 生产 300)
  python -m app --no-engine           # 只起 API 服务(前端联调/冒烟)
  python -m app --no-boot             # 跳过开机联动(等效 ME_AUTOSTART=0)

开机联动(2026-10-26 第 13 轮): 启动后默认自动拉起 config/programs.yaml
autostart 编排的第三方程序(hindsight → snowluma → obsidian_sync)并接入,
整链幂等、失败降级不阻塞; 需手动触发用 POST /api/v1/system/boot。

单实例锁(2026-10-25 补强): state/ 下 claim 票据, 同一 state 目录双开直接拒绝;
换独立实例请设 ME_STATE_DIR=<其他目录>。

环境变量: .env 自动加载(config/.env → 项目根 .env); 关键项:
  ME_INTERNAL_TOKEN   插件回连令牌
  ONEBOT_ACCESS_TOKEN OneBot 鉴权(与 SnowLuma 侧一致)
  DEEPSEEK_API_KEY    LLM(不配则对话降级为占位回复)
  ME_STATE_DIR        state 目录覆盖(多实例/测试)
  ME_ONEBOT_MODE      通道模式覆盖(reverse|forward)
  ME_ONEBOT_WS_URL    forward 模式协议端地址覆盖
  ME_AUTOSTART        开机联动开关(0=启动不自跑; 手动 POST /system/boot 不受影响)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from app.instancelock import InstanceLockError, instance_lock

log = logging.getLogger("app")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m app", description="ME_Agent 一键启动")
    ap.add_argument("--host", default="", help="监听地址(默认取 config/console.yaml host)")
    ap.add_argument("--port", type=int, default=0, help="端口(默认取 config/console.yaml port)")
    ap.add_argument("--tick-seconds", type=int, default=0,
                    help="世界心跳节拍秒数(覆盖配置; 联调可加速)")
    ap.add_argument("--no-engine", action="store_true", help="不起心跳引擎(仅 API 服务)")
    ap.add_argument("--no-boot", action="store_true",
                    help="跳过开机联动(不自动拉起第三方程序; 等效 ME_AUTOSTART=0, "
                         "手动触发仍可用 POST /api/v1/system/boot)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    import os
    if args.no_boot:
        os.environ["ME_AUTOSTART"] = "0"   # 开机联动开关在 startup() 读取(见 app/api/context.py)

    from app.config import Settings

    settings = Settings()
    host = args.host or settings.console.get("host", "0.0.0.0")
    port = args.port or int(settings.console.get("port", 8000))
    if args.tick_seconds > 0:
        settings.console.setdefault("engine", {})
        settings.console["engine"]["tick_seconds"] = args.tick_seconds

    try:
        with instance_lock(settings.state_dir):
            return _serve(settings, host, port, start_engine=not args.no_engine)
    except InstanceLockError as e:
        # 双实例启动: 明确报错 + 修复指引, 直接拒绝(2026-10-25 补强第 1 项)
        msg = f"启动被拒绝: {e}"
        print(f"\n[ME_Agent] {msg}\n", file=sys.stderr)
        log.error(msg)
        return 2


def _serve(settings, host: str, port: int, start_engine: bool = True) -> int:
    import uvicorn

    from app.api import create_app
    from app.api.context import AppContext

    ctx = AppContext(settings, start_engine=start_engine)
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
