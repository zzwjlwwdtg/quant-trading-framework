"""fill_ledger.py — 权威 fill 事件读取器 (WP03 深度重构).

audit F04 finding: cohort_tracker / position 统计源应是 broker fill 事件,
不是 submit 事件. 之前 paper_trader._log_trade 提交时写 trade_log.jsonl (=
submit event) 并 fire cohort_tracker, 未成交/撤单也污染. Forward 已修
(commit 7b9d44235), 但历史数据 (36% legacy_unreconciled per audit) 无 authority.

本 module 提供**只读**的 fill event reader, 之后 cohort/position/NAV 都应
migrate 到从 fill_ledger 读, 而不是 trade_log. 保留 trade_log 作 audit
(它有决策上下文), 但 authoritative 数字必须来自 fills.

## Public API

- get_fills(ticker=None, since=None) → 所有 fill/partial 事件
- get_position(ticker) → 从 fill events 派生当前持仓 (cumulative dealt)
- get_cash_flow(since=None) → 从 fill events 派生现金流入/出
- summary_by_ticker(since=None) → 每 ticker 的 net qty + net cash

只读, 不写盘, 不改数据. cohort_tracker 未来可选择使用.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).parent
EXEC_LEDGER_PATH = SCRIPT_DIR / "signals" / "execution_ledger.jsonl"


def _load_ledger(path: Path = EXEC_LEDGER_PATH) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def get_fills(
    ticker: Optional[str] = None,
    since: Optional[str] = None,
    include_partial: bool = True,
) -> list[dict]:
    """Return fill/partial events from execution_ledger.

    Args:
      ticker: canonical (US.SOXL) or bare (SOXL); None = all tickers
      since: ISO ts; None = all history
      include_partial: True to include partial fills; False = filled only

    Each event: {event, order_id, ticker, side, dealt_qty, average_fill_price,
                  requested_qty, ts, broker_status, ...}
    """
    events = _load_ledger()
    kinds = {"filled", "partial"} if include_partial else {"filled"}
    out = []
    # normalize ticker to compare against both US.X and X
    norm_ticker = None
    if ticker:
        try:
            from instrument_registry import normalize
            norm_ticker = normalize(ticker)
        except Exception:
            norm_ticker = f"US.{ticker.upper()}" if not ticker.upper().startswith("US.") else ticker.upper()
    for ev in events:
        if ev.get("event") not in kinds:
            continue
        if norm_ticker and ev.get("ticker") != norm_ticker:
            # also try short form comparison for tolerance
            evt_ticker = ev.get("ticker", "")
            evt_stripped = evt_ticker.replace("US.", "")
            expect_stripped = norm_ticker.replace("US.", "")
            if evt_stripped != expect_stripped:
                continue
        if since and ev.get("ts", "") < since:
            continue
        out.append(ev)
    return out


def get_position(ticker: str, since: Optional[str] = None) -> dict:
    """Derive current position from time-ordered fill events (FIFO).

    Returns: {ticker, qty, avg_cost, total_bought, total_sold, n_fills, realized_pnl}

    R03 fix (2026-09-20 audit): avg_cost 是**当前持仓**成本, 不是历史平均.
    卖出释放对应比例成本; 清仓后重买 → avg_cost = 新买价 (不是历史 blended).
    实现: FIFO layers.

    避免用 last dealt_qty: broker 累计 dealt_qty 是当前总量, 不是增量. 同一 oid
    多个 partial 事件, 只取最后一次 dealt_qty (最终累计) 作 fill 总量.
    """
    fills = get_fills(ticker=ticker, since=since, include_partial=True)
    # 按 oid 取最后事件 (最终 dealt = 累计)
    final_by_oid: dict[str, dict] = {}
    for ev in fills:
        oid = str(ev.get("order_id") or "")
        if not oid:
            continue
        prev = final_by_oid.get(oid)
        if prev is None or ev.get("ts", "") > prev.get("ts", ""):
            final_by_oid[oid] = ev

    # 按 ts 排序, 时间序列处理 (FIFO)
    ordered = sorted(final_by_oid.values(), key=lambda e: e.get("ts", ""))

    # FIFO layers: 每层 {qty, price}
    layers: list[dict] = []
    total_bought_qty = 0.0
    total_sold_qty   = 0.0
    total_bought_cash = 0.0   # 累计买入现金 (informational)
    total_sold_cash   = 0.0
    realized_pnl = 0.0
    n_fills = 0

    for ev in ordered:
        side = (ev.get("side") or "").upper()
        dealt = float(ev.get("dealt_qty") or 0)
        avg = float(ev.get("average_fill_price") or 0)
        if dealt <= 0 or avg <= 0:
            continue
        n_fills += 1
        if side == "BUY":
            layers.append({"qty": dealt, "price": avg})
            total_bought_qty  += dealt
            total_bought_cash += dealt * avg
        elif side in ("SELL", "SELL_ALL", "REDUCE"):
            remaining = dealt
            total_sold_qty  += dealt
            total_sold_cash += dealt * avg
            while remaining > 0 and layers:
                layer = layers[0]
                take = min(remaining, layer["qty"])
                realized_pnl += take * (avg - layer["price"])
                layer["qty"] -= take
                remaining     -= take
                if layer["qty"] <= 1e-9:
                    layers.pop(0)
            # 剩余 remaining 是"短仓" (超卖)— 罕见, 记 realized_pnl 假 avg_cost=0
            if remaining > 0:
                realized_pnl += remaining * avg   # 视为 avg_cost=0 (空仓卖出)
                # 记一个负 layer 表示短仓
                layers.append({"qty": -remaining, "price": avg})

    # 当前持仓 qty + avg_cost 从 layers 计算
    current_qty = sum(l["qty"] for l in layers)
    current_cash = sum(l["qty"] * l["price"] for l in layers)
    if current_qty > 1e-9:
        avg_cost = current_cash / current_qty
    elif current_qty < -1e-9:
        # 净短仓
        avg_cost = current_cash / current_qty
    else:
        avg_cost = None   # 零持仓 → 无 avg_cost

    return {
        "ticker":         ticker,
        "qty":            int(current_qty) if abs(current_qty - round(current_qty)) < 1e-6 else round(current_qty, 4),
        "avg_cost":       round(avg_cost, 4) if avg_cost is not None else None,
        "total_bought":   int(total_bought_qty) if abs(total_bought_qty - round(total_bought_qty)) < 1e-6 else round(total_bought_qty, 4),
        "total_sold":     int(total_sold_qty) if abs(total_sold_qty - round(total_sold_qty)) < 1e-6 else round(total_sold_qty, 4),
        "realized_pnl":   round(realized_pnl, 2),
        "n_fills":        n_fills,
        "since":          since,
    }


def get_cash_flow(since: Optional[str] = None) -> dict:
    """Derive net cash flow from all fills across all tickers.

    Returns: {gross_out, gross_in, net_flow, n_buys, n_sells, n_fills_total}
    """
    fills = get_fills(since=since, include_partial=True)
    # 同样按 oid 聚合 (最终 dealt = 累计)
    final_by_oid: dict[str, dict] = {}
    for ev in fills:
        oid = str(ev.get("order_id") or "")
        if not oid:
            continue
        prev = final_by_oid.get(oid)
        if prev is None or ev.get("ts", "") > prev.get("ts", ""):
            final_by_oid[oid] = ev

    gross_out = 0.0
    gross_in = 0.0
    n_buys = 0
    n_sells = 0
    for oid, ev in final_by_oid.items():
        side = (ev.get("side") or "").upper()
        dealt = float(ev.get("dealt_qty") or 0)
        avg = float(ev.get("average_fill_price") or 0)
        if dealt <= 0 or avg <= 0:
            continue
        cash = dealt * avg
        if side == "BUY":
            gross_out += cash
            n_buys += 1
        elif side in ("SELL", "SELL_ALL", "REDUCE"):
            gross_in += cash
            n_sells += 1
    return {
        "gross_out":       round(gross_out, 2),
        "gross_in":        round(gross_in, 2),
        "net_flow":        round(gross_in - gross_out, 2),
        "n_buys":          n_buys,
        "n_sells":         n_sells,
        "n_fills_total":   len(final_by_oid),
        "since":           since,
    }


def summary_by_ticker(since: Optional[str] = None) -> dict[str, dict]:
    """Per-ticker net position + cash summary. Returns {ticker: {qty, avg_cost, ...}}."""
    fills = get_fills(since=since, include_partial=True)
    tickers = set()
    for ev in fills:
        tk = ev.get("ticker")
        if tk:
            tickers.add(tk)
    return {tk: get_position(tk, since=since) for tk in sorted(tickers)}
