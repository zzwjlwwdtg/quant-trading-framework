"""_backtest_take_profit.py — 对实盘 BUY 重播不同 TP (take-profit) 分档策略

背景 (2026-09-04 分析): TSLA 触发 tp15 (+15.8%) 卖 30% 后, 24h 内又涨 3.3%,
    次日 +7.34%. 用户直觉认为 tp15 太紧, "反指". 按 memory rule
    (feedback_backtest_before_intuition) 用同一批 winning trade 重播 4-5 种 TP
    档位, 数据说话.

方法:
    对 trade_log.jsonl 里所有非-REBALANCE BUY, 从入场后 60 天 daily bar 里
    重播 N 种 TP 策略 (与 trailing stop 8%×√lev 竞争谁先触发), 比 realized pnl.

**Caveat**:
    · 样本 n≈24 单, 统计意义弱, 结论 exploratory
    · 覆盖近 2 个月特殊 regime (semi 崩盘 + 债券 rally + TSLA rally)
    · 不做 train/test split (样本不够), 全样本
    · 用同一批 trades 也用过 stop_distance backtest → 视作同一 trade pool 的
      "TP 敏感性分析", 换的是 exit 逻辑不是新假设 (memory 允许 same-hypothesis
      sensitivity study)

**Pass condition (跑前不改)**:
    C1: 新最佳策略 avg pnl 比 current tp15_30_50 高 ≥ 2 pp
    C2: 新最佳 median pnl 也不劣于 current
    通过 → 建议改, 否则保持现制度

CLI: python _backtest_take_profit.py
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

TRADE_LOG = Path("signals/trade_log.jsonl")
HOLD_DAYS = 60
LEVERAGE = {"US.SOXL": 3, "US.TQQQ": 3, "US.MULL": 2, "US.SQQQ": 3, "US.SOXS": 3}
TRAILING_STOP_BASE = 0.08   # 保持跟 paper_trader.py:422 一致

EXCLUDE_TAG_KEYWORDS = ["REBALANCE"]

# 策略列表: 每档 [(pct_target, sell_fraction_of_REMAINING, label), ...]
TP_POLICIES = [
    ("current_15_30_50", [(0.15, 0.30), (0.30, 0.30), (0.50, 0.40)]),   # ← 现制度
    ("tight_10_20_35",   [(0.10, 0.30), (0.20, 0.30), (0.35, 0.40)]),
    ("wider_20_35_55",   [(0.20, 0.30), (0.35, 0.30), (0.55, 0.40)]),
    ("far_25_45_75",     [(0.25, 0.30), (0.45, 0.30), (0.75, 0.40)]),
    ("single_tp30",      [(0.30, 1.00)]),                                # 单档 30% 全卖
    ("single_tp50",      [(0.50, 1.00)]),
    ("no_tp",            []),                                            # 仅 trailing stop
]

PASS_PP_MIN = 2.0


def _leverage_sqrt(ticker: str) -> float:
    return math.sqrt(LEVERAGE.get(ticker, 1))


def _load_buys():
    if not TRADE_LOG.exists():
        return []
    out = []
    for line in TRADE_LOG.read_text(encoding="utf-8").splitlines():
        try:
            t = json.loads(line)
        except Exception:
            continue
        if t.get("side") != "BUY":
            continue
        tag = t.get("tag") or ""
        if any(k in tag.upper() for k in EXCLUDE_TAG_KEYWORDS):
            continue
        out.append({"ticker": t["ticker"], "ts": t["ts"],
                    "price": float(t["price"]), "tag": tag})
    return out


def _pull_bars(ticker, buy_ts):
    import yfinance as yf
    d0 = datetime.fromisoformat(buy_ts.replace("Z", "+00:00")).date()
    end = date.fromordinal(d0.toordinal() + HOLD_DAYS + 10)
    yf_sym = ticker.replace("US.", "")
    try:
        df = yf.Ticker(yf_sym).history(start=d0.isoformat(), end=end.isoformat(),
                                        interval="1d", auto_adjust=True)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    return df


def _simulate_policy(bars, buy_price, ticker, tp_levels: list) -> dict:
    """给一个 BUY, 模拟 policy 下的 realized pnl%.
    trailing stop 保持 8%×√lev, 与 TP 并行监控, 谁先触发按谁走.
    分档 TP: 每档触发时卖 fraction × 当前剩余仓位, 剩余 continues.

    tp_levels: [(pct_gain_target, sell_frac_of_remaining), ...] 顺序执行

    返回: {realized_pnl_pct, exit_days, tp_hits, stop_hit, remaining_at_end}
    """
    lev_scale = _leverage_sqrt(ticker)
    trail_pct = TRAILING_STOP_BASE * lev_scale
    tp_scaled = [(gain * lev_scale, frac) for gain, frac in tp_levels]

    remaining = 1.0   # 归一化, 起始持仓 100%
    weighted_exit_pct = 0.0   # sum(sell_frac × exit_price / buy_price)
    tp_hits = []
    stop_hit = False
    exit_day = None
    rolling_high = buy_price

    max_days = min(HOLD_DAYS, len(bars))
    tp_idx = 0   # 下一个待触发 TP

    for i in range(max_days):
        row = bars.iloc[i]
        try:
            day_high = float(row["High"])
            day_low  = float(row["Low"])
            day_close = float(row["Close"])
        except (TypeError, ValueError):
            continue
        # skip NaN bars (holiday / data gap)
        if day_high != day_high or day_low != day_low or day_close != day_close:
            continue

        # 更新 rolling high
        if day_high > rolling_high:
            rolling_high = day_high

        # Trailing stop
        trail_stop = rolling_high * (1 - trail_pct)
        if day_low <= trail_stop and remaining > 0:
            # 全平剩余
            exit_price = trail_stop
            weighted_exit_pct += remaining * (exit_price / buy_price - 1) * 100
            remaining = 0
            stop_hit = True
            exit_day = i
            break

        # TP levels (顺序检查, 一天内可能触发多个)
        while tp_idx < len(tp_scaled) and remaining > 0:
            tp_gain, tp_frac = tp_scaled[tp_idx]
            tp_price = buy_price * (1 + tp_gain)
            if day_high >= tp_price:
                # 触发, 卖 fraction × remaining
                qty_sold = remaining * tp_frac
                weighted_exit_pct += qty_sold * (tp_price / buy_price - 1) * 100
                remaining -= qty_sold
                tp_hits.append(tp_gain)
                tp_idx += 1
                # 继续检查下一档 (可能同一天多个)
            else:
                break

    # window 结束仍有剩余 → 用最后有效 close 平
    if remaining > 0:
        last_close = None
        for j in range(max_days - 1, -1, -1):
            c = float(bars.iloc[j]["Close"])
            if c == c and c > 0:
                last_close = c
                break
        if last_close is None:
            return {"realized_pnl_pct": None, "exit_day": None,
                    "tp_hits": [], "stop_hit": False, "n_tp_triggered": 0}
        weighted_exit_pct += remaining * (last_close / buy_price - 1) * 100
        exit_day = exit_day if exit_day is not None else max_days - 1

    return {
        "realized_pnl_pct": round(weighted_exit_pct, 3),
        "exit_day": exit_day,
        "tp_hits": tp_hits,
        "stop_hit": stop_hit,
        "n_tp_triggered": len(tp_hits),
    }


def run():
    print("=" * 100)
    print("Take-Profit 分档敏感性回测 (对实盘 BUY 重播)")
    print("=" * 100)

    buys = _load_buys()
    print(f"\n共 {len(buys)} 单非-REBALANCE BUY")

    # 拉 bars
    print("拉 60-day fwd bars...")
    valid = []
    for b in buys:
        bars = _pull_bars(b["ticker"], b["ts"])
        if bars is None or bars.empty:
            print(f"  {b['ticker']} {b['ts'][:10]}: 无 fwd 数据, 跳过")
            continue
        b["bars"] = bars
        valid.append(b)
    print(f"  有效: {len(valid)} 单")

    if len(valid) < 5:
        print("样本太少, 结论无意义")
        return

    # 模拟每个策略
    results = {name: [] for name, _ in TP_POLICIES}
    for b in valid:
        for name, tp_levels in TP_POLICIES:
            r = _simulate_policy(b["bars"], b["price"], b["ticker"], tp_levels)
            results[name].append({**r, "ticker": b["ticker"]})

    # 汇总
    print()
    print("=" * 100)
    print("【策略对比】")
    print("=" * 100)
    print(f"{'策略':<22} {'n':>3} {'avg_pnl':>10} {'median':>10} {'win%':>7} "
          f"{'stop_hit%':>10} {'max_loss':>10} {'max_win':>10} {'avg_tp_hit':>10}")
    print("-" * 110)

    baseline_avg = None
    for name, _ in TP_POLICIES:
        rs_all = results[name]
        # filter out None realized_pnl_pct
        rs = [r for r in rs_all if r["realized_pnl_pct"] is not None]
        n = len(rs)
        if n == 0: continue
        avg = sum(r["realized_pnl_pct"] for r in rs) / n
        med = sorted(r["realized_pnl_pct"] for r in rs)[n//2]
        wins = sum(1 for r in rs if r["realized_pnl_pct"] > 0) / n * 100
        stops = sum(1 for r in rs if r["stop_hit"]) / n * 100
        max_l = min(r["realized_pnl_pct"] for r in rs)
        max_w = max(r["realized_pnl_pct"] for r in rs)
        avg_tp = sum(r["n_tp_triggered"] for r in rs) / n
        marker = "  ← 现制度" if name == "current_15_30_50" else ""
        print(f"{name:<22} {n:>3} {avg:>+9.2f}% {med:>+9.2f}% {wins:>6.1f}% "
              f"{stops:>9.1f}% {max_l:>+9.2f}% {max_w:>+9.2f}% {avg_tp:>9.2f}{marker}")
        if name == "current_15_30_50":
            baseline_avg = avg

    print()
    print("=" * 100)
    print("【VERDICT】")
    print("=" * 100)
    if baseline_avg is None:
        print("  ! 无 baseline current_15_30_50 数据")
        return
    best_name, best_avg = None, baseline_avg
    for name, _ in TP_POLICIES:
        if name == "current_15_30_50": continue
        rs = [r for r in results[name] if r["realized_pnl_pct"] is not None]
        if not rs: continue
        avg = sum(r["realized_pnl_pct"] for r in rs) / len(rs)
        if avg > best_avg:
            best_avg = avg
            best_name = name

    if best_name is None or (best_avg - baseline_avg) < PASS_PP_MIN:
        gap = (best_avg - baseline_avg) if best_name else 0
        best_str = best_name or "无更优策略"
        print(f"  → **保持 current_15_30_50** ({baseline_avg:+.2f}%). 最佳候选 {best_str} 只高 {gap:+.2f}pp < {PASS_PP_MIN}pp 阈值")
        verdict_val = "keep_baseline"
        should_int = False
        rec = "keep TP_BASE_LEVELS unchanged"
    else:
        print(f"  → **建议改用 {best_name}** ({best_avg:+.2f}% vs baseline {baseline_avg:+.2f}%, +{best_avg-baseline_avg:.2f}pp)")
        print(f"     实施: paper_trader.py:415 TP_BASE_LEVELS 改为对应档位")
        verdict_val = "pass"
        should_int = True
        rec = f"switch TP_BASE_LEVELS to match {best_name}"

    # verdict system
    try:
        from backtest_verdicts import write_verdict
        write_verdict(
            "take_profit_distance",
            verdict_val,
            conclusion=f"current baseline={baseline_avg:+.2f}% best_alt={best_name} gap={best_avg-baseline_avg:+.2f}pp",
            metrics={"baseline_avg": round(baseline_avg, 4),
                     "best_alt": best_name,
                     "best_alt_avg": round(best_avg, 4),
                     "delta_pp": round(best_avg - baseline_avg, 4),
                     "n_trades": len(valid),
                     "hold_days": HOLD_DAYS},
            params={"universe": "trade_log_signal_buys",
                    "trailing_stop_base": TRAILING_STOP_BASE,
                    "pass_pp_min": PASS_PP_MIN,
                    "n_policies_tested": len(TP_POLICIES)},
            next_review_days=60,
            should_integrate=should_int,
            recommendation=rec,
        )
        print("  [verdict] 写入 signals/backtest_verdicts/take_profit_distance.json")
    except Exception as _e:
        print(f"  [verdict] 写入失败: {_e}")

    # 逐单看每策略下 TSLA 具体表现 (用户问的那笔)
    print()
    print("=" * 100)
    print("【TSLA 那笔在各策略下】")
    print("=" * 100)
    tsla_idx = next((i for i, b in enumerate(valid) if b["ticker"] == "US.TSLA"), None)
    if tsla_idx is not None:
        for name, _ in TP_POLICIES:
            r = results[name][tsla_idx]
            print(f"  {name:<22} pnl={r['realized_pnl_pct']:>+7.2f}% "
                  f"exit_day={r['exit_day']} tp_hits={r['tp_hits']} stop={r['stop_hit']}")
    else:
        print("  TSLA 未在 valid pool")


if __name__ == "__main__":
    import os, threading
    threading.Timer(600, lambda: (print("\n[watchdog] 超时"), os._exit(2))).start()
    run()
