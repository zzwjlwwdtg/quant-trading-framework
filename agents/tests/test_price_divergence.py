"""EVO-20261013-2 (2026-10-13): 决策/日志/成交三端一致性校验测试.

覆盖:
  · decision_context 的 compute_price_divergence / DecisionContext.price_snapshot /
    is_price_diverged 行为
  · decision_agent.build_price_snapshot 输出 schema 稳定, 阈值标记正确
  · fill_ledger.get_order_price_trio 能从 execution_ledger submit + fill 事件
    还原三端价差
  · _trade_postmortem.pair_trades 为每笔配对交易展开 ref/live/trigger 字段
  · data_quality.price_divergence_rate 对 trade_log 的 flagged 比例统计准确

不涉及真实下单, 不触发 moomoo SDK.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


class ComputeDivergenceTests(unittest.TestCase):
    def test_basic_positive(self):
        from decision_context import compute_price_divergence
        self.assertAlmostEqual(compute_price_divergence(100.0, 100.6), 0.006, places=6)

    def test_basic_negative(self):
        from decision_context import compute_price_divergence
        self.assertAlmostEqual(compute_price_divergence(50.0, 49.5), -0.01, places=6)

    def test_missing_or_zero_returns_none(self):
        from decision_context import compute_price_divergence
        self.assertIsNone(compute_price_divergence(None, 10))
        self.assertIsNone(compute_price_divergence(10, None))
        self.assertIsNone(compute_price_divergence(0, 10))
        self.assertIsNone(compute_price_divergence(10, 0))
        self.assertIsNone(compute_price_divergence("abc", 10))

    def test_threshold_default_is_0p6pct(self):
        from decision_context import PRICE_DIVERGENCE_THRESHOLD, is_price_diverged
        self.assertAlmostEqual(PRICE_DIVERGENCE_THRESHOLD, 0.006, places=6)
        self.assertFalse(is_price_diverged(100.0, 100.5))      # 0.5% < 0.6%
        self.assertTrue(is_price_diverged(100.0, 100.7))       # 0.7% >= 0.6%
        self.assertTrue(is_price_diverged(100.0, 99.3))        # -0.7%
        self.assertFalse(is_price_diverged(None, 100.0))

    def test_decision_context_price_snapshot(self):
        from decision_context import DecisionContext
        ctx = DecisionContext(
            as_of=datetime.now(timezone.utc),
            market={"ticker": "SOXL", "price": 20.00},
            live_quote=20.30,              # +1.5% → flagged
            quote_source="yfinance_1m_prepost",
            staleness_sec=45.0,
            is_backtest=True,
        )
        snap = ctx.price_snapshot()
        self.assertEqual(snap["ref_price"], 20.0)
        self.assertEqual(snap["live_quote"], 20.30)
        self.assertEqual(snap["quote_source"], "yfinance_1m_prepost")
        self.assertEqual(snap["staleness_sec"], 45.0)
        self.assertAlmostEqual(snap["divergence_pct"], 0.015, places=6)
        self.assertEqual(snap["divergence_flag"], "PRICE_DIVERGENCE")
        self.assertTrue(ctx.is_price_diverged())

    def test_decision_context_no_live_quote(self):
        from decision_context import DecisionContext
        ctx = DecisionContext(
            as_of=datetime.now(timezone.utc),
            market={"ticker": "SOXL", "price": 20.00},
            is_backtest=True,
        )
        snap = ctx.price_snapshot()
        self.assertEqual(snap["ref_price"], 20.0)
        self.assertIsNone(snap["live_quote"])
        self.assertIsNone(snap["divergence_pct"])
        self.assertIsNone(snap["divergence_flag"])
        self.assertFalse(ctx.is_price_diverged())

    def test_with_updates_preserves_live_fields(self):
        from decision_context import DecisionContext
        ctx = DecisionContext(
            as_of=datetime.now(timezone.utc),
            market={"ticker": "SOXL", "price": 20.00},
            live_quote=20.30, quote_source="yf", staleness_sec=10.0,
            is_backtest=True,
        )
        ctx2 = ctx.with_updates(board_regime="bull_trending")
        self.assertEqual(ctx2.live_quote, 20.30)
        self.assertEqual(ctx2.quote_source, "yf")
        self.assertEqual(ctx2.staleness_sec, 10.0)


class BuildPriceSnapshotTests(unittest.TestCase):
    def test_schema_keys_fixed(self):
        from decision_agent import build_price_snapshot
        snap = build_price_snapshot(100.0, 100.3, "yfinance_1m_prepost", 15.5)
        for k in ("ref_price", "live_quote", "quote_source", "staleness_sec",
                   "divergence_pct", "divergence_flag", "threshold"):
            self.assertIn(k, snap)

    def test_flagged_above_threshold(self):
        from decision_agent import build_price_snapshot
        snap = build_price_snapshot(100.0, 101.0, "yf", 10)
        self.assertEqual(snap["divergence_flag"], "PRICE_DIVERGENCE")
        self.assertAlmostEqual(snap["divergence_pct"], 0.01, places=6)

    def test_not_flagged_below_threshold(self):
        from decision_agent import build_price_snapshot
        snap = build_price_snapshot(100.0, 100.3, "yf", 10)
        self.assertIsNone(snap["divergence_flag"])
        self.assertAlmostEqual(snap["divergence_pct"], 0.003, places=6)

    def test_none_inputs_graceful(self):
        from decision_agent import build_price_snapshot
        snap = build_price_snapshot(None, None, None, None)
        self.assertIsNone(snap["ref_price"])
        self.assertIsNone(snap["live_quote"])
        self.assertIsNone(snap["divergence_pct"])
        self.assertIsNone(snap["divergence_flag"])

    def test_rounds_to_sensible_precision(self):
        from decision_agent import build_price_snapshot
        snap = build_price_snapshot(100.12345678, 100.98765432, "yf", 7.1234567)
        # ref/live round to 4 位, staleness 2 位
        self.assertEqual(snap["ref_price"], 100.1235)
        self.assertEqual(snap["live_quote"], 100.9877)
        self.assertEqual(snap["staleness_sec"], 7.12)


class FillLedgerTrioTests(unittest.TestCase):
    def setUp(self):
        import fill_ledger
        self.fl = fill_ledger
        self.tmp = Path(tempfile.mkdtemp())
        self.ledger = self.tmp / "execution_ledger.jsonl"

    def _write(self, rows):
        with open(self.ledger, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def test_trio_from_submit_and_fill(self):
        self._write([
            {"event": "submitted", "order_id": "o1", "ticker": "US.SOXL",
             "side": "BUY", "order_price": 20.11, "trigger_price": 20.11,
             "reference_price": 20.00, "ref_price": 20.00,
             "live_quote": 20.30, "quote_source": "yfinance_1m_prepost",
             "staleness_sec": 20.0, "divergence_pct": 0.015,
             "divergence_flag": "PRICE_DIVERGENCE", "ts": "2026-10-13T13:30:00+00:00"},
            {"event": "partial", "order_id": "o1", "ticker": "US.SOXL",
             "side": "BUY", "dealt_qty": 50, "average_fill_price": 20.25,
             "ts": "2026-10-13T13:30:30+00:00"},
            {"event": "filled", "order_id": "o1", "ticker": "US.SOXL",
             "side": "BUY", "dealt_qty": 100, "average_fill_price": 20.27,
             "ts": "2026-10-13T13:30:45+00:00"},
        ])
        trio = self.fl.get_order_price_trio("o1", path=self.ledger)
        self.assertEqual(trio["trigger_price"], 20.11)
        self.assertEqual(trio["ref_price"], 20.00)
        self.assertEqual(trio["live_quote"], 20.30)
        self.assertEqual(trio["fill_price"], 20.27)   # 最后一次 avg
        self.assertEqual(trio["divergence_flag"], "PRICE_DIVERGENCE")
        self.assertEqual(trio["quote_source"], "yfinance_1m_prepost")

    def test_trio_missing_submit_legacy(self):
        # 老订单无 submit 的 price_snapshot 字段 → 全 None, 不报错
        self._write([
            {"event": "filled", "order_id": "legacy", "ticker": "US.NBIS",
             "side": "SELL", "dealt_qty": 10, "average_fill_price": 55.0,
             "ts": "2026-09-01T00:00:00+00:00"},
        ])
        trio = self.fl.get_order_price_trio("legacy", path=self.ledger)
        self.assertIsNone(trio["ref_price"])
        self.assertIsNone(trio["live_quote"])
        self.assertIsNone(trio["divergence_flag"])
        self.assertEqual(trio["fill_price"], 55.0)

    def test_trio_unknown_order(self):
        self._write([
            {"event": "submitted", "order_id": "o1", "ticker": "US.SOXL",
             "side": "BUY", "ts": "2026-10-13T13:30:00+00:00"},
        ])
        trio = self.fl.get_order_price_trio("does-not-exist", path=self.ledger)
        self.assertIsNone(trio["trigger_price"])
        self.assertIsNone(trio["fill_price"])


class TradePostmortemPairTests(unittest.TestCase):
    def test_pair_trades_carries_price_snapshot(self):
        # 直接 import 底下的模块, 不依赖 trade_log 文件
        import importlib
        tp = importlib.import_module("_trade_postmortem")
        events = [
            {"ts": "2026-10-01T13:30:00+00:00", "ticker": "US.SOXL",
             "side": "BUY", "qty": 10, "price": 20.11, "tag": "BUY",
             "ref_price": 20.00, "live_quote": 20.30,
             "trigger_price": 20.11, "divergence_pct": 0.015,
             "divergence_flag": "PRICE_DIVERGENCE", "quote_source": "yf",
             "staleness_sec": 10, "decision": {"reason": "r"}, "market": {}},
            {"ts": "2026-10-03T13:30:00+00:00", "ticker": "US.SOXL",
             "side": "SELL", "qty": 10, "price": 18.50, "tag": "[STOP]",
             "ref_price": 18.70, "live_quote": 18.45,
             "trigger_price": 18.50, "divergence_pct": -0.0134,
             "divergence_flag": "PRICE_DIVERGENCE", "quote_source": "yf",
             "staleness_sec": 5, "decision": {}, "market": {}},
        ]
        trades = tp.pair_trades(events)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["entry_ref_price"], 20.00)
        self.assertEqual(t["entry_live_quote"], 20.30)
        self.assertEqual(t["entry_divergence_flag"], "PRICE_DIVERGENCE")
        self.assertEqual(t["exit_divergence_flag"], "PRICE_DIVERGENCE")
        self.assertAlmostEqual(t["pnl_pct"], (18.50 - 20.11) / 20.11 * 100, places=2)

    def test_pair_trades_legacy_entries_still_pair(self):
        import importlib
        tp = importlib.import_module("_trade_postmortem")
        # 老格式 trade_log 没有三端字段
        events = [
            {"ts": "2026-10-01T13:30:00+00:00", "ticker": "US.XYZ",
             "side": "BUY", "qty": 10, "price": 100.0, "tag": "BUY",
             "decision": {"reason": "r"}, "market": {}},
            {"ts": "2026-10-02T13:30:00+00:00", "ticker": "US.XYZ",
             "side": "SELL", "qty": 10, "price": 110.0, "tag": "[TP]",
             "decision": {}, "market": {}},
        ]
        trades = tp.pair_trades(events)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertIsNone(t["entry_ref_price"])
        self.assertIsNone(t["entry_divergence_flag"])
        # entry_trigger_price fallback 到 price
        self.assertEqual(t["entry_trigger_price"], 100.0)


class DataQualityPriceDivergenceRateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.log = self.tmp / "trade_log.jsonl"

    def _write_entries(self, rows):
        with open(self.log, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def test_rate_mixes_flagged_and_clean(self):
        from data_quality import price_divergence_rate
        self._write_entries([
            {"ts": "2026-10-11T13:30:00+00:00", "ticker": "US.SOXL",
             "side": "BUY", "ref_price": 20.0, "live_quote": 20.30,
             "divergence_pct": 0.015, "divergence_flag": "PRICE_DIVERGENCE"},
            {"ts": "2026-10-12T13:30:00+00:00", "ticker": "US.TQQQ",
             "side": "BUY", "ref_price": 70.0, "live_quote": 70.10,
             "divergence_pct": 0.00143, "divergence_flag": None},
            {"ts": "2026-10-12T15:00:00+00:00", "ticker": "US.SOXL",
             "side": "SELL", "ref_price": 19.5, "live_quote": 19.80,
             "divergence_pct": 0.01538, "divergence_flag": "PRICE_DIVERGENCE"},
            # legacy 条目, 无 divergence_pct
            {"ts": "2026-09-01T00:00:00+00:00", "ticker": "US.NBIS",
             "side": "BUY", "price": 55.0},
        ])
        r = price_divergence_rate(self.log, threshold=0.006)
        self.assertEqual(r["total"], 4)
        self.assertEqual(r["with_snapshot"], 3)
        self.assertEqual(r["flagged"], 2)
        self.assertAlmostEqual(r["rate"], 2 / 3, places=4)
        self.assertAlmostEqual(r["snapshot_coverage"], 3 / 4, places=4)
        # top_samples 按绝对值降序
        self.assertEqual(r["top_samples"][0]["ticker"], "US.SOXL")

    def test_rate_skips_dry_run_by_default(self):
        from data_quality import price_divergence_rate
        self._write_entries([
            {"ts": "2026-10-11T13:30:00+00:00", "ticker": "US.SOXL",
             "side": "BUY", "divergence_pct": 0.02,
             "divergence_flag": "PRICE_DIVERGENCE", "dry_run": True},
            {"ts": "2026-10-12T13:30:00+00:00", "ticker": "US.SOXL",
             "side": "BUY", "divergence_pct": 0.001,
             "divergence_flag": None, "dry_run": False},
        ])
        r = price_divergence_rate(self.log, threshold=0.006)
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["flagged"], 0)
        r2 = price_divergence_rate(self.log, threshold=0.006, include_dry_run=True)
        self.assertEqual(r2["total"], 2)
        self.assertEqual(r2["flagged"], 1)

    def test_rate_filters_by_since(self):
        from data_quality import price_divergence_rate
        self._write_entries([
            {"ts": "2026-09-15T00:00:00+00:00", "ticker": "US.SOXL",
             "side": "BUY", "divergence_pct": 0.02,
             "divergence_flag": "PRICE_DIVERGENCE"},
            {"ts": "2026-10-10T00:00:00+00:00", "ticker": "US.SOXL",
             "side": "BUY", "divergence_pct": 0.001,
             "divergence_flag": None},
        ])
        r = price_divergence_rate(self.log, since="2026-10-01T00:00:00+00:00")
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["flagged"], 0)

    def test_rate_empty_log(self):
        from data_quality import price_divergence_rate
        r = price_divergence_rate(self.log)   # file 不存在
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["flagged"], 0)
        self.assertIsNone(r["rate"])


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
