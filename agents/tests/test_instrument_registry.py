"""WP01 (audit 2026-09-19): instrument_registry 是 ticker 元数据单一源.

锁死:
- normalize 归一化 (裸/带前缀/小写 → canonical)
- get 支持任意形式查找
- Frozen Instrument dataclass
- 派生的 ticker_to_sector_map / leveraged_option_price_map 一致
- 覆盖所有 tracked ticker
- 关键 semi 都在 SMH sector
- SOXL price_proxy=SOXX, options_proxy=SMH (audit F12)
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import instrument_registry as ir


class NormalizeTests(unittest.TestCase):

    def test_bare_ticker_gets_us_prefix(self):
        self.assertEqual(ir.normalize("SOXL"), "US.SOXL")

    def test_prefixed_ticker_unchanged(self):
        self.assertEqual(ir.normalize("US.SOXL"), "US.SOXL")

    def test_lowercase_uppercased(self):
        self.assertEqual(ir.normalize("soxl"), "US.SOXL")

    def test_hk_prefix_preserved(self):
        self.assertEqual(ir.normalize("HK.00700"), "HK.00700")

    def test_jp_prefix_preserved(self):
        self.assertEqual(ir.normalize("JP.7203"), "JP.7203")

    def test_empty_returns_empty(self):
        self.assertEqual(ir.normalize(""), "")
        self.assertEqual(ir.normalize(None), "")


class GetTests(unittest.TestCase):

    def test_get_by_canonical(self):
        i = ir.get("US.SOXL")
        self.assertIsNotNone(i)
        self.assertEqual(i.canonical, "US.SOXL")
        self.assertEqual(i.display, "SOXL")

    def test_get_by_bare(self):
        # F01 fix: 裸 SOXL 必须匹配到同一 Instrument
        i1 = ir.get("SOXL")
        i2 = ir.get("US.SOXL")
        self.assertIs(i1, i2, "SOXL 和 US.SOXL 必须返回同一 Instrument")

    def test_get_by_lowercase(self):
        i = ir.get("soxl")
        self.assertIsNotNone(i)
        self.assertEqual(i.canonical, "US.SOXL")

    def test_unknown_returns_none(self):
        self.assertIsNone(ir.get("US.NONEXISTENT_ZZZ"))


class InstrumentImmutabilityTests(unittest.TestCase):

    def test_instrument_is_frozen(self):
        i = ir.get("US.SOXL")
        with self.assertRaises(FrozenInstanceError):
            i.canonical = "changed"   # type: ignore


class LeveragedETFMetadataTests(unittest.TestCase):
    """audit F12 / WP08 关键: 杠杆 ETF 价格 anchor 是 underlying, 期权可用 proxy."""

    def test_soxl_is_3x_soxx_options_smh(self):
        i = ir.get("US.SOXL")
        self.assertEqual(i.leverage, 3.0)
        self.assertEqual(i.price_proxy, "US.SOXX",
                          "SOXL 价格必须锚 SOXX, 不能锚 SMH (SMH 期权更深但价格不同)")
        self.assertEqual(i.options_proxy, "US.SMH",
                          "SOXL 期权流用 SMH (更深流动性)")
        self.assertEqual(i.asset_class, "leveraged_etf")

    def test_tqqq_is_3x_qqq(self):
        i = ir.get("US.TQQQ")
        self.assertEqual(i.leverage, 3.0)
        self.assertEqual(i.price_proxy, "US.QQQ")
        self.assertEqual(i.options_proxy, "US.QQQ")

    def test_soxs_is_inverse(self):
        i = ir.get("US.SOXS")
        self.assertTrue(i.is_short)
        self.assertEqual(i.leverage, 3.0)   # |leverage|, is_short 标反向

    def test_is_leveraged_helper(self):
        self.assertTrue(ir.is_leveraged("SOXL"))
        self.assertTrue(ir.is_leveraged("TQQQ"))
        self.assertTrue(ir.is_leveraged("MULL"))
        self.assertFalse(ir.is_leveraged("NVDA"))
        self.assertFalse(ir.is_leveraged("US.GLD"))
        self.assertFalse(ir.is_leveraged("US.UNKNOWN_ZZZ"))

    def test_leverage_of_returns_1_for_unknown(self):
        self.assertEqual(ir.leverage_of("US.UNKNOWN_ZZZ"), 1.0)


class DerivedMapsTests(unittest.TestCase):
    """派生 API 与旧 hardcoded map 兼容 — 迁移 sanity."""

    def test_ticker_to_sector_map_covers_key_tickers(self):
        m = ir.ticker_to_sector_map()
        # 与旧 decision_agent.TICKER_TO_SECTOR 关键条目应一致
        self.assertEqual(m.get("US.SOXL"), "SMH")
        self.assertEqual(m.get("US.TQQQ"), "QQQ")
        self.assertEqual(m.get("US.NVDA"), "SMH")
        self.assertEqual(m.get("US.GLD"), "GLD")
        self.assertEqual(m.get("US.IEI"), "IEI")

    def test_leveraged_map_covers_tqqq_soxl(self):
        m = ir.leveraged_option_price_map()
        self.assertIn("TQQQ", m)
        self.assertEqual(m["TQQQ"]["source"], "QQQ")
        self.assertEqual(m["TQQQ"]["leverage"], 3.0)
        self.assertIn("SOXL", m)
        self.assertEqual(m["SOXL"]["source"], "SOXX")
        self.assertEqual(m["SOXL"]["leverage"], 3.0)

    def test_leveraged_map_excludes_1x(self):
        # NVDA leverage=1.0 → 不应在 leveraged_option_price_map 里
        m = ir.leveraged_option_price_map()
        self.assertNotIn("NVDA", m)


class UniverseCoverageTests(unittest.TestCase):
    """确保 signals/*_latest.json 里的 ticker 都在 registry."""

    def test_all_actively_traded_tickers_registered(self):
        # 参考 60d autopsy 里出现的 real tickers
        must_have = [
            "AAPL", "AMAT", "CBRS", "DRAM", "GLD", "GOOGL", "IEI",
            "KLAC", "LITE", "MSFT", "MU", "MULL", "NBIS", "NVDA",
            "QRVO", "SHY", "SOXL", "SOXS", "TQQQ", "USO", "XLV",
        ]
        for tk in must_have:
            with self.subTest(ticker=tk):
                self.assertIsNotNone(ir.get(tk),
                                       f"{tk} 应在 registry (audit universe 覆盖)")

    def test_expanded_blacklist_2026_09_18_all_in_registry(self):
        # F02 后加的 4 个 semi 必须有 registry entry
        for tk in ["QRVO", "SWKS", "MPWR", "STM"]:
            with self.subTest(ticker=tk):
                i = ir.get(tk)
                self.assertIsNotNone(i)
                self.assertEqual(i.sector_bucket, "SMH")


class FilterHelpersTests(unittest.TestCase):

    def test_list_by_class(self):
        levs = ir.list_by_class("leveraged_etf")
        canonical = {i.canonical for i in levs}
        self.assertIn("US.TQQQ", canonical)
        self.assertIn("US.SOXL", canonical)
        self.assertIn("US.SOXS", canonical)
        self.assertIn("US.MULL", canonical)

    def test_list_by_sector_smh(self):
        smh = ir.list_by_sector("SMH")
        # 应包含所有 semi (NVDA, KLAC, DRAM, MULL, etc)
        canonical = {i.canonical for i in smh}
        for tk in ["US.NVDA", "US.KLAC", "US.SOXL", "US.QRVO", "US.MPWR"]:
            self.assertIn(tk, canonical)


if __name__ == "__main__":
    unittest.main()
