"""R07 (Codex SYSTEM_REVIEW 2026-09-20): 动作只有一个权威定义.

旧状态: trading_actions (enum, 无生产调用) 与 trading_contracts (生产用的字符串集合)
并行定义, 已出现分歧: SELL_ALL / REDUCE_RISK 在 enum 里是下单动作, 生产集合里没有
→ 若有模块输出它们, 交易器会静默忽略.
现在: trading_contracts 的集合由 trading_actions 的决策词表派生.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import trading_actions as ta
import trading_contracts as tc

OLD_BUY = {"BUY", "WATCH_BUY", "WATCH_BUY_PROBE", "PROBE", "ADD"}
OLD_SELL = {"SELL", "EXIT"}
OLD_REDUCE = {"REDUCE"}
OLD_PROBE = {"WATCH_BUY_PROBE", "PROBE"}
OLD_WATCH = {"WATCH_BUY_LONG_HOLD", "WATCH"}


class SingleSource(unittest.TestCase):
    def test_contract_sets_agree_with_enum(self):
        for s in ta.decision_vocabulary():
            a = ta.Action.parse(s)
            with self.subTest(action=s):
                self.assertEqual(s in tc.ORDER_ACTIONS, ta.is_order(s))
                self.assertEqual(s in tc.BUY_ACTIONS, a in {ta.Action.BUY, ta.Action.PROBE, ta.Action.ADD})
                self.assertEqual(s in tc.SELL_ACTIONS, a == ta.Action.EXIT)
                self.assertEqual(s in tc.REDUCE_ACTIONS, a == ta.Action.REDUCE)
                self.assertEqual(s in tc.PROBE_ONLY_ACTIONS, a == ta.Action.PROBE)

    def test_backward_compatible_supersets(self):
        self.assertTrue(OLD_BUY == set(tc.BUY_ACTIONS))
        self.assertTrue(OLD_SELL <= set(tc.SELL_ACTIONS))
        self.assertTrue(OLD_REDUCE <= set(tc.REDUCE_ACTIONS))
        self.assertEqual(set(tc.PROBE_ONLY_ACTIONS), OLD_PROBE)
        self.assertEqual(set(tc.NON_EXECUTING_BULLISH_ACTIONS), OLD_WATCH)

    def test_previously_ignored_aliases_now_tradable(self):
        self.assertIn("SELL_ALL", tc.SELL_ACTIONS)
        self.assertIn("REDUCE_RISK", tc.REDUCE_ACTIONS)
        new = (set(tc.ORDER_ACTIONS) - OLD_BUY - OLD_SELL - OLD_REDUCE)
        self.assertEqual(new, {"SELL_ALL", "REDUCE_RISK"})

    def test_watch_and_execution_labels_never_orders(self):
        for s in ("WATCH", "WATCH_BUY_LONG_HOLD", "HOLD", "CAUTION",
                  "TAKE_PROFIT", "TRAILING_STOP", "STOP_LOSS", "PYRAMID_ADD",
                  "REBALANCE_UP", "REBALANCE_DOWN"):
            self.assertNotIn(s, tc.ORDER_ACTIONS, s)


class ConsumersUseContracts(unittest.TestCase):
    def test_top_picks_and_research_validation_share_sets(self):
        import top_picks, research_validation  # noqa
        self.assertEqual(set(top_picks._SELL_ACTIONS), set(tc.SELL_ACTIONS))
        self.assertEqual(set(top_picks._REDUCE_ACTIONS), set(tc.REDUCE_ACTIONS))


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
