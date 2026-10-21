from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import decision_agent
import option_walls


def _calendar(events, *, source="yfinance", calendar_ok=True):
    return {
        "stock": "NVDA",
        "events": events,
        "source": source,
        "stale": False,
        "fetched_at": "2026-10-21T00:00:00-04:00",
        "error": None,
        "providers": {},
        "calendar_ok": calendar_ok,
    }


EVENT = {
    "date": "2026-11-17",
    "date_latest": "2026-11-18",
    "session": "AMC",
    "sources": ["calendar", "earnings_dates"],
    "conflict": True,
}


class EarningsCalendarTests(unittest.TestCase):
    def test_remote_sources_merge_into_window(self):
        raw = {
            "providers": {
                "earnings_dates": [{"date": "2026-11-17", "session": "AMC"}],
                "calendar": [{"date": "2026-11-18", "session": "unknown"}],
            },
            "errors": [],
        }
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(option_walls, "SIGNALS_DIR", tmp), \
                patch.object(option_walls, "_CALENDAR_MEM", {}), \
                patch.object(option_walls, "_fetch_earnings_remote", return_value=raw):
            cal = option_walls.get_earnings_events(
                "NVDA", force=True, today=date(2026, 10, 21),
            )
            self.assertEqual(cal["source"], "yfinance")
            self.assertEqual(len(cal["events"]), 1)
            ev = cal["events"][0]
            self.assertEqual(ev["date"], "2026-11-17")
            self.assertEqual(ev["date_latest"], "2026-11-18")
            self.assertTrue(ev["conflict"])
            self.assertEqual(ev["session"], "AMC")
            self.assertEqual(ev["sources"], ["calendar", "earnings_dates"])
            self.assertTrue(Path(tmp, "earnings_calendar_cache.json").exists())

    def test_fresh_cache_avoids_remote_call(self):
        raw = {
            "providers": {
                "earnings_dates": [{"date": "2026-11-17", "session": "AMC"}],
                "calendar": [],
            },
            "errors": [],
        }
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(option_walls, "SIGNALS_DIR", tmp), \
                patch.object(option_walls, "_CALENDAR_MEM", {}), \
                patch.object(option_walls, "_fetch_earnings_remote", return_value=raw):
            option_walls.get_earnings_events("NVDA", force=True)
            option_walls._CALENDAR_MEM.clear()
            with patch.object(
                option_walls, "_fetch_earnings_remote",
                side_effect=AssertionError("remote should not be called"),
            ):
                cal = option_walls.get_earnings_events("NVDA")
            self.assertEqual(cal["source"], "cache")
            self.assertFalse(cal["stale"])
            self.assertEqual(cal["events"][0]["date"], "2026-11-17")

    def test_stale_cache_is_used_before_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(option_walls, "SIGNALS_DIR", tmp), \
                patch.object(option_walls, "_CALENDAR_MEM", {}):
            option_walls._save_calendar_cache({
                "version": 1,
                "stocks": {
                    "MU": {
                        "fetched_at": time.time() - 2 * 24 * 3600,
                        "fetched_at_iso": "2026-10-19T00:00:00-04:00",
                        "events": [{
                            "date": "2026-12-23",
                            "date_latest": "2026-12-24",
                            "session": "AMC",
                            "sources": ["earnings_dates", "calendar"],
                            "conflict": True,
                        }],
                        "providers": {},
                        "errors": [],
                    }
                },
            })
            with patch.object(
                option_walls, "_fetch_earnings_remote",
                side_effect=RuntimeError("offline"),
            ):
                cal = option_walls.get_earnings_events("MU", force=True)
            self.assertEqual(cal["source"], "cache_stale")
            self.assertTrue(cal["stale"])
            self.assertEqual(cal["events"][0]["date"], "2026-12-23")

    def test_expired_cache_falls_back_to_static_window(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(option_walls, "SIGNALS_DIR", tmp), \
                patch.object(option_walls, "_CALENDAR_MEM", {}):
            option_walls._save_calendar_cache({
                "version": 1,
                "stocks": {
                    "MU": {
                        "fetched_at": time.time() - 8 * 24 * 3600,
                        "fetched_at_iso": "2026-10-13T00:00:00-04:00",
                        "events": [],
                        "providers": {},
                        "errors": [],
                    }
                },
            })
            with patch.object(
                option_walls, "_fetch_earnings_remote",
                side_effect=RuntimeError("offline"),
            ):
                cal = option_walls.get_earnings_events("MU", force=True)
            self.assertEqual(cal["source"], "static_fallback")
            self.assertTrue(cal["stale"])
            self.assertEqual(cal["events"][0]["date"], "2026-12-23")
            self.assertEqual(cal["events"][0]["date_latest"], "2026-12-24")

    def test_upcoming_event_uses_whole_window(self):
        with patch.object(
            option_walls, "get_earnings_events", return_value=_calendar([EVENT]),
        ):
            self.assertEqual(
                option_walls._next_earnings_for("SOXL", today=date(2026, 11, 16)),
                ("NVDA", "2026-11-17", 1),
            )
            self.assertEqual(
                option_walls._next_earnings_for("SOXL", today=date(2026, 11, 18)),
                ("NVDA", "2026-11-17", 0),
            )
            self.assertIsNone(
                option_walls._next_earnings_for("SOXL", today=date(2026, 11, 19)),
            )

    def test_mu_september_event_is_not_lost_after_old_hardcoded_date(self):
        # Regression: the old static table said 2026-09-24, so after that date
        # _next_earnings_for returned None even though the real event was 09-30.
        event = {
            "date": "2026-09-30",
            "date_latest": "2026-09-30",
            "session": "AMC",
            "sources": ["earnings_dates"],
            "conflict": False,
        }
        with patch.object(
            option_walls, "get_earnings_events", return_value=_calendar([event]),
        ):
            self.assertEqual(
                option_walls._next_earnings_for("MULL", today=date(2026, 9, 29)),
                ("MU", "2026-09-30", 1),
            )

    def test_expiry_must_be_strictly_after_event_window(self):
        self.assertEqual(
            option_walls._select_earnings_expiry(
                ["2026-12-23", "2026-12-24", "2026-12-26", "2027-01-15"],
                "2026-12-24",
            ),
            ("2026-12-26", 2),
        )
        self.assertEqual(
            option_walls._select_earnings_expiry(
                ["2026-12-23", "2026-12-24"], "2026-12-24",
            ),
            (None, None),
        )

    def test_im_returns_calendar_context_when_expiry_invalid(self):
        class FakeTicker:
            def __init__(self, symbol):
                self.symbol = symbol

            @property
            def info(self):
                return {"regularMarketPrice": 100.0}

            @property
            def options(self):
                return ["2026-12-23", "2026-12-24"]

        cal = _calendar([{
            "date": "2026-12-23",
            "date_latest": "2026-12-24",
            "session": "AMC",
            "sources": ["earnings_dates", "calendar"],
            "conflict": True,
        }])
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(option_walls, "SIGNALS_DIR", tmp), \
                patch.object(option_walls, "get_earnings_events", return_value=cal), \
                patch("yfinance.Ticker", FakeTicker):
            result = option_walls.get_earnings_implied_move(
                "MU", "2026-12-23", force=True,
            )
        self.assertEqual(result["error"], "no_expiry_after_earnings")
        self.assertEqual(result["required_expiry_after"], "2026-12-25")
        self.assertEqual(result["event_date_latest"], "2026-12-24")
        self.assertTrue(result["calendar_ok"])
        self.assertIsNotNone(result["days_to_earnings"])

    def test_not_tracked_stock_does_not_hit_network(self):
        with patch.object(
            option_walls, "_fetch_earnings_remote",
            side_effect=AssertionError("network should not be called"),
        ):
            cal = option_walls.get_earnings_events("AAPL")
        self.assertEqual(cal["source"], "not_tracked")
        self.assertEqual(cal["events"], [])
        self.assertFalse(cal["calendar_ok"])


class EarningsGuardTests(unittest.TestCase):
    def _events(self, **payload):
        base = {"stock": "MU", "days_to_earnings": 1, "leverage": 2}
        base.update(payload)
        return {"earnings_implied_move": {"US.MULL": base}}

    def test_blackout_survives_missing_im(self):
        result = {"action": "WATCH_BUY", "confidence": 5, "reason": "test"}
        guarded = decision_agent._apply_earnings_guard(
            result, "US.MULL",
            self._events(error="no_expiry_after_earnings"),
        )
        self.assertEqual(guarded["action"], "HOLD")
        self.assertIsNone(guarded["stop_ref"])
        self.assertTrue(guarded["earnings_guard"])
        self.assertIn("IM不可用", guarded["reason"])

    def test_event_window_day_zero_is_blackout(self):
        result = {"action": "WATCH_BUY", "confidence": 5, "reason": "test"}
        guarded = decision_agent._apply_earnings_guard(
            result, "US.MULL",
            self._events(days_to_earnings=0, error="no_expiry_after_earnings"),
        )
        self.assertEqual(guarded["action"], "HOLD")

    def test_missing_im_far_from_event_does_not_change_action(self):
        result = {"action": "WATCH_BUY", "confidence": 5, "reason": "test"}
        guarded = decision_agent._apply_earnings_guard(
            result, "US.MULL",
            self._events(days_to_earnings=5, error="no_expiry_after_earnings"),
        )
        self.assertEqual(guarded["action"], "WATCH_BUY")
        self.assertEqual(guarded["confidence"], 5)

    def test_blackout_with_valid_im_still_works(self):
        result = {"action": "WATCH_BUY", "confidence": 5, "reason": "test"}
        guarded = decision_agent._apply_earnings_guard(
            result, "US.MULL",
            self._events(implied_move_pct=8),
        )
        self.assertEqual(guarded["action"], "HOLD")
        self.assertIn("跨日 IM=16.0%", guarded["reason"])


if __name__ == "__main__":
    unittest.main()
