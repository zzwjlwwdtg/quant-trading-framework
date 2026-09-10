"""Small-universe option-flow monitor for the simulation portfolio.

This module deliberately separates three concepts that used to be mixed in the
dashboard:

* option walls / GEX describe *positioning structure*;
* intraday volume deltas describe *new activity*;
* next-session open-interest changes confirm whether activity became new risk.

The bundled data sources expose option-chain snapshots, not an exchange trade
tape.  Therefore quote-side classification is explicitly marked as heuristic
and snapshot-only signals are capped at 69/100.  A future tape adapter can pass
``data_quality="tape"`` and supply authoritative aggressor/complex-order fields
without changing the consumer contract.
"""
from __future__ import annotations

import json
import logging
import math
import os
import statistics
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from atomic_io import atomic_write_json
from config import SIGNALS_DIR


logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

SCHEMA_VERSION = 1
REFRESH_SEC = max(300, int(os.environ.get("OPTIONS_FLOW_REFRESH_SEC", "1800")))
MAX_DTE = max(120, int(os.environ.get("OPTIONS_FLOW_MAX_DTE", "540")))
MAX_EXPIRIES_PER_SOURCE = max(
    5, int(os.environ.get("OPTIONS_FLOW_MAX_EXPIRIES", "18"))
)
SIGNAL_MIN_SCORE = 40
ACTION_SCORE = 60
STRONG_SCORE = 75
SNAPSHOT_SCORE_CAP = 69
SIGNAL_PERSIST_SEC = 2 * 3600

FLOW_DIR = Path(SIGNALS_DIR) / "options_flow"
STATE_PATH = FLOW_DIR / "state.json"
LATEST_PATH = FLOW_DIR / "latest.json"

# We read the liquid unlevered proxy and translate only the *direction/risk*,
# never a proxy strike, into the portfolio ticker.
#
# 2026-09-09 扩展: 加 6 只 TRACKED_TICKERS 覆盖 (MSFT/GOOGL/NBIS/LITE/NVDA/AAPL/TSLA)
# 因为 decision_agent _apply_options_flow_guard 齐全但空跑, 用户持仓 LITE 等
# 无 option flow guard 保护. 每加 1 只 = +1 source × 18 expiries 一次拉取,
# 6 只增加约 108 chain requests / 30min refresh, yfinance 应能承载.
POSITION_PROXY_MAP: dict[str, list[dict[str, Any]]] = {
    # === 杠杆 ETF + GLD (原有, 通过 proxy 覆盖) ===
    "TQQQ": [
        {"source": "QQQ", "weight": 1.0, "role": "Nasdaq-100 primary"},
    ],
    "SOXL": [
        {"source": "SMH", "weight": 1.0, "role": "liquid semiconductor primary"},
        {"source": "SOXX", "weight": 0.85, "role": "index-matched confirmation"},
    ],
    "DRAM": [
        {"source": "MU", "weight": 1.0, "role": "memory bellwether"},
        {"source": "SMH", "weight": 0.35, "role": "sector confirmation"},
    ],
    "MULL": [
        {"source": "MU", "weight": 1.0, "role": "single-stock underlying"},
    ],
    "GLD": [
        {"source": "GLD", "weight": 1.0, "role": "direct underlying"},
    ],
    # === 单股直接 (新增 2026-09-09) ===
    "MSFT": [
        {"source": "MSFT", "weight": 1.0, "role": "direct underlying"},
    ],
    "GOOGL": [
        {"source": "GOOGL", "weight": 1.0, "role": "direct underlying"},
    ],
    "AAPL": [
        {"source": "AAPL", "weight": 1.0, "role": "direct underlying"},
    ],
    "NVDA": [
        {"source": "NVDA", "weight": 1.0, "role": "direct underlying"},
    ],
    "TSLA": [
        {"source": "TSLA", "weight": 1.0, "role": "direct underlying"},
    ],
    "LITE": [
        {"source": "LITE", "weight": 1.0, "role": "direct underlying"},
    ],
    "NBIS": [
        {"source": "NBIS", "weight": 1.0, "role": "direct underlying"},
        {"source": "SMH", "weight": 0.30, "role": "AI cloud sector confirmation"},
    ],
    "KLAC": [
        {"source": "KLAC", "weight": 1.0, "role": "direct underlying"},
    ],
    "AMAT": [
        {"source": "AMAT", "weight": 1.0, "role": "direct underlying"},
    ],
}

MONITORED_SOURCES = tuple(dict.fromkeys(
    proxy["source"]
    for proxies in POSITION_PROXY_MAP.values()
    for proxy in proxies
))

