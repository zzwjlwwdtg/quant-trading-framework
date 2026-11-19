"""止损距离研究的选参和独立检验。"""
import unittest

import stop_gap_research as research


def _rows(n, entry_date, stop, pnl=-0.02, prefix="sample"):
    return [
        {
            "sample_id": f"{prefix}-{i}",
            "entry_date": entry_date,
            "exit_date": entry_date,
            "entry_price": 100,
            "stop_line": stop,
            "pnl_pct": pnl,
            "exit_reason": "stop",
        }
        for i in range(n)
    ]


class StopGapResearchTests(unittest.TestCase):
    def test_training_selects_only_eligible_threshold(self):
        rows = _rows(30, "2025-01-02", 99.5)
        rows += _rows(30, "2025-01-02", 98.3, 0.01, "wider")
        result = research.select_training(rows)
        self.assertEqual(result["selected_threshold"], 0.01)
        self.assertEqual(result["training"][0.01]["n"], 30)
        self.assertAlmostEqual(result["training"][0.02]["avg_pnl"], -0.005)

    def test_insufficient_training_does_not_select(self):
        result = research.select_training(_rows(29, "2025-01-02", 99.5))
        self.assertIsNone(result["selected_threshold"])

    def test_oos_checks_frozen_threshold_and_stays_disabled(self):
        result = research.evaluate_oos(_rows(30, "2026-07-01", 99.5), 0.01)
        self.assertTrue(result["oos_pass"])
        self.assertFalse(result["trading_enabled"])
        self.assertFalse(research.evaluate_oos(
            _rows(29, "2026-07-01", 99.5), 0.01
        )["oos_pass"])

    def test_boundary_is_strict_and_zero_pnl_is_not_a_win(self):
        rows = _rows(30, "2026-07-01", 99, 0)
        result = research.evaluate_oos(rows, 0.01)
        self.assertEqual(result["oos"]["n"], 0)
        result = research.evaluate_oos(rows, 0.015)
        self.assertEqual(result["oos"]["win_rate"], 0)
        self.assertFalse(result["oos_pass"])

    def test_invalid_samples_and_unfrozen_threshold_are_rejected(self):
        with self.assertRaises(ValueError):
            research.select_training(_rows(1, "2026-06-01", 99))
        with self.assertRaises(ValueError):
            research.evaluate_oos([], 0.012)
        rows = _rows(1, "2026-07-01", 99)
        rows[0]["entry_price"] = float("nan")
        with self.assertRaises(ValueError):
            research.evaluate_oos(rows, 0.01)
        rows = _rows(1, "2026-07-01", 99)
        with self.assertRaises(ValueError):
            research.evaluate_oos(rows + rows, 0.01)


if __name__ == "__main__":
    unittest.main()
