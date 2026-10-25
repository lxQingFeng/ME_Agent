import sys, json, random, re
data = json.loads(sys.stdin.read() or "{}")
expr = (data.get("params") or {}).get("expr", "1d6")
m = re.match(r"^(\d{1,2})d(\d{1,4})$", expr.strip())
if not m:
    print(json.dumps({"result": "表达式要像 2d6 这样"}, ensure_ascii=False)); sys.exit(0)
n, s = int(m.group(1)), int(m.group(2))
n = max(1, min(n, 20)); s = max(2, min(s, 1000))
rolls = [random.randint(1, s) for _ in range(n)]
total = sum(rolls)
detail = "+".join(str(r) for r in rolls)
print(json.dumps({"result": f"{n}d{s}: {detail} = {total}"}, ensure_ascii=False))