_REFRESH_LOCK = threading.Lock()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _iso(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat()


def _et_day(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ET).date().isoformat()


def _dte_bucket(dte: int) -> str:
    if dte <= 2:
        return "0-2d_gamma"
    if dte <= 14:
        return "3-14d_event"
    if dte <= 45:
        return "15-45d_swing"
    if dte <= 120:
        return "46-120d_institutional"
    return "121-540d_strategic"


def _is_monthly(expiry: str) -> bool:
    try:
        d = datetime.strptime(expiry, "%Y-%m-%d").date()
    except ValueError:
        return False
    return d.weekday() == 4 and 15 <= d.day <= 21


def select_expiries(
    expiries: Iterable[str],
    *,
    now: datetime | None = None,
    max_expiries: int = MAX_EXPIRIES_PER_SOURCE,
) -> list[str]:
    """Cover every DTE regime while prioritising liquid monthly contracts.

    Weekly contracts are dense near spot and expensive to query.  We keep the
    nearest four <=14 DTE expiries, all standard monthlies, and at least one
    representative expiry in each remaining bucket.  The returned coverage is
    included in the result so the dashboard never implies an unscanned expiry
    was checked.
    """
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ET).date()
    parsed: list[tuple[str, int]] = []
    for expiry in expiries:
        try:
            dte = (datetime.strptime(str(expiry), "%Y-%m-%d").date() - today).days
        except ValueError:
            continue
        if 0 <= dte <= MAX_DTE:
            parsed.append((str(expiry), dte))
    parsed.sort(key=lambda item: item[1])
    if not parsed:
        return []

    chosen: set[str] = {expiry for expiry, dte in parsed if dte <= 14}
    # Near weeklies can be numerous; the first four are enough for gamma/event flow.
    if len(chosen) > 4:
        chosen = {expiry for expiry, _ in parsed[:4]}
    chosen.update(expiry for expiry, _ in parsed if _is_monthly(expiry))

    for bucket in (
        "15-45d_swing",
        "46-120d_institutional",
        "121-540d_strategic",
    ):
        candidates = [(expiry, dte) for expiry, dte in parsed if _dte_bucket(dte) == bucket]
        if candidates:
            chosen.add(candidates[0][0])
            # Also retain the far edge; this avoids missing a large quarterly/LEAPS line.
            chosen.add(candidates[-1][0])

    ordered = [expiry for expiry, _ in parsed if expiry in chosen]
    if len(ordered) <= max_expiries:
        return ordered

    # Preserve one expiry per regime first, then fill by monthly/near-date priority.
    must_keep: list[str] = []
    for bucket in (
        "0-2d_gamma", "3-14d_event", "15-45d_swing",
        "46-120d_institutional", "121-540d_strategic",
    ):
        candidate = next(
            (expiry for expiry, dte in parsed
             if expiry in chosen and _dte_bucket(dte) == bucket),
            None,
        )
        if candidate:
            must_keep.append(candidate)
    ranked = sorted(
        ordered,
        key=lambda expiry: (
            0 if expiry in must_keep else 1,
            0 if _is_monthly(expiry) else 1,
            next(dte for item, dte in parsed if item == expiry),
        ),
    )
    selected = set(ranked[:max_expiries])
    return [expiry for expiry, _ in parsed if expiry in selected]


def _contract_key(source: str, expiry: str, option_type: str, strike: float) -> str:
    return f"{source}|{expiry}|{option_type}|{strike:.4f}"


def _mid_and_spread(row: dict[str, Any]) -> tuple[float, float | None]:
    bid = _safe_float(row.get("bid"))
    ask = _safe_float(row.get("ask"))
    last = _safe_float(row.get("lastPrice") or row.get("last"))
    if bid > 0 and ask >= bid:
        mid = (bid + ask) / 2
        spread_pct = ((ask - bid) / mid) if mid > 0 else None
        return (last if last > 0 else mid), spread_pct
    return last, None


def _aggressor_heuristic(row: dict[str, Any]) -> tuple[str, float, str]:
    """Infer quote side from a snapshot last price, with deliberately low trust."""
    bid = _safe_float(row.get("bid"))
    ask = _safe_float(row.get("ask"))
    last = _safe_float(row.get("lastPrice") or row.get("last"))
    if bid <= 0 or ask <= bid or last <= 0:
        return "unknown", 0.0, "snapshot_no_nbbo"
    position = (last - bid) / (ask - bid)
    if position >= 0.75:
        return "buy", min(1.0, position), "snapshot_last_near_ask"
    if position <= 0.25:
        return "sell", min(1.0, 1.0 - position), "snapshot_last_near_bid"
    return "unknown", 0.0, "snapshot_last_midmarket"


