"""
_backtest_overlay_value.py — 回测 auto_rebalance 8 层 overlay 是否真值得

问题: 系统有 8 层 overlay + 5 event triggers, 用户担心 "两周就忘了".
数据证明 keep full 还是 simplify: 用 2022-2024 历史 3 策略对比:

  1. Baseline (60/40 静态): SPY 60% + IEI 40%, 无 rebalance
  2. Simple (3 rules): 基础配置 + 3 个可重建 event trigger
     - MOVE > 140 → IEI × 0.75, GLD × 1.3
     - KBE/SPY 20d < -10% → MSFT × 0.3, GLD × 1.5
     - sb_corr > 0.1 → GLD × 1.2 (脆弱性加 hedge)
  3. Full-overlay (可重建部分): 加 liq_level + sb_corr scaler 完整版
     - 不含 chain_blocked_at / asia_repat (需 AI CLI/nowcast, 历史无法重建)

评分维度:
  - 年化收益 (annualized return)
  - 最大回撤 (max drawdown)
  - Sharpe ratio (return / stdev)
  - 若 Simple ≈ Full (< 2pp/年 差), 则 keep full 无价值 → simplify
  - 若 Full > Simple 显著, 保留复杂度 + notification 补记忆

窗口: 2022-01-01 → 2024-12-31 (覆盖 2022 双杀 + 2023 SVB + 2024 恢复)
"""
from __future__ import annotations

import statistics
import sys
import urllib.request
import json
from datetime import date, datetime, timedelta

sys.stdout.reconfigure(line_buffering=True)

from config import FRED_API_KEY

START = "2022-01-01"
END = "2024-12-31"


def _get_yf(ticker: str) -> dict:
    """yfinance 拉 max 历史, 返 {date: close}."""
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(start=START, end=END)
        if h.empty:
            return {}
        return {ts.date(): float(px) for ts, px in h["Close"].items()}
    except Exception as e:
        print(f"  {ticker} err: {e}")
        return {}


def _get_fred(sid: str) -> dict:
    url = (f"https://api.stlouisfed.org/fred/series/observations"
           f"?series_id={sid}&api_key={FRED_API_KEY}&file_type=json"
           f"&observation_start={START}&observation_end={END}&sort_order=asc")
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            d = json.loads(r.read())
    except Exception as e:
        print(f"  FRED {sid} err: {e}")
        return {}
    return {date.fromisoformat(o["date"]): float(o["value"])
            for o in d.get("observations", [])
            if o.get("value") not in ("", ".", None)}


def _rolling_corr_60d(spy: dict, ief: dict, all_dates: list) -> dict:
    """SPY-IEF 60d 相关性."""
    common = [d for d in all_dates if d in spy and d in ief]
    result = {}
    for i in range(61, len(common)):
        window = common[i-60:i+1]
        try:
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
                result[common[i]] = cov / (std_s * std_i)
        except Exception:
            continue
    return result


def _kbe_spy_20d(kbe: dict, spy: dict, all_dates: list) -> dict:
    """KBE/SPY 20d 相对变化 (%)."""
    common = [d for d in all_dates if d in kbe and d in spy]
    ratios = {d: kbe[d] / spy[d] for d in common}
    result = {}
    for i in range(20, len(common)):
        d = common[i]
        d_20 = common[i-20]
        result[d] = (ratios[d] / ratios[d_20] - 1) * 100
    return result


def _hy_oas_pct_from_fred(hy: dict) -> dict:
    """FRED BAMLH0A0HYM2 单位 %, 转 bps."""
    return {d: v * 100 for d, v in hy.items()}


