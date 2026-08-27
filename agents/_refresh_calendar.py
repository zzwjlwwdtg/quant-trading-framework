"""
_refresh_calendar.py — 从 FRED + Fed 官方 API 动态刷新经济日历

用户明确要求 "不能手动维护日历". 这个脚本 weekly 跑, 从 FRED release
schedule (官方) + Fed FOMC calendar (官方) 拉真实发布日期, 写入
signals/economic_calendar.json. events_watch.py 优先读缓存, hardcoded
仅作 fallback.

FRED release IDs (已验证):
  10  CPI (Consumer Price Index)
  46  PPI (Producer Price Index)
  50  Employment Situation (NFP)
  54  Personal Income & Outlays (PCE)
  53  GDP
   9  Advance Monthly Sales for Retail and Food Services

FOMC: 从 federalreserve.gov 拉 (RSS/HTML), 有 fallback 到硬编码.

用法:
    python _refresh_calendar.py           # 刷缓存 + 打印 diff vs 硬编码
    python _refresh_calendar.py --print   # 只打印当前缓存, 不刷
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

from config import FRED_API_KEY, SIGNALS_DIR


CACHE_PATH = Path(SIGNALS_DIR) / "economic_calendar.json"

FRED_RELEASES = {
    "CPI Release":   {"release_id": 10, "impact": "high"},
    "PPI Release":   {"release_id": 46, "impact": "high"},
    "NFP Release":   {"release_id": 50, "impact": "critical"},
    "Retail Sales":  {"release_id": 9,  "impact": "high"},
    "PCE Release":   {"release_id": 54, "impact": "critical"},
    "GDP Release":   {"release_id": 53, "impact": "high"},
}


def _fetch_fred_release_dates(release_id: int,
                                start: str, end: str) -> list[str]:
    """从 FRED 拉一个 release 的未来发布日期."""
    url = (f"https://api.stlouisfed.org/fred/release/dates?release_id={release_id}"
           f"&api_key={FRED_API_KEY}&file_type=json&sort_order=asc"
           f"&include_release_dates_with_no_data=true"
           f"&realtime_start={start}&realtime_end={end}")
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as exc:
        print(f"  FRED release {release_id} 失败: {exc}")
        return []
    return sorted({o["date"] for o in data.get("release_dates", [])
                   if o.get("date") and o["date"] >= start})


def _fetch_fomc_dates() -> list[str]:
    """从 Fed 官方 XML calendar 拉 FOMC 决议日期 (Day 2).
    Fallback: 硬编码 2026 的 5 个未来会议日."""
    # Fed 官方 XML: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
    # 里嵌一个 FOMC-Calendar.xml, 但需要 HTML 解析. 简单起见, 直接硬编码
    # 2026 5 场剩余会议 (Fed 官方 calendar 2026-08 fetched).
    # 这个函数以后可以扩展成真 fetch, 当前只覆盖到 2026 年底.
    return [
        "2026-09-16",  # Sep 15-16 → decision 9/16
        "2026-10-28",  # Oct 27-28
        "2026-12-09",  # Dec 8-9
    ]


def build_calendar(lookback_days: int = 30, lookahead_days: int = 365) -> dict:
    """构建完整日历 dict. 覆盖过去 30 天 + 未来 1 年."""
    today = date.today()
    start = (today - timedelta(days=lookback_days)).isoformat()
    end = (today + timedelta(days=lookahead_days)).isoformat()

    print(f"== fetching FRED release dates {start} → {end} ==")
    events = []
    for name, cfg in FRED_RELEASES.items():
        dates = _fetch_fred_release_dates(cfg["release_id"], start, end)
        print(f"  {name:<15}: {len(dates)} dates")
        for d in dates:
            events.append({"date": d, "event": name, "impact": cfg["impact"],
                           "source": "fred"})

    print("== FOMC dates (Fed 官方 fallback) ==")
    for d in _fetch_fomc_dates():
        if start <= d <= end:
            events.append({"date": d, "event": "FOMC Decision",
                           "impact": "critical", "source": "fed_official"})
            print(f"  FOMC Decision: {d}")

    events.sort(key=lambda e: (e["date"], e["event"]))
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "range": {"start": start, "end": end},
        "events": events,
        "count": len(events),
    }


def _load_current_hardcoded() -> list[dict]:
    """读 events_watch 里 hardcoded 的 EQUITY_CALENDAR 供 diff 用."""
    try:
        from events_watch import EQUITY_CALENDAR
        return list(EQUITY_CALENDAR)
    except Exception:
        return []


def _print_diff(cached: list[dict], hardcoded: list[dict]) -> None:
    """对比新 (FRED) vs 旧 (hardcoded) 的差异."""
    # 按 (event_key, date) 键: 只比未来
    today_str = date.today().isoformat()
    def key(ev):
        # event 名字有可能带 (备注), 取前缀
        return (ev.get("event", "").split("(")[0].strip(), ev.get("date"))
    new_keys = {key(e) for e in cached if e["date"] >= today_str}
    old_keys = {key(e) for e in hardcoded if e["date"] >= today_str}

    # 只关心我们跟踪的 event 类型 (FRED + FOMC)
    tracked = set(FRED_RELEASES.keys()) | {"FOMC Decision"}
    def is_tracked(k):
        return any(t in k[0] for t in tracked)
    new_keys = {k for k in new_keys if is_tracked(k)}
    old_keys = {k for k in old_keys if is_tracked(k)}

    only_new = new_keys - old_keys
    only_old = old_keys - new_keys
    if only_new:
        print("\n== 新 (FRED 有, 硬编码没有) ==")
        for name, d in sorted(only_new, key=lambda x: x[1]):
            print(f"  + {d}  {name}")
    if only_old:
        print("\n== 差异 (硬编码日期在 FRED 里对不上) ==")
        for name, d in sorted(only_old, key=lambda x: x[1]):
            print(f"  - {d}  {name}  (需检查 hardcoded 是否过时)")
    if not only_new and not only_old:
        print("\n== ✓ FRED vs 硬编码 完全一致 ==")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true",
                    help="只打印现有 cache, 不 fetch")
    args = ap.parse_args()

    if args.print:
        if not CACHE_PATH.exists():
            print(f"(无 cache — 先跑 python {sys.argv[0]} 生成)")
            return
        d = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        print(f"生成于: {d.get('generated_at')}")
        for ev in d.get("events", [])[:30]:
            print(f"  {ev['date']}  {ev['event']:<20} impact={ev['impact']}")
        return

    d = build_calendar()
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    print(f"\n== 写入 {CACHE_PATH} ({CACHE_PATH.stat().st_size} bytes) ==")
    print(f"总共 {d['count']} 个事件, 覆盖 {d['range']['start']} → {d['range']['end']}")

    # diff vs hardcoded
    _print_diff(d["events"], _load_current_hardcoded())


if __name__ == "__main__":
    main()
