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
                self.assertFalse(profile["show_forward_outlook"])
                self.assertFalse(profile["show_ai_analysis"])
                self.assertFalse(profile["show_supply_chain"])

    def test_unknown_ticker_keeps_full_stock_card_defaults(self):
        profile = webui.ticker_display_profile("US.TEST")
        self.assertEqual(profile["card_group"], "main")
        self.assertEqual(profile["asset_class"], "equity")
        self.assertTrue(profile["show_options"])
        self.assertTrue(profile["show_fundamentals"])
        self.assertTrue(profile["show_forward_outlook"])
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
                    "cache_schema": webui.TICKER_OPTIONS_CACHE_SCHEMA,
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

    def test_expired_same_contract_recomputes_full_option_universe(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(webui, "TICKER_TO_OPTION_SOURCE", {"TQQQ": "QQQ"}):
            cache_dir = Path(tmp)
            contract = webui._ticker_options_cache_contract()
            (cache_dir / "ticker_options.json").write_text(
                json.dumps({
                    "ts": "old",
                    "tickers": {"TQQQ": {"underlying": "QQQ", "spot": 100}},
                    "cache_schema": webui.TICKER_OPTIONS_CACHE_SCHEMA,
                    "cache_contract": contract,
                }),
                encoding="utf-8",
            )
            refreshed = {
                "ts": "new",
                "tickers": {"TQQQ": {"underlying": "QQQ", "spot": 101}},
                "cache_schema": webui.TICKER_OPTIONS_CACHE_SCHEMA,
                "cache_contract": contract,
            }
            with patch.object(webui, "_WEBUI_CACHE_DIR", cache_dir), \
                 patch.object(webui, "_compute_ticker_options", return_value=refreshed) as compute:
                result = webui._compute_ticker_options_incremental()

        compute.assert_called_once_with({"TQQQ": "QQQ"})
        self.assertEqual(result["ts"], "new")
        self.assertEqual(result["tickers"]["TQQQ"]["spot"], 101)

    def test_same_expiry_retains_last_good_gex_when_refresh_lacks_oi(self):
        previous = {
            "ts": "old-ts",
            "tickers": {
                "AAPL": {
                    "underlying": "AAPL",
                    "expiry": "2026-08-21",
                    "gex_analysis": {
                        "gex": {"total_gex_millions": 8.0},
                        "stock_verdict": {"summary": "valid"},
                    },
                }
            },
        }
        refreshed = {
            "ts": "new-ts",
            "tickers": {
                "AAPL": {
                    "underlying": "AAPL",
                    "expiry": "2026-08-21",
                    "gex_analysis": {"error": "insufficient_open_interest"},
                }
            },
        }

        result = webui._retain_last_known_good_gex(previous, refreshed)
        current = result["tickers"]["AAPL"]

        self.assertEqual(current["gex_analysis"]["stock_verdict"]["summary"], "valid")
        self.assertTrue(current["gex_analysis"]["stale"])
        self.assertEqual(current["gex_analysis"]["stale_reason"], "insufficient_open_interest")
        self.assertEqual(current["gex_analysis"]["stale_as_of"], "old-ts")
        self.assertEqual(
            refreshed["tickers"]["AAPL"]["gex_analysis"],
            {"error": "insufficient_open_interest"},
        )

    def test_changed_expiry_never_reuses_old_gex(self):
        previous = {
            "ts": "old-ts",
            "tickers": {
                "AAPL": {
                    "underlying": "AAPL",
                    "expiry": "2026-08-21",
                    "gex_analysis": {
                        "gex": {"total_gex_millions": 8.0},
                        "stock_verdict": {"summary": "valid"},
                    },
                }
            },
        }
        refreshed = {
            "ts": "new-ts",
            "tickers": {
                "AAPL": {
                    "underlying": "AAPL",
                    "expiry": "2026-08-28",
                    "gex_analysis": {"error": "insufficient_open_interest"},
                }
            },
        }

        result = webui._retain_last_known_good_gex(previous, refreshed)

        self.assertEqual(
            result["tickers"]["AAPL"]["gex_analysis"],
            {"error": "insufficient_open_interest"},
        )

    def test_fundamentals_api_obeys_the_same_macro_capability_contract(self):
        for ticker in ("GLD", "SHY", "IEI"):
            with self.subTest(ticker=ticker):
                result = webui.api_fundamentals(ticker)
                self.assertEqual(result["error"], "no_fundamentals_for_this_type")

    def test_forward_outlook_obeys_the_same_macro_capability_contract(self):
        for ticker in ("GLD", "SHY", "IEI"):
            with self.subTest(ticker=ticker):
                result = webui.api_equity_outlook(ticker)
                self.assertEqual(result["error"], "no_forward_outlook_for_this_type")

    def test_expectation_pricing_classifies_underpriced_consensus(self):
        consensus = {
            "current_price": 100,
            "trailing_eps": 5,
            "eps_growth_pct": 20,
            "hist_pe_median": 20,
            "n_analysts": 12,
            "target_mean": 125,
            "target_upside_pct": 25,
            "implied_pe_hold": 120,
            "implied_pe_hold_upside": 20,
            "implied_hist_median": 122,
            "implied_hist_upside": 22,
            "forward_estimates": {
                "revision_summary": {"signal": "upward"},
            },
        }

        pricing = webui._build_expectation_pricing(consensus)

        self.assertEqual(pricing["classification"], "underpriced")
        self.assertEqual(pricing["label"], "市场预期偏低")
        self.assertEqual(pricing["revision_signal"], "upward")
        self.assertEqual(pricing["anchor_count"], 3)

    def test_expectation_pricing_flags_price_above_forward_anchors(self):
        consensus = {
            "current_price": 150,
            "trailing_eps": 5,
            "eps_growth_pct": 5,
            "hist_pe_median": 20,
            "n_analysts": 10,
            "target_mean": 120,
            "target_upside_pct": -20,
            "implied_pe_hold": 118,
            "implied_pe_hold_upside": -21.3,
            "implied_hist_median": 115,
            "implied_hist_upside": -23.3,
            "forward_estimates": {
                "revision_summary": {"signal": "downward"},
            },
        }

        pricing = webui._build_expectation_pricing(consensus)

        self.assertEqual(pricing["classification"], "overpriced")
        self.assertEqual(pricing["label"], "预期透支")
        self.assertLess(pricing["median_gap_pct"], -15)

    def test_revision_summary_marks_eps_value_and_breadth_conflict_as_mixed(self):
        class FakeFrame:
            empty = False

            def __init__(self, rows):
                self.rows = rows

            def iterrows(self):
                return iter(self.rows)

        class FakeTicker:
            earnings_estimate = None
            revenue_estimate = None
            eps_trend = FakeFrame([
                ("+1y", {"current": 9.86, "30daysAgo": 10.0}),
            ])
            eps_revisions = FakeFrame([
                ("+1y", {"upLast30days": 8, "downLast30days": 3}),
            ])

        summary = webui._fetch_forward_estimate_snapshot(FakeTicker())["revision_summary"]

        self.assertEqual(summary["trend_signal"], "downward")
        self.assertEqual(summary["breadth_signal"], "upward")
        self.assertEqual(summary["signal"], "mixed")

    def test_equity_outlook_labels_proxy_without_using_etf_as_price_anchor(self):
        ready = {
            "source_stock": "NVDA",
            "state": "ready",
            "analyst_consensus": {"current_price": 190},
            "pricing": {"classification": "matched"},
        }
        with patch.object(webui, "_cached", return_value=ready) as cached:
            result = webui.api_equity_outlook("TQQQ")

        self.assertTrue(result["is_proxy"])
        self.assertEqual(result["source_stock"], "NVDA")
        self.assertEqual(result["analyst_consensus"]["current_price"], 190)
        self.assertEqual(cached.call_args.args[0], webui._equity_outlook_cache_name("NVDA"))

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

    def test_ai_cache_key_tracks_forward_outlook_changes(self):
        signal = {"action_zh": "买入", "conf": 4, "scale": 5}
        options = {"spot": 100}
        fundamentals = {"latest": {"croic": 10}}
        first = [("AAPL", signal, options, fundamentals, {
            "source_stock": "AAPL",
            "pricing": {"classification": "matched", "median_gap_pct": 1},
            "analyst_consensus": {"forward_eps": 8},
        })]
        revised = [("AAPL", signal, options, fundamentals, {
            "source_stock": "AAPL",
            "pricing": {"classification": "overpriced", "median_gap_pct": -20},
            "analyst_consensus": {"forward_eps": 7},
        })]

        self.assertNotEqual(
            webui._ticker_ai_cache_key(first),
            webui._ticker_ai_cache_key(revised),
        )


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
        self.assertIn("const forwardOutlook = showForwardOutlook ?", self.html)
        self.assertIn("const supplyChain = showSupplyChain ?", self.html)

    def test_forward_outlook_is_visible_and_discloses_proxy_scope(self):
        self.assertIn("前瞻与预期定价", self.html)
        self.assertIn("不能当作 ${ticker} 的目标价", self.html)
        self.assertIn("本模块用于解释预期，不单独触发交易", self.html)

    def test_stale_gex_is_visibly_disclosed(self):
        self.assertIn("GEX 为上一份有效数据", self.html)
        self.assertIn("ga.stale_reason", self.html)


if __name__ == "__main__":
    unittest.main()
