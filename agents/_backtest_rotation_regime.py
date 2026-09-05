"""_backtest_rotation_regime.py — 验证"快速轮动 → momentum 衰减"在 US ETF 池

原始研究 (A股, 2026-08 财新): 主线不留存率 rolling 3 年分位数 → 高分位 = 快速轮动
  · PB-ROE / value 强势
  · momentum 衰减 (月均 -116 bps in high rotation vs low rotation)

移植到 US 前必须本地 OOS (memory: 跨市场移植 = 新假设).

设计 (**头部硬编码, 跑前不改**):

  Sector universe (算 rotation):
    SPY QQQ SMH GLD TLT XLE XLF XLV IWM EFA (10 大类)

  Rotation 指标:
    每周五 close 时算 10 sector 过去 20d 收益 → 排名 → 记 top-1
    rolling 12-week 窗口内 top-1 换人次数 = rotation_speed_index (0-12)

  Momentum signal (测试对象):
    每交易日: 20d cumulative return > 0 → "momentum 信号"
    fwd 5d close-to-close return 作为绩效

  测试标的:
    TQQQ SOXL NVDA MSFT AAPL (5 只你 watchlist 里动量特征强的)

  数据窗口 (未接触过 BVC 假设):
    2024-01-01 → 2026-06-10 (~30 months daily)
    跟 BVC 用的 2026-06-11 → 2026-08-20 60d 窗口不重叠

  Train/Test:
    2024-01-01 → 2025-11-30 (~24 months) = train, 决定 rotation tertile 分位阈值
    2025-12-01 → 2026-06-10 (~6 months) = test, 报 OOS 结论

  通过条件 (硬编码):
    C1: high rotation 桶  momentum 5d win_rate ≤ 45%
    C2: low  rotation 桶  momentum 5d win_rate ≥ 52%
    C3: high vs low diff t-stat |t| ≥ 2.0 (5% two-sided)
    C4: high 桶 avg ret ≤ -0.3%, low 桶 avg ret ≥ +0.2%

  Verdict:
    通过 4/4 → 建议加入 regime_today (rotation dimension) + 调 confluence
    通过 2-3 → 边缘, 只作 dashboard verifier
    通过 0-1 → A股结论在 US ETF 池不成立, 淘汰

CLI: python _backtest_rotation_regime.py
"""
from __future__ import annotations

import math
import sys
from datetime import date
from typing import Optional

sys.stdout.reconfigure(line_buffering=True)


# ── 硬编码配置 (跑前不改) ────────────────────────────────────────────────
SECTORS   = ["SPY", "QQQ", "SMH", "GLD", "TLT", "XLE", "XLF", "XLV", "IWM", "EFA"]
TARGETS   = ["TQQQ", "SOXL", "NVDA", "MSFT", "AAPL"]
DATA_START = "2024-01-01"
DATA_END   = "2026-06-10"
TRAIN_END  = date(2025, 11, 30)          # 分位阈值在 train 上确定, test 冻结
MOMENTUM_LOOKBACK = 20                   # 20d cum ret > 0 = momentum on
FWD_HORIZON       = 5                    # 5d fwd return
ROTATION_WINDOW_WEEKS = 12               # rolling 12-week 换人次数

# 通过条件
PASS_WIN_HIGH = 45.0      # % (high rotation 桶应 ≤ 此值)
PASS_WIN_LOW  = 52.0      # % (low rotation 桶应 ≥ 此值)
PASS_AVG_HIGH = -0.3      # % (high rotation avg 5d ret 应 ≤)
PASS_AVG_LOW  = +0.2      # % (low rotation avg 5d ret 应 ≥)
PASS_T_STAT   = 2.0       # |t| 应 ≥ (Welch t-test)

# 15 分钟 watchdog
WATCHDOG_SEC = 900


def _install_watchdog(timeout_sec: int) -> None:
    import os
    import threading
    def _killer():
        print(f"\n[watchdog] {timeout_sec}s 超时, 强制退出防止僵尸")
        os._exit(2)
    t = threading.Timer(timeout_sec, _killer)
    t.daemon = True
    t.start()


