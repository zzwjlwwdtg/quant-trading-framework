"""_backtest_crack_spread.py — 3-2-1 crack spread 作为衰退前置指标回测

假设: 炼油厂 3-2-1 crack spread (每桶原油炼油利润) 崩塌 → 消费者需求疲软 →
       股市 3-6 月内承压 (类似 2008-Q1 / 2019-Q4 / 2022-Q3 pattern).

3-2-1 crack (per barrel):
    (2 × 汽油 + 1 × 取暖油) × 42 / 3 − WTI
        = 平均每桶原油炼成汽油/柴油的毛利
    单位: USD / barrel
    (RB / HO 是 $/gal, WTI 是 $/barrel, 1 barrel = 42 gal)

正常范围: $15-30 / barrel
< $10 = 需求疲软, 炼油利润压缩 → 消费者/工业活动减少的领先信号
> $40 = 供给短缺 (飓风, 炼厂 offline)

**设计 (跑前不改)**:

  数据窗口: 2010-01-01 → 2024-12-31 (~15 年 daily, 避 2008 极端 outlier)
  数据源  : yfinance CL=F / RB=F / HO=F + SPY
  Regime  : 3-year rolling percentile
            low  (≤ 20th percentile) = 需求疲软信号
            mid  (20-80)
            high (≥ 80th percentile) = 供给紧张 / 需求旺
  Fwd horizon: 3M (63 交易日) / 6M (126 交易日) SPY 收益

  通过条件 (硬编码):
    C1: low bucket 3M avg SPY ret ≤ baseline avg - 2%
    C2: low bucket 6M avg SPY ret ≤ baseline avg - 3%
    C3: low vs baseline |t-stat| ≥ 2.0
    C4: low bucket n ≥ 30 (信号触发够多)

  Verdict:
    通过 4/4 → ✅ 接入 bond_monitor + macro_context
    通过 2-3 → ⚠ dashboard 显示不入决策
    通过 0-1 → ✗ 淘汰
"""
from __future__ import annotations

import math
import sys
from typing import Optional

sys.stdout.reconfigure(line_buffering=True)

DATA_START = "2010-01-01"
DATA_END   = "2024-12-31"
LOW_PCT    = 20
HIGH_PCT   = 80
ROLLING_YEARS = 3
FWD_HORIZONS = {"3M": 63, "6M": 126}   # trading days

# 通过条件
PASS_LOW_3M_DELTA = -2.0   # low bucket 3M avg 应 ≤ baseline - 2%
PASS_LOW_6M_DELTA = -3.0
PASS_T_STAT_MIN   = 2.0
PASS_LOW_N_MIN    = 30

WATCHDOG_SEC = 600


def _install_watchdog(n: int) -> None:
    import os
    import threading
    threading.Timer(n, lambda: (print(f"\n[watchdog] {n}s 超时"), os._exit(2))).start()


def _welch_t(x: list, y: list) -> float:
    if len(x) < 2 or len(y) < 2: return 0.0
    mx = sum(x) / len(x); my = sum(y) / len(y)
    vx = sum((a - mx) ** 2 for a in x) / (len(x) - 1)
    vy = sum((b - my) ** 2 for b in y) / (len(y) - 1)
    se = math.sqrt(vx / len(x) + vy / len(y))
    return (mx - my) / se if se > 0 else 0.0


