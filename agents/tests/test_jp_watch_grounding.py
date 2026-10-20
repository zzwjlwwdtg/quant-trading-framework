from __future__ import annotations

import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd


AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import webui


class JPGuidanceGroundingTests(unittest.TestCase):
    def test_mufg_official_guidance_has_verified_structured_values(self):
        guidance = webui._official_jp_guidance("MUFG")

        self.assertEqual(guidance["fiscal_year"], "FY2026")
        self.assertEqual(guidance["net_income_target_jpy"], 2_700_000_000_000)
        self.assertEqual(guidance["net_income_yoy_pct"], 11)
        self.assertEqual(guidance["guidance_effective_date"], "2026-05-15")
        self.assertEqual(guidance["source_published_at"], "2026-05-19")
        self.assertEqual(guidance["last_update"], "2026-05-15")
        self.assertEqual(guidance["earnings_date"], "2026-08-03")
        self.assertIs(guidance["source_verified"], True)
        self.assertEqual(guidance["revision_type"], "initial")

    def test_legacy_claude_cache_cannot_override_official_mufg_guidance(self):
        legacy_cache = {
            "ticker": "MUFG",
            "direction": "上修",
            "magnitude": "中",
            "last_update": "2026-05-15",
            "next_earnings": "2026-08-04",
            "guidance_note": "純利益予想 1.8兆円 (YoY +5%)",
            "revision_reason": "米金利上昇による海外 NIM 拡大",
            "source_verified": False,
            "revision_type": "raised",
        }

        with patch.object(webui, "_cached", return_value=legacy_cache):
            guidance = webui.api_jp_guidance("MUFG")

        self.assertEqual(guidance["fiscal_year"], "FY2026")
        self.assertEqual(guidance["net_income_target_jpy"], 2_700_000_000_000)
        self.assertEqual(guidance["net_income_yoy_pct"], 11)
        self.assertEqual(guidance["last_update"], "2026-05-15")
        self.assertEqual(guidance["earnings_date"], "2026-08-03")
        self.assertIs(guidance["source_verified"], True)
        self.assertEqual(guidance["revision_type"], "initial")
        self.assertNotIn("1.8兆円", guidance.get("guidance_note", ""))

    def test_earnings_event_status_uses_jst_calendar_day(self):
        # These UTC instants are respectively 2026-08-03 00:30 and
        # 2026-08-04 00:01 in Japan.
        today_utc = datetime(2026, 8, 2, 15, 30, tzinfo=timezone.utc)
        released_utc = datetime(2026, 8, 3, 15, 1, tzinfo=timezone.utc)

        self.assertEqual(
            webui._jp_earnings_event_state("2026-08-03", now=today_utc),
            "today",
        )
        self.assertEqual(
            webui._jp_earnings_event_state("2026-08-03", now=released_utc),
            "released",
        )
        released = webui._official_jp_guidance("MUFG", now=released_utc)
        self.assertIsNone(released["next_earnings"])
        self.assertEqual(released["earnings_event"]["state"], "released")
        self.assertIn("仍展示 2026-05-15", released["data_note"])
        self.assertFalse(released["current_verified"])
        self.assertEqual(released["source_status"], "awaiting_post_earnings_update")


