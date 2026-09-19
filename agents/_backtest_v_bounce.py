"""
V 型反转追买回测 — 验证新加的 decision_agent V-bounce 分支：

1. 历史日 K 里找 V 型日 (前日 ≤ -3% & 当日 ≥ +2%) — 看样本量
2. 对每个 V 型日跑两版决策：
   legacy = 不传 prev_pct（V 分支不触发）
   new    = 正常传 prev_pct
3. 比较：
   · new 在 V 型日触发 WATCH_BUY 次数
   · WATCH_BUY 后 5d / 10d 实际收益 hit rate
   · 非 V 型日 new 与 legacy 决策是否 100% 一致（无副作用）

判据（feedback_backtest_gate.md）：
  · V 型日 WATCH_BUY 在 5d 后 hit rate ≥ 55%（趋势型反弹历史胜率）
  · 非 V 型日新版决策 = 旧版（无副作用）
"""
from __future__ import annotations
import sys
sys.stdout.reconfigure(line_buffering=True)

import numpy as np
import pandas as pd

from backtest_engine import (
    load_history, add_indicators, build_mkt, TICKERS,
)
from decision_agent import get_decision
# WP04 (2026-09-20): backtest context 隔离历史 thesis
from decision_context import from_snapshot as _from_snapshot
from datetime import timezone as _tz

DAYS = 500       # V 型日比较稀有，要更长窗口
FORWARD = [5, 10, 20]
FAKE_EVENTS = {"breaking_news": False, "days_to_event": 99,
               "risk_level": "moderate", "gold_bias": "neutral",
               "trump_signal": {"fallback": True}}
FAKE_MACRO  = {"vix": 18, "fg_score": 55, "t10y2y": 0.4}

def _find_v_days(df: pd.DataFrame, scaled_prev_threshold: float = -3.0,
                  scaled_today_threshold: float = 2.0,
                  leverage: float = 1.0) -> list:
    """返回 V 型日的 (date, prev_pct_scaled, today_pct_scaled, rsi) 列表。"""
    out = []
    for i in range(1, len(df)):
        prev_pct = df.iloc[i - 1]["pct_chg"]
        today_pct = df.iloc[i]["pct_chg"]
        rsi = df.iloc[i]["rsi_14"]
        if pd.isna(prev_pct) or pd.isna(today_pct) or pd.isna(rsi):
            continue
        prev_eff = prev_pct / leverage
        today_eff = today_pct / leverage
        if prev_eff <= scaled_prev_threshold and today_eff >= scaled_today_threshold:
            out.append((df.index[i], prev_eff, today_eff, float(rsi)))
    return out


def _forward_returns(df: pd.DataFrame, base_date) -> dict:
    """返回 {5: pct_ret, 10: pct_ret, 20: pct_ret}"""
    out = {}
    try:
        idx = list(df.index).index(base_date)
    except ValueError:
        return out
    base_close = float(df.iloc[idx]["close"])
    for n in FORWARD:
        target = idx + n
        if target >= len(df):
            continue
        fut = float(df.iloc[target]["close"])
        out[n] = (fut - base_close) / base_close * 100
    return out


def backtest(tk: str, leverage: float = 1.0) -> dict:
    df = add_indicators(load_history(tk, days=DAYS + 60))
    df = df.dropna(subset=["rsi_14", "ma20", "ma50", "bb_pct", "cci_20"])
    dates = list(df.index)[-DAYS:]
    df_w = df.loc[dates[0]:]

    v_days = _find_v_days(df_w, leverage=leverage)
    print(f"  V 型日数: {len(v_days)} (在 {len(df_w)} 天里)")

    full = "US." + tk
    new_watchbuy_returns = {n: [] for n in FORWARD}
    legacy_watchbuy_returns = {n: [] for n in FORWARD}
    non_v_diff = 0
    non_v_total = 0
    new_action_counts = {}     # action → count on V days
    legacy_action_counts = {}

    for i, d in enumerate(dates):
        if i + max(FORWARD) >= len(dates):
            break
        row = df.loc[d]
        try:
            mkt_new = build_mkt(full, row)
            mkt_legacy = {**mkt_new, "prev_pct": 0}  # 屏蔽 V 分支
            # WP04: per-bar context, thesis_snapshot={} 消 look-ahead bias
            try:
                as_of = d.to_pydatetime().replace(tzinfo=_tz.utc)
            except Exception:
                as_of = None
            ctx = _from_snapshot(as_of=as_of, market=mkt_new,
                                  events=FAKE_EVENTS, macro=FAKE_MACRO,
                                  thesis_snapshot={}, board_regime="neutral",
                                  strategy_version="backtest_v_bounce") if as_of else None
            dec_new = get_decision(mkt_new, FAKE_EVENTS, FAKE_MACRO,
                                    board_regime="neutral", context=ctx)
            dec_leg = get_decision(mkt_legacy, FAKE_EVENTS, FAKE_MACRO,
                                    board_regime="neutral", context=ctx)
        except Exception:
            continue

        is_v_day = any(v[0] == d for v in v_days)

        if dec_new.get("action") != dec_leg.get("action"):
            if not is_v_day:
                non_v_diff += 1
        if not is_v_day:
            non_v_total += 1

        # 仅记录 V 型日的 WATCH_BUY / WATCH_BUY_LONG_HOLD 后续表现
        if is_v_day:
            fwd = _forward_returns(df, d)
            a_new = dec_new.get("action")
            a_leg = dec_leg.get("action")
            new_action_counts[a_new] = new_action_counts.get(a_new, 0) + 1
            legacy_action_counts[a_leg] = legacy_action_counts.get(a_leg, 0) + 1
            if a_new in ("WATCH_BUY", "BUY", "WATCH_BUY_LONG_HOLD"):
                for n, ret in fwd.items():
                    new_watchbuy_returns[n].append(ret)
            if a_leg in ("WATCH_BUY", "BUY", "WATCH_BUY_LONG_HOLD"):
                for n, ret in fwd.items():
                    legacy_watchbuy_returns[n].append(ret)

    return {
        "ticker": tk,
        "n_v_days": len(v_days),
        "new_watchbuy": new_watchbuy_returns,
        "legacy_watchbuy": legacy_watchbuy_returns,
        "non_v_diff": non_v_diff,
        "non_v_total": non_v_total,
        "v_days": v_days,
        "new_action_counts": new_action_counts,
        "legacy_action_counts": legacy_action_counts,
    }


