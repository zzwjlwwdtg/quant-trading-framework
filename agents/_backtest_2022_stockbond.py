"""
_backtest_2022_stockbond.py — 2022 股债双杀回测

验证新的 asia_scaler + corr_scaler overlay 若在 2022 危机期启用, 会不会
减小损失.

2022 背景:
  - Fed 从 3 月开始激进加息 (0 → 4.5%), 通胀 CPI YoY 峰 9.1% (6月)
  - SPY 全年 -19% (从 476 → 383)
  - IEI 全年 -10% (中久期最伤)
  - AGG 全年 -13%
  - GLD 全年 -1% (真避险起作用)
  - 股债 60d correlation 5月起转正, 一度 +0.45
  - USDJPY 从 115 → 152 (JP 干预 9-10 月)

对照 3 组组合:
  A. Baseline: SPY 50% + IEI 50% (传统 60/40 变种)
  B. Asia+corr overlay: 若 asia 或 corr broken 触发 → 减 IEI 加 GLD
  C. All-cash comparison

计算 2022-01-01 → 2022-12-31 各组合 NAV 变化.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta

sys.stdout.reconfigure(line_buffering=True)


def _fetch_ohlc(ticker: str, start: str, end: str) -> dict:
    """yfinance 拉一段时期日 close, 返回 {date: close}."""
    import yfinance as yf
    h = yf.Ticker(ticker).history(start=start, end=end)
    if h.empty:
        return {}
    return {ts.date(): float(px) for ts, px in h["Close"].items()}


def _rolling_corr_60d(spy: dict, ief: dict, dates: list) -> dict:
    """按日算 SPY vs IEF 60d 滚动 correlation."""
    import statistics
    common = sorted(set(spy) & set(ief) & set(dates))
    if len(common) < 62:
        return {}
    result = {}
    for i in range(61, len(common)):
        window = common[i-60:i+1]
        spy_rets = [spy[window[j]]/spy[window[j-1]] - 1 for j in range(1, len(window))]
        ief_rets = [ief[window[j]]/ief[window[j-1]] - 1 for j in range(1, len(window))]
        n = min(len(spy_rets), len(ief_rets))
        if n < 30: continue
        mean_s = sum(spy_rets[:n])/n
        mean_i = sum(ief_rets[:n])/n
        cov = sum((spy_rets[j]-mean_s)*(ief_rets[j]-mean_i) for j in range(n))/n
        std_s = statistics.stdev(spy_rets[:n])
        std_i = statistics.stdev(ief_rets[:n])
        if std_s > 0 and std_i > 0:
            result[common[i]] = round(cov / (std_s * std_i), 3)
    return result


def _run_portfolio(prices: dict, weights_by_date: dict) -> tuple[list, list]:
    """按日期 weight 时序模拟 rebalance 每月一次 NAV.

    prices: {ticker: {date: close}}
    weights_by_date: {date: {ticker: pct}} — 每个 rebalance 日的目标权重
    返回 (dates, navs)
    """
    all_dates = sorted(set().union(*[set(v) for v in prices.values()]))
    if not all_dates:
        return [], []
    nav = 100.0
    shares: dict = {}
    navs = []
    last_rebal = None
    for d in all_dates:
        # 若是 rebalance 日期, 按目标 weight 重新分配
        if d in weights_by_date:
            target = weights_by_date[d]
            # 计算当前 NAV
            if shares:
                nav = sum(shares.get(tk, 0) * prices[tk].get(d, prices[tk].get(last_rebal, 0))
                          for tk in shares)
            # 按目标 weight 买入
            shares = {}
            for tk, pct in target.items():
                if pct <= 0: continue
                px = prices[tk].get(d)
                if px is None: continue
                shares[tk] = (nav * pct / 100) / px
            last_rebal = d
        # 若已建仓, 每日重新计 NAV (mark-to-market)
        if shares:
            mv = sum(shares.get(tk, 0) * prices[tk].get(d, 0) for tk in shares)
            if mv > 0:
                nav = mv
        navs.append((d, nav))
    return [x[0] for x in navs], [x[1] for x in navs]


def main():
    print("== 2022 股债双杀回测 (Fed 激进加息 + 通胀高企) ==")
    start = "2022-01-01"
    end = "2023-01-01"

    print(f"\n拉取数据 {start} → {end} ...")
    prices = {}
    for tk in ["SPY", "IEI", "IEF", "GLD"]:
        prices[tk] = _fetch_ohlc(tk, start, end)
        if prices[tk]:
            first_d = min(prices[tk])
            last_d = max(prices[tk])
            perf = (prices[tk][last_d] / prices[tk][first_d] - 1) * 100
            print(f"  {tk}: {len(prices[tk])} bars · 全年 {perf:+.1f}%")

    # 每月月初 rebalance
    all_dates = sorted(set(prices["SPY"]))
    rebal_dates = [d for d in all_dates if d.day <= 5]  # 每月前 5 天第一个交易日
    rebal_dates_set = set()
    seen_months = set()
    for d in all_dates:
        if (d.year, d.month) not in seen_months:
            rebal_dates_set.add(d)
            seen_months.add((d.year, d.month))
    rebal_dates = sorted(rebal_dates_set)

    # 算 60d correlation
    print("\n算 SPY-IEF 60d 相关性...")
    corr_series = _rolling_corr_60d(prices["SPY"], prices["IEF"], all_dates)
    corr_dates = sorted(corr_series.keys())
    if corr_dates:
        first_corr = corr_series[corr_dates[0]]
        max_corr = max(corr_series.values())
        max_d = max(corr_series, key=corr_series.get)
        print(f"  首个 {corr_dates[0]}: {first_corr}")
        print(f"  峰值 {max_d}: {max_corr}")

    # 组合 A: 静态 60/40 (SPY 50, IEI 50), 每月 rebalance
    weights_A = {d: {"SPY": 50, "IEI": 50} for d in rebal_dates}

    # 组合 B: 动态 overlay
    #   基础 SPY 50 / IEI 50
    #   若 60d corr > 0.1 (broken) → IEI × 0.75, 加 GLD
    #   若 corr > 0.4 (extreme) → IEI × 0.5, 加 GLD 更多
    weights_B = {}
    for d in rebal_dates:
        corr = corr_series.get(d)
        if corr is None:
            corr = 0  # fallback
        if corr > 0.4:
            weights_B[d] = {"SPY": 40, "IEI": 25, "GLD": 35}
        elif corr > 0.1:
            weights_B[d] = {"SPY": 45, "IEI": 37.5, "GLD": 17.5}
        else:
            weights_B[d] = {"SPY": 50, "IEI": 50}

    # 组合 C: 全现金 (基准)
    # 就是常数 100 no growth

    dates_A, nav_A = _run_portfolio(prices, weights_A)
    dates_B, nav_B = _run_portfolio(prices, weights_B)

    if nav_A and nav_B:
        end_A = nav_A[-1]
        end_B = nav_B[-1]
        print(f"\n=== 结果 (start NAV = 100) ===")
        print(f"  A. 静态 60/40 (SPY+IEI):       末 NAV = {end_A:.1f}  ({end_A-100:+.1f}%)")
        print(f"  B. 动态 corr overlay:          末 NAV = {end_B:.1f}  ({end_B-100:+.1f}%)")
        print(f"  差异: overlay 相对 baseline = {end_B - end_A:+.1f}pp")

        # 最大回撤
        def max_dd(navs):
            peak = navs[0]
            dd = 0
            for v in navs:
                peak = max(peak, v)
                dd = min(dd, v/peak - 1)
            return dd * 100
        print(f"\n  A max DD: {max_dd(nav_A):.1f}%")
        print(f"  B max DD: {max_dd(nav_B):.1f}%")

        # Corr 触发次数统计
        broken_dates = [d for d in rebal_dates if corr_series.get(d, 0) > 0.1]
        extreme_dates = [d for d in rebal_dates if corr_series.get(d, 0) > 0.4]
        print(f"\n  月度 corr > 0.1 (broken): {len(broken_dates)}/{len(rebal_dates)} 次")
        print(f"  月度 corr > 0.4 (extreme): {len(extreme_dates)}/{len(rebal_dates)} 次")


if __name__ == "__main__":
    main()
