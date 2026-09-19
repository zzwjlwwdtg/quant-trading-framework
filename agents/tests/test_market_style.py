from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import market_style
import regime_today


def _ohlc(closes: list[float]) -> tuple[pd.Series, pd.Series, pd.Series]:
    close = pd.Series(closes, dtype=float)
    return close, close + 0.8, close - 0.8


class MarketStyleTests(unittest.TestCase):
    def test_ohlc_alignment_survives_column_missing_dates(self):
        """F07 regression (audit 2026-09-19): 若 high 缺 day 5 而 low 缺 day 10,
        分列 dropna 会返 same-length 但错位; joint dropna 应返回同一 index 交集."""
        import numpy as np
        close = pd.Series([100 + i * 0.3 for i in range(60)])
        high  = close + 0.5
        low   = close - 0.5
        # 制造独立 NaN pattern
        high  = high.copy();  high.iloc[5]  = np.nan
        low   = low.copy();   low.iloc[10]  = np.nan
        # 手工计算 expected 联合有效行数: 60 - 2 (day 5, day 10 各一) = 58
        c, h, l = market_style._align_ohlc(close, high, low)
        self.assertEqual(len(c), 58, "joint dropna 后应保留 58 行 (60 - 2 独立 NaN)")
        self.assertEqual(len(h), 58)
        self.assertEqual(len(l), 58)
        # 关键: 每一位置 high >= close >= low 应仍成立 (对齐正确)
        self.assertTrue((h >= c).all(), "high 必须 ≥ close on same day")
        self.assertTrue((c >= l).all(), "close 必须 ≥ low on same day")

    def test_directionless_reversals_are_chop_without_atr_spike(self):
        closes = [100 + (0.7 if i % 2 else -0.7) + i * 0.01 for i in range(60)]
        close, high, low = _ohlc(closes)
        result = market_style.analyze_price_style(close, high, low)

        self.assertTrue(result["is_choppy"])
        self.assertGreaterEqual(result["chop_score"], 3)
        self.assertIn(result["style"], {"chop", "chop_bull", "chop_weak"})
        self.assertLess(result["metrics"]["atr_5_20_ratio"], 1.2)

    def test_clean_uptrend_is_not_mislabeled_as_chop(self):
        close, high, low = _ohlc([100 + i * 0.5 for i in range(60)])
        result = market_style.analyze_price_style(close, high, low)

        self.assertFalse(result["is_choppy"])
        self.assertEqual(result["style"], "trend_up")
        self.assertGreater(result["metrics"]["efficiency_10"], 0.9)

    def test_chop_overlays_daily_regime_but_not_hmm(self):
        style = {"is_choppy": True}
        self.assertEqual(
            market_style.effective_board_regime("bull_trending", style),
            "bull_chop",
        )
        self.assertEqual(
            market_style.effective_board_regime("neutral", style),
            "neutral_chop",
        )
        self.assertEqual(
            market_style.effective_board_regime("crisis", style),
            "crisis",
        )

    def test_regime_today_uses_neutral_chop_as_effective_trading_premise(self):
        inputs = {
            "sox_mkt_today_pct": 0.0,
            "sox_mkt_5d_pct": -0.1,
            "sox_mkt_20d_pct": 0.1,
            "spy_today_pct": 0.0,
            "short_style": {"is_choppy": True},
        }
        fake_decision_agent = types.SimpleNamespace(get_regime=lambda macro, market: "neutral")
        with patch.dict(sys.modules, {"decision_agent": fake_decision_agent}):
            self.assertEqual(regime_today._classify(inputs), "neutral_chop")

    def test_price_selloff_is_not_mislabeled_as_macro_recession(self):
        inputs = {
            "sox_mkt_today_pct": -4.0,
            "sox_mkt_5d_pct": -0.4,
            "sox_mkt_20d_pct": -0.2,
            "spy_today_pct": -0.5,
            "t10y2y": 0.5,
            "short_style": {"is_choppy": True},
        }
        fake_decision_agent = types.SimpleNamespace(
            get_regime=lambda macro, market: "recession_risk"
        )
        with patch.dict(sys.modules, {"decision_agent": fake_decision_agent}):
            self.assertEqual(regime_today._classify(inputs), "risk_off")

    def test_dashboard_explicitly_says_hmm_is_not_a_buy_signal(self):
        html = (AGENTS_DIR / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("慢周期背景，不是当前买入或加仓信号", html)
        self.assertIn("③ 短线风格", html)


if __name__ == "__main__":
    unittest.main()
