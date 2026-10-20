"""2026-10-20: 最后一个对账窗口之后成交的订单, 次日仍要补回.

order_list_query 只返回当前交易日; 10/16 TQQQ 15:46 ET 成交发生在 15:45 对账
之后, 次日 today-only 查询已看不到它, execution ledger 永久缺失. 现在用
history_order_list_query 覆盖未结委托的提交日.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import pandas as pd
import stop_line_guard


def _order_row(oid, qty, dealt, avg, status, when, code="US.TQQQ"):
    return {"order_id": oid, "code": code, "qty": qty, "dealt_qty": dealt,
            "dealt_avg_price": avg, "order_status": status,
            "updated_time": when.strftime("%Y-%m-%d %H:%M:%S"),
            "create_time": when.strftime("%Y-%m-%d %H:%M:%S")}


class FakeCtx:
    def __init__(self, history_df=None, today_df=None, history_exc=None):
        self.history_df = pd.DataFrame() if history_df is None else history_df
        self.today_df = pd.DataFrame() if today_df is None else today_df
        self.history_exc = history_exc
        self.history_kwargs = None
        self.today_calls = 0

    def history_order_list_query(self, **kw):
        self.history_kwargs = kw
        if self.history_exc:
            raise self.history_exc
        return 0, self.history_df

    def order_list_query(self, **kw):
        self.today_calls += 1
        return 0, self.today_df


class HistoryBackfill(unittest.TestCase):
    def setUp(self):
        import cohort_tracker as ct
        import paper_trader as pt
        self.pt, self.ct = pt, ct
        self.tmp = Path(tempfile.mkdtemp())
        self.exec_log = self.tmp / "execution.jsonl"
        self.cohort_ledger = self.tmp / "cohort_fired.jsonl"
        self.stop_lines = self.tmp / "stop_lines.json"
        self.stop_blocks = self.tmp / "stop_blocks.jsonl"

    def _submit(self, oid, qty, price, when):
        self.exec_log.write_text(json.dumps({
            "event": "submitted", "order_id": oid, "ticker": "US.TQQQ",
            "side": "BUY", "requested_qty": qty, "order_price": price,
            "reference_price": price, "ts": when.isoformat(),
        }) + "\n", encoding="utf-8")

    def _run(self, ctx):
        with patch.object(self.pt, "EXECUTION_LOG_PATH", self.exec_log), \
             patch.object(self.pt, "COHORT_FIRED_LEDGER_PATH", self.cohort_ledger), \
             patch.object(self.pt, "DRY_RUN", False), \
             patch.object(self.pt, "_ctx_get", return_value=ctx), \
             patch.object(self.pt, "_state_load", return_value={}), \
             patch.object(self.pt, "_state_save"), \
             patch.object(stop_line_guard, "LINES", self.stop_lines), \
             patch.object(stop_line_guard, "BLOCKS", self.stop_blocks), \
             patch.object(self.ct, "on_buy"), \
             patch.object(self.ct, "on_sell"):
            self.pt.refresh_execution_ledger()
        return [json.loads(line) for line in
                self.exec_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_fill_after_last_window_recovered_from_history(self):
        submitted = datetime.now(timezone.utc) - timedelta(days=5)
        self._submit("474274", 296, 67.86, submitted)
        filled_at = submitted + timedelta(days=2)
        ctx = FakeCtx(history_df=pd.DataFrame([
            _order_row("474274", 296, 296, 67.878, "FILLED_ALL", filled_at)
        ]), today_df=pd.DataFrame())
        rows = self._run(ctx)
        fills = [r for r in rows if r.get("event") == "filled"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["order_id"], "474274")
        self.assertEqual(fills[0]["dealt_qty"], 296)
        self.assertIn("broker_time", fills[0])
        self.assertTrue(fills[0]["broker_time"])
        # 查询起点必须回到提交日, 而不是只看今天
        start = datetime.strptime(ctx.history_kwargs["start"], "%Y-%m-%d %H:%M:%S")
        self.assertLessEqual(start.date(), submitted.date())

    def test_history_covers_old_submission_within_cap(self):
        submitted = datetime.now(timezone.utc) - timedelta(days=20)
        self._submit("OLD", 10, 100.0, submitted)
        ctx = FakeCtx(today_df=pd.DataFrame())
        self._run(ctx)
        start = datetime.strptime(ctx.history_kwargs["start"], "%Y-%m-%d %H:%M:%S")
        self.assertLessEqual(start.date(), submitted.date())

    def test_history_failure_falls_back_to_today(self):
        submitted = datetime.now(timezone.utc)
        self._submit("T1", 10, 100.0, submitted)
        ctx = FakeCtx(today_df=pd.DataFrame([
            _order_row("T1", 10, 10, 100.5, "FILLED_ALL", submitted)
        ]), history_exc=RuntimeError("history unavailable"))
        rows = self._run(ctx)
        self.assertEqual(len([r for r in rows if r.get("event") == "filled"]), 1)
        self.assertEqual(ctx.today_calls, 1)

    def test_no_submitted_orders_skips_broker_queries(self):
        self.exec_log.write_text(json.dumps({
            "event": "filled", "order_id": "X", "ticker": "US.TQQQ",
            "side": "BUY", "dealt_qty": 1, "average_fill_price": 1.0,
        }) + "\n", encoding="utf-8")
        ctx = FakeCtx(history_exc=AssertionError("must not query"))
        rows = self._run(ctx)
        self.assertIsNone(ctx.history_kwargs)
        self.assertEqual(ctx.today_calls, 0)
        self.assertEqual(len(rows), 1)


class OpenOrderTruth(unittest.TestCase):
    def _tickers(self, ctx):
        import paper_trader as pt
        with patch.object(pt, "_ctx_get", return_value=ctx):
            return pt._open_order_tickers()

    def test_prior_day_gtc_order_visible_from_history(self):
        submitted = datetime.now(timezone.utc) - timedelta(days=3)
        ctx = FakeCtx(history_df=pd.DataFrame([
            _order_row("GTC1", 10, 0, 0, "SUBMITTED", submitted)
        ]), today_df=pd.DataFrame())
        self.assertEqual(self._tickers(ctx), {"US.TQQQ"})
        start = datetime.strptime(ctx.history_kwargs["start"], "%Y-%m-%d %H:%M:%S")
        self.assertLessEqual(start.date(), submitted.date())

    def test_today_working_order_visible_with_empty_history(self):
        now = datetime.now(timezone.utc)
        ctx = FakeCtx(history_df=pd.DataFrame(), today_df=pd.DataFrame([
            _order_row("T1", 5, 0, 0, "SUBMITTED", now, code="US.DRAM")
        ]))
        self.assertEqual(self._tickers(ctx), {"US.DRAM"})

    def test_terminal_orders_ignored(self):
        now = datetime.now(timezone.utc)
        ctx = FakeCtx(history_df=pd.DataFrame([
            _order_row("H1", 10, 10, 1.0, "FILLED_ALL", now),
            _order_row("H2", 10, 0, 0, "CANCELLED_ALL", now),
        ]), today_df=pd.DataFrame([
            _order_row("T1", 10, 0, 0, "CANCELLED_PART", now),
        ]))
        self.assertEqual(self._tickers(ctx), set())

    def test_history_failure_fails_closed(self):
        ctx = FakeCtx(history_exc=RuntimeError("history unavailable"))
        self.assertIsNone(self._tickers(ctx))

    def test_today_failure_fails_closed(self):
        ctx = FakeCtx()
        with patch.object(ctx, "order_list_query", side_effect=RuntimeError("down")):
            self.assertIsNone(self._tickers(ctx))


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
