from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import bond_ai_interpret
import webui


class TltFallbackTests(unittest.TestCase):
    def test_missing_data_defaults_to_hold(self):
        out = bond_ai_interpret._fallback_from_rules({})
        self.assertEqual(out["tlt_action"], "hold")
        self.assertTrue(out["tlt_reason"])

    def test_duration_selling_warning_marks_sell(self):
        out = bond_ai_interpret._fallback_from_rules({
            "warnings": [{
                "level": "warn", "key": "duration_selling",
                "msg": "长端加速抛售: 30Y +18bps vs 10Y +4bps",
            }],
        })
        self.assertEqual(out["tlt_action"], "sell")
        self.assertIn("长端", out["tlt_reason"])

    def test_unrelated_warning_keeps_hold(self):
        out = bond_ai_interpret._fallback_from_rules({
            "warnings": [{
                "level": "info", "key": "tga_high", "msg": "TGA 高位",
            }],
        })
        self.assertEqual(out["tlt_action"], "hold")


class TltInterpretationTests(unittest.TestCase):
    def _interpret(self, payload: dict) -> dict:
        raw = json.dumps(payload, ensure_ascii=False)
        with patch("ai_prompt.query_ai_cli",
                   return_value=(raw, "ok", "test", None)):
            return bond_ai_interpret.interpret_bond_context({
                "yields": {}, "warnings": [],
            })

    def test_action_is_normalized_and_reason_trimmed(self):
        out = self._interpret({
            "tlt_action": "BUY",
            "tlt_reason": "  收益率回落，长债压力缓解  ",
        })
        self.assertEqual(out["tlt_action"], "buy")
        self.assertEqual(out["tlt_reason"], "收益率回落，长债压力缓解")

    def test_invalid_action_becomes_none(self):
        out = self._interpret({"tlt_action": "maybe", "tlt_reason": "x"})
        self.assertIsNone(out["tlt_action"])

    def test_missing_fields_default_to_none(self):
        out = self._interpret({"chain_summary": "中性"})
        self.assertIsNone(out["tlt_action"])
        self.assertEqual(out["tlt_reason"], "")


class TltHintPayloadTests(unittest.TestCase):
    def test_valid_payload_is_passed_through(self):
        hint = webui._tlt_hint_from_interpretation({
            "tlt_action": "SELL",
            "tlt_reason": "长端抛售 + 实际利率高位",
            "_source": "ai_cli_claude",
            "generated_at": "2026-10-23T00:00:00+00:00",
            "_meta": {"stale": True},
        })
        self.assertEqual(hint["action"], "sell")
        self.assertEqual(hint["reason"], "长端抛售 + 实际利率高位")
        self.assertEqual(hint["source"], "ai_cli_claude")
        self.assertTrue(hint["stale"])

    def test_error_or_missing_input_returns_none(self):
        self.assertIsNone(webui._tlt_hint_from_interpretation(None))
        self.assertIsNone(webui._tlt_hint_from_interpretation({"error": "boom"}))

    def test_unknown_action_exposes_none(self):
        hint = webui._tlt_hint_from_interpretation({
            "tlt_action": "sideways", "tlt_reason": "x",
        })
        self.assertIsNone(hint["action"])


class TltSignalPayloadTests(unittest.TestCase):
    def test_buy_like_action_exposes_system_levels(self):
        payload = webui._tlt_signal_payload(
            {
                "price": 88.1, "pct_chg": 0.5,
                "quote_ts": "2026-10-23T14:00:00-04:00",
            },
            {
                "action": "watch_buy", "confidence": 4, "reason": "ma cross",
                "entry_ref": 88.0, "stop_ref": 86.5,
            },
            {"score": 4.2, "why": ["WATCH_BUY conf=4.0"], "whitelist": False},
        )
        self.assertEqual(payload["action"], "WATCH_BUY")
        self.assertEqual(payload["entry_ref"], 88.0)
        self.assertEqual(payload["stop_ref"], 86.5)
        self.assertEqual(payload["score"], 4.2)
        self.assertEqual(payload["quote_ts"], "2026-10-23T14:00:00-04:00")
        self.assertIsNotNone(payload["generated_at"])

    def test_non_buy_action_hides_entry_and_stop(self):
        payload = webui._tlt_signal_payload(
            {"price": 88.1},
            {"action": "SELL", "reason": "yields rising",
             "entry_ref": 88.1, "stop_ref": 90.0},
        )
        self.assertEqual(payload["action"], "SELL")
        self.assertIsNone(payload["entry_ref"])
        self.assertIsNone(payload["stop_ref"])
        self.assertIn("yields rising", payload["reason"])

    def test_long_reason_is_trimmed(self):
        payload = webui._tlt_signal_payload(
            {"price": 88.1}, {"action": "HOLD", "reason": "x" * 500},
        )
        self.assertLessEqual(len(payload["reason"]), 240)


class TltSignalComputeTests(unittest.TestCase):
    def test_uses_shared_decision_path_without_emitting(self):
        market = {
            "ticker": "US.TLT", "price": 88.1, "pct_chg": 0.4,
            "quote_ts": "2026-10-23T14:00:00-04:00",
        }
        decision = {
            "action": "WATCH_BUY", "confidence": 3, "reason": "test signal",
            "entry_ref": 88.1, "stop_ref": 86.9,
        }
        score = {"score": 3.0, "why": ["WATCH_BUY conf=3.0"], "whitelist": False}
        with patch("market_watch.get_market_signal", return_value=market) as mkt, \
                patch("decision_agent.get_decision", return_value=decision) as dec, \
                patch("confluence.get_confluence",
                      return_value={"bull_count": 2}), \
                patch("regime_today.get_today_regime",
                      return_value="bull_trending"), \
                patch("top_picks._score_signal", return_value=score), \
                patch("notifier.emit") as emit:
            out = webui._compute_tlt_system_signal()

        mkt.assert_called_once_with("US.TLT")
        kwargs = dec.call_args.kwargs
        self.assertEqual(kwargs["confluence"], {"bull_count": 2})
        self.assertEqual(kwargs["board_regime"], "bull_trending")
        self.assertEqual(out["action"], "WATCH_BUY")
        self.assertEqual(out["score"], 3.0)
        self.assertFalse(emit.called)

    def test_market_error_is_returned_without_decision(self):
        with patch("market_watch.get_market_signal",
                   return_value={"ticker": "US.TLT", "error": "snapshot down"}), \
                patch("decision_agent.get_decision") as dec:
            out = webui._compute_tlt_system_signal()
        self.assertIn("snapshot down", out["error"])
        self.assertFalse(dec.called)


class TltSignalCacheTests(unittest.TestCase):
    def test_cache_contract(self):
        with patch.object(webui, "_cached", return_value={"action": "HOLD"}) as cached:
            out = webui.api_tlt_system_signal()
        self.assertEqual(out["action"], "HOLD")
        kwargs = cached.call_args.kwargs
        self.assertEqual(kwargs["ttl_sec"], webui._TLT_SIGNAL_TTL_SEC)
        self.assertTrue(kwargs["first_call_async"])
        self.assertIs(kwargs["compute_fn"], webui._compute_tlt_system_signal)


class DashboardTltRowTests(unittest.TestCase):
    def test_dashboard_has_tlt_signal_row(self):
        html = (AGENTS_DIR / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("t.tlt_signal", html)
        self.assertIn("仅展示 · 未纳入自动交易", html)
        self.assertIn("同一套技术决策引擎", html)
        self.assertIn("宏观 AI 参考", html)


if __name__ == "__main__":
    unittest.main()
