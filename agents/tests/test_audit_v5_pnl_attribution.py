"""V5-01 audit (2026-09-23): P&L attribution must not conflate unknown-cost
sells with actual losses, nor confuse ticker-count with round-trip-count.

audit 精确复现场景 (每条对应报告的一行):
- 40天前买 10@100, 昨天卖 10@110, 统计近30天 → 应 +100 (期初继承), 当前 +0/1 loser
- 一 ticker 先 +100 再 -200 → 应 n=2 round-trips (1W/1L), 当前只算 1 条
- 无 buy 只有 sell → 应 unknown/unreconciled 不算 loser, 当前算 1 loser
- 裸 TEST 与 US.TEST 应合并 → 当前重复分组
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


class V5_01_UnmatchedSellsNotLoser(unittest.TestCase):
    """无 buy 只有 sell → unreconciled_sells, 不算 loser."""

    def test_orphan_sell_marked_unreconciled_not_loser(self):
        # 只有 sell (期初持仓被卖) → realized_pnl=0 (无 cost basis)
        # 不能被记成 loser, 应 explicitly unreconciled
        events = [
            _fill("S1", "2026-08-01T14:00Z", "US.ORPHAN", "SELL", 100, 50.0),
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            s = ct.stats_from_fills(since_days=365)
        # 关键 audit assertion: 不能 count 成 loser
        self.assertEqual(s["n_losers"], 0,
                          f"V5-01: 只有 sell 无 buy → 应 unreconciled 不算 loser, actual={s}")
        # 应暴露 unreconciled 信息
        self.assertIn("unreconciled_tickers", s,
                        "V5-01: stats_from_fills 必须暴露 unreconciled_tickers")
        self.assertIn("US.ORPHAN", s["unreconciled_tickers"])

    def test_authority_warning_when_unmatched_exist(self):
        events = [
            _fill("S1", "2026-08-01T14:00Z", "US.ORPHAN", "SELL", 100, 50.0),
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            s = ct.stats_from_fills(since_days=365)
        # audit: 有 unmatched 必须 warn, 不能 authority=fills_replay + warning=None
        self.assertIsNotNone(s["warning"],
                              f"V5-01: unmatched exist → warning 必须非空, actual={s['warning']}")
        self.assertIn("unreconciled", s["warning"].lower() + s.get("authority", "").lower())


class V5_01_RoundTripCount(unittest.TestCase):
    """n 应是 round-trip 次数, 不是 ticker 数. 一个 ticker 两轮独立计."""

    def test_two_roundtrips_on_same_ticker(self):
        events = [
            # Round 1: buy 10@100, sell 10@110 → +100 win
            _fill("A1", "2026-08-01T14:00Z", "US.T1", "BUY", 10, 100),
            _fill("A2", "2026-08-05T14:00Z", "US.T1", "SELL", 10, 110),
            # Round 2: buy 10@50, sell 10@30 → -200 loss
            _fill("A3", "2026-08-10T14:00Z", "US.T1", "BUY", 10, 50),
            _fill("A4", "2026-08-15T14:00Z", "US.T1", "SELL", 10, 30),
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            s = ct.stats_from_fills(since_days=365)
        # audit: 一 ticker 先 +100 再 -200 → 2 round-trips (1W/1L)
        self.assertEqual(s["n_roundtrips"], 2,
                          f"V5-01: 应识别 2 独立 round-trips, actual n_roundtrips={s.get('n_roundtrips')}")
        # win rate 应是 50% (1 win, 1 loss)
        # But total pnl should be -100 (+100 -200)
        self.assertEqual(s["total_pnl_usd"], -100.0)


class V5_01_InterleavedOrders(unittest.TestCase):
    """F1 followup (2026-09-23): 交错成交 (buy partial → 别单 sell → 原 buy 完成)
    per-oid 压缩会破坏时序. stats 与 fill_ledger.get_position 必须一致."""

    def test_interleaved_orders_stats_matches_reader(self):
        # Order 'b': partial 5@100 at t+0s, then filled cum=10@110 at t+120s
        # Order 's': SELL 5@110 at t+60s
        # 正确: 第二批 buy 增量 = (10*110 - 5*100)/5 = 120
        # 卖 5 消耗 layer[0] {5,100} → realized = 5*(110-100) = 50
        # 剩余 layers = [{5, 120}]
        events = [
            {"event": "partial", "order_id": "b", "ts": "2026-09-01T14:00:00Z",
             "ticker": "US.TEST", "side": "BUY",
             "dealt_qty": 5, "average_fill_price": 100.0},
            {"event": "filled", "order_id": "s", "ts": "2026-09-01T14:01:00Z",
             "ticker": "US.TEST", "side": "SELL",
             "dealt_qty": 5, "average_fill_price": 110.0},
            {"event": "filled", "order_id": "b", "ts": "2026-09-01T14:02:00Z",
             "ticker": "US.TEST", "side": "BUY",
             "dealt_qty": 10, "average_fill_price": 110.0},
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            s = ct.stats_from_fills(since_days=365)
            reader = fl.get_position("US.TEST")
        # stats 应给正确 realized + 无 unreconciled
        self.assertEqual(s["total_pnl_usd"], 50.0,
                          f"F1: interleaved realized 应 +50, actual={s['total_pnl_usd']}, s={s}")
        self.assertEqual(s["unreconciled_sells_qty"], 0,
                          f"F1: 交错 sell 不应 unreconciled, actual={s}")
        # position 应剩 5 @ 120
        pos = s["positions"].get("US.TEST", {})
        self.assertEqual(pos.get("qty"), 5,
                          f"F1: 剩余 qty 应 5, actual={pos}")
        self.assertAlmostEqual(pos.get("avg_cost") or 0, 120.0, places=1,
                                msg=f"F1: 剩余 avg_cost 应 120, actual={pos}")
        # stats 与 fill_ledger.get_position 一致
        self.assertEqual(reader["qty"], 5,
                          f"F1: reader 应剩 5, actual={reader}")
        self.assertAlmostEqual(reader["avg_cost"], 120.0, places=1,
                                msg=f"F1: reader avg 应 120, actual={reader}")


class V5_01_WindowInheritsCost(unittest.TestCase):
    """卖出实际时间归属报告窗口, 不能先裁 cost."""

    def test_old_buy_recent_sell_inherits_cost(self):
        # 买: 60d 前, 卖: 5d 前. since=30d 应包含 sell + 继承 buy 成本
        events = [
            _fill("B1", "2026-06-01T14:00Z", "US.T1", "BUY", 10, 100.0),   # 100+ days ago
            _fill("S1", "2026-09-18T14:00Z", "US.T1", "SELL", 10, 110.0),
        ]
        with patch.object(fl, "_load_ledger", return_value=events):
            s = ct.stats_from_fills(since_days=30)
        # audit: since 30d should include the sell + inherit buy cost from earlier
        # Realized should be +100 (10 * (110-100))
        self.assertEqual(s["total_pnl_usd"], 100.0,
                          f"V5-01: 期初 buy 应被继承作 cost basis, actual={s}")
        self.assertEqual(s["n_winners"], 1)
        self.assertEqual(s["n_losers"], 0)


if __name__ == "__main__":
    unittest.main()