def _pull_daily(ticker: str) -> Optional["pd.DataFrame"]:
    import yfinance as yf
    try:
        df = yf.Ticker(ticker).history(start=DATA_START, end=DATA_END,
                                        interval="1d", auto_adjust=True)
    except Exception as ex:
        print(f"  [{ticker}] pull failed: {ex}")
        return None
    if df is None or df.empty:
        return None
    df = df.copy()
    df.index = [ts.date() for ts in df.index]
    return df[["Close"]]


def _welch_t(x: list, y: list) -> float:
    """Welch's t-test: 不同方差假设下 x vs y 均值差的 t 统计量."""
    if len(x) < 2 or len(y) < 2:
        return 0.0
    mx = sum(x) / len(x); my = sum(y) / len(y)
    vx = sum((a - mx) ** 2 for a in x) / (len(x) - 1)
    vy = sum((a - my) ** 2 for a in y) / (len(y) - 1)
    se = math.sqrt(vx / len(x) + vy / len(y))
    if se == 0:
        return 0.0
    return (mx - my) / se


def run():
    import pandas as pd

    print("=" * 100)
    print("Rotation Regime × Momentum 回测 (A股结论移植 US ETF 池)")
    print("=" * 100)
    print(f"数据窗口: {DATA_START} → {DATA_END}   train/test 切点: {TRAIN_END}")
    print()

    # 1) 拉 10 sector daily
    print("[1/4] 拉 10 sector daily...")
    sector_data = {}
    for s in SECTORS:
        df = _pull_daily(s)
        if df is not None:
            sector_data[s] = df
            print(f"  {s:5s} n={len(df)}")
        else:
            print(f"  {s:5s} 拉取失败")
    if len(sector_data) < 5:
        print("! sector 数据不足, 无法算 rotation, 退出")
        return

    # 2) 算 rotation_speed_index
    print("\n[2/4] 算 rotation_speed_index (rolling 12-week top-1 换人次数)...")
    # 合并所有 sector 的 close 到一个 DataFrame
    sector_close = pd.DataFrame({s: d["Close"] for s, d in sector_data.items()})
    sector_close = sector_close.dropna(how="any")
    # 20d cumulative return
    sector_ret_20d = sector_close.pct_change(MOMENTUM_LOOKBACK)
    # 剔除全 NaN 行 (前 MOMENTUM_LOOKBACK 天), 避 pandas FutureWarning
    sector_ret_20d = sector_ret_20d.dropna(how="all")
    # 每交易日 top-1 sector
    top1 = sector_ret_20d.idxmax(axis=1)
    # 每周五取样 (weekly resample)
    top1_weekly = top1.resample("W-FRI") if hasattr(top1.index, "week") else None
    # top1 是 Series, index 是 date. 转 datetime 再 resample
    top1.index = pd.to_datetime(top1.index)
    top1_weekly = top1.resample("W-FRI").last().dropna()
    # 每周 top-1 是否变化
    top1_change = (top1_weekly != top1_weekly.shift(1)).astype(int)
    # rolling 12-week 变化次数
    rotation_speed = top1_change.rolling(ROTATION_WINDOW_WEEKS).sum()
    rotation_speed = rotation_speed.dropna()
    rotation_speed.index = [d.date() for d in rotation_speed.index]
    print(f"  有效周数: {len(rotation_speed)}   "
          f"rotation_speed range: {int(rotation_speed.min())}-{int(rotation_speed.max())}")

    # 3) 用 train 决定 tertile 分位阈值
    print("\n[3/4] 用 train 段决定 tertile 阈值...")
    train_rs = rotation_speed[rotation_speed.index <= TRAIN_END]
    test_rs  = rotation_speed[rotation_speed.index >  TRAIN_END]
    if len(train_rs) < 20 or len(test_rs) < 5:
        print(f"! train n={len(train_rs)} / test n={len(test_rs)}, 样本不足")
        return
    q33 = train_rs.quantile(1/3)
    q67 = train_rs.quantile(2/3)
    print(f"  train n={len(train_rs)}   test n={len(test_rs)}")
    print(f"  q33={q33:.1f}   q67={q67:.1f}")
    print(f"  test bucket 分布: "
          f"low={sum(test_rs <= q33)}   mid={sum((test_rs > q33) & (test_rs < q67))}   "
          f"high={sum(test_rs >= q67)}")

    # 每周分桶: date → "low"/"mid"/"high"
    def _bucket(rs_val):
        if rs_val <= q33: return "low"
        if rs_val >= q67: return "high"
        return "mid"
    test_bucket = test_rs.apply(_bucket)

    # 4) 拉 target ticker daily → 生成 momentum 信号 + 5d fwd → 按分桶统计
    print("\n[4/4] 生成 momentum 信号 + 分桶 fwd return...")
    all_events: list[dict] = []
    for tk in TARGETS:
        df = _pull_daily(tk)
        if df is None or len(df) < MOMENTUM_LOOKBACK + FWD_HORIZON + 5:
            print(f"  {tk}: 数据不足, 跳过")
            continue
        close = df["Close"]
        ret_20d = close.pct_change(MOMENTUM_LOOKBACK)
        # 每天 momentum 信号 = ret_20d > 0
        # fwd_5d = close.shift(-5) / close - 1
        fwd_5d = close.shift(-FWD_HORIZON) / close - 1
        # 只保留 test 窗口
        for d in close.index:
            if d <= TRAIN_END or d > date.fromisoformat(DATA_END):
                continue
            r20 = ret_20d.get(d)
            f5  = fwd_5d.get(d)
            if r20 is None or f5 is None:
                continue
            try:
                if math.isnan(r20) or math.isnan(f5):
                    continue
            except TypeError:
                continue
            if r20 <= 0:
                continue   # 只统计 momentum 触发的信号
            # 找该 date 对应的最近一个 weekly rotation bucket
            # 用 <= d 的最近一个 rotation_speed 索引
            bucket_dates = [bd for bd in test_bucket.index if bd <= d]
            if not bucket_dates:
                continue
            bucket = test_bucket[bucket_dates[-1]]
            all_events.append({
                "ticker": tk,
                "date":   d,
                "ret_20d": float(r20),
                "fwd_5d":  float(f5) * 100,   # %
                "bucket":  bucket,
            })
        n_tk = sum(1 for e in all_events if e["ticker"] == tk)
        print(f"  {tk}: {n_tk} momentum 触发事件")

    if not all_events:
        print("\n! 无有效事件, 退出")
        return

    # 分桶统计
    print("\n" + "=" * 100)
    print(f"【结果】 test 窗口 momentum 信号 fwd {FWD_HORIZON}d return 按 rotation bucket")
    print("=" * 100)
    print(f"{'bucket':<8} {'n':>6} {'avg_ret':>10} {'win_rate':>10} {'median':>10}")
    print("-" * 60)
    by_bucket = {"low": [], "mid": [], "high": []}
    for e in all_events:
        by_bucket[e["bucket"]].append(e["fwd_5d"])
    stats = {}
    for b in ("low", "mid", "high"):
        xs = by_bucket[b]
        if not xs:
            stats[b] = None
            print(f"{b:<8} {'-':>6} {'-':>10} {'-':>10} {'-':>10}")
            continue
        avg = sum(xs) / len(xs)
        win = sum(1 for r in xs if r > 0) / len(xs) * 100
        med = sorted(xs)[len(xs) // 2]
        stats[b] = {"n": len(xs), "avg": avg, "win": win, "med": med}
        print(f"{b:<8} {len(xs):>6} {avg:>+9.2f}% {win:>9.1f}% {med:>+9.2f}%")

    # t-test high vs low
    if stats.get("high") and stats.get("low"):
        t = _welch_t(by_bucket["high"], by_bucket["low"])
        print(f"\nWelch t-stat (high vs low avg_ret 差异): {t:+.2f}")
    else:
        t = 0.0
        print("\nWelch t-stat: 桶样本不足")

    # verdict
    print("\n" + "=" * 100)
    print("【VERDICT】 通过条件评估")
    print("=" * 100)
    checks = []

    if stats.get("high"):
        c1 = stats["high"]["win"] <= PASS_WIN_HIGH
        checks.append((f"C1: high rotation win_rate ≤ {PASS_WIN_HIGH}%",
                       f"{stats['high']['win']:.1f}%", c1))
    else:
        checks.append((f"C1: high rotation win_rate ≤ {PASS_WIN_HIGH}%", "N/A", False))

    if stats.get("low"):
        c2 = stats["low"]["win"] >= PASS_WIN_LOW
        checks.append((f"C2: low rotation win_rate ≥ {PASS_WIN_LOW}%",
                       f"{stats['low']['win']:.1f}%", c2))
    else:
        checks.append((f"C2: low rotation win_rate ≥ {PASS_WIN_LOW}%", "N/A", False))

    if stats.get("high") and stats.get("low"):
        c3 = abs(t) >= PASS_T_STAT
        checks.append((f"C3: |t-stat| ≥ {PASS_T_STAT}", f"{abs(t):.2f}", c3))
        c4a = stats["high"]["avg"] <= PASS_AVG_HIGH
        c4b = stats["low"]["avg"]  >= PASS_AVG_LOW
        c4 = c4a and c4b
        checks.append((f"C4: high avg ≤ {PASS_AVG_HIGH}% AND low avg ≥ {PASS_AVG_LOW}%",
                       f"high={stats['high']['avg']:+.2f}% low={stats['low']['avg']:+.2f}%", c4))
    else:
        checks.append(("C3: |t-stat| ≥ 2.0", "N/A", False))
        checks.append(("C4: avg return spread", "N/A", False))

    for lbl, val, ok in checks:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {lbl}: {val}")

    n_pass = sum(1 for _, _, ok in checks if ok)
    print()
    if n_pass == 4:
        print("  → ✅ 建议加入 regime_today (rotation dimension), fast rotation 削 momentum 权重")
        verdict_val, should_int = "pass", True
    elif n_pass >= 2:
        print(f"  → ⚠ 边缘 ({n_pass}/4). 只做 dashboard verifier, 不入 confluence 打分")
        verdict_val, should_int = "edge", False
    else:
        print(f"  → ✗ 淘汰 ({n_pass}/4). A股 PB-ROE / 快速轮动结论在 US ETF 池不成立")
        verdict_val, should_int = "reject", False

    try:
        from backtest_verdicts import write_verdict
        write_verdict(
            "rotation_regime",
            verdict_val,
            conclusion=f"{n_pass}/4 pass · high_win={stats.get('high',{}).get('win','?')} low_win={stats.get('low',{}).get('win','?')} t={t:+.2f}",
            metrics={"n_events": len(all_events),
                     "high_avg": stats.get("high", {}).get("avg") if stats.get("high") else None,
                     "low_avg": stats.get("low", {}).get("avg") if stats.get("low") else None,
                     "t_stat": round(t, 3),
                     "checks_pass": n_pass},
            params={"targets": TARGETS, "sectors": SECTORS,
                    "window_weeks": ROTATION_WINDOW_WEEKS,
                    "momentum_lookback": MOMENTUM_LOOKBACK,
                    "fwd_horizon": FWD_HORIZON},
            next_review_days=90,
            should_integrate=should_int,
            recommendation="rotation_speed only dashboard verifier" if not should_int else "wire into regime_today",
        )
        print("  [verdict] 写入 signals/backtest_verdicts/rotation_regime.json")
    except Exception as _e:
        print(f"  [verdict] 写入失败: {_e}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--window-weeks", type=int, default=ROTATION_WINDOW_WEEKS,
                        help=f"rolling window (weeks) 换人计数, default={ROTATION_WINDOW_WEEKS}")
    args = parser.parse_args()
    if args.window_weeks != ROTATION_WINDOW_WEEKS:
        ROTATION_WINDOW_WEEKS = args.window_weeks
        print(f"[敏感性] 使用 window_weeks={args.window_weeks} (非默认 12)")
        print(f"[NOTE] 通过阈值 (PASS_WIN_HIGH/LOW, PASS_AVG_HIGH/LOW, PASS_T_STAT) 保持不变")
        print(f"       memory rule: 敏感性分析可换 window/horizon, 但阈值不可后调 (p-hacking)")
        print()
    _install_watchdog(WATCHDOG_SEC)
    run()
