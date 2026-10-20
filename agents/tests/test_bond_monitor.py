from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path


AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import bond_monitor


class BondMonitorRegressionTests(unittest.TestCase):
    def test_yahoo_yield_indexes_are_already_percentage_values(self):
        self.assertEqual(bond_monitor._YF_YIELDS["10y"]["scale"], 1.0)
        self.assertEqual(bond_monitor._YF_YIELDS["5y"]["scale"], 1.0)
        self.assertEqual(bond_monitor._YF_YIELDS["30y"]["scale"], 1.0)

    def test_rolling_correlation_drops_non_finite_pairs(self):
        left = [1, 2, math.nan, 4, 5, 6, 7]
        right = [2, 4, 6, math.inf, 10, 12, 14]
        self.assertEqual(bond_monitor._rolling_corr(left, right), 1.0)

    def test_non_finite_correlation_is_unknown_not_broken(self):
        regime, reason = bond_monitor._correlation_regime(math.nan)
        self.assertEqual(regime, "unknown")
        self.assertIn("数据不足", reason)

    def test_json_safe_payload_is_strict_json_serializable(self):
        payload = bond_monitor._json_safe({
            "corr": math.nan,
            "nested": [1.0, math.inf, -math.inf],
        })
        encoded = json.dumps(payload, allow_nan=False)
        self.assertEqual(json.loads(encoded), {
            "corr": None,
            "nested": [1.0, None, None],
        })

    def test_yield_metrics_ignore_nan_observations(self):
        metrics = bond_monitor._compute_yield_metrics(
            [4.7, math.nan, 4.68, 4.65, 4.64, 4.60]
        )
        self.assertEqual(metrics["value"], 4.7)
        self.assertTrue(math.isfinite(metrics["chg_1d_bps"]))


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
