from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd


AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from gex_calc import compute_gex, generate_stock_verdict


def _chain(strike: float, oi: int, iv: float = 0.30) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "strike": strike,
            "openInterest": oi,
            "impliedVolatility": iv,
        }
    ])


class GammaFlipRegressionTests(unittest.TestCase):
    def test_chain_without_open_interest_is_data_error_not_neutral_regime(self):
        calls = _chain(105.0, 0)
        puts = _chain(95.0, 0)

        result = compute_gex(calls, puts, spot=100.0, days_to_expiry=30)

        self.assertEqual(result, {"error": "insufficient_open_interest"})

    def test_chain_without_usable_iv_is_data_error_not_neutral_regime(self):
        calls = _chain(105.0, 1_000, iv=0.0)
        puts = _chain(95.0, 1_000, iv=0.0)

        result = compute_gex(calls, puts, spot=100.0, days_to_expiry=30)

        self.assertEqual(result, {"error": "insufficient_gamma_inputs"})

    def test_flip_is_zero_crossing_of_repriced_total_gex(self):
        calls = _chain(105.0, 2_000)
        puts = _chain(95.0, 2_000)

        result = compute_gex(calls, puts, spot=100.0, days_to_expiry=30)

        # The repriced whole-chain exposure crosses zero around 99.1.  The old
        # cumulative-by-strike implementation incorrectly returned strike 105.
        self.assertGreater(result["gamma_flip_strike"], 98.0)
        self.assertLess(result["gamma_flip_strike"], 100.0)
        self.assertEqual(result["gamma_flip_method"], "repriced_total_gex")

    def test_negative_total_gex_cannot_be_classified_positive_pin(self):
        calls = _chain(105.0, 100)
        puts = _chain(95.0, 5_000)

        result = compute_gex(calls, puts, spot=100.0, days_to_expiry=30)

        self.assertLess(result["total_gex_millions"], 0)
        self.assertEqual(result["regime"], "negative_squeeze")
        self.assertIsNone(result["gamma_flip_strike"])
        self.assertIsNone(result["spot_vs_flip_pct"])

    def test_no_flip_does_not_invent_a_lower_boundary_pin(self):
        calls = _chain(105.0, 5_000)
        puts = _chain(95.0, 100)

        result = compute_gex(calls, puts, spot=100.0, days_to_expiry=30)

        self.assertGreater(result["total_gex_millions"], 0)
        self.assertEqual(result["regime"], "positive_pin")
        self.assertIsNone(result["gamma_flip_strike"])
        self.assertEqual(result["gamma_flip_status"], "not_found_in_chain_range")

    def test_positive_gex_near_put_wall_can_surface_a_buy_opportunity_without_flip(self):
        verdict = generate_stock_verdict(
            gex={
                "regime": "positive_pin",
                "gamma_flip_strike": None,
                "spot_vs_flip_pct": None,
            },
            iv={"regime": "cheap", "iv_premium_pct": -20},
            skew={"regime": "flat", "skew_pct": 2},
            spot=100.0,
            call_wall_strike=106.0,
            put_wall_strike=98.0,
        )

        self.assertEqual(verdict["opportunities"]["buy_now"]["level"], "good")
        self.assertEqual(verdict["opportunities"]["add_more"]["level"], "good")
        self.assertEqual(verdict["key_prices"]["lower_support"], 98.0)

    def test_negative_gex_without_flip_surfaces_risk_instead_of_crashing(self):
        verdict = generate_stock_verdict(
            gex={
                "regime": "negative_squeeze",
                "gamma_flip_strike": None,
                "spot_vs_flip_pct": None,
            },
            iv={"regime": "normal", "iv_premium_pct": 0},
            skew={"regime": "flat", "skew_pct": 2},
            spot=100.0,
            call_wall_strike=103.0,
            put_wall_strike=96.0,
        )

        self.assertEqual(verdict["opportunities"]["buy_now"]["level"], "none")
        self.assertEqual(verdict["opportunities"]["reduce"]["level"], "good")
        self.assertIn("$103.0", verdict["key_prices"]["break_watch"])


if __name__ == "__main__":
    unittest.main()
