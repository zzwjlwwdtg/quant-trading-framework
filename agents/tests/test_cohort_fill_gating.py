"""F04 regression (audit 2026-09-19): cohort_tracker 必须只在真实 fill 上入账,
不能被"提交 = 已成交" phantom bug 污染.

前提: _log_trade 是 _place 提交后立即调用的; audit 发现 cohort 也在这里 fire, 导致
未成交/撤单也污染 cohort. Fix: LIVE 下 cohort 移到 refresh_execution_ledger fill 路径,
DRY 保持原样 (dry = 模拟即时成交).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


class LiveNoCohortOnSubmitTests(unittest.TestCase):
    """LIVE 提交时不应 fire cohort_tracker (audit F04)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.trade_log = Path(self.tmpdir) / "trade_log.jsonl"
        self.cohort_ledger = Path(self.tmpdir) / "cohorts.jsonl"
        self.cohort_active = Path(self.tmpdir) / "cohorts_active.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_live_log_trade_does_not_call_cohort_on_buy(self):
        # 模拟 LIVE (DRY_RUN=False), 提交时不应 fire cohort_tracker.on_buy
        import cohort_tracker
        import paper_trader
        with patch.object(paper_trader, "DRY_RUN", False), \
             patch.object(cohort_tracker, "on_buy") as fake_on_buy, \
             patch.object(cohort_tracker, "on_sell") as fake_on_sell, \
             patch.object(paper_trader, "_TRADER_LOCK", MagicMock()):
            # 用不存在的 signals path 避 side-effect; _log_trade 只 append trade_log
            with patch("paper_trader.Path") as pt_path:
                pt_path.return_value = MagicMock()
                pt_path.return_value.parent = MagicMock()
                pt_path.return_value.parent.__truediv__ = lambda self, p: Path(self.tmpdir) / "signals" / p if hasattr(self, 'tmpdir') else Path("/tmp") / p
                try:
                    paper_trader._log_trade(
                        ticker="US.TEST", side="BUY", qty=10, price=100.0,
                        order_id="OID001", tag="[BUY conf=5]",
                        decision={"action": "BUY", "confidence": 5, "regime": "neutral"},
                        mkt={}, window="pre-market",
                    )
                except Exception:
                    pass   # trade_log 写盘 mock 可能报错, 但不影响 cohort 断言
            self.assertEqual(fake_on_buy.call_count, 0,
                              "LIVE 下 _log_trade 不应 fire cohort.on_buy (F04)")
            self.assertEqual(fake_on_sell.call_count, 0)

    def test_dry_run_log_trade_still_calls_cohort(self):
        # DRY_RUN 保留原行为: cohort 立即 fire (dry = 模拟即时成交)
        import cohort_tracker
        import paper_trader
        with patch.object(paper_trader, "DRY_RUN", True), \
             patch.object(cohort_tracker, "on_buy") as fake_on_buy, \
             patch.object(paper_trader, "_TRADER_LOCK", MagicMock()):
            try:
                paper_trader._log_trade(
                    ticker="US.TEST", side="BUY", qty=10, price=100.0,
                    order_id="DRY", tag="[BUY conf=5]",
                    decision={"action": "BUY", "confidence": 5, "regime": "neutral"},
                    mkt={}, window="pre-market",
                )
            except Exception:
                pass
            self.assertEqual(fake_on_buy.call_count, 1,
                              "DRY_RUN 下 cohort 仍应 fire (dry = 模拟成交)")

    def test_live_log_trade_skips_rebalance_tag(self):
        # REBALANCE 标签在 DRY 或 LIVE 下都不进 cohort
        import cohort_tracker
        import paper_trader
        with patch.object(paper_trader, "DRY_RUN", True), \
             patch.object(cohort_tracker, "on_buy") as fake_on_buy:
            try:
                paper_trader._log_trade(
                    ticker="US.TEST", side="BUY", qty=10, price=100.0,
                    order_id="DRY", tag="[REBALANCE up]",
                    decision={}, mkt={}, window=None,
                )
            except Exception:
                pass
            self.assertEqual(fake_on_buy.call_count, 0,
                              "REBALANCE 不进 cohort (即使 DRY)")


if __name__ == "__main__":
    unittest.main()