class JPWatchTechnicalContractTests(unittest.TestCase):
    def test_ichimoku_senkou_spans_are_shifted_forward_26_periods(self):
        high = pd.Series([float(i + 10) for i in range(100)])
        low = high - 4.0

        ichimoku = webui._compute_ichimoku_series(high, low)
        tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
        kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2
        expected_a = ((tenkan + kijun) / 2).shift(26)
        expected_b = ((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26)

        pd.testing.assert_series_equal(
            ichimoku["senkou_a"], expected_a, check_names=False
        )
        pd.testing.assert_series_equal(
            ichimoku["senkou_b"], expected_b, check_names=False
        )
        position, direction = webui._classify_ichimoku_position(
            float(high.iloc[-1] - 1),
            float(ichimoku["senkou_a"].iloc[-1]),
            float(ichimoku["senkou_b"].iloc[-1]),
        )
        self.assertEqual((position, direction), ("云上", "bullish"))


class JPThesisContractTests(unittest.TestCase):
    def test_mufg_is_independent_with_mixed_q3_thesis_compatibility(self):
        mufg = next(row for row in webui.JP_WATCH_LIST if row["ticker"] == "MUFG")

        self.assertEqual(mufg["thesis_fit"]["classification"], "independent")
        self.assertEqual(mufg["thesis_fit"]["compatibility"], "mixed")

    def test_q3_rates_thesis_expresses_two_and_five_years_not_long_end(self):
        rates = webui.Q3_MACRO_THESIS["rates"]

        self.assertEqual(set(rates["preferred_tenors"]), {"2Y", "5Y"})
        self.assertIn("10Y+", rates["avoid_tenors"])
        self.assertEqual(rates["invalidation_window"], "2026-08 release")

        cloud = webui.Q3_MACRO_THESIS["cloud"]
        self.assertIn("GOOG", cloud["preferred_expressions"])
        self.assertIn("GOOGL", cloud["ticker_aliases"]["GOOG"])
        self.assertGreaterEqual(len(cloud["invalidation"]), 2)


class JPNotificationGroundingTests(unittest.TestCase):
    def test_guidance_notification_requires_verified_raised_revision(self):
        notify = Mock()
        notifications = types.ModuleType("notifications")
        notifications.notify_jp_guidance_opportunity = notify
        payloads = {
            "VERIFIED_RAISED": {
                "direction": "上修",
                "source_verified": True,
                "current_verified": True,
                "revision_type": "raised",
            },
            "VERIFIED_INITIAL": {
                "direction": "上修",
                "source_verified": True,
                "current_verified": True,
                "revision_type": "initial",
            },
            "UNVERIFIED_RAISED": {
                "direction": "上修",
                "source_verified": False,
                "current_verified": True,
                "revision_type": "raised",
            },
            "HISTORICAL_VERIFIED_RAISED": {
                "direction": "上修",
                "source_verified": True,
                "current_verified": False,
                "revision_type": "raised",
            },
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            watch = {
                "tickers": [
                    {"ticker": ticker, "name_zh": ticker, "rsi": 20, "pct_5d": -1}
                    for ticker in payloads
                ]
            }

            with patch.object(webui, "_WEBUI_CACHE_DIR", cache_dir), \
                 patch.object(webui, "_official_jp_guidance", side_effect=payloads.get), \
                 patch.dict(sys.modules, {"notifications": notifications}):
                webui._check_jp_catalyst_triggers(watch)

        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["ticker"], "VERIFIED_RAISED")


class JPBankFundamentalsTests(unittest.TestCase):
    def test_bank_fundamentals_do_not_return_croic_or_ccc(self):
        legacy_payload = {
            "ticker": "MUFG",
            "pbr": 1.2,
            "croic": [3.0, 4.0],
            "ccc": [20.0, 18.0],
            "latest": {"croic": 4.0, "ccc": 18.0, "piotroski": 6},
        }

        with patch.object(webui, "_cached", return_value=legacy_payload):
            fundamentals = webui.api_fundamentals("MUFG")

        self.assertNotIn("croic", fundamentals)
        self.assertNotIn("ccc", fundamentals)
        self.assertNotIn("croic", fundamentals.get("latest", {}))
        self.assertNotIn("ccc", fundamentals.get("latest", {}))
        self.assertEqual(fundamentals["fundamental_profile"], "bank")
        self.assertTrue(fundamentals["source_verified"])
        self.assertEqual(
            next(m["value"] for m in fundamentals["metrics"] if m["key"] == "roe"),
            "11.3%",
        )


class JPWatchMarkupContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (AGENTS_DIR / "dashboard.html").read_text(encoding="utf-8")

    def test_jp_card_renders_grounded_event_and_thesis_badges(self):
        self.assertIn("renderJPEarningsBadge(t.earnings_event)", self.html)
        self.assertIn("renderJPThesisFit(t.thesis_fit)", self.html)
        self.assertIn("今日财报（官方）", self.html)
        self.assertIn("非核心表达", self.html)

    def test_bank_profile_has_dedicated_renderer(self):
        self.assertIn("fd.fundamental_profile === 'bank'", self.html)
        self.assertIn("银行专用口径", self.html)
        self.assertIn("未接入则不生成数字", self.html)
        self.assertIn("历史来源已核验，当前待更新", self.html)
        self.assertNotIn("Claude 生成，7 天缓存", self.html)
        self.assertNotIn("return el.innerHTML =", self.html)


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
