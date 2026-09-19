"""F04 deep regression (audit 2026-09-19): 只读对账 trade_log vs execution_ledger.

锁死:
- filled 事件 → reconciled_filled (dealt qty 记录)
- 有 submit 无 fill → legacy_unreconciled
- cancelled 事件 → reconciled_cancelled (phantom cohort 风险)
- partial fill → reconciled_partial + fill_pct
- DRY_RUN → dry_run (预期无 broker)
- Aggregate counts 正确
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import _reconcile_cohorts as rc


def _trade(ts, ticker, side, qty, price, order_id="1000", dry=False):
    return {
        "ts": ts, "ticker": ticker, "side": side, "qty": qty,
        "price": price, "order_id": order_id, "dry_run": dry,
    }


def _submit(oid, ts):
    return {"event": "submitted", "order_id": oid, "ts": ts}


def _fill(oid, ts, dealt, avg):
    return {"event": "filled", "order_id": oid, "ts": ts,
            "dealt_qty": dealt, "average_fill_price": avg}


def _partial(oid, ts, dealt, avg):
    return {"event": "partial", "order_id": oid, "ts": ts,
            "dealt_qty": dealt, "average_fill_price": avg}


def _cancel(oid, ts):
    return {"event": "cancelled", "order_id": oid, "ts": ts}


class ReconcileMatchingTests(unittest.TestCase):

    def test_full_fill_marked_reconciled_filled(self):
        trades = [_trade("2026-08-01T14:00:00Z", "US.NBIS", "BUY", 100, 200.0, "O1")]
        ledger = [_submit("O1", "2026-08-01T14:00:00Z"),
                  _fill("O1", "2026-08-01T14:05:00Z", 100.0, 199.5)]
        idx = rc._index_fill_events(ledger)
        r = rc.reconcile(trades, idx)
        self.assertEqual(r["counts"]["reconciled_filled"], 1)
        detail = r["per_trade"][0]
        self.assertEqual(detail["reconcile_status"], "reconciled_filled")
        self.assertEqual(detail["dealt_qty"], 100.0)
        self.assertEqual(detail["avg_fill"], 199.5)

    def test_partial_fill_marked_reconciled_partial(self):
        trades = [_trade("2026-08-01T14:00:00Z", "US.NBIS", "BUY", 100, 200.0, "O2")]
        ledger = [_submit("O2", "2026-08-01T14:00:00Z"),
                  _partial("O2", "2026-08-01T14:05:00Z", 60.0, 200.5)]
        idx = rc._index_fill_events(ledger)
        r = rc.reconcile(trades, idx)
        self.assertEqual(r["counts"]["reconciled_partial"], 1)
        detail = r["per_trade"][0]
        self.assertEqual(detail["fill_pct"], 60.0)

    def test_cancelled_marked_reconciled_cancelled(self):
        # audit 关键: cancelled 但已经进 cohort 是 phantom risk
        trades = [_trade("2026-08-01T14:00:00Z", "US.NBIS", "BUY", 100, 200.0, "O3")]
        ledger = [_submit("O3", "2026-08-01T14:00:00Z"),
                  _cancel("O3", "2026-08-01T14:03:00Z")]
        idx = rc._index_fill_events(ledger)
        r = rc.reconcile(trades, idx)
        self.assertEqual(r["counts"]["reconciled_cancelled"], 1)
        self.assertIn("phantom cohort", r["per_trade"][0]["note"])

    def test_no_fill_event_marked_legacy_unreconciled(self):
        # 无 execution_ledger 事件 → 老数据无法对账
        trades = [_trade("2026-07-15T14:00:00Z", "US.KLAC", "BUY", 50, 220.0, "OLD_OID")]
        idx = rc._index_fill_events([])   # empty ledger
        r = rc.reconcile(trades, idx)
        self.assertEqual(r["counts"]["legacy_unreconciled"], 1)

    def test_dry_run_marked_dry_run_not_unreconciled(self):
        # DRY_RUN 明确豁免, 不进 legacy_unreconciled
        trades = [_trade("2026-08-01T14:00:00Z", "US.TQQQ", "BUY", 10, 70.0, "DRY", dry=True)]
        r = rc.reconcile(trades, {})
        self.assertEqual(r["counts"]["dry_run"], 1)
        self.assertNotIn("legacy_unreconciled", r["counts"])


class AggregateReportTests(unittest.TestCase):

    def test_mixed_batch_aggregate_counts(self):
        trades = [
            _trade("2026-08-01T14:00:00Z", "A", "BUY", 10, 100, "F1"),      # filled
            _trade("2026-08-01T15:00:00Z", "B", "BUY", 10, 100, "C1"),      # cancelled
            _trade("2026-08-01T16:00:00Z", "C", "BUY", 10, 100, "MISSING"), # unreconciled
            _trade("2026-08-01T17:00:00Z", "D", "BUY", 10, 100, "DRY", dry=True),  # dry
        ]
        ledger = [
            _submit("F1", "2026-08-01T14:00:01Z"),
            _fill("F1", "2026-08-01T14:00:05Z", 10.0, 100.5),
            _submit("C1", "2026-08-01T15:00:01Z"),
            _cancel("C1", "2026-08-01T15:00:10Z"),
        ]
        idx = rc._index_fill_events(ledger)
        r = rc.reconcile(trades, idx)
        self.assertEqual(r["total_trades"], 4)
        self.assertEqual(r["counts"]["reconciled_filled"], 1)
        self.assertEqual(r["counts"]["reconciled_cancelled"], 1)
        self.assertEqual(r["counts"]["legacy_unreconciled"], 1)
        self.assertEqual(r["counts"]["dry_run"], 1)


class DateFilterTests(unittest.TestCase):

    def test_min_date_excludes_earlier_trades(self):
        trades = [
            _trade("2026-07-01T14:00:00Z", "OLD", "BUY", 10, 100, "OLD1"),
            _trade("2026-09-01T14:00:00Z", "NEW", "BUY", 10, 100, "NEW1"),
        ]
        r = rc.reconcile(trades, {}, min_date="2026-08-01")
        self.assertEqual(r["total_trades"], 1)
        self.assertEqual(r["per_trade"][0]["ticker"], "NEW")


if __name__ == "__main__":
    unittest.main()
