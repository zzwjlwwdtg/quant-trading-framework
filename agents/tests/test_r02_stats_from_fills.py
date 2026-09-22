"""R02 audit followup: cohort_tracker.stats_from_fills() event-replay.

Audit: fills 是唯一事实, cohort 是可重建结果. 崩溃/state 丢, stats_from_fills
应能从 execution_ledger 独立重建, 不依赖 cohort ledger.
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


def _fill(oid, ts, ticker, side, dealt, avg):
    return {
        "event": "filled", "order_id": oid, "ts": ts,
        "ticker": ticker, "side": side,
        "dealt_qty": dealt, "average_fill_price": avg,
    }


class StatsFromFillsTests(unittest.TestCase):

    def test_derives_realized_pnl_from_fills(self):
        # Buy 100@50, Sell 100@60 → realized +1000, closed position
        events = [
            _fill("A", "2026-08-01T14:00Z", "US.T1", "BUY", 100, 50.0),
            _fill("B", "2026-08-05T14:00Z", "US.T1", "SELL", 100, 60.0),
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            s = ct.stats_from_fills(since_days=365)
        self.assertEqual(s["authority"], "fills_replay")
        self.assertEqual(s["n"], 1)   # 1 closed
        self.assertEqual(s["n_winners"], 1)
        self.assertEqual(s["total_pnl_usd"], 1000.0)

    def test_active_position_not_counted_as_closed(self):
        # 只买不卖 → active, 不算 closed cohort
        events = [
            _fill("A", "2026-08-01T14:00Z", "US.T1", "BUY", 100, 50.0),
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            s = ct.stats_from_fills(since_days=365)
        self.assertEqual(s["n"], 0)   # 0 closed
        self.assertEqual(s["n_events"], 1)
        # position 仍 tracked
        self.assertIn("US.T1", s["positions"])
        self.assertEqual(s["positions"]["US.T1"]["qty"], 100)

    def test_win_loss_split(self):
        events = [
            # T1: +100 win
            _fill("A1", "2026-08-01T14:00Z", "US.T1", "BUY", 10, 100),
            _fill("A2", "2026-08-05T14:00Z", "US.T1", "SELL", 10, 110),
            # T2: -50 loss
            _fill("B1", "2026-08-02T14:00Z", "US.T2", "BUY", 10, 50),
            _fill("B2", "2026-08-06T14:00Z", "US.T2", "SELL", 10, 45),
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            s = ct.stats_from_fills(since_days=365)
        self.assertEqual(s["n"], 2)
        self.assertEqual(s["n_winners"], 1)
        self.assertEqual(s["n_losers"], 1)
        self.assertEqual(s["win_rate"], 50.0)
        self.assertEqual(s["total_pnl_usd"], 50.0)   # +100 - 50

    def test_empty_ledger_returns_zero_authority_still_fills(self):
        with patch.object(fl, "_load_ledger", return_value=[]):
            s = ct.stats_from_fills(since_days=30)
        self.assertEqual(s["n"], 0)
        self.assertEqual(s["authority"], "fills_replay")
        self.assertEqual(s["n_events"], 0)

    def test_fills_replay_survives_state_loss(self):
        # 关键 R02: 即使 cohort ledger 完全为空 (state 丢), fills replay 仍工作
        events = [
            _fill("A", "2026-08-01T14:00Z", "US.T1", "BUY", 100, 50.0),
            _fill("B", "2026-08-05T14:00Z", "US.T1", "SELL", 100, 60.0),
        ]
        with patch.object(fl, "_load_ledger", return_value=events), \
             patch.object(ct, "_load_closed_cohorts", return_value=[]):
            # cohort_ledger 空 → stats() 返 n=0
            s_ledger = ct.stats(since_days=365)
            self.assertEqual(s_ledger["n"], 0)
            # 但 fills replay 仍能算出 realized $1000
            s_fills = ct.stats_from_fills(since_days=365)
            self.assertEqual(s_fills["n"], 1)
            self.assertEqual(s_fills["total_pnl_usd"], 1000.0)


if __name__ == "__main__":
    unittest.main()
