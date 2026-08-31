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
