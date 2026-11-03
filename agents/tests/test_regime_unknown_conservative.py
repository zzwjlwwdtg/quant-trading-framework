"""2026-11-02 审计 P2-6 / P2-3: SOX 输入缺失 → unknown, 下游统一按保守 risk_off."""
import unittest
from unittest.mock import patch

import decision_agent as da
import paper_trader as pt
import regime_today as rt

FULL = {"sox_mkt_today_pct": -1.0, "sox_mkt_5d_pct": -2.0, "sox_mkt_20d_pct": -3.0,
        "spy_today_pct": -0.5, "vix": 18, "fg": 40, "t10y2y": 0.4}


class ClassifyMissingInputs(unittest.TestCase):
    def test_missing_sox_multi_period_is_unknown(self):
        for k in ("sox_mkt_5d_pct", "sox_mkt_20d_pct"):
            self.assertEqual(rt._classify({**FULL, k: None}), "unknown", k)

    def test_missing_both_today_inputs_is_unknown(self):
        self.assertEqual(rt._classify({**FULL, "sox_mkt_today_pct": None, "spy_today_pct": None}),
                         "unknown")

    def test_spy_only_today_still_classifies(self):
        self.assertNotEqual(rt._classify({**FULL, "sox_mkt_today_pct": None}), "unknown")

    def test_zero_values_are_not_missing(self):
        self.assertNotEqual(rt._classify({**FULL, "sox_mkt_5d_pct": 0.0, "sox_mkt_20d_pct": 0.0}),
                            "unknown")


class ConservativeMapping(unittest.TestCase):
    def test_helper(self):
        for r in (None, "", "unknown", "UNKNOWN"):
            self.assertEqual(rt.conservative_if_unknown(r), "risk_off", r)
        self.assertEqual(rt.conservative_if_unknown("bull_trending"), "bull_trending")

    def test_decision_maps_unknown_board_but_keeps_none_for_backtests(self):
        self.assertEqual(da._unknown_board_to_conservative("unknown", {"ticker": "US.X"}), "risk_off")
        self.assertIsNone(da._unknown_board_to_conservative(None, {"ticker": "US.X"}))
        self.assertEqual(da._unknown_board_to_conservative("crisis", {}), "crisis")

    def test_sizing_uses_risk_off_vol_when_unknown(self):
        self.assertLess(pt.TARGET_PORT_VOL["risk_off"], pt.TARGET_PORT_VOL["neutral"])
        src = open(pt.__file__, encoding="utf-8").read()
        self.assertIn("regime = conservative_if_unknown(get_today_regime())", src)


class BoardCrisisAlertOnce(unittest.TestCase):
    def test_board_crisis_alert_is_single_summary(self):
        import inspect
        import orchestrator
        src = inspect.getsource(orchestrator._etf_cycle)
        self.assertIn('if board_regime == "crisis":', src)
        self.assertIn('notify_crisis("系统级(板块)"', src)


if __name__ == "__main__":
    raise SystemExit("use: python -m unittest discover -s tests -t .")
