"""Pre-trade liquidity estimates and post-trade execution-quality records."""
from __future__ import annotations

import json
import hashlib
import math
from datetime import datetime, timezone
from pathlib import Path


LIQUID_ETFS = {"TQQQ", "SOXL", "QQQ", "SOXX", "SPY", "GLD", "SHY", "IEI"}
HIGH_VOL_ETFS = {"TQQQ", "SOXL", "MULL", "DRAM"}


def _ticker(value: str) -> str:
    return str(value or "").upper().replace("US.", "")


def _num(value, default: float | None = 0.0) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def estimate_execution(
    ticker: str,
    side: str,
    qty: int,
    reference_price: float,
    *,
    bid: float | None = None,
    ask: float | None = None,
    avg_daily_volume: float | None = None,
    annual_volatility: float | None = None,
    outside_rth: bool = False,
    resting_limit: bool = False,
    small_account: bool = True,
) -> dict:
    """Estimate spread, impact, capacity, fill probability and shortfall.

    Values are conservative estimates, not synthetic alpha.  Actual fills are
    reconciled separately from the broker order feed.
    """
    tk = _ticker(ticker)
    direction = 1.0 if str(side).upper().startswith("BUY") else -1.0
    ref = float(reference_price)
    qty = max(0, int(qty))
    safe_bid = _num(bid, None)
    safe_ask = _num(ask, None)
    quote_valid = bool(
        safe_bid is not None and safe_ask is not None
        and safe_bid > 0 and safe_ask >= safe_bid
    )
    if small_account:
        # The account is intentionally small relative to displayed liquidity.
        # Do not invent spread/impact penalties; reconcile actual broker fills.
        midpoint = ref
        spread_bps = 0.0
        spread_source = "small_account_ignored"
    elif quote_valid:
        midpoint = (safe_bid + safe_ask) / 2.0
        spread_bps = (safe_ask - safe_bid) / midpoint * 10_000 if midpoint else 0.0
        spread_source = "quote"
    else:
        midpoint = ref
        fallback = 5.0 if tk in LIQUID_ETFS else 15.0
        if tk in HIGH_VOL_ETFS:
            fallback += 4.0
        spread_bps = fallback * (2.5 if outside_rth else 1.0)
        spread_source = "conservative_fallback"

    adv = _num(avg_daily_volume, 0.0) or 0.0
    participation = qty / adv if adv > 0 else None
    vol = _num(annual_volatility, None)
    if vol is not None and vol > 2.0:
        vol /= 100.0
    vol = vol if vol is not None and vol > 0 else (0.70 if tk in HIGH_VOL_ETFS else 0.35)
    impact_bps = 0.0 if small_account else 1.5 + 45.0 * math.sqrt(max(0.0, participation or 0.0)) * max(0.5, vol / 0.35)
    if outside_rth and not small_account:
        impact_bps *= 1.8
    half_spread_bps = spread_bps / 2.0
    shortfall_bps = half_spread_bps + impact_bps
    expected_fill = midpoint * (1.0 + direction * shortfall_bps / 10_000.0)

    participation_cap = 0.005 if outside_rth else 0.02
    suggested_max_qty = int(adv * participation_cap) if adv > 0 else None
    capacity_status = "ok_small_account" if small_account else "unknown"
    if participation is not None and not small_account:
        capacity_status = "ok" if participation <= participation_cap else "too_large"
    fill_probability = 1.0 if small_account else 0.96
    if resting_limit:
        fill_probability -= 0.28
    if outside_rth and not small_account:
        fill_probability -= 0.18
    if participation is not None and not small_account:
        fill_probability -= min(0.45, participation / max(participation_cap, 1e-9) * 0.20)
    fill_probability = max(0.05, min(0.99, fill_probability))
    modeled_fill_qty = int(round(qty * fill_probability))

    return {
        "schema_version": 1,
        "ticker": tk,
        "side": "BUY" if direction > 0 else "SELL",
        "requested_qty": qty,
        "reference_price": ref,
        "midpoint": midpoint,
        "spread_bps": spread_bps,
        "spread_source": spread_source,
        "impact_bps": impact_bps,
        "expected_shortfall_bps": shortfall_bps,
        "expected_fill_price": expected_fill,
        "fill_probability": fill_probability,
        "modeled_fill_qty": modeled_fill_qty,
        "avg_daily_volume": adv or None,
        "participation_pct_adv": participation * 100 if participation is not None else None,
        "participation_cap_pct": participation_cap * 100,
        "suggested_max_qty": suggested_max_qty,
        "capacity_status": capacity_status,
        "outside_rth": bool(outside_rth),
        "resting_limit": bool(resting_limit),
        "small_account_assumption": bool(small_account),
    }


