"""入场质量研究回归测试；仅由 unittest discover 在隔离环境中执行。"""

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from probe_entry_quality_research import (
    CHANGE_DESCRIPTION, HORIZONS, evaluate, preregister, write_verdict,
)


class ProbeEntryQualityResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "preregistration.json"
        self.manifest = {
            "events": "a" * 64, "prices": "b" * 64, "calendar": "c" * 64,
        }
        # 仅为测试夹具，不是生产 GAP_MAX 或校准结论。
        self.calibration = {
            "gap_max": 0.02, "verdict": "pass",
            "sample_end": "2026-06-30",
            "preregistration_sha256": "d" * 64,
            "verdict_sha256": "e" * 64,
        }
        registration = {"registered_at": "2023-12-31T00:00:00Z", "train_start": "2024-01-01", "train_end": "2026-06-30"}
        raw = json.dumps(registration).encode()
        registration_path = Path(self.tmp.name) / "calibration-registration.json"
        registration_path.write_bytes(raw)
        self.calibration.update(preregistration_path=str(registration_path), preregistration_sha256=hashlib.sha256(raw).hexdigest())
        verdict = {"gap_max": 0.02, "verdict": "pass", "sample_end": "2026-06-30", "preregistration_sha256": self.calibration["preregistration_sha256"]}
        raw = json.dumps(verdict).encode()
        verdict_path = Path(self.tmp.name) / "calibration-verdict.json"
        verdict_path.write_bytes(raw)
        self.calibration.update(verdict_path=str(verdict_path), verdict_sha256=hashlib.sha256(raw).hexdigest())
        self.sessions = {}
        for event in self.sample():
            for horizon in ("EOD", "T+1 EOD"):
                ts = event["forward_prices"][horizon]["ts"]
                self.sessions[ts[:10]] = ts
        self.registration_args = dict(
            event_ids=[e["event_id"] for e in self.sample()], sessions=self.sessions,
            registered_at="2026-11-03T22:00:00Z",
        )
        preregister(
            self.path, as_of="2026-11-03",
            gap_calibration=self.calibration, universe=["US.NBIS", "US.SOXL"],
            evidence_manifest=self.manifest,
            **self.registration_args,
        )

    def event(self, event_id, day, *, skip=True, result=-0.01):
        return {
            "event_id": event_id, "ticker": "US.NBIS",
            "trigger": "sim_active_probe_after_claude_veto",
            "ts": f"{day}T14:00:00-04:00", "session_date": day,
            "snapshot_ts": f"{day}T14:00:00-04:00",
            "ref_price": 100, "live_price": 100,
            "quote_source": "historical_quote",
            "ai_gate_status": "HOLD" if skip else "BUY",
            "regime_today": "neutral",
            "evidence": {"snapshot": event_id, "calendar": "fixture"},
            "forward_prices": {
                "1h": {"ts": f"{day}T15:00:00-04:00", "price": 100 * (1 + result)},
                "EOD": {"ts": f"{day}T16:00:00-04:00", "price": 100 * (1 + result)},
                "T+1 EOD": {
                    "ts": (
                        datetime.fromisoformat(f"{day}T16:00:00-04:00")
                        + timedelta(days=1)
                    ).isoformat(),
                    "price": 100 * (1 + result),
                },
            },
        }

    def sample(self):
        events = []
        for period, start in (("train", "2026-06-01"), ("oos", "2026-07-06")):
            first = datetime.fromisoformat(start)
            for i in range(4):
                day = (first + timedelta(days=i)).date().isoformat()
                events.append(self.event(f"{period}-{i}", day))
            events.append(self.event(f"{period}-pass", start, skip=False, result=0.02))
        return events

    def run_report(self, events, **kwargs):
        return evaluate(
            self.path, events, observed_at="2026-11-03T23:00:00Z",
            evidence_manifest=kwargs.get("manifest", self.manifest),
        )

    def test_preregistration_is_immutable_and_research_only(self):
        plan = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(plan["change"]["description"], CHANGE_DESCRIPTION)
        with self.assertRaises(FileExistsError):
            preregister(
                self.path, as_of="2026-11-03",
                gap_calibration=self.calibration, universe=["US.NBIS"],
                evidence_manifest=self.manifest,
                **self.registration_args,
            )

    def test_manual_or_oos_calibration_is_rejected(self):
        for changes in ({"verdict": "reject"}, {"sample_end": "2026-07-01"},
                        {"gap_max": float("nan")}, {"gap_max": True}):
            calibration = {**self.calibration, **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                preregister(
                    Path(self.tmp.name) / "invalid.json", as_of="2026-11-03",
                    gap_calibration=calibration, universe=["US.NBIS"],
                    evidence_manifest=self.manifest,
                    **self.registration_args,
                )

    def test_pass_retains_cash_denominator_and_never_allows_deployment(self):
        events = self.sample()
        before = copy.deepcopy(events)
        report = self.run_report(events)
        self.assertEqual(events, before)
        self.assertEqual(report["status"], "pass")
        self.assertFalse(report["deployment_allowed"])
        for period in ("train", "oos"):
            for horizon in HORIZONS:
                result = report["results"][period][horizon]
                self.assertEqual(result["skip_negative_p"], 1 / 16)
                self.assertAlmostEqual(result["baseline"]["avg_return"], -0.004)
                self.assertAlmostEqual(result["filtered_cash_zero"]["avg_return"], 0.004)

    def test_gap_boundary_and_other_two_conditions_are_independent(self):
        cases = (
            ({"ref_price": 100, "live_price": 102}, "ref_live_gap"),
            ({"ai_gate_status": "SELL"}, "ai_gate"),
            ({"regime_today": "risk_off"}, "risk_off"),
        )
        for changes, reason in cases:
            events = self.sample()
            event = events[0]
            event.update(ai_gate_status="BUY")
            event.update(changes)
            row = self.run_report(events)["shadow_records"][0]
            self.assertEqual(row["gate"], "skip")
            self.assertIn(reason, row["reasons"])

    def test_missing_duplicate_and_unmatured_data_block_acceptance(self):
        for kind in ("missing", "duplicate", "unmatured", "fallback", "nan"):
            events = self.sample()
            if kind == "missing":
                del events[0]["regime_today"]
            elif kind == "duplicate":
                events.append(copy.deepcopy(events[0]))
            elif kind == "unmatured":
                events[0]["forward_prices"]["1h"]["ts"] = "2027-01-01T15:00:00Z"
            elif kind == "fallback":
                events[0]["quote_source"] = "fallback_daily"
            else:
                # 无效数值必须明确失败；序列化摘要也拒绝非有限数。
                events[0]["live_price"] = "nan"
            with self.subTest(kind=kind):
                report = self.run_report(events)
                self.assertEqual(report["status"], "insufficient")
                self.assertTrue(report["errors"])

    def test_same_day_duplicates_do_not_create_significance(self):
        events = self.sample()
        for event in events:
            if event["ai_gate_status"] == "HOLD":
                day = "2026-06-01" if event["event_id"].startswith("train") else "2026-07-06"
                replacement = self.event(event["event_id"], day)
                event.update(replacement)
        report = self.run_report(events)
        self.assertEqual(report["status"], "reject")
        self.assertEqual(report["results"]["oos"]["1h"]["skip_negative_p"], 0.5)

    def test_oos_direction_reversal_rejects(self):
        events = self.sample()
        for event in events:
            if event["event_id"].startswith("oos-") and event["ai_gate_status"] == "HOLD":
                for point in event["forward_prices"].values():
                    point["price"] = 103
        report = self.run_report(events)
        self.assertEqual(report["status"], "reject")
        self.assertFalse(report["results"]["oos"]["EOD"]["criterion_b"])

    def test_evidence_change_and_empty_sample(self):
        with self.assertRaises(ValueError):
            self.run_report(self.sample(), manifest={"events": "f" * 64})
        self.assertEqual(self.run_report([])["status"], "insufficient")

    def test_verdict_writes_only_explicit_temporary_directory(self):
        report = self.run_report(self.sample())
        directory = Path(self.tmp.name) / "signals" / "backtest_verdicts"
        path = write_verdict(report, directory)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], report["status"])
        with self.assertRaises(FileExistsError):
            write_verdict(report, directory)

    def test_missing_frozen_event_and_future_snapshot_cannot_pass(self):
        for mutation in ("missing", "future_snapshot", "nan_float", "unknown_regime"):
            events = self.sample()
            if mutation == "missing": events.pop()
            elif mutation == "future_snapshot": events[0]["snapshot_ts"] = "2026-06-01T15:00:00-04:00"
            elif mutation == "nan_float": events[0]["live_price"] = float("nan")
            else: events[0]["regime_today"] = "unknown"
            with self.subTest(mutation=mutation):
                self.assertEqual(self.run_report(events)["status"], "insufficient")

    def test_wrong_eod_and_next_session_price_times_are_rejected(self):
        for horizon, ts in (("EOD", "2026-06-01T15:59:00-04:00"),
                            ("T+1 EOD", "2026-06-03T16:00:00-04:00")):
            events = self.sample()
            events[0]["forward_prices"][horizon]["ts"] = ts
            self.assertEqual(self.run_report(events)["status"], "insufficient")

    def test_registration_tampering_is_rejected(self):
        plan = json.loads(self.path.read_text(encoding="utf-8"))
        plan["gap_calibration"]["gap_max"] = 0.9
        self.path.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_report(self.sample())

    def test_zero_returns_are_not_significant(self):
        events = self.sample()
        for event in events:
            for point in event["forward_prices"].values(): point["price"] = 100
        report = self.run_report(events)
        self.assertEqual(report["status"], "reject")
        self.assertEqual(report["results"]["train"]["EOD"]["skip_negative_p"], 1)

    def test_gap_boundary_sell_and_risk_off_are_shadow_reasons(self):
        events = self.sample()
        events[0].update(ref_price=100, live_price=102, ai_gate_status="SELL", regime_today="risk_off")
        row = self.run_report(events)["shadow_records"][0]
        self.assertEqual(row["reasons"], ["ref_live_gap", "ai_gate", "risk_off"])

    def test_all_losses_on_same_day_do_not_create_independent_significance(self):
        self.test_same_day_duplicates_do_not_create_significance()

    def test_bad_calibration_hash_and_duplicate_manifest_ids_rejected(self):
        for changes in ({"gap_calibration": {**self.calibration, "verdict_sha256": "manual"}},
                        {"event_ids": ["x", "x"]}):
            args = dict(as_of="2026-11-03", gap_calibration=self.calibration,
                        universe=["US.NBIS"], evidence_manifest=self.manifest,
                        **self.registration_args)
            args.update(changes)
            with self.assertRaises(ValueError):
                preregister(Path(self.tmp.name) / "invalid.json", **args)

    def test_calibration_source_content_cannot_be_replaced_by_hash_claims(self):
        Path(self.calibration["verdict_path"]).write_text('{"gap_max":0.9}', encoding="utf-8")
        with self.assertRaises(ValueError):
            preregister(Path(self.tmp.name) / "invalid.json", as_of="2026-11-03",
                        gap_calibration=self.calibration, universe=["US.NBIS"],
                        evidence_manifest=self.manifest, **self.registration_args)
