"""运行版本与验收材料的只读核对；不调用行情、模型或交易入口。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from model_versions import runtime_acceptance_lines, runtime_acceptance_summary


class RuntimeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.identity = {
            "run_id": "main-20261029",
            "code_version": "fixture-current",
            "started_at": "2026-10-29T12:00:00+00:00",
            "config_digest": "fixture-config",
            "entrypoint": "orchestrator.main",
        }
        self.case_ids = (
            "dual_model_unavailable",
            "symbol_not_found",
            "etf_fundamentals_missing",
            "pre_fix_price_mismatch",
            "mixed_old_worker",
        )

    def evidence(self, relation="normal_degradation"):
        return {
            "runtime": copy.deepcopy(self.identity),
            "fix_version": "fixture-fix",
            "expected_result": "已有预期结果",
            "observed_result": "已有预期结果",
            "fault_classification": "引用既有故障分类",
            "data_health_result": "引用既有健康结果",
            "source": "fixture:验收记录",
            "relation": relation,
        }

    def document(self):
        return {
            "current_runtime": copy.deepcopy(self.identity),
            "cases": {key: self.evidence() for key in self.case_ids},
        }

    def test_frozen_cases_missing_evidence_are_unknown(self):
        summary = runtime_acceptance_summary({})
        self.assertEqual(
            [row["case_id"] for row in summary["cases"]],
            list(self.case_ids))
        self.assertTrue(all(
            row["status"] == "unknown" for row in summary["cases"]))
        self.assertFalse(summary["accepted"])

    def test_complete_cases_reference_runtime_without_mutation(self):
        document = self.document()
        before = copy.deepcopy(document)
        summary = runtime_acceptance_summary(document)
        self.assertTrue(all(
            row["status"] == "normal_degradation"
            for row in summary["cases"]))
        self.assertEqual(document, before)
        self.assertFalse(summary["accepted"])

    def test_current_reproduction_requires_result_mismatch(self):
        document = self.document()
        case = document["cases"]["dual_model_unavailable"]
        case["relation"] = "current_reproduction"
        self.assertEqual(
            runtime_acceptance_summary(document)["cases"][0]["status"],
            "unknown")
        case["observed_result"] = "引用记录中的复现结果"
        self.assertEqual(
            runtime_acceptance_summary(document)["cases"][0]["status"],
            "current_reproduction")

    def test_historical_event_requires_earlier_aware_timestamp(self):
        document = self.document()
        case = document["cases"]["pre_fix_price_mismatch"]
        case["relation"] = "historical"
        runtime = case["runtime"]
        runtime["run_id"] = "old-main"
        runtime["code_version"] = "fixture-old"
        runtime["started_at"] = "2026-10-28T12:00:00+00:00"
        self.assertEqual(
            runtime_acceptance_summary(document)["cases"][3]["status"],
            "historical")
        for value in ("无效时间", "2026-10-28T12:00:00",
                      "2026-10-30T12:00:00+00:00"):
            with self.subTest(value=value):
                runtime["started_at"] = value
                self.assertEqual(
                    runtime_acceptance_summary(document)["cases"][3]["status"],
                    "unknown")

    def test_old_worker_cannot_inherit_main_identity(self):
        document = self.document()
        case = document["cases"]["mixed_old_worker"]
        case["runtime"]["run_id"] = "old-worker"
        case["runtime"]["code_version"] = "fixture-old"
        case["runtime"]["entrypoint"] = "worker.main"
        self.assertEqual(
            runtime_acceptance_summary(document)["cases"][4]["status"],
            "unknown")

    def test_conflicting_or_incomplete_identity_is_unknown(self):
        for key in self.identity:
            with self.subTest(key=key):
                document = self.document()
                document["cases"]["symbol_not_found"]["runtime"][key] = ""
                self.assertEqual(
                    runtime_acceptance_summary(document)["cases"][1]["status"],
                    "unknown")

    def test_existing_classification_and_health_evidence_required(self):
        for key in ("fix_version", "source", "fault_classification",
                    "data_health_result", "expected_result"):
            with self.subTest(key=key):
                document = self.document()
                del document["cases"]["etf_fundamentals_missing"][key]
                self.assertEqual(
                    runtime_acceptance_summary(document)["cases"][2]["status"],
                    "unknown")

    def test_malformed_documents_do_not_imply_acceptance(self):
        for document in (None, [], "merged", {"cases": []},
                         {"current_runtime": "merged", "cases": {}}):
            with self.subTest(document=document):
                summary = runtime_acceptance_summary(document)
                self.assertFalse(summary["accepted"])
                self.assertTrue(all(
                    row["status"] == "unknown" for row in summary["cases"]))

    def test_reader_is_read_only_and_missing_material_warns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            lines = runtime_acceptance_lines(path)
            self.assertFalse(path.exists())
            self.assertTrue(any("告警" in line for line in lines))
            path.write_text(json.dumps(self.document()), encoding="utf-8")
            before = path.read_bytes()
            lines = runtime_acceptance_lines(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(any("未独立验证" in line for line in lines))
            self.assertTrue(any("验收状态" in line for line in lines))
            path.write_text("{", encoding="utf-8")
            lines = runtime_acceptance_lines(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "{")
            self.assertTrue(any("不可读" in line for line in lines))
