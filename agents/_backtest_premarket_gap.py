"""_backtest_premarket_gap.py — pre-market BUY gap 阈值敏感性回测

背景 (2026-09-07):
    paper_trader.py:84 `PREMARKET_BUY_MAX_POSITIVE_GAP_PCT = 0.02` 是现制度.
    pre-market 窗口 (08:30 ET) 若 BUY 时实时价比参考价高 >2% → SKIP.
    用户问"下调会怎么样". Memory rule "改动必须回测" → 用同一批 pre-market
    BUY 重播不同 gap 阈值, 数据说话.

方法:
    1. 读 trade_log 所有 window="pre-market" 的 BUY (排 REBALANCE)
    2. 每笔 BUY 从 yfinance 拉 daily bars 算 gap = open_today / close_prev - 1
       (这是 pre-market 结束时 gap 的近似, 无法拿真历史 pre-market tick 数据)
    3. 对每个阈值模拟:
       gap > threshold → SKIP (0 P&L, 0 fwd)
       gap ≤ threshold → ALLOW (at today's open), 计 5d/10d fwd close-to-close
    4. 阈值间比总 P&L, 找是否有比 2.0% 更优的档

**Caveats**:
    · gap 用 open/prev_close 是**日频 proxy**, 真 pre-market 触发时价 gap 可能不同
    · yfinance daily open 是 RTH 开盘, 与 pre-market 最后价可能有小差异
    · 样本 pre-market BUY 通常不多 (~10-20 单), 统计意义弱

**Pass condition (硬编码, 跑前不改)**:
    C1: 某新阈值总 P&L (5d) 比 baseline (2.0%) 高 ≥ 1.0pp
    C2: 该阈值样本数 ≥ 10 (避免过小 sample 假信号)
    通过 → 建议改; 否则保持 2.0%

CLI: python _backtest_premarket_gap.py
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

TRADE_LOG = Path("signals/trade_log.jsonl")
HOLD_DAYS = 15   # 只需算 fwd 5d/10d
FWD_HORIZONS = [1, 5, 10]

# 排除的 tag 前缀
EXCLUDE_TAG_KEYWORDS = ["REBALANCE"]

# 测试阈值 (2.0% 是 baseline)
GAP_THRESHOLDS_PCT = [0.75, 1.0, 1.5, 2.0, 2.5, 3.0]
BASELINE_PCT = 2.0
PASS_PP_MIN = 1.0
PASS_MIN_N = 10


def _load_premarket_buys():
    """读 trade_log 所有 window='pre-market' 的 BUY (排 REBALANCE)."""
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
        if t.get("window") != "pre-market":
            continue
        tag = (t.get("tag") or "")
        if any(k in tag.upper() for k in EXCLUDE_TAG_KEYWORDS):
            continue
        out.append({
            "ticker": t["ticker"],
            "ts":     t["ts"],
            "price":  float(t["price"]),  # 触发时 reference price
            "tag":    tag,
        })
    return out


def _pull_bars(ticker: str, buy_ts: str):
    """拉入场日前 3 天 + 后 15 天 daily bars (要 prev close)."""
    import yfinance as yf
    d0 = datetime.fromisoformat(buy_ts.replace("Z", "+00:00")).date()
    start = date.fromordinal(d0.toordinal() - 5)
    end = date.fromordinal(d0.toordinal() + HOLD_DAYS + 5)
    yf_sym = ticker.replace("US.", "")
    try:
        df = yf.Ticker(yf_sym).history(start=start.isoformat(), end=end.isoformat(),
                                        interval="1d", auto_adjust=True)
    except Exception:
        return None, None
    if df is None or df.empty:
        return None, None
    # 找 today (buy 日) 和 prev day
    dates = [d.date() for d in df.index]
    if d0 not in dates:
        # buy_ts 可能是 window="pre-market" 但技术上是"下一交易日的 pre-market"
        # 找 d0 之后最近的交易日
        future = [d for d in dates if d >= d0]
        if not future:
            return df, None
        today_idx = dates.index(future[0])
    else:
        today_idx = dates.index(d0)
    if today_idx == 0:
        return df, None
    return df, today_idx


def _simulate_gap(bars, today_idx: int, threshold_pct: float) -> dict:
    """给定 gap 阈值, 返 {skipped, gap_pct, fwd_1d_pct, fwd_5d_pct, fwd_10d_pct}"""
    prev_close = float(bars["Close"].iloc[today_idx - 1])
    today_open = float(bars["Open"].iloc[today_idx])
    if prev_close <= 0 or today_open <= 0:
        return {"skipped": None, "reason": "bad_price"}
    gap_pct = (today_open / prev_close - 1) * 100

    if gap_pct > threshold_pct:
        return {"skipped": True, "gap_pct": gap_pct, "fwd_1d": 0.0, "fwd_5d": 0.0, "fwd_10d": 0.0}

    # ALLOW: 入场价 = today_open, fwd close-to-close
    result = {"skipped": False, "gap_pct": gap_pct}
    for h in FWD_HORIZONS:
        j = today_idx + h
        if j >= len(bars):
            result[f"fwd_{h}d"] = None
            continue
        fwd_close = float(bars["Close"].iloc[j])
        if fwd_close <= 0 or fwd_close != fwd_close:
            result[f"fwd_{h}d"] = None
            continue
        result[f"fwd_{h}d"] = (fwd_close / today_open - 1) * 100
    return result


def run():
    print("=" * 100)
    print("Pre-market Gap 阈值敏感性回测")
    print("=" * 100)

    buys = _load_premarket_buys()
    print(f"\n共 {len(buys)} 单 pre-market BUY (排 REBALANCE)")
    if not buys:
        print("无 pre-market BUY. 可能所有 BUY 都在其他 window 触发.")
        return

    for b in buys[-5:]:
        print(f"  {b['ts'][:10]} {b['ticker']:<10} @ ${b['price']:.2f}  tag={b['tag'][:50]}")

    # 拉每笔 bars + 找 today idx
    print("\n拉 yfinance daily bars...")
    valid = []
    for b in buys:
        bars, tidx = _pull_bars(b["ticker"], b["ts"])
        if bars is None or tidx is None or tidx == 0:
            continue
        b["bars"] = bars
        b["today_idx"] = tidx
        valid.append(b)
    print(f"  有效: {len(valid)} 单")

    if len(valid) < 5:
        print(f"样本太少 ({len(valid)} < 5), 结论无意义")
        return

    # 模拟每个阈值
    results = {t: [] for t in GAP_THRESHOLDS_PCT}
    for b in valid:
        for th in GAP_THRESHOLDS_PCT:
            r = _simulate_gap(b["bars"], b["today_idx"], th)
            if r.get("skipped") is None:
                continue
            results[th].append({**r, "ticker": b["ticker"]})

    # 汇总
    print()
    print("=" * 110)
    print("【阈值对比】")
    print("=" * 110)
    print(f"{'阈值':<8} {'n':>3} {'allowed':>8} {'skipped':>8} "
          f"{'allow_avg5d':>12} {'skip_avg5d*':>14} "
          f"{'total_pnl_5d':>14} {'total_pnl_10d':>14}")
    print("-" * 110)
    print("  (* skip_avg5d = 若没被挡, 这批 BUY 5d 平均 P&L. 若 >0 说明挡错好机会)")
    print()

    baseline_total_5d = None
    for th in GAP_THRESHOLDS_PCT:
        rs = results[th]
        n = len(rs)
        if n == 0:
            continue
        allowed = [r for r in rs if not r["skipped"] and r.get("fwd_5d") is not None]
        skipped = [r for r in rs if r["skipped"]]
        # 被挡的 BUY 实际 5d 表现 (若下单会怎样)
        skip_actual = []
        for r in skipped:
            # 用同 ticker + 同 idx 重算 fwd 5d (不管 gap)
            r_forced = _simulate_gap(next(b["bars"] for b in valid if b["ticker"] == r["ticker"] and abs(_simulate_gap(b["bars"], b["today_idx"], 100.0).get("gap_pct", 0) - r["gap_pct"]) < 0.01),
                                       next(b["today_idx"] for b in valid if b["ticker"] == r["ticker"] and abs(_simulate_gap(b["bars"], b["today_idx"], 100.0).get("gap_pct", 0) - r["gap_pct"]) < 0.01),
                                       threshold_pct=100.0)   # 100% 阈值 = 不挡任何
            if r_forced.get("fwd_5d") is not None:
                skip_actual.append(r_forced["fwd_5d"])

        allow_avg_5d = sum(r["fwd_5d"] for r in allowed) / len(allowed) if allowed else 0
        skip_avg_5d = sum(skip_actual) / len(skip_actual) if skip_actual else 0
        # total P&L = 允许的 5d avg × 允许数 (skipped 视为 0 P&L)
        total_5d = sum(r["fwd_5d"] for r in allowed)
        total_10d = sum(r["fwd_10d"] for r in allowed if r.get("fwd_10d") is not None)

        marker = "  ← 现制度" if th == BASELINE_PCT else ""
        print(f"{th:<7.2f}% {n:>3} {len(allowed):>8} {len(skipped):>8} "
              f"{allow_avg_5d:>+11.2f}% {skip_avg_5d:>+13.2f}% "
              f"{total_5d:>+13.2f}% {total_10d:>+13.2f}%{marker}")

        if th == BASELINE_PCT:
            baseline_total_5d = total_5d

    # verdict
    print()
    print("=" * 110)
    print("【VERDICT】")
    print("=" * 110)
    if baseline_total_5d is None:
        print("  ! baseline 2.0% 无数据")
        return

    best_th, best_total = BASELINE_PCT, baseline_total_5d
    for th in GAP_THRESHOLDS_PCT:
        if th == BASELINE_PCT:
            continue
        rs = results[th]
        if len(rs) < PASS_MIN_N:
            continue
        allowed = [r for r in rs if not r["skipped"] and r.get("fwd_5d") is not None]
        total_5d = sum(r["fwd_5d"] for r in allowed)
        if total_5d > best_total:
            best_total = total_5d
            best_th = th

    if best_th == BASELINE_PCT or (best_total - baseline_total_5d) < PASS_PP_MIN:
        gap = best_total - baseline_total_5d
        print(f"  → **保持 2.0%** (total P&L 5d={baseline_total_5d:+.2f}%). "
              f"最佳候选 {best_th}% 只高 {gap:+.2f}pp < {PASS_PP_MIN}pp 阈值")
        verdict_val = "keep_baseline"
        should_int = False
        rec = "keep PREMARKET_BUY_MAX_POSITIVE_GAP_PCT = 0.02"
    else:
        print(f"  → **建议改用 {best_th}%** (total 5d={best_total:+.2f}% vs baseline {baseline_total_5d:+.2f}%, +{best_total-baseline_total_5d:.2f}pp)")
        print(f"     实施: paper_trader.py:84 PREMARKET_BUY_MAX_POSITIVE_GAP_PCT = {best_th/100:.4f}")
        verdict_val = "pass"
        should_int = True
        rec = f"switch PREMARKET_BUY_MAX_POSITIVE_GAP_PCT to {best_th/100:.4f}"

    # 写 verdict
    try:
        from backtest_verdicts import write_verdict
        write_verdict(
            "premarket_gap_threshold",
            verdict_val,
            conclusion=f"baseline 2.0% total 5d={baseline_total_5d:+.2f}%, best_alt={best_th}% total 5d={best_total:+.2f}%",
            metrics={"baseline_total_5d": round(baseline_total_5d, 4),
                     "best_threshold_pct": best_th,
                     "best_total_5d": round(best_total, 4),
                     "delta_pp": round(best_total - baseline_total_5d, 4),
                     "n_trades": len(valid)},
            params={"universe": "premarket_buys",
                    "thresholds_tested": GAP_THRESHOLDS_PCT,
                    "hold_days": HOLD_DAYS,
                    "pass_pp_min": PASS_PP_MIN},
            next_review_days=90,
            should_integrate=should_int,
            recommendation=rec,
        )
        print("  [verdict] 写入 signals/backtest_verdicts/premarket_gap_threshold.json")
    except Exception as _e:
        print(f"  [verdict] 写入失败: {_e}")


if __name__ == "__main__":
    import os, threading
    threading.Timer(600, lambda: (print("\n[watchdog] 超时"), os._exit(2))).start()
    run()
