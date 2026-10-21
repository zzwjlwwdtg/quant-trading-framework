"""All numerical inputs are synthetic unit fixtures, NOT market or backtest results."""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic import ValidationError
from cn_decisions.core import (DecisionInput, DecisionResult, Estimate, RuleConfig, SourceRef,
                               ValidationReport, action, build_decision, classify_direction, render_report)
from cn_decisions.store import compare_predictions, freeze, get_run, read

NOW = datetime.fromisoformat("2026-10-21T15:15:00+08:00")


def request(**changes):
    values = dict(symbol="600000.SH", prediction_time=NOW, cutoff_time=NOW - timedelta(minutes=15),
                  horizon_trading_days=5)
    values.update(changes)
    return DecisionInput(**values)


def numerical_fixture(**changes):
    """A non-synthetic flag is used ONLY to exercise the positive gate branch locally."""
    report = ValidationReport(report_id="UNIT_FIXTURE_NOT_PERFORMANCE", model_id="unit", model_version="1",
                              horizon_trading_days=5, train_label_end=NOW-timedelta(days=100),
                              calibration_start=NOW-timedelta(days=99), calibration_end=NOW-timedelta(days=70),
                              test_start=NOW-timedelta(days=69), test_end=NOW-timedelta(days=2),
                              available_at=NOW-timedelta(days=1), oos_samples=220, oos_brier=0.2,
                              baseline_brier=0.25, calibration_method="PLATT", purge_embargo_verified=True,
                              is_synthetic=False, effective_sample_size_note="unit fixture only; effective N unknown")
    ref = SourceRef(id="UNIT_NOT_REAL", source="synthetic unit input", url="https://example.invalid/unit",
                    published_at=NOW-timedelta(hours=1), available_at=NOW-timedelta(hours=1))
    return request(model_id="unit", model_version="1", validation=report, estimate=Estimate(raw_p_up=0.7, calibrated_p_up=0.7,
                   expected_gross_return=0.01, predicted_return_p05=-0.05), snapshot_values={"unit_feature": 1.0},
                   source_refs=(ref,), data_available_at=NOW-timedelta(hours=1), data_complete=True,
                   target_window={"basis":"RESEARCH_CLOSE_TO_CLOSE", "calendar_snapshot_id":"UNIT_NOT_REAL_CALENDAR",
                                  "start":NOW.date(), "end":(NOW+timedelta(days=7)).date()},
                   risk_ok=True, executable=True, costs=0.001, **changes)


