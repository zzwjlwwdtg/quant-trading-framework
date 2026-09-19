"""
_backtest_trend_capture.py
──────────────────────────
回到原系统（无 A+B 无 C），衡量"趋势跟随胜率"——而不是"反转预测胜率"。

目标按用户原话："稳定吃到一段行情"。所以要看的不是"我能不能预测顶/底"，
而是"系统给出 WATCH_BUY 时，往后能不能赚到一段"。

指标：
  · WATCH_BUY 触发后 1d / 5d / 10d / 20d 平均收益（越正越好）
  · 上涨胜率（>0 占比）
  · REDUCE 触发后 5d/10d 平均收益（越负越好）
  · CAUTION 后 5d 收益（"停手观望"是不是真的让你少亏？）

测试：
  · 训练集：TQQQ/SOXL/DRAM/MULL/GLD
  · OOS：SPY/QQQ/IWM/NVDA/AAPL/TSLA/NFLX/META/7203.T/6758.T/9984.T/8035.T/USO/TLT
"""
from __future__ import annotations

import os
import pandas as pd
import numpy as np

os.environ.setdefault("TECHNICAL_ONLY", "1")
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from backtest_engine import load_history, add_indicators, build_mkt
from decision_agent import get_decision
# WP04 (2026-09-20): backtest context 隔离历史 thesis
from decision_context import from_snapshot as _from_snapshot
from datetime import timezone as _tz
from confluence import get_confluence


FAKE_EVENTS = {"breaking_news": False, "days_to_event": 99, "risk_level": "normal",
               "next_event_impact": "moderate", "gold_bias": "neutral",
               "trump_signal": {"fallback": True}}
FAKE_MACRO  = {"vix": 18, "fg_score": 50, "t10y2y": 0.27, "fedfunds": 3.63}
HORIZONS = [1, 5, 10, 20]


def _enrich_df(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["cum_5d_pct"]  = (d["close"] / d["close"].shift(5) - 1) * 100
    d["cum_10d_pct"] = (d["close"] / d["close"].shift(10) - 1) * 100
    d["dist_from_ma20"] = (d["close"] - d["ma20"]) / d["ma20"] * 100
    daily_ret = d["close"].pct_change()
    d["vol_20d_annual"] = daily_ret.rolling(20).std() * (252 ** 0.5) * 100
    for h in HORIZONS:
        d[f"fwd_{h}d"] = (d["close"].shift(-h) / d["close"] - 1) * 100
    return d


def _enrich_mkt(row, ticker_full: str) -> dict:
    mkt = build_mkt(ticker_full, row)
    mkt["cum_5d_pct"]  = row.get("cum_5d_pct")
    mkt["cum_10d_pct"] = row.get("cum_10d_pct")
    mkt["dist_from_ma20_pct"] = row.get("dist_from_ma20")
    mkt["bb_upper"]    = float(row.get("bb_upper", 0) or 0)
    mkt["vol_20d_annual"] = row.get("vol_20d_annual")
    return mkt


def analyze(ticker: str, days: int = 250) -> dict | None:
    try:
        df = add_indicators(load_history(ticker, days=days + 60))
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}
    df = df.dropna(subset=["rsi_14", "ma20", "ma50", "bb_pct", "cci_20", "vol_ratio"])
    if len(df) < 80:
        return {"ticker": ticker, "error": f"only {len(df)} days"}
    df = _enrich_df(df).iloc[-days:]
    full = ticker if "." in ticker else f"US.{ticker}"

    actions = []
    confs = []
    for date, r in df.iterrows():
        mkt = _enrich_mkt(r, full)
        cf  = get_confluence(mkt)
        # WP04: per-bar context, thesis empty → 消 look-ahead
        try:
            as_of = date.to_pydatetime().replace(tzinfo=_tz.utc)
        except Exception:
            as_of = None
        ctx = _from_snapshot(as_of=as_of, market=mkt, events=FAKE_EVENTS,
                              macro=FAKE_MACRO, thesis_snapshot={},
                              board_regime="neutral",
                              strategy_version="backtest_trend_capture") if as_of else None
        d   = get_decision(mkt, FAKE_EVENTS, FAKE_MACRO,
                            confluence=cf, board_regime="neutral", context=ctx)
        actions.append(d.get("action", "?"))
        confs.append(d.get("confidence", 0))
    df["action"] = actions
    df["conf"]   = confs

    n = len(df)
    # 基线
    base = {f"fwd_{h}d": df[f"fwd_{h}d"].dropna() for h in HORIZONS}
    base_stats = {f"{h}d": {"n": len(base[f"fwd_{h}d"]),
                              "up_rate": round(float((base[f"fwd_{h}d"] > 0).mean() * 100), 0),
                              "avg":     round(float(base[f"fwd_{h}d"].mean()), 2)}
                  for h in HORIZONS}

    def _stats_for(action: str):
        mask = df["action"] == action
        out = {}
        for h in HORIZONS:
            sub = df.loc[mask, f"fwd_{h}d"].dropna()
            if sub.empty:
                out[f"{h}d"] = {"n": 0, "up_rate": 0, "avg": 0}
            else:
                out[f"{h}d"] = {"n": len(sub),
                                 "up_rate": round(float((sub > 0).mean() * 100), 0),
                                 "avg":     round(float(sub.mean()), 2)}
        out["count"] = int(mask.sum())
        return out

    return {
        "ticker": ticker,
        "n_days": n,
        "base": base_stats,
        "WATCH_BUY": _stats_for("WATCH_BUY"),
        "REDUCE":    _stats_for("REDUCE"),
        "CAUTION":   _stats_for("CAUTION"),
        "HOLD":      _stats_for("HOLD"),
    }


