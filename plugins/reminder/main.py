import sys, json, time, os
STORE = os.path.join(os.path.dirname(__file__), "tasks.json")
def load():
    if os.path.exists(STORE):
        return json.loads(open(STORE, encoding="utf-8").read())
    return []
def save(t):
    json.dump(t, open(STORE, "w", encoding="utf-8"), ensure_ascii=False)
data = json.loads(sys.stdin.read() or "{}")
op = data.get("op", "invoke")
ctx = data.get("context") or {}
tasks = load()
if op == "invoke":
    p = data.get("params") or {}
    minutes = max(1, min(int(p.get("minutes", 5)), 1440))
    tasks.append({"due": time.time() + minutes * 60, "content": p.get("content", ""),
                  "chat_id": ctx.get("chat_id"), "chat_type": ctx.get("chat_type")})
    save(tasks)
    print(json.dumps({"result": f"记下了,{minutes}分钟后提醒你:{p.get('content','')}"}, ensure_ascii=False))
else:
    now = time.time()
    due = [t for t in tasks if t["due"] <= now]
    rest = [t for t in tasks if t["due"] > now]
    save(rest)
    actions = [{"type": "send", "chat_id": t["chat_id"], "chat_type": t.get("chat_type", "group"),
                "text": f"⏰ 提醒:{t['content']}"} for t in due if t.get("chat_id")]
    print(json.dumps({"condition_met": bool(due), "actions": actions}, ensure_ascii=False))
