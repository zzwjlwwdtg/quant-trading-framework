"""
_backtest_bvc_flow.py — BVC (Bulk Volume Classification) 主力方向信号回测

对比 3 种 aggressor 方向估计:
  1) tick_rule    — 现状 capital_flow.py 用的: Close>Open→buy, Close<Open→sell
  2) bvc          — Easley-Lopez-O'Hara 2012: buy_frac = Φ(ret / σ_rolling)
  3) moomoo_smart — super+big 净流入占总成交额 (仅 US, 仅有当前值不能做历史回测)
                     → 本脚本回测 tick_rule vs bvc, moomoo 仅在末尾附一句"当前值供参考"

数据源:
  yfinance 5min bar, 60d, 每只 ticker 独立跑.
  剔除每日最后一根 bar (15:55-16:00 ET, 避 auction imbalance).

σ 估计:
  rolling 20-day 5min return std (~1540 samples, 适应波动率切换; 全局 σ 太粗).

事件定义:
  (ticker, date) → net_aggressor_pct = (buy_vol - sell_vol) / total_vol (该日全部 5min bar).
  两种方法各算一个 net_aggressor_pct.

分桶 (阈值扫描):
  net_aggressor_pct > +T → "净外盘信号 = 看多"
  net_aggressor_pct < -T → "净内盘信号 = 看空"
  T ∈ [10, 15, 20, 25, 30]%
  报每桶 5d/10d 前向 close-to-close return.

regime split:
  用 SPY 当日 dist_ma20 + SOX 20d 分 3 桶:
    bull_trending:   SPY dist_ma20 > +5% OR (SPY dist_ma20 > 0 AND SOX 20d > +5%)
    risk_off:        SPY dist_ma20 < -5% OR SOX 20d < -5%
    neutral:         其它

OOS 保护:
  前 40 交易日 = train (选最佳阈值)
  后 20 交易日 = test (报 OOS 数字)
  memory rule: 自动信号必须 OOS 验证.

通过条件 (打印在末尾, verdict 自动判):
  · BVC 最佳阈值 OOS 5d win_rate  ≥ 55%
  · BVC vs tick_rule 差值           ≥ +5pp
  · 阈值扫描单调性 corr             ≥ 0.5
  · bull_trending regime win_rate   ≥ 60%
任一不达 → 建议保持 dashboard-only 不接入 confluence.

CLI: python _backtest_bvc_flow.py
"""
from __future__ import annotations

import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

sys.stdout.reconfigure(line_buffering=True)

TICKERS = ["SOXL", "TQQQ", "NVDA", "MSFT", "AAPL", "TSLA"]
THRESHOLDS_PCT = [10, 15, 20, 25, 30]
HORIZONS = [5, 10]
SIGMA_WIN_DAYS = 20
OOS_TEST_DAYS = 20


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _pull_5min(ticker: str) -> Optional["pd.DataFrame"]:
    import yfinance as yf
    try:
        df = yf.Ticker(ticker).history(period="60d", interval="5m", auto_adjust=False)
    except Exception as ex:
        print(f"  [{ticker}] 5min pull failed: {ex}")
        return None
    if df is None or df.empty:
        return None
    df = df.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    try:
        df.index = df.index.tz_convert("America/New_York")
    except Exception:
        pass
    df["date"] = df.index.strftime("%Y-%m-%d")
    df["time"] = df.index.strftime("%H:%M")
    # 剔除每日最后一根 (15:55) auction 影响
    df = df[df["time"] < "15:55"].copy()
    df["ret"] = df["Close"].pct_change().fillna(0)
    return df


def _pull_daily(ticker: str) -> Optional["pd.DataFrame"]:
    import yfinance as yf
    try:
        df = yf.Ticker(ticker).history(period="120d", interval="1d", auto_adjust=False)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df.copy()
    df.index = [ts.date() for ts in df.index]
    return df


