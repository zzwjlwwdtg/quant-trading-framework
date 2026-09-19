"""cohort_tracker tests (2026-09-08 feature).

锁死:
- open (第一 BUY 无 active) / add (第二 BUY 已 active) / partial exit / full close
- realized pnl 计算正确
- stats 聚合正确 (胜率 / avg / 最好最差)
- REBALANCE 不进 cohort (paper_trader 里已排除)
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cohort_tracker


class CohortLifecycleTests(unittest.TestCase):
    """从 on_buy 开始, 单元 flow 覆盖."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ledger = Path(self.tmpdir) / "ledger.jsonl"
        self.active = Path(self.tmpdir) / "active.json"
        self._patches = [
            patch.object(cohort_tracker, "_LEDGER", self.ledger),
            patch.object(cohort_tracker, "_ACTIVE", self.active),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_first_buy_opens_cohort(self):
        c = cohort_tracker.on_buy(
            "US.TQQQ", 72.5, 100,
            signal_ctx={"action": "WATCH_BUY", "confidence": 5,
                        "regime": "neutral_chop", "entry_target": 72.5,
                        "reason": "oversold + uptrend", "tag": "[WATCH_BUY conf=5]"},
            ts="2026-09-08T14:00:00Z",
        )
        self.assertEqual(c["status"], "active")
        self.assertEqual(c["current_qty"], 100)
        self.assertEqual(c["avg_entry_price"], 72.5)
        self.assertEqual(c["cost_basis_usd"], 7250.0)
        # active 里
        active = cohort_tracker.active_cohort("US.TQQQ")
        self.assertEqual(active["cohort_id"], c["cohort_id"])

    def test_second_buy_adds_to_cohort(self):
        cohort_tracker.on_buy("US.TQQQ", 72.5, 100,
                              signal_ctx={"action": "WATCH_BUY", "tag": "L1"},
                              ts="2026-09-08T14:00:00Z")
        c = cohort_tracker.on_buy("US.TQQQ", 74.0, 50,
                                  signal_ctx={"action": "WATCH_BUY", "tag": "PYRAMID L2"},
                                  ts="2026-09-09T14:00:00Z")
        self.assertEqual(c["current_qty"], 150)
        # avg = (72.5*100 + 74.0*50) / 150 = (7250 + 3700) / 150 = 73.0
        self.assertAlmostEqual(c["avg_entry_price"], 73.0, places=4)
        self.assertEqual(len(c["entries"]), 2)

    def test_partial_sell_keeps_cohort_active_with_realized(self):
        cohort_tracker.on_buy("US.TQQQ", 72.5, 100,
                              signal_ctx={"action": "WATCH_BUY", "tag": "L1"},
                              ts="2026-09-08T14:00:00Z")
        # tp15 sells 30%
        r = cohort_tracker.on_sell("US.TQQQ", 83.4, 30,
                                    exit_reason="TAKE-PROFIT tp15",
                                    ts="2026-09-10T14:00:00Z")
        self.assertIsNone(r, "partial exit 返 None")
        active = cohort_tracker.active_cohort("US.TQQQ")
        self.assertEqual(active["current_qty"], 70)
        # realized = (83.4 - 72.5) * 30 = 10.9 * 30 = 327.0
        self.assertAlmostEqual(active["realized_pnl_usd"], 327.0, places=2)
        self.assertEqual(len(active["exits"]), 1)

    def test_full_sell_closes_cohort_and_ledger_records(self):
        cohort_tracker.on_buy("US.TQQQ", 72.5, 100,
                              signal_ctx={"action": "WATCH_BUY", "tag": "L1"},
                              ts="2026-09-08T14:00:00Z")
        c = cohort_tracker.on_sell("US.TQQQ", 76.8, 100,
                                    exit_reason="TRAILING-STOP",
                                    ts="2026-09-15T14:00:00Z")
        # close 返 closed cohort
        self.assertIsNotNone(c)
        self.assertEqual(c["status"], "closed")
        self.assertTrue(c["is_winner"])
        # realized = (76.8 - 72.5) * 100 = 430
        self.assertAlmostEqual(c["realized_pnl_usd"], 430.0, places=2)
        # pnl_pct = 430 / (72.5*100) * 100 = 5.931
        self.assertAlmostEqual(c["realized_pnl_pct"], 5.931, places=2)
        # hold_days ≈ 7
        self.assertAlmostEqual(c["hold_days"], 7.0, places=1)
        # active 已移除
        self.assertIsNone(cohort_tracker.active_cohort("US.TQQQ"))
        # ledger 有 close 事件
        entries = [json.loads(l) for l in self.ledger.read_text(encoding="utf-8").splitlines()]
        events = [e["event"] for e in entries]
        self.assertIn("open", events)
        self.assertIn("close", events)

    def test_full_sell_after_pyramid_and_partial(self):
        """真实 flow: L1 BUY → L2 BUY → tp15 partial → trailing-stop 剩余."""
        cohort_tracker.on_buy("US.TQQQ", 70.0, 100,
                              signal_ctx={"action": "WATCH_BUY", "tag": "L1"},
                              ts="2026-09-01T14:00:00Z")
        cohort_tracker.on_buy("US.TQQQ", 72.0, 50,
                              signal_ctx={"action": "WATCH_BUY", "tag": "PYRAMID L2"},
                              ts="2026-09-03T14:00:00Z")
        # avg entry = (70*100+72*50)/150 = (7000+3600)/150 = 70.67
        cohort_tracker.on_sell("US.TQQQ", 81.3, 45,
                                exit_reason="TAKE-PROFIT tp15",
                                ts="2026-09-05T14:00:00Z")
        # partial realized = (81.3 - 70.67) * 45 = 478.5 (approximately)
        c = cohort_tracker.on_sell("US.TQQQ", 76.0, 105,
                                    exit_reason="TRAILING-STOP",
                                    ts="2026-09-10T14:00:00Z")
        self.assertEqual(c["status"], "closed")
        self.assertTrue(c["is_winner"])

    def test_sell_without_active_cohort_ignored(self):
        # 没 BUY 过, 直接 SELL 应无 error 且 return None
        r = cohort_tracker.on_sell("US.NEVER_BOUGHT", 100.0, 10,
                                    exit_reason="rebalance", ts="2026-09-08T14:00:00Z")
        self.assertIsNone(r)


class CohortStatsTests(unittest.TestCase):
    """close 多个 cohorts, 验证 stats 聚合."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ledger = Path(self.tmpdir) / "ledger.jsonl"
        self.active = Path(self.tmpdir) / "active.json"
        self._patches = [
            patch.object(cohort_tracker, "_LEDGER", self.ledger),
            patch.object(cohort_tracker, "_ACTIVE", self.active),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_stats_aggregates_wins_and_losses(self):
        # 3 cohorts: 2 win + 1 loss
        # cohort 1: TQQQ 100@70 → sell 100@77 = +10%
        cohort_tracker.on_buy("US.TQQQ", 70.0, 100,
                              signal_ctx={"action": "WATCH_BUY", "tag": "L1"},
                              ts="2026-09-01T14:00:00Z")
        cohort_tracker.on_sell("US.TQQQ", 77.0, 100, exit_reason="TP",
                                ts="2026-09-05T14:00:00Z")
        # cohort 2: MSFT 10@500 → sell 10@520 = +4%
        cohort_tracker.on_buy("US.MSFT", 500.0, 10,
                              signal_ctx={"action": "WATCH_BUY", "tag": "L1"},
                              ts="2026-09-02T14:00:00Z")
        cohort_tracker.on_sell("US.MSFT", 520.0, 10, exit_reason="TP",
                                ts="2026-09-06T14:00:00Z")
        # cohort 3: SOXL 10@100 → sell 10@90 = -10%
        cohort_tracker.on_buy("US.SOXL", 100.0, 10,
                              signal_ctx={"action": "WATCH_BUY", "tag": "L1"},
                              ts="2026-09-03T14:00:00Z")
        cohort_tracker.on_sell("US.SOXL", 90.0, 10, exit_reason="STOP",
                                ts="2026-09-07T14:00:00Z")

        s = cohort_tracker.stats(since_days=365)
        self.assertEqual(s["n"], 3)
        self.assertEqual(s["n_winners"], 2)
        self.assertEqual(s["n_losers"], 1)
        self.assertAlmostEqual(s["win_rate"], 66.7, places=1)
        # total_pnl = 700 + 200 - 100 = 800
        self.assertAlmostEqual(s["total_pnl_usd"], 800.0, places=2)
        self.assertAlmostEqual(s["best_pnl_pct"], 10.0, places=1)
        self.assertAlmostEqual(s["worst_pnl_pct"], -10.0, places=1)

    def test_stats_empty_no_error(self):
        s = cohort_tracker.stats()
        self.assertEqual(s["n"], 0)
        # format also handles empty
        text = cohort_tracker.format_stats()
        self.assertIn("无 closed cohort", text)


if __name__ == "__main__":
    unittest.main()
