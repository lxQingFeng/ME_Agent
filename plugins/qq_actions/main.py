import sys, json
data = json.loads(sys.stdin.read() or "{}")
ctx = data.get("context") or {}
print(json.dumps({
    "result": "戳回去了",
    "actions": [{"type": "poke", "chat_id": ctx.get("chat_id"), "chat_type": ctx.get("chat_type", "group"),
                 "user_id": ctx.get("sender_id")}],
}, ensure_ascii=False))