def _direction(option_type: str, aggressor: str) -> str:
    if aggressor == "buy":
        return "bullish" if option_type == "call" else "bearish"
    if aggressor == "sell":
        return "bearish" if option_type == "call" else "bullish"
    return "unknown"


def _log_points(value: float, low: float, high: float, max_points: int) -> int:
    if value <= low:
        return 0
    if value >= high:
        return max_points
    ratio = math.log(value / low) / math.log(high / low)
    return int(round(max_points * max(0.0, min(1.0, ratio))))


def _zscore(value: float, history: list[float]) -> float | None:
    clean = [float(v) for v in history[-20:] if _safe_float(v) > 0]
    if len(clean) < 4:
        return None
    std = statistics.pstdev(clean)
    if std <= 0:
        return 5.0 if value > clean[-1] else 0.0
    return (value - statistics.mean(clean)) / std


def _novelty_points(volume_delta: int, oi: int, zscore: float | None) -> tuple[int, float | None]:
    ratio = volume_delta / oi if oi > 0 else None
    if ratio is None:
        ratio_points = 6 if volume_delta >= 300 else 2
    elif ratio >= 1.0:
        ratio_points = 10
    elif ratio >= 0.5:
        ratio_points = 8
    elif ratio >= 0.25:
        ratio_points = 5
    elif ratio >= 0.10:
        ratio_points = 2
    else:
        ratio_points = 0

    if zscore is None:
        # On the first run there is no intraday history yet.  A very large
        # absolute burst still deserves a watch-level score, but not the full
        # z-score credit that requires a baseline.
        z_points = 5 if volume_delta >= 2500 else (2 if volume_delta >= 1000 else 0)
    elif zscore >= 5:
        z_points = 10
    elif zscore >= 3:
        z_points = 8
    elif zscore >= 2:
        z_points = 5
    elif zscore >= 1:
        z_points = 2
    else:
        z_points = 0
    return min(20, ratio_points + z_points), ratio


def _row_dicts(frame_or_rows: Any) -> list[dict[str, Any]]:
    if frame_or_rows is None:
        return []
    if isinstance(frame_or_rows, list):
        return [dict(row) for row in frame_or_rows if isinstance(row, dict)]
    try:
        return [dict(row) for row in frame_or_rows.to_dict("records")]
    except Exception:
        return []


def _normalise_iv(value: Any) -> float | None:
    iv = _safe_float(value)
    if iv <= 0:
        return None
    return iv / 100.0 if iv > 3 else iv


def _approx_abs_delta(
    spot: float,
    strike: float,
    dte: int,
    iv: float | None,
    option_type: str,
) -> float | None:
    """Black-Scholes delta fallback for snapshot feeds without Greeks."""
    if spot <= 0 or strike <= 0 or not iv or iv <= 0:
        return None
    years = max(1.0 / 365.0, dte / 365.0)
    sigma_t = iv * math.sqrt(years)
    if sigma_t <= 0:
        return None
    # A fixed short-rate approximation is sufficient for exposure ranking; it
    # is clearly labelled and never used for option pricing or order entry.
    d1 = (math.log(spot / strike) + (0.04 + 0.5 * iv * iv) * years) / sigma_t
    normal_cdf = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    delta = normal_cdf if option_type == "call" else normal_cdf - 1.0
    return max(0.0, min(1.0, abs(delta)))


def _row_trade_day(row: dict[str, Any]) -> str | None:
    """Return the option's last-trade ET date when the source supplies it."""
    value = row.get("lastTradeDate") or row.get("last_trade_time")
    if value is None:
        return None
    try:
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(ET).date().isoformat()
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone(ET).date().isoformat()
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            # A date-only provider value is already a market-session label.
            return parsed.date().isoformat()
        return parsed.astimezone(ET).date().isoformat()
    except (TypeError, ValueError, OSError):
        text = str(value)
        return text[:10] if len(text) >= 10 else None