def _compute_bvc(df: "pd.DataFrame") -> "pd.DataFrame":
    """给 5min df 增加 tick_rule_bias 和 bvc_buy_frac 列, 输出 per-day aggregate."""
    import pandas as pd
    df = df.copy()
    # σ: rolling 20-day 5min return std (~1540 obs). 用绝对交易日 window ≈ 78 bars × 20.
    win = 78 * SIGMA_WIN_DAYS
    sigma_series = df["ret"].rolling(win, min_periods=78 * 5).std()
    df["sigma"] = sigma_series
    valid = df["sigma"].notna() & (df["sigma"] > 0)

    # BVC
    df["bvc_buy_frac"] = 0.5
    df.loc[valid, "bvc_buy_frac"] = df.loc[valid].apply(
        lambda r: _norm_cdf(r["ret"] / r["sigma"]), axis=1
    )
    df["bvc_buy_vol"]  = df["Volume"] * df["bvc_buy_frac"]
    df["bvc_sell_vol"] = df["Volume"] - df["bvc_buy_vol"]

    # tick rule (baseline): Close>Open→buy, <Open→sell, ==→split 50/50
    def _tick_frac(row):
        o, c = row["Open"], row["Close"]
        if o <= 0:
            return 0.5
        if c > o:
            return 1.0
        if c < o:
            return 0.0
        return 0.5
    df["tick_buy_frac"] = df.apply(_tick_frac, axis=1)
    df["tick_buy_vol"]  = df["Volume"] * df["tick_buy_frac"]
    df["tick_sell_vol"] = df["Volume"] - df["tick_buy_vol"]

    # per-day agg
    grp = df.groupby("date")
    total_vol   = grp["Volume"].sum()
    bvc_buy     = grp["bvc_buy_vol"].sum()
    bvc_sell    = grp["bvc_sell_vol"].sum()
    tick_buy    = grp["tick_buy_vol"].sum()
    tick_sell   = grp["tick_sell_vol"].sum()
    valid_bars  = grp["sigma"].apply(lambda s: (s.notna() & (s > 0)).sum())

    daily = pd.DataFrame({
        "total_vol":  total_vol,
        "bvc_net":    bvc_buy - bvc_sell,
        "tick_net":   tick_buy - tick_sell,
        "valid_bars": valid_bars,
    })
    daily["bvc_pct"]  = daily["bvc_net"]  / daily["total_vol"].replace(0, float("nan")) * 100
    daily["tick_pct"] = daily["tick_net"] / daily["total_vol"].replace(0, float("nan")) * 100
    daily.index = [date.fromisoformat(d) for d in daily.index]
    daily = daily.sort_index()
    return daily


def _fwd_return(daily: "pd.DataFrame", d: date, h: int) -> Optional[float]:
    """d 日 close → d+h 交易日 close 的 pct."""
    if d not in daily.index:
        return None
    idx = list(daily.index)
    i = idx.index(d)
    if i + h >= len(idx):
        return None
    p0 = float(daily.loc[d, "Close"])
    p1 = float(daily.loc[idx[i + h], "Close"])
    if p0 <= 0 or math.isnan(p0) or math.isnan(p1):
        return None
    return (p1 / p0 - 1) * 100


def _classify_regime(spy_daily: "pd.DataFrame", sox_daily: "pd.DataFrame", d: date) -> str:
    """粗糙 regime 分类, 用 SPY dist_ma20 + SOX 20d 动量."""
    if d not in spy_daily.index:
        return "unknown"
    idx = list(spy_daily.index)
    i = idx.index(d)
    if i < 20:
        return "unknown"
    spy_close = float(spy_daily.loc[d, "Close"])
    spy_ma20  = float(spy_daily["Close"].iloc[i-19:i+1].mean())
    spy_dist  = (spy_close / spy_ma20 - 1) * 100
    sox_20d = 0.0
    if d in sox_daily.index:
        j = list(sox_daily.index).index(d)
        if j >= 20:
            sox_close = float(sox_daily.loc[d, "Close"])
            sox_ago   = float(sox_daily["Close"].iloc[j-20])
            if sox_ago > 0:
                sox_20d = (sox_close / sox_ago - 1) * 100
    if spy_dist > 5 or (spy_dist > 0 and sox_20d > 5):
        return "bull_trending"
    if spy_dist < -5 or sox_20d < -5:
        return "risk_off"
    return "neutral"


