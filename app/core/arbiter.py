"""仲裁器:事件分级 P0-P4 / 突发合并 / 延迟决策 / 醒来补看。
LLM 并发=1(asyncio.Lock),世界心跳让路规则见方案 §5。"""
from __future__ import annotations
import asyncio
import time

class Arbiter:
    def __init__(self, settings, engine, pipeline, store, bus, outbound):
        self.settings = settings
        self.engine = engine
        self.pipeline = pipeline
        self.store = store
        self.bus = bus
        self.outbound = outbound
        self.llm_lock = asyncio.Lock()
        self._burst: dict[str, dict] = {}     # chat_id -> {envs, task}
        self._interest: dict[str, list] = {}  # chat_id -> [(ts, text)]
        bus.subscribe("onebot.message", self._on_message_evt)
        bus.subscribe("engine.wake", self._on_wake)

    async def _on_message_evt(self, evt: dict) -> None:
        env = evt.get("data")
        if isinstance(env, dict):
            await self.on_message(env)
    async def on_message(self, env: dict) -> None:
        priority = self.classify(env)
        env["priority"] = priority
        if priority == "P2":
            self.engine.observe(env)
            # P2 原料入会话库(供 retain 与上下文)
            self.store.append_message(ts=float(env.get("ts") or time.time()),
                                      chat_id=env["chat_id"], chat_type=env["chat_type"],
                                      sender_id=env["sender_id"], sender_name=env["sender_name"],
                                      role="user", content=env["text"], segments=env["segments"],
                                      origin="onebot_p2")
            self._track_interest(env)
            return
        if priority == "P1":
            self._track_interest(env)
            self.engine.observe(env)
            await self._maybe_interject(env)
            return
        # P0: 突发合并(400ms 滚动窗)
        merged = await self._burst_merge(env)
        if merged is None:
            return
        attend = self.engine.attend(merged)
        merged["attend"] = attend
        if attend["respond"] == "catchup":
            q = self.store.kv_get(f"catchup:{merged['chat_id']}", [])
            q.append({"sender_name": merged["sender_name"], "text": merged["text"], "ts": merged["ts"]})
            self.store.kv_set(f"catchup:{merged['chat_id']}", q)
            return
        if attend.get("delay_s"):
            await asyncio.sleep(min(float(attend["delay_s"]), 1.0 if self._virtual() else 3600))
        async with self.llm_lock:
            await self.pipeline.handle(merged, attend)

    def _virtual(self) -> bool:
        return getattr(self.engine.clock, "advance", None) is not None

    def classify(self, env: dict) -> str:
        if env["chat_type"] == "private" or env.get("at_me"):
            return "P0"
        text = env.get("text") or ""
        name = self.settings.persona.get("name", "")
        hot = self.engine.state.social.density >= 6
        if name and name in text:
            return "P1"
        if hot:
            return "P1"
        return "P2"

    async def _burst_merge(self, env: dict) -> dict | None:
        """同 chat 400ms 滚动合并, 上限 8 条/2.5s。返回合并后的信封(仅首条触发者拿到)。"""
        chat = env["chat_id"]
        slot = self._burst.get(chat)
        now = time.time()
        overflow: list[dict] | None = None
        if slot is None or now - slot["last"] > 2.5 or len(slot["envs"]) >= 8:
            if slot is not None:
                slot["task"].cancel()
                if slot["envs"]:
                    overflow = slot["envs"]  # 满仓/过期换仓: 已积压消息必须先放行, 不能静默丢弃
            slot = {"envs": [], "last": now, "task": None}
            self._burst[chat] = slot
        if overflow:
            base = overflow[0]
            if len(overflow) > 1:
                base = dict(base)
                base["text"] = " / ".join(e["text"] for e in overflow if e.get("text"))
                base["merged_count"] = len(overflow)
            await self._route_merged(base)
        slot["envs"].append(env)
        slot["last"] = now
        if slot["task"]:
            slot["task"].cancel()
        merged_holder: dict = {}

        async def flush() -> None:
            await asyncio.sleep(0.4 if not self._virtual() else 0)
            s = self._burst.get(chat)
            if s is not slot:
                return
            envs = slot["envs"]
            self._burst.pop(chat, None)
            if not envs:
                return
            base = envs[0]
            if len(envs) > 1:
                base = dict(base)
                base["text"] = " / ".join(e["text"] for e in envs if e.get("text"))
                base["merged_count"] = len(envs)
            merged_holder["env"] = base
            await self._route_merged(base)

        slot["task"] = asyncio.get_event_loop().create_task(flush())
        return None

    async def _route_merged(self, env: dict) -> None:
        attend = self.engine.attend(env)
        env["attend"] = attend
        if attend["respond"] == "catchup":
            q = self.store.kv_get(f"catchup:{env['chat_id']}", [])
            q.append({"sender_name": env["sender_name"], "text": env["text"], "ts": env["ts"]})
            self.store.kv_set(f"catchup:{env['chat_id']}", q)
            return
        if attend.get("delay_s") and not self._virtual():
            await asyncio.sleep(float(attend["delay_s"]))
        async with self.llm_lock:
            await self.pipeline.handle(env, attend)

    def _track_interest(self, env: dict) -> None:
        chat = env["chat_id"]
        lst = self._interest.setdefault(chat, [])
        lst.append((time.time(), env.get("text") or ""))
        cutoff = time.time() - 1800
        self._interest[chat] = [(t, x) for t, x in lst if t > cutoff][-20:]

    async def _maybe_interject(self, env: dict) -> None:
        """P1 迟滞:30 分钟内 ≥2 次点名/热议才考虑插话。"""
        chat = env["chat_id"]
        lst = self._interest.get(chat, [])
        if len(lst) < 2 or self.engine.state.sleep_state != "awake":
            return
        if self.engine.state.social.battery < 25:
            return
        name = self.settings.persona.get("name", "")
        mentions = sum(1 for _, t in lst if name and name in t)
        if mentions < 2:
            return
        self._interest[chat] = []
        async with self.llm_lock:
            try:
                await self.pipeline.handle(dict(env, at_me=True), {"respond": "now", "delay_s": 0})
            except Exception:
                pass

    async def _on_wake(self, evt: dict) -> None:
        for chat in list(self._interest.keys()):
            try:
                await self.pipeline.handle_catchup(chat)
            except Exception:
                pass

    async def handle_broadcast_candidate(self, text: str, ctx: dict) -> None:
        """P3:引擎冒泡候选 → 限额决定 → LLM 润色(可用时) → 输出。"""
        ok, why = self.outbound.broadcast_allowed()
        if not ok:
            return
        final = text
        if self.pipeline.llm is not None and self.pipeline.llm.enabled:
            try:
                res = await self.pipeline.llm.chat([
                    {"role": "system",
                     "content": "你在群里自然地冒个泡(1-2 句,像随手发的),不要@人,不要问句结尾。"},
                    {"role": "user", "content": f"当前状态参考:\n{text}"}])
                final = (res.get("content") or "").strip() or text
            except Exception:
                pass
        chats = [c["chat_id"] for c in self.store.chats() if c["chat_type"] == "group"]
        for chat in chats[:1]:
            await self.outbound.broadcast(chat, final)
