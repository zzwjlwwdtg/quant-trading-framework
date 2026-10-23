"""quote_ts 生产链路: moomoo snapshot update_time 必须进入 market/futures 信号.

回归背景: 两个 watcher 之前都把 ts 写成 datetime.now(), broker 行情自身的
update_time 被丢弃, 下游 data-quality 门禁无法识别过期报价.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import futures_watch  # noqa: E402
import market_watch  # noqa: E402


def _kline(periods: int = 60) -> pd.DataFrame:
    idx = pd.bdate_range(end="2026-10-21", periods=periods)
    close = pd.Series([90.0 + i * 0.2 for i in range(periods)])
    return pd.DataFrame({
        "time_key": idx.strftime("%Y-%m-%d %H:%M:%S"),
        "open": close - 0.1,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": [1_000_000.0] * periods,
    })


class _FakeQuoteCtx:
    def __init__(self, snapshot: pd.DataFrame, kline: pd.DataFrame):
        self.snapshot = snapshot
        self.kline = kline

    def get_market_snapshot(self, tickers):
        return 0, self.snapshot

    def request_history_kline(self, *args, **kwargs):
        return 0, self.kline, None


class MarketWatchQuoteTsTests(unittest.TestCase):

    def _snapshot(self, *, with_update: bool = True) -> pd.DataFrame:
        row = {
            "last_price": 101.0,
            "prev_close_price": 100.0,
            "volume": 2_000_000.0,
            "highest52weeks_price": 120.0,
            "lowest52weeks_price": 60.0,
            "bid_price": 100.99,
            "ask_price": 101.01,
        }
        if with_update:
            row["update_time"] = "2026-10-22 12:28:37"
        return pd.DataFrame([row])

    def _signal(self, snapshot: pd.DataFrame) -> dict:
        ctx = _FakeQuoteCtx(snapshot, _kline())
        with patch.object(market_watch, "get_quote_ctx", return_value=ctx), \
                patch.object(market_watch, "_enrich_commodity", None):
            return market_watch.get_market_signal("US.TQQQ")

    def test_quote_ts_wired_from_update_time(self):
        mkt = self._signal(self._snapshot())
        self.assertNotIn("error", mkt)
        self.assertEqual(mkt["quote_ts"], "2026-10-22T12:28:37-04:00")

    def test_missing_update_time_sets_none_not_generation_time(self):
        mkt = self._signal(self._snapshot(with_update=False))
        self.assertNotIn("error", mkt)
        self.assertIn("quote_ts", mkt)
        self.assertIsNone(mkt["quote_ts"])


class FuturesWatchQuoteTsTests(unittest.TestCase):

    def _snapshot(self, *, with_update: bool = True) -> pd.DataFrame:
        row = {
            "last_price": 4100.0,
            "prev_close_price": 4090.0,
            "volume": 5000.0,
            "future_last_settle_price": 4095.0,
            "future_position": 1000,
            "future_position_change": 10,
            "highest52weeks_price": 4500.0,
            "lowest52weeks_price": 3000.0,
        }
        if with_update:
            row["update_time"] = "2026-10-22 12:28:37"
        return pd.DataFrame([row])

    def _signal(self, snapshot: pd.DataFrame) -> dict:
        ctx = _FakeQuoteCtx(snapshot, _kline())
        with patch.object(futures_watch, "get_quote_ctx", return_value=ctx):
            return futures_watch.get_futures_signal("US.GCmain")

    def test_quote_ts_wired_from_update_time(self):
        mkt = self._signal(self._snapshot())
        self.assertNotIn("error", mkt)
        self.assertEqual(mkt["quote_ts"], "2026-10-22T12:28:37-04:00")

    def test_missing_update_time_sets_none(self):
        mkt = self._signal(self._snapshot(with_update=False))
        self.assertNotIn("error", mkt)
        self.assertIsNone(mkt["quote_ts"])


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
