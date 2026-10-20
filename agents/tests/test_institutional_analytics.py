from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from data_quality import PointInTimeStore, order_data_gate
from execution_analytics import (
    actual_execution_quality,
    append_execution_event,
    estimate_execution,
    verify_execution_log,
)
from leveraged_etf_risk import estimate_realized_decay, standard_path_scenarios
from portfolio_analytics import analyze_portfolio, pretrade_portfolio_gate
from research_validation import evaluate_rule_walk_forward, purged_walk_forward_splits


class LeveragedEtfPathTests(unittest.TestCase):
    def test_flat_high_vol_path_exposes_volatility_decay(self):
        scenarios = standard_path_scenarios("US.TQQQ")
        flat = next(item for item in scenarios if item["name"] == "横盘高波动")
        self.assertLess(abs(flat["proxy_return"]), 0.02)
        self.assertLess(flat["leveraged_return"], -0.05)
        self.assertLess(flat["path_gap"], 0)

    def test_realized_tracking_residual_is_measured(self):
        result = estimate_realized_decay(
            [100.0, 102.0, 100.0, 103.0],
            [100.0, 105.8, 99.4, 108.0],
            3.0,
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["days"], 3)
        self.assertIn("compounding_and_tracking_gap", result)


class PortfolioAnalyticsTests(unittest.TestCase):
    @staticmethod
    def _prices(start: float, returns: list[float]) -> list[float]:
        values = [start]
        for ret in returns:
            values.append(values[-1] * (1.0 + ret))
        return values

    def test_leveraged_exposure_stress_and_benchmark_are_reported(self):
        alternating = [0.012 if i % 2 == 0 else -0.009 for i in range(90)]
        spy_returns = [0.004 if i % 2 == 0 else -0.003 for i in range(90)]
        histories = {
            "TQQQ": self._prices(100, alternating),
            "GLD": self._prices(100, [0.001] * 90),
        }
        report = analyze_portfolio(
            [
                {"ticker": "US.TQQQ", "qty": 60, "current_price": 100, "market_val": 6000},
                {"ticker": "US.GLD", "qty": 10, "current_price": 200, "market_val": 2000},
            ],
            nav=10_000,
            cash=2_000,
            histories=histories,
            proxy_histories={"QQQ": self._prices(100, spy_returns)},
            benchmark_history=self._prices(100, spy_returns),
        )
        self.assertAlmostEqual(report["effective_gross_exposure_pct"], 200.0)
        tech = next(item for item in report["stress_tests"] if item["name"] == "科技回调")
        tqqq = next(item for item in tech["positions"] if item["ticker"] == "US.TQQQ")
        self.assertAlmostEqual(tqqq["position_shock_pct"], -27.0)
        self.assertTrue(report["benchmark_attribution"]["available"])
        self.assertTrue(report["leveraged_path_risk"][0]["leveraged"])
        self.assertFalse(report["liquidity"]["capacity_is_binding"])
        self.assertIsNone(report["liquidity"]["positions"][0]["estimated_impact_bps"])

    def test_pretrade_report_detects_risk_but_sells_remain_allowed(self):
        positions = [{"ticker": "US.TQQQ", "qty": 30, "current_price": 100, "market_val": 3000}]
        buy = pretrade_portfolio_gate(
            positions, nav=10_000, ticker="US.TQQQ", side="BUY", qty=50, price=100
        )
        sell = pretrade_portfolio_gate(
            positions, nav=10_000, ticker="US.TQQQ", side="SELL", qty=30, price=100
        )
        self.assertFalse(buy["allow_order"])
        self.assertTrue(buy["breaches"])
        self.assertTrue(sell["allow_order"])


class SmallAccountExecutionTests(unittest.TestCase):
    def test_theoretical_spread_impact_and_capacity_are_not_constraints(self):
        plan = estimate_execution(
            "SOXL", "BUY", 7, 145.0, bid=144.0, ask=146.0,
            avg_daily_volume=10, annual_volatility=0.9, outside_rth=True,
        )
        self.assertEqual(plan["spread_bps"], 0.0)
        self.assertEqual(plan["impact_bps"], 0.0)
        self.assertEqual(plan["capacity_status"], "ok_small_account")
        self.assertEqual(plan["expected_fill_price"], 145.0)

    def test_actual_fill_shortfall_and_hash_chain_are_auditable(self):
        quality = actual_execution_quality(
            side="BUY", requested_qty=10, dealt_qty=8,
            reference_price=100, average_fill_price=100.25,
        )
        self.assertAlmostEqual(quality["fill_ratio"], 0.8)
        self.assertAlmostEqual(quality["implementation_shortfall_bps"], 25.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            append_execution_event(path, {"event": "submitted", "order_id": "1"})
            append_execution_event(path, {"event": "filled", "order_id": "1", "quality": quality})
            self.assertEqual(verify_execution_log(path)["checked"], 2)
            rows = path.read_text(encoding="utf-8").splitlines()
            tampered = json.loads(rows[0])
            tampered["event"] = "changed"
            rows[0] = json.dumps(tampered)
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            self.assertFalse(verify_execution_log(path)["valid"])


class DataQualityTests(unittest.TestCase):
    def test_stale_invalid_buy_is_blocked_but_exit_is_allowed(self):
        now = datetime(2026, 8, 18, tzinfo=timezone.utc)
        market = {"price": -1, "ts": (now - timedelta(days=3)).isoformat()}
        self.assertFalse(order_data_gate(market, "BUY", now=now)["allow_order"])
        sell = order_data_gate(market, "SELL", now=now)
        self.assertTrue(sell["allow_order"])
        self.assertTrue(sell["risk_reducing_override"])

    def test_point_in_time_store_prevents_future_leakage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PointInTimeStore(Path(tmp) / "pit.jsonl")
            t0 = datetime(2026, 8, 18, 10, tzinfo=timezone.utc)
            store.append(source="market", payload={"price": 100}, observed_at=t0)
            store.append(source="market", payload={"price": 101}, observed_at=t0 + timedelta(hours=2))
            self.assertEqual(store.as_of(t0 + timedelta(hours=1))["payload"]["price"], 100)
            with self.assertRaises(ValueError):
                store.append(
                    source="future", payload={}, observed_at=t0,
                    effective_at=t0 + timedelta(minutes=1),
                )


class WalkForwardValidationTests(unittest.TestCase):
    def test_splits_keep_purge_gap_and_rule_is_oos_evaluated(self):
        splits = purged_walk_forward_splits(240, train_size=80, test_size=40, purge=5, embargo=3)
        self.assertTrue(splits)
        self.assertTrue(all(split.test_start - split.train_end == 5 for split in splits))
        rows = []
        close = 100.0
        for i in range(240):
            close *= 1.002
            rows.append({"close": close, "signal": i % 4 == 0, "time_key": i})
        result = evaluate_rule_walk_forward(
            {"action": "BUY", "hold": 3}, rows,
            lambda row, rule: row["signal"], train_size=80, test_size=40,
        )
        self.assertGreaterEqual(result["fold_count"], 3)
        self.assertGreaterEqual(result["oos_n"], 5)
        self.assertTrue(result["passed"])


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