def actual_execution_quality(
    *,
    side: str,
    requested_qty: int,
    dealt_qty: float,
    reference_price: float,
    average_fill_price: float,
) -> dict:
    requested = max(0.0, float(requested_qty))
    dealt = max(0.0, float(dealt_qty))
    reference = float(reference_price)
    fill = float(average_fill_price)
    direction = 1.0 if str(side).upper().startswith("BUY") else -1.0
    fill_ratio = dealt / requested if requested else 0.0
    shortfall_bps = direction * (fill - reference) / reference * 10_000 if reference > 0 and fill > 0 else None
    return {
        "fill_ratio": fill_ratio,
        "unfilled_qty": max(0.0, requested - dealt),
        "implementation_shortfall_bps": shortfall_bps,
        "implementation_shortfall_usd": (
            direction * (fill - reference) * dealt if reference > 0 and fill > 0 else None
        ),
    }


def broker_time_to_utc_iso(s) -> str:
    """moomoo 美股订单时间 (美东, 无时区, 如 "2026-10-16 12:02:39.123") → UTC ISO. 解析不了返回 "".

    与 _broker_history_reconcile.broker_time_to_utc_iso 同一口径 (2026-10-02 已按券商历史核对).
    """
    from datetime import timedelta
    s = str(s or "").strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    else:
        return ""
    try:
        from zoneinfo import ZoneInfo
        off = naive.replace(tzinfo=ZoneInfo("America/New_York")).utcoffset()
    except Exception:
        y = naive.year
        mar = datetime(y, 3, 8) + timedelta(days=(6 - datetime(y, 3, 8).weekday()) % 7)
        nov = datetime(y, 11, 1) + timedelta(days=(6 - datetime(y, 11, 1).weekday()) % 7)
        off = timedelta(hours=-4 if mar.replace(hour=2) <= naive < nov.replace(hour=2) else -5)
    return (naive - off).replace(tzinfo=timezone.utc).isoformat()


