"""F09 regression (audit 2026-09-19): rebalance 最小订单金额按 NAV 换算, 不再
全局硬 5000. 之前 $5000 floor 让 <$100k 小账户几乎所有 rebalance 单被过滤,
且用户目标是小资金激进策略, 完全违反 product 意图.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import auto_rebalance


class MinOrderNAVScaledTests(unittest.TestCase):

    def test_tiny_account_hits_absolute_floor(self):
        # $10k account: 0.5% = $50 = floor
        self.assertEqual(auto_rebalance._min_order_usd(10_000), 50)

    def test_medium_account_pct_based(self):
        # $50k: 0.5% = $250 > $50 floor → 用 pct
        self.assertEqual(auto_rebalance._min_order_usd(50_000), 250)

    def test_large_account_matches_legacy_5000(self):
        # $1M: 0.5% = $5000 → 与旧硬编码等值 (向后兼容大账户预期)
        self.assertEqual(auto_rebalance._min_order_usd(1_000_000), 5000)

    def test_100k_account_gets_500_min(self):
        # $100k: 0.5% = $500. 之前旧硬编 $5000 时 <$5000 单全过滤, 现在允许 $500 起
        self.assertEqual(auto_rebalance._min_order_usd(100_000), 500)

    def test_zero_or_negative_nav_falls_back_to_floor(self):
        self.assertEqual(auto_rebalance._min_order_usd(0), 50)
        self.assertEqual(auto_rebalance._min_order_usd(-100), 50)
        self.assertEqual(auto_rebalance._min_order_usd(None), 50)

    def test_floor_is_50_documented_reason(self):
        # 绝对下限 $50 覆盖 commission + slippage 经济意义
        # 更小的 tick-based 单是噪声; 更大的会 pretend 小账户没市场
        self.assertEqual(auto_rebalance._MIN_ORDER_USD_FLOOR, 50)
        self.assertEqual(auto_rebalance._MIN_ORDER_PCT_NAV, 0.5)


if __name__ == "__main__":
    unittest.main()
