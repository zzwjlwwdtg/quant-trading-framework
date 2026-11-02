"""2026-11-02: US.TLT 撤回为只展示 —— 不开新仓、不花 AI 复核额度，卖出不受影响。"""
import unittest
from unittest.mock import patch

import claude_gate
import config
import paper_trader as pt


class DisplayOnlyTickerTests(unittest.TestCase):
    def test_tlt_is_display_only(self):
        self.assertIn("US.TLT", config.DISPLAY_ONLY_TICKERS)

    def test_buy_is_refused_before_any_broker_or_cash_call(self):
        with patch.object(pt, "_ctx_get", side_effect=AssertionError("broker touched")), \
             patch("stop_line_guard.check_buy", side_effect=AssertionError("guard reached")):
            self.assertIsNone(pt._place("US.TLT", pt.TrdSide.BUY, 10, 90.0, tag="test"))

    def test_sell_path_is_not_blocked_by_display_only(self):
        src = open(pt.__file__, encoding="utf-8").read()
        self.assertIn("if side == TrdSide.BUY and code in DISPLAY_ONLY_TICKERS:", src)

    def test_gate_skips_display_only_inside_window(self):
        decision = {"action": "BUY", "confidence": 9, "regime": "neutral"}
        window = sorted(claude_gate.TRADE_WINDOWS)[0]
        with patch.object(claude_gate, "_enabled", return_value=True), \
             patch.object(claude_gate, "_prompt", side_effect=AssertionError("AI called")):
            out = claude_gate.apply_claude_gate("US.TLT", {}, {}, decision, {}, window)
        self.assertEqual(out["ai_gate_status"], "skipped_display_only")
        self.assertEqual(out["action"], "BUY")   # 决策照常展示, 下单由 _place 拦截


if __name__ == "__main__":
    raise SystemExit("use: python -m unittest discover -s tests -t .")
