"""_backtest_crack_spread_oos.py — 反向 crack spread 假设的 OOS 验证

Prior 分析 (_backtest_crack_spread.py, 2010-2024):
    假设 "low crack → SPY 下跌" 失败
    实际 "low crack → SPY 6M avg +10.66% 92.5% win, t=10.45"
    机制推测: crack 反向反映油价 / 通胀降温 → Fed 鸽 → SPY 涨

按 memory rule (feedback_data_snooping.md) 反向翻符号 = 新假设 → 必须新 OOS.

**设计 (跑前不改)**:

  Reference 窗口 (仅算冻结阈值, 不参与 test 统计):
    2015-01-01 → 2024-12-31
    从这里取 crack spread 的 20th / 80th 百分位作绝对边界

  OOS 窗口 (fresh, 从未测过此假设):
    2025-01-01 → 2026-09-01
    3M 有效期需 fwd 63 天数据可用, 6M 需 126 天
    保留所有 daily 观察

  Bucket (用冻结阈值):
    low  = crack ≤ q20_ref
    high = crack ≥ q80_ref
    mid  = 之间

  **反向假设通过条件 (硬编码, 跑前不改)**:
    C1: OOS low 3M avg SPY ≥ baseline + 1.5%
    C2: OOS low 6M avg SPY ≥ baseline + 2.5%
    C3: OOS low vs baseline |t-stat| ≥ 2.0
    C4: OOS low bucket n ≥ 15 (window 短放松)

  Verdict:
    4/4 → ✅ 反向假设 OOS validated, 接入 dashboard + 可入 confluence 作弱票 (< 1 分权重)
    2-3 → ⚠ 边缘, dashboard-only
    0-1 → ✗ prior 是过拟合, 结论作废

**Caveat**: SPY 2025+ 数据在 _backtest_rotation_regime.py 里也用过 (测 rotation vs momentum),
    严格意义上不是完全 fresh. 但 driver (rotation_speed vs crack_spread) 不同, 算不同假设.
    真正 100% independent OOS 需等 2026-10+ 更多数据.
"""
from __future__ import annotations

import math
import sys

sys.stdout.reconfigure(line_buffering=True)

REF_START = "2015-01-01"
REF_END   = "2024-12-31"
OOS_START = "2025-01-01"
OOS_END   = "2026-09-01"
LOW_PCT   = 20
HIGH_PCT  = 80
FWD_HORIZONS = {"3M": 63, "6M": 126}

# 反向假设通过条件 (跑前不改!)
PASS_LOW_3M_DELTA = +1.5   # low bucket 3M avg 应 ≥ baseline + 1.5%
PASS_LOW_6M_DELTA = +2.5
PASS_T_STAT_MIN   = 2.0
PASS_LOW_N_MIN    = 15


def _welch_t(x, y):
    if len(x) < 2 or len(y) < 2: return 0.0
    mx = sum(x) / len(x); my = sum(y) / len(y)
    vx = sum((a - mx) ** 2 for a in x) / (len(x) - 1)
    vy = sum((b - my) ** 2 for b in y) / (len(y) - 1)
    se = math.sqrt(vx / len(x) + vy / len(y))
    return (mx - my) / se if se > 0 else 0.0


