"""上下文组装器:人设+世界状态+记忆+历史+群规 → LLM 消息序列。"""
from __future__ import annotations

class ContextBuilder:
    def __init__(self, settings):
        self.settings = settings

    def system_prompt(self, world_rendered: str, memories: list[dict] | None,
                      persona_name: str) -> str:
        p = self.settings.persona
        parts = [f"# 你是谁\n{p.get('essence','').strip()}"]
        rules = p.get("style", {}).get("rules", [])
        if rules:
            parts.append("# 说话规矩\n" + "\n".join(f"- {r}" for r in rules))
        parts.append(f"# 你的当前状态(真实反映, 别编)\n{world_rendered}")
        digest = p.get("group_rules_digest", "").strip()
        if digest:
            parts.append(f"# 群规摘要\n{digest}")
        if memories:
            lines = []
            for m in memories[:8]:
                t = m.get("content") or m.get("text") or ""
                lines.append(f"- {t[:160]}" if t else None)
            lines = [x for x in lines if x]
            if lines:
                parts.append("# 相关记忆(检索自长期记忆)\n" + "\n".join(lines))
        parts.append("# 输出要求\n直接给出要发送的消息内容, 不要任何前缀/解释/引号。一次一条,口语短句。")
        return "\n\n".join(parts)

    def history(self, rows) -> list[dict]:
        """DB 消息行 → LLM 对话历史(带名字标注的 user/assistant)。"""
        msgs = []
        for r in rows:
            role = "assistant" if r["role"] == "assistant" else "user"
            who = r["sender_name"] or r["sender_id"]
            content = r["content"] or ""
            if not content:
                continue
            if role == "user":
                content = f"{who}: {content}"
            msgs.append({"role": role, "content": content})
        return msgs

    def user_message(self, env: dict) -> str:
        who = env.get("sender_name") or env.get("sender_id")
        return f"{who}: {env.get('text','')}"
