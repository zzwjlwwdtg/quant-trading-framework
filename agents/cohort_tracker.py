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


def _fired_key_in_ledger(fired_key: Optional[str]) -> bool:
    """F3-#3 (2026-09-24): fired_key 是否已写进 cohort ledger.

    active cohort 已关闭 (被 pop) 后, 仅靠 active state 无法识别重放;
    ledger 事件带 fired_key 字段 → 扫描一次即可. 旧事件无该字段 → 不匹配.
    """
    if not fired_key or not _LEDGER.exists():
        return False
    try:
        with open(_LEDGER, encoding="utf-8") as f:
            for line in f:
                if fired_key not in line:
                    continue
                try:
                    if json.loads(line).get("fired_key") == fired_key:
                        return True
                except Exception:
                    continue
    except Exception:
        return False
    return False


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
    # F3 followup fix (2026-09-23): idempotent by fired_key. paper_trader passes
    # signal_ctx.fired_key = "env|acc|oid|cum_dealt" — 若同一 fired_key 已被记录
    # (crash-recovery 场景 fired_ledger append 失败 + state.clear 导致 refresh
    # 再次 fire), 幂等 skip 避免 cohort 双计.
    fired_key = signal_ctx.get("fired_key")
    if fired_key:
        if cohort and any(isinstance(e, dict) and e.get("fired_key") == fired_key
                          for e in cohort.get("entries", [])):
            return cohort   # already recorded, skip
        if _fired_key_in_ledger(fired_key):
            # cohort 已关闭后重放同一 BUY 成交 → 不得开新 cohort
            return cohort or {}

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
                "ts":         ts,
                "price":      round(float(exec_price), 4),
                "qty":        int(exec_qty),
                "tag":        signal_ctx.get("tag"),
                "fired_key":  fired_key,
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
            "fired_key":   fired_key,
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
        "ts":         ts,
        "price":      round(float(exec_price), 4),
        "qty":        int(exec_qty),
        "tag":        signal_ctx.get("tag"),
        "fired_key":  fired_key,
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
        "fired_key":   fired_key,
        "signal_ctx":  signal_ctx,
    })
    return cohort


def on_sell(ticker: str, exec_price: float, exec_qty: int,
            exit_reason: str = "", ts: Optional[str] = None,
            context=None, fired_key: Optional[str] = None) -> Optional[dict]:
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

    # F3-#3 (2026-09-24): SELL 也按 fired_key 幂等. 之前只有 on_buy 去重,
    # fired_ledger append 失败 + state 丢失后重放同一 SELL 成交会再卖一次
    # (部分退出被放大, 甚至误 close cohort).
    if fired_key:
        if any(isinstance(x, dict) and x.get("fired_key") == fired_key
               for x in cohort.get("exits", [])):
            return None
        if _fired_key_in_ledger(fired_key):
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
        "fired_key": fired_key,
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
            "fired_key": fired_key,
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
        "fired_key":       fired_key,
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
    """聚合近 N 天已 close 的 cohorts: n / win_rate / avg_pnl_pct / total_pnl_usd / best / worst / avg_hold.

    F04 deep audit (2026-09-19): cohort ledger 有 36% legacy_unreconciled
    (pre-fill-migration 期间的 phantom entries). 结果字段附 `authority`
    警示等级 + 建议用 fill_ledger 交叉验证.
    """
    cohorts = _load_closed_cohorts(since_days)
    if not cohorts:
        return {"n": 0, "since_days": since_days,
                "authority": "no_data", "warning": None}
    wins = [c for c in cohorts if c.get("is_winner")]
    pnl_pcts = [c.get("realized_pnl_pct", 0) for c in cohorts]
    pnl_usds = [c.get("realized_pnl_usd", 0) for c in cohorts]
    holds = [c.get("hold_days", 0) for c in cohorts]

    # F04 deep: 查 fill_ledger 交叉验证.
    # R08 fix (2026-09-20 audit): 之前 heuristic 有洞 — cohort > 0 但 fill sells=0
    # 时不 warn (因为条件 fill_sells>0). 现在: 只要 cohort 存在 broker 无对应
    # fill sell 就 warn (完全无成交对账证据). 明确标示 "sample heuristic, 非权威".
    authority = "cohort_ledger_only"
    warning = None
    try:
        from fill_ledger import get_fills
        # 只做 sanity: 计算 fills 里的 sell count vs cohort close count
        fill_sells = [ev for ev in get_fills(include_partial=False)
                       if (ev.get("side") or "").upper() in ("SELL", "SELL_ALL", "REDUCE")]
        if len(cohorts) > 0:
            if len(fill_sells) == 0:
                # R08: 0 broker sells 但 N cohort close → 完全无成交证据
                authority = "cohort_ledger_only_no_broker_evidence"
                warning = (f"{len(cohorts)} closed cohorts but broker has 0 SELL fills. "
                            f"cohort 数据 pre-dates ledger 或 broker 侧无对账证据. "
                            f"这些数字仅供参考, 不是 authoritative P&L.")
            else:
                ratio = len(cohorts) / len(fill_sells)
                if ratio > 2.0:
                    authority = "cohort_ledger_only_unreconciled"
                    warning = (f"cohort close 数 ({len(cohorts)}) 显著多于 broker fill sells "
                                f"({len(fill_sells)}), 部分 cohort 可能是 phantom "
                                f"(pre-F04-fix). 建议 _reconcile_cohorts.py 交叉核实.")
    except Exception:
        pass

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
        "authority":       authority,
        "warning":         warning,
    }


