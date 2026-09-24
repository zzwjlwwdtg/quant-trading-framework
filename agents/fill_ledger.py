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
# 拆股/合股记录 (人工维护, 每条须带 source). 格式:
#   {"actions": [{"ticker": "US.MULL", "type": "split", "ratio": 25,
#                 "effective": "2026-06-26T13:30:00+00:00", "source": "<url>"}]}
# ratio = 新股数 / 旧股数 (25:1 拆股 → 25; 1:20 合股 → 0.05).
CORPORATE_ACTIONS_PATH = SCRIPT_DIR / "signals" / "corporate_actions.json"
# 券商历史基线 (2026-09-24): 本地账本开始前/之外的券商成交, 由
# _broker_history_reconcile.py --import-baseline 生成. 每行带 origin=system|manual.
BROKER_HISTORY_PATH = SCRIPT_DIR / "signals" / "broker_history_fills.jsonl"


def _load_corporate_actions(path: Optional[Path] = None) -> list[dict]:
    path = path or CORPORATE_ACTIONS_PATH
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    acts = data.get("actions", []) if isinstance(data, dict) else data
    out = []
    for a in acts or []:
        try:
            if str(a.get("type", "split")).lower() != "split":
                continue
            r = float(a["ratio"])
            if r > 0 and a.get("ticker") and a.get("effective"):
                out.append({**a, "ratio": r})
        except Exception:
            continue
    return out


def apply_split(layers: list[dict], ratio: float) -> None:
    """持仓层按拆股比例换算: 股数 × ratio, 单价 ÷ ratio (成本总额不变)."""
    for layer in layers:
        layer["qty"] = layer["qty"] * ratio
        layer["price"] = layer["price"] / ratio


def _read_jsonl(path: Path) -> list[dict]:
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


def _load_ledger(path: Optional[Path] = None,
                 include_broker_history: bool = True) -> list[dict]:
    """读取成交事实.

    path=None (默认): execution_ledger + 券商历史基线 (BROKER_HISTORY_PATH) 中
    本地账本没有的订单 (2026-09-24). 同一 order_id 以本地账本为准.
    execution_ledger 本身不被修改 (hash 链不动).
    显式传 path → 只读该文件, 不合并.
    """
    if path is not None:
        return _read_jsonl(path)
    rows = _read_jsonl(EXEC_LEDGER_PATH)
    if not include_broker_history:
        return rows
    local_oids = {str(r.get("order_id")) for r in rows
                  if r.get("event") in ("filled", "partial")}
    for r in _read_jsonl(BROKER_HISTORY_PATH):
        if r.get("event") in ("filled", "partial") and str(r.get("order_id")) not in local_oids:
            rows.append(r)
    return rows


def canonical_ticker(ticker: Optional[str]) -> str:
    """Instrument identity used for grouping fills (audit v5 遗留项, 2026-09-24).

    与 instrument_registry.normalize 同一规则: 无市场前缀 → US.; 已有
    US./HK./JP. 前缀保留. 不做"删除所有市场前缀"式合并 (HK.X ≠ US.X).
    """
    t = (ticker or "").strip()
    if not t:
        return ""
    try:
        from instrument_registry import normalize
        return normalize(t)
    except Exception:
        u = t.upper()
        return u if u.startswith(("US.", "HK.", "JP.")) else f"US.{u}"


