"""WP02 (audit 2026-09-19): Action enum + legacy compatibility tests.

锁死:
- Action.parse accepts str, Action, None (permissive)
- LEGACY_ACTION_MAP 覆盖所有历史字符串
- is_buy_like / is_reduce_like / is_order semantic 分组正确
- Action == str 比较 (str Enum inheritance)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from trading_actions import (
    Action, LEGACY_ACTION_MAP, is_buy_like, is_reduce_like, is_order,
    BUY_TYPE_ACTIONS, REDUCE_TYPE_ACTIONS, ORDER_ACTIONS,
)


class ActionEnumTests(unittest.TestCase):

    def test_enum_values_stable(self):
        # 每个 canonical case 的 str value 必须固定
        self.assertEqual(Action.BUY.value, "BUY")
        self.assertEqual(Action.WATCH.value, "WATCH")
        self.assertEqual(Action.PROBE.value, "PROBE")
        self.assertEqual(Action.ADD.value, "ADD")
        self.assertEqual(Action.HOLD.value, "HOLD")
        self.assertEqual(Action.REDUCE.value, "REDUCE")
        self.assertEqual(Action.EXIT.value, "EXIT")
        self.assertEqual(Action.CAUTION.value, "CAUTION")

    def test_str_enum_equals_string(self):
        # str Enum: Action.BUY == "BUY" 为 True (兼容旧字符串比较)
        self.assertEqual(Action.BUY, "BUY")
        self.assertEqual("BUY", Action.BUY)


class ParseTests(unittest.TestCase):

    def test_parse_none_returns_none(self):
        self.assertIsNone(Action.parse(None))

    def test_parse_canonical_string(self):
        self.assertEqual(Action.parse("BUY"), Action.BUY)
        self.assertEqual(Action.parse("EXIT"), Action.EXIT)

    def test_parse_case_insensitive(self):
        self.assertEqual(Action.parse("buy"), Action.BUY)
        self.assertEqual(Action.parse("Buy"), Action.BUY)

    def test_parse_legacy_watch_buy_is_buy(self):
        # 语义: WATCH_BUY 现在会执行 → 对应 canonical BUY
        self.assertEqual(Action.parse("WATCH_BUY"), Action.BUY)

    def test_parse_legacy_watch_buy_probe_is_probe(self):
        self.assertEqual(Action.parse("WATCH_BUY_PROBE"), Action.PROBE)

    def test_parse_legacy_watch_buy_long_hold_is_watch(self):
        self.assertEqual(Action.parse("WATCH_BUY_LONG_HOLD"), Action.WATCH)

    def test_parse_legacy_sell_variants_map_to_exit(self):
        self.assertEqual(Action.parse("SELL"), Action.EXIT)
        self.assertEqual(Action.parse("SELL_ALL"), Action.EXIT)

    def test_parse_legacy_reduce_risk_is_reduce(self):
        self.assertEqual(Action.parse("REDUCE_RISK"), Action.REDUCE)

    def test_parse_action_input_returned(self):
        self.assertEqual(Action.parse(Action.BUY), Action.BUY)

    def test_parse_unknown_returns_none(self):
        # 不 raise, 返 None (permissive)
        self.assertIsNone(Action.parse("MADE_UP_ACTION"))
        self.assertIsNone(Action.parse(""))


class SemanticGroupingTests(unittest.TestCase):

    def test_is_buy_like_covers_watch_probe_buy_add(self):
        for a in ("BUY", "WATCH", "PROBE", "ADD",
                  "WATCH_BUY", "WATCH_BUY_PROBE"):
            with self.subTest(a=a):
                self.assertTrue(is_buy_like(a), f"{a} should be buy-like")

    def test_is_buy_like_excludes_reduce_hold(self):
        for a in ("REDUCE", "EXIT", "HOLD", "SELL", "SELL_ALL", "CAUTION"):
            with self.subTest(a=a):
                self.assertFalse(is_buy_like(a), f"{a} should NOT be buy-like")

    def test_is_reduce_like(self):
        for a in ("REDUCE", "EXIT", "SELL", "SELL_ALL", "REDUCE_RISK"):
            with self.subTest(a=a):
                self.assertTrue(is_reduce_like(a))
        for a in ("BUY", "HOLD", "WATCH", "CAUTION"):
            with self.subTest(a=a):
                self.assertFalse(is_reduce_like(a))

    def test_is_order_excludes_display_only(self):
        # WATCH / HOLD / CAUTION 是显示型, 不下单
        for a in ("WATCH", "HOLD", "CAUTION", "WATCH_BUY_LONG_HOLD"):
            with self.subTest(a=a):
                self.assertFalse(is_order(a))
        # PROBE / BUY / ADD / REDUCE / EXIT 是订单
        for a in ("PROBE", "BUY", "ADD", "REDUCE", "EXIT"):
            with self.subTest(a=a):
                self.assertTrue(is_order(a))

    def test_none_input_returns_false_everywhere(self):
        self.assertFalse(is_buy_like(None))
        self.assertFalse(is_reduce_like(None))
        self.assertFalse(is_order(None))


class LegacyMapCoverageTests(unittest.TestCase):
    """审计: LEGACY_ACTION_MAP 必须覆盖 trading_contracts.py 里所有 action."""

    def test_covers_trading_contracts_actions(self):
        try:
            from trading_contracts import (
                BUY_ACTIONS, SELL_ACTIONS, REDUCE_ACTIONS,
                NON_EXECUTING_BULLISH_ACTIONS, BEARISH_SIGNAL_ACTIONS,
            )
        except ImportError:
            self.skipTest("trading_contracts not available")
        # 所有 legacy action 都应能 parse 到 Action
        all_legacy = (BUY_ACTIONS | SELL_ACTIONS | REDUCE_ACTIONS
                       | NON_EXECUTING_BULLISH_ACTIONS | BEARISH_SIGNAL_ACTIONS)
        for a in all_legacy:
            with self.subTest(a=a):
                parsed = Action.parse(a)
                self.assertIsNotNone(parsed,
                                       f"legacy {a} 应能 parse (加入 LEGACY_ACTION_MAP)")


if __name__ == "__main__":
    unittest.main()