def _run_portfolio(prices: dict, weights_over_time: dict,
                    all_dates: list, name: str = "") -> dict:
    """按日期 weight 时序模拟 rebalance 每月一次 NAV.

    weights_over_time: {date: {ticker_or_'cash': pct}}, 每 rebal 日期一条
    Returns: {dates, navs, monthly_returns, max_dd, annualized_return, sharpe}
    """
    nav = 100.0
    shares: dict = {}
    cash = 0.0
    navs = []
    for d in all_dates:
        # Rebalance if date is in weights_over_time
        if d in weights_over_time:
            # Compute current NAV
            if shares or cash > 0:
                mv = sum(shares.get(tk, 0) * prices[tk].get(d, prices[tk].get(prev_d, 0))
                         for tk in shares if tk in prices)
                nav = mv + cash
            # Reallocate
            target = weights_over_time[d]
            shares = {}
            cash = 0.0
            for tk, pct in target.items():
                if pct <= 0: continue
                if tk == "cash":
                    cash += nav * pct / 100
                    continue
                px = prices.get(tk, {}).get(d)
                if px is None or px <= 0:
                    continue
                shares[tk] = (nav * pct / 100) / px
        # Mark-to-market NAV
        if shares or cash > 0:
            mv = sum(shares.get(tk, 0) * prices[tk].get(d, 0)
                     for tk in shares if tk in prices)
            if mv > 0 or cash > 0:
                nav = mv + cash
        navs.append((d, nav))
        prev_d = d
    # Metrics
    values = [n[1] for n in navs]
    n_days = len(values)
    if n_days < 2:
        return {"error": "no_data"}
    years = n_days / 252
    total_ret = values[-1] / values[0] - 1
    annualized = (values[-1] / values[0]) ** (1 / max(years, 0.01)) - 1
    # Max DD
    peak = values[0]
    max_dd = 0
    for v in values:
        peak = max(peak, v)
        max_dd = min(max_dd, v / peak - 1)
    # Sharpe (daily)
    if n_days > 30:
        daily_rets = [values[i]/values[i-1] - 1 for i in range(1, n_days)]
        if daily_rets:
            mean_r = sum(daily_rets) / len(daily_rets)
            std_r = statistics.stdev(daily_rets) if len(daily_rets) > 1 else 0
            sharpe = (mean_r * 252) / (std_r * (252 ** 0.5)) if std_r > 0 else 0
        else:
            sharpe = 0
    else:
        sharpe = 0
    return {
        "name": name,
        "start_nav": values[0],
        "end_nav": values[-1],
        "total_return_pct": round(total_ret * 100, 2),
        "annualized_pct": round(annualized * 100, 2),
        "max_dd_pct": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 2),
        "n_days": n_days,
    }