class Decisions(unittest.TestCase):
    def test_cli_emits_utf8_json_even_with_japanese_codepage(self):
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            env={**os.environ,"PYTHONIOENCODING":"cp932"}
            proc=subprocess.run([sys.executable,"-m","cn_decisions","--db",str(Path(d)/"unit.sqlite3"),
                                 "analyze","--symbol","600000.SH","--horizon","5","--json"],
                                cwd=Path(__file__).resolve().parents[1],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                timeout=10,check=True)
            self.assertEqual(json.loads(proc.stdout.decode("utf-8"))["direction"],"证据不足")

    def test_legacy_morning_cache_is_not_replayed_as_current_forecast(self):
        from unittest.mock import patch
        import bot_gateway
        legacy={"generated_at":"2026-10-21T09:03:04+09:00","markdown":"old probability 66%","overnight":{"last_a_close":"2026-10-19"}}
        with patch.object(bot_gateway,"_read_json",return_value=legacy):
            safe=bot_gateway.brief("morning")
            self.assertEqual(safe["status"],"LEGACY_BACKGROUND_ONLY")
            self.assertNotIn("66%",safe["markdown"])
            self.assertEqual(legacy["markdown"],"old probability 66%")

    def test_no_data_all_horizons_no_fake_probability(self):
        for h in (1, 3, 5):
            r = build_decision(request(horizon_trading_days=h))
            self.assertEqual((r.direction, r.trade_action), ("证据不足", "数据不足"))
            self.assertIsNone(r.raw_p_up); self.assertIsNone(r.calibrated_p_up)
            self.assertIsNone(r.expected_net_return); self.assertFalse(r.oos_edge_validated)
            self.assertIn(r.run_id, render_report(r))
            self.assertIn("未验证", render_report(r))

    def test_boundary_rules(self):
        c = RuleConfig()
        for p, expected in ((0.349999,"强看空"),(0.35,"强看空"),(0.350001,"偏空"),
                            (0.439999,"偏空"),(0.44,"偏空"),(0.440001,"中性"),
                            (0.559999,"中性"),(0.56,"偏多"),(0.560001,"偏多"),
                            (0.649999,"偏多"),(0.65,"强看多"),(0.650001,"强看多")):
            self.assertEqual(classify_direction(p, True, c), expected)
            self.assertEqual(classify_direction(p, False, c), "证据不足")

    def test_full_rule_fixture_costs_execution_and_no_short(self):
        r = build_decision(numerical_fixture())
        self.assertEqual((r.direction,r.trade_action),("强看多","观望"))
        self.assertIsNone(r.expected_net_return)  # research close-to-close is not executable trade P&L
        for change in ({"costs":0.02},{"costs":None},{"executable":False},{"risk_ok":False}):
            changed = numerical_fixture().model_copy(update=change)
            self.assertEqual(build_decision(changed).trade_action,"观望")
        c = RuleConfig()
        self.assertEqual(action("强看多",0.009,True,True,c),"做多候选")
        self.assertEqual(action("强看多",0.002,True,True,c),"观望")
        self.assertEqual(action("强看空",0.1,True,True,c),"观望")

    def test_mock_never_qualifies_despite_high_probability(self):
        r = build_decision(numerical_fixture().model_copy(update={"is_mock_data":True}))
        self.assertEqual(r.direction,"证据不足"); self.assertIsNone(r.calibrated_p_up)
        self.assertIn("fixture",render_report(r))

    def test_source_and_data_time_leaks_rejected(self):
        with self.assertRaises(ValidationError):
            request(data_available_at=NOW)
        with self.assertRaises(ValidationError):
            request(source_refs=(SourceRef(id="future",source="fixture",url="https://example.invalid/unit",
                                          published_at=NOW,available_at=NOW),))
        with self.assertRaises(ValidationError):
            request(prediction_time=NOW.replace(tzinfo=None))

    def test_calibration_overlap_and_future_validation_rejected(self):
        v=numerical_fixture().validation
        for update in ({"calibration_end":v.test_start},{"train_label_end":v.calibration_start},
                       {"available_at":v.test_end-timedelta(seconds=1)}):
            with self.assertRaises(ValidationError):
                ValidationReport.model_validate({**v.model_dump(),**update})
        with self.assertRaises(ValidationError):
            request(validation=v.model_copy(update={"available_at":NOW}))

    def test_missing_oos_edge_overlap_or_n_abstains(self):
        base=numerical_fixture()
        for update, flag in (({"oos_brier":0.25},"NO_POSITIVE_OOS_EDGE"),
                             ({"oos_samples":199},"OOS_SAMPLES_INSUFFICIENT"),
                             ({"is_synthetic":True},"SYNTHETIC_VALIDATION"),
                             ({"purge_embargo_verified":False},"OVERLAPPING_LABELS_UNVERIFIED"),
                             ({"horizon_trading_days":1},"VALIDATION_IDENTITY_MISMATCH")):
            r=build_decision(base.model_copy(update={"validation":base.validation.model_copy(update=update)}))
            self.assertEqual(r.direction,"证据不足"); self.assertIn(flag,r.quality_flags)
            self.assertFalse(r.oos_edge_validated); self.assertIsNone(r.calibrated_p_up)

    def test_stale_data_abstains(self):
        r=build_decision(numerical_fixture().model_copy(update={"data_available_at":NOW-timedelta(days=2)}))
        self.assertIn("DATA_STALE",r.quality_flags); self.assertEqual(r.direction,"证据不足")

    def test_repeatable_and_strict_outputs(self):
        req=numerical_fixture(); a,b=build_decision(req),build_decision(req)
        self.assertEqual(a.model_dump_json(),b.model_dump_json())
        for field,value in (("direction","偏空"),("trade_action","做多候选"),("calibrated_p_up",0.6),
                            ("expected_net_return",0.5),("quality_flags",["altered"]),("run_id","cn-p0-"+"0"*32)):
            data=json.loads(a.model_dump_json()); data[field]=value
            with self.assertRaises(ValidationError):
                DecisionResult.model_validate_json(json.dumps(data))
        with self.assertRaises(ValidationError):
            render_report(a.model_copy(update={"direction":"偏空"}))

    def test_immutable_freeze_conflict_and_read_only_lookup(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"runs.sqlite3"
            self.assertEqual(read("600000.SH",5,path=path)["status"],"NO_RECORD")
            self.assertFalse(path.exists())
            r=build_decision(numerical_fixture()); self.assertEqual(freeze(r,path),r)
            self.assertEqual(freeze(r,path),r); self.assertEqual(get_run(path,r.run_id),r)
            self.assertEqual(read("600000.SH",5,path=path)["result"]["run_id"],r.run_id)
            same_identity=build_decision(numerical_fixture().model_copy(update={"estimate":Estimate(raw_p_up=0.8,calibrated_p_up=0.8,expected_gross_return=0.01,predicted_return_p05=-0.05)}))
            with self.assertRaisesRegex(ValueError,"CONFLICT"):
                freeze(same_identity,path)
            db=sqlite3.connect(path)
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE cn_decision_runs SET body='{}'")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("DELETE FROM cn_decision_runs")
            finally:
                db.close()

    def test_revision_is_same_horizon_only_and_has_threshold_causes(self):
        req=numerical_fixture(); old=build_decision(req)
        new=build_decision(req,RuleConfig(version="unit-rules-2",strong_bullish=0.8))
        diff=compare_predictions(old,new)
        self.assertTrue(diff["direction_changed"]); self.assertIn("decision_rule_version",diff["causes"])
        self.assertEqual(diff["delta_p_up"],0.0)
        with self.assertRaises(ValueError):
            compare_predictions(old,build_decision(request(horizon_trading_days=1)))

    def test_config_types_and_null_not_zero(self):
        with self.assertRaises(ValidationError):
            RuleConfig(strong_bullish=0.3)
        for value in (float("nan"),float("inf"),-0.1,1.1,"0.7",True):
            with self.assertRaises(ValidationError):
                Estimate(raw_p_up=value,calibrated_p_up=0.7)
        r=build_decision(numerical_fixture().model_copy(update={"estimate":Estimate(raw_p_up=0.0,calibrated_p_up=0.0,expected_gross_return=0.0)}))
        self.assertEqual(r.calibrated_p_up,0.0); self.assertEqual(r.direction,"强看空")


if __name__ == "__main__":
    unittest.main()