def _summarize(label: str, rets: dict):
    print(f"  {label}:")
    for n in FORWARD:
        samples = rets[n]
        if not samples:
            print(f"    {n}d: 无样本")
            continue
        arr = np.array(samples)
        hit = (arr > 0).mean() * 100
        print(f"    {n}d:  N={len(samples)}  hit={hit:>5.1f}%  avg={arr.mean():+6.2f}%  med={np.median(arr):+6.2f}%")


def main():
    LEVERAGE = {"TQQQ": 3.0, "SOXL": 3.0, "GLD": 1.0}
    print("=" * 72)
    print(f"  V 型反转追买回测  | 标的: {TICKERS}  | 窗口: {DAYS}d")
    print(f"  V 型日定义: 前日 pct/lev ≤ -3.0% AND 今日 pct/lev ≥ +2.0%")
    print("=" * 72)

    overall_new = {n: [] for n in FORWARD}
    overall_leg = {n: [] for n in FORWARD}
    overall_non_v_diff = 0
    overall_non_v_total = 0

    for tk in TICKERS:
        lev = LEVERAGE.get(tk, 1.0)
        print(f"\n[{tk}] (lev={lev}x) 回测中...")
        r = backtest(tk, leverage=lev)
        print(f"  V 型日 action 分布:")
        print(f"    new   : {dict(sorted(r['new_action_counts'].items()))}")
        print(f"    legacy: {dict(sorted(r['legacy_action_counts'].items()))}")
        _summarize("new (V 分支启用)", r["new_watchbuy"])
        _summarize("legacy (V 分支屏蔽)", r["legacy_watchbuy"])
        print(f"  非 V 型日决策差异: {r['non_v_diff']}/{r['non_v_total']} = "
              f"{r['non_v_diff']/r['non_v_total']*100:.2f}%" if r['non_v_total'] else "  非 V 型日: 无样本")
        # 累加
        for n in FORWARD:
            overall_new[n].extend(r["new_watchbuy"][n])
            overall_leg[n].extend(r["legacy_watchbuy"][n])
        overall_non_v_diff += r["non_v_diff"]
        overall_non_v_total += r["non_v_total"]

    print()
    print("=" * 72)
    print("  汇总（3 只 ETF）")
    print("=" * 72)
    _summarize("ALL new", overall_new)
    _summarize("ALL legacy", overall_leg)
    if overall_non_v_total:
        rate = overall_non_v_diff / overall_non_v_total * 100
        print(f"\n  非 V 型日决策差异: {overall_non_v_diff}/{overall_non_v_total} = {rate:.2f}%")
        if overall_non_v_diff == 0:
            print("  ✓ V 分支无副作用（非 V 型日决策 100% 等同 legacy）")
        else:
            print(f"  ⚠ 非 V 型日有 {overall_non_v_diff} 天决策变化，可能有泄漏")

    # 判据
    print()
    n5 = overall_new[5]
    if n5:
        hit5 = (np.array(n5) > 0).mean() * 100
        if hit5 >= 55:
            print(f"  ✓ 5d hit rate {hit5:.1f}% ≥ 55% — 可以 ship")
        else:
            print(f"  ✗ 5d hit rate {hit5:.1f}% < 55% — 需要调阈值或回滚")
    else:
        print("  ⚠ new WATCH_BUY 触发次数 = 0，V 分支没生效或阈值太严，检查 bounce_score")


if __name__ == "__main__":
    main()