def _score_current_event(
    *,
    source: str,
    expiry: str,
    dte: int,
    option_type: str,
    row: dict[str, Any],
    spot: float,
    spot_change_pct: float,
    volume_delta: int,
    previous: dict[str, Any],
    baseline_missing: bool,
    now: datetime,
    data_quality: str,
) -> dict[str, Any] | None:
    if volume_delta <= 0:
        return None
    strike = _safe_float(row.get("strike"))
    if strike <= 0 or spot <= 0:
        return None
    oi = _safe_int(row.get("openInterest"))
    price, spread_pct = _mid_and_spread(row)
    if price <= 0:
        return None
    premium = volume_delta * 100 * price
    iv = _normalise_iv(row.get("impliedVolatility") or row.get("iv"))
    supplied_delta = abs(_safe_float(row.get("delta")))
    approximated_delta = _approx_abs_delta(spot, strike, dte, iv, option_type)
    delta = supplied_delta if supplied_delta > 0 else (approximated_delta or 0.0)
    delta_source = "provider" if supplied_delta > 0 else (
        "black_scholes_approx" if approximated_delta is not None else "unavailable"
    )
    delta_notional = volume_delta * 100 * spot * delta if delta > 0 else None
    aggressor, aggressor_strength, direction_evidence = _aggressor_heuristic(row)
    direction = _direction(option_type, aggressor)

    history = list(previous.get("delta_history") or [])
    volume_zscore = _zscore(volume_delta, history)
    novelty, vol_oi = _novelty_points(volume_delta, oi, volume_zscore)
    size_points = _log_points(premium, 50_000, 10_000_000, 20)
    if data_quality == "tape":
        direction_points = 20 if direction != "unknown" else 0
    else:
        direction_points = 8 if direction != "unknown" else 0

    previous_snapshot = previous.get("snapshot") or {}
    previous_iv = _normalise_iv(previous_snapshot.get("iv"))
    iv_change_pct = None
    confirmation_points = 0
    if iv is not None and previous_iv:
        iv_change_pct = (iv / previous_iv - 1) * 100
        if aggressor == "buy" and iv_change_pct >= 3:
            confirmation_points += 3
    price_aligned = (
        direction == "bullish" and spot_change_pct >= 0.5
    ) or (
        direction == "bearish" and spot_change_pct <= -0.5
    )
    if price_aligned:
        confirmation_points += 5
    confirmation_points = min(10, confirmation_points)

    previous_positive = bool(previous.get("last_positive_delta"))
    streak = _safe_int(previous.get("hit_streak")) + 1 if previous_positive else 1
    persistence_points = min(15, max(0, streak - 1) * 5)

    penalties: list[dict[str, Any]] = []
    penalty_total = 0
    if baseline_missing:
        penalties.append({"reason": "first_snapshot_accumulated_volume", "points": 3})
        penalty_total += 3
    if spread_pct is not None and spread_pct > 0.50:
        penalties.append({"reason": "very_wide_spread", "points": 10})
        penalty_total += 10
    elif spread_pct is not None and spread_pct > 0.25:
        penalties.append({"reason": "wide_spread", "points": 5})
        penalty_total += 5
    moneyness = strike / spot
    if moneyness < 0.50 or moneyness > 1.50:
        penalties.append({"reason": "deep_tail_contract", "points": 5})
        penalty_total += 5

    raw_score = (
        size_points + novelty + direction_points + persistence_points
        + confirmation_points - penalty_total
    )
    cap = 100 if data_quality == "tape" else SNAPSHOT_SCORE_CAP
    score = max(0, min(cap, int(round(raw_score))))
    if score < SIGNAL_MIN_SCORE:
        return None

    return {
        "id": _contract_key(source, expiry, option_type, strike),
        "observed_at": _iso(now),
        "event_date": _et_day(now),
        "status": "oi_confirmation_pending",
        "source": source,
        "expiry": expiry,
        "dte": dte,
        "dte_bucket": _dte_bucket(dte),
        "option_type": option_type,
        "strike": round(strike, 4),
        "spot": round(spot, 4),
        "moneyness": round(moneyness, 4),
        "contracts": volume_delta,
        "session_volume": _safe_int(row.get("volume")),
        "open_interest": oi,
        "volume_oi_ratio": round(vol_oi, 3) if vol_oi is not None else None,
        "volume_zscore": round(volume_zscore, 2) if volume_zscore is not None else None,
        "estimated_premium": round(premium, 2),
        "delta_notional": round(delta_notional, 2) if delta_notional is not None else None,
        "abs_delta": round(delta, 4) if delta > 0 else None,
        "delta_source": delta_source,
        "price_used": round(price, 4),
        "spread_pct": round(spread_pct * 100, 2) if spread_pct is not None else None,
        "iv": round(iv, 6) if iv is not None else None,
        "iv_change_pct": round(iv_change_pct, 2) if iv_change_pct is not None else None,
        "aggressor": aggressor,
        "aggressor_confidence": (
            1.0 if data_quality == "tape" else round(aggressor_strength * 0.35, 2)
        ),
        "direction": direction,
        "direction_evidence": direction_evidence,
        "price_confirmed": price_aligned,
        "repeat_count": streak,
        "complex_suspected": False,
        "oi_confirmation": {
            "state": "pending",
            "note": "open interest is confirmed after clearing on the next session",
        },
        "score": score,
        "score_cap": cap,
        "data_quality": data_quality,
        "components": {
            "size": size_points,
            "novelty": novelty,
            "direction": direction_points,
            "persistence": persistence_points,
            "oi_confirmation": 0,
            "iv_price_confirmation": confirmation_points,
            "penalty": -penalty_total,
        },
        "penalties": penalties,
    }