def stats_from_fills(since_days: int = 30) -> dict:
    """R02 event-replay stats. V5-01 audit rewrite (2026-09-23).

    Key semantic fixes from V5-01 audit:
    - Round-trips (not tickers) are the unit for wins/losses. One ticker
      that goes flat, then buys again, then goes flat again, counts as 2.
    - Sells with no matched buy layer (期初 unknown / pre-window buy) are
      reported as unreconciled_tickers + unreconciled_sells_qty, NOT as
      losers. realized_pnl for those is not credited.
    - Buy events BEFORE the window are used to seed cost basis for sells
      WITHIN the window, so a 40-day-old buy + yesterday's sell counts.
      Only sell events time-attribute to the window.
    - Authority label degrades to fills_replay_partial when unreconciled
      exist, and warning is set explicitly. Never `authority=fills_replay
      + warning=None` unless everything reconciled.

    Return shape (V5-01):
      n:                     round-trip count in window
      n_winners / n_losers:  from realized round-trips only
      total_pnl_usd:         sum of realized round-trip PnL (excludes unknown)
      unreconciled_tickers:  list of tickers with unmatched sells in window
      unreconciled_sells_qty: total unmatched sell shares
      authority:             "fills_replay" (clean) or "fills_replay_partial"
      warning:               None if clean, else description
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    try:
        from fill_ledger import get_fills, fill_increments, apply_split
    except Exception:
        return {
            "n": 0, "since_days": since_days,
            "authority": "fill_ledger_unavailable",
            "warning": "fill_ledger import failed",
            "n_roundtrips": 0, "n_winners": 0, "n_losers": 0,
            "total_pnl_usd": 0, "n_events": 0,
            "unreconciled_tickers": [], "unreconciled_sells_qty": 0,
            "positions": {},
        }
    cutoff_dt = _dt.now(_tz.utc) - _td(days=since_days)
    cutoff_iso = cutoff_dt.isoformat()
    # V5-01 fix: pre-window BUYs also loaded so we can seed cost basis
    all_fills = get_fills(include_partial=True)
    if not all_fills:
        return {
            "n": 0, "since_days": since_days,
            "authority": "fills_replay",
            "warning": None,
            "n_roundtrips": 0, "n_winners": 0, "n_losers": 0,
            "total_pnl_usd": 0, "n_events": 0,
            "unreconciled_tickers": [], "unreconciled_sells_qty": 0,
            "positions": {},
        }
    # 2026-09-24: 共用 fill_ledger.fill_increments reducer (与 get_position 同一套
    # 时序差分 / 价格修订 / canonical ticker), 不再在这里另写一份.
    increments, reducer_meta = fill_increments(all_fills)
    from collections import defaultdict as _dd
    by_ticker = _dd(list)
    for inc in increments:
        if inc["ticker"]:
            by_ticker[inc["ticker"]].append(inc)

    # Per-ticker FIFO walk detecting round-trips within window
    total_realized = 0.0
    n_wins = 0
    n_losses = 0
    n_roundtrips = 0
    unreconciled_tickers: set[str] = set()
    unreconciled_sells_qty = 0.0
    events_in_window = 0
    positions: dict = {}   # ticker → {qty, avg_cost} for open positions
    pnl_by_origin: dict[str, float] = {}

    for tk, tk_events in by_ticker.items():
        # increments 已按 ts 排序且是订单增量 (F1: 不先压缩订单)
        layers: list[dict] = []
        rt_realized_current = 0.0   # current in-progress round-trip
        rt_had_buys = False         # became active after a buy
        tk_unreconciled_qty = 0.0

        for inc in tk_events:
            if inc["side"] == "SPLIT":
                apply_split(layers, inc["ratio"])
                continue
            in_window = inc["ts"] >= cutoff_iso
            side = inc["side"]
            delta_qty = inc["qty"]
            delta_price = inc["price"]

            if in_window:
                events_in_window += 1

            if side == "BUY":
                # Buy pre-window OR in-window: seeds cost basis (V5-01: pre-window BUY
                # inherits cost for in-window sells)
                layers.append({"qty": delta_qty, "price": delta_price,
                               "origin": inc.get("origin", "system")})
                rt_had_buys = True
            elif side in ("SELL", "SELL_ALL", "REDUCE"):
                remaining = delta_qty
                sell_realized = 0.0
                while remaining > 0 and layers and layers[0]["qty"] > 0:
                    layer = layers[0]
                    take = min(remaining, layer["qty"])
                    piece = take * (delta_price - layer["price"])
                    sell_realized += piece
                    if in_window:
                        # 谁开的仓算谁的 (2026-09-24): 按被消耗买入层的 origin 归属
                        o = layer.get("origin", "system")
                        pnl_by_origin[o] = pnl_by_origin.get(o, 0.0) + piece
                    layer["qty"] -= take
                    remaining     -= take
                    if layer["qty"] <= 1e-9:
                        layers.pop(0)
                # V5-01 fix: unmatched sell qty → unreconciled (NOT loser, NOT realized)
                if remaining > 0:
                    tk_unreconciled_qty += remaining
                    unreconciled_sells_qty += remaining
                # Attribute realized to window based on SELL time
                if in_window:
                    total_realized += sell_realized
                    rt_realized_current += sell_realized
                # If all layers gone → round-trip closed
                if not layers or all(l["qty"] <= 1e-9 for l in layers):
                    if rt_had_buys and in_window:
                        # Only count round-trip if sell was in-window (audit: 卖出实际时间)
                        n_roundtrips += 1
                        if rt_realized_current > 0:
                            n_wins += 1
                        elif rt_realized_current < 0:
                            n_losses += 1
                        # rt_realized_current == 0 → not counted as loss (V5-01)
                    rt_realized_current = 0.0
                    rt_had_buys = False

        if tk_unreconciled_qty > 0:
            unreconciled_tickers.add(tk)
        # Backward-compat: expose remaining open layers as position
        remaining_qty = sum(l["qty"] for l in layers if l["qty"] > 0)
        if remaining_qty > 0:
            remaining_cost = sum(l["qty"] * l["price"] for l in layers if l["qty"] > 0)
            positions[tk] = {
                "qty":      remaining_qty,
                "avg_cost": remaining_cost / remaining_qty if remaining_qty > 0 else 0.0,
            }

    # V5-01: authority degrades when unreconciled exist
    has_unreconciled = bool(unreconciled_tickers)
    qty_reversals = reducer_meta["qty_reversals"]
    authority = ("fills_replay_partial" if (has_unreconciled or qty_reversals)
                 else "fills_replay")
    warnings = []
    if has_unreconciled:
        warnings.append(
            f"{len(unreconciled_tickers)} tickers have unreconciled sells "
            f"({int(unreconciled_sells_qty)} shares total) — cost basis unknown. "
            f"Consumed as pre-window state without matched buys.")
    if qty_reversals:
        warnings.append(
            f"{qty_reversals} broker cumulative-qty reversal(s) (bust/cancel) not "
            f"auto-reconciled — affected orders keep their pre-reversal fills.")
    warning = " ".join(warnings) or None

    win_rate = round(n_wins / n_roundtrips * 100, 1) if n_roundtrips else 0.0

    return {
        "n":                     n_roundtrips,
        "n_roundtrips":          n_roundtrips,
        "since_days":            since_days,
        "n_winners":             n_wins,
        "n_losers":              n_losses,
        "win_rate":              win_rate,
        "total_pnl_usd":         round(total_realized, 2),
        "n_events":              events_in_window,
        "n_tickers":             len(by_ticker),
        "authority":             authority,
        "warning":               warning,
        "unreconciled_tickers":  sorted(unreconciled_tickers),
        "unreconciled_sells_qty": int(unreconciled_sells_qty),
        "price_revisions":       reducer_meta["price_revisions"],
        "qty_reversals":         qty_reversals,
        "splits_applied":        reducer_meta.get("splits_applied", 0),
        "pnl_by_origin":         {k: round(v, 2) for k, v in sorted(pnl_by_origin.items())},
        "positions":             positions,
    }


def format_stats(since_days: int = 30, prefer_fills: bool = True) -> str:
    """R02 v4 migration (2026-09-22): 默认优先 fills_replay (broker 权威源).
    prefer_fills=False → 走旧 cohort ledger stats() (audit trail 保留).

    双源都展示: fills 权威, cohort ledger 参考; 分歧显著时明确 flag.
    """
    s_fills = stats_from_fills(since_days)
    s_ledger = stats(since_days)
    active = all_active_cohorts()

    # 主源: 若 prefer_fills 且 fills 有数据, 用 fills; 否则 fallback ledger
    primary = s_fills if (prefer_fills and s_fills.get("n_events", 0) > 0) else s_ledger
    src_label = primary.get("authority", "unknown")

    lines = [
        f"=== Position Stats · 近 {since_days} 天 · 数据源: {src_label} ===",
    ]
    if primary is s_fills:
        # V5-01 #3: 本地执行记录重建 ≠ 券商账户级对账 (无期初快照/全历史/费用)
        lines.append("  (本地成交记录 FIFO 重建估算, 未做账户级对账)")

    if primary.get("n", 0) == 0 and (primary.get("n_events", 0) == 0):
        lines.append(f"  近 {since_days} 天无 fill/close 事件.")
        # 仍展示 active + cohort ledger diff
    else:
        # F2 followup fix (2026-09-23): 之前 render 仅按 exact-match "fills_replay"
        # 分流, "fills_replay_partial" fallthrough 到旧 ledger 分支 → 标题写新源、
        # 正文却是旧账本数字. 现在: 按 primary is s_fills 与否, 一律 render primary
        # 自己的数字, 与 authority 字符串解耦; partial 追加 unreconciled warning.
        primary_is_fills = primary is s_fills
        if primary_is_fills:
            lines.append(f"  closed round-trips:  {s_fills['n']}   "
                          f"({s_fills['n_winners']}W / {s_fills['n_losers']}L)")
            lines.append(f"  胜率:              {s_fills.get('win_rate', 0)}%")
            lines.append(f"  total 实现 P&L:    ${s_fills.get('total_pnl_usd', 0):+,.2f}")
            by_o = s_fills.get("pnl_by_origin") or {}
            if by_o:
                names = {"system": "系统开仓", "manual": "手动开仓", "unknown": "来源未知"}
                lines.append("  按开仓来源:        " + " · ".join(
                    f"{names.get(k, k)} ${v:+,.2f}" for k, v in sorted(by_o.items(),
                                                                      key=lambda kv: kv[0] != "system")))
            lines.append(f"  broker fill events: {s_fills.get('n_events', 0)} 条 "
                          f"({s_fills.get('n_tickers', 0)} tickers)")
            n_rev = s_fills.get("price_revisions", 0) or 0
            n_rvs = s_fills.get("qty_reversals", 0) or 0
            if n_rev or n_rvs:
                lines.append(f"  回报修订: 价格修订 {n_rev} 条 (已按修订价回溯), "
                              f"数量倒退 {n_rvs} 条 (未自动冲回)")
            if src_label == "fills_replay_partial":
                unreco = s_fills.get("unreconciled_tickers") or []
                unreco_qty = s_fills.get("unreconciled_sells_qty", 0)
                lines.append(f"  ⚠ partial: {len(unreco)} tickers 有 unreconciled sells "
                              f"({unreco_qty} 股, cost basis 未知): {', '.join(unreco[:5])}"
                              + (" ..." if len(unreco) > 5 else ""))
                if s_fills.get("warning"):
                    lines.append(f"  ⚠ {s_fills['warning']}")
        else:
            lines.append(f"  已 close cohorts:  {s_ledger['n']}   "
                          f"({s_ledger.get('n_winners', 0)}W / {s_ledger.get('n_losers', 0)}L)")
            lines.append(f"  胜率:              {s_ledger.get('win_rate', 0)}%")
            lines.append(f"  total 实现 P&L:    ${s_ledger.get('total_pnl_usd', 0):+,.2f}")

    # 双源对比 (若两个 authority 都有数据 且 divergent)
    if (s_fills.get("n", 0) > 0 or s_ledger.get("n", 0) > 0):
        fills_pnl  = s_fills.get("total_pnl_usd", 0)
        ledger_pnl = s_ledger.get("total_pnl_usd", 0)
        if abs(fills_pnl - ledger_pnl) > 100:   # >$100 divergence 值得注意
            lines.append("")
            # V5-01 #5: 两源样本范围不同, 差额不能归因于任一方账本错误.
            lines.append(f"⚠ 双源分歧: fills_replay=${fills_pnl:+,.2f} vs "
                          f"cohort_ledger=${ledger_pnl:+,.2f} "
                          f"(口径不同: fills 含仍持仓标的的已实现部分, cohort 只含"
                          f"已关闭 cohort; 差额不代表任一方错误)")

    lines.append("")
    lines.append(f"当前 active cohorts (旧 cohort 投影, 非权威, 仅供对比): {len(active)}")
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
