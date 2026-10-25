import sys, json, os, urllib.request
data = json.loads(sys.stdin.read() or "{}")
p = data.get("params") or {}
ctx = data.get("context") or {}
url = os.environ.get("ME_CONSOLE_URL", "http://127.0.0.1:8080")
token = os.environ.get("ME_INTERNAL_TOKEN", "")
body = json.dumps({"text": p.get("text", ""), "voice": p.get("voice"),
                   "chat_id": ctx.get("chat_id"), "chat_type": ctx.get("chat_type")}).encode()
req = urllib.request.Request(url + "/api/internal/tts/bridge", data=body,
                             headers={"Content-Type": "application/json", "X-Internal-Token": token})
try:
    r = json.loads(urllib.request.urlopen(req, timeout=55).read())
    if r.get("ok") and r.get("sent"):
        print(json.dumps({"result": f"(语音已发出: {p.get('text','')[:30]}…)"}, ensure_ascii=False))
    elif r.get("ok"):
        print(json.dumps({"result": f"(合成成功但未发送: {r.get('detail','')})"}, ensure_ascii=False))
    else:
        print(json.dumps({"result": f"TTS 失败: {r.get('error') or r.get('detail')}"}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"result": f"TTS 桥接不可用: {e}"}, ensure_ascii=False))
