"""输出整形(规则层,LLM 之后再过一道)。"""
from __future__ import annotations

def modulate(reply: str, state) -> str:
    if not reply:
        return reply
    reply = reply.strip().strip('"').strip("“”")
    # 状态调制:能量低→逼短
    if state.body.energy < 20 and len(reply) > 60:
        reply = _cut(reply, 60)
    if state.mood.valence < -0.4:
        reply = reply.replace("哈哈哈", "哈").replace("!", "。")
    return reply

def _cut(text: str, n: int) -> str:
    for sep in ("。", "!", "?", " ", "~"):
        i = text.rfind(sep, 0, n)
        if i > 10:
            return text[: i + 1]
    return text[:n]
