from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

try:
    import openai  # noqa: F401
except ImportError:  # minimal test runtime; decision rules do not call the client
    sys.modules["openai"] = types.SimpleNamespace(OpenAI=object)

import decision_agent
import option_flow


NOW = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)


def _smh_payload(volume: int, oi: int = 500) -> dict:
    return {
        "SMH": {
            "spot": 500.0,
            "spot_change_pct": -2.0,
            "data_quality": "snapshot",
            "chains": [{
                "expiry": "2026-12-18",
                "calls": [],
                "puts": [{
                    "strike": 450.0,
                    "volume": volume,
                    "openInterest": oi,
                    "lastPrice": 20.0,
                    "bid": 19.5,
                    "ask": 20.1,
                    "impliedVolatility": 0.42,
                    "delta": -0.35,
                }],
            }],
        },
    }


class OptionFlowAnalysisTests(unittest.TestCase):
    def test_far_dated_put_burst_is_detected_but_snapshot_score_is_capped(self):
        result, _ = option_flow.analyze_option_payloads(_smh_payload(5_000), {}, now=NOW)

        self.assertTrue(result["events"])
        event = result["events"][0]
        self.assertEqual(event["dte_bucket"], "121-540d_strategic")
        self.assertEqual(event["direction"], "bearish")
        self.assertEqual(event["status"], "oi_confirmation_pending")
        self.assertLessEqual(event["score"], option_flow.SNAPSHOT_SCORE_CAP)
        self.assertEqual(result["positions"]["SOXL"]["direction"], "bearish")

    def test_repeated_snapshot_bursts_can_pause_but_not_auto_reduce(self):
        state = {}
        latest = None
        for index in range(5):
            latest, state = option_flow.analyze_option_payloads(
                _smh_payload(5_000 * (index + 1)),
                state,
                now=NOW + timedelta(minutes=30 * index),
            )
        signal = latest["positions"]["SOXL"]
        self.assertGreaterEqual(signal["score"], option_flow.ACTION_SCORE)
        self.assertLess(signal["score"], option_flow.STRONG_SCORE)
        self.assertEqual(signal["action"], "pause_buy")
        self.assertEqual(signal["score_cap"], option_flow.SNAPSHOT_SCORE_CAP)

    def test_next_session_oi_change_confirms_prior_event(self):
        _, state = option_flow.analyze_option_payloads(_smh_payload(5_000), {}, now=NOW)
        result, _ = option_flow.analyze_option_payloads(
            _smh_payload(0, oi=4_500),
            state,
            now=NOW + timedelta(days=1),
        )
        confirmed = [e for e in result["events"] if e["status"] == "oi_confirmed"]
        self.assertTrue(confirmed)
        self.assertGreater(confirmed[0]["oi_confirmation"]["oi_delta"], 0)
        self.assertLessEqual(confirmed[0]["score"], option_flow.SNAPSHOT_SCORE_CAP)

    def test_expiry_selection_covers_near_swing_and_long_dated_buckets(self):
        expiries = [
            "2026-08-21", "2026-08-28", "2026-09-04", "2026-09-18",
            "2026-10-16", "2026-12-18", "2027-03-19", "2027-06-18",
        ]
        selected = option_flow.select_expiries(expiries, now=NOW, max_expiries=18)
        self.assertIn("2026-08-21", selected)
        self.assertIn("2026-09-18", selected)
        self.assertIn("2026-12-18", selected)
        self.assertIn("2027-06-18", selected)

    def test_previous_session_volume_is_not_replayed_as_new_flow(self):
        payload = _smh_payload(5_000)
        payload["SMH"]["chains"][0]["puts"][0]["lastTradeDate"] = "2026-08-18T19:59:00+00:00"
        result, _ = option_flow.analyze_option_payloads(payload, {}, now=NOW)
        self.assertEqual(result["events"], [])

    def test_delta_notional_uses_labelled_bs_fallback_when_feed_has_no_greeks(self):
        payload = _smh_payload(5_000)
        payload["SMH"]["chains"][0]["puts"][0].pop("delta")
        result, _ = option_flow.analyze_option_payloads(payload, {}, now=NOW)
        event = result["events"][0]
        self.assertGreater(event["delta_notional"], 0)
        self.assertEqual(event["delta_source"], "black_scholes_approx")


class OptionFlowDecisionGuardTests(unittest.TestCase):
    def _events(self, *, quality: str, score: int) -> dict:
        return {
            "options_flow": {
                "stale": False,
                "positions": {
                    "TQQQ": {
                        "direction": "bearish",
                        "score": score,
                        "action": "pause_buy" if score < 75 else "reduce_candidate",
                        "confirming_sources": ["QQQ"],
                        "data_quality": quality,
                        "score_cap": 69 if quality == "snapshot" else 100,
                        "top_events": [],
                    },
                },
            },
        }

    def test_snapshot_bearish_flow_pauses_conflicting_buy(self):
        guarded = decision_agent._apply_options_flow_guard(
            {"action": "WATCH_BUY", "confidence": 4, "reason": "momentum", "stop_ref": 90},
            "US.TQQQ",
            {"ticker": "US.TQQQ", "pct_chg": 1.0, "trend": "up"},
            self._events(quality="snapshot", score=65),
        )
        self.assertEqual(guarded["action"], "HOLD")
        self.assertEqual(guarded["options_flow_guard"], "pause_conflicting_buy")

    def test_authoritative_flow_needs_price_confirmation_for_reduce(self):
        result = {"action": "HOLD", "confidence": 2, "reason": "neutral", "stop_ref": None}
        not_confirmed = decision_agent._apply_options_flow_guard(
            result,
            "US.TQQQ",
            {"ticker": "US.TQQQ", "pct_chg": 1.0, "trend": "up"},
            self._events(quality="tape", score=82),
        )
        self.assertEqual(not_confirmed["action"], "HOLD")

        confirmed = decision_agent._apply_options_flow_guard(
            result,
            "US.TQQQ",
            {"ticker": "US.TQQQ", "pct_chg": -4.0, "trend": "down"},
            self._events(quality="tape", score=82),
        )
        self.assertEqual(confirmed["action"], "REDUCE")
        self.assertEqual(confirmed["options_flow_guard"], "confirmed_reduce")


if __name__ == "__main__":
    unittest.main()
