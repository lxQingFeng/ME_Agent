"""第 11 轮专项自测: LLM/人设设置 API(保存热生效 + 密钥脱敏)。

运行: python3 tests/test_settings.py    (ME_Agent 仓库根, 无需起服务)

覆盖:
  S. LLM 设置: GET 默认回读(无 key 优雅降级) / PUT 保存(覆盖层落 state/settings/,
     git 不追踪) / api_key 三态(新值保存、*** 回传保留、空串清除) / 明文永不回显 /
     校验拒绝(非法 base_url / temperature 越界) / 热生效(客户端重建 + pipeline/
     engine 重接线 + 旧客户端关闭) / 模型列表代理(上游响应解析);
  P. 人设设置: GET 默认 / PUT 保存(名字/性格/风格规则) / 对话管道热生效口径
     (settings.persona 即时更新) / 校验拒绝(空名字、空规则);
  C. 契约卫生: 日志分页端点新字段(total/offset/has_more) / programs 列表新增
     progress/webui 字段 / 未知路由不 500。

退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["PYTHONUTF8"] = "1"
os.environ.pop("DEEPSEEK_API_KEY", None)   # 测试口径: 不受外部环境变量干扰
os.environ["ME_AUTOSTART"] = "0"           # 第 13 轮: 关闭启动自联动, 避免起服时真装/真拉第三方程序

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def _ctx(state: Path):
    from app.api.context import build_context
    return build_context(state_dir=state, start_engine=False)


def test_llm_settings() -> None:
    print("[S] LLM 设置 API: 保存/热生效/脱敏/校验")
    from fastapi.testclient import TestClient
    from app.api import create_app

    state = Path(tempfile.mkdtemp(prefix="me-ts-"))
    ctx = _ctx(state)
    app = create_app(ctx)
    secret = "sk-round11-secret-9876543210"
    try:
        with TestClient(app) as c:
            # S1 默认回读(仓库 config 无 key)
            r = c.get("/api/v1/settings/llm").json()
            check("S1 GET 默认: 脱敏结构齐(base_url/model/来源标记), 无 key → enabled=False",
                  r.get("ok") is True and r.get("base_url") == "https://api.deepseek.com/v1"
                  and r.get("api_key") == "" and r.get("has_api_key") is False
                  and r.get("api_key_source") == "none" and r.get("enabled") is False)

            # S2 保存全量
            old_llm = ctx.llm
            old_pipeline_llm = ctx.pipeline.llm
            r = c.put("/api/v1/settings/llm", json={
                "base_url": "https://api.deepseek.com/v1", "api_key": secret,
                "chat_model": "deepseek-chat", "planner_model": "deepseek-reasoner",
                "provider": "deepseek", "temperature": 0.5, "max_tool_rounds": 3,
                "timeout_s": 60}).json()
            check("S2 PUT 保存: 响应脱敏(api_key=***)+热生效标记",
                  r.get("ok") is True and r.get("api_key") == "***"
                  and r.get("has_api_key") is True and r.get("api_key_source") == "state"
                  and (r.get("applied") or {}).get("enabled") is True, str(r)[:120])
            # S3 覆盖层落盘(git 不追踪) + 明文只在 state 文件
            ov = state / "settings" / "llm.yaml"
            txt = ov.read_text("utf-8") if ov.exists() else ""
            check("S3 覆盖层落盘 state/settings/llm.yaml(明文仅存于此, state/ 已 gitignore)",
                  ov.exists() and secret in txt and str(ov).startswith(str(state)))
            # S4 热生效: 客户端重建 + pipeline/engine 重接线
            check("S4a 热生效: LLM 客户端已重建(enabled=True)",
                  ctx.llm is not old_llm and ctx.llm.enabled is True
                  and ctx.llm_or_none is ctx.llm)
            check("S4b 热生效: pipeline.llm / engine.llm 已重接线",
                  ctx.pipeline.llm is ctx.llm and ctx.engine.llm is ctx.llm
                  and ctx.pipeline.llm is not old_pipeline_llm)
            # S5 GET 回读一致 + 明文不泄漏
            r = c.get("/api/v1/settings/llm").json()
            check("S5 GET 回读: 模型/参数持久生效, 任何响应无明文密钥",
                  r.get("chat_model") == "deepseek-chat"
                  and r.get("planner_model") == "deepseek-reasoner"
                  and r.get("temperature") == 0.5 and r.get("max_tool_rounds") == 3
                  and secret not in str(r))

            # S6 *** 回传保留旧密钥
            r = c.put("/api/v1/settings/llm", json={"api_key": "***", "chat_model": "deepseek-chat2"}).json()
            check("S6 脱敏回传(***): 旧密钥保留, 其他字段照常更新",
                  r.get("api_key") == "***" and r.get("has_api_key") is True
                  and r.get("chat_model") == "deepseek-chat2"
                  and secret in ov.read_text("utf-8"))

            # S7 空串显式清除
            r = c.put("/api/v1/settings/llm", json={"api_key": ""}).json()
            check("S7 空串清除: 回落 none/env(覆盖层密钥删除)",
                  r.get("has_api_key") is False and r.get("api_key_source") == "none"
                  and secret not in ov.read_text("utf-8"))
            # 恢复密钥供后续用例
            c.put("/api/v1/settings/llm", json={"api_key": secret})

            # S8 校验拒绝
            r = c.put("/api/v1/settings/llm", json={"base_url": "ftp://x"})
            check("S8a 非法 base_url → 400", r.status_code == 400)
            r = c.put("/api/v1/settings/llm", json={"temperature": 9.9})
            check("S8b temperature 越界 → 422", r.status_code == 422)
            r = c.put("/api/v1/settings/llm", json={"max_tool_rounds": 0})
            check("S8c max_tool_rounds 越界 → 422", r.status_code == 422)

            # S9 模型列表代理(httpx 客户端桩)
            import app.api.settings as S

            class FakeResp:
                status_code = 200
                text = ""
                def raise_for_status(self):
                    pass
                def json(self):
                    return {"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"},
                                     {"id": "deepseek-chat"}, "junk"]}

            class FakeClient:
                calls: list[str] = []
                def __init__(self, **kw):
                    pass
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *a):
                    return False
                async def get(self, url, headers=None, timeout=None):
                    FakeClient.calls.append(url)
                    return FakeResp()
                async def aclose(self):
                    pass

            orig_cli = S.httpx.AsyncClient
            S.httpx.AsyncClient = FakeClient
            try:
                r = c.post("/api/v1/settings/llm/models", json={}).json()
                check("S9 模型列表代理: 去重排序, 密钥仅后端持有(不走浏览器)",
                      r.get("ok") is True and r.get("models") == ["deepseek-chat", "deepseek-reasoner"]
                      and r.get("count") == 2 and FakeClient.calls
                      and FakeClient.calls[0].startswith("https://api.deepseek.com/v1/models"),
                      str(r)[:120])
            finally:
                S.httpx.AsyncClient = orig_cli

            # S10 密钥清除后的 enabled 联动(供前端提示)
            c.put("/api/v1/settings/llm", json={"api_key": ""})
            r = c.get("/api/v1/settings/llm").json()
            check("S10 清除密钥 → enabled=False(对话管道优雅降级口径)",
                  r.get("enabled") is False)
    finally:
        shutil.rmtree(state, ignore_errors=True)


def test_persona_settings() -> None:
    print("[P] 人设设置 API: 保存 + 对话管道热生效")
    from fastapi.testclient import TestClient
    from app.api import create_app

    state = Path(tempfile.mkdtemp(prefix="me-tp-"))
    ctx = _ctx(state)
    app = create_app(ctx)
    try:
        with TestClient(app) as c:
            r = c.get("/api/v1/settings/persona").json()
            check("P1 GET 默认人设(仓库 config/persona.yaml)",
                  r.get("ok") is True and r.get("name") == "小默"
                  and len(r.get("style", {}).get("rules", [])) >= 5)

            r = c.put("/api/v1/settings/persona", json={
                "name": "小鲸", "essence": "你是群友小鲸, 说话简短爱用比喻。",
                "style_rules": ["短句", "口语化", "不刷存在感"],
                "reply_fallbacks": ["在呢", "刚看到"]}).json()
            check("P2 PUT 保存: 名字/性格/风格规则落覆盖层 + applied 回执",
                  r.get("ok") is True and r.get("name") == "小鲸"
                  and (r.get("applied") or {}).get("persona_name") == "小鲸")

            # 对话管道热生效口径: 管道每次现取 settings.persona
            check("P3 热生效: settings.persona 即时更新(管道构造提示词时现取)",
                  ctx.settings.persona.get("name") == "小鲸"
                  and ctx.settings.persona.get("essence", "").startswith("你是群友小鲸")
                  and len(ctx.settings.persona.get("style", {}).get("rules", [])) == 3)
            ov = state / "settings" / "persona.yaml"
            check("P4 覆盖层落盘 state/settings/persona.yaml", ov.exists()
                  and "小鲸" in ov.read_text("utf-8"))

            r = c.get("/api/v1/settings/persona").json()
            check("P5 GET 回读一致(覆盖层 > config 默认)",
                  r.get("name") == "小鲸" and r.get("style", {}).get("rules") == ["短句", "口语化", "不刷存在感"])

            r = c.put("/api/v1/settings/persona", json={"name": "   "})
            check("P6a 空名字 → 400", r.status_code == 400)
            r = c.put("/api/v1/settings/persona", json={"style_rules": ["", "  "]})
            check("P6b 空规则列表 → 400", r.status_code == 400)

            # P7: 部分更新(只改名字, 其余字段保留)
            r = c.put("/api/v1/settings/persona", json={"name": "小默2"}).json()
            check("P7 部分更新: 只传 name, 性格/规则保持",
                  r.get("name") == "小默2" and r.get("essence", "").startswith("你是群友小鲸"))
    finally:
        shutil.rmtree(state, ignore_errors=True)


def test_contract_hygiene() -> None:
    print("[C] 契约卫生: 日志分页字段 / programs 列表新字段 / 路由容错")
    from fastapi.testclient import TestClient
    from app.api import create_app

    state = Path(tempfile.mkdtemp(prefix="me-tc-"))
    ctx = _ctx(state)
    app = create_app(ctx)
    try:
        with TestClient(app) as c:
            # 日志文件预置: 首行即 SnowLuma 凭据样式(启动段从 offset=0 可翻)
            logf = state / "programs" / "logs" / "snowluma.log"
            logf.parent.mkdir(parents=True, exist_ok=True)
            logf.write_text("2026-10-25 20:00:01 INFO  [WebUI] initial credentials: "
                            "user=admin password=Round11TokenABC\n", "utf-8")
            r = c.get("/api/v1/programs/snowluma/logs?offset=0&limit=1").json()
            check("C1 日志分页端点: offset=0 命中启动段首行(含凭据行)",
                  r.get("offset") == 0 and r.get("total") >= 1
                  and len(r.get("lines") or []) == 1
                  and "Round11TokenABC" in (r.get("lines") or [""])[0])
            r = c.get("/api/v1/programs/snowluma/logs").json()
            check("C2 日志端点 V4 兼容: 不传 offset → 尾部语义, 结构含 total/has_more",
                  "lines" in r and "total" in r and "has_more" in r and "offset" in r)

            progs = {p["id"]: p for p in c.get("/api/v1/programs").json()}
            check("C3 programs 列表新增 progress/webui 字段(V5 契约)",
                  "progress" in progs.get("snowluma", {})
                  and "webui" in progs.get("snowluma", {}))
            # C4: 未知程序日志 → 200 空页(不 500)
            r = c.get("/api/v1/programs/nosuch/logs")
            check("C4 未知程序日志端点 → 200 空页", r.status_code == 200
                  and r.json().get("total") == 0)
    finally:
        shutil.rmtree(state, ignore_errors=True)


def main() -> int:
    t0 = time.time()
    test_llm_settings()
    test_persona_settings()
    test_contract_hygiene()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n第11轮设置专项: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS" +
          ("" if not failed else f", {len(failed)} FAIL: {[r[0] for r in failed]}") +
          f"  ({time.time() - t0:.0f}s)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
