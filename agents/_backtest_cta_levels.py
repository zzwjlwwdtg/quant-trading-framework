"""_backtest_cta_levels.py — 验证"关键 support 破位 → CTA 系统性抛售"机制

分析师提的 CTA 触发位 (7620 / 7356 / 684) 是**当前市场特定**的模型输出,
无法用历史数据直接验证 SPY 那些绝对价位. 但我们能验证**机制**:
  "SPY 破 20d/50d 低点 + 放量 → 未来 5d 显著跌 (CTA-style forced selling)"

若机制成立 → 用同一方法算当前 CTA 等效 breach 位, 接入 event_trigger.
若不成立 → 分析师给的具体点位是 anecdotal, 不值得写 hard rule.

设计 (**头部硬编码, 跑前不改**):

  数据窗口: 2018-01-01 → 2024-12-31 (7 年, 未接触过)
  测试标的: SPY (代理 SPX)

  Trigger 定义 (5 种):
    T1: SPY close < 20d min
    T2: SPY close < 20d min AND vol > 1.3 × avg_20d
    T3: SPY close < 50d min
    T4: SPY close < 50d min AND vol > 1.5 × avg_50d
    T5: SPY close < 200d MA AND prev close ≥ 200d MA (fresh cross)

  绩效: 5d / 10d fwd close-to-close return
  Baseline: 全期无条件 5d / 10d return 分布

  通过条件 (硬编码, 跑前不改):
    C1: 至少 1 种 trigger 后 5d avg return ≤ baseline avg - 0.7%
    C2: 至少 1 种 trigger 后 5d win_rate ≤ baseline win - 8pp
    C3: 该 trigger 的 |t-stat| vs baseline ≥ 2.0
    C4: 该 trigger 历史样本 ≥ 20 (确保不是偶然)

  Verdict:
    通过 4/4 (且是同一个 trigger) → 值得接入
    通过 2-3    → 边缘, dashboard 显示不入决策
    通过 0-1    → 淘汰, 分析师给的点位不写 hard rule
"""
from __future__ import annotations

import math
import sys
from typing import Optional

sys.stdout.reconfigure(line_buffering=True)


DATA_START = "2018-01-01"
DATA_END   = "2024-12-31"
TICKER     = "SPY"
FWD_HORIZONS = [5, 10]

# 通过条件 (硬编码)
PASS_AVG_DELTA_MIN   = 0.7   # trigger avg 应 ≤ baseline avg - 0.7% (pp)
PASS_WIN_DELTA_MIN   = 8     # trigger win_rate 应 ≤ baseline win - 8 pp
PASS_T_STAT_MIN      = 2.0
PASS_MIN_N           = 20

WATCHDOG_SEC = 300


def _install_watchdog(n: int) -> None:
    import os
    import threading
    threading.Timer(n, lambda: (print(f"\n[watchdog] {n}s 超时, 强退"), os._exit(2))).start()


def _welch_t(x: list, y: list) -> float:
    if len(x) < 2 or len(y) < 2: return 0.0
    mx = sum(x) / len(x); my = sum(y) / len(y)
    vx = sum((a - mx) ** 2 for a in x) / (len(x) - 1)
    vy = sum((b - my) ** 2 for b in y) / (len(y) - 1)
    se = math.sqrt(vx / len(x) + vy / len(y))
    return (mx - my) / se if se > 0 else 0.0