def main():
    print("=" * 90)
    print("Overlay 价值回测: 2022-01-01 → 2024-12-31 (3 年, 覆盖双杀+SVB+恢复)")
    print("=" * 90)

    print("\n拉取历史数据...")
    tickers = ["SPY", "IEI", "SHY", "MSFT", "GLD", "KBE", "MOVE"]
    prices = {}
    for tk in tickers:
        # ^MOVE 特殊 (yfinance ticker 有 ^)
        actual_tk = "^MOVE" if tk == "MOVE" else tk
        prices[tk] = _get_yf(actual_tk)
        print(f"  {tk}: {len(prices[tk])} bars" + (f" ({min(prices[tk])} → {max(prices[tk])})" if prices[tk] else ""))
    hy = _hy_oas_pct_from_fred(_get_fred("BAMLH0A0HYM2"))
    print(f"  HY OAS (FRED): {len(hy)} obs")

    # 用 SPY 的日期作为基准
    all_dates = sorted(prices["SPY"].keys())
    print(f"\n交易日总数: {len(all_dates)}")

    # 每月 rebalance 日 (每月第一个交易日)
    rebal_dates = []
    seen_months = set()
    for d in all_dates:
        if (d.year, d.month) not in seen_months:
            rebal_dates.append(d)
            seen_months.add((d.year, d.month))
    print(f"月度 rebalance 日: {len(rebal_dates)}")

    # 计算 signals
    print("\n计算历史 signals...")
    sb_corr = _rolling_corr_60d(prices["SPY"], prices["IEI"], all_dates)
    kbe_20d = _kbe_spy_20d(prices["KBE"], prices["SPY"], all_dates)
    move = prices["MOVE"]
    print(f"  sb_corr: {len(sb_corr)} · KBE/SPY 20d: {len(kbe_20d)} · MOVE: {len(move)} · HY OAS: {len(hy)}")

    # ─── 策略 1: Baseline 60/40 ─────────────────────
    baseline_weights = {d: {"SPY": 60, "IEI": 40} for d in rebal_dates}

    # ─── 策略 2: Simple (3 rules) ─────────────────────
    def simple_weights(d):
        base = {"SHY": 30, "IEI": 20, "MSFT": 20, "GLD": 10, "cash": 20}
        w = dict(base)
        m = move.get(d, 0)
        k = kbe_20d.get(d, 0)
        c = sb_corr.get(d, -0.3)
        if m >= 140:
            w["IEI"] *= 0.75
            w["GLD"] *= 1.3
        if k <= -10:
            w["MSFT"] *= 0.3
            w["GLD"] *= 1.5
        if c > 0.1:
            w["GLD"] *= 1.2
        # renormalize (加 cash 吸收缺口)
        total = sum(w.values())
        w["cash"] = max(0, base["cash"] + (100 - total))
        return w
    simple_ww = {d: simple_weights(d) for d in rebal_dates}

    # ─── 策略 3: Full-ish (含 liq_level + corr + event triggers) ─────
    def full_weights(d):
        base = {"SHY": 30, "IEI": 20, "MSFT": 20, "GLD": 10, "cash": 20}
        w = dict(base)
        m = move.get(d, 0)
        k = kbe_20d.get(d, 0)
        c = sb_corr.get(d, -0.3)
        h = hy.get(d, 0)
        # liq_level (approx)
        liq_bad = m >= 140 or k <= -10 or h >= 400
        liq_extreme = m >= 180 or k <= -15 or h >= 500
        # regime approx: risk_off if bad; crisis if extreme
        # regime_scaler (bond up, cloud down in risk_off/crisis)
        if liq_extreme:
            w["IEI"] *= 0.5; w["MSFT"] *= 0.3; w["GLD"] *= 1.5
        elif liq_bad:
            w["IEI"] *= 0.75; w["MSFT"] *= 0.6; w["GLD"] *= 1.3
        # sb_corr overlay (fragility, add hedge only, don't cut bond)
        if c > 0.4:
            w["GLD"] *= 1.3
        elif c > 0.1:
            w["GLD"] *= 1.2
        elif c > -0.3:
            w["GLD"] *= 1.05
        # Event triggers (5 rules, but recreatable subset)
        if m >= 180:
            w["IEI"] *= 0.6; w["GLD"] *= 1.2
        if k <= -10:
            w["MSFT"] *= 0.5; w["GLD"] *= 1.3
        if h >= 400:
            w["MSFT"] *= 0.7
        # renormalize
        total = sum(w.values())
        w["cash"] = max(0, 100 - (total - base["cash"]))
        return w
    full_ww = {d: full_weights(d) for d in rebal_dates}

    # 跑三个组合
    print("\n运行 3 策略...")
    r1 = _run_portfolio(prices, baseline_weights, all_dates, "Baseline 60/40")
    r2 = _run_portfolio(prices, simple_ww, all_dates, "Simple (3 rules)")
    r3 = _run_portfolio(prices, full_ww, all_dates, "Full overlays (可重建部分)")

    # 报告
    print("\n" + "=" * 90)
    print("回测结果对比 (2022-01-01 → 2024-12-31)")
    print("=" * 90)
    print(f"{'策略':<32} | {'年化%':<8} | {'总收益%':<10} | {'MaxDD%':<10} | {'Sharpe':<8}")
    print("-" * 90)
    for r in [r1, r2, r3]:
        print(f"{r['name']:<32} | {r['annualized_pct']:>+6.2f}% | {r['total_return_pct']:>+7.2f}% | {r['max_dd_pct']:>+7.2f}% | {r['sharpe']:>6.2f}")

    # 关键判断
    print("\n" + "=" * 90)
    print("Overlay 价值判断")
    print("=" * 90)
    delta_simple = r2["annualized_pct"] - r1["annualized_pct"]
    delta_full = r3["annualized_pct"] - r1["annualized_pct"]
    delta_full_vs_simple = r3["annualized_pct"] - r2["annualized_pct"]
    dd_simple = r2["max_dd_pct"] - r1["max_dd_pct"]
    dd_full = r3["max_dd_pct"] - r1["max_dd_pct"]
    dd_full_vs_simple = r3["max_dd_pct"] - r2["max_dd_pct"]

    print(f"\n  Simple vs Baseline: 年化 {delta_simple:+.2f}pp, MaxDD {dd_simple:+.2f}pp")
    print(f"  Full   vs Baseline: 年化 {delta_full:+.2f}pp, MaxDD {dd_full:+.2f}pp")
    print(f"  Full   vs Simple:   年化 {delta_full_vs_simple:+.2f}pp, MaxDD {dd_full_vs_simple:+.2f}pp")

    print("\n📊 结论:")
    if abs(delta_full_vs_simple) < 1 and abs(dd_full_vs_simple) < 2:
        print("  → Simple ≈ Full (差异 < 1pp 年化, < 2pp MaxDD)")
        print("  → 建议 simplify: 5 个 overlay 就够 (删掉 chain/asia/corr 精细化)")
        print("  → 系统更容易理解, 2 周后 review 无压力")
    elif delta_full_vs_simple > 1:
        print(f"  → Full 显著优于 Simple ({delta_full_vs_simple:+.2f}pp/年)")
        print("  → 保留全部 8 层 overlay + notifications + 备忘录 (A+B 方案)")
        print("  → 复杂度值得, 但需要 push 通知补记忆")
    elif dd_full_vs_simple < -2:
        print(f"  → Full 主要在 MaxDD 上更好 ({dd_full_vs_simple:+.2f}pp)")
        print("  → 保留复杂度用于回撤保护 + 加 notification")
    else:
        print("  → 结果模糊, 需要更多历史或不同窗口验证")


if __name__ == "__main__":
    main()
