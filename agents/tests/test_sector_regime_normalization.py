"""F01 regression (per audit 2026-09-19): sector_regime.classify_ticker_sector 必须
接受裸 ticker (SOXL) 和 US.SOXL 都能匹配到同一板块.

之前 bug: market_watch 返裸 'SOXL' 但 TICKER_TO_SECTOR 用 'US.SOXL' → get() 返 None,
导致 UI/策略/回测在同一 ticker 得到不同板块约束.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import sector_regime


class TickerNormalizationTests(unittest.TestCase):

    def setUp(self):
        # 用最小假 map + patch classify_sector 避免调 yfinance
        self.mapping = {
            "US.SOXL": "SMH",
            "US.TQQQ": "QQQ",
            "US.GLD":  "GLD",
        }
        self._patch = patch.object(sector_regime, "classify_sector",
                                    return_value={"regime": "sector_bull"})
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_prefixed_ticker_matches(self):
        r = sector_regime.classify_ticker_sector("US.SOXL", self.mapping)
        self.assertEqual(r, "sector_bull")

    def test_bare_ticker_matches_prefixed_map(self):
        # F01 fix: 裸 'SOXL' 应匹配 map 里的 'US.SOXL'
        r = sector_regime.classify_ticker_sector("SOXL", self.mapping)
        self.assertEqual(r, "sector_bull", "bare SOXL 必须与 US.SOXL 归一")

    def test_unknown_ticker_returns_none(self):
        r = sector_regime.classify_ticker_sector("ZZZUNKNOWN", self.mapping)
        self.assertIsNone(r)

    def test_lowercase_ticker_matches(self):
        # 顺手做 case-insensitive: 'soxl' 应与 US.SOXL 归一
        r = sector_regime.classify_ticker_sector("soxl", self.mapping)
        self.assertEqual(r, "sector_bull")

    def test_map_with_bare_key_matches_prefixed_input(self):
        # 反向: map 用裸 key, 输入带前缀 → 也应匹配
        bare_map = {"SOXL": "SMH"}
        r = sector_regime.classify_ticker_sector("US.SOXL", bare_map)
        self.assertEqual(r, "sector_bull")

    def test_empty_inputs_return_none(self):
        self.assertIsNone(sector_regime.classify_ticker_sector("", self.mapping))
        self.assertIsNone(sector_regime.classify_ticker_sector("US.SOXL", {}))


if __name__ == "__main__":
    unittest.main()
