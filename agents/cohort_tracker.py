"""cohort_tracker.py — 每个"信号触发的持仓 cohort" 从入场到出场的完整 P&L 追踪.

用户 spec (2026-09-08):
    每次某个标的给出加仓预期时, 一旦股价打到设定值就开始统计,
    直到系统判断卖出为止. 需要看盈亏 + 胜率.

数据模型:
    Cohort = 一次"信号驱动的持仓周期"
    - 起点: 第一笔 BUY 成交 (signal target 打到)
    - 中间: pyramid 加仓 (同一 cohort 延续)
    - 终点: 系统 SELL 到 qty=0 (trailing-stop / take-profit / rule sell 都算)

    Cohort 内多笔 BUY 权重平均入场价. 多笔 SELL (阶梯止盈) 累计 realized P&L,
    最后一笔 SELL 归零 qty 时 close cohort.

Storage:
    signals/position_cohorts.jsonl        - append-only 事件日志 (open/add/exit/close)
    signals/position_cohorts_active.json  - 每 ticker 当前 active cohort 状态

API:
    on_buy(ticker, exec_price, exec_qty, signal_ctx, ts)
        signal_ctx = {action, confidence, regime, entry_target, reason, tag}
    on_sell(ticker, exec_price, exec_qty, exit_reason, ts)
    active_cohort(ticker) -> dict | None
    all_active_cohorts() -> list[dict]
    stats(since_days=30) -> dict  # 胜率 / avg / total / best / worst
    format_stats(since_days=30) -> str  # 人类可读文本

CLI:
    python cohort_tracker.py --stats [--days 30]
    python cohort_tracker.py --active

集成 (paper_trader._log_trade 尾部):
    from cohort_tracker import on_buy, on_sell
    if side.upper() == "BUY":
        on_buy(ticker, price, qty, signal_ctx={...}, ts=ts)
    elif side.upper() in ("SELL", "SELL_ALL", "REDUCE"):
        on_sell(ticker, price, qty, exit_reason=tag, ts=ts)
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import SIGNALS_DIR

_LEDGER = Path(SIGNALS_DIR) / "position_cohorts.jsonl"
_ACTIVE = Path(SIGNALS_DIR) / "position_cohorts_active.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_active() -> dict:
    if not _ACTIVE.exists():
        return {}
    try:
        return json.loads(_ACTIVE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_active(state: dict) -> None:
    try:
        from atomic_io import atomic_write_json
        atomic_write_json(_ACTIVE, state)
    except Exception:
        try:
            _ACTIVE.parent.mkdir(parents=True, exist_ok=True)
            _ACTIVE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


def _append_ledger(entry: dict) -> None:
    try:
        from atomic_io import append_jsonl
        append_jsonl(_LEDGER, entry)
    except Exception:
        try:
            _LEDGER.parent.mkdir(parents=True, exist_ok=True)
            with open(_LEDGER, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass


def on_buy(ticker: str, exec_price: float, exec_qty: int,
           signal_ctx: Optional[dict] = None, ts: Optional[str] = None,
           context=None) -> dict:
    """处理 BUY 成交: 无 active cohort → open, 有 → add.
    signal_ctx 建议含: {action, confidence, regime, entry_target, reason, tag}
    返回当前 cohort 状态.

    WP04 (2026-09-20): 若 context (DecisionContext) 提供且未显式传 ts,
    用 context.as_of 时间戳 — 让 backtest replay 打出的 cohort 有历史 ts
    而非 now(). 优先级: 显式 ts > context.as_of > now()."""
    if exec_qty <= 0 or exec_price <= 0:
        return {}
    if ts is None and context is not None:
        try:
            ts = context.as_of.isoformat()
        except Exception:
            ts = None
    ts = ts or _now_iso()
    signal_ctx = signal_ctx or {}
    state = _load_active()
    cohort = state.get(ticker)

    if cohort is None:
        # 新 cohort
        cohort_id = f"{ticker}_{ts}"
        cohort = {
            "cohort_id":            cohort_id,
            "ticker":               ticker,
            "open_ts":              ts,
            "signal_action":        signal_ctx.get("action"),
            "signal_confidence":    signal_ctx.get("confidence"),
            "signal_regime":        signal_ctx.get("regime"),
            "signal_entry_target":  signal_ctx.get("entry_target"),
            "signal_reason":        signal_ctx.get("reason"),
            "signal_tag":           signal_ctx.get("tag"),
            "entries": [{
                "ts":     ts,
                "price":  round(float(exec_price), 4),
                "qty":    int(exec_qty),
                "tag":    signal_ctx.get("tag"),
            }],
            "exits": [],
            "current_qty":          int(exec_qty),
            "avg_entry_price":      round(float(exec_price), 4),
            "cost_basis_usd":       round(float(exec_price) * int(exec_qty), 2),
            "realized_pnl_usd":     0.0,
            "status":               "active",
        }
        state[ticker] = cohort
        _save_active(state)
        _append_ledger({
            "event":       "open",
            "ts":          ts,
            "ticker":      ticker,
            "cohort_id":   cohort_id,
            "price":       round(float(exec_price), 4),
            "qty":         int(exec_qty),
            "signal_ctx":  signal_ctx,
        })
        return cohort

    # 已有 active cohort → pyramid add
    old_qty = int(cohort.get("current_qty", 0))
    old_cost = float(cohort.get("cost_basis_usd", 0))
    add_cost = float(exec_price) * int(exec_qty)
    new_qty = old_qty + int(exec_qty)
    new_cost = old_cost + add_cost
    new_avg = new_cost / new_qty if new_qty > 0 else 0
    cohort["entries"].append({
        "ts":    ts,
        "price": round(float(exec_price), 4),
        "qty":   int(exec_qty),
        "tag":   signal_ctx.get("tag"),
    })
    cohort["current_qty"] = new_qty
    cohort["cost_basis_usd"] = round(new_cost, 2)
    cohort["avg_entry_price"] = round(new_avg, 4)
    state[ticker] = cohort
    _save_active(state)
    _append_ledger({
        "event":       "add",
        "ts":          ts,
        "ticker":      ticker,
        "cohort_id":   cohort["cohort_id"],
        "price":       round(float(exec_price), 4),
        "qty":         int(exec_qty),
        "new_qty":     new_qty,
        "new_avg":     round(new_avg, 4),
        "signal_ctx":  signal_ctx,
    })
    return cohort


def on_sell(ticker: str, exec_price: float, exec_qty: int,
            exit_reason: str = "", ts: Optional[str] = None,
            context=None) -> Optional[dict]:
    """处理 SELL 成交: 部分卖 → 记 exit; 卖到 qty=0 → close cohort + 记 stats.
    返回 closed cohort (若 close 了), 否则 None (仅部分退出).

    WP04 (2026-09-20): 同 on_buy — context.as_of 作为默认 ts, 允许 backtest
    replay 记录历史时间戳."""
    if exec_qty <= 0 or exec_price <= 0:
        return None
    if ts is None and context is not None:
        try:
            ts = context.as_of.isoformat()
        except Exception:
            ts = None
    ts = ts or _now_iso()
    state = _load_active()
    cohort = state.get(ticker)
    if cohort is None:
        # 无 active cohort → 忽略 (SELL 可能是 REBALANCE / 手工, 不属于 cohort 跟踪)
        return None

    sell_qty = min(int(exec_qty), int(cohort.get("current_qty", 0)))
    if sell_qty <= 0:
        return None

    avg_entry = float(cohort.get("avg_entry_price", 0))
    realized_this = (float(exec_price) - avg_entry) * sell_qty
    cohort["exits"].append({
        "ts":       ts,
        "price":    round(float(exec_price), 4),
        "qty":      sell_qty,
        "reason":   exit_reason,
        "pnl_usd":  round(realized_this, 2),
    })
    cohort["current_qty"] = int(cohort.get("current_qty", 0)) - sell_qty
    cohort["realized_pnl_usd"] = round(
        float(cohort.get("realized_pnl_usd", 0)) + realized_this, 2)

    if cohort["current_qty"] > 0:
        # 部分退出 (阶梯止盈), cohort 保持 active
        state[ticker] = cohort
        _save_active(state)
        _append_ledger({
            "event":     "partial_exit",
            "ts":        ts,
            "ticker":    ticker,
            "cohort_id": cohort["cohort_id"],
            "price":     round(float(exec_price), 4),
            "qty":       sell_qty,
            "reason":    exit_reason,
            "pnl_usd":   round(realized_this, 2),
            "remain_qty": cohort["current_qty"],
        })
        return None

    # 全出 → close cohort
    open_ts = cohort.get("open_ts")
    try:
        hold_days = (
            datetime.fromisoformat(ts.replace("Z", "+00:00")) -
            datetime.fromisoformat(open_ts.replace("Z", "+00:00"))
        ).total_seconds() / 86400
    except Exception:
        hold_days = 0.0
    cost = float(cohort.get("cost_basis_usd", 0))
    realized = float(cohort.get("realized_pnl_usd", 0))
    pnl_pct = (realized / cost * 100) if cost > 0 else 0.0

    cohort["close_ts"]           = ts
    cohort["close_reason"]       = exit_reason
    cohort["hold_days"]          = round(hold_days, 2)
    cohort["realized_pnl_pct"]   = round(pnl_pct, 3)
    cohort["is_winner"]          = realized > 0
    cohort["status"]             = "closed"

    # 从 active state 移除
    state.pop(ticker, None)
    _save_active(state)
    _append_ledger({
        "event":           "close",
        "ts":              ts,
        "ticker":          ticker,
        "cohort_id":       cohort["cohort_id"],
        "close_price":     round(float(exec_price), 4),
        "close_reason":    exit_reason,
        "hold_days":       round(hold_days, 2),
        "realized_pnl_usd": round(realized, 2),
        "realized_pnl_pct": round(pnl_pct, 3),
        "is_winner":       realized > 0,
        "cohort":          cohort,   # 完整 snapshot 供后续统计追溯
    })
    return cohort


def active_cohort(ticker: str) -> Optional[dict]:
    return _load_active().get(ticker)


def all_active_cohorts() -> list[dict]:
    return list(_load_active().values())


def _load_closed_cohorts(since_days: int = 30) -> list[dict]:
    """从 ledger 里读所有 close event 里的 cohort snapshot (近 N 天)."""
    if not _LEDGER.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - since_days * 86400
    out = []
    for line in _LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("event") != "close":
            continue
        try:
            e_ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if e_ts < cutoff:
            continue
        c = e.get("cohort") or {}
        if c:
            out.append(c)
    return out


def stats(since_days: int = 30) -> dict:
    """聚合近 N 天已 close 的 cohorts: n / win_rate / avg_pnl_pct / total_pnl_usd / best / worst / avg_hold."""
    cohorts = _load_closed_cohorts(since_days)
    if not cohorts:
        return {"n": 0, "since_days": since_days}
    wins = [c for c in cohorts if c.get("is_winner")]
    pnl_pcts = [c.get("realized_pnl_pct", 0) for c in cohorts]
    pnl_usds = [c.get("realized_pnl_usd", 0) for c in cohorts]
    holds = [c.get("hold_days", 0) for c in cohorts]
    return {
        "n":               len(cohorts),
        "since_days":      since_days,
        "n_winners":       len(wins),
        "n_losers":        len(cohorts) - len(wins),
        "win_rate":        round(len(wins) / len(cohorts) * 100, 1),
        "avg_pnl_pct":     round(statistics.mean(pnl_pcts), 3),
        "median_pnl_pct":  round(statistics.median(pnl_pcts), 3),
        "total_pnl_usd":   round(sum(pnl_usds), 2),
        "best_pnl_pct":    round(max(pnl_pcts), 3),
        "worst_pnl_pct":   round(min(pnl_pcts), 3),
        "avg_hold_days":   round(statistics.mean(holds), 2),
    }


def format_stats(since_days: int = 30) -> str:
    s = stats(since_days)
    if s["n"] == 0:
        return f"过去 {since_days} 天无 closed cohort. (系统尚未积累或数据 stale)"
    active = all_active_cohorts()
    lines = [
        f"=== Position Cohort Stats · 近 {since_days} 天 ===",
        f"  已 close cohorts:  {s['n']}   ({s['n_winners']}W / {s['n_losers']}L)",
        f"  胜率:              {s['win_rate']}%",
        f"  avg P&L:           {s['avg_pnl_pct']:+.2f}%   median: {s['median_pnl_pct']:+.2f}%",
        f"  total 实现 P&L:    ${s['total_pnl_usd']:+,.2f}",
        f"  best / worst:      {s['best_pnl_pct']:+.2f}% / {s['worst_pnl_pct']:+.2f}%",
        f"  avg 持仓天数:      {s['avg_hold_days']:.1f}d",
        f"",
        f"当前 active cohorts: {len(active)}",
    ]
    for c in active:
        cur_qty = c.get("current_qty", 0)
        avg_e = c.get("avg_entry_price", 0)
        real = c.get("realized_pnl_usd", 0)
        lines.append(f"  · {c['ticker']:<10}  qty={cur_qty}  avg_entry=${avg_e:.2f}  "
                     f"realized(部分)=${real:+.2f}  开 {c['open_ts'][:10]}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--active", action="store_true")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.stats:
        if args.json:
            print(json.dumps(stats(args.days), ensure_ascii=False, indent=2))
        else:
            print(format_stats(args.days))
    elif args.active:
        active = all_active_cohorts()
        if args.json:
            print(json.dumps(active, ensure_ascii=False, indent=2))
        else:
            print(f"active cohorts: {len(active)}")
            for c in active:
                print(f"  {c['ticker']:<10} qty={c.get('current_qty')} "
                      f"avg=${c.get('avg_entry_price')} open={c.get('open_ts')[:10]}")
    else:
        parser.print_help()
