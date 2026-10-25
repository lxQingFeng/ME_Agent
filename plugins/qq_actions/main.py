import sys, json

# UTF-8 pipe contract with the host process (registry writes/reads UTF-8
# bytes; without this a GBK-locale Windows console would corrupt I/O).
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
data = json.loads(sys.stdin.read() or "{}")
ctx = data.get("context") or {}
print(json.dumps({
    "result": "戳回去了",
    "actions": [{"type": "poke", "chat_id": ctx.get("chat_id"), "chat_type": ctx.get("chat_type", "group"),
                 "user_id": ctx.get("sender_id")}],
}, ensure_ascii=False))
