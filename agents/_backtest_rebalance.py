"""
_backtest_rebalance.py — 模拟自动 rebalance 的历史效果

方法:
1. 从 rebalance_plan.jsonl (auto_rebalance 每次 pre-close 落盘的记录) 里
   取每天的 planned orders
2. 模拟"如果这些 orders 都被执行", 组合 NAV 会怎样演化
3. 对比 baseline (什么都不动, 现有仓位跑到今天)

也能对 "如果从 N 天前就启用" 做假设性 backfill: 用当天的信号 + 价格,
调用 compute_target_weights + plan_rebalance, 得到那天的 planned orders.

CLI:
    python _backtest_rebalance.py           # 分析已有 rebalance_plan.jsonl
    python _backtest_rebalance.py --backfill 30   # 假设 30 天前启用, backfill 模拟
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

from config import SIGNALS_DIR
from auto_rebalance import (
    _load_current_positions, _load_signals,
    compute_target_weights, plan_rebalance,
)


def simulate_orders_impact(orders: list[dict], days_ahead: int = 5,
                            entry_date: date | None = None) -> dict:
    """给定订单列表 + 入场日期, 用 yfinance 查 entry_date + N 天的 close, 算 PnL.
    entry_date=None → 用今天 (通常没有未来数据, 会返回 skipped).
    """
    if not orders:
        return {"gross_pnl": 0.0, "per_order": [], "skipped": 0}
    try:
        import yfinance as yf
    except ImportError:
        return {"error": "yfinance not available"}

    if entry_date is None:
        entry_date = date.today()

    per_order = []
    total_pnl = 0.0
    skipped = 0
    for o in orders:
        tk = o["ticker"]
        qty = o["qty"]
        side = o["side"]
        try:
            hist = yf.Ticker(tk).history(period="60d")
            if hist.empty:
                skipped += 1
                continue
            hist_dates = [ts.date() for ts in hist.index]
            hist_close = list(hist["Close"])
            entry_idx = None
            for i, d in enumerate(hist_dates):
                if d >= entry_date:
                    entry_idx = i
                    break
            if entry_idx is None:
                skipped += 1
                continue
            target_idx = entry_idx + days_ahead
            if target_idx >= len(hist_close):
                skipped += 1
                continue
            # 关键: entry_px 用 entry_date 当天的 close, 不用今天的价.
            # 这样 5d forward return 才有真实意义.
            entry_px = float(hist_close[entry_idx])
            exit_px = float(hist_close[target_idx])
        except Exception:
            skipped += 1
            continue
        if side == "SELL":
            pnl = -(exit_px - entry_px) * qty
        else:
            pnl = (exit_px - entry_px) * qty
        total_pnl += pnl
        per_order.append({
            "ticker": tk, "side": side, "qty": qty,
            "entry": entry_px, "exit_5d": exit_px,
            "pnl": round(pnl, 0),
            "pct": round((exit_px - entry_px) / entry_px * 100, 2),
        })
    return {"gross_pnl": round(total_pnl, 0), "per_order": per_order,
            "skipped": skipped}


def analyze_existing_plans() -> None:
    """分析 rebalance_plan.jsonl 里已有的历史计划."""
    p = Path(SIGNALS_DIR) / "rebalance_plan.jsonl"
    if not p.exists():
        print("(无 rebalance_plan.jsonl, 先跑 auto_rebalance.py 至少一次)")
        return
    plans = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            plans.append(json.loads(line))
        except Exception:
            continue
    print(f"历史 rebalance 计划: {len(plans)} 次")
    for pl in plans[-5:]:
        ts = pl.get("ts", "")[:16]
        n = pl.get("n_orders", 0)
        dry = pl.get("dry_run")
        # 计划日期解析
        try:
            entry_d = date.fromisoformat(pl.get("ts", "")[:10])
        except Exception:
            entry_d = date.today()
        days_ago = (date.today() - entry_d).days
        print(f"  {ts} dry={dry} n_orders={n} (计划日 {days_ago}d 前)")
        if pl.get("orders") and days_ago >= 5:
            impact = simulate_orders_impact(pl["orders"], days_ahead=5,
                                              entry_date=entry_d)
            skipped = impact.get("skipped", 0)
            print(f"    5d 实测: gross P&L ${impact.get('gross_pnl', 0):,.0f}"
                  + (f" (skipped {skipped})" if skipped else ""))
            for o in impact.get("per_order", []):
                print(f"      {o['side']:<4} {o['qty']:>5} {o['ticker']:<6} "
                      f"@ ${o['entry']:.2f} → ${o['exit_5d']:.2f} "
                      f"({o['pct']:+.1f}%)  pnl ${o['pnl']:,.0f}")
        elif pl.get("orders"):
            print(f"    ({days_ago}d 前的计划, 尚不足 5d 前向数据, 跳过)")


def simulate_current_plan() -> None:
    """用当前的持仓 + 信号跑一次 rebalance, 模拟 5d/10d 影响."""
    positions, cash, nav = _load_current_positions()
    signals, prices = _load_signals()
    if nav <= 0:
        print("(无 NAV 数据)")
        return

    # regime 简化: 取第一个信号的 regime
    regime = next((s["regime"] for s in signals.values() if s.get("regime")), "neutral")
    targets = compute_target_weights(signals, regime, drawdown_pct=0)
    orders = plan_rebalance(positions, cash, nav, targets, prices=prices)

    print(f"\n=== 当前计划模拟 (NAV ${nav:,.0f}, regime={regime}) ===")
    print(f"orders: {len(orders)}")
    for o in orders:
        print(f"  {o['side']:<4} {o['qty']:>6} {o['ticker']:<6} "
              f"@ ${o['price']:.2f}  =${o['usd']:>8,.0f}  {o['reason']}")

    # 用 N 天前的日期 backfill 模拟, 才能真拿到 5d/10d 前向数据
    # 5 交易日 ~= 8-10 日历日 (跨周末); 10 交易日 ~= 14-17 日历日
    from datetime import timedelta
    for days, cal_offset in [(5, 10), (10, 17)]:
        backdate = date.today() - timedelta(days=cal_offset)
        impact = simulate_orders_impact(orders, days_ahead=days,
                                          entry_date=backdate)
        print(f"\n  若 {backdate} 就下这些订单, {days}d 净影响: ${impact.get('gross_pnl', 0):+,.0f}"
              + (f" (skipped {impact.get('skipped',0)})" if impact.get('skipped') else ""))
        sells = [o for o in impact.get("per_order", []) if o["side"] == "SELL"]
        buys  = [o for o in impact.get("per_order", []) if o["side"] == "BUY"]
        if sells:
            spnl = sum(o["pnl"] for o in sells)
            print(f"    SELL 节省损失: ${spnl:+,.0f}")
        if buys:
            bpnl = sum(o["pnl"] for o in buys)
            print(f"    BUY 新头寸 P&L: ${bpnl:+,.0f}")
        for o in impact.get("per_order", []):
            print(f"    {o['side']:<4} {o['qty']:>5} {o['ticker']:<6} "
                  f"@ ${o['entry']:.2f} → ${o['exit_5d']:.2f} ({o['pct']:+.1f}%)  pnl ${o['pnl']:,.0f}")
        sells = [o for o in impact.get("per_order", []) if o["side"] == "SELL"]
        buys  = [o for o in impact.get("per_order", []) if o["side"] == "BUY"]
        if sells:
            spnl = sum(o["pnl"] for o in sells)
            print(f"    SELL 节省损失: ${spnl:+,.0f}")
        if buys:
            bpnl = sum(o["pnl"] for o in buys)
            print(f"    BUY 新头寸 P&L: ${bpnl:+,.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0,
                    help="模拟 N 天前启用, 补跑历史 rebalance (未实现: 需重放 signal)")
    args = ap.parse_args()

    if args.backfill > 0:
        print("(--backfill 需重放历史信号, 首版未实现)")
    print("=" * 80)
    print("已有 rebalance_plan.jsonl 记录分析")
    print("=" * 80)
    analyze_existing_plans()

    print()
    print("=" * 80)
    print("当前持仓 + 信号跑一次模拟")
    print("=" * 80)
    simulate_current_plan()


if __name__ == "__main__":
    main()
