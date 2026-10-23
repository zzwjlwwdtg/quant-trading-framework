"""52 周指标 warm-up 回归.

350 个日历日只有 ~240 个交易日, rolling(252) 全 NaN → 校准里
new_high_healthy / new_high_diverge 永远不触发. 评估路径必须额外加载
warm-up 数据, 且 warm-up 期不得伪造新高.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import backtest_engine  # noqa: E402


def _frame(periods: int = 300, *, high_equals_close: bool = True) -> pd.DataFrame:
    idx = pd.bdate_range(end="2026-10-21", periods=periods)
    # 需要真实涨跌交替, 否则 RSI 的 loss 恒 0 会全 NaN
    close = pd.Series(95.0 + 5.0 * np.sin(np.arange(periods) / 6.0), index=idx)
    close.iloc[-1] = float(close.max()) + 1.0
    high = close if high_equals_close else close + 0.5
    return pd.DataFrame({
        "open": close - 0.2,
        "high": high,
        "low": close - 0.6,
        "close": close,
        "volume": [1_000_000.0] * periods,
    })


class LoadHistoryWarmupTests(unittest.TestCase):

    def test_warmup_is_added_to_download_period(self):
        calls = {}

        class _Ticker:
            def history(self, **kwargs):
                calls.update(kwargs)
                return pd.DataFrame()

        class _FakeYF:
            @staticmethod
            def Ticker(ticker):
                return _Ticker()

        with patch.dict(sys.modules, {"yfinance": _FakeYF()}):
            backtest_engine.load_history("TQQQ", days=110, warmup_days=400)
        self.assertEqual(calls["period"], "510d")

    def test_no_warmup_keeps_existing_contract(self):
        calls = {}

        class _Ticker:
            def history(self, **kwargs):
                calls.update(kwargs)
                return pd.DataFrame()

        class _FakeYF:
            @staticmethod
            def Ticker(ticker):
                return _Ticker()

        with patch.dict(sys.modules, {"yfinance": _FakeYF()}):
            backtest_engine.load_history("TQQQ", days=350)
        self.assertEqual(calls["period"], "350d")


class Indicators52WeekTests(unittest.TestCase):

    def test_high_52w_requires_full_window(self):
        out = backtest_engine.add_indicators(_frame())
        self.assertTrue(pd.isna(out["high_52w"].iloc[250]),
                        "不足 252 行时必须保持 NaN, 不能伪造 52 周高")
        self.assertFalse(pd.isna(out["high_52w"].iloc[251]))
        self.assertFalse(pd.isna(out["dist_from_high"].iloc[-1]))

    def test_new_high_can_fire_after_warmup(self):
        out = backtest_engine.add_indicators(_frame())
        # high == close 且价格单调上行 → 最后一日是真实 52 周新高
        self.assertEqual(int(out["is_new_52w_high"].iloc[-1]), 1)
        self.assertEqual(int(out["is_new_52w_high"].iloc[10]), 0)


class StrategyEngineWarmupTests(unittest.TestCase):

    def test_no_fake_new_high_during_warmup(self):
        import strategy_engine
        out = strategy_engine._compute_indicators(_frame())
        self.assertFalse(bool(out["is_new_52w_high"].iloc[0]),
                         "warm-up 期不得用 fillna(close) 伪造新高")
        self.assertTrue(bool(out["is_new_52w_high"].iloc[-1]))


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
