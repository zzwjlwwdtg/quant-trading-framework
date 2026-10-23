"""交割单置顶 + 公开 (2026-10-07 用户要求)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import fill_ledger as fl

FILLS = [
    {"event": "partial", "order_id": "1", "ticker": "US.MSFT", "side": "BUY", "dealt_qty": 5, "average_fill_price": 500.0, "ts": "2026-10-05T14:00:00+00:00"},
    {"event": "filled", "order_id": "1", "ticker": "US.MSFT", "side": "BUY", "dealt_qty": 10, "average_fill_price": 501.0, "ts": "2026-10-05T14:01:00+00:00"},
    {"event": "filled", "order_id": "2", "ticker": "SHY", "side": "SELL_ALL", "dealt_qty": 100, "average_fill_price": 81.0, "ts": "2026-10-06T14:00:00+00:00"},
    {"event": "filled", "order_id": "3", "ticker": "US.X", "side": "BUY", "dealt_qty": 0, "average_fill_price": 0, "ts": "2026-10-06T15:00:00+00:00"},
]


class Statement(unittest.TestCase):
    def test_aggregate_sort_and_fees(self):
        with patch.object(fl, "get_fills", return_value=FILLS), \
             patch.object(fl, "load_order_fees", return_value={"1": 1.25, "2": None}):
            rows = fl.statement_rows(n=10)
        self.assertEqual([r["order_id"] for r in rows], ["2", "1"])        # 最新在前, 空成交剔除
        self.assertEqual(rows[0]["side"], "SELL")
        self.assertEqual(rows[0]["ticker"], "SHY")
        self.assertIsNone(rows[0]["fee"])                                 # 未知 ≠ 0
        self.assertEqual((rows[1]["qty"], rows[1]["price"], rows[1]["amount"], rows[1]["fee"]),
                         (10.0, 501.0, 5010.0, 1.25))                      # 部分成交取累计最后一条
        self.assertFalse(rows[1]["partial"])

    def test_public_and_pinned(self):
        import snapshot_generator as sg
        self.assertIn("/api/fills", sg.GLOBAL_ENDPOINTS)
        self.assertNotIn("/api/fills", sg.PRIVATE_ENDPOINTS)
        html = (AGENTS_DIR / "dashboard.html").read_text(encoding="utf-8")
        i_pos, i_fills, i_eq = (html.index('id="positions"'), html.index('id="fills"'),
                                html.index('id="equity-chart"'))
        self.assertTrue(i_pos < i_fills < i_eq)
        promises = html[html.index("async function loadAll()"):html.index("// Owner-only endpoints")]
        self.assertIn("loadFills()", promises)



class FillTimeSource(unittest.TestCase):
    """2026-10-19: 成交时间不能用对账时刻 (10/16 TQQQ 12:02 买 / 14:58 卖 被显示成 15:45 同一分钟)."""

    def test_broker_time_conversion(self):
        from execution_analytics import broker_time_to_utc_iso as b
        self.assertEqual(b("2026-10-16 12:02:39.120"), "2026-10-16T16:02:39.120000+00:00")   # 夏令时 -4
        self.assertEqual(b("2026-01-15 10:00:00"), "2026-01-15T15:00:00+00:00")              # 冬令时 -5
        self.assertEqual(b(""), "")
        self.assertEqual(b("garbage"), "")


class StatementFillTime(unittest.TestCase):
    def test_row_ts_prefers_broker_then_submitted(self):
        fills = [{"event": "filled", "order_id": "A", "ticker": "US.TQQQ", "side": "BUY", "dealt_qty": 286,
                  "average_fill_price": 70.02, "ts": "2026-10-16T19:45:24+00:00"},
                 {"event": "filled", "order_id": "B", "ticker": "US.TQQQ", "side": "SELL", "dealt_qty": 286,
                  "average_fill_price": 67.915, "ts": "2026-10-16T19:45:24+00:00", "broker_time": "2026-10-16T18:58:30+00:00"},
                 {"event": "filled", "order_id": "C", "ticker": "US.X", "side": "BUY", "dealt_qty": 1,
                  "average_fill_price": 1.0, "ts": "2026-10-16T19:45:25+00:00"}]
        submitted = [{"event": "submitted", "order_id": "A", "ts": "2026-10-16T16:02:38+00:00"},
                     {"event": "submitted", "order_id": "B", "ts": "2026-10-16T18:58:24+00:00"}]
        with patch.object(fl, "get_fills", return_value=fills), patch.object(fl, "load_order_fees", return_value={}), \
             patch.object(fl, "_read_jsonl", return_value=submitted + fills):
            rows = {r["order_id"]: r for r in fl.statement_rows(n=10)}
        self.assertEqual((rows["A"]["ts"], rows["A"]["time_source"]), ("2026-10-16T16:02:38+00:00", "submitted"))
        self.assertEqual((rows["B"]["ts"], rows["B"]["time_source"]), ("2026-10-16T18:58:30+00:00", "broker"))
        self.assertEqual(rows["C"]["time_source"], "recorded")
        self.assertEqual(rows["A"]["recorded_ts"], "2026-10-16T19:45:24+00:00")


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
