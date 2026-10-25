import sys, json, os, urllib.request

# UTF-8 pipe contract with the host process (registry writes/reads UTF-8
# bytes; without this a GBK-locale Windows console would corrupt I/O).
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
data = json.loads(sys.stdin.read() or "{}")
p = data.get("params") or {}
text = (p.get("text") or "").strip()
to = p.get("to") or "中文"
url = os.environ.get("ME_CONSOLE_URL", "http://127.0.0.1:8080")
token = os.environ.get("ME_INTERNAL_TOKEN", "")
if not text:
    print(json.dumps({"result": "没给要翻译的文本"}, ensure_ascii=False)); sys.exit(0)
body = json.dumps({"text": text, "to": to}).encode()
req = urllib.request.Request(url + "/api/internal/llm/translate", data=body,
                             headers={"Content-Type": "application/json", "X-Internal-Token": token})
try:
    r = json.loads(urllib.request.urlopen(req, timeout=25).read())
    print(json.dumps({"result": r.get("translation") or r.get("result") or "翻译失败"}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"result": f"翻译通道不可用: {e}"}, ensure_ascii=False))