def _stat(xs):
    if not xs: return None
    avg = sum(xs) / len(xs)
    win = sum(1 for r in xs if r > 0) / len(xs) * 100
    med = sorted(xs)[len(xs) // 2]
    return {"n": len(xs), "avg": avg, "win": win, "med": med}


def run():
    import pandas as pd
    import yfinance as yf

    print("=" * 100)
    print(f"3-2-1 Crack Spread 回测 {DATA_START} → {DATA_END}")
    print("=" * 100)

    # 拉 4 只
    dfs = {}
    for t in ["CL=F", "RB=F", "HO=F", "SPY"]:
        try:
            h = yf.Ticker(t).history(start=DATA_START, end=DATA_END,
                                      interval="1d", auto_adjust=True)
        except Exception as ex:
            print(f"  {t}: pull failed {ex}")
            return
        if h is None or h.empty:
            print(f"  {t}: 空数据")
            return
        h.index = [ts.date() for ts in h.index]
        dfs[t] = h["Close"].astype(float)
        print(f"  {t}: n={len(h)} range={h.index[0]} → {h.index[-1]}")

    # 合并 (只保留 4 只都有的日期)
    merged = pd.DataFrame(dfs).dropna(how="any")
    print(f"\nmerged n={len(merged)}")

    # 3-2-1 crack per barrel
    # (2 × RB + HO) × 42 / 3 − CL
    merged["crack"] = (2 * merged["RB=F"] + merged["HO=F"]) * 42 / 3 - merged["CL=F"]
    print(f"crack range: {merged['crack'].min():.2f} → {merged['crack'].max():.2f}  median={merged['crack'].median():.2f}")

    # 3-year rolling percentile
    win = 252 * ROLLING_YEARS
    def _pct_rank(s):
        return (s.rank(pct=True).iloc[-1]) * 100
    merged["crack_pct"] = merged["crack"].rolling(win, min_periods=252).apply(_pct_rank, raw=False)

    # SPY fwd returns
    for lbl, days in FWD_HORIZONS.items():
        merged[f"spy_fwd_{lbl}"] = (merged["SPY"].shift(-days) / merged["SPY"] - 1) * 100

    # 分桶
    valid = merged["crack_pct"].notna()
    events = merged[valid].copy()
    events["bucket"] = "mid"
    events.loc[events["crack_pct"] <= LOW_PCT, "bucket"] = "low"
    events.loc[events["crack_pct"] >= HIGH_PCT, "bucket"] = "high"

    # baseline: 全期
    baseline = {}
    for lbl in FWD_HORIZONS:
        col = f"spy_fwd_{lbl}"
        vals = events[col].dropna().tolist()
        baseline[lbl] = _stat(vals)

    print()
    print("=" * 100)
    print("【Baseline】 全期无条件 SPY fwd return")
    print("=" * 100)
    for lbl, s in baseline.items():
        if s:
            print(f"  {lbl}: n={s['n']:<5} avg={s['avg']:>+7.2f}% win={s['win']:>5.1f}% median={s['med']:>+7.2f}%")

    # 分桶
    for horizon_lbl in FWD_HORIZONS:
        col = f"spy_fwd_{horizon_lbl}"
        print()
        print("=" * 100)
        print(f"【{horizon_lbl} fwd SPY】 按 crack_spread rolling pct 分桶")
        print("=" * 100)
        print(f"  {'bucket':<8} {'n':>6} {'avg':>9} {'win':>7} {'median':>9} {'Δ vs base':>10} {'t-stat':>7}")
        for b in ["low", "mid", "high"]:
            xs = events[events["bucket"] == b][col].dropna().tolist()
            s = _stat(xs)
            if not s:
                print(f"  {b:<8} —")
                continue
            b_avg = baseline[horizon_lbl]["avg"] if baseline.get(horizon_lbl) else 0
            d = s["avg"] - b_avg
            b_vals = events[col].dropna().tolist()   # 全期作 t-test 参照
            t = _welch_t(xs, b_vals) if b_vals else 0
            print(f"  {b:<8} {s['n']:>6} {s['avg']:>+8.2f}% {s['win']:>6.1f}% {s['med']:>+8.2f}% "
                  f"{d:>+9.2f}% {t:>+7.2f}")

    # verdict — 用 low bucket
    print()
    print("=" * 100)
    print("【VERDICT】 通过条件 (针对 low crack_spread bucket)")
    print("=" * 100)
    low_3m = events[events["bucket"] == "low"]["spy_fwd_3M"].dropna().tolist()
    low_6m = events[events["bucket"] == "low"]["spy_fwd_6M"].dropna().tolist()
    all_3m = events["spy_fwd_3M"].dropna().tolist()
    all_6m = events["spy_fwd_6M"].dropna().tolist()

    s3 = _stat(low_3m); s6 = _stat(low_6m)
    b3 = _stat(all_3m); b6 = _stat(all_6m)
    t3 = _welch_t(low_3m, all_3m) if s3 and b3 else 0
    t6 = _welch_t(low_6m, all_6m) if s6 and b6 else 0

    checks = []
    if s3 and b3:
        d = s3["avg"] - b3["avg"]
        c = d <= PASS_LOW_3M_DELTA
        checks.append((f"C1 low 3M avg ≤ baseline{PASS_LOW_3M_DELTA:+.1f}%",
                       f"delta={d:+.2f}%", c))
    else:
        checks.append(("C1 low 3M", "N/A", False))

    if s6 and b6:
        d = s6["avg"] - b6["avg"]
        c = d <= PASS_LOW_6M_DELTA
        checks.append((f"C2 low 6M avg ≤ baseline{PASS_LOW_6M_DELTA:+.1f}%",
                       f"delta={d:+.2f}%", c))
    else:
        checks.append(("C2 low 6M", "N/A", False))

    max_t = max(abs(t3), abs(t6))
    checks.append((f"C3 |t-stat| ≥ {PASS_T_STAT_MIN}",
                   f"max(3M={t3:.2f}, 6M={t6:.2f})={max_t:.2f}",
                   max_t >= PASS_T_STAT_MIN))
    n_low = s3["n"] if s3 else 0
    checks.append((f"C4 low bucket n ≥ {PASS_LOW_N_MIN}",
                   f"{n_low}", n_low >= PASS_LOW_N_MIN))

    for lbl, val, ok in checks:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {lbl}: {val}")

    n_pass = sum(1 for _, _, ok in checks if ok)
    print()
    if n_pass == 4:
        print("  → ✅ 建议接入 bond_monitor.macro_context: crack_spread_pct + crack_spread_signal")
        print("     Dashboard 显示 rolling 3y percentile, < 20 触发 warn")
    elif n_pass >= 2:
        print(f"  → ⚠ 边缘 ({n_pass}/4). Dashboard 显示不入决策")
    else:
        print(f"  → ✗ 淘汰 ({n_pass}/4). 未来若数据 pattern 变化可复跑")

    # 附: 当前 crack spread level (供参考)
    if len(events) > 0:
        latest = events.iloc[-1]
        print()
        print(f"[附] 最新一天 crack_spread = ${latest['crack']:.2f}/bbl, 3y percentile = {latest['crack_pct']:.1f}%")
        print(f"     ({latest.name})")


if __name__ == "__main__":
    _install_watchdog(WATCHDOG_SEC)
    run()
