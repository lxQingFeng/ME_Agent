import sys, json, os, urllib.request, urllib.parse

# UTF-8 pipe contract with the host process (registry writes/reads UTF-8
# bytes; without this a GBK-locale Windows console would corrupt I/O).
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
data = json.loads(sys.stdin.read() or "{}")
q = (data.get("params") or {}).get("query", "").strip()
base = os.environ.get("SEARXNG_BASE", "http://127.0.0.1:8889")
if not q:
    print(json.dumps({"result": "搜索词是空的"}, ensure_ascii=False)); sys.exit(0)
try:
    u = f"{base.rstrip('/')}/search?q={urllib.parse.quote(q)}&format=json"
    r = json.loads(urllib.request.urlopen(u, timeout=12).read())
    items = (r.get("results") or [])[:4]
    if not items:
        print(json.dumps({"result": f"搜 {q} 没啥结果"}, ensure_ascii=False))
    else:
        lines = [f"{i+1}. {it.get('title','')} — {it.get('content','')[:80]}" for i, it in enumerate(items)]
        print(json.dumps({"result": "\n".join(lines)}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"result": f"搜索服务连不上({base}): {e}"}, ensure_ascii=False))
