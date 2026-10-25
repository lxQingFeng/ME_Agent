#!/usr/bin/env python3
"""compose/配置静态校验(沙箱无 docker, 交付前必跑)。

校验 deploy/hindsight/docker-compose.yml 与 config/memory.yaml、app/memory/hindsight.py
三者的端口/端点/环境变量/双 bank/webhook 回连是否一致对齐。
运行: python3 deploy/hindsight/check_compose.py   (仓库根)
退出码 0 = 全部通过。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  | {detail}" if detail and not ok else ""))


def main() -> int:
    print("== Hindsight 编排静态校验 ==")
    compose = yaml.safe_load((ROOT / "deploy/hindsight/docker-compose.yml").read_text("utf-8"))
    mem = yaml.safe_load((ROOT / "config/memory.yaml").read_text("utf-8"))
    hs = mem.get("hindsight", {})

    # ---------- compose 本体 ----------
    svc = compose["services"]["hindsight"]
    check("C1 compose 语法可解析且含 hindsight 服务", isinstance(svc, dict))
    check("C2 官方镜像 ghcr.io/vectorize-io/hindsight",
          str(svc.get("image", "")).startswith("ghcr.io/vectorize-io/hindsight"), svc.get("image"))
    def _port(p) -> str:
        return f"{p['published']}:{p['target']}" if isinstance(p, dict) else str(p)
    ports = {_port(p) for p in svc.get("ports", [])}
    check("C3 端口 8888(API)+9999(UI) 对外开放",
          "8888:8888" in ports and "9999:9999" in ports, str(ports))
    def _vol(v) -> str:
        return (v.get("target") or v.get("Target", "")) if isinstance(v, dict) else str(v).split(":")[-1]
    vols = {_vol(v) for v in svc.get("volumes", [])}
    check("C4 持久化卷挂官方路径 /home/hindsight/.pg0", "/home/hindsight/.pg0" in vols, str(vols))
    env: dict[str, str] = {}
    raw_env = svc.get("environment", [])
    if isinstance(raw_env, dict):
        env.update({str(k): str(v) for k, v in raw_env.items()})
    else:
        for e in raw_env:
            if isinstance(e, dict):
                env.update({str(k): str(v) for k, v in e.items()})
            else:
                k, _, v = str(e).partition("=")
                env[k] = v
    check("C5 LLM provider/key 环境变量名对齐官方",
          "HINDSIGHT_API_LLM_PROVIDER" in env and "HINDSIGHT_API_LLM_API_KEY" in env, str(list(env)))
    check("C6 key 复用 DEEPSEEK_API_KEY 注入(默认缺省不炸)",
          env.get("HINDSIGHT_API_LLM_API_KEY") == "${DEEPSEEK_API_KEY:-}", env.get("HINDSIGHT_API_LLM_API_KEY"))
    eh = [x for x in svc.get("extra_hosts", []) if x.startswith("host.docker.internal")]
    check("C7 webhook 回连宿主机通道(host.docker.internal)", bool(eh), str(svc.get("extra_hosts")))
    check("C8 重启策略 unless-stopped", svc.get("restart") == "unless-stopped")
    check("C9 资源上限 mem_limit 4g", svc.get("mem_limit") == "4g", str(svc.get("mem_limit")))

    # ---------- memory.yaml ↔ 客户端 ----------
    py = (ROOT / "app/memory/hindsight.py").read_text("utf-8")
    eps = hs.get("endpoints", {})
    check("M1 retain 端点官方形态(banks/{bank}/memories)",
          eps.get("retain") == "/v1/default/banks/{bank}/memories", str(eps.get("retain")))
    check("M2 recall 端点(memories/recall)",
          eps.get("recall") == "/v1/default/banks/{bank}/memories/recall", str(eps.get("recall")))
    check("M3 reflect 端点", eps.get("reflect") == "/v1/default/banks/{bank}/reflect", str(eps.get("reflect")))
    check("M4 banks/config/webhooks 端点齐备",
          eps.get("banks") == "/v1/default/banks" and "config" in eps and "webhooks" in eps, str(eps))
    check("M5 health 端点", eps.get("health") == "/health", str(eps.get("health")))
    # 客户端默认值与 yaml 一致(防漂移)
    check("M6 客户端默认 retain/recall 与 yaml 同步",
          '"/v1/default/banks/{bank}/memories"' in py
          and '"/v1/default/banks/{bank}/memories/recall"' in py)
    check("M7 客户端把 {bank} 模板实际代入", ".format(bank=" in py)
    check("M8 retain 请求体 items 形态(官方 RetainRequest)", '"items":' in py and '"content":' in py)
    check("M9 recall 请求体 query/max_tokens(无 top_k 野字段)", '"max_tokens":' in py)

    # ---------- 双 bank / webhook ----------
    banks = hs.get("banks", {})
    check("B1 双 bank: qq-agent(对话)+knowledge(知识)",
          banks.get("dialogue") == "qq-agent" and banks.get("knowledge") == "knowledge", str(banks))
    check("B2 ensure_banks 幂等下发双 bank", '"dialogue", "knowledge"' in py or
          '("dialogue", "knowledge")' in py)
    wh = hs.get("webhook", {})
    check("W1 webhook 回连 URL 指向 ME_Agent 接收路由",
          wh.get("url", "").endswith("/webhooks/hindsight"), str(wh.get("url")))
    check("W2 官方事件类型三种齐备",
          set(wh.get("event_types", [])) >= {"retain.completed", "consolidation.completed",
                                             "memory_defense.triggered"}, str(wh.get("event_types")))
    wk = (ROOT / "app/memory/webhooks.py").read_text("utf-8")
    check("W3 接收端路由与 URL 路径一致", '"/webhooks/hindsight"' in wk)
    check("W4 接收端验官方签名头 X-Hindsight-Signature", "x-hindsight-signature" in wk)
    check("W5 接收端兼容 bank_id/timestamp 官方载荷字段", "bank_id" in wk and "timestamp" in wk)
    check("W6 注册端调用 register_webhook", "register_webhook" in py)

    # ---------- ME_Agent 侧端口与 base_url ----------
    console = yaml.safe_load((ROOT / "config/console.yaml").read_text("utf-8"))
    port = console.get("api", {}).get("port", 8000)
    check("P1 webhook URL 端口 = ME_Agent API 端口",
          f":{port}/webhooks" in wh.get("url", ""), f"url={wh.get('url')} port={port}")
    check("P2 memory base_url 指向 compose 暴露的 8888",
          str(hs.get("base_url", "")).endswith(":8888"), str(hs.get("base_url")))

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n静态校验: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS" +
          ("" if not failed else f", FAIL: {[r[0] for r in failed]}"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
