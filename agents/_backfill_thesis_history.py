"""_backfill_thesis_history.py — 一次性 seed 180d 历史 thesis snapshot.

从 FRED 拉 daily 数据 (10Y, TIPS, IG OAS, HY OAS, NFCI, RRP, TGA) + monthly CPI/UNRATE,
按日重构 bond_monitor 快照 → 追加到 thesis_history.jsonl

用法:
  python _backfill_thesis_history.py             # 默认 180 天
  python _backfill_thesis_history.py --days 365  # 拉一年
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

from thesis_history import HIST_PATH, _estimate_cut_probability
from bond_monitor import _fetch_fred_series


# FRED series 需要 backfill
_FRED_SERIES = {
    "y10":      "DGS10",       # 10Y nominal yield (%)
    "y30":      "DGS30",       # 30Y nominal yield (%)
    "tips":     "DFII10",      # 10Y TIPS real yield (%)
    "ig_pct":   "BAMLC0A0CM",  # IG OAS (%)
    "hy_pct":   "BAMLH0A0HYM2", # HY OAS (%)
    "nfci":     "NFCI",         # Weekly, but FRED returns daily forward-filled
    "rrp":      "RRPONTSYD",    # Fed Reverse Repo (billions)
    "tga":      "WTREGEN",      # Treasury General Account (millions → billions)
    "cpi_lvl":  "CPIAUCSL",     # CPI level (monthly, seasonally adj)
    "core_lvl": "CPILFESL",     # Core CPI level (monthly, sa)
    "unrate":   "UNRATE",       # Unemployment rate (monthly)
}


def _fetch_daily_map(sid: str, days: int) -> dict:
    """Fetch FRED series, return {date_str: value} dict."""
    rows = _fetch_fred_series(sid, days=days + 30)
    if not rows:
        return {}
    return {r[0]: r[1] for r in rows}


def _cpi_yoy_at(cpi_map: dict, date_str: str) -> float | None:
    """算某日的 CPI YoY: 用最近的月度值 / 12 个月前."""
    try:
        cur_dt = datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return None
    # 找最近月度点
    dates = sorted(cpi_map.keys(), reverse=True)
    latest_v = None; year_ago_v = None
    for d in dates:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
        except Exception:
            continue
        if dt <= cur_dt and latest_v is None:
            latest_v = cpi_map[d]
        year_ago_target = cur_dt - timedelta(days=365)
        if dt <= year_ago_target and year_ago_v is None:
            year_ago_v = cpi_map[d]
            break
    if latest_v and year_ago_v and year_ago_v > 0:
        return round((latest_v / year_ago_v - 1) * 100, 2)
    return None


def _unrate_at(unrate_map: dict, date_str: str) -> float | None:
    """某日之前最近一次 UNRATE."""
    try:
        cur_dt = datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return None
    for d in sorted(unrate_map.keys(), reverse=True):
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            if dt <= cur_dt:
                return round(unrate_map[d], 1)
        except Exception:
            continue
    return None


def backfill(days: int = 180) -> int:
    """Backfill last N days. 返回 append 数量."""
    print(f"[backfill] fetching FRED series ({days}d)…")
    data_maps = {}
    for key, sid in _FRED_SERIES.items():
        print(f"  {key:<10s} <- {sid}")
        data_maps[key] = _fetch_daily_map(sid, days=days)

    # 目标日期集合: 用 y10 (最完整的 daily) 定日历
    dates = sorted(data_maps.get("y10", {}).keys())
    if not dates:
        print("[backfill] no dates from FRED, abort")
        return 0
    # 只保留最近 N 天
    cutoff_dt = datetime.now() - timedelta(days=days)
    dates = [d for d in dates if datetime.strptime(d, "%Y-%m-%d") >= cutoff_dt]
    print(f"[backfill] {len(dates)} trading days to seed")

    # 读现有 jsonl 已存在的日期 (避免重复)
    existing_dates = set()
    if HIST_PATH.exists():
        with HIST_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    existing_dates.add(row.get("date"))
                except Exception:
                    pass

    written = 0
    rows_to_write = []
    for d in dates:
        if d in existing_dates:
            continue
        y10 = data_maps["y10"].get(d)
        if y10 is None:
            continue
        y30 = data_maps["y30"].get(d)
        tips = data_maps["tips"].get(d)
        ig_pct = data_maps["ig_pct"].get(d)
        hy_pct = data_maps["hy_pct"].get(d)
        nfci = data_maps["nfci"].get(d)
        rrp = data_maps["rrp"].get(d)
        tga_raw = data_maps["tga"].get(d)
        row = {
            "ts": f"{d}T12:00:00+00:00",  # 假设收盘时刻
            "date": d,
            "y10": round(y10, 3) if y10 else None,
            "y30": round(y30, 3) if y30 else None,
            "tips": round(tips, 3) if tips else None,
            "cpi_yoy": _cpi_yoy_at(data_maps["cpi_lvl"], d),
            "core_cpi_yoy": _cpi_yoy_at(data_maps["core_lvl"], d),
            "unrate": _unrate_at(data_maps["unrate"], d),
            "ig_bps": round(ig_pct * 100, 1) if ig_pct else None,
            "hy_bps": round(hy_pct * 100, 1) if hy_pct else None,
            "nfci": round(nfci, 3) if nfci is not None else None,
            # RRP/TGA 单位处理
            "rrp_bn": round(rrp, 1) if rrp is not None else None,
            "tga_bn": round(tga_raw / 1000, 1) if tga_raw is not None else None,
            # VIX/DXY/oil/BBI/stablecoin: 无 FRED 历史, 留 None
            "vix": None, "dxy": None, "oil_uso": None, "oil_pct_20d": None,
            "bbi": None, "stablecoin_bn": None, "erp": None,
            "_source": "fred_backfill",
        }
        row["cut_probability_pct"] = _estimate_cut_probability(row)
        rows_to_write.append(row)

    if not rows_to_write:
        print("[backfill] no new dates to seed (all exist)")
        return 0

    # 按日期排序追加
    rows_to_write.sort(key=lambda r: r["date"])
    HIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HIST_PATH.open("a", encoding="utf-8") as f:
        for row in rows_to_write:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    print(f"[backfill] wrote {written} rows to {HIST_PATH.name}")

    # 简报: 头/尾 3 条 + cut probability 范围
    probs = [r["cut_probability_pct"] for r in rows_to_write]
    print(f"[backfill] cut_probability range: {min(probs)} ~ {max(probs)}, mean {sum(probs)//len(probs)}")
    print(f"[backfill] first 3 rows date/cut_prob:")
    for row in rows_to_write[:3]:
        print(f"  {row['date']}: y10={row['y10']} tips={row['tips']} hy={row['hy_bps']} cpi_yoy={row['cpi_yoy']} → cut_prob {row['cut_probability_pct']}%")
    print(f"[backfill] last 3:")
    for row in rows_to_write[-3:]:
        print(f"  {row['date']}: y10={row['y10']} tips={row['tips']} hy={row['hy_bps']} cpi_yoy={row['cpi_yoy']} → cut_prob {row['cut_probability_pct']}%")
    return written


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=180, help="backfill last N days (default 180)")
    args = p.parse_args()
    n = backfill(days=args.days)
    return 0 if n >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
