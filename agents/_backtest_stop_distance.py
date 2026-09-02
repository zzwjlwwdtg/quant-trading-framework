"""_backtest_stop_distance.py — 对实盘 BUY 重播不同 stop 距离, 看现制度是否最优

背景 (2026-09-02 分析): paper_trader 近 60d 12 个真信号 pair 里 10 hit stop,
    stop 桶 avg -8.74%, 净胜率 16.7%. 问题: 是"stop 太紧"造成过早触发,
    还是"signal 入场就 timing 差"?

方法:
    对 trade_log.jsonl 里所有非-REBALANCE BUY, 从入场后 30 天 daily bar 里
    重播 N 种 stop 策略, 比较 realized pnl 分布. 现制度是 TRAIL_8 × √lev.
    若某新策略 avg pnl ≥ TRAIL_8 + 2pp → 值得考虑改.

**Caveat**:
    · 样本 n≈15 单, 统计意义弱, 结论 exploratory
    · 只覆盖近 2 个月 semi 崩盘 + 债券 rally 特殊 regime
    · 不做 train/test split (样本不够), 全样本报告

**Pass condition (跑前不改)**:
    C1: 某策略 avg pnl 比 TRAIL_8 高 ≥ 2 pp (绝对)
    C2: 该策略 median pnl 也不劣于 TRAIL_8
    通过 → 建议改, 否则保持现制度

CLI: python _backtest_stop_distance.py
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

TRADE_LOG = Path("signals/trade_log.jsonl")
HOLD_DAYS = 30    # 拉入场后 30 天 daily bar
LEVERAGE = {"US.SOXL": 3, "US.TQQQ": 3, "US.MULL": 2, "US.SQQQ": 3, "US.SOXS": 3}

# 排除的 tag 前缀 (rebalance / pyramid 属于操作性交易, 不是信号入场)
EXCLUDE_TAG_KEYWORDS = ["REBALANCE"]

# 策略列表 (leverage-scaled)
STOP_POLICIES = [
    ("fixed_5",   "fixed",   0.05),
    ("fixed_8",   "fixed",   0.08),
    ("fixed_12",  "fixed",   0.12),
    ("trail_5",   "trail",   0.05),
    ("trail_8",   "trail",   0.08),   # ← 现制度 baseline
    ("trail_12",  "trail",   0.12),
    ("trail_15",  "trail",   0.15),
    ("no_stop_20d", "hold",  20),      # 不设 stop, 20 天后无脑卖
]

PASS_PP_MIN = 2.0   # 新策略 avg pnl 至少高 2pp 才 promote


def _leverage_sqrt(ticker: str) -> float:
    return math.sqrt(LEVERAGE.get(ticker, 1))


def _load_buys():
    """读 trade_log 抽出所有非-REBALANCE BUY, 返 [(ticker, ts, price, tag)]"""
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
        tag = (t.get("tag") or "")
        if any(k in tag.upper() for k in EXCLUDE_TAG_KEYWORDS):
            continue
        out.append({
            "ticker": t["ticker"],
            "ts":     t["ts"],
            "price":  float(t["price"]),
            "tag":    tag,
        })
    return out


def _pull_bars(ticker, buy_ts):
    """拉入场后 30 天的 daily bars (open/high/low/close)"""
    import yfinance as yf
    # buy_ts 是 UTC ISO. 转 date.
    d0 = datetime.fromisoformat(buy_ts.replace("Z", "+00:00")).date()
    # 拉到 +40 天 (buffer 遇 holidays)
    end = date.fromordinal(d0.toordinal() + 40)
    # 除掉 US. 前缀
    yf_sym = ticker.replace("US.", "")
    try:
        df = yf.Ticker(yf_sym).history(start=d0.isoformat(), end=end.isoformat(),
                                        interval="1d", auto_adjust=True)
    except Exception as ex:
        return None
    if df is None or df.empty:
        return None
    return df


def _simulate_stop(bars, buy_price, ticker, kind, param):
    """给定策略参数, 遍历 bars 求实际退出价.
    返回 (exit_price, exit_day_index, reason)."""
    lev_scale = _leverage_sqrt(ticker)

    if kind == "hold":
        # 简单 hold N 天然后收盘价平
        max_days = min(int(param), len(bars) - 1)
        if max_days < 0:
            max_days = 0
        exit_price = float(bars["Close"].iloc[max_days])
        return exit_price, max_days, "hold_end"

    stop_pct = float(param) * lev_scale
    rolling_high = buy_price
    max_days = min(HOLD_DAYS, len(bars))
    for i in range(max_days):
        row = bars.iloc[i]
        day_high = float(row["High"])
        day_low  = float(row["Low"])
        day_close = float(row["Close"])

        # 更新 rolling high (仅 trail 用)
        if kind == "trail" and day_high > rolling_high:
            rolling_high = day_high

        if kind == "fixed":
            stop_price = buy_price * (1 - stop_pct)
        else:  # trail
            stop_price = rolling_high * (1 - stop_pct)

        # 若当日 low ≤ stop_price → 触发, 用 stop_price 出场
        if day_low <= stop_price:
            return stop_price, i, f"stop@{stop_price:.2f}"

    # 到 window end 没触发 stop → 用最后收盘退
    last_i = max_days - 1
    return float(bars["Close"].iloc[last_i]), last_i, "hold_end"


def run():
    print("=" * 100)
    print("Stop 距离敏感性回测 (对实盘 BUY 单重播)")
    print("=" * 100)

    buys = _load_buys()
    print(f"\n共 {len(buys)} 单非-REBALANCE BUY:")
    for b in buys[-10:]:
        print(f"  {b['ts'][:10]} {b['ticker']:<10} @ ${b['price']:.2f}  tag={b['tag'][:40]}")

    if not buys:
        print("无 BUY 数据, 退出")
        return

    # 对每个 BUY 拉 30 天 bars
    print("\n拉 30-day fwd bars...")
    valid_buys = []
    for b in buys:
        bars = _pull_bars(b["ticker"], b["ts"])
        if bars is None or bars.empty:
            print(f"  {b['ticker']} {b['ts'][:10]}: 无 fwd 数据, 跳过")
            continue
        b["bars"] = bars
        valid_buys.append(b)
    print(f"  有效: {len(valid_buys)} 单")

    if len(valid_buys) < 5:
        print("样本太少 (< 5), 结论无意义")
        return

    # 对每种策略, 每个 BUY 模拟
    results = {name: [] for name, _, _ in STOP_POLICIES}
    for b in valid_buys:
        for name, kind, param in STOP_POLICIES:
            exit_px, exit_i, reason = _simulate_stop(b["bars"], b["price"],
                                                     b["ticker"], kind, param)
            pnl_pct = (exit_px / b["price"] - 1) * 100
            results[name].append({
                "ticker": b["ticker"], "pnl": pnl_pct,
                "exit_day": exit_i, "reason": reason,
            })

    # 汇总
    print()
    print("=" * 100)
    print("【策略对比】")
    print("=" * 100)
    print(f"{'策略':<15} {'n':>3} {'avg_pnl':>9} {'median':>9} {'win%':>7} "
          f"{'stop_hit%':>10} {'max_loss':>10} {'avg_days':>10}")
    print("-" * 100)

    baseline_avg = None
    for name, _, _ in STOP_POLICIES:
        rs = results[name]
        n = len(rs)
        if n == 0: continue
        avg = sum(r["pnl"] for r in rs) / n
        med = sorted(r["pnl"] for r in rs)[n//2]
        wins = sum(1 for r in rs if r["pnl"] > 0) / n * 100
        stop_hit = sum(1 for r in rs if "stop@" in r["reason"]) / n * 100
        max_loss = min(r["pnl"] for r in rs)
        avg_days = sum(r["exit_day"] for r in rs) / n
        marker = "  ← 现制度" if name == "trail_8" else ""
        print(f"{name:<15} {n:>3} {avg:>+8.2f}% {med:>+8.2f}% {wins:>6.1f}% "
              f"{stop_hit:>9.1f}% {max_loss:>+9.2f}% {avg_days:>9.1f}d{marker}")
        if name == "trail_8":
            baseline_avg = avg

    # 找最好非-baseline 策略
    print()
    print("=" * 100)
    print("【VERDICT】")
    print("=" * 100)
    if baseline_avg is None:
        print("  ! 无 baseline trail_8 数据")
        return
    best_name, best_avg = None, baseline_avg
    for name, _, _ in STOP_POLICIES:
        if name == "trail_8": continue
        rs = results[name]
        if not rs: continue
        avg = sum(r["pnl"] for r in rs) / len(rs)
        if avg > best_avg:
            best_avg = avg
            best_name = name

    if best_name is None or (best_avg - baseline_avg) < PASS_PP_MIN:
        best_str = f"{best_name}" if best_name else "无更优策略"
        gap = (best_avg - baseline_avg) if best_name else 0
        print(f"  → **保持 trail_8** ({baseline_avg:+.2f}%). 最佳候选 {best_str} 只高 {gap:+.2f}pp < {PASS_PP_MIN}pp 阈值")
        print(f"     结论: 当前 stop 距离在这批数据上不是最优, 但也没有明显更好的替代")
        print(f"     真正问题不在 stop, 在 signal 入场 timing")
        verdict_val = "keep_baseline"
        should_int = False
        recommendation = "keep TRAILING_STOP_BASE_PCT=0.08"
    else:
        print(f"  → **建议改用 {best_name}** ({best_avg:+.2f}% vs baseline {baseline_avg:+.2f}%, +{best_avg-baseline_avg:.2f}pp)")
        print(f"     实施: paper_trader.py:422 TRAILING_STOP_BASE_PCT 改为对应值")
        verdict_val = "pass"
        should_int = True
        recommendation = f"switch TRAILING_STOP_BASE_PCT to match {best_name}"

    # 写 verdict 供系统消费
    try:
        from backtest_verdicts import write_verdict
        write_verdict(
            "stop_distance",
            verdict_val,
            conclusion=f"trail_8 baseline={baseline_avg:+.2f}% best_alt={best_name} gap={best_avg-baseline_avg:+.2f}pp",
            metrics={"baseline_avg": round(baseline_avg, 4),
                     "best_alt": best_name,
                     "best_alt_avg": round(best_avg, 4),
                     "delta_pp": round(best_avg - baseline_avg, 4),
                     "n_trades": len(valid_buys)},
            params={"universe": "trade_log_signal_buys",
                    "hold_window_days": HOLD_DAYS,
                    "pass_pp_min": PASS_PP_MIN,
                    "n_policies_tested": len(STOP_POLICIES)},
            next_review_days=60,
            should_integrate=should_int,
            recommendation=recommendation,
        )
        print("  [verdict] 写入 signals/backtest_verdicts/stop_distance.json")
    except Exception as _e:
        print(f"  [verdict] 写入失败: {_e}")

    # 详细看每单在最坏 vs 最好策略下的表现差异
    print()
    print("=" * 100)
    print("【逐单最优 exit 与 trail_8 的差距】")
    print("=" * 100)
    print(f"{'ticker':<10} {'buy_ts':<12} {'trail_8':>10} {'best_strat':>12} {'best_pnl':>10}")
    for b in valid_buys:
        tk = b["ticker"]
        tr8 = next(r for r in results["trail_8"] if r["ticker"] == tk and "exit_day" in r and abs(r.get("exit_day",-1) - (results["trail_8"][valid_buys.index(b)]["exit_day"])) < 1)  # match
        # simplify: just index by position
        tr8 = results["trail_8"][valid_buys.index(b)]
        # find best strategy for THIS trade
        per_trade = {name: results[name][valid_buys.index(b)]["pnl"] for name, _, _ in STOP_POLICIES}
        best_for_trade = max(per_trade.items(), key=lambda x: x[1])
        print(f"{tk:<10} {b['ts'][:10]:<12} {tr8['pnl']:>+9.2f}% {best_for_trade[0]:>12} {best_for_trade[1]:>+9.2f}%")


if __name__ == "__main__":
    import os, threading
    threading.Timer(600, lambda: (print("\n[watchdog] 超时"), os._exit(2))).start()
    run()
