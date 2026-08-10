from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import webui


class DashboardAssetProfileTests(unittest.TestCase):
    def test_macro_assets_share_the_simplified_capability_contract(self):
        expected_classes = {
            "GLD": "commodity",
            "SHY": "fixed_income",
            "IEI": "fixed_income",
        }
        for ticker, asset_class in expected_classes.items():
            with self.subTest(ticker=ticker):
                profile = webui.ticker_display_profile(ticker)
                self.assertEqual(profile["card_group"], "macro")
                self.assertEqual(profile["asset_class"], asset_class)
                self.assertFalse(profile["show_options"])
                self.assertFalse(profile["show_fundamentals"])
                self.assertFalse(profile["show_ai_analysis"])
                self.assertFalse(profile["show_supply_chain"])

    def test_unknown_ticker_keeps_full_stock_card_defaults(self):
        profile = webui.ticker_display_profile("US.TEST")
        self.assertEqual(profile["card_group"], "main")
        self.assertEqual(profile["asset_class"], "equity")
        self.assertTrue(profile["show_options"])
        self.assertTrue(profile["show_fundamentals"])
        self.assertTrue(profile["show_ai_analysis"])
        self.assertTrue(profile["show_supply_chain"])

    def test_api_signals_attaches_backend_profile_to_each_card(self):
        line = "2026-08-01 04:46:12 【SHY】价格:81.98  RSI:57.0  量比:0.38  趋势:up\n"
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "run.log"
            log_path.write_text(line, encoding="utf-8")
            with patch.object(webui, "_today_log_path", return_value=log_path):
                result = webui.api_signals()

        profile = result["tickers"]["SHY"]["display_profile"]
        self.assertEqual(profile["card_group"], "macro")
        self.assertFalse(profile["show_options"])

    def test_options_api_filters_macro_assets_from_legacy_cache(self):
        cached = {
            "tickers": {"GLD": {"spot": 1}, "SHY": {"spot": 2}, "TQQQ": {"spot": 3}},
            "_meta": {"stale": False},
        }
        with patch.object(webui, "_cached", return_value=cached):
            result = webui.api_ticker_options()
        self.assertEqual(set(result["tickers"]), {"TQQQ"})

    def test_option_cache_key_changes_with_visible_option_universe(self):
        with patch.object(webui, "TICKER_TO_OPTION_SOURCE", {"TQQQ": "QQQ"}):
            before = webui._ticker_options_cache_contract()
        with patch.object(
            webui,
            "TICKER_TO_OPTION_SOURCE",
            {"TQQQ": "QQQ", "LITE": "LITE"},
        ):
            after = webui._ticker_options_cache_contract()
        self.assertNotEqual(before, after)
        self.assertEqual(len(after), 12)

    def test_option_refresh_only_computes_newly_added_ticker(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            (cache_dir / "ticker_options.json").write_text(
                json.dumps({
                    "ts": "old",
                    "tickers": {"TQQQ": {"underlying": "QQQ", "spot": 100}},
                    "data_source": "old",
                }),
                encoding="utf-8",
            )
            lite = {
                "ts": "new",
                "tickers": {"LITE": {"underlying": "LITE", "spot": 868}},
            }
            with patch.object(webui, "_WEBUI_CACHE_DIR", cache_dir), \
                 patch.object(
                     webui,
                     "TICKER_TO_OPTION_SOURCE",
                     {"TQQQ": "QQQ", "LITE": "LITE"},
                 ), \
                 patch.object(webui, "_compute_ticker_options", return_value=lite) as compute:
                result = webui._compute_ticker_options_incremental()
                expected_contract = webui._ticker_options_cache_contract()
        compute.assert_called_once_with({"LITE": "LITE"})
        self.assertEqual(set(result["tickers"]), {"TQQQ", "LITE"})
        self.assertEqual(result["cache_contract"], expected_contract)

    def test_fundamentals_api_obeys_the_same_macro_capability_contract(self):
        for ticker in ("GLD", "SHY", "IEI"):
            with self.subTest(ticker=ticker):
                result = webui.api_fundamentals(ticker)
                self.assertEqual(result["error"], "no_fundamentals_for_this_type")

    def test_option_recompute_never_opens_macro_chains(self):
        opened: list[str] = []
        yfinance = types.ModuleType("yfinance")
        yfinance.Ticker = lambda symbol: opened.append(symbol)
        moomoo_data = types.ModuleType("moomoo_data")
        moomoo_data.health_check = lambda: {"available": False}
        moomoo_data.get_option_chain_via_openD = lambda _symbol: None
        sources = {"GLD": "GLD", "SHY": "SHY", "IEI": "IEI"}

        with patch.dict(sys.modules, {"yfinance": yfinance, "moomoo_data": moomoo_data}), \
             patch.object(webui, "TICKER_TO_OPTION_SOURCE", sources):
            result = webui._compute_ticker_options()

        self.assertEqual(opened, [])
        self.assertEqual(result["tickers"], {})

    def test_macro_assets_do_not_enter_live_or_snapshot_ai(self):
        macro_only = webui._ticker_ai_live_all({"GLD": {"price": 1}}, {})
        self.assertEqual(macro_only, {"tickers": {}, "state": "empty"})

        with patch.object(webui, "api_signals", return_value={"tickers": {}}), \
             patch.object(webui, "api_ticker_options", return_value={"tickers": {}}), \
             patch.object(
                 webui,
                 "_ticker_ai_live_all",
                 return_value={"tickers": {"TQQQ": "live", "GLD": "hidden"}, "state": "cached"},
             ), \
             patch.object(
                 webui,
                 "_ticker_ai_snapshot_parse",
                 return_value={"tickers": {"SHY": "hidden", "TQQQ": "old"}},
             ):
            result = webui.api_ticker_ai()

        self.assertEqual(result["tickers"], {"TQQQ": "live"})
        self.assertEqual(result["sources"], {"TQQQ": "live"})

    def test_ai_reads_the_current_period_qualified_fundamentals_cache(self):
        payload = {"ticker": "TQQQ", "latest": {"croic": 12.5}}
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            (cache_dir / "fundamentals_TQQQ_year.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            with patch.object(webui, "_WEBUI_CACHE_DIR", cache_dir):
                self.assertEqual(webui._read_fundamentals_cache_for_ai("TQQQ"), payload)
                self.assertIsNone(webui._read_fundamentals_cache_for_ai("GLD"))

    def test_ai_cache_key_tracks_fundamentals_and_schema_changes(self):
        signal = {"action_zh": "买入", "conf": 4, "scale": 5}
        options = {"spot": 100, "call_wall_oi": 500, "put_wall_oi": 250}
        first = [("TQQQ", signal, options, {"latest": {"croic": 10}})]
        revised = [("TQQQ", signal, options, {"latest": {"croic": 12}})]

        first_key = webui._ticker_ai_cache_key(first)
        self.assertNotEqual(first_key, webui._ticker_ai_cache_key(revised))
        with patch.object(webui, "_TICKER_AI_CACHE_SCHEMA", webui._TICKER_AI_CACHE_SCHEMA + 1):
            self.assertNotEqual(first_key, webui._ticker_ai_cache_key(first))


class DashboardMarkupContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (AGENTS_DIR / "dashboard.html").read_text(encoding="utf-8")

    def test_macro_section_and_backend_driven_grouping_exist(self):
        self.assertIn('id="macro-signals"', self.html)
        self.assertIn("profile.card_group === 'macro'", self.html)
        self.assertIn("(t.display_profile || {}).card_group === 'macro'", self.html)

    def test_advanced_modules_are_guarded_by_profile_capabilities(self):
        self.assertIn("showOptions ? renderMiniWall(opt) : ''", self.html)
        self.assertIn("showOptions ? renderOptAnalysis(opt) : ''", self.html)
        self.assertIn("showAI ? renderTickerAI(t, context) : ''", self.html)
        self.assertIn("const fundamentals = showFundamentals ?", self.html)
        self.assertIn("const supplyChain = showSupplyChain ?", self.html)


if __name__ == "__main__":
    unittest.main()