def fill_increments(events: list[dict],
                    corporate_actions: Optional[list[dict]] = None) -> tuple[list[dict], dict]:
    """Shared reducer: broker 累计回报 → 按时间排序的成交增量.

    get_position / stats_from_fills / summary_by_ticker 共用 (F1 followup:
    "统计与持仓共用 reducer, 不要另写一份").

    输入: filled/partial 事件 (dealt_qty / average_fill_price 为订单累计值).
    输出: (increments, meta)
      increments: [{ts, order_id, ticker(canonical), side, qty, price}], ts 升序
      meta: {n_valid_events, price_revisions, qty_reversals}

    回报修订政策 (明确, 不静默):
    - 同一订单同一累计数量出现新的累计均价 → 视为券商价格修订, 以最后一次
      报价为准, 回溯覆盖该成交 (时间仍按首次回报). price_revisions 计数.
    - 累计数量倒退 (撤销/bust) → 不自动冲回, qty_reversals 计数; 调用方
      必须据此降级权威标签.
    """
    ordered = sorted(
        (ev for ev in events
         if float(ev.get("dealt_qty") or 0) > 0
         and float(ev.get("average_fill_price") or 0) > 0),
        key=lambda e: e.get("ts", ""))
    # pass 1: 每 (oid, 累计数量) 的最终均价 → 修订生效
    final_avg: dict[tuple[str, float], float] = {}
    first_avg: dict[tuple[str, float], float] = {}
    for ev in ordered:
        key = (str(ev.get("order_id") or ""), round(float(ev["dealt_qty"]), 8))
        avg = float(ev["average_fill_price"])
        first_avg.setdefault(key, avg)
        final_avg[key] = avg
    price_revisions = sum(1 for k in final_avg
                          if abs(final_avg[k] - first_avg[k]) > 1e-9)
    # pass 2: 时间顺序差分
    prev_dealt: dict[str, float] = {}
    increments: list[dict] = []
    qty_reversals = 0
    for ev in ordered:
        oid = str(ev.get("order_id") or "")
        cum = float(ev["dealt_qty"])
        prev = prev_dealt.get(oid, 0.0)
        delta_qty = cum - prev
        if delta_qty < -1e-9:
            qty_reversals += 1
            continue
        if delta_qty <= 1e-9:
            continue   # 重复回报或纯价格修订 (已由 final_avg 处理)
        cum_cash = cum * final_avg[(oid, round(cum, 8))]
        prev_cash = prev * final_avg[(oid, round(prev, 8))] if prev > 0 else 0.0
        delta_cash = cum_cash - prev_cash
        price = (delta_cash / delta_qty) if delta_cash > 0 else final_avg[(oid, round(cum, 8))]
        prev_dealt[oid] = cum
        origin = ev.get("origin") or (
            "unknown" if ev.get("source") == "broker_history" else "system")
        increments.append({
            "ts": ev.get("ts", ""), "order_id": oid,
            "ticker": canonical_ticker(ev.get("ticker")),
            "side": (ev.get("side") or "").upper(),
            "qty": delta_qty, "price": price, "origin": origin,
        })
    # 拆股/合股 (2026-09-24): 在生效时点插入 SPLIT 标记, 由 FIFO 消费方换算持仓层.
    # 只为本批事件涉及的标的插入. 零碎股现金补偿不建模.
    if corporate_actions is None:
        corporate_actions = _load_corporate_actions()
    present = {inc["ticker"] for inc in increments}
    markers = []
    for a in corporate_actions or []:
        tk = canonical_ticker(a.get("ticker"))
        if tk in present:
            markers.append({"ts": str(a["effective"]), "order_id": "", "ticker": tk,
                            "side": "SPLIT", "qty": 0.0, "price": 0.0,
                            "ratio": float(a["ratio"])})
    if markers:
        increments = sorted(increments + markers, key=lambda x: x["ts"])
    return increments, {"n_valid_events": len(ordered),
                        "price_revisions": price_revisions,
                        "qty_reversals": qty_reversals,
                        "splits_applied": len(markers)}


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
    norm_ticker = canonical_ticker(ticker) if ticker else None
    for ev in events:
        if ev.get("event") not in kinds:
            continue
        if norm_ticker and canonical_ticker(ev.get("ticker")) != norm_ticker:
            continue
        if since and ev.get("ts", "") < since:
            continue
        out.append(ev)
    return out