def _bucket_stats(events: list[dict], key: str, threshold: float, horizon: int) -> dict:
    """给定阈值, 分 3 桶 (long_signal / short_signal / neutral) 报统计."""
    ret_key = f"fwd_{horizon}d"
    def _valid(e):
        v = e.get(ret_key)
        k = e.get(key)
        if v is None or k is None:
            return False
        try:
            return not (math.isnan(v) or math.isnan(k))
        except TypeError:
            return False
    long_ret  = [e[ret_key] for e in events if _valid(e) and e[key] >  threshold]
    short_ret = [e[ret_key] for e in events if _valid(e) and e[key] < -threshold]
    def _stat(xs):
        if not xs:
            return {"n": 0}
        return {
            "n":   len(xs),
            "avg": sum(xs) / len(xs),
            "win": sum(1 for r in xs if r > 0) / len(xs) * 100,
            "med": sorted(xs)[len(xs) // 2],
        }
    return {"long": _stat(long_ret), "short": _stat(short_ret)}


def run():
    import pandas as pd  # noqa: F401 (ensure available)

    print("=" * 100)
    print("BVC 主力方向信号回测 (60d 5min bar × 6 US tickers)")
    print("=" * 100)

    # 拉 SPY / SOX 用于 regime 分类
    print("[setup] 拉 SPY/SOXX 用于 regime 分类...")
    spy_daily = _pull_daily("SPY")
    sox_daily = _pull_daily("SOXX")
    if spy_daily is None or sox_daily is None:
        print("  ! SPY/SOXX 拉取失败, 无法做 regime split, 全部当 neutral")
        spy_daily = None

    all_events: list[dict] = []
    for tk in TICKERS:
        print(f"\n[{tk}] 拉 5min + daily ...")
        five = _pull_5min(tk)
        daily = _pull_daily(tk)
        if five is None or daily is None or five.empty or daily.empty:
            print(f"  ! {tk} 数据不足, 跳过")
            continue

        # 加 Close 到 daily 索引对齐
        agg = _compute_bvc(five)
        # 合并 daily close 供 fwd return 用
        merge = agg.join(pd.DataFrame({"Close": daily["Close"]}), how="left")

        for d, row in merge.iterrows():
            if pd.isna(row.get("Close")):
                continue
            if row["valid_bars"] < 40:  # 至少 40 根 valid bar 才算完整一天
                continue
            fwd = {}
            for h in HORIZONS:
                v = _fwd_return(daily, d, h)
                fwd[f"fwd_{h}d"] = v
            if fwd[f"fwd_{HORIZONS[0]}d"] is None:
                continue
            rg = _classify_regime(spy_daily, sox_daily, d) if spy_daily is not None else "unknown"
            all_events.append({
                "ticker":   tk,
                "date":     d,
                "bvc_pct":  float(row["bvc_pct"]),
                "tick_pct": float(row["tick_pct"]),
                "regime":   rg,
                **fwd,
            })

    if not all_events:
        print("\n! 没有任何有效事件, 检查 yfinance 是否可用")
        return

    all_events.sort(key=lambda e: e["date"])
    print(f"\n[dataset] 有效事件数: {len(all_events)}")
    dates_uniq = sorted({e["date"] for e in all_events})
    print(f"          日期范围: {dates_uniq[0]} → {dates_uniq[-1]} ({len(dates_uniq)} 交易日)")
    n_by_rg = defaultdict(int)
    for e in all_events:
        n_by_rg[e["regime"]] += 1
    print(f"          regime 分布: {dict(n_by_rg)}")

    # ── 阈值扫描 (全集) ────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("【阈值扫描】 net_aggressor_pct > +T (long) / < -T (short) 后的前向收益")
    print("=" * 100)
    print(f"{'method':<10} {'T':>4} {'H':>3} {'long_n':>7} {'long_avg':>9} {'long_win':>9} "
          f"{'short_n':>8} {'short_avg':>10} {'short_win':>10}")
    print("-" * 100)
    for method_key, method_lbl in [("bvc_pct", "bvc"), ("tick_pct", "tick_rule")]:
        for T in THRESHOLDS_PCT:
            for H in HORIZONS:
                s = _bucket_stats(all_events, method_key, T, H)
                lo, sh = s["long"], s["short"]
                lo_str = (f"{lo['n']:>7} {lo['avg']:>+8.2f}% {lo['win']:>8.1f}%"
                          if lo["n"] else f"{'-':>7} {'-':>9} {'-':>9}")
                sh_str = (f"{sh['n']:>8} {sh['avg']:>+9.2f}% {sh['win']:>9.1f}%"
                          if sh["n"] else f"{'-':>8} {'-':>10} {'-':>10}")
                print(f"{method_lbl:<10} {T:>4}% {H:>2}d {lo_str} {sh_str}")

    # ── 单调性检查 ──────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("【单调性】 阈值 T vs long_win_rate (5d) — corr ≥ 0.5 才算稳定")
    print("=" * 100)
    for method_key, method_lbl in [("bvc_pct", "bvc"), ("tick_pct", "tick_rule")]:
        ts, wins = [], []
        for T in THRESHOLDS_PCT:
            s = _bucket_stats(all_events, method_key, T, 5)
            if s["long"]["n"] >= 3:
                ts.append(T)
                wins.append(s["long"]["win"])
        if len(ts) >= 3:
            mean_t = sum(ts) / len(ts)
            mean_w = sum(wins) / len(wins)
            num = sum((t - mean_t) * (w - mean_w) for t, w in zip(ts, wins))
            den = math.sqrt(sum((t - mean_t) ** 2 for t in ts) * sum((w - mean_w) ** 2 for w in wins))
            corr = num / den if den > 0 else 0.0
            print(f"  {method_lbl:<10} corr(T, long_win_5d) = {corr:+.3f}   {'✓' if corr >= 0.5 else '⚠ 不单调'}")
        else:
            print(f"  {method_lbl:<10} 样本太少无法算 corr")

    # ── regime split ─────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("【regime split】 T=20% × 5d 前向, 分 regime 桶")
    print("=" * 100)
    print(f"{'method':<10} {'regime':<14} {'long_n':>7} {'long_avg':>9} {'long_win':>9} "
          f"{'short_n':>8} {'short_avg':>10} {'short_win':>10}")
    print("-" * 100)
    for method_key, method_lbl in [("bvc_pct", "bvc"), ("tick_pct", "tick_rule")]:
        for rg in ["bull_trending", "neutral", "risk_off", "unknown"]:
            sub = [e for e in all_events if e["regime"] == rg]
            if not sub:
                continue
            s = _bucket_stats(sub, method_key, 20, 5)
            lo, sh = s["long"], s["short"]
            lo_str = (f"{lo['n']:>7} {lo['avg']:>+8.2f}% {lo['win']:>8.1f}%"
                      if lo["n"] else f"{'-':>7} {'-':>9} {'-':>9}")
            sh_str = (f"{sh['n']:>8} {sh['avg']:>+9.2f}% {sh['win']:>9.1f}%"
                      if sh["n"] else f"{'-':>8} {'-':>10} {'-':>10}")
            print(f"{method_lbl:<10} {rg:<14} {lo_str} {sh_str}")

    # ── OOS split ────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print(f"【OOS】 前 {len(dates_uniq) - OOS_TEST_DAYS} 交易日 train → 挑最佳 T; 后 {OOS_TEST_DAYS} 天 test 报 OOS")
    print("=" * 100)
    cutoff = dates_uniq[-OOS_TEST_DAYS] if len(dates_uniq) > OOS_TEST_DAYS else dates_uniq[len(dates_uniq)//2]
    train = [e for e in all_events if e["date"] < cutoff]
    test  = [e for e in all_events if e["date"] >= cutoff]
    print(f"  train: n={len(train)} ({dates_uniq[0]} → {cutoff - timedelta(days=1)})")
    print(f"  test:  n={len(test)}  ({cutoff} → {dates_uniq[-1]})")

    verdict_data = {}
    for method_key, method_lbl in [("bvc_pct", "bvc"), ("tick_pct", "tick_rule")]:
        best_T, best_win = None, -1
        for T in THRESHOLDS_PCT:
            s = _bucket_stats(train, method_key, T, 5)
            n_total = s["long"]["n"] + s["short"]["n"]
            if n_total < 5:
                continue
            # combined win: long_win + (100 - short_win) if we go long/short accordingly
            long_win = s["long"]["win"] if s["long"]["n"] else 0
            short_win = (100 - s["short"]["win"]) if s["short"]["n"] else 0  # short 胜率 = 前向 < 0 的比例
            if s["long"]["n"] and s["short"]["n"]:
                combined = (long_win * s["long"]["n"] + short_win * s["short"]["n"]) / n_total
            elif s["long"]["n"]:
                combined = long_win
            else:
                combined = short_win
            if combined > best_win:
                best_win = combined
                best_T = T
        if best_T is None:
            print(f"  {method_lbl:<10} train 样本不足, 跳过 OOS")
            verdict_data[method_lbl] = None
            continue
        # OOS test
        s_oos = _bucket_stats(test, method_key, best_T, 5)
        lo, sh = s_oos["long"], s_oos["short"]
        n_oos = lo["n"] + sh["n"]
        if n_oos == 0:
            print(f"  {method_lbl:<10} best_T={best_T}% (train), OOS 无信号触发")
            verdict_data[method_lbl] = {"best_T": best_T, "oos_win": None, "oos_n": 0}
            continue
        long_oos_win = lo["win"] if lo["n"] else 0
        short_oos_win = (100 - sh["win"]) if sh["n"] else 0
        if lo["n"] and sh["n"]:
            oos_win = (long_oos_win * lo["n"] + short_oos_win * sh["n"]) / n_oos
        elif lo["n"]:
            oos_win = long_oos_win
        else:
            oos_win = short_oos_win
        lo_str = f"long n={lo['n']} avg{lo['avg']:+.2f}% win{lo['win']:.1f}%" if lo["n"] else "long -"
        sh_str = f"short n={sh['n']} avg{sh['avg']:+.2f}% win{sh['win']:.1f}%" if sh["n"] else "short -"
        print(f"  {method_lbl:<10} best_T={best_T}% (train combined_win={best_win:.1f}%)")
        print(f"             OOS: {lo_str}, {sh_str}, combined_win={oos_win:.1f}%")
        verdict_data[method_lbl] = {"best_T": best_T, "oos_win": oos_win, "oos_n": n_oos,
                                     "long": lo, "short": sh}

    # ── verdict ──────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("【VERDICT】 通过条件评估")
    print("=" * 100)
    bvc = verdict_data.get("bvc")
    tick = verdict_data.get("tick_rule")

    checks = []
    if bvc and bvc.get("oos_win") is not None:
        c1 = bvc["oos_win"] >= 55
        checks.append(("BVC OOS combined_win ≥ 55%", f"{bvc['oos_win']:.1f}%", c1))
    else:
        checks.append(("BVC OOS combined_win ≥ 55%", "N/A (无触发)", False))

    if bvc and tick and bvc.get("oos_win") is not None and tick.get("oos_win") is not None:
        diff = bvc["oos_win"] - tick["oos_win"]
        c2 = diff >= 5
        checks.append(("BVC 比 tick_rule ≥ +5pp",
                       f"BVC {bvc['oos_win']:.1f}% vs tick {tick['oos_win']:.1f}% = {diff:+.1f}pp", c2))
    else:
        checks.append(("BVC vs tick_rule 差值", "N/A", False))

    # bull_trending regime win rate check
    bull_events = [e for e in all_events if e["regime"] == "bull_trending"]
    if bull_events and bvc:
        s_bull = _bucket_stats(bull_events, "bvc_pct", bvc["best_T"], 5)
        n_bt = s_bull["long"]["n"] + s_bull["short"]["n"]
        if n_bt >= 3:
            lw = s_bull["long"]["win"] if s_bull["long"]["n"] else 0
            sw = (100 - s_bull["short"]["win"]) if s_bull["short"]["n"] else 0
            if s_bull["long"]["n"] and s_bull["short"]["n"]:
                bt_win = (lw * s_bull["long"]["n"] + sw * s_bull["short"]["n"]) / n_bt
            elif s_bull["long"]["n"]:
                bt_win = lw
            else:
                bt_win = sw
            checks.append(("bull_trending regime combined_win ≥ 60%", f"{bt_win:.1f}% (n={n_bt})", bt_win >= 60))
        else:
            checks.append(("bull_trending regime combined_win ≥ 60%", f"样本不足 (n={n_bt})", False))
    else:
        checks.append(("bull_trending regime combined_win ≥ 60%", "N/A", False))

    for lbl, val, ok in checks:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {lbl}: {val}")

    all_pass = all(ok for _, _, ok in checks)
    print()
    if all_pass:
        print("  → 建议: 用 BVC 替换 capital_flow.py 的 tick rule, 并作为 confluence 一票 (弱权重).")
    else:
        n_pass = sum(1 for _, _, ok in checks if ok)
        if n_pass >= len(checks) // 2:
            print(f"  → 边缘: {n_pass}/{len(checks)} 通过. 仅在通过的 regime 桶用, 不进主 confluence.")
        else:
            print(f"  → 淘汰: {n_pass}/{len(checks)} 通过. 保持 dashboard-only, 不接入决策链.")

    # 附: moomoo 当前 smart_pct (仅参考, 无历史)
    print("\n" + "-" * 100)
    print("[附] moomoo super+big 当前值 (仅 US, 无历史, 供对比):")
    try:
        from moomoo_data import get_capital_distribution_via_openD
        for tk in TICKERS:
            raw = get_capital_distribution_via_openD(tk)
            if raw:
                total = raw["total_in"] + raw["total_out"]
                smart_net = raw["super_net"] + raw["big_net"]
                smart_pct = (smart_net / total * 100) if total else 0
                print(f"  {tk:<6} smart_net=${smart_net/1e6:+7.1f}M  smart_pct={smart_pct:+5.1f}%")
    except Exception as ex:
        print(f"  moomoo 不可用: {ex}")


if __name__ == "__main__":
    run()
