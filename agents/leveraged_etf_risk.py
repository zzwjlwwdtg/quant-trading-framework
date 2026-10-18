"""Leveraged ETF path-risk and volatility-decay analytics.

This module deliberately does not generate trading signals.  It explains how a
daily-reset leveraged ETF can diverge from a simple ``leverage × endpoint``
estimate and provides deterministic stress paths for pre-trade risk checks.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


LEVERAGED_ETFS: dict[str, dict] = {
    "TQQQ": {"proxy": "QQQ", "leverage": 3.0, "annual_fee": 0.0084},
    "SOXL": {"proxy": "SOXX", "leverage": 3.0, "annual_fee": 0.0075},
    "MULL": {"proxy": "MU", "leverage": 2.0, "annual_fee": 0.0150},
}


def normalize_ticker(ticker: str) -> str:
    return str(ticker or "").upper().replace("US.", "")


def leveraged_profile(ticker: str) -> dict | None:
    profile = LEVERAGED_ETFS.get(normalize_ticker(ticker))
    return dict(profile) if profile else None


def simulate_daily_reset(
    proxy_returns: Iterable[float],
    leverage: float,
    *,
    annual_fee: float = 0.0,
    annual_tracking_drag: float = 0.0,
    initial_value: float = 100.0,
) -> dict:
    """Compound a daily-reset leveraged product over an explicit return path."""
    returns = [float(r) for r in proxy_returns]
    daily_drag = (annual_fee + annual_tracking_drag) / 252.0
    proxy_value = float(initial_value)
    etf_value = float(initial_value)
    for ret in returns:
        if ret <= -1:
            raise ValueError("proxy daily return must be greater than -100%")
        proxy_value *= 1.0 + ret
        etf_daily = leverage * ret - daily_drag
        etf_value *= max(0.0, 1.0 + etf_daily)

    proxy_total = proxy_value / initial_value - 1.0
    etf_total = etf_value / initial_value - 1.0
    endpoint_linear = leverage * proxy_total
    return {
        "days": len(returns),
        "proxy_return": proxy_total,
        "leveraged_return": etf_total,
        "linear_endpoint_return": endpoint_linear,
        "path_gap": etf_total - endpoint_linear,
        "ending_value": etf_value,
    }


def _flat_high_vol_path(days: int, daily_move: float) -> list[float]:
    """Alternating path whose proxy endpoint stays near flat."""
    path: list[float] = []
    for i in range(days):
        path.append(daily_move if i % 2 == 0 else -daily_move)
    return path


def _trend_path(total_return: float, days: int) -> list[float]:
    if days <= 0:
        return []
    daily = math.pow(1.0 + total_return, 1.0 / days) - 1.0
    return [daily] * days


def standard_path_scenarios(ticker: str) -> list[dict]:
    """Return deterministic stress scenarios for one leveraged ETF."""
    profile = leveraged_profile(ticker)
    if not profile:
        return []
    lev = float(profile["leverage"])
    fee = float(profile["annual_fee"])
    cases = [
        ("单边回撤", _trend_path(-0.10, 10), "proxy -10% over 10 sessions"),
        ("横盘高波动", _flat_high_vol_path(20, 0.04), "20 sessions alternating ±4%"),
        (
            "V形先跌后涨",
            _trend_path(-0.12, 5) + _trend_path(0.12 / 0.88, 5),
            "proxy -12%, then recovers to its starting level",
        ),
        ("趋势上涨", _trend_path(0.10, 10), "proxy +10% over 10 sessions"),
    ]
    out = []
    for name, path, description in cases:
        result = simulate_daily_reset(path, lev, annual_fee=fee, annual_tracking_drag=0.002)
        result.update({"name": name, "description": description})
        out.append(result)
    return out


def estimate_realized_decay(
    proxy_prices: Iterable[float], leveraged_prices: Iterable[float], leverage: float
) -> dict:
    """Measure realized daily tracking residual and compounding gap."""
    proxy = [float(x) for x in proxy_prices]
    etf = [float(x) for x in leveraged_prices]
    n = min(len(proxy), len(etf))
    if n < 3:
        return {"available": False, "reason": "insufficient_history"}
    proxy = proxy[-n:]
    etf = etf[-n:]
    residuals: list[float] = []
    for i in range(1, n):
        if proxy[i - 1] <= 0 or etf[i - 1] <= 0:
            continue
        pr = proxy[i] / proxy[i - 1] - 1.0
        er = etf[i] / etf[i - 1] - 1.0
        residuals.append(er - leverage * pr)
    if not residuals:
        return {"available": False, "reason": "invalid_history"}
    proxy_total = proxy[-1] / proxy[0] - 1.0
    etf_total = etf[-1] / etf[0] - 1.0
    linear = leverage * proxy_total
    mean_daily = sum(residuals) / len(residuals)
    rms = math.sqrt(sum(x * x for x in residuals) / len(residuals))
    return {
        "available": True,
        "days": len(residuals),
        "proxy_return": proxy_total,
        "leveraged_return": etf_total,
        "linear_endpoint_return": linear,
        "compounding_and_tracking_gap": etf_total - linear,
        "mean_daily_tracking_residual": mean_daily,
        "tracking_error_daily_rms": rms,
        "annualized_residual": mean_daily * 252.0,
    }


def analyze_position_path_risk(
    ticker: str,
    market_value: float,
    *,
    proxy_prices: Iterable[float] | None = None,
    leveraged_prices: Iterable[float] | None = None,
) -> dict:
    profile = leveraged_profile(ticker)
    if not profile:
        return {"ticker": normalize_ticker(ticker), "leveraged": False}
    scenarios = standard_path_scenarios(ticker)
    for scenario in scenarios:
        scenario["position_pnl"] = float(market_value) * scenario["leveraged_return"]
    realized = {"available": False, "reason": "history_not_supplied"}
    if proxy_prices is not None and leveraged_prices is not None:
        realized = estimate_realized_decay(
            proxy_prices, leveraged_prices, float(profile["leverage"])
        )
    return {
        "ticker": normalize_ticker(ticker),
        "leveraged": True,
        "proxy": profile["proxy"],
        "leverage": profile["leverage"],
        "market_value": float(market_value),
        "effective_exposure": float(market_value) * float(profile["leverage"]),
        "realized_decay": realized,
        "scenarios": scenarios,
    }

