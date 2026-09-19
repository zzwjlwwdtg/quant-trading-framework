"""
Regime 单一源修改的回测对比。

修前：board_regime=="neutral" → 绕过去走 per-ticker get_regime(macro, market)
修后：board_regime=="neutral" → 永远 neutral（除非单股 ≤-5% override 成 crisis）

回测方法：对历史每一天，构造两种决策：
  legacy: 显式传 board_regime = get_regime(macro, mkt) per-ticker
  new   : 显式传 board_regime = "neutral"
两种都用同一份 mkt / events / macro，唯一变量就是 regime。

然后对每个 WATCH_BUY / REDUCE 信号，查 N 天后的实际收益 → 算胜率 + 平均收益。
"""
from __future__ import annotations
import sys
from collections import defaultdict
sys.stdout.reconfigure(line_buffering=True)

import numpy as np
import pandas as pd

from backtest_engine import (
    load_history, add_indicators, build_mkt, TICKERS, BACKTEST_DAYS
)
from decision_agent import get_decision, get_regime
# WP04 (2026-09-20): backtest context 消 look-ahead
from decision_context import from_snapshot as _from_snapshot
from datetime import timezone as _tz

DAYS = 250                # 用 1 年
FORWARD_DAYS = 5          # 信号后第 N 天测净收益
# CONF_MIN as canonical 10-scale. Runtime scales to actual mode:
#   TECHNICAL_ONLY=1 (default) → scale=5 → effective threshold = round(6*5/10) = 3
#   TECHNICAL_ONLY=0            → scale=10 → effective threshold = 6
# 2026-09-19 fix: 之前硬编 6, TECHNICAL_ONLY=1 下 max conf=5 → 所有采样为 0
CONF_MIN_CANONICAL = 6
def _effective_conf_min() -> int:
    try:
        from decision_agent import _conf_scale
        scale = _conf_scale()
    except Exception:
        scale = 5   # TECHNICAL_ONLY default
    return max(1, round(CONF_MIN_CANONICAL * scale / 10))
CONF_MIN = _effective_conf_min()   # 用于内层比较
FAKE_EVENTS = {"breaking_news": False, "days_to_event": 99,
               "risk_level": "moderate", "gold_bias": "neutral",
               "trump_signal": {"fallback": True}}
FAKE_MACRO  = {"vix": 18, "fg_score": 55, "t10y2y": 0.4}


def run_one(tk: str):
    """返回 dict: {scenario: {action: [(date, fwd_return), ...]}}"""
    full = "US." + tk
    df = add_indicators(load_history(tk, days=DAYS + 60))
    df = df.dropna(subset=["rsi_14","ma20","ma50","bb_pct","cci_20"])
    dates = list(df.index)[-DAYS:]
    results = {"legacy": defaultdict(list), "new": defaultdict(list)}
    for i, d in enumerate(dates):
        if i + FORWARD_DAYS >= len(dates):
            break
        row = df.loc[d]
        try:
            mkt = build_mkt(full, row)
        except Exception:
            continue
        # 当时的"per-ticker regime"（修前行为）
        legacy_regime = get_regime(FAKE_MACRO, mkt)
        # WP04: per-bar context, thesis empty → 历史 backtest 不受当前 blacklist 干扰
        try:
            as_of = d.to_pydatetime().replace(tzinfo=_tz.utc)
        except Exception:
            as_of = None
        ctx = _from_snapshot(as_of=as_of, market=mkt, events=FAKE_EVENTS,
                              macro=FAKE_MACRO, thesis_snapshot={},
                              board_regime="neutral",
                              strategy_version="backtest_regime_fix") if as_of else None
        # 两种决策
        try:
            dec_legacy = get_decision(mkt, FAKE_EVENTS, FAKE_MACRO,
                                       board_regime=legacy_regime, context=ctx)
            dec_new    = get_decision(mkt, FAKE_EVENTS, FAKE_MACRO,
                                       board_regime="neutral", context=ctx)
        except Exception:
            continue

        future_close = float(df.loc[dates[i + FORWARD_DAYS], "close"])
        today_close  = float(df.loc[d, "close"])
        fwd_ret = (future_close - today_close) / today_close * 100

        for scen, dec in [("legacy", dec_legacy), ("new", dec_new)]:
            act = dec.get("action", "HOLD")
            conf = dec.get("confidence", 0) or 0
            if conf < CONF_MIN:
                continue
            results[scen][act].append((d.strftime("%Y-%m-%d"), fwd_ret))

    return results


def summary(tk: str, results: dict):
    print(f"\n[{tk}] forward-{FORWARD_DAYS}d return per action (conf≥{CONF_MIN})")
    print(f"  {'scenario':<8} {'action':<12} {'N':>5} {'hit%':>7} {'avg%':>8} {'med%':>8}")
    print(f"  {'-'*8} {'-'*12} {'-'*5} {'-'*7} {'-'*8} {'-'*8}")
    for scen in ("legacy", "new"):
        per_act = results[scen]
        for act in ("REDUCE", "CAUTION", "HOLD", "WATCH_BUY", "BUY", "SELL"):
            samples = per_act.get(act, [])
            if not samples:
                continue
            rets = np.array([r for _, r in samples])
            # WATCH_BUY/BUY 用 ret > 0 算 hit；REDUCE/SELL 用 ret < 0 算 hit
            if act in ("WATCH_BUY", "BUY"):
                hit = (rets > 0).mean() * 100
            elif act in ("REDUCE", "SELL", "CAUTION"):
                hit = (rets < 0).mean() * 100
            else:
                hit = (rets > 0).mean() * 100
            print(f"  {scen:<8} {act:<12} {len(rets):>5} {hit:>6.1f}% {rets.mean():>+7.2f}% {np.median(rets):>+7.2f}%")


def main():
    print("="*72)
    print("  Regime 单一源修复 回测对比")
    print(f"  天数={DAYS} 前望={FORWARD_DAYS}d  最小置信={CONF_MIN}")
    print("="*72)
    overall = {"legacy": defaultdict(list), "new": defaultdict(list)}
    for tk in TICKERS:
        r = run_one(tk)
        summary(tk, r)
        for scen in ("legacy", "new"):
            for act, samples in r[scen].items():
                overall[scen][act].extend(samples)
    print("\n" + "="*72)
    print("  汇总 (3 只 ETF)")
    summary("ALL", overall)


if __name__ == "__main__":
    main()