def _print_action(name, results, action_key, want_up: bool):
    """want_up=True: action 应该跟着涨（如 WATCH_BUY）；False: 应该跟着跌（REDUCE）"""
    print(f"\n  ─── {name} ───")
    print(f"  {'ticker':<8}  {'n':>4}   " +
          "    ".join([f"1d↑% / avg     5d↑% / avg     10d↑% / avg    20d↑% / avg"]))
    total_count = 0
    total_h = {h: {"n": 0, "up_sum": 0, "ret_sum": 0} for h in HORIZONS}
    for r in results:
        if r.get("error"): continue
        a = r[action_key]
        if a["count"] == 0: continue
        line = f"  {r['ticker']:<8}  {a['count']:>4}  "
        for h in HORIZONS:
            s = a[f"{h}d"]
            line += f"  {s['up_rate']:>4.0f}% /{s['avg']:>+6.2f}%"
            total_h[h]["n"] += s["n"]
            total_h[h]["up_sum"] += s["up_rate"] * s["n"]
            total_h[h]["ret_sum"] += s["avg"] * s["n"]
        print(line)
        total_count += a["count"]
    print("  " + "─" * 80)
    line = f"  {'聚合':<8}  {total_count:>4}  "
    for h in HORIZONS:
        t = total_h[h]
        if t["n"] == 0:
            line += "      n/a"
            continue
        avg_up = t["up_sum"] / t["n"]
        avg_ret = t["ret_sum"] / t["n"]
        line += f"  {avg_up:>4.0f}% /{avg_ret:>+6.2f}%"
    print(line)


def _print_baseline(results):
    print(f"\n  ─── 基线（全期，无筛选） ───")
    total_h = {h: {"n": 0, "up_sum": 0, "ret_sum": 0} for h in HORIZONS}
    for r in results:
        if r.get("error"): continue
        for h in HORIZONS:
            s = r["base"][f"{h}d"]
            total_h[h]["n"] += s["n"]
            total_h[h]["up_sum"] += s["up_rate"] * s["n"]
            total_h[h]["ret_sum"] += s["avg"] * s["n"]
    line = f"  {'聚合 (n=%d)':<14}  " % sum(t["n"] for t in total_h.values())
    for h in HORIZONS:
        t = total_h[h]
        if t["n"] == 0:
            continue
        avg_up = t["up_sum"] / t["n"]
        avg_ret = t["ret_sum"] / t["n"]
        line += f"  {h}d:{avg_up:>4.0f}% /{avg_ret:>+6.2f}%"
    print(line)


def main():
    in_sample = ["TQQQ", "SOXL", "DRAM", "MULL", "GLD"]
    oos = ["SPY", "QQQ", "IWM", "NVDA", "AAPL", "TSLA", "NFLX", "META",
           "7203.T", "6758.T", "9984.T", "8035.T", "USO", "TLT"]

    print("=" * 110)
    print("  原系统 趋势捕获率回测（无 A+B 无 C，纯 confluence + regime + earnings guard）")
    print("=" * 110)
    print()
    print("  评判：WATCH_BUY 触发后 5d/10d/20d 平均收益 vs 基线（基线 ≈ +0.5% / 5d）")
    print("  系统目标：'稳定吃到一段行情' — 跟随趋势，不预测反转")

    for group_name, tickers in [("训练集 (5 标的)", in_sample),
                                ("OOS (14 标的)", oos)]:
        print(f"\n{'=' * 110}")
        print(f"  {group_name}")
        print('=' * 110)
        results = [analyze(tk) for tk in tickers]
        _print_baseline(results)
        _print_action("WATCH_BUY 后续走势", results, "WATCH_BUY", want_up=True)
        _print_action("REDUCE 后续走势",   results, "REDUCE",    want_up=False)
        _print_action("CAUTION 后续走势",  results, "CAUTION",   want_up=False)


if __name__ == "__main__":
    main()
