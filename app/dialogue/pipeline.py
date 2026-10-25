"""对话管道(P0 全流程):去重→上下文→LLM(≤2 轮工具)→整形→输出→记忆。"""
from __future__ import annotations
import asyncio
import hashlib
import time
import random

class DialoguePipeline:
    def __init__(self, settings, engine, llm, memory, plugins, outbound, store, renderer, bus,
                 tts=None):
        self.settings = settings
        self.engine = engine
        self.llm = llm
        self.memory = memory
        self.plugins = plugins
        self.outbound = outbound
        self.store = store
        self.renderer = renderer
        self.bus = bus
        self.tts = tts          # TTSService(总装注入; speak 工具用)

    async def handle(self, env: dict, attend: dict | None = None) -> None:
        chat_id = env["chat_id"]
        # 记录用户消息
        self.store.append_message(ts=float(env.get("ts") or time.time()), chat_id=chat_id,
                                  chat_type=env["chat_type"], sender_id=env["sender_id"],
                                  sender_name=env["sender_name"], role="user",
                                  content=env["text"], segments=env["segments"], origin="onebot")
        self.bus.emit("message.in", env)
        # 同题去重
        h = hashlib.md5((chat_id + "|" + (env["text"] or "").lower().strip()).encode()).hexdigest()
        window = int(self.settings.shaping.get("dedup_window_minutes", 3)) * 60
        if self.store.dedup_check(h, window):
            await self.outbound.send(chat_id, "嗯刚说过这事,再说一遍?"[:40], reply_to=env.get("message_id"))
            return
        # 生成
        reply = await self._generate(env)
        if not reply:
            return
        await self.outbound.send(chat_id, reply, reply_to=env.get("message_id"))
        # 世界侧:回复完成
        self.engine.note_reply_done()
        # 记忆:会话 retain(重要度启发)
        await self._retain_dialogue(env, reply)

    async def _generate(self, env: dict) -> str:
        from app.dialogue.context import ContextBuilder
        from app.dialogue.style import modulate
        ctx = ContextBuilder(self.settings)
        history = ctx.history(self.store.recent_messages(env["chat_id"], limit=24))
        memories = []
        if self.memory is not None:
            try:
                memories = await asyncio.wait_for(
                    self.memory.recall(env["text"], top_k=8), timeout=0.8)
            except Exception:
                memories = []
        world = self.renderer.render(self.engine.state, None)
        persona_name = self.settings.persona.get("name", "小默")
        messages = [{"role": "system",
                     "content": ctx.system_prompt(world, memories, persona_name)}]
        messages.extend(history[:-1])
        messages.append({"role": "user", "content": ctx.user_message(env)})
        reply = ""
        if self.llm is not None and self.llm.enabled:
            tools = self.plugins.tools() if self.plugins else None
            res = await self.llm.chat(messages, tools=tools)
            reply = (res.get("content") or "").strip()
            rounds = 0
            while res.get("tool_calls") and rounds < int(self.settings.llm_cfg.get("max_tool_rounds", 2)):
                rounds += 1
                obs_lines = []
                for call in res["tool_calls"]:
                    out = await self._dispatch_tool(call["name"], call["arguments"], env)
                    obs_lines.append(f"{call['name']} -> {out}")
                messages.append({"role": "assistant", "content": reply or "(调用工具)",
                                 "tool_calls": [{"id": str(i), "type": "function",
                                                 "function": {"name": c["name"],
                                                              "arguments": __import__("json").dumps(c["arguments"], ensure_ascii=False)}}
                                                for i, c in enumerate(res["tool_calls"])]})
                messages.append({"role": "user", "content": "工具结果:\n" + "\n".join(obs_lines)})
                res = await self.llm.chat(messages, tools=tools)
                reply = (res.get("content") or "").strip()
        if not reply:
            # 模板兜底(无 LLM/失败)
            fallbacks = self.settings.persona.get("reply_fallbacks", ["嗯,在"])
            disp = self.engine.activities.display(self.engine.state.current.id)
            reply = random.choice(fallbacks).replace("{activity}", disp)
        return modulate(reply, self.engine.state)

    async def _dispatch_tool(self, name: str, args: dict, env: dict) -> str:
        if self.plugins is None:
            return "no plugin system"
        context = {"chat_id": env["chat_id"], "chat_type": env["chat_type"],
                   "sender_id": env["sender_id"], "sender_name": env["sender_name"],
                   "message_id": env.get("message_id")}
        if name == "speak":
            # TTS 桥接(语音消息), 无插件系统时也可用
            from app.tts.bridge import speak_tool_invoke
            return await speak_tool_invoke(self.tts, self.outbound, args, context)
        try:
            result = await self.plugins.invoke(name, args, context)
            return str(result)[:400]
        except Exception as e:
            return f"error: {e!r}"

    async def _retain_dialogue(self, env: dict, reply: str) -> None:
        if self.memory is None:
            return
        importance = self._importance(env, reply)
        threshold = int(self.settings.memory.get("retain", {}).get("immediate_importance_min", 2))
        if importance >= threshold:
            content = f"{env['sender_name']}: {env['text']}\n小默: {reply}"
            try:
                await self.memory.retain(content, kind="dialogue",
                                         metadata={"chat_id": env["chat_id"],
                                                   "sender": env["sender_name"],
                                                   "importance": importance})
            except Exception:
                pass

    def _importance(self, env: dict, reply: str) -> int:
        text = (env.get("text") or "") + reply
        score = 1
        for kw in ("记住", "我叫", "我是", "生日", "喜欢", "讨厌", "偏好", "重要"):
            if kw in text:
                score = 2
                break
        if len(env.get("text") or "") > 80:
            score += 1
        if "@" in (env.get("text") or ""):
            score += 0
        return min(score, 3)

    async def handle_catchup(self, chat_id: str) -> None:
        """醒来补看:攒的消息总结成一句(有 LLM 用 LLM,没有就模板)。"""
        queue = self.store.kv_get(f"catchup:{chat_id}", [])
        if not queue:
            return
        self.store.kv_set(f"catchup:{chat_id}", [])
        names = {m.get("sender_name") for m in queue}
        texts = [m.get("text", "") for m in queue if m.get("text")][:12]
        summary = ""
        if self.llm is not None and self.llm.enabled:
            joined = "\n".join(f"{m.get('sender_name')}: {m.get('text','')}" for m in queue[:20])
            res = await self.llm.chat([
                {"role": "system", "content": "你在睡觉时攒了这些群消息, 醒来用一两句口语概括一下都聊了啥(提及人名), 语气自然:"},
                {"role": "user", "content": joined}])
            summary = (res.get("content") or "").strip()
        if not summary:
            summary = f"醒了,翻了下消息——{', '.join(n for n in names if n)}聊了{len(texts)}条,我看看。"
        await self.outbound.send(chat_id, summary, origin="catchup")
