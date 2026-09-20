"""R05/R06/R07/R08 audit-replication regression (2026-09-20 SYSTEM_REVIEW).

R05: BACKTEST_MODE flag 不能绕过显式 context 里的历史观点
R06: historical view 完全时间隔离 (soft_bl/calib/next_conj 也历史)
R07: Action enum canonical 值 (PROBE/ADD/EXIT/WATCH) 接入 ORDER_ACTIONS
R08: authority flag 0-broker-sell 也 warn
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


class R05_ContextOverridesBacktestModeFlag(unittest.TestCase):

    def test_context_thesis_wins_over_env_flag(self):
        # R05 复现: BACKTEST_MODE=1 + context 明确禁止 US.FOO → BUY 应 HOLD
        from decision_context import DecisionContext
        from decision_agent import _apply_thesis_filter
        with patch.dict(os.environ, {"BACKTEST_MODE": "1"}):
            snap = {"blacklist_tickers": ["US.FOO"],
                    "blacklist_reason": "explicit historical block"}
            ctx = DecisionContext(as_of=datetime.now(timezone.utc),
                                    thesis_snapshot=snap)
            decision = {"action": "BUY", "confidence": 8, "reason": "test"}
            out = _apply_thesis_filter(decision, "US.FOO", context=ctx)
            self.assertEqual(out["action"], "HOLD",
                              "R05: context 明确 block 应优先, env flag 不能绕过历史观点")
            self.assertTrue(out.get("thesis_blocked"))

    def test_no_context_env_flag_still_bypasses(self):
        # 兼容: 无 context 时 env flag 仍工作 (旧 caller)
        from decision_agent import _apply_thesis_filter
        with patch.dict(os.environ, {"BACKTEST_MODE": "1"}):
            decision = {"action": "BUY", "confidence": 8, "reason": "test"}
            out = _apply_thesis_filter(decision, "US.SOXL")   # live blacklisted
            self.assertEqual(out["action"], "BUY",
                              "无 context + BACKTEST_MODE=1 → 兼容旧路径, 跳过")


class R06_HistoricalViewFullyIsolated(unittest.TestCase):

    def test_historical_mode_soft_blacklist_not_live(self):
        # R06: as_of 模式下 soft_blacklist 不能返 live current 数据
        from webui import api_thesis_state
        r = api_thesis_state(as_of="2026-09-01")
        self.assertTrue(r.get("historical_mode"))
        # 2026-Q3 (原始) 没设 soft_blacklist → 应返 empty, 不是 live current 的 3 项
        self.assertEqual(r["soft_blacklist"], {},
                          "R06: historical 模式 soft_blacklist 应从历史 thesis body 读, 不返 live")

    def test_historical_calibration_marked_unavailable(self):
        from webui import api_thesis_state
        r = api_thesis_state(as_of="2026-09-01")
        # calibration 在 historical 模式应标 unavailable, 不是当前 age
        self.assertFalse(r["calibration"].get("exists"))
        self.assertEqual(r["calibration"].get("note"), "historical_unavailable")

    def test_pre_thesis_date_returns_historical_unknown(self):
        # 2020-01-01 早于所有 thesis 存在 → historical_unknown=True
        from webui import api_thesis_state
        r = api_thesis_state(as_of="2020-01-01")
        self.assertTrue(r.get("historical_unknown"))
        # current.ok 应 False (无历史证据)
        self.assertFalse(r["current"].get("ok"))
        # calibration note 应是 unknown 不是 unavailable
        self.assertEqual(r["calibration"].get("note"), "historical_unknown")

    def test_live_mode_returns_current(self):
        from webui import api_thesis_state
        r = api_thesis_state()   # no as_of
        self.assertFalse(r.get("historical_mode"))
        # live soft_blacklist 应有 3 项 (SHY/IEI/NBIS)
        self.assertGreater(len(r["soft_blacklist"]), 0)


class R07_ActionEnumBridgedToTradingContracts(unittest.TestCase):

    def test_canonical_probe_in_buy_actions(self):
        from trading_contracts import BUY_ACTIONS
        self.assertIn("PROBE", BUY_ACTIONS,
                        "R07: canonical Action.PROBE 必须在 BUY_ACTIONS 里, 否则 executor 忽略")

    def test_canonical_add_in_buy_actions(self):
        from trading_contracts import BUY_ACTIONS
        self.assertIn("ADD", BUY_ACTIONS)

    def test_canonical_exit_in_sell_actions(self):
        from trading_contracts import SELL_ACTIONS
        self.assertIn("EXIT", SELL_ACTIONS)

    def test_canonical_watch_in_non_executing(self):
        # WATCH 应是显示型, 不下单
        from trading_contracts import NON_EXECUTING_BULLISH_ACTIONS, ORDER_ACTIONS
        self.assertIn("WATCH", NON_EXECUTING_BULLISH_ACTIONS)
        self.assertNotIn("WATCH", ORDER_ACTIONS,
                          "R07: WATCH 是 non-executing, 不进 ORDER_ACTIONS")

    def test_new_probe_recognized_by_order_actions(self):
        from trading_contracts import ORDER_ACTIONS
        self.assertIn("PROBE", ORDER_ACTIONS,
                        "R07: PROBE 是 order action, 之前 audit 指出 executor 认不出")
        self.assertIn("ADD", ORDER_ACTIONS)
        self.assertIn("EXIT", ORDER_ACTIONS)


class R08_AuthorityFlagHandlesZeroSells(unittest.TestCase):

    def test_zero_broker_sells_still_warns_when_cohort_exists(self):
        # R08 复现: cohort > 0 但 broker sells = 0 → 之前不 warn (bug),
        # 现在应 warn "no_broker_evidence"
        import cohort_tracker
        with patch.object(cohort_tracker, "_load_closed_cohorts",
                            return_value=[{"is_winner": True, "realized_pnl_pct": 5.0,
                                           "realized_pnl_usd": 100, "hold_days": 3}]):
            # fill_ledger.get_fills 返 empty (0 sells)
            with patch("fill_ledger.get_fills", return_value=[]):
                s = cohort_tracker.stats(60)
        self.assertEqual(s["authority"], "cohort_ledger_only_no_broker_evidence")
        self.assertIsNotNone(s["warning"])
        # 明确说 0 SELL 或 no evidence
        w = s["warning"].lower()
        self.assertTrue("0 sell" in w or "no broker" in w or "无对账" in w)


if __name__ == "__main__":
    unittest.main()