def get_position(ticker: str, since: Optional[str] = None) -> dict:
    """Derive current position from time-ordered fill events (FIFO).

    Returns: {ticker, qty, avg_cost, total_bought, total_sold, n_fills,
              realized_pnl, unreconciled_sells}

    R03 fix (2026-09-20 audit): avg_cost 是**当前持仓**成本, 不是历史平均.
    卖出释放对应比例成本; 清仓后重买 → avg_cost = 新买价 (不是历史 blended).

    R03 followup fix (2026-09-20 followup audit): 之前把 oid 压成"最后 event"
    再按 ts 排序 → 交错成交丢失时序. 复现: Buy oid B (5@100 at 14:00),
    Sell oid S (5@110 at 14:01), Buy oid B (+5@120 at 14:02, broker 累计
    10@110). 旧代码返 avg_cost=110/realized=550, 正确应 avg_cost=120/realized=50.
    Fix: 按 ts 排全部 event, 用 per-oid running dealt 计算 event 增量, 走 FIFO.

    Unreconciled sells (无 buy 覆盖) 记 unreconciled_sells, 不当零成本盈利.
    """
    fills = get_fills(ticker=ticker, since=since, include_partial=True)
    # 2026-09-24: 共用 fill_increments reducer (时序差分 + 修订 + canonical ticker)
    increments, meta = fill_increments(fills)
    n_events = meta["n_valid_events"]

    layers: list[dict] = []
    total_bought_qty  = 0.0
    total_sold_qty    = 0.0
    realized_pnl = 0.0
    unreconciled_sells = 0.0   # sells 无 buy 覆盖 → 无法对账

    for inc in increments:
        side, delta_qty, delta_price = inc["side"], inc["qty"], inc["price"]
        if side == "SPLIT":
            apply_split(layers, inc["ratio"])
            continue
        if side == "BUY":
            layers.append({"qty": delta_qty, "price": delta_price})
            total_bought_qty += delta_qty
        elif side in ("SELL", "SELL_ALL", "REDUCE"):
            remaining = delta_qty
            total_sold_qty += delta_qty
            while remaining > 0 and layers and layers[0]["qty"] > 0:
                layer = layers[0]
                take = min(remaining, layer["qty"])
                realized_pnl += take * (delta_price - layer["price"])
                layer["qty"] -= take
                remaining     -= take
                if layer["qty"] <= 1e-9:
                    layers.pop(0)
            # R03 followup: 无 buy 覆盖的卖出 → unreconciled, 不算盈利
            if remaining > 0:
                unreconciled_sells += remaining

    current_qty = sum(l["qty"] for l in layers if l["qty"] > 0)
    current_cash = sum(l["qty"] * l["price"] for l in layers if l["qty"] > 0)
    avg_cost = (current_cash / current_qty) if current_qty > 1e-9 else None

    return {
        "ticker":              ticker,
        "qty":                 int(current_qty) if abs(current_qty - round(current_qty)) < 1e-6 else round(current_qty, 4),
        "avg_cost":            round(avg_cost, 4) if avg_cost is not None else None,
        "total_bought":        int(total_bought_qty) if abs(total_bought_qty - round(total_bought_qty)) < 1e-6 else round(total_bought_qty, 4),
        "total_sold":          int(total_sold_qty) if abs(total_sold_qty - round(total_sold_qty)) < 1e-6 else round(total_sold_qty, 4),
        "realized_pnl":        round(realized_pnl, 2),
        "unreconciled_sells":  int(unreconciled_sells) if abs(unreconciled_sells - round(unreconciled_sells)) < 1e-6 else round(unreconciled_sells, 4),
        "n_events":            n_events,
        "n_fills":             n_events,   # backwards-compat name
        "price_revisions":     meta["price_revisions"],
        "qty_reversals":       meta["qty_reversals"],
        "splits_applied":      meta["splits_applied"],
        "since":               since,
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
        tk = canonical_ticker(ev.get("ticker"))
        if tk:
            tickers.add(tk)
    return {tk: get_position(tk, since=since) for tk in sorted(tickers)}