def _mark_possible_complex(events: list[dict[str, Any]]) -> None:
    """Conservatively flag equal-size same-expiry clusters as possible multi-leg."""
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for event in events:
        contracts = _safe_int(event.get("contracts"))
        if contracts < 100:
            continue
        key = (str(event.get("source")), str(event.get("expiry")), contracts)
        groups.setdefault(key, []).append(event)
    for group in groups.values():
        strikes = {event.get("strike") for event in group}
        if len(group) < 2 or len(strikes) < 2:
            continue
        for event in group:
            event["complex_suspected"] = True
            event["score"] = max(0, _safe_int(event.get("score")) - 8)
            event["components"]["penalty"] -= 8
            event["penalties"].append({"reason": "possible_multi_leg_cluster", "points": 8})


def _confirmed_prior_event(
    previous: dict[str, Any], current_oi: int, now: datetime, data_quality: str
) -> dict[str, Any] | None:
    pending = previous.get("pending_event")
    snapshot = previous.get("snapshot") or {}
    if not isinstance(pending, dict):
        return None
    if pending.get("event_date") == _et_day(now):
        return None
    previous_oi = _safe_int(snapshot.get("open_interest"))
    oi_delta = current_oi - previous_oi
    contracts = max(1, _safe_int(pending.get("contracts")))
    confirmation_ratio = max(0.0, oi_delta) / contracts
    event = dict(pending)
    _backfill_event_delta(event)
    event["observed_at"] = _iso(now)
    event["status"] = "oi_confirmed" if oi_delta > 0 else "oi_not_confirmed"
    event["oi_confirmation"] = {
        "state": "confirmed" if oi_delta > 0 else "not_confirmed",
        "oi_delta": oi_delta,
        "ratio": round(confirmation_ratio, 3),
    }
    components = dict(event.get("components") or {})
    if confirmation_ratio >= 0.60:
        oi_points = 15
    elif confirmation_ratio >= 0.30:
        oi_points = 10
    elif confirmation_ratio >= 0.10:
        oi_points = 5
    else:
        oi_points = 0
    components["oi_confirmation"] = oi_points
    event["components"] = components
    base = _safe_int(event.get("score"))
    cap = 100 if data_quality == "tape" else SNAPSHOT_SCORE_CAP
    event["score"] = min(cap, base + oi_points) if oi_delta > 0 else max(0, base - 8)
    event["score_cap"] = cap
    return event if event["score"] >= SIGNAL_MIN_SCORE else None


def _backfill_event_delta(event: dict[str, Any]) -> None:
    """Migrate persisted v1 events created before the delta fallback existed."""
    if _safe_float(event.get("delta_notional")) > 0:
        return
    delta = _approx_abs_delta(
        _safe_float(event.get("spot")),
        _safe_float(event.get("strike")),
        _safe_int(event.get("dte")),
        _normalise_iv(event.get("iv")),
        str(event.get("option_type") or "call"),
    )
    if delta is None:
        return
    event["abs_delta"] = round(delta, 4)
    event["delta_source"] = "black_scholes_approx"
    event["delta_notional"] = round(
        _safe_int(event.get("contracts")) * 100 * _safe_float(event.get("spot")) * delta,
        2,
    )


def _position_action(direction: str, score: int) -> str:
    if score >= STRONG_SCORE:
        return "reduce_candidate" if direction == "bearish" else "add_candidate"
    if score >= ACTION_SCORE:
        return "pause_buy" if direction == "bearish" else "watch_add"
    return "watch" if score >= SIGNAL_MIN_SCORE else "none"