def _stat(xs):
    if not xs: return None
    return {"n": len(xs),
            "avg": sum(xs) / len(xs),
            "win": sum(1 for r in xs if r > 0) / len(xs) * 100,
            "med": sorted(xs)[len(xs) // 2]}


def _pull(t, start, end):
    import yfinance as yf
    h = yf.Ticker(t).history(start=start, end=end, interval="1d", auto_adjust=True)
    if h is None or h.empty:
        return None
    h.index = [ts.date() for ts in h.index]
    return h["Close"].astype(float)


def run():
    import pandas as pd

    print("=" * 100)
    print(f"Crack Spread 反向假设 OOS 验证")
    print(f"  Reference: {REF_START} → {REF_END} (只取阈值)")
    print(f"  OOS:       {OOS_START} → {OOS_END} (fresh 测试窗口)")
    print("=" * 100)

    # 1. Reference 窗口 → 冻结阈值
    print("\n[1/3] 拉 reference 窗口 (2015-2024) 计算冻结阈值...")
    ref_dfs = {}
    for t in ["CL=F", "RB=F", "HO=F"]:
        s = _pull(t, REF_START, REF_END)
        if s is None:
            print(f"  {t} 失败, 退出")
            return
        ref_dfs[t] = s
    ref = pd.DataFrame(ref_dfs).dropna(how="any")
    ref["crack"] = (2 * ref["RB=F"] + ref["HO=F"]) * 42 / 3 - ref["CL=F"]
    q_low = float(ref["crack"].quantile(LOW_PCT / 100))
    q_high = float(ref["crack"].quantile(HIGH_PCT / 100))
    print(f"  n={len(ref)}   crack 范围 [{ref['crack'].min():.2f}, {ref['crack'].max():.2f}]")
    print(f"  冻结阈值: q{LOW_PCT}={q_low:.2f}  q{HIGH_PCT}={q_high:.2f}")

    # 2. OOS 窗口 → 用冻结阈值分桶 + fwd SPY
    print(f"\n[2/3] 拉 OOS 窗口 ({OOS_START} → {OOS_END}) + SPY...")
    oos_dfs = {}
    for t in ["CL=F", "RB=F", "HO=F", "SPY"]:
        s = _pull(t, OOS_START, OOS_END)
        if s is None:
            print(f"  {t} 失败, 退出")
            return
        oos_dfs[t] = s
    oos = pd.DataFrame(oos_dfs).dropna(how="any")
    oos["crack"] = (2 * oos["RB=F"] + oos["HO=F"]) * 42 / 3 - oos["CL=F"]
    print(f"  n={len(oos)}   OOS crack 范围 [{oos['crack'].min():.2f}, {oos['crack'].max():.2f}]")

    # fwd SPY (向前看)
    for lbl, days in FWD_HORIZONS.items():
        oos[f"spy_fwd_{lbl}"] = (oos["SPY"].shift(-days) / oos["SPY"] - 1) * 100

    # 分桶 (用 reference 冻结阈值)
    oos["bucket"] = "mid"
    oos.loc[oos["crack"] <= q_low, "bucket"] = "low"
    oos.loc[oos["crack"] >= q_high, "bucket"] = "high"

    bucket_dist = oos["bucket"].value_counts().to_dict()
    print(f"  OOS bucket 分布 (含无 fwd 的): {bucket_dist}")

    # 3. 分桶统计
    print()
    print("=" * 100)
    print("【OOS 结果】 每桶前向 SPY return")
    print("=" * 100)

    baseline = {}
    for lbl in FWD_HORIZONS:
        col = f"spy_fwd_{lbl}"
        vals = oos[col].dropna().tolist()
        baseline[lbl] = _stat(vals)
        if baseline[lbl]:
            b = baseline[lbl]
            print(f"  OOS baseline {lbl}: n={b['n']:<4} avg={b['avg']:>+7.2f}% win={b['win']:>5.1f}% median={b['med']:>+7.2f}%")

    for horizon_lbl in FWD_HORIZONS:
        col = f"spy_fwd_{horizon_lbl}"
        print()
        print(f"--- {horizon_lbl} fwd SPY (OOS, 用 ref 冻结阈值) ---")
        print(f"  {'bucket':<8} {'n':>5} {'avg':>9} {'win':>7} {'median':>9} {'Δ vs base':>10} {'t-stat':>8}")
        base_vals = oos[col].dropna().tolist()
        for b in ["low", "mid", "high"]:
            xs = oos[oos["bucket"] == b][col].dropna().tolist()
            s = _stat(xs)
            if not s:
                print(f"  {b:<8} —")
                continue
            d = s["avg"] - baseline[horizon_lbl]["avg"]
            t = _welch_t(xs, base_vals)
            print(f"  {b:<8} {s['n']:>5} {s['avg']:>+8.2f}% {s['win']:>6.1f}% {s['med']:>+8.2f}% "
                  f"{d:>+9.2f}% {t:>+8.2f}")

    # 4. VERDICT
    print()
    print("=" * 100)
    print("【VERDICT】 反向假设通过条件")
    print("=" * 100)

    low_3m = oos[oos["bucket"] == "low"]["spy_fwd_3M"].dropna().tolist()
    low_6m = oos[oos["bucket"] == "low"]["spy_fwd_6M"].dropna().tolist()
    all_3m = oos["spy_fwd_3M"].dropna().tolist()
    all_6m = oos["spy_fwd_6M"].dropna().tolist()

    s3 = _stat(low_3m); s6 = _stat(low_6m)
    b3 = _stat(all_3m); b6 = _stat(all_6m)
    t3 = _welch_t(low_3m, all_3m) if s3 and b3 else 0
    t6 = _welch_t(low_6m, all_6m) if s6 and b6 else 0

    checks = []
    if s3 and b3:
        d = s3["avg"] - b3["avg"]
        checks.append((f"C1 low 3M avg ≥ baseline+{PASS_LOW_3M_DELTA}%", f"delta={d:+.2f}%", d >= PASS_LOW_3M_DELTA))
    else:
        checks.append((f"C1 low 3M", "无 low 桶样本", False))

    if s6 and b6:
        d = s6["avg"] - b6["avg"]
        checks.append((f"C2 low 6M avg ≥ baseline+{PASS_LOW_6M_DELTA}%", f"delta={d:+.2f}%", d >= PASS_LOW_6M_DELTA))
    else:
        checks.append((f"C2 low 6M", "无 low 桶样本", False))

    max_t = max(abs(t3), abs(t6))
    checks.append((f"C3 |t-stat| ≥ {PASS_T_STAT_MIN}",
                   f"max(3M={t3:.2f}, 6M={t6:.2f})={max_t:.2f}",
                   max_t >= PASS_T_STAT_MIN))
    n_low = max(s3["n"] if s3 else 0, s6["n"] if s6 else 0)
    checks.append((f"C4 low bucket n ≥ {PASS_LOW_N_MIN}", f"{n_low}", n_low >= PASS_LOW_N_MIN))

    for lbl, val, ok in checks:
        print(f"  {'✓' if ok else '✗'} {lbl}: {val}")

    n_pass = sum(1 for _, _, ok in checks if ok)
    print()
    if n_pass == 4:
        print("  → ✅ 反向假设 OOS VALIDATED. 可接入 dashboard + confluence 弱票")
        print("     整体框架: crack spread ≤ q_low → 通胀降温 → SPY 长仓略偏乐观")
        verdict_val, should_int = "pass", True
    elif n_pass >= 2:
        print(f"  → ⚠ 边缘 ({n_pass}/4). 仅 dashboard 显示不入决策")
        verdict_val, should_int = "edge", False
    else:
        print(f"  → ✗ 拒 ({n_pass}/4). Prior 数据结论是过拟合, 反向假设 OOS 不成立")
        verdict_val, should_int = "reject", False

    try:
        from backtest_verdicts import write_verdict
        write_verdict(
            "crack_spread_oos",
            verdict_val,
            conclusion=f"{n_pass}/4 pass · low_n={s3['n'] if s3 else 0} · t3M={t3:+.2f} t6M={t6:+.2f}",
            metrics={"low_3m_avg": s3.get("avg") if s3 else None,
                     "low_6m_avg": s6.get("avg") if s6 else None,
                     "baseline_3m_avg": b3.get("avg") if b3 else None,
                     "baseline_6m_avg": b6.get("avg") if b6 else None,
                     "t3M": round(t3, 3), "t6M": round(t6, 3),
                     "checks_pass": n_pass},
            params={"ref_window": f"{REF_START} → {REF_END}",
                    "oos_window": f"{OOS_START} → {OOS_END}",
                    "q_low_ref": round(q_low, 2), "q_high_ref": round(q_high, 2)},
            next_review_days=180,  # OOS regime-specific 半年 review 一次
            should_integrate=should_int,
            recommendation="do NOT integrate to confluence" if not should_int else "wire to bond_monitor as leading indicator",
        )
        print("  [verdict] 写入 signals/backtest_verdicts/crack_spread_oos.json")
    except Exception as _e:
        print(f"  [verdict] 写入失败: {_e}")

    # 附
    if len(oos) > 0:
        latest = oos.iloc[-1]
        bucket_now = latest["bucket"]
        print()
        print(f"[附] 最新 crack = ${latest['crack']:.2f}/bbl → bucket={bucket_now} "
              f"(vs ref q{LOW_PCT}={q_low:.2f} q{HIGH_PCT}={q_high:.2f})  date={latest.name}")


if __name__ == "__main__":
    import os, threading
    threading.Timer(600, lambda: (print("\n[watchdog] 超时"), os._exit(2))).start()
    run()
