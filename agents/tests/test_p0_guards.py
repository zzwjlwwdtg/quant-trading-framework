"""P0 guard tests (2026-09-05 audit).

锁死 3 个 P0-CRITICAL fix:
- P0-1: auto_rebalance 信号不足时**绝不**执行 rebalance (防数据 outage 清仓)
- P0-2: _clear_protective_stop 必须先 CANCEL broker STOP (防 phantom short)
- P0-3: Pyramid add 必须重建 STOP 覆盖新总 qty (防 layer 2/3 裸奔)
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import auto_rebalance
import paper_trader


class P0_1_AutoRebalanceSignalSafeguardTests(unittest.TestCase):
    """P0-1: 信号数 < _MIN_SIGNALS_FOR_REBALANCE 时 skip."""

    def test_zero_signals_skips_rebalance(self):
        """全 stale (0 fresh signal) → skip, 不 SELL 现有仓位."""
        with patch.object(auto_rebalance, "_load_current_positions",
                          return_value=({"US.MSFT": {"qty": 100, "last_px": 500}}, 50_000, 1_000_000)), \
             patch.object(auto_rebalance, "_load_signals",
                          return_value=({}, {})):
            r = auto_rebalance.check_and_execute_rebalance(window="pre-close", dry_run=True)
        self.assertEqual(r["status"], "skipped_insufficient_signals")
        self.assertEqual(r["n_signals"], 0)
        self.assertGreaterEqual(r["min_required"], 3)

    def test_two_signals_still_skips(self):
        """只 2 个 signal, 仍 skip (< min 3)."""
        with patch.object(auto_rebalance, "_load_current_positions",
                          return_value=({"US.MSFT": {"qty": 100, "last_px": 500}}, 50_000, 1_000_000)), \
             patch.object(auto_rebalance, "_load_signals",
                          return_value=({"MSFT": {"action": "WATCH_BUY", "conf": 5, "regime": "neutral"},
                                          "IEI":  {"action": "HOLD",       "conf": 2, "regime": "neutral"}}, {})):
            r = auto_rebalance.check_and_execute_rebalance(window="pre-close", dry_run=True)
        self.assertEqual(r["status"], "skipped_insufficient_signals")

    def test_three_signals_proceeds(self):
        """3+ signal → 不 skip (至少走进 compute_target_weights)."""
        with patch.object(auto_rebalance, "_load_current_positions",
                          return_value=({}, 100_000, 100_000)), \
             patch.object(auto_rebalance, "_load_signals",
                          return_value=({"MSFT": {"action": "WATCH_BUY", "conf": 5, "regime": "neutral"},
                                          "IEI":  {"action": "WATCH_BUY", "conf": 5, "regime": "neutral"},
                                          "SHY":  {"action": "WATCH_BUY", "conf": 5, "regime": "neutral"}},
                                         {"MSFT": 500, "IEI": 116, "SHY": 82})):
            r = auto_rebalance.check_and_execute_rebalance(window="pre-close", dry_run=True)
        self.assertNotEqual(r.get("status"), "skipped_insufficient_signals")

    def test_min_signals_constant_exists_and_reasonable(self):
        self.assertTrue(hasattr(auto_rebalance, "_MIN_SIGNALS_FOR_REBALANCE"))
        self.assertGreaterEqual(auto_rebalance._MIN_SIGNALS_FOR_REBALANCE, 2)
        self.assertLessEqual(auto_rebalance._MIN_SIGNALS_FOR_REBALANCE, 10)


class P0_2_ProtectiveStopCancelTests(unittest.TestCase):
    """P0-2: _clear_protective_stop 必须先 CANCEL broker STOP."""

    def test_clear_calls_cancel_when_broker_order_id_present(self):
        tstate = {"protective_stop_price": 100.0, "protective_stop_order_id": "ORD123",
                  "protective_stop_mode": "broker",
                  "protective_stop_updated_utc": "2026-09-05T00:00:00Z"}
        with patch.object(paper_trader, "_cancel_broker_stop") as m_cancel, \
             patch.object(paper_trader, "DRY_RUN", False):
            paper_trader._clear_protective_stop(tstate)
        m_cancel.assert_called_once_with("ORD123")
        # 本地字段也清了
        self.assertNotIn("protective_stop_price", tstate)
        self.assertNotIn("protective_stop_order_id", tstate)

    def test_clear_skips_cancel_when_no_broker_order_id(self):
        """无 order_id (software-only stop) 不发 CANCEL."""
        tstate = {"protective_stop_price": 100.0, "protective_stop_mode": "software"}
        with patch.object(paper_trader, "_cancel_broker_stop") as m_cancel:
            paper_trader._clear_protective_stop(tstate)
        m_cancel.assert_not_called()

    def test_cancel_broker_stop_dry_run_returns_false_no_exc(self):
        """DRY_RUN 下 CANCEL 是 no-op, 不抛异常."""
        with patch.object(paper_trader, "DRY_RUN", True):
            self.assertFalse(paper_trader._cancel_broker_stop("ORD123"))

    def test_cancel_broker_stop_invalid_id_returns_false(self):
        with patch.object(paper_trader, "DRY_RUN", False):
            self.assertFalse(paper_trader._cancel_broker_stop(""))
            self.assertFalse(paper_trader._cancel_broker_stop(None))
            self.assertFalse(paper_trader._cancel_broker_stop("DRY"))


class P0_3_PyramidStopRebuildTests(unittest.TestCase):
    """P0-3: Pyramid 加仓后必须重建 broker STOP.

    这里做 white-box test: 直接测代码 path (在 paper_trader 里搜 PYRAMID L{layer+1}
    位置附近, 断言调 _cancel_broker_stop + _place_stop_loss)."""

    def test_pyramid_add_source_calls_cancel_and_place_stop(self):
        """AST/文本级断言: pyramid 分支同时调 _cancel_broker_stop 和 _place_stop_loss."""
        import inspect
        src = inspect.getsource(paper_trader)
        p_idx = src.find("PYRAMID L")
        self.assertGreater(p_idx, 0, "PYRAMID 分支不存在 (可能已重命名)")
        # 找 pyramid 段结束: 找 pyramid 后的下一个 def 或 明显新 block
        # 取 pyramid 前 200 字 + 后 2500 字 (足够覆盖 stop 重建 + state update)
        segment = src[max(0, p_idx - 200): p_idx + 2500]
        self.assertIn("_cancel_broker_stop", segment,
                      "P0-3 regressed: PYRAMID 分支未调 _cancel_broker_stop")
        self.assertIn("_place_stop_loss", segment,
                      "P0-3 regressed: PYRAMID 分支未调 _place_stop_loss")
        self.assertIn("protective_stop_order_id", segment,
                      "P0-3 regressed: PYRAMID 未更新 protective_stop_order_id")


if __name__ == "__main__":
    unittest.main()
