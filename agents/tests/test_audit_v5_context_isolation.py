"""F4 audit followup (2026-09-23): 历史 Context 全链路冻结.
Board regime 缺失时不能 fallback live; confluence 计算不能读今日模块 _CALIB.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


class F4_BoardRegimeIsolation(unittest.TestCase):

    def test_backtest_missing_board_regime_does_not_read_live(self):
        import decision_agent as da
        from decision_context import DecisionContext

        ctx = DecisionContext(
            as_of=datetime(2020, 1, 1, tzinfo=timezone.utc),
            is_backtest=True,
            board_regime=None,
        )
        live_calls = {"n": 0}
        def fake_today_regime():
            live_calls["n"] += 1
            return "neutral_today"
        market = {"ticker": "US.MSFT", "price": 100, "pct_chg": 0, "trend": "up",
                   "ma_stack": "bull", "rsi_14": 60, "vol_ratio": 1.1}
        with patch("regime_today.get_today_regime", side_effect=fake_today_regime):
            # 直接调 get_decision, 不显式传 board_regime
            da.get_decision(market, {}, {}, confluence=None, context=ctx)
        self.assertEqual(live_calls["n"], 0,
                          f"F4: backtest 缺 board_regime 应 unavailable, 不能调 live, calls={live_calls['n']}")


class F4_ConfluenceCalibIsolation(unittest.TestCase):

    def test_historical_context_pins_confluence_calibration(self):
        import decision_agent as da
        import confluence as cf
        from decision_context import DecisionContext

        # historical context 明确无 calibration
        ctx = DecisionContext(
            as_of=datetime(2020, 1, 1, tzinfo=timezone.utc),
            is_backtest=True,
            board_regime="neutral",
            calibration_snapshot={},   # explicit empty
        )
        market = {"ticker": "US.MSFT", "price": 100, "pct_chg": 0, "trend": "up",
                   "ma_stack": "bull", "rsi_14": 60, "vol_ratio": 1.1}
        # 场景 A: 模块 _CALIB=None
        with patch.object(cf, "_CALIB", None):
            baseline = da.get_decision(market, {}, {}, confluence=None, context=ctx)
        # 场景 B: 模块 _CALIB 有内容 (模拟今日 file 存在)
        with patch.object(cf, "_CALIB",
                           {"bull_weights": {}, "bear_weights": {},
                            "per_class": {}}):
            other = da.get_decision(market, {}, {}, confluence=None, context=ctx)
        # 两次结果的 confluence.calibrated 必须一致 (由 context 冻结, 与 live file 无关)
        base_cal = (baseline.get("confluence") or {}).get("calibrated")
        other_cal = (other.get("confluence") or {}).get("calibrated")
        self.assertEqual(base_cal, other_cal,
                          f"F4: historical context 的 confluence.calibrated 被 live _CALIB 污染: "
                          f"baseline={base_cal}, other={other_cal}")
        # 由于 calibration_snapshot={} → 显式无 → calibrated=False
        self.assertFalse(base_cal,
                          f"F4: calibration_snapshot={{}} 应 → calibrated=False, actual={base_cal}")


if __name__ == "__main__":
    unittest.main()
