"""EVO-20261104-2：离线合成材料检验，不构成真实30笔验收。"""
from __future__ import annotations

import copy
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from execution_analytics import (
    actual_execution_quality, estimate_execution, sell_cost_breakdown,
    execution_evidence_metrics, execution_evidence_coverage,
)
from trade_postmortem import replay_execution_evidence_metrics


TS = "2026-11-02T15:00:00+00:00"
TS2 = "2026-11-02T15:01:00+00:00"
BASIS = "成交日原始股数与价格"


def fixture(side="SELL"):
    evidence = {
        "order_id": "1", "ticker": "US.QQQ", "side": side,
        "qty": 10, "price": 99, "basis": BASIS,
        "first_fill_ts": TS, "last_fill_ts": TS,
        "anomalies": [],
        "fills": [{
            "fill_id": "f1", "qty": 10, "price": 99, "ts": TS,
            "basis": BASIS, "qty_semantics": "incremental",
        }],
        "hindsight_verdict": "合理",
        "analysis": {"hindsight": {"verdict": "不合理"}},
    }
    # 日期固定为此前20个工作日；这里只是合成材料，不声明真实交易日历。
    days, day = [], date(2026, 11, 2)
    while len(days) < 20:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            days.append({
                "session_date": day.isoformat(), "volume": 1000, "basis": BASIS,
            })
    market = {
        "fill_quotes": {"f1": {
            "bid": 99, "ask": 101, "ts": TS, "available_ts": TS,
            "source": "离线实测测试夹具", "kind": "observed", "basis": BASIS,
        }},
        "daily_volume": {
            "volume": 100, "basis": BASIS, "source": "离线日量测试夹具",
            "kind": "observed_full_day", "session_date": "2026-11-02",
            "session_start_ts": "2026-11-02T14:30:00+00:00",
            "session_end_ts": "2026-11-02T21:00:00+00:00",
            "available_ts": "2026-11-02T21:01:00+00:00",
            "previous_20_sessions_verified": True,
            "previous_20_sessions": days,
        },
    }
    return evidence, market


