"""Pyramid 加仓规模 (2026-09-24, 券商历史对账发现).

实例: 2026-06-18 SOXL 持仓 36 股, PYRAMID L2 一次买入 1490 股 @249.77 (~$37 万),
当时 AI 建议 "仓位 ≤ 10%". 设计注释写 "每层加 50% 原仓", 代码却是
add = size_usd(整笔目标仓位) × 50%, 且不检查加仓后总仓位.

期望:
- 加仓量 = 当前持仓 × PYRAMID_ADD_FRAC (36 → 18).
- 加仓金额 ≤ size_usd (今日单笔目标/组剩余额度).
- 加仓后总市值 ≤ 账户 × POSITION_FRACTION_MAX; 已达上限 → 0.
- 不额外连 OpenD (用购买力缓存).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import paper_trader as pt


class PyramidAddQtyHelper(unittest.TestCase):
    def test_soxl_20260618_replay(self):
        qty = pt._pyramid_add_qty(pos_qty=36, price=249.77,
                                  size_usd=742_000.0, power=1_000_000.0)
        self.assertEqual(qty, 18)

    def test_capped_by_size_usd(self):
        # 持仓 100 @100; 组剩余额度只有 $2k → 最多加 20 股 (不是 50)
        self.assertEqual(pt._pyramid_add_qty(100, 100.0, 2_000.0, 1_000_000.0), 20)

    def test_capped_by_position_fraction_max(self):
        # 目标很大, 但账户 $100k × 40% = $40k; 持仓 300 @100 = $30k → 最多加 100 股
        self.assertEqual(pt._pyramid_add_qty(300, 100.0, 1e9, 100_000.0), 100)

    def test_no_room_no_add(self):
        # 账户 $50k × 40% = $20k, 已持 200 @100 = $20k → 0
        self.assertEqual(pt._pyramid_add_qty(200, 100.0, 1e9, 50_000.0), 0)

    def test_bad_inputs(self):
        self.assertEqual(pt._pyramid_add_qty(0, 100.0, 1e6, 1e6), 0)
        self.assertEqual(pt._pyramid_add_qty(10, 0.0, 1e6, 1e6), 0)
        self.assertEqual(pt._pyramid_add_qty(10, 100.0, 0.0, 1e6), 0)


class PyramidBranchUsesHelper(unittest.TestCase):
    """真实 _execute_unlocked 路径: SOXL 场景只下 18 股."""

    def test_execute_pyramid_places_half_of_current_position(self):
        window = sorted(pt.TRADE_WINDOWS)[0]
        state = {"US.SOXL": {"entry_conf": 6, "entry_conf_scale": 10,
                             "pyramid_layer": 1, "entry_price": 249.0,
                             "entry_high": 250.0, "entry_qty": 566}}
        placed = []

        def fake_place(ticker, side, qty, price, **kw):
            placed.append(qty)
            return "DRY"

        import decision_agent
        with patch.object(pt, "_state_load", return_value=state), \
             patch.object(pt, "_state_save"), \
             patch.object(pt, "_position_qty", return_value=36), \
             patch.object(pt, "_position_size_usd", return_value=742_000.0), \
             patch.object(pt, "_power_cache", 1_000_000.0), \
             patch.object(pt, "_get_account_power",
                          side_effect=AssertionError("no extra OpenD call")), \
             patch.object(pt, "_place", side_effect=fake_place), \
             patch.object(pt, "_is_loss_streak_paused", return_value=(False, "")), \
             patch.object(pt, "extended_chase_signals", return_value=[]), \
             patch.object(pt, "_trailing_stop_pct", return_value=0.99), \
             patch.object(pt, "confidence_min", return_value=1), \
             patch.object(decision_agent, "_conf_scale", return_value=10):
            pt._execute_unlocked("US.SOXL",
                                 {"action": "WATCH_BUY", "confidence": 7},
                                 {"price": 249.77}, window, _manual=True)
        self.assertEqual(placed, [18], f"placed={placed}")


if __name__ == "__main__":
    unittest.main()
