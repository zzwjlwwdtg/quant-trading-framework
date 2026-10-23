"""机器人接入自检 (只读): 在本机请求 /api/bot/* 和几个常用接口, 结果写 signals/bot_selftest.json."""
import json, time
from pathlib import Path
import requests
OUT = Path(__file__).resolve().parent / "signals" / "bot_selftest.json"
res = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
for path in ("/api/bot", "/api/bot/summary", "/api/bot/events?since=2026-10-16T00:00:00%2B09:00&limit=5",
             "/api/bot/brief?slot=morning", "/api/positions", "/api/health"):
    t = time.time()
    try:
        r = requests.get("http://127.0.0.1:8080" + path, timeout=60)
        body = r.json()
        res[path] = {"status": r.status_code, "sec": round(time.time() - t, 2), "keys": sorted(body)[:15] if isinstance(body, dict) else type(body).__name__,
                     "sample": json.dumps(body, ensure_ascii=False, default=str)[:600]}
    except Exception as e:
        res[path] = {"error": repr(e)[:300]}
OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
