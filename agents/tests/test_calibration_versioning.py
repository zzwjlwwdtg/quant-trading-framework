"""F10 lite (audit 2026-09-19): calibration versioning + stale warning.

2026-08-11 calib 覆盖 5 ticker (TQQQ/SOXL/DRAM/MULL/GLD), 但 tracked universe
后续加了 MSFT/GOOGL/AAPL/NBIS/IEI/SHY 等. 需要 dashboard/audit 看得到 (a) 校准
何时训练, (b) 覆盖多少 ticker, (c) 是否 stale.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import decision_agent


class CalibrationInfoTests(unittest.TestCase):

    def test_missing_calibration_reports_stale(self):
        with patch.object(decision_agent, "_load_calibration", return_value=None):
            info = decision_agent.get_calibration_info()
            self.assertFalse(info["exists"])
            self.assertTrue(info["is_stale"])
            self.assertEqual(info["reason"], "no_calibration_file")

    def test_fresh_calibration_not_stale(self):
        # ts 3 天前
        fresh_ts = (datetime.now() - timedelta(days=3)).isoformat()
        fake_data = {"ts": fresh_ts, "tickers": ["A", "B"], "lookback_days": 250, "forward_days": 5}
        with patch.object(decision_agent, "_load_calibration", return_value=fake_data):
            info = decision_agent.get_calibration_info()
            self.assertTrue(info["exists"])
            self.assertFalse(info["is_stale"])
            self.assertLessEqual(info["age_days"], 4)

    def test_60d_old_calibration_flagged_stale(self):
        # ts 90 天前
        old_ts = (datetime.now() - timedelta(days=90)).isoformat()
        fake_data = {"ts": old_ts, "tickers": ["A"], "lookback_days": 250, "forward_days": 5}
        with patch.object(decision_agent, "_load_calibration", return_value=fake_data):
            info = decision_agent.get_calibration_info()
            self.assertTrue(info["exists"])
            self.assertTrue(info["is_stale"])
            self.assertGreater(info["age_days"], 60)

    def test_covered_tickers_exposed(self):
        fake_data = {"ts": datetime.now().isoformat(),
                      "tickers": ["US.TQQQ", "US.SOXL", "US.GLD"]}
        with patch.object(decision_agent, "_load_calibration", return_value=fake_data):
            info = decision_agent.get_calibration_info()
            self.assertEqual(len(info["covered_tickers"]), 3)
            self.assertIn("US.TQQQ", info["covered_tickers"])

    def test_warn_stale_only_fires_once(self):
        # 多次调用 _warn_stale_calibration_once 应只 log 一次
        decision_agent._CALIB_STALE_WARNED["done"] = False
        old_data = {"ts": (datetime.now() - timedelta(days=100)).isoformat(),
                     "tickers": []}
        call_count = {"n": 0}
        def fake_warning(msg):
            call_count["n"] += 1
        fake_logger = type("Fake", (), {"warning": staticmethod(fake_warning),
                                          "info": staticmethod(lambda m: None),
                                          "error": staticmethod(lambda m: None)})()
        with patch.object(decision_agent, "_load_calibration", return_value=old_data):
            with patch.dict(sys.modules, {"notifier": type("N", (), {"logger": fake_logger})()}):
                decision_agent._warn_stale_calibration_once()
                decision_agent._warn_stale_calibration_once()
                decision_agent._warn_stale_calibration_once()
        # Reset for other tests
        decision_agent._CALIB_STALE_WARNED["done"] = False
        # 至多一次 (第一次调用后 done=True)
        self.assertLessEqual(call_count["n"], 1)


if __name__ == "__main__":
    unittest.main()