def _aggregate_positions(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    positions: dict[str, dict[str, Any]] = {}
    for ticker, proxies in POSITION_PROXY_MAP.items():
        weights = {proxy["source"]: _safe_float(proxy.get("weight"), 1.0) for proxy in proxies}
        relevant = [event for event in events if event.get("source") in weights]
        by_direction: dict[str, list[dict[str, Any]]] = {"bullish": [], "bearish": []}
        for event in relevant:
            direction = event.get("direction")
            if direction in by_direction:
                by_direction[direction].append(event)

        direction_scores: dict[str, int] = {}
        direction_sources: dict[str, list[str]] = {}
        for direction, items in by_direction.items():
            if not items:
                direction_scores[direction] = 0
                direction_sources[direction] = []
                continue
            weighted = sorted(
                (_safe_int(event.get("score")) * weights.get(event["source"], 1.0)
                 for event in items),
                reverse=True,
            )
            sources = sorted({str(event.get("source")) for event in items})
            repeat_boost = min(6, max(0, len(items) - 1) * 2)
            cross_proxy_boost = 5 if len(sources) >= 2 else 0
            quality_cap = min(_safe_int(event.get("score_cap"), SNAPSHOT_SCORE_CAP) for event in items)
            direction_scores[direction] = min(
                quality_cap, int(round(weighted[0] + repeat_boost + cross_proxy_boost))
            )
            direction_sources[direction] = sources

        bull_score = direction_scores["bullish"]
        bear_score = direction_scores["bearish"]
        if max(bull_score, bear_score) < SIGNAL_MIN_SCORE:
            direction = "neutral"
            score = max(bull_score, bear_score)
        elif abs(bull_score - bear_score) < 8:
            direction = "mixed"
            score = max(bull_score, bear_score)
        elif bull_score > bear_score:
            direction, score = "bullish", bull_score
        else:
            direction, score = "bearish", bear_score

        ranked_events = sorted(
            relevant, key=lambda event: _safe_int(event.get("score")), reverse=True
        )
        # Always reserve room for institutional/strategic maturities.  Otherwise
        # high-volume 0DTE gamma prints hide the far-dated signal the user cares
        # about even though both were detected.
        top_events = ranked_events[:3]
        for event in (item for item in ranked_events if _safe_int(item.get("dte")) >= 46):
            if event.get("id") not in {item.get("id") for item in top_events}:
                top_events.append(event)
            if len(top_events) >= 5:
                break
        quality = "tape" if top_events and all(
            event.get("data_quality") == "tape" for event in top_events
        ) else "snapshot"
        positions[ticker] = {
            "ticker": ticker,
            "direction": direction,
            "score": score,
            "bullish_score": bull_score,
            "bearish_score": bear_score,
            "action": _position_action(direction, score) if direction in ("bullish", "bearish") else "watch",
            "sources": [proxy["source"] for proxy in proxies],
            "confirming_sources": direction_sources.get(direction, []),
            "data_quality": quality,
            "score_cap": 100 if quality == "tape" else SNAPSHOT_SCORE_CAP,
            "top_events": top_events,
            "explanation": (
                "snapshot chain only; buy/sell side is heuristic and cannot trigger an automatic reduction"
                if quality != "tape" else
                "trade tape classification available; price momentum is still required for execution"
            ),
        }
    return positions


def analyze_option_payloads(
    payloads: dict[str, dict[str, Any]],
    previous_state: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pure analysis entry point used by the live fetcher and unit tests."""
    now = now or datetime.now(timezone.utc)
    day = _et_day(now)
    previous_state = previous_state if isinstance(previous_state, dict) else {}
    old_contracts = previous_state.get("contracts")
    if not isinstance(old_contracts, dict):
        old_contracts = {}
    new_contracts = dict(old_contracts)
    events: list[dict[str, Any]] = []
    source_summary: dict[str, dict[str, Any]] = {}

    for source, payload in payloads.items():
        spot = _safe_float(payload.get("spot"))
        spot_change_pct = _safe_float(payload.get("spot_change_pct"))
        data_quality = str(payload.get("data_quality") or "snapshot")
        scanned_expiries: list[str] = []
        source_errors = list(payload.get("errors") or [])
        contracts_seen = 0
        for chain in payload.get("chains") or []:
            expiry = str(chain.get("expiry") or "")
            try:
                expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
                dte = (expiry_date - now.astimezone(ET).date()).days
            except ValueError:
                continue
            if dte < 0 or dte > MAX_DTE:
                continue
            scanned_expiries.append(expiry)
            for option_type, rows in (
                ("call", _row_dicts(chain.get("calls"))),
                ("put", _row_dicts(chain.get("puts"))),
            ):
                for row in rows:
                    strike = _safe_float(row.get("strike"))
                    if strike <= 0:
                        continue
                    key = _contract_key(source, expiry, option_type, strike)
                    old = old_contracts.get(key) if isinstance(old_contracts.get(key), dict) else {}
                    current_volume = _safe_int(row.get("volume"))
                    row_trade_day = _row_trade_day(row)
                    # yfinance can expose yesterday's cumulative volume before
                    # the first trade of a new session.  Do not manufacture a
                    # fresh whale alert from that stale counter.
                    if row_trade_day is not None and row_trade_day < day:
                        current_volume = 0
                    current_oi = _safe_int(row.get("openInterest"))
                    # Retain contracts with a pending prior event even if today's volume is zero.
                    if current_volume <= 0 and not old.get("pending_event"):
                        continue
                    contracts_seen += 1
                    old_snapshot = old.get("snapshot") or {}
                    same_day = old_snapshot.get("session_date") == day
                    baseline_missing = not bool(old_snapshot)
                    old_volume = _safe_int(old_snapshot.get("volume")) if same_day else 0
                    volume_delta = max(0, current_volume - old_volume)

                    confirmed = _confirmed_prior_event(old, current_oi, now, data_quality)
                    if confirmed is not None:
                        events.append(confirmed)

                    event = _score_current_event(
                        source=source,
                        expiry=expiry,
                        dte=dte,
                        option_type=option_type,
                        row=row,
                        spot=spot,
                        spot_change_pct=spot_change_pct,
                        volume_delta=volume_delta,
                        previous=old,
                        baseline_missing=baseline_missing,
                        now=now,
                        data_quality=data_quality,
                    )
                    prior_pending = old.get("pending_event")
                    if event is not None:
                        # Keep the session's peak score.  Otherwise a persistent
                        # whale cluster can flicker from 63 to 58 merely because
                        # the rolling z-score baseline caught up with it.
                        if (isinstance(prior_pending, dict)
                                and prior_pending.get("event_date") == day
                                and _safe_int(prior_pending.get("score")) > _safe_int(event.get("score"))):
                            event["score"] = _safe_int(prior_pending.get("score"))
                            event["peak_score_persisted"] = True
                        events.append(event)
                    elif isinstance(prior_pending, dict) and prior_pending.get("event_date") == day:
                        try:
                            observed = datetime.fromisoformat(str(prior_pending.get("observed_at")))
                            age = now.timestamp() - observed.timestamp()
                        except (TypeError, ValueError):
                            age = SIGNAL_PERSIST_SEC + 1
                        if age <= SIGNAL_PERSIST_SEC:
                            carried = dict(prior_pending)
                            _backfill_event_delta(carried)
                            carried["status"] = "monitoring_existing_burst"
                            carried["last_checked_at"] = _iso(now)
                            events.append(carried)

                    history = list(old.get("delta_history") or [])
                    if volume_delta > 0:
                        history.append(volume_delta)
                    history = history[-20:]
                    pending = prior_pending
                    if event is not None and (
                        not isinstance(pending, dict)
                        or pending.get("event_date") != day
                        or _safe_int(event.get("score")) >= _safe_int(pending.get("score"))
                    ):
                        pending = event
                    elif isinstance(pending, dict) and pending.get("event_date") != day:
                        pending = None
                    new_contracts[key] = {
                        "last_seen": _iso(now),
                        "snapshot": {
                            "session_date": day,
                            "volume": current_volume,
                            "open_interest": current_oi,
                            "last_trade_date": row_trade_day,
                            "iv": _normalise_iv(row.get("impliedVolatility") or row.get("iv")),
                        },
                        "delta_history": history,
                        "last_positive_delta": volume_delta > 0,
                        "hit_streak": (
                            _safe_int(event.get("repeat_count")) if event is not None else 0
                        ),
                        "pending_event": pending,
                    }
        source_summary[source] = {
            "spot": spot or None,
            "spot_change_pct": round(spot_change_pct, 2),
            "data_quality": data_quality,
            "scanned_expiries": sorted(set(scanned_expiries)),
            "contracts_seen": contracts_seen,
            "errors": source_errors,
        }

    _mark_possible_complex(events)
    events = [event for event in events if _safe_int(event.get("score")) >= SIGNAL_MIN_SCORE]
    events.sort(key=lambda event: (_safe_int(event.get("score")), _safe_float(event.get("estimated_premium"))), reverse=True)
    positions = _aggregate_positions(events)

    # Avoid unbounded state growth while retaining pending far-dated contracts.
    now_ts = now.timestamp()
    compact_contracts: dict[str, Any] = {}
    for key, value in new_contracts.items():
        if not isinstance(value, dict):
            continue
        try:
            seen_ts = datetime.fromisoformat(str(value.get("last_seen"))).timestamp()
        except (TypeError, ValueError):
            seen_ts = now_ts
        keep_days = 45 if value.get("pending_event") else 14
        if now_ts - seen_ts <= keep_days * 86400:
            compact_contracts[key] = value

    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(now),
        "session_date_et": day,
        "refresh_sec": REFRESH_SEC,
        "monitoring_scope": list(MONITORED_SOURCES),
        "method": "small_universe_option_chain_delta",
        "limitations": [
            "snapshot last-vs-NBBO is a heuristic, not authoritative trade direction",
            "multi-leg detection is conservative without exchange execution identifiers",
            "open-interest confirmation is available on the next session",
            f"snapshot-only scores are capped at {SNAPSHOT_SCORE_CAP}",
        ],
        "sources": source_summary,
        "events": events[:100],
        "long_dated_events": [event for event in events if _safe_int(event.get("dte")) >= 46][:30],
        "positions": positions,
    }
    state = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _iso(now),
        "contracts": compact_contracts,
    }
    return result, state


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _default_fetch_source(source: str, now: datetime) -> dict[str, Any]:
    """Fetch selected expiries with yfinance; callers never see an exception."""
    payload: dict[str, Any] = {
        "source": source,
        "data_quality": "snapshot",
        "chains": [],
        "errors": [],
    }
    try:
        import yfinance as yf

        ticker = yf.Ticker(source)
        history = ticker.history(period="5d", interval="1d", auto_adjust=False)
        if history is not None and not history.empty:
            closes = history["Close"].dropna()
            if len(closes):
                payload["spot"] = float(closes.iloc[-1])
            if len(closes) >= 2 and float(closes.iloc[-2]) > 0:
                payload["spot_change_pct"] = (
                    float(closes.iloc[-1]) / float(closes.iloc[-2]) - 1
                ) * 100
        available_expiries = list(ticker.options or [])
        expiries = select_expiries(available_expiries, now=now)
        payload["available_expiries"] = available_expiries
        payload["selected_expiries"] = expiries
        for expiry in expiries:
            try:
                chain = ticker.option_chain(expiry)
                payload["chains"].append({
                    "expiry": expiry,
                    "calls": chain.calls,
                    "puts": chain.puts,
                })
            except Exception as exc:
                payload["errors"].append(f"{expiry}: {str(exc)[:120]}")
    except Exception as exc:
        payload["errors"].append(str(exc)[:200])
    if not payload["chains"]:
        # OpenD fallback keeps the monitor useful during a yfinance outage.  It
        # returns one liquid <=60 DTE chain, so coverage remains explicit rather
        # than pretending the long-dated scan succeeded.
        try:
            from moomoo_data import get_option_chain_via_openD
            chain = get_option_chain_via_openD(source)
            if chain:
                payload["chains"].append({
                    "expiry": chain["expiry"],
                    "calls": chain["calls"],
                    "puts": chain["puts"],
                })
                payload["fallback_source"] = "openD"
        except Exception as exc:
            payload["errors"].append(f"openD fallback: {str(exc)[:120]}")
    return payload


def refresh_option_flow(
    *,
    force: bool = False,
    fetch_source: Callable[[str, datetime], dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Refresh and persist the flow monitor.  Safe to call from a daemon thread."""
    now = now or datetime.now(timezone.utc)
    if not force and LATEST_PATH.exists():
        try:
            if time.time() - LATEST_PATH.stat().st_mtime < REFRESH_SEC:
                return load_option_flow_signal()
        except OSError:
            pass
    if not _REFRESH_LOCK.acquire(blocking=False):
        current = load_option_flow_signal()
        current["refreshing"] = True
        return current
    try:
        fetcher = fetch_source or _default_fetch_source
        payloads = {source: fetcher(source, now) for source in MONITORED_SOURCES}
        result, state = analyze_option_payloads(
            payloads,
            _load_json(STATE_PATH),
            now=now,
        )
        atomic_write_json(STATE_PATH, state)
        atomic_write_json(LATEST_PATH, result)
        return result
    finally:
        _REFRESH_LOCK.release()


def load_option_flow_signal(*, max_age_sec: int = 8 * 3600) -> dict[str, Any]:
    """Load the last complete result without performing network I/O."""
    result = _load_json(LATEST_PATH)
    if not result:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": None,
            "events": [],
            "positions": {},
            "status": "not_ready",
        }
    try:
        age_sec = max(0.0, time.time() - LATEST_PATH.stat().st_mtime)
    except OSError:
        age_sec = float("inf")
    result["age_sec"] = round(age_sec, 1) if math.isfinite(age_sec) else None
    result["stale"] = age_sec > max_age_sec
    result["status"] = "stale" if result["stale"] else "ready"
    return result


def should_refresh_now(now: datetime | None = None) -> bool:
    """US-session cadence: pre-market OI check through shortly after close."""
    now = now or datetime.now(timezone.utc)
    et_now = now.astimezone(ET)
    if et_now.weekday() >= 5:
        return False
    minutes = et_now.hour * 60 + et_now.minute
    return 8 * 60 <= minutes <= 16 * 60 + 30


if __name__ == "__main__":
    print(json.dumps(refresh_option_flow(force=True), indent=2, ensure_ascii=False))