class ExecutionEvidenceMetricsTests(unittest.TestCase):
    def test_direction_math_and_independent_completeness(self):
        evidence, market = fixture()
        result = execution_evidence_metrics(evidence, market)
        self.assertEqual(result["data_completeness"], "complete")
        self.assertAlmostEqual(result["volume_pct_of_20d_avg"], 10)
        self.assertAlmostEqual(result["spread_pct_at_fill"], 2)
        self.assertAlmostEqual(result["slippage_vs_mid_bps"], 100)
        evidence["side"] = "BUY"
        self.assertAlmostEqual(
            execution_evidence_metrics(evidence, market)["slippage_vs_mid_bps"], -100)
        # 极端偏离和缩量不降低证据完整性，也不生成优劣结论。
        evidence["price"] = evidence["fills"][0]["price"] = 1
        market["daily_volume"]["volume"] = 0
        result = execution_evidence_metrics(evidence, market)
        self.assertEqual(result["data_completeness"], "complete")
        self.assertEqual(result["volume_pct_of_20d_avg"], 0)
        self.assertNotIn("tier", result)
        self.assertNotIn("hindsight_verdict", result)

    def test_zero_locked_quote_and_invalid_prices(self):
        for bid, ask, expected in [(100, 100, 0), (0, 1, None),
                                   (101, 99, None), (None, 101, None),
                                   (float("nan"), 101, None)]:
            with self.subTest(bid=bid, ask=ask):
                evidence, market = fixture()
                market["fill_quotes"]["f1"].update(bid=bid, ask=ask)
                result = execution_evidence_metrics(evidence, market)
                self.assertEqual(result["spread_pct_at_fill"], expected)
        for field in ("qty", "price"):
            for value in (0, None, float("inf")):
                evidence, market = fixture()
                evidence[field] = value
                self.assertIsNone(
                    execution_evidence_metrics(evidence, market)["slippage_vs_mid_bps"])

    def test_quote_time_availability_and_estimates(self):
        changes = [
            {"ts": TS2},
            {"ts": "2026-11-02T14:59:59+00:00"},
            {"available_ts": TS2},
            {"available_ts": None},
            {"ts": "2026-11-02 15:00:00"},
            {"stale": True},
            {"kind": "estimated"},
            {"source": "conservative_fallback"},
            {"basis": "拆股后"},
        ]
        for change in changes:
            with self.subTest(change=change):
                evidence, market = fixture()
                market["fill_quotes"]["f1"].update(change)
                result = execution_evidence_metrics(evidence, market)
                self.assertIsNone(result["spread_pct_at_fill"])
                self.assertIsNone(result["slippage_vs_mid_bps"])
                self.assertEqual(result["data_completeness"], "partial")
        evidence, _ = fixture()
        result = execution_evidence_metrics(evidence, {
            "estimate": estimate_execution("QQQ", "SELL", 10, 100),
            "bid": 99, "ask": 101,
        })
        self.assertEqual(result["data_completeness"], "unknown")

    def test_partial_fills_duplicates_and_reconciliation(self):
        evidence, market = fixture()
        evidence["last_fill_ts"] = TS2
        evidence["price"] = 100.2
        evidence["fills"][0]["qty"] = 4
        evidence["fills"].append({
            "fill_id": "f2", "qty": 6, "price": 101, "ts": TS2,
            "basis": BASIS, "qty_semantics": "incremental",
        })
        market["fill_quotes"]["f2"] = dict(
            market["fill_quotes"]["f1"], ts=TS2, available_ts=TS2)
        evidence["fills"].append(copy.deepcopy(evidence["fills"][0]))
        result = execution_evidence_metrics(evidence, market)
        self.assertEqual(result["reconciliation"]["status"], "matched")
        self.assertEqual(result["reconciliation"]["qty"], 10)
        self.assertEqual(len(result["fills"]), 2)
        self.assertAlmostEqual(result["slippage_vs_mid_bps"], -20)
        del market["fill_quotes"]["f2"]
        self.assertIsNone(
            execution_evidence_metrics(evidence, market)["spread_pct_at_fill"])
        evidence["fills"][-1]["qty"] = 5
        self.assertEqual(
            execution_evidence_metrics(evidence, market)["reconciliation"]["status"],
            "unknown")

    def test_cumulative_reports_and_split_basis_not_inferred(self):
        evidence, market = fixture()
        evidence["fills"][0]["qty_semantics"] = "cumulative"
        self.assertIsNone(
            execution_evidence_metrics(evidence, market)["spread_pct_at_fill"])
        evidence, market = fixture()
        evidence["fills"][0]["basis"] = "拆股后"
        self.assertIsNone(
            execution_evidence_metrics(evidence, market)["slippage_vs_mid_bps"])
        # 同一权威口径成比例改变股数和价格，百分比不受拆股影响。
        evidence, market = fixture()
        before = execution_evidence_metrics(evidence, market)
        evidence["qty"] *= 2
        evidence["price"] /= 2
        evidence["fills"][0]["qty"] *= 2
        evidence["fills"][0]["price"] /= 2
        market["fill_quotes"]["f1"]["bid"] /= 2
        market["fill_quotes"]["f1"]["ask"] /= 2
        after = execution_evidence_metrics(evidence, market)
        self.assertEqual(before["spread_pct_at_fill"], after["spread_pct_at_fill"])
        self.assertEqual(before["slippage_vs_mid_bps"], after["slippage_vs_mid_bps"])

    def test_daily_volume_is_posthoc_and_requires_twenty_sessions(self):
        evidence, market = fixture()
        result = execution_evidence_metrics(evidence, market)
        self.assertIn("事后", result["volume_context"])
        self.assertGreater(
            market["daily_volume"]["available_ts"], evidence["last_fill_ts"])
        for change in (
            {"previous_20_sessions": []},
            {"previous_20_sessions_verified": False},
            {"basis": "拆股后"},
            {"volume": None},
            {"available_ts": TS},
            {"session_date": "无效"},
        ):
            with self.subTest(change=change):
                changed = copy.deepcopy(market)
                changed["daily_volume"].update(change)
                self.assertIsNone(execution_evidence_metrics(
                    evidence, changed)["volume_pct_of_20d_avg"])
        for sample in market["daily_volume"]["previous_20_sessions"]:
            sample["volume"] = 0
        self.assertIsNone(
            execution_evidence_metrics(evidence, market)["volume_pct_of_20d_avg"])

    def test_inputs_verdicts_and_existing_functions_unchanged(self):
        evidence, market = fixture()
        original = copy.deepcopy((evidence, market))
        estimate = estimate_execution("QQQ", "SELL", 10, 100)
        quality = actual_execution_quality(
            side="SELL", requested_qty=10, dealt_qty=10,
            reference_price=100, average_fill_price=99)
        cost = sell_cost_breakdown(evidence)
        execution_evidence_metrics(evidence, market)
        self.assertEqual((evidence, market), original)
        self.assertEqual(estimate, estimate_execution("QQQ", "SELL", 10, 100))
        self.assertEqual(quality, actual_execution_quality(
            side="SELL", requested_qty=10, dealt_qty=10,
            reference_price=100, average_fill_price=99))
        self.assertEqual(cost, sell_cost_breakdown(evidence))
        self.assertEqual(evidence["hindsight_verdict"], "合理")
        self.assertEqual(evidence["analysis"]["hindsight"]["verdict"], "不合理")

    def test_fixed_thirty_denominator_and_authoritative_reader(self):
        evidence, market = fixture()
        orders = [dict(copy.deepcopy(evidence), order_id=str(i)) for i in range(31)]
        original = copy.deepcopy(orders)
        with patch("fill_ledger.sell_execution_evidence", return_value=orders) as reader:
            report = replay_execution_evidence_metrics(
                market_by_order={str(i): market for i in range(31)})
        reader.assert_called_once_with(None, None)
        self.assertEqual(orders, original)
        self.assertEqual(report["found"], 30)
        self.assertEqual(len(report["records"]), 30)
        self.assertTrue(report["selection_verified"])
        self.assertFalse(report["validation_passed"])
        for field in report["fields"].values():
            self.assertEqual(field["denominator"], 30)
            self.assertEqual(field["known"], 30)
        incomplete = execution_evidence_coverage(
            [execution_evidence_metrics(evidence)])
        for field in incomplete["fields"].values():
            self.assertEqual(field["denominator"], 30)
            self.assertEqual(field["coverage"], 0)
            self.assertEqual(field["missing_reasons"]["缺少真实卖出订单"], 29)

    def test_unknown_times_and_duplicate_order_ids_block_selection(self):
        evidence, _ = fixture()
        evidence["last_fill_ts"] = None
        with patch("fill_ledger.sell_execution_evidence",
                   return_value=[evidence, copy.deepcopy(evidence)]):
            report = replay_execution_evidence_metrics()
        self.assertFalse(report["selection_verified"])
        self.assertFalse(report["identities_verified"])
        self.assertEqual(len(report["unknown_order_times"]), 2)
        self.assertFalse(report["validation_passed"])
