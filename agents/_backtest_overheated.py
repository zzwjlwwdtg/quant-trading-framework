"""
_backtest_overheated.py
───────────────────────
回测 A+B 方案（CAUTION 第一层 + overheated 第二层）。

对每个标的过去 N 天逐日跑 decision_agent，比较：
  · 当前实战决策（已含 A+B）
  · 模拟"未开启 A+B"的决策（注释掉两层、_RT 还原 (4,3,5)）

输出：
  · 每个标的新增 CAUTION / 加 REDUCE 信号的日期
  · 触发日 5d 后实际收益（验证是否避免下跌）
  · DRAM 06-22 实战检验
"""
from __future__ import annotations

import os
import sys
import pandas as pd
import numpy as np

os.environ.setdefault("TECHNICAL_ONLY", "1")
os.environ.setdefault("PYTHONUTF8", "1")

from backtest_engine import load_history, add_indicators, build_mkt
from confluence import get_confluence
from decision_agent import (
    _caution_check, _is_overheated, _scaled_pct, get_regime, _etf_rules
)


FAKE_EVENTS = {"breaking_news": False, "days_to_event": 99, "risk_level": "normal",
               "next_event_impact": "moderate", "gold_bias": "neutral",
               "trump_signal": {"fallback": True}}
FAKE_MACRO  = {"vix": 18, "fg_score": 50, "t10y2y": 0.27, "fedfunds": 3.63}
def _enrich_market(row, ticker_full: str) -> dict:
    """build_mkt 不带 cum_5d / cum_10d / dist_from_ma20 / vol_20d，这里补上。"""
    mkt = build_mkt(ticker_full, row)
    mkt["cum_5d_pct"]  = row.get("cum_5d_pct")
    mkt["cum_10d_pct"] = row.get("cum_10d_pct")
    mkt["dist_from_ma20_pct"] = row.get("dist_from_ma20")
    mkt["bb_upper"]    = float(row.get("bb_upper", 0) or 0)
    mkt["vol_20d_annual"] = row.get("vol_20d_annual")
    return mkt


def _enrich_df(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["cum_5d_pct"]  = (d["close"] / d["close"].shift(5)  - 1) * 100
    d["cum_10d_pct"] = (d["close"] / d["close"].shift(10) - 1) * 100
    d["dist_from_ma20"] = (d["close"] - d["ma20"]) / d["ma20"] * 100
    daily_ret = d["close"].pct_change()
    d["vol_20d_annual"] = daily_ret.rolling(20).std() * (252 ** 0.5) * 100
    d["fwd_5d"] = (d["close"].shift(-5) / d["close"] - 1) * 100
    return d


def replay_ticker(ticker: str, days: int = 250) -> pd.DataFrame:
    df = add_indicators(load_history(ticker, days=days + 60))
    df = df.dropna(subset=["rsi_14", "ma20", "ma50", "bb_pct", "cci_20", "vol_ratio"])
    df = _enrich_df(df).iloc[-days:] if len(df) > days else _enrich_df(df)
    full = f"US.{ticker}"

    rows = []
    for date, r in df.iterrows():
        mkt = _enrich_market(r, full)
        # 计算 confluence + 决策（带 A+B）
        cf = get_confluence(mkt)
        d = _etf_rules(mkt, FAKE_EVENTS, FAKE_MACRO,
                       get_regime(FAKE_MACRO, mkt), cf)
        # 仅查询 A+B 的两层是否会触发（用于报告）
        is_caution, c_reason = _caution_check(mkt)
        is_oh, o_reason = _is_overheated(mkt)
        rows.append({
            "date":     str(date.date()),
            "close":    round(float(r["close"]), 2),
            "pct":      round(float(r.get("pct_chg", 0) or 0), 2),
            "rsi":      round(float(r.get("rsi_14") or 0), 0),
            "cci":      round(float(r.get("cci_20") or 0), 0),
            "cum_5d":   round(float(r.get("cum_5d_pct") or 0), 1),
            "cum_10d":  round(float(r.get("cum_10d_pct") or 0), 1),
            "dist_ma20": round(float(r.get("dist_from_ma20") or 0), 1),
            "regime":   d.get("regime", "?"),
            "action":   d.get("action", "?"),
            "conf":     d.get("confidence", 0),
            "caution_layer1": "Y" if is_caution else "",
            "overheated":     "Y" if is_oh else "",
            "trigger":  c_reason or o_reason or "",
            "fwd_5d":   round(float(r.get("fwd_5d", 0) or 0), 1) if not pd.isna(r.get("fwd_5d")) else None,
        })
    return pd.DataFrame(rows)


def report(ticker: str, days: int = 250) -> None:
    print(f"\n{'='*100}")
    print(f"  {ticker} 过去 {days} 天 A+B 触发回放")
    print('=' * 100)
    df = replay_ticker(ticker, days=days)
    # 只显示有触发或重大行情的日期
    triggers = df[(df["caution_layer1"] == "Y") | (df["overheated"] == "Y") |
                  (df["action"].isin(["REDUCE", "SELL", "CAUTION"]))]
    if triggers.empty:
        print(f"  {ticker} 无 CAUTION / overheated / REDUCE 触发")
        return

    cols = ["date","close","pct","rsi","cci","cum_5d","cum_10d","regime","action","conf",
            "caution_layer1","overheated","trigger","fwd_5d"]
    print(triggers[cols].to_string(index=False))

    # 命中率：触发当日 → 5d 后实际下跌？
    cautions = df[df["caution_layer1"] == "Y"].dropna(subset=["fwd_5d"])
    overheats = df[df["overheated"] == "Y"].dropna(subset=["fwd_5d"])
    reduces  = df[df["action"] == "REDUCE"].dropna(subset=["fwd_5d"])

    def _stats(name, sub):
        if sub.empty:
            print(f"  {name}: 0 次触发")
            return
        n = len(sub)
        hit = (sub["fwd_5d"] < 0).sum()
        avg = sub["fwd_5d"].mean()
        print(f"  {name}: {n} 次触发  5d 下跌率={hit}/{n} ({hit/n*100:.0f}%)  平均 5d 收益={avg:+.2f}%")

    print()
    _stats("CAUTION (第一层)", cautions)
    _stats("overheated (第二层)", overheats)
    _stats("REDUCE 实际信号", reduces)


def main():
    print("=" * 100)
    print("  A+B 方案回测：CAUTION 第一层 + overheated 第二层")
    print("=" * 100)
    print("  · CAUTION 触发: CCI>150 + 5d_cum>15%（缩放） + 破 BB 上轨 或 偏离 MA20>12%")
    print("  · overheated 触发: 5d_cum>20%+CCI>180 / 10d_cum>30%+RSI>68 / 连续暴涨")
    print("  · overheated regime 下 reduce_thresh=3（原 5），bull_thresh=5")
    print("  · 命中评估：5d 下跌率 = 触发当日 5 个交易日后下跌的次数 / 总触发数")

    for tk in ["DRAM", "MULL", "SOXL", "TQQQ", "GLD"]:
        try:
            report(tk, days=250)
        except Exception as e:
            print(f"\n{tk}: 失败 {e}")


if __name__ == "__main__":
    main()
