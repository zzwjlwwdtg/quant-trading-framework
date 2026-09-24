"""Audit v5 followup 遗留项 (2026-09-24): 成交 reducer 的标的身份与回报修订.

1. 标的身份: 裸 TEST 买入 + US.TEST 卖出属于同一 instrument, 必须合并后回放
   (instrument_registry.normalize 规则: 无市场前缀 → US.; 已有 HK./JP. 前缀保留,
   不做"删除所有前缀"式的合并).
2. 同数量价格修订: 同一订单累计 10@100 → 券商修订为 10@101 → 修订价回溯
   覆盖该成交. 之后卖 10@110 → +90 (旧实现 +100).
3. 累计数量倒退 (撤销/bust): 不支持自动对账 → 计数并降级 authority, 不静默.
4. 持仓 reader 与 stats 共用 reducer: 两者对同一事件流结果一致.
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

RECENT = "2099-01-01T14:00:00+00:00"   # always inside a since_days window


def _f(oid, ts, ticker, side, dealt, avg, kind="filled"):
    return {"event": kind, "order_id": oid, "ts": ts, "ticker": ticker,
            "side": side, "dealt_qty": dealt, "average_fill_price": avg}


def _ts(minute):
    return f"2099-01-01T14:{minute:02d}:00+00:00"


class TickerIdentity(unittest.TestCase):
    EVENTS = [_f("B", _ts(0), "TEST", "BUY", 5, 100.0),
              _f("S", _ts(1), "US.TEST", "SELL", 5, 110.0)]

    def test_stats_merges_bare_and_prefixed(self):
        with patch.object(fl, "_load_ledger", return_value=self.EVENTS):
            s = ct.stats_from_fills(since_days=30)
        self.assertEqual(s["total_pnl_usd"], 50.0)
        self.assertEqual((s["n_roundtrips"], s["n_winners"], s["n_losers"]), (1, 1, 0))
        self.assertEqual(s["unreconciled_tickers"], [])
        self.assertEqual(s["authority"], "fills_replay")
        self.assertEqual(s["n_tickers"], 1)
        self.assertEqual(s["positions"], {})

    def test_summary_by_ticker_single_canonical_key(self):
        with patch.object(fl, "_load_ledger", return_value=self.EVENTS):
            summ = fl.summary_by_ticker()
        self.assertEqual(list(summ), ["US.TEST"])
        self.assertEqual(summ["US.TEST"]["realized_pnl"], 50.0)

    def test_other_market_prefix_not_merged(self):
        events = [_f("B", _ts(0), "HK.TEST", "BUY", 5, 100.0),
                  _f("S", _ts(1), "US.TEST", "SELL", 5, 110.0)]
        with patch.object(fl, "_load_ledger", return_value=events):
            s = ct.stats_from_fills(since_days=30)
        self.assertEqual(s["unreconciled_tickers"], ["US.TEST"])
        self.assertEqual(s["total_pnl_usd"], 0.0)


class SameQtyPriceCorrection(unittest.TestCase):
    EVENTS = [_f("B", _ts(0), "US.TEST", "BUY", 10, 100.0),
              _f("B", _ts(1), "US.TEST", "BUY", 10, 101.0),   # broker revision
              _f("S", _ts(2), "US.TEST", "SELL", 10, 110.0)]

    def test_position_reader_applies_revision(self):
        with patch.object(fl, "_load_ledger", return_value=self.EVENTS):
            p = fl.get_position("US.TEST")
        self.assertEqual(p["realized_pnl"], 90.0)
        self.assertEqual(p["price_revisions"], 1)

    def test_stats_applies_revision(self):
        with patch.object(fl, "_load_ledger", return_value=self.EVENTS):
            s = ct.stats_from_fills(since_days=30)
        self.assertEqual(s["total_pnl_usd"], 90.0)
        self.assertEqual(s["price_revisions"], 1)
        self.assertEqual(s["authority"], "fills_replay")

    def test_revision_of_earlier_partial_level(self):
        # 5@100 → revised 5@102 → cum 10@106 (second tranche 5@110) → sell 10@110
        events = [_f("B", _ts(0), "US.TEST", "BUY", 5, 100.0, "partial"),
                  _f("B", _ts(1), "US.TEST", "BUY", 5, 102.0, "partial"),
                  _f("B", _ts(2), "US.TEST", "BUY", 10, 106.0),
                  _f("S", _ts(3), "US.TEST", "SELL", 10, 110.0)]
        with patch.object(fl, "_load_ledger", return_value=events):
            p = fl.get_position("US.TEST")
            s = ct.stats_from_fills(since_days=30)
        # cost 5*102 + 5*110 = 1060; proceeds 1100 → +40
        self.assertEqual(p["realized_pnl"], 40.0)
        self.assertEqual(s["total_pnl_usd"], 40.0)


class QtyReversalNotSilent(unittest.TestCase):
    def test_cum_qty_decrease_degrades_authority(self):
        events = [_f("B", _ts(0), "US.TEST", "BUY", 10, 100.0),
                  _f("B", _ts(1), "US.TEST", "BUY", 8, 100.0),    # bust 2 shares
                  _f("S", _ts(2), "US.TEST", "SELL", 8, 110.0)]
        with patch.object(fl, "_load_ledger", return_value=events):
            s = ct.stats_from_fills(since_days=30)
            p = fl.get_position("US.TEST")
        self.assertEqual(s["qty_reversals"], 1)
        self.assertEqual(p["qty_reversals"], 1)
        self.assertEqual(s["authority"], "fills_replay_partial")
        self.assertIn("reversal", (s["warning"] or "").lower())


class ReaderStatsAgree(unittest.TestCase):
    def test_interleaved_orders_same_answer(self):
        events = [_f("B", _ts(0), "US.TEST", "BUY", 5, 100.0, "partial"),
                  _f("S", _ts(1), "TEST", "SELL", 5, 110.0),
                  _f("B", _ts(2), "US.TEST", "BUY", 10, 110.0)]
        with patch.object(fl, "_load_ledger", return_value=events):
            p = fl.get_position("TEST")
            s = ct.stats_from_fills(since_days=30)
        self.assertEqual((p["qty"], p["avg_cost"], p["realized_pnl"]), (5, 120.0, 50.0))
        self.assertEqual(s["total_pnl_usd"], 50.0)
        self.assertEqual(s["positions"], {"US.TEST": {"qty": 5.0, "avg_cost": 120.0}})


if __name__ == "__main__":
    unittest.main()


class FormatStatsWording(unittest.TestCase):
    """V5-01 #5/#6: 不把本地重建称为 broker 权威; 旧 cohort 明确标非权威."""

    def _text(self, fills, ledger, active=()):
        with patch.object(ct, "stats_from_fills", return_value=fills), \
             patch.object(ct, "stats", return_value=ledger), \
             patch.object(ct, "all_active_cohorts", return_value=list(active)):
            return ct.format_stats(30)

    FILLS = {"n": 1, "n_roundtrips": 1, "n_winners": 1, "n_losers": 0,
             "win_rate": 100.0, "total_pnl_usd": 500.0, "n_events": 2,
             "n_tickers": 1, "authority": "fills_replay", "warning": None,
             "unreconciled_tickers": [], "unreconciled_sells_qty": 0,
             "price_revisions": 2, "qty_reversals": 0, "positions": {}}

    def test_no_broker_authority_claim_and_estimate_label(self):
        text = self._text(self.FILLS, {"n": 1, "total_pnl_usd": -100.0})
        self.assertNotIn("broker 权威", text)
        self.assertIn("未做账户级对账", text)
        self.assertIn("双源分歧", text)          # diagnostic line kept
        self.assertIn("口径不同", text)

    def test_revision_count_shown(self):
        text = self._text(self.FILLS, {"n": 0, "total_pnl_usd": 0.0})
        self.assertIn("价格修订 2", text)

    def test_active_cohorts_marked_non_authoritative(self):
        active = [{"ticker": "US.X", "current_qty": 1, "avg_entry_price": 1.0,
                   "realized_pnl_usd": 0.0, "open_ts": "2026-09-01T00:00:00"}]
        text = self._text(self.FILLS, {"n": 0, "total_pnl_usd": 0.0}, active)
        self.assertIn("旧 cohort 投影, 非权威", text)
