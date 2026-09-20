"""2026-09-20 followup audit (FIX_REVIEW.md) regression tests.

关键: 真调用生产 flow (refresh_execution_ledger / get_position 完整), 不重写公式.

- R01/R02 regression: 上轮 fix 反了 — seen[oid]=signature 先于 cohort 回调
  → prev_dealt = current dealt → delta_qty=0 → 从不入账. 复现: 两次 partial
  fill 应触 2 次 cohort.on_buy, bug 期间 0 次.
- R03 interleaved: Buy@100 → Sell@110 → Buy@120 (same oid B for buys), 正确
  剩余持仓 5@120, realized=50. Bug: 压缩 oid 后按 last-ts 排, 变 550.
- R05: context.is_backtest → _llm_call 应返 None.
- R06: 空 archive + pre-live-created 请求应 historical_unknown.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


class R01R02_ProductionFlowCohortCalledPerFill(unittest.TestCase):
    """真调 refresh_execution_ledger, 断言 cohort.on_buy 被调用."""

    def test_two_partial_fills_trigger_two_on_buy_calls(self):
        import paper_trader
        import cohort_tracker

        # Fake broker orders_query response: 两次调用返回两个累计状态
        # 第 1 次: dealt 5 avg 100 (partial)
        # 第 2 次: dealt 10 avg 110 (filled)
        import pandas as pd

        call_state = {"n": 0}
        def fake_order_list_query(**kw):
            call_state["n"] += 1
            if call_state["n"] == 1:
                df = pd.DataFrame([{
                    "order_id": "OID_TEST_R01", "qty": 10, "dealt_qty": 5.0,
                    "dealt_avg_price": 100.0, "order_status": "SUBMITTED",
                }])
            else:
                df = pd.DataFrame([{
                    "order_id": "OID_TEST_R01", "qty": 10, "dealt_qty": 10.0,
                    "dealt_avg_price": 110.0, "order_status": "FILLED_ALL",
                }])
            return (0, df)   # RET_OK = 0

        # 预置 execution_ledger 里有一个 submitted event
        tmpdir = tempfile.mkdtemp()
        exec_log = Path(tmpdir) / "execution_ledger.jsonl"
        submit_event = {
            "event": "submitted", "order_id": "OID_TEST_R01",
            "ticker": "US.TEST_R01", "side": "BUY",
            "requested_qty": 10, "order_price": 100.0,
            "reference_price": 100.0, "plan": {}, "ts": "2026-09-20T14:00:00Z",
        }
        with open(exec_log, "w", encoding="utf-8") as f:
            f.write(json.dumps(submit_event) + "\n")

        # Fake state 存 seen 空
        state = {}

        def fake_state_load():
            return state

        def fake_state_save(s):
            nonlocal state
            state = s

        # Mock ctx and RET_OK, DRY_RUN, ACC_ID, TRD_ENV
        fake_ctx = MagicMock()
        fake_ctx.order_list_query = fake_order_list_query
        buy_calls = []
        def fake_on_buy(ticker, price, qty, signal_ctx=None, ts=None, context=None):
            buy_calls.append({"ticker": ticker, "price": price, "qty": qty})
            return {}

        with patch.object(paper_trader, "EXECUTION_LOG_PATH", exec_log), \
             patch.object(paper_trader, "DRY_RUN", False), \
             patch.object(paper_trader, "_ctx_get", return_value=fake_ctx), \
             patch.object(paper_trader, "_state_load", side_effect=fake_state_load), \
             patch.object(paper_trader, "_state_save", side_effect=fake_state_save), \
             patch.object(paper_trader, "RET_OK", 0), \
             patch.object(paper_trader, "TRD_ENV", "SIMULATE"), \
             patch.object(paper_trader, "ACC_ID", 12345), \
             patch.object(cohort_tracker, "on_buy", side_effect=fake_on_buy):
            # First reconcile: partial 5@100
            paper_trader.refresh_execution_ledger()
            # Second reconcile: filled +5@120 (broker cum 10@110)
            paper_trader.refresh_execution_ledger()

        # R01/R02 关键断言: cohort.on_buy 至少被调 2 次
        self.assertGreaterEqual(len(buy_calls), 2,
                                  f"R01/R02: 2 partial 应触 2 次 on_buy, 实际 {len(buy_calls)} 次: {buy_calls}")
        # First: 5 shares @100
        self.assertEqual(buy_calls[0]["qty"], 5)
        self.assertEqual(buy_calls[0]["price"], 100.0)
        # Second: 5 shares @120 (per R01 formula: (10*110 - 5*100)/5 = 120)
        self.assertEqual(buy_calls[1]["qty"], 5)
        self.assertAlmostEqual(buy_calls[1]["price"], 120.0, places=2,
                                 msg="R01: 第二 batch 增量价应是 120, 不是 batch avg 110")


class R03_InterleavedSameOrderIntersectsWithOtherSells(unittest.TestCase):
    """R03 followup: Buy oid B partial → Sell oid S → Buy oid B partial (累积)
    正确: 剩 5@120, realized=50. Bug: 550."""

    def test_interleaved_buys_and_sells_correct_avg_cost(self):
        import fill_ledger as fl

        events = [
            # 14:00 - Buy B partial 5@100
            {"event": "partial", "order_id": "B", "ts": "2026-09-20T14:00:00Z",
             "ticker": "US.TEST_R03", "side": "BUY",
             "dealt_qty": 5.0, "average_fill_price": 100.0},
            # 14:01 - Sell S filled 5@110
            {"event": "filled", "order_id": "S", "ts": "2026-09-20T14:01:00Z",
             "ticker": "US.TEST_R03", "side": "SELL",
             "dealt_qty": 5.0, "average_fill_price": 110.0},
            # 14:02 - Buy B filled +5@120 (broker 累计 10@110)
            {"event": "filled", "order_id": "B", "ts": "2026-09-20T14:02:00Z",
             "ticker": "US.TEST_R03", "side": "BUY",
             "dealt_qty": 10.0, "average_fill_price": 110.0},
        ]

        with patch.object(fl, "_load_ledger", return_value=events):
            pos = fl.get_position("US.TEST_R03")

        # R03 audit expected: 剩 5, avg_cost=120, realized=50
        self.assertEqual(pos["qty"], 5,
                          f"R03: 剩余持仓应 5, 实际 {pos['qty']}")
        self.assertAlmostEqual(pos["avg_cost"], 120.0, places=2,
                                msg=f"R03: 剩仓 avg_cost 应 120 (最后 buy), 实际 {pos['avg_cost']}")
        self.assertAlmostEqual(pos["realized_pnl"], 50.0, places=2,
                                msg=f"R03: realized 应 50 (5*(110-100)), 实际 {pos['realized_pnl']}")


class R05_ContextIsBacktestBypassesLLM(unittest.TestCase):

    def test_context_is_backtest_true_returns_none_without_env(self):
        from decision_agent import _llm_call
        from decision_context import DecisionContext
        # Ensure BACKTEST_MODE NOT set
        env_clean = {k: v for k, v in os.environ.items() if k != "BACKTEST_MODE"}
        with patch.dict(os.environ, env_clean, clear=True):
            ctx = DecisionContext(as_of=datetime.now(timezone.utc),
                                    is_backtest=True)
            r = _llm_call("sys", {"price": 100}, {}, {}, ("price",), (),
                           context=ctx)
            self.assertIsNone(r,
                              "R05 followup: context.is_backtest=True 应绕过 LLM, 即使无 env flag")

    def test_context_is_backtest_false_still_can_call(self):
        # is_backtest=False + no OPENAI_API_KEY → 返 None (但不是 backtest guard 触发)
        from decision_agent import _llm_call
        from decision_context import DecisionContext
        env_clean = {k: v for k, v in os.environ.items() if k != "BACKTEST_MODE"}
        with patch.dict(os.environ, env_clean, clear=True):
            ctx = DecisionContext(as_of=datetime.now(timezone.utc),
                                    is_backtest=False)
            # 无 key 也 None, 但这是 API-key guard, 不是 backtest guard
            r = _llm_call("sys", {"price": 100}, {}, {}, ("price",), (),
                           context=ctx)
            self.assertIsNone(r)


class R06_EmptyArchivePreLiveReturnsUnknown(unittest.TestCase):

    def test_pre_live_thesis_created_date_marks_unknown_even_with_empty_archive(self):
        from webui import api_thesis_state
        # 用真实 archive; live thesis created_at = "2026-08-24"
        # 请求 2020-01-01 → 应 historical_unknown=True
        r = api_thesis_state(as_of="2020-01-01")
        self.assertTrue(r.get("historical_unknown"),
                          f"R06 followup: 早于 live thesis created_at 应 unknown, actual={r}")


if __name__ == "__main__":
    unittest.main()
