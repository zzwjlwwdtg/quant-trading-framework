"""现金归因只读研究回归；通过 unittest discover 的隔离入口运行。"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from portfolio_analytics import cash_event_attribution
from trade_postmortem import replay_cash_attribution


def fixture(oid="S1"):
    """金额和占款均为明确记账事实，不由订单累计成交量推算。"""
    times = [
        "2026-10-20T20:00:00+00:00",
        "2026-10-21T14:00:00+00:00",
        "2026-10-22T14:00:00+00:00",
    ]
    material = {
        "order_id": oid, "decision_ts": times[1],
        "source_ref": "历史材料:1", "cash_field": "available_cash",
        "currency": "USD", "minor_unit": "0.01",
        "field_definition": "券商已扣剩余订单占款的可用现金",
        "required_cash": "0", "required_cash_source": "决策记录:1",
        "snapshots": {}, "coverage": {}, "postings": [],
    }
    pending = [
        [{"order_id": "B1", "reserved_remaining": "30", "source_ref": "订单:1"}],
        [{"order_id": "B1", "reserved_remaining": "10", "source_ref": "订单:2"}],
        [],
    ]
    for role, ts, value, orders in zip(
        ("before", "decision", "after"), times, ("100", "-10", "50"), pending
    ):
        material["snapshots"][role] = {
            "ts": ts, "value": value, "source_ref": f"快照:{role}",
            "cash_field": "available_cash", "currency": "USD",
            "pending_orders": orders,
        }
    for label, start, end in (
        ("before_to_decision", times[0], times[1]),
        ("decision_to_after", times[1], times[2]),
    ):
        material["coverage"][label] = {
            "complete": True, "source_ref": "账务区间导出:1",
            "start": start, "end": end,
            "currency": "USD", "cash_field": "available_cash",
        }
    for pid, ts, delta, kind in (
        ("P1", times[1], "-110", "部分成交后的实际可用现金记账"),
        ("P2", "2026-10-21T15:00:00+00:00", "10", "撤单释放剩余占款"),
        ("P3", times[2], "50", "跨日结算实际入账"),
    ):
        material["postings"].append({
            "posting_id": pid, "ts": ts, "delta": delta, "kind": kind,
            "source_ref": f"账务:{pid}", "order_id": "B1" if pid != "P3" else oid,
            "settlement_ref": "结算:1" if pid == "P3" else None,
            "currency": "USD", "cash_field": "available_cash",
        })
    return material


class CashAttributionTests(unittest.TestCase):
    def test_fake_minor_unit_cannot_relax_accounting_tolerance(self):
        material = fixture()
        material["minor_unit"] = "1000"
        material["snapshots"]["after"]["value"] = "999"
        report = cash_event_attribution(material)
        self.assertEqual(report["status"], "无法归因")
        self.assertTrue(report["missing_evidence"])

    def test_invalid_nested_postings_and_pending_orders_are_explicit(self):
        for mutation in ("posting", "pending", "null_id", "conflicting_link"):
            material = fixture()
            if mutation == "posting": material["postings"].append(None)
            elif mutation == "pending": material["snapshots"]["before"]["pending_orders"].append(None)
            elif mutation == "null_id": material["postings"][0]["posting_id"] = None
            else: material["postings"].append({**material["postings"][0], "order_id": "OTHER"})
            with self.subTest(mutation=mutation):
                report = cash_event_attribution(material)
                self.assertEqual(report["status"], "无法归因")
                self.assertTrue(report["anomalies"])

    def test_malformed_snapshot_or_coverage_is_missing_evidence(self):
        for field in ("snapshots", "coverage"):
            material = fixture()
            material[field] = ["not a mapping"]
            report = cash_event_attribution(material)
            self.assertEqual(report["status"], "无法归因")
            self.assertTrue(report["missing_evidence"])

    def test_partial_cancel_settlement_and_repeat_are_not_double_counted(self):
        material = fixture()
        original = copy.deepcopy(material)
        first = cash_event_attribution(material)
        self.assertEqual(material, original)
        self.assertEqual(first, cash_event_attribution(material))
        material["postings"] += copy.deepcopy(material["postings"])
        repeated = cash_event_attribution(material)
        self.assertEqual(first, repeated)
        self.assertEqual(first["trigger_shortfall"], "10")
        self.assertEqual(
            [r["unexplained_difference"] for r in first["intervals"]], ["0", "0"])
        self.assertEqual(
            [len(r["events"]) for r in first["intervals"]], [1, 2])
        self.assertFalse(first["missing_evidence"])
        self.assertFalse(first["anomalies"])

    def test_missing_history_never_uses_current_state(self):
        material = fixture()
        del material["snapshots"]["decision"]
        material["current_account"] = {"value": "999999"}
        report = cash_event_attribution(material)
        self.assertEqual(report["status"], "无法归因")
        self.assertIsNone(report["trigger_shortfall"])
        self.assertTrue(report["missing_evidence"])

    def test_conflicting_posting_and_incomplete_coverage_fail_closed(self):
        material = fixture()
        material["postings"].append({**material["postings"][0], "delta": "-109"})
        material["coverage"]["before_to_decision"]["complete"] = False
        report = cash_event_attribution(material)
        self.assertEqual(report["status"], "无法归因")
        self.assertTrue(report["anomalies"])
        self.assertFalse(report["intervals"][0]["accounting_passed"])

    def test_residual_limit_uses_decimal(self):
        for residual, expected in (("50.01", True), ("50.02", False)):
            material = fixture()
            material["snapshots"]["after"]["value"] = residual
            report = cash_event_attribution(material)
            self.assertEqual(report["intervals"][1]["accounting_passed"], expected)

    def test_wrong_currency_or_snapshot_time_cannot_pass(self):
        for change in ("currency", "time"):
            material = fixture()
            if change == "currency":
                material["postings"][0]["currency"] = "JPY"
            else:
                material["snapshots"]["decision"]["ts"] = "2026-10-21T14:01:00+00:00"
            self.assertEqual(cash_event_attribution(material)["status"], "无法归因")

    def test_six_missing_evidence_chains_are_explicit_and_no_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            trades = Path(directory) / "trades.jsonl"
            evidence = Path(directory) / "cash.jsonl"
            rows = [
                {"order_id": f"S{i}", "ts": fixture()["decision_ts"],
                 "tag": "[REBALANCE CASH restore]"}
                for i in range(6)
            ]
            trades.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            evidence.write_text("", encoding="utf-8")
            before = {p.name: p.read_bytes() for p in Path(directory).iterdir()}
            with patch.object(Path, "write_text", side_effect=AssertionError("禁止写入")), \
                    patch.object(Path, "mkdir", side_effect=AssertionError("禁止创建目录")):
                report = replay_cash_attribution(
                    trade_log_path=trades, evidence_path=evidence)
            self.assertTrue(report["coverage"]["complete"])
            self.assertEqual(len(report["records"]), 6)
            self.assertTrue(all(r["missing_evidence"] for r in report["records"]))
            self.assertFalse(report["historical_accounting_passed"])
            self.assertFalse(report["publication_authorized"])
            self.assertEqual(
                before, {p.name: p.read_bytes() for p in Path(directory).iterdir()})

    def test_six_complete_bundles_still_require_forward_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            trades = Path(directory) / "trades.jsonl"
            evidence = Path(directory) / "cash.jsonl"
            materials = [fixture(f"S{i}") for i in range(6)]
            trades.write_text("".join(json.dumps({
                "order_id": m["order_id"], "ts": m["decision_ts"],
                "tag": "REBALANCE CASH restore",
            }) + "\n" for m in materials), encoding="utf-8")
            evidence.write_text(
                "".join(json.dumps(m) + "\n" for m in materials), encoding="utf-8")
            report = replay_cash_attribution(
                trade_log_path=trades, evidence_path=evidence)
            self.assertTrue(report["historical_accounting_passed"])
            self.assertFalse(report["publication_authorized"])
            self.assertFalse(
                report["forward_validation"]["attribution_effectiveness_validated"])
