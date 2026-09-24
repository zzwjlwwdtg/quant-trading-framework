"""拆股/合股处理 (2026-09-24, 券商历史对账发现).

MULL 2026-06-26 25:1 拆股 (https://www.roic.ai/quote/MULL/stock-splits):
6 月 1 日买 100 股 @839, 卖出 87 股后剩 13 股, 拆股后变 325 股, 8 月 7 日卖 325 @17.76.
不处理拆股时: 13 股按 $839 成本卖 $17.76 (巨亏) + 312 股 "找不到成本".
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import cohort_tracker as ct
import fill_ledger as fl

SPLIT = [{"ticker": "US.MULL", "type": "split", "ratio": 25,
          "effective": "2026-06-26T13:30:00+00:00", "source": "test"}]


def _f(oid, ts, side, qty, px, ticker="US.MULL"):
    return {"event": "filled", "order_id": oid, "ts": ts, "ticker": ticker,
            "side": side, "dealt_qty": qty, "average_fill_price": px}


MULL = [_f("B1", "2026-06-01T13:36:57+00:00", "BUY", 100, 839.0),
        _f("S1", "2026-06-03T16:07:54+00:00", "SELL", 50, 890.0),
        _f("S2", "2026-06-04T12:52:21+00:00", "SELL", 25, 818.1),
        _f("S3", "2026-06-04T16:08:42+00:00", "SELL", 12, 803.553),
        _f("S4", "2099-08-07T14:05:27+00:00", "SELL", 325, 17.76)]


class SplitApplied(unittest.TestCase):
    def test_position_reader(self):
        with patch.object(fl, "_load_ledger", return_value=MULL), \
             patch.object(fl, "_load_corporate_actions", return_value=SPLIT):
            p = fl.get_position("MULL")
        self.assertEqual(p["qty"], 0)
        self.assertEqual(p["unreconciled_sells"], 0)
        # 2550 - 522.5 - 425.364 + 325*(17.76-33.56) = -3532.864
        self.assertAlmostEqual(p["realized_pnl"], -3532.86, places=2)
        self.assertEqual(p["splits_applied"], 1)

    def test_stats_window_attribution(self):
        with patch.object(fl, "_load_ledger", return_value=MULL), \
             patch.object(fl, "_load_corporate_actions", return_value=SPLIT):
            s = ct.stats_from_fills(since_days=30)
        self.assertAlmostEqual(s["total_pnl_usd"], -5135.0, places=2)
        self.assertEqual(s["unreconciled_sells_qty"], 0)
        self.assertEqual(s["authority"], "fills_replay")

    def test_without_split_record_old_behaviour(self):
        with patch.object(fl, "_load_ledger", return_value=MULL), \
             patch.object(fl, "_load_corporate_actions", return_value=[]):
            p = fl.get_position("MULL")
        self.assertEqual(p["unreconciled_sells"], 312)

    def test_split_only_affects_its_ticker(self):
        ev = [_f("B1", "2026-06-01T13:00:00+00:00", "BUY", 10, 100.0, "US.OTHER"),
              _f("S1", "2099-01-01T13:00:00+00:00", "SELL", 10, 110.0, "US.OTHER")]
        with patch.object(fl, "_load_ledger", return_value=ev), \
             patch.object(fl, "_load_corporate_actions", return_value=SPLIT):
            p = fl.get_position("US.OTHER")
        self.assertEqual((p["realized_pnl"], p["unreconciled_sells"]), (100.0, 0))

    def test_reverse_split(self):
        ev = [_f("B1", "2026-04-01T13:00:00+00:00", "BUY", 200, 5.0, "US.RS"),
              _f("S1", "2026-05-01T13:00:00+00:00", "SELL", 10, 110.0, "US.RS")]
        rs = [{"ticker": "US.RS", "type": "split", "ratio": 0.05,
               "effective": "2026-04-24T13:30:00+00:00", "source": "test"}]
        with patch.object(fl, "_load_ledger", return_value=ev), \
             patch.object(fl, "_load_corporate_actions", return_value=rs):
            p = fl.get_position("US.RS")
        # 200 @5 → 10 @100; sell 10 @110 → +100
        self.assertEqual((p["qty"], p["realized_pnl"], p["unreconciled_sells"]), (0, 100.0, 0))

    def test_loader_reads_file(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ca.json"
            path.write_text(json.dumps({"actions": SPLIT}), encoding="utf-8")
            with patch.object(fl, "CORPORATE_ACTIONS_PATH", path):
                acts = fl._load_corporate_actions()
        self.assertEqual(acts[0]["ratio"], 25)


if __name__ == "__main__":
    unittest.main()