def append_execution_event(path: Path, event: dict) -> None:
    """Append a hash-chained event. 加跨进程锁保证 read-then-append 原子性,
    防止并发 paper_trader 让 hash chain 分叉 (verify_execution_log 会误报篡改)."""
    from atomic_io import with_file_lock
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(event)
    payload.setdefault("ts", datetime.now(timezone.utc).isoformat())

    def _read_and_append():
        previous_hash = ""
        if path.exists():
            try:
                with open(path, "rb") as handle:
                    lines = [line for line in handle.read().splitlines() if line.strip()]
                if lines:
                    previous_hash = str(json.loads(lines[-1].decode("utf-8")).get("record_hash") or "")
            except Exception:
                previous_hash = ""
        payload["previous_hash"] = previous_hash
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload["record_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    with_file_lock(path, _read_and_append)


def verify_execution_log(path: Path) -> dict:
    if not path.exists():
        return {"valid": True, "checked": 0, "legacy_unhashed": 0}
    previous = ""
    checked = 0
    legacy = 0
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            try:
                payload = json.loads(line)
            except Exception:
                return {"valid": False, "checked": checked, "broken_line": line_no, "reason": "invalid_json"}
            record_hash = payload.pop("record_hash", None)
            if not record_hash:
                legacy += 1
                continue
            if payload.get("previous_hash", "") != previous:
                return {"valid": False, "checked": checked, "broken_line": line_no, "reason": "broken_chain"}
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if expected != record_hash:
                return {"valid": False, "checked": checked, "broken_line": line_no, "reason": "hash_mismatch"}
            previous = record_hash
            checked += 1
    return {"valid": True, "checked": checked, "legacy_unhashed": legacy, "last_hash": previous or None}


# EVO-20261021-2：只读测量范围；不接入下单、重试或止损判断。
CHANGE_DESCRIPTION = (
    "止损成交成本分解与复盘覆盖校验；合并全部历史 TRAILING_STOP "
    "窗口检查偏离统计。只测量，不改订单类型、重试链路与止损逻辑；"
    "超卖仅作分组标签，缺少同期报价或完整窗口证据时明确标记未知。"
)


def evidence_time(value):
    """仅接受带时区的时间；不把对账时间或无时区文本猜成成交时间。"""
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def sell_cost_breakdown(evidence: dict) -> dict:
    """卖出成本为正表示不利偏离；各项共用同一数量，互不重复计量。

    同期报价必须显式包含 bid、ask、行情时间 ts、可得时间 available_ts。
    保守要求行情时间等于事件时间；历史日线参考价、限价不能替代报价。
    """
    anomalies = list(evidence.get("anomalies") or [])
    missing = []
    times = {key: evidence_time(evidence.get(key)) for key in
             ("trigger_ts", "submitted_ts", "confirmed_ts", "first_fill_ts", "last_fill_ts")}
    for key, value in times.items():
        if value is None:
            missing.append(key)
    pairs = (
        ("trigger_ts", "submitted_ts"),
        ("submitted_ts", "confirmed_ts"),
        ("submitted_ts", "first_fill_ts"),
        ("confirmed_ts", "first_fill_ts"),
        ("first_fill_ts", "last_fill_ts"),
    )
    for left, right in pairs:
        if times[left] is not None and times[right] is not None and times[left] > times[right]:
            anomalies.append(f"time_order:{left}>{right}")

    def bid_at(name, event_time):
        quote = evidence.get(name)
        if not isinstance(quote, dict) or event_time is None:
            missing.append(name)
            return None
        bid, ask = _num(quote.get("bid"), None), _num(quote.get("ask"), None)
        quote_time = evidence_time(quote.get("ts"))
        available = evidence_time(quote.get("available_ts"))
        if (bid is None or ask is None or bid <= 0 or ask < bid
                or quote_time != event_time or available is None or available > event_time):
            missing.append(f"{name}:无同期可得买卖报价")
            return None
        return bid

    trigger_bid = bid_at("trigger_quote", times["trigger_ts"])
    submit_bid = bid_at("submit_quote", times["submitted_ts"])
    qty = _num(evidence.get("qty"), None)
    fill = _num(evidence.get("price"), None)
    stop = _num(evidence.get("stop_line"), None)
    fee = _num(evidence.get("fee"), None)
    if qty is None or qty <= 0:
        missing.append("qty")
    if fill is None or fill <= 0:
        missing.append("price")
    if stop is None or stop <= 0:
        stop = None
        missing.append("stop_line")
    if fee is None:
        missing.append("fee")

    # 只有相邻基准都有证据才计算；止损线至触发价不是纯粹的开盘跳空。
    usable = not anomalies and qty is not None and qty > 0 and fill is not None and fill > 0
    components = {
        "stop_to_trigger_usd": (
            (stop - trigger_bid) * qty
            if usable and stop is not None and trigger_bid is not None else None),
        "trigger_to_submit_usd": (
            (trigger_bid - submit_bid) * qty
            if usable and trigger_bid is not None and submit_bid is not None else None),
        "submit_to_fill_usd": (
            (submit_bid - fill) * qty
            if usable and submit_bid is not None else None),
        "fees_usd": fee,
    }
    complete = usable and all(value is not None for value in components.values())
    total = sum(components.values()) if complete else None
    expected = (stop - fill) * qty + fee if complete else None
    if complete and not math.isclose(total, expected, rel_tol=1e-10, abs_tol=1e-8):
        anomalies.append("cost_components_do_not_reconcile")
    return {
        "order_id": evidence.get("order_id"),
        "status": "invalid" if anomalies else ("complete" if complete else "unknown"),
        "components": components,
        "total_cost_usd": total,
        "execution_slippage_bps": (
            (submit_bid - fill) / submit_bid * 10_000
            if usable and submit_bid is not None else None),
        "submission_delay_seconds": (
            (times["submitted_ts"] - times["trigger_ts"]).total_seconds()
            if not anomalies and times["trigger_ts"] and times["submitted_ts"] else None),
        "gap_usd": None,
        "gap_reason": "止损线至触发报价含观察间隔内市场移动，不能直接认定为跳空",
        "missing": sorted(set(missing)),
        "anomalies": sorted(set(anomalies)),
        "quotes": {name: evidence.get(name) for name in ("trigger_quote", "submit_quote")},
        "times": {name: evidence.get(name) for name in times},
        "fills": evidence.get("fills", []),
    }


def trailing_stop_measurement(evidence: dict) -> dict:
    """窗口信息只消费显式历史证据，不臆造交易窗口或盘中价格路径。

    离散样本只能给出最早观察到跌穿的时间，不能证明真正首次跌穿时间。
    窗口损失是反事实，止损线与成交价之差不直接归因于窗口检查。
    """
    stop = _num(evidence.get("stop_line"), None)
    price = _num(evidence.get("price"), None)
    qty = _num(evidence.get("qty"), None)
    start = evidence_time(evidence.get("window_start_ts"))
    previous_end = evidence_time(evidence.get("previous_window_end_ts"))
    trigger = evidence_time(evidence.get("trigger_ts"))
    observations = []
    for row in evidence.get("between_window_quotes") or []:
        if not isinstance(row, dict):
            continue
        ts = evidence_time(row.get("ts"))
        available = evidence_time(row.get("available_ts"))
        bid, ask = _num(row.get("bid"), None), _num(row.get("ask"), None)
        if (previous_end and start and previous_end < start and ts and available
                and previous_end < ts < start and available <= ts
                and bid is not None and ask is not None and 0 < bid <= ask
                and stop is not None and stop > 0 and bid < stop):
            observations.append(ts)
    valid = stop is not None and stop > 0 and price is not None and price > 0
    return {
        "order_id": evidence.get("order_id"),
        "stop_line": stop,
        "fill_price": price,
        "deviation_bps": (stop - price) / stop * 10_000 if valid else None,
        "stop_to_fill_usd": (
            (stop - price) * qty if valid and qty is not None and qty > 0 else None),
        "minutes_from_window_start": (
            (trigger - start).total_seconds() / 60
            if start and trigger and trigger >= start else None),
        "first_observed_breach_ts": min(observations).isoformat() if observations else None,
        "earliest_breach_ts": None,
        "window_extra_loss_usd": None,
        "status": "unknown",
        "reason": "缺少连续行情覆盖及可执行反事实；离散观察与止损线偏离不等于窗口额外损失",
    }


def summarize_execution_log(path: Path, limit: int = 500) -> dict:
    if not path.exists():
        return {"orders": 0, "filled_orders": 0, "avg_fill_ratio": None, "avg_shortfall_bps": None,
                "audit_integrity": verify_execution_log(path)}
    events = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    events = events[-limit:]
    final = [e for e in events if e.get("event") in {"filled", "partial", "cancelled", "modeled"}]
    ratios = [float(e["quality"]["fill_ratio"]) for e in final if (e.get("quality") or {}).get("fill_ratio") is not None]
    costs = [float(e["quality"]["implementation_shortfall_bps"]) for e in final if (e.get("quality") or {}).get("implementation_shortfall_bps") is not None]
    return {
        "orders": len({str(e.get("order_id")) for e in events if e.get("order_id")}),
        "filled_orders": sum(1 for e in final if e.get("event") == "filled"),
        "partial_orders": sum(1 for e in final if e.get("event") == "partial"),
        "avg_fill_ratio": sum(ratios) / len(ratios) if ratios else None,
        "avg_shortfall_bps": sum(costs) / len(costs) if costs else None,
        "recent": final[-20:],
        "audit_integrity": verify_execution_log(path),
    }
