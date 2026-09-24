"""Gold rules 买入理由标签必须与 RSI 一致 (2026-09-24).

日志曾出现 "RSI(14)=54.5 < 38，处于超卖区间" — 标签与数值自相矛盾.
股票路径已按 RSI_OVERSOLD 判断; _gold_rules 仍无条件写 "oversold + uptrend".
只修解释文本, 不改 action / confidence.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import decision_agent as da
from config import RSI_OVERSOLD

BASE = dict(ticker="US.GLD", price=300, trend="up", ma_stack="bull", vol_ratio=1.2,
            cci_zone="neutral", bb_zone="normal", psar_signal="bull",
            macd_signal="golden", macd_zone="bull", adx_zone="strong",
            support=290, resistance=320)


def run(rsi):
    return da._gold_rules(dict(BASE, rsi_14=rsi),
                          {"gold_bias": "neutral", "days_to_event": 99}, {}, "neutral")


class GoldReasonMatchesRsi(unittest.TestCase):
    def test_neutral_rsi_not_called_oversold(self):
        r = run(55)
        self.assertEqual(r["action"], "BUY")
        self.assertNotIn("oversold", r["reason"])
        self.assertEqual(r["reason"], "uptrend + positive confluence")

    def test_oversold_rsi_keeps_label(self):
        r = run(RSI_OVERSOLD - 3)
        self.assertEqual(r["reason"], "oversold + uptrend")

    def test_extreme_oversold_keeps_label(self):
        self.assertEqual(run(25)["reason"], "RSI extreme oversold")

    def test_action_and_confidence_unchanged(self):
        self.assertEqual((run(55)["action"], run(55)["confidence"]), ("BUY", 5))
        self.assertEqual((run(35)["action"], run(35)["confidence"]), ("BUY", 4))


if __name__ == "__main__":
    unittest.main()
