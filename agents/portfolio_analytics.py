"""Institutional-style portfolio, attribution, risk and liquidity analytics.

The functions are intentionally pure where possible so the same calculations
can be used by the live dashboard, scheduled reports and unit tests.  This is a
risk/measurement layer; it never creates a BUY or SELL signal.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from leveraged_etf_risk import analyze_position_path_risk, leveraged_profile, normalize_ticker


DEFAULT_ANNUAL_VOL = {
    "TQQQ": 0.60,
    "SOXL": 0.80,
    "MULL": 0.65,
    "DRAM": 0.38,
    "GLD": 0.18,
    "SHY": 0.05,
    "IEI": 0.08,
    "TLT": 0.15,
}

GROUPS = {
    "TQQQ": "nasdaq",
    "SOXL": "semiconductor",
    "MULL": "semiconductor",
    "DRAM": "semiconductor",
    "NVDA": "semiconductor",
    "AMAT": "semiconductor",
    "KLAC": "semiconductor",
    "GLD": "gold",
    "USO": "energy",
    "SHY": "rates",
    "IEI": "rates",
    "TLT": "rates",
}

STRESS_SHOCKS = {
    "科技回调": {
        "nasdaq": -0.09,
        "semiconductor": -0.15,
        "equity": -0.06,
        "gold": 0.01,
        "rates": 0.005,
        "energy": -0.04,
    },
    "流动性冲击": {
        "nasdaq": -0.18,
        "semiconductor": -0.25,
        "equity": -0.12,
        "gold": -0.04,
        "rates": 0.01,
        "energy": -0.12,
    },
    "利率上行": {
        "nasdaq": -0.10,
        "semiconductor": -0.12,
        "equity": -0.06,
        "gold": -0.07,
        "rates": -0.04,
        "energy": 0.03,
    },
    "避险反弹": {
        "nasdaq": 0.06,
        "semiconductor": 0.09,
        "equity": 0.04,
        "gold": 0.03,
        "rates": -0.005,
        "energy": 0.02,
    },
}


def _num(value, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _returns(prices: Iterable[float]) -> list[float]:
    values = [_num(x) for x in prices]
    out = []
    for prev, cur in zip(values, values[1:]):
        if prev > 0 and cur > 0:
            out.append(cur / prev - 1.0)
    return out


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = max(0.0, min(1.0, p)) * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    weight = pos - lo
    return ordered[lo] * (1 - weight) + ordered[hi] * weight


def _group(ticker: str) -> str:
    tk = normalize_ticker(ticker)
    if tk in GROUPS:
        return GROUPS[tk]
    return "equity"


def normalize_positions(positions: list[dict], nav: float | None = None) -> list[dict]:
    rows: list[dict] = []
    for raw in positions or []:
        qty = _num(raw.get("qty"))
        if qty <= 0:
            continue
        ticker = str(raw.get("ticker") or raw.get("code") or "").upper()
        price = _num(raw.get("current_price") or raw.get("nominal_price"))
        cost = _num(raw.get("cost_price") or raw.get("entry_price"))
        market_value = _num(raw.get("market_val"), qty * price)
        if market_value <= 0:
            market_value = qty * price
        pnl = _num(raw.get("pl_val"), (price - cost) * qty if cost and price else 0.0)
        profile = leveraged_profile(ticker)
        leverage = float(profile["leverage"]) if profile else 1.0
        rows.append({
            "ticker": ticker,
            "qty": qty,
            "price": price,
            "cost_price": cost,
            "market_value": market_value,
            "unrealized_pnl": pnl,
            "group": _group(ticker),
            "leverage": leverage,
            "effective_exposure": market_value * leverage,
            "avg_daily_volume": _num(raw.get("avg_daily_volume")),
        })
    total_mv = sum(row["market_value"] for row in rows)
    denominator = _num(nav) or total_mv or 1.0
    for row in rows:
        row["weight"] = row["market_value"] / denominator
    return rows


def _portfolio_return_series(rows: list[dict], histories: dict[str, list[float]]) -> list[float]:
    series: list[tuple[float, list[float]]] = []
    for row in rows:
        tk = normalize_ticker(row["ticker"])
        values = histories.get(tk) or histories.get(row["ticker"])
        if not values:
            continue
        ret = _returns(values)
        if ret:
            series.append((row["weight"], ret))
    if not series:
        return []
    n = min(len(ret) for _, ret in series)
    if n <= 0:
        return []
    return [sum(weight * ret[-n + i] for weight, ret in series) for i in range(n)]


def _fallback_var(rows: list[dict], nav: float) -> tuple[float, float, float]:
    # Conservative group-correlation approximation for missing history.
    variance = 0.0
    for i, left in enumerate(rows):
        ltk = normalize_ticker(left["ticker"])
        lv = DEFAULT_ANNUAL_VOL.get(ltk, 0.40) / math.sqrt(252)
        for j, right in enumerate(rows):
            rtk = normalize_ticker(right["ticker"])
            rv = DEFAULT_ANNUAL_VOL.get(rtk, 0.40) / math.sqrt(252)
            corr = 1.0 if i == j else (0.75 if left["group"] == right["group"] else 0.25)
            variance += left["weight"] * right["weight"] * lv * rv * corr
    sigma = math.sqrt(max(0.0, variance))
    return sigma, nav * 1.645 * sigma, nav * 2.063 * sigma


def risk_metrics(rows: list[dict], nav: float, histories: dict[str, list[float]] | None = None) -> dict:
    histories = histories or {}
    returns = _portfolio_return_series(rows, histories)
    if len(returns) >= 30:
        q05 = _percentile(returns, 0.05)
        tail = [r for r in returns if r <= q05]
        var95 = max(0.0, -q05 * nav)
        es95 = max(0.0, -(sum(tail) / len(tail)) * nav) if tail else var95
        sigma = math.sqrt(sum((r - sum(returns) / len(returns)) ** 2 for r in returns) / max(1, len(returns) - 1))
        source = "historical"
    else:
        sigma, var95, es95 = _fallback_var(rows, nav)
        source = "fallback_covariance"
    return {
        "source": source,
        "observations": len(returns),
        "daily_vol_pct": sigma * 100,
        "annual_vol_pct": sigma * math.sqrt(252) * 100,
        "var_95_1d": var95,
        "var_95_1d_pct": (var95 / nav * 100) if nav else 0.0,
        "expected_shortfall_95_1d": es95,
        "expected_shortfall_95_1d_pct": (es95 / nav * 100) if nav else 0.0,
    }


def stress_test(rows: list[dict], nav: float) -> list[dict]:
    out = []
    for name, shocks in STRESS_SHOCKS.items():
        details = []
        total = 0.0
        for row in rows:
            proxy_shock = shocks.get(row["group"], shocks["equity"])
            # Stress shocks describe the underlying risk group.  Daily-reset
            # ETF positions need the first-order leveraged move here; their
            # multi-day volatility decay is reported separately by path tests.
            shock = max(-1.0, proxy_shock * row["leverage"])
            pnl = row["market_value"] * shock
            total += pnl
            details.append({
                "ticker": row["ticker"],
                "proxy_shock_pct": proxy_shock * 100,
                "position_shock_pct": shock * 100,
                "leverage": row["leverage"],
                "pnl": pnl,
            })
        out.append({
            "name": name,
            "pnl": total,
            "nav_impact_pct": total / nav * 100 if nav else 0.0,
            "positions": details,
        })
    return out


def liquidity_metrics(rows: list[dict], nav: float) -> dict:
    details = []
    known = 0
    for row in rows:
        adv = row.get("avg_daily_volume", 0.0)
        if adv > 0 and row["price"] > 0:
            known += 1
            daily_capacity = adv * 0.10
            days = row["qty"] / daily_capacity if daily_capacity else None
            participation = row["qty"] / adv
        else:
            days = None
            participation = None
        details.append({
            "ticker": row["ticker"],
            "days_to_liquidate_at_10pct_adv": days,
            "position_pct_adv": participation * 100 if participation is not None else None,
            # Do not charge a synthetic spread/impact penalty for this small
            # account.  Real broker fills are measured in execution_analytics.
            "estimated_impact_bps": None,
        })
    return {
        "small_account_assumption": True,
        "capacity_is_binding": False,
        "policy": "observe_only_no_order_constraint",
        "coverage_pct": known / len(rows) * 100 if rows else 100.0,
        "positions": details,
        "max_days_to_liquidate": max((x["days_to_liquidate_at_10pct_adv"] or 0) for x in details) if details else 0.0,
    }


def return_attribution(rows: list[dict], histories: dict[str, list[float]] | None = None) -> dict:
    histories = histories or {}
    by_group = defaultdict(float)
    total_unrealized = 0.0
    daily = []
    for row in rows:
        by_group[row["group"]] += row["unrealized_pnl"]
        total_unrealized += row["unrealized_pnl"]
        tk = normalize_ticker(row["ticker"])
        values = histories.get(tk) or histories.get(row["ticker"])
        if values and len(values) >= 2 and _num(values[-2]) > 0:
            ret = _num(values[-1]) / _num(values[-2]) - 1.0
            daily.append({
                "ticker": row["ticker"],
                "return_pct": ret * 100,
                "estimated_pnl": row["market_value"] * ret,
                "group": row["group"],
            })
    return {
        "unrealized_pnl": total_unrealized,
        "unrealized_by_group": dict(by_group),
        "latest_session": daily,
        "latest_session_estimated_pnl": sum(x["estimated_pnl"] for x in daily),
    }


def correlation_and_risk_contribution(rows: list[dict], histories: dict[str, list[float]] | None = None) -> dict:
    histories = histories or {}
    usable = []
    for row in rows:
        tk = normalize_ticker(row["ticker"])
        ret = _returns(histories.get(tk) or histories.get(row["ticker"]) or [])
        if len(ret) >= 30:
            usable.append((row, ret))
    if len(usable) < 2:
        total = sum(abs(row["effective_exposure"]) for row in rows) or 1.0
        return {
            "source": "effective_exposure_fallback",
            "correlations": [],
            "risk_contributions": [
                {"ticker": row["ticker"], "pct": abs(row["effective_exposure"]) / total * 100}
                for row in rows
            ],
        }
    n = min(len(ret) for _, ret in usable)
    matrix = [ret[-n:] for _, ret in usable]
    means = [sum(ret) / n for ret in matrix]
    cov = []
    for i, left in enumerate(matrix):
        row_cov = []
        for j, right in enumerate(matrix):
            value = sum((left[k] - means[i]) * (right[k] - means[j]) for k in range(n)) / max(1, n - 1)
            row_cov.append(value)
        cov.append(row_cov)
    weights = [item[0]["weight"] for item in usable]
    cw = [sum(cov[i][j] * weights[j] for j in range(len(weights))) for i in range(len(weights))]
    variance = sum(weights[i] * cw[i] for i in range(len(weights)))
    contributions = []
    for i, (row, _) in enumerate(usable):
        pct = weights[i] * cw[i] / variance * 100 if variance > 0 else 0.0
        contributions.append({"ticker": row["ticker"], "pct": pct})
    correlations = []
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            denom = math.sqrt(max(0.0, cov[i][i] * cov[j][j]))
            correlations.append({
                "left": usable[i][0]["ticker"],
                "right": usable[j][0]["ticker"],
                "correlation": cov[i][j] / denom if denom else 0.0,
            })
    return {
        "source": "historical_covariance",
        "observations": n,
        "correlations": correlations,
        "risk_contributions": sorted(contributions, key=lambda x: x["pct"], reverse=True),
    }


def benchmark_attribution(
    rows: list[dict], histories: dict[str, list[float]] | None, benchmark_prices: list[float] | None
) -> dict:
    portfolio = _portfolio_return_series(rows, histories or {})
    benchmark = _returns(benchmark_prices or [])
    n = min(len(portfolio), len(benchmark))
    if n < 30:
        return {"available": False, "reason": "insufficient_aligned_history", "observations": n}
    p = portfolio[-n:]
    b = benchmark[-n:]
    pm = sum(p) / n
    bm = sum(b) / n
    variance_b = sum((x - bm) ** 2 for x in b) / max(1, n - 1)
    covariance = sum((p[i] - pm) * (b[i] - bm) for i in range(n)) / max(1, n - 1)
    beta = covariance / variance_b if variance_b > 0 else 0.0
    alpha_daily = pm - beta * bm
    active = [p[i] - b[i] for i in range(n)]
    active_mean = sum(active) / n
    active_var = sum((x - active_mean) ** 2 for x in active) / max(1, n - 1)
    tracking = math.sqrt(active_var) * math.sqrt(252)
    return {
        "available": True,
        "benchmark": "SPY",
        "observations": n,
        "beta": beta,
        "annualized_alpha_pct": alpha_daily * 252 * 100,
        "tracking_error_pct": tracking * 100,
        "information_ratio": active_mean * 252 / tracking if tracking > 0 else 0.0,
    }


def risk_balance_reference(rows: list[dict]) -> list[dict]:
    """Non-binding inverse-vol reference weights for dashboard comparison."""
    scores = []
    for row in rows:
        vol = DEFAULT_ANNUAL_VOL.get(normalize_ticker(row["ticker"]), 0.40)
        scores.append((row, 1.0 / max(vol, 0.01)))
    total = sum(score for _, score in scores) or 1.0
    return [
        {
            "ticker": row["ticker"],
            "current_weight_pct": row["weight"] * 100,
            "risk_balanced_reference_pct": score / total * 100,
            "difference_pct_points": row["weight"] * 100 - score / total * 100,
            "binding": False,
        }
        for row, score in scores
    ]


def analyze_portfolio(
    positions: list[dict],
    *,
    nav: float | None = None,
    cash: float | None = None,
    histories: dict[str, list[float]] | None = None,
    proxy_histories: dict[str, list[float]] | None = None,
    benchmark_history: list[float] | None = None,
) -> dict:
    rows = normalize_positions(positions, nav)
    market_value = sum(row["market_value"] for row in rows)
    safe_nav = _num(nav) or (market_value + _num(cash))
    if safe_nav <= 0:
        safe_nav = market_value
    safe_cash = _num(cash, safe_nav - market_value) if cash is not None else safe_nav - market_value
    gross = sum(abs(row["market_value"]) for row in rows)
    effective = sum(abs(row["effective_exposure"]) for row in rows)
    by_group = defaultdict(float)
    for row in rows:
        by_group[row["group"]] += row["effective_exposure"]

    risk = risk_metrics(rows, safe_nav, histories)
    stresses = stress_test(rows, safe_nav)
    liquidity = liquidity_metrics(rows, safe_nav)
    attribution = return_attribution(rows, histories)
    factor_risk = correlation_and_risk_contribution(rows, histories)
    benchmark = benchmark_attribution(rows, histories, benchmark_history)
    path_risk = []
    proxy_histories = proxy_histories or {}
    histories = histories or {}
    for row in rows:
        profile = leveraged_profile(row["ticker"])
        if not profile:
            continue
        path_risk.append(analyze_position_path_risk(
            row["ticker"],
            row["market_value"],
            proxy_prices=proxy_histories.get(profile["proxy"]),
            leveraged_prices=histories.get(normalize_ticker(row["ticker"])),
        ))

    flags = []
    max_weight = max((row["weight"] for row in rows), default=0.0)
    if max_weight > 0.35:
        flags.append({"level": "high", "code": "concentration", "message": "单一仓位超过NAV的35%"})
    if safe_nav and effective / safe_nav > 1.5:
        flags.append({"level": "high", "code": "effective_leverage", "message": "杠杆调整后总暴露超过NAV的150%"})
    if risk["var_95_1d_pct"] > 3.0:
        flags.append({"level": "high", "code": "var", "message": "单日95% VaR超过NAV的3%"})

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "nav": safe_nav,
        "cash": safe_cash,
        "market_value": market_value,
        "gross_exposure_pct": gross / safe_nav * 100 if safe_nav else 0.0,
        "effective_gross_exposure_pct": effective / safe_nav * 100 if safe_nav else 0.0,
        "cash_pct": safe_cash / safe_nav * 100 if safe_nav else 0.0,
        "positions": rows,
        "effective_exposure_by_group": dict(by_group),
        "risk": risk,
        "stress_tests": stresses,
        "liquidity": liquidity,
        "attribution": attribution,
        "factor_and_correlation_risk": factor_risk,
        "benchmark_attribution": benchmark,
        "risk_balance_reference": risk_balance_reference(rows),
        "leveraged_path_risk": path_risk,
        "risk_flags": flags,
        "status": "high_risk" if any(x["level"] == "high" for x in flags) else "ok",
    }


def pretrade_portfolio_gate(
    positions: list[dict],
    *,
    nav: float,
    ticker: str,
    side: str,
    qty: int,
    price: float,
) -> dict:
    """Run a hypothetical post-trade portfolio through hard risk limits."""
    hypothetical = [dict(p) for p in positions or []]
    target = str(ticker).upper()
    signed_qty = int(qty) if str(side).upper().startswith("BUY") else -int(qty)
    found = False
    for row in hypothetical:
        code = str(row.get("ticker") or row.get("code") or "").upper()
        if code != target:
            continue
        found = True
        new_qty = max(0.0, _num(row.get("qty")) + signed_qty)
        row["qty"] = new_qty
        row["current_price"] = _num(row.get("current_price"), price) or price
        row["market_val"] = new_qty * row["current_price"]
        break
    if not found and signed_qty > 0:
        hypothetical.append({
            "ticker": target,
            "qty": signed_qty,
            "current_price": price,
            "cost_price": price,
            "market_val": signed_qty * price,
            "pl_val": 0.0,
        })
    report = analyze_portfolio(hypothetical, nav=nav, cash=nav - sum(_num(p.get("market_val")) for p in hypothetical))
    worst_stress = min((x["nav_impact_pct"] for x in report["stress_tests"]), default=0.0)
    max_weight = max((x["weight"] * 100 for x in report["positions"]), default=0.0)
    checks = {
        "single_position_pct": {"value": max_weight, "limit": 40.0, "passed": max_weight <= 40.0},
        "effective_gross_pct": {
            "value": report["effective_gross_exposure_pct"],
            "limit": 180.0,
            "passed": report["effective_gross_exposure_pct"] <= 180.0,
        },
        "var_95_1d_pct": {
            "value": report["risk"]["var_95_1d_pct"],
            "limit": 5.0,
            "passed": report["risk"]["var_95_1d_pct"] <= 5.0,
        },
        "worst_stress_pct": {
            "value": worst_stress,
            "limit": -18.0,
            "passed": worst_stress >= -18.0,
        },
    }
    reducing = str(side).upper().startswith("SELL")
    breaches = [name for name, check in checks.items() if not check["passed"]]
    return {
        "allow_order": reducing or not breaches,
        "risk_reducing_override": reducing and bool(breaches),
        "breaches": breaches,
        "checks": checks,
        "post_trade": {
            "effective_gross_exposure_pct": report["effective_gross_exposure_pct"],
            "var_95_1d_pct": report["risk"]["var_95_1d_pct"],
            "worst_stress_pct": worst_stress,
            "max_position_pct": max_weight,
        },
    }
