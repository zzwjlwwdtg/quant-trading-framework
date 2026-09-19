"""F04 regression (audit 2026-09-19): cohort_tracker 必须只在真实 fill 上入账,
不能被"提交 = 已成交" phantom bug 污染.

前提: _log_trade 是 _place 提交后立即调用的; audit 发现 cohort 也在这里 fire, 导致
未成交/撤单也污染 cohort. Fix: LIVE 下 cohort 移到 refresh_execution_ledger fill 路径,
DRY 保持原样 (dry = 模拟即时成交).

Test hygiene fix (2026-09-19): 之前 mock 未生效, 33 条 US.TEST 行泄漏到
production trade_log.jsonl. 现在 monkey-patch _log_trade 的 open() 到 tempfile,
彻底隔离测试写盘.
"""
from __future__ import annotations

import builtins
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


class _IsolatedTradeLogMixin:
    """把 _log_trade 的 open() 重定向到内存 buffer, 完全不写盘."""

    def setUp(self):
        self._buf = io.StringIO()
        real_open = builtins.open

        def fake_open(path, mode="r", *a, **kw):
            # 只拦截 trade_log.jsonl 的写入; 其他 open (json.load 等) 正常
            if "trade_log.jsonl" in str(path) and "a" in mode:
                return self._buf
            return real_open(path, mode, *a, **kw)

        self._open_patch = patch("builtins.open", side_effect=fake_open)
        self._open_patch.start()

    def tearDown(self):
        self._open_patch.stop()


class LiveNoCohortOnSubmitTests(_IsolatedTradeLogMixin, unittest.TestCase):

    def test_live_log_trade_does_not_call_cohort_on_buy(self):
        import cohort_tracker
        import paper_trader
        with patch.object(paper_trader, "DRY_RUN", False), \
             patch.object(cohort_tracker, "on_buy") as fake_on_buy, \
             patch.object(cohort_tracker, "on_sell") as fake_on_sell:
            paper_trader._log_trade(
                ticker="US.TEST_ISOLATED_NOFILE", side="BUY", qty=10, price=100.0,
                order_id="OID001", tag="[BUY conf=5]",
                decision={"action": "BUY", "confidence": 5, "regime": "neutral"},
                mkt={}, window="pre-market",
            )
            self.assertEqual(fake_on_buy.call_count, 0,
                              "LIVE 下 _log_trade 不应 fire cohort.on_buy (F04)")
            self.assertEqual(fake_on_sell.call_count, 0)

    def test_dry_run_log_trade_still_calls_cohort(self):
        import cohort_tracker
        import paper_trader
        with patch.object(paper_trader, "DRY_RUN", True), \
             patch.object(cohort_tracker, "on_buy") as fake_on_buy:
            paper_trader._log_trade(
                ticker="US.TEST_ISOLATED_NOFILE", side="BUY", qty=10, price=100.0,
                order_id="DRY", tag="[BUY conf=5]",
                decision={"action": "BUY", "confidence": 5, "regime": "neutral"},
                mkt={}, window="pre-market",
            )
            self.assertEqual(fake_on_buy.call_count, 1,
                              "DRY_RUN 下 cohort 仍应 fire (dry = 模拟成交)")

    def test_live_log_trade_skips_rebalance_tag(self):
        import cohort_tracker
        import paper_trader
        with patch.object(paper_trader, "DRY_RUN", True), \
             patch.object(cohort_tracker, "on_buy") as fake_on_buy:
            paper_trader._log_trade(
                ticker="US.TEST_ISOLATED_NOFILE", side="BUY", qty=10, price=100.0,
                order_id="DRY", tag="[REBALANCE up]",
                decision={}, mkt={}, window=None,
            )
            self.assertEqual(fake_on_buy.call_count, 0,
                              "REBALANCE 不进 cohort (即使 DRY)")


if __name__ == "__main__":
    unittest.main()
