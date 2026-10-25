import sys, json, urllib.request

# UTF-8 pipe contract with the host process (registry writes/reads UTF-8
# bytes; without this a GBK-locale Windows console would corrupt I/O).
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
BAD = ["rain", "snow", "thunderstorm", "shower", "drizzle"]
def get(city: str):
    geo = urllib.request.urlopen(
        f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city)}&count=1&language=zh", timeout=8).read()
    results = json.loads(geo).get("results") or []
    if not results:
        return {"error": f"没找到 {city}"}
    r = results[0]
    w = json.loads(urllib.request.urlopen(
        f"https://api.open-meteo.com/v1/forecast?latitude={r['latitude']}&longitude={r['longitude']}"
        "&current=temperature_2m,weather_code,wind_speed_10m&timezone=auto", timeout=8).read())
    cur = w.get("current", {})
    code = cur.get("weather_code", 0)
    descs = {0: "晴", 1: "基本晴", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
             51: "毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 71: "小雪", 73: "中雪", 75: "大雪",
             80: "阵雨", 81: "强阵雨", 82: "暴雨", 95: "雷雨", 96: "雷雨伴冰雹", 99: "强雷雨"}
    desc = descs.get(code, f"代码{code}")
    return {"city": r.get("name", city), "temp": cur.get("temperature_2m"), "desc": desc,
            "wind": cur.get("wind_speed_10m"), "condition": "rain" if any(b in desc for b in ["雨"]) else
                     ("snow" if "雪" in desc else ("thunderstorm" if "雷" in desc else "ok"))}
data = json.loads(sys.stdin.read() or "{}")
op = data.get("op", "invoke")
try:
    city = (data.get("params") or {}).get("city") or "北京"
    w = get(city)
except Exception as e:
    w = {"error": f"天气服务连不上: {e}"}
if op == "invoke":
    if "error" in w:
        print(json.dumps({"result": w["error"]}, ensure_ascii=False))
    else:
        print(json.dumps({"result": f"{w['city']}现在 {w['desc']}, {w['temp']}°C, 风速 {w['wind']}km/h"}, ensure_ascii=False))
else:
    import datetime
    cond = w.get("condition", "ok")
    met = cond in BAD
    text = f"看了眼天气,{w.get('city')}现在{w.get('desc')},{w.get('temp')}°C,出门带伞吧" if met else None
    print(json.dumps({"condition_met": met, "broadcast_text": text}, ensure_ascii=False))