def _stat(xs: list) -> dict:
    if not xs: return {"n": 0}
    avg = sum(xs) / len(xs)
    win = sum(1 for r in xs if r > 0) / len(xs) * 100
    med = sorted(xs)[len(xs) // 2]
    return {"n": len(xs), "avg": avg, "win": win, "med": med}


def run():
    import pandas as pd
    import yfinance as yf

    print("=" * 100)
    print(f"CTA 机制回测: {TICKER} {DATA_START} → {DATA_END}")
    print("=" * 100)

    # 拉 SPY
    df = yf.Ticker(TICKER).history(start=DATA_START, end=DATA_END,
                                    interval="1d", auto_adjust=True)
    if df is None or df.empty:
        print("! SPY 数据拉取失败")
        return
    close = df["Close"].astype(float)
    vol   = df["Volume"].astype(float)
    print(f"数据: {len(close)} trading days")

    # 各种滚动指标
    low_20d = close.rolling(20).min().shift(1)     # 昨日为止的 20d min
    low_50d = close.rolling(50).min().shift(1)
    ma_200  = close.rolling(200).mean()
    prev_close = close.shift(1)
    vol_avg_20 = vol.rolling(20).mean().shift(1)
    vol_avg_50 = vol.rolling(50).mean().shift(1)

    # fwd returns
    fwd = {}
    for h in FWD_HORIZONS:
        fwd[h] = close.shift(-h) / close - 1

    # 无条件 baseline (仅有 fwd 5d/10d 数据的日子)
    dates = list(close.index)
    baseline: dict = {h: [] for h in FWD_HORIZONS}
    triggers: dict = {
        "T1_break_20dmin":       {h: [] for h in FWD_HORIZONS},
        "T2_break_20dmin+vol":   {h: [] for h in FWD_HORIZONS},
        "T3_break_50dmin":       {h: [] for h in FWD_HORIZONS},
        "T4_break_50dmin+vol":   {h: [] for h in FWD_HORIZONS},
        "T5_cross_below_200ma":  {h: [] for h in FWD_HORIZONS},
    }

    for d in dates:
        c = close.loc[d]
        for h in FWD_HORIZONS:
            fv = fwd[h].loc[d]
            if fv != fv:  # NaN
                continue
            baseline[h].append(float(fv) * 100)

        # T1: close < 20d min
        l20 = low_20d.loc[d]
        if l20 == l20 and c < l20:
            for h in FWD_HORIZONS:
                fv = fwd[h].loc[d]
                if fv == fv:
                    triggers["T1_break_20dmin"][h].append(float(fv) * 100)

        # T2: T1 + vol > 1.3 × 20d avg
        v = vol.loc[d]; va = vol_avg_20.loc[d]
        if l20 == l20 and c < l20 and va == va and v > 1.3 * va:
            for h in FWD_HORIZONS:
                fv = fwd[h].loc[d]
                if fv == fv:
                    triggers["T2_break_20dmin+vol"][h].append(float(fv) * 100)

        # T3: close < 50d min
        l50 = low_50d.loc[d]
        if l50 == l50 and c < l50:
            for h in FWD_HORIZONS:
                fv = fwd[h].loc[d]
                if fv == fv:
                    triggers["T3_break_50dmin"][h].append(float(fv) * 100)

        # T4: T3 + vol > 1.5 × 50d avg
        v50a = vol_avg_50.loc[d]
        if l50 == l50 and c < l50 and v50a == v50a and v > 1.5 * v50a:
            for h in FWD_HORIZONS:
                fv = fwd[h].loc[d]
                if fv == fv:
                    triggers["T4_break_50dmin+vol"][h].append(float(fv) * 100)

        # T5: fresh cross below 200d MA
        m200 = ma_200.loc[d]; pc = prev_close.loc[d]
        if m200 == m200 and pc == pc and c < m200 and pc >= m200:
            for h in FWD_HORIZONS:
                fv = fwd[h].loc[d]
                if fv == fv:
                    triggers["T5_cross_below_200ma"][h].append(float(fv) * 100)

    # 报告
    print()
    for h in FWD_HORIZONS:
        print("=" * 100)
        print(f"【{h}d fwd return】")
        print("=" * 100)
        bl = _stat(baseline[h])
        print(f"  baseline (无条件):        n={bl['n']:<5} avg={bl['avg']:>+6.2f}%  win={bl['win']:>5.1f}%  median={bl['med']:>+6.2f}%")
        print()
        print(f"  {'trigger':<28} {'n':>5} {'avg':>8} {'win':>7} {'median':>8} {'Δ avg':>7} {'Δ win':>7} {'t-stat':>7}")
        print("  " + "-" * 90)
        for name, buckets in triggers.items():
            s = _stat(buckets[h])
            if s["n"] == 0:
                print(f"  {name:<28} {'-':>5}")
                continue
            d_avg = s["avg"] - bl["avg"]
            d_win = s["win"] - bl["win"]
            t = _welch_t(buckets[h], baseline[h])
            print(f"  {name:<28} {s['n']:>5} {s['avg']:>+7.2f}% {s['win']:>6.1f}% {s['med']:>+7.2f}% "
                  f"{d_avg:>+6.2f}% {d_win:>+6.1f} {t:>+7.2f}")
        print()

    # verdict: 只看 5d, 找最好的 trigger
    print("=" * 100)
    print("【VERDICT】 通过条件评估 (5d)")
    print("=" * 100)
    bl5 = _stat(baseline[5])
    best_trigger = None
    best_score = 0
    for name, buckets in triggers.items():
        s = _stat(buckets[5])
        if s["n"] < PASS_MIN_N:
            continue
        d_avg = s["avg"] - bl5["avg"]
        d_win = s["win"] - bl5["win"]
        t = _welch_t(buckets[5], baseline[5])
        c1 = d_avg <= -PASS_AVG_DELTA_MIN
        c2 = d_win <= -PASS_WIN_DELTA_MIN
        c3 = abs(t) >= PASS_T_STAT_MIN
        c4 = s["n"] >= PASS_MIN_N
        score = sum([c1, c2, c3, c4])
        print(f"\n  {name}:")
        print(f"    C1 avg Δ ≤ -{PASS_AVG_DELTA_MIN}%: {d_avg:+.2f}%  {'✓' if c1 else '✗'}")
        print(f"    C2 win Δ ≤ -{PASS_WIN_DELTA_MIN} pp: {d_win:+.1f} pp {'✓' if c2 else '✗'}")
        print(f"    C3 |t| ≥ {PASS_T_STAT_MIN}: {abs(t):.2f}  {'✓' if c3 else '✗'}")
        print(f"    C4 n ≥ {PASS_MIN_N}: {s['n']}  {'✓' if c4 else '✗'}")
        print(f"    score: {score}/4")
        if score > best_score:
            best_score = score
            best_trigger = name

    print()
    print("=" * 100)
    if best_trigger and best_score == 4:
        print(f"  → ✅ 建议接入: {best_trigger} (4/4 pass)")
        print(f"     实现: 在 auto_rebalance._EVENT_TRIGGERS 加此条件, level=当前对应位")
        verdict_val, should_int = "pass", True
    elif best_trigger and best_score >= 2:
        print(f"  → ⚠ 边缘: {best_trigger} ({best_score}/4). Dashboard 显示, 不入决策")
        verdict_val, should_int = "edge", False
    else:
        print(f"  → ✗ 淘汰: 所有 trigger 均未通过 (最好 {best_score}/4)")
        print(f"     分析师给的 CTA 具体点位 (7620/7356) 是 anecdotal, 不写 hard rule")
        verdict_val, should_int = "reject", False

    try:
        from backtest_verdicts import write_verdict
        write_verdict(
            "cta_levels",
            verdict_val,
            conclusion=f"best trigger={best_trigger or '无'} score={best_score}/4",
            metrics={"best_trigger": best_trigger, "best_score": best_score},
            params={"ticker": TICKER, "data_start": DATA_START, "data_end": DATA_END,
                    "pass_avg_delta_min": PASS_AVG_DELTA_MIN,
                    "pass_win_delta_min": PASS_WIN_DELTA_MIN,
                    "pass_t_stat_min": PASS_T_STAT_MIN},
            next_review_days=180,   # CTA levels 半年 review 一次即可
            should_integrate=should_int,
            recommendation="do NOT hard-code analyst-provided levels" if not should_int else f"integrate {best_trigger} into auto_rebalance triggers",
        )
        print("  [verdict] 写入 signals/backtest_verdicts/cta_levels.json")
    except Exception as _e:
        print(f"  [verdict] 写入失败: {_e}")


if __name__ == "__main__":
    _install_watchdog(WATCHDOG_SEC)
    run()
