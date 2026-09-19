"""WP03 (audit 2026-09-19) — fill_ledger 权威 fill 读取器测试.

锁死:
- get_fills 只返 filled/partial 事件 (submitted / cancelled 不进)
- ticker 过滤支持裸 + prefixed
- since 时间过滤
- get_position 从 fills 派生 net qty (buys - sells)
- get_cash_flow 派生 gross in/out + net
- 同 oid 的多 partial 事件取最后 (avoid double-count)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import fill_ledger as fl


def _fill(oid, ts, ticker, side, dealt, avg):
    return {
        "event": "filled", "order_id": oid, "ts": ts,
        "ticker": ticker, "side": side,
        "dealt_qty": dealt, "average_fill_price": avg,
    }


def _partial(oid, ts, ticker, side, dealt, avg):
    return {
        "event": "partial", "order_id": oid, "ts": ts,
        "ticker": ticker, "side": side,
        "dealt_qty": dealt, "average_fill_price": avg,
    }


def _submit(oid, ts, ticker, side, requested):
    return {
        "event": "submitted", "order_id": oid, "ts": ts,
        "ticker": ticker, "side": side, "requested_qty": requested,
    }


def _cancel(oid, ts, ticker, side):
    return {
        "event": "cancelled", "order_id": oid, "ts": ts,
        "ticker": ticker, "side": side,
    }


class GetFillsTests(unittest.TestCase):

    def test_only_filled_and_partial_events_returned(self):
        events = [
            _submit("O1", "2026-08-01T14:00:00Z", "US.SOXL", "BUY", 10),
            _fill("O1", "2026-08-01T14:05:00Z", "US.SOXL", "BUY", 10, 100.0),
            _submit("O2", "2026-08-02T14:00:00Z", "US.SOXL", "BUY", 10),
            _cancel("O2", "2026-08-02T14:03:00Z", "US.SOXL", "BUY"),
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            fills = fl.get_fills()
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["order_id"], "O1")

    def test_ticker_filter_prefixed(self):
        events = [
            _fill("A", "2026-08-01T14:00:00Z", "US.SOXL", "BUY", 10, 100.0),
            _fill("B", "2026-08-01T14:00:00Z", "US.NBIS", "BUY", 5, 200.0),
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            soxl_only = fl.get_fills(ticker="US.SOXL")
        self.assertEqual(len(soxl_only), 1)
        self.assertEqual(soxl_only[0]["ticker"], "US.SOXL")

    def test_ticker_filter_bare(self):
        events = [
            _fill("A", "2026-08-01T14:00:00Z", "US.SOXL", "BUY", 10, 100.0),
            _fill("B", "2026-08-01T14:00:00Z", "US.NBIS", "BUY", 5, 200.0),
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            # 裸 SOXL 应匹配 US.SOXL
            fills = fl.get_fills(ticker="SOXL")
        self.assertEqual(len(fills), 1)

    def test_since_filter(self):
        events = [
            _fill("A", "2026-07-01T14:00:00Z", "US.SOXL", "BUY", 10, 100.0),
            _fill("B", "2026-09-01T14:00:00Z", "US.SOXL", "BUY", 10, 105.0),
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            recent = fl.get_fills(since="2026-08-01")
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["order_id"], "B")


class GetPositionTests(unittest.TestCase):

    def test_position_from_single_fill(self):
        events = [_fill("O1", "2026-08-01T14:00:00Z", "US.SOXL", "BUY", 100, 30.0)]
        with patch.object(fl, "_load_ledger", return_value=events):
            pos = fl.get_position("US.SOXL")
        self.assertEqual(pos["qty"], 100)
        self.assertEqual(pos["avg_cost"], 30.0)
        self.assertEqual(pos["total_bought"], 100)
        self.assertEqual(pos["total_sold"], 0)

    def test_position_from_buy_sell(self):
        events = [
            _fill("O1", "2026-08-01T14:00:00Z", "US.SOXL", "BUY", 100, 30.0),
            _fill("O2", "2026-08-05T14:00:00Z", "US.SOXL", "SELL", 40, 33.0),
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            pos = fl.get_position("US.SOXL")
        self.assertEqual(pos["qty"], 60)   # 100 buy - 40 sell
        self.assertEqual(pos["total_bought"], 100)
        self.assertEqual(pos["total_sold"], 40)

    def test_partial_events_same_oid_only_last_counted(self):
        # 关键: partial 事件里 dealt_qty 是当时累计, 不是增量
        # oid 相同 → 应只用最后一次 (最终累计)
        events = [
            _partial("O1", "2026-08-01T14:00:00Z", "US.SOXL", "BUY", 30, 30.0),
            _partial("O1", "2026-08-01T14:05:00Z", "US.SOXL", "BUY", 60, 30.0),
            _fill("O1",    "2026-08-01T14:10:00Z", "US.SOXL", "BUY", 100, 30.0),
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            pos = fl.get_position("US.SOXL")
        # 应是 100 (最终), 不是 30+60+100=190 (double count)
        self.assertEqual(pos["qty"], 100)

    def test_zero_dealt_ignored(self):
        events = [
            _fill("O1", "2026-08-01T14:00:00Z", "US.SOXL", "BUY", 0, 0),
            _fill("O2", "2026-08-01T14:00:00Z", "US.SOXL", "BUY", 10, 30.0),
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            pos = fl.get_position("US.SOXL")
        self.assertEqual(pos["qty"], 10)
        self.assertEqual(pos["n_fills"], 1)


class CashFlowTests(unittest.TestCase):

    def test_gross_and_net(self):
        events = [
            _fill("A", "2026-08-01T14:00:00Z", "US.SOXL", "BUY", 100, 30.0),   # -3000
            _fill("B", "2026-08-05T14:00:00Z", "US.SOXL", "SELL", 40, 33.0),   # +1320
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            cf = fl.get_cash_flow()
        self.assertEqual(cf["gross_out"], 3000.0)
        self.assertEqual(cf["gross_in"], 1320.0)
        self.assertEqual(cf["net_flow"], -1680.0)
        self.assertEqual(cf["n_buys"], 1)
        self.assertEqual(cf["n_sells"], 1)


class SummaryByTickerTests(unittest.TestCase):

    def test_summary_covers_all_tickers_in_ledger(self):
        events = [
            _fill("A", "2026-08-01T14:00:00Z", "US.SOXL", "BUY", 100, 30.0),
            _fill("B", "2026-08-05T14:00:00Z", "US.NBIS", "BUY", 5, 200.0),
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            s = fl.summary_by_ticker()
        self.assertIn("US.SOXL", s)
        self.assertIn("US.NBIS", s)
        self.assertEqual(s["US.SOXL"]["qty"], 100)
        self.assertEqual(s["US.NBIS"]["qty"], 5)


if __name__ == "__main__":
    unittest.main()
