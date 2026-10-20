"""券商订单手续费 (数据项, 2026-10-02).

Codex: "统计没有计入费用". 修复:
- _broker_history_reconcile 只读调用 order_fee_query (加入白名单), 结果另存
  broker_order_fees_*.jsonl; --import-fees 写入 signals/broker_order_fees.jsonl.
- fill_ledger.load_order_fees() 读取 {order_id: fee | None}.
- stats_from_fills 报告窗口内成交订单的手续费、覆盖率与扣费后实现盈亏;
  没有费用记录时明确写"未计入", 不当作 0.
"""
from __future__ import annotations

import argparse
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

import cohort_tracker as ct
import fill_ledger as fl
import _broker_history_reconcile as bh


def _ts(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _f(oid, ts, side, qty, px, tk="US.X"):
    return {"event": "filled", "order_id": oid, "ts": ts, "ticker": tk,
            "side": side, "dealt_qty": qty, "average_fill_price": px}


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


EVENTS = [_f("B0", _ts(100), "BUY", 10, 100.0),     # 窗口外买入
          _f("B1", _ts(5), "BUY", 10, 100.0),
          _f("S1", _ts(2), "SELL", 20, 110.0)]       # 实现 +200


class LoadFees(unittest.TestCase):
    def test_load_last_wins_and_null_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "fees.jsonl"
            _write(p, [{"order_id": "A", "fee_amount": 1.0},
                       {"order_id": "A", "fee_amount": 2.5},
                       {"order_id": "B", "fee_amount": None}])
            self.assertEqual(fl.load_order_fees(p), {"A": 2.5, "B": None})

    def test_missing_file_is_empty(self):
        self.assertEqual(fl.load_order_fees(Path("/nonexistent/x.jsonl")), {})


class StatsFees(unittest.TestCase):
    def _stats(self, fees):
        with patch.object(fl, "_load_ledger", return_value=EVENTS), \
             patch.object(fl, "_load_corporate_actions", return_value=[]), \
             patch.object(fl, "load_order_fees", return_value=fees):
            return ct.stats_from_fills(since_days=30)

    def test_no_fee_data_is_not_zero(self):
        s = self._stats({})
        self.assertEqual(s["total_pnl_usd"], 200.0)
        self.assertEqual(s["fee_status"], "no_data")
        self.assertIsNone(s["fees_usd"])
        self.assertIsNone(s["total_pnl_net_usd"])

    def test_full_coverage_net(self):
        s = self._stats({"B0": 9.0, "B1": 1.5, "S1": 2.0})   # B0 在窗口外, 不计
        self.assertEqual(s["fee_status"], "complete")
        self.assertEqual(s["fees_usd"], 3.5)
        self.assertEqual(s["fee_orders"], {"known": 2, "unknown": 0})
        self.assertEqual(s["total_pnl_net_usd"], 196.5)

    def test_partial_coverage_flagged(self):
        s = self._stats({"S1": 2.0, "B1": None})
        self.assertEqual(s["fee_status"], "partial")
        self.assertEqual(s["fee_orders"], {"known": 1, "unknown": 1})
        self.assertEqual(s["fees_usd"], 2.0)
        self.assertEqual(s["total_pnl_net_usd"], 198.0)

    def test_format_stats_wording(self):
        for fees, needle in (({}, "手续费: 未计入"),
                             ({"B1": 1.5, "S1": 2.0}, "扣费后实现 P&L")):
            with patch.object(fl, "_load_ledger", return_value=EVENTS), \
                 patch.object(fl, "_load_corporate_actions", return_value=[]), \
                 patch.object(fl, "load_order_fees", return_value=fees), \
                 patch.object(ct, "stats", return_value={"n": 0, "total_pnl_usd": 0}), \
                 patch.object(ct, "all_active_cohorts", return_value=[]):
                self.assertIn(needle, ct.format_stats(since_days=30))


class ReconcileFeeQuery(unittest.TestCase):
    def test_fee_query_whitelisted_but_trading_still_blocked(self):
        self.assertIn("order_fee_query", bh.ALLOWED)
        class Fake:
            def place_order(self, *a, **k): raise AssertionError
            def order_fee_query(self, **k): return 0, None
        ctx = bh.ReadOnlyCtx(Fake())
        ctx.order_fee_query
        for bad in ("place_order", "modify_order", "unlock_trade", "cancel_all_order"):
            with self.assertRaises(PermissionError):
                getattr(ctx, bad)

    def test_fetch_fees_chunks_and_parses(self):
        import pandas as pd
        calls = []
        class Fake:
            def order_fee_query(self, order_id_list, trd_env, acc_id):
                calls.append(list(order_id_list))
                return 0, pd.DataFrame([{"order_id": o, "fee_amount": "N/A" if o == "2" else 1.25,
                                         "fee_details": [("Commission", 1.25)]}
                                        for o in order_id_list])
        ids = [str(i) for i in range(5)]
        with patch.object(bh, "FEE_CHUNK", 2):
            rows, status = bh.fetch_fees(Fake(), "SIMULATE", 1, ids, sleep=lambda s: None)
        self.assertEqual([len(c) for c in calls], [2, 2, 1])
        self.assertEqual(status, "ok")
        by = {r["order_id"]: r for r in rows}
        self.assertEqual(by["1"]["fee_amount"], 1.25)
        self.assertIsNone(by["2"]["fee_amount"])

    def test_fetch_fees_unsupported_does_not_raise(self):
        class Fake:
            def order_fee_query(self, **k): return -1, "模拟账户不支持"
        rows, status = bh.fetch_fees(Fake(), "SIMULATE", 1, ["1"], sleep=lambda s: None)
        self.assertEqual(rows, [])
        self.assertTrue(status.startswith("unsupported"))
        self.assertIn("模拟账户不支持", status)

    def test_import_fees_dedup_rewrite(self):
        with tempfile.TemporaryDirectory() as td:
            src, out = Path(td) / "f.jsonl", Path(td) / "signals" / "fees.jsonl"
            _write(src, [{"order_id": "1", "fee_amount": 1.0},
                         {"order_id": "1", "fee_amount": 1.5},
                         {"order_id": "2", "fee_amount": None}])
            self.assertEqual(bh.import_fees(src, out), 2)
            self.assertEqual(fl.load_order_fees(out), {"1": 1.5, "2": None})
            self.assertEqual(bh.import_fees(src, out), 2)   # 可重复


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
