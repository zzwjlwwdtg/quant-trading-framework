"""
_build_liquidity_history.py — 长时序流动性历史数据构建器

生成 signals/liquidity_history.json, 供 dashboard 大图渲染. 26 年 (2000-今),
月度采样, 3 个指标 + 历史危机峰值参考.

数据源:
  MOVE:      ^MOVE (yfinance, 2002-11 → 今)
  Funding:   TEDRATE (FRED, 1986-01 → 2022-01) + SOFR-IORB spread (2018-04 → 今)
             note: TEDRATE 单位 % (as is), SOFR-IORB 单位 bps → 统一转 bps
  Bank:      XLF/SPY 20d 相对变化 (yfinance, 1998-12 → 今)
             XLF 覆盖更长; KBE 只有 2005+, 用 XLF 保持全时段一致.

跑法 (weekly.bat 里追加):
  python _build_liquidity_history.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

from config import FRED_API_KEY, SIGNALS_DIR


OUT_PATH = Path(SIGNALS_DIR) / "liquidity_history.json"

# 历史危机峰值/关键日期 (画注释用)
CRISIS_EVENTS = [
    {"date": "2008-09-15", "label": "Lehman 破产", "code": "GFC"},
    {"date": "2020-03-16", "label": "COVID 封城", "code": "COVID"},
    {"date": "2023-03-10", "label": "SVB 倒闭", "code": "SVB"},
    {"date": "2019-09-17", "label": "Repo 危机", "code": "REPO"},
]

# 阈值 (dashboard 用) — 与 auto_rebalance / bond_monitor 保持一致
THRESHOLDS = {
    "move":         {"warn": 100, "bad": 140, "extreme": 180, "direction": "high"},
    "funding_bps":  {"warn": 30,  "bad": 100, "extreme": 200, "direction": "high"},
    "bank_20d_pct": {"warn": -3,  "bad": -6,  "extreme": -10, "direction": "low"},
}


def _fetch_fred(sid: str) -> list[tuple[date, float]]:
    """完整 FRED 历史 (asc 排列)."""
    url = (f"https://api.stlouisfed.org/fred/series/observations"
           f"?series_id={sid}&api_key={FRED_API_KEY}&file_type=json&sort_order=asc")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            obs = json.loads(r.read()).get("observations", [])
    except Exception as exc:
        print(f"  FRED {sid} error: {exc}")
        return []
    out = []
    for o in obs:
        v = o.get("value")
        if v in (None, ".", ""):
            continue
        try:
            out.append((date.fromisoformat(o["date"]), float(v)))
        except Exception:
            continue
    return out


def _monthly_last(series: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """每月最后一个观测值 (asc). 用来降采样到 monthly."""
    if not series:
        return []
    from collections import OrderedDict
    monthly: OrderedDict = OrderedDict()
    for d, v in series:
        key = (d.year, d.month)
        monthly[key] = (d, v)
    return list(monthly.values())


def _fetch_yf(ticker: str) -> list[tuple[date, float]]:
    """从 yfinance 拉 max 历史, 每个交易日 close."""
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="max")
        if h.empty:
            return []
        return [(ts.date(), float(px)) for ts, px in h["Close"].items()]
    except Exception as exc:
        print(f"  yfinance {ticker} error: {exc}")
        return []


def build_move() -> list[dict]:
    print("  fetching ^MOVE ...")
    raw = _fetch_yf("^MOVE")
    if not raw:
        return []
    m = _monthly_last(raw)
    return [{"date": d.isoformat(), "value": round(v, 1)} for d, v in m]


def build_funding() -> list[dict]:
    """TEDRATE (1986-2022) → SOFR-IORB (2018+) 缝合, 单位 bps."""
    print("  fetching TEDRATE ...")
    ted = _fetch_fred("TEDRATE")            # 单位 %
    print("  fetching SOFR ...")
    sofr = _fetch_fred("SOFR")               # 单位 %
    print("  fetching IORB ...")
    iorb = _fetch_fred("IORB")               # 单位 %

    # TEDRATE % → bps
    ted_bps = [(d, v * 100) for d, v in ted]
    ted_monthly = _monthly_last(ted_bps)

    # SOFR-IORB spread bps by date
    sofr_map = dict(sofr)
    iorb_map = dict(iorb)
    sofr_spread = []
    for d in sorted(set(sofr_map) & set(iorb_map)):
        sofr_spread.append((d, (sofr_map[d] - iorb_map[d]) * 100))
    sofr_monthly = _monthly_last(sofr_spread)

    # 缝合: TEDRATE 是 T-bill vs LIBOR 差 (整体水位比 SOFR-IORB 高个 20-50bps),
    # 直接串接会有 level shift. 但目的是看**危机时的相对突起**, 用户能读懂.
    # 时间上以 2018-04 为切换点: 之前 TEDRATE, 之后 SOFR-IORB.
    result = []
    for d, v in ted_monthly:
        if d < date(2018, 4, 1):
            result.append({"date": d.isoformat(), "value": round(v, 1), "source": "TED"})
    for d, v in sofr_monthly:
        if d >= date(2018, 4, 1):
            result.append({"date": d.isoformat(), "value": round(v, 1), "source": "SOFR-IORB"})
    result.sort(key=lambda x: x["date"])
    return result


def build_bank_stress() -> list[dict]:
    """XLF/SPY 20d 相对变化, 月度."""
    print("  fetching XLF ...")
    xlf = _fetch_yf("XLF")
    print("  fetching SPY ...")
    spy = _fetch_yf("SPY")
    if not xlf or not spy:
        return []
    xlf_map = dict(xlf)
    spy_map = dict(spy)
    common = sorted(set(xlf_map) & set(spy_map))
    ratios = [(d, xlf_map[d] / spy_map[d]) for d in common]
    # 每日 20d 相对变化 (%)
    result = []
    for i in range(20, len(ratios)):
        d = ratios[i][0]
        r_now = ratios[i][1]
        r_20 = ratios[i - 20][1]
        delta = round((r_now / r_20 - 1) * 100, 2)
        result.append((d, delta))
    monthly = _monthly_last(result)
    return [{"date": d.isoformat(), "value": v} for d, v in monthly]


def main():
    print("== building liquidity history (2000-now, monthly) ==")
    move = build_move()
    print(f"  MOVE: {len(move)} monthly points")
    funding = build_funding()
    print(f"  funding: {len(funding)} monthly points")
    bank = build_bank_stress()
    print(f"  bank: {len(bank)} monthly points")

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "range": {
            "move":    {"first": move[0]["date"], "last": move[-1]["date"]} if move else None,
            "funding": {"first": funding[0]["date"], "last": funding[-1]["date"]} if funding else None,
            "bank":    {"first": bank[0]["date"], "last": bank[-1]["date"]} if bank else None,
        },
        "series": {
            "move": move,
            "funding": funding,
            "bank": bank,
        },
        "crisis_events": CRISIS_EVENTS,
        "thresholds": THRESHOLDS,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"== wrote {OUT_PATH.stat().st_size // 1024}KB → {OUT_PATH} ==")


if __name__ == "__main__":
    main()
