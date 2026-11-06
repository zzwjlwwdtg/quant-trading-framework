"""只读止损拒单监控；合成材料与临时目录。"""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import _stop_fail_monitor as monitor
import webui

NOW = "2026-11-06T06:00:00+00:00"


class StopFailMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_parse_jst_fail_exception_and_software_config(self):
        path = self.write("run_20261106.log", "\n".join([
            "2026-11-06 14:59:00 [trader] STOP FAIL US.AAPL @ 100.00: simulated STOP unsupported",
            "2026-11-06 14:59:00 [trader] US.AAPL broker stop unavailable; armed software stop at $100.00",
            "2026-11-06 14:59:01 [trader] STOP EXC  US.MSFT: disconnected",
            "2026-11-06 14:59:02 [risk-monitor] software stop check failed: offline",
        ]))
        result = monitor.parse_events([path], start="2026-11-05T06:00:00Z", end=NOW)
        self.assertEqual(result["coverage"]["parsed_lines"], 2)
        self.assertEqual(result["events"][0]["ts"], "2026-11-06T05:59:00+00:00")
        self.assertTrue(result["events"][0]["software_armed_at_event"])
        self.assertIsNone(result["events"][1]["stop_price"])
        self.assertFalse(result["risk_monitor_evidence"]["proves_current_polling_health"])
        self.assertEqual(result["risk_monitor_evidence"]["ts"], "2026-11-06T05:59:02+00:00")

    def test_dedup_rotation_keeps_candidate_denominator(self):
        line = "2026-11-06 14:59:00 [trader] STOP FAIL US.AAPL @ 100: rejected"
        paths = [self.write("a.log", line), self.write("b.log", line + "\n[trader] STOP FAIL malformed")]
        report = monitor.parse_events(paths, start="2026-11-05T00:00:00Z", end=NOW)
        self.assertEqual(report["coverage"]["candidate_lines"], 3)
        self.assertEqual(report["coverage"]["parse_rate"], 2 / 3)
        self.assertEqual(report["coverage"]["unique_events"], 1)
        self.assertEqual(len(report["events"][0]["sources"]), 2)
        self.assertEqual(len(report["unparsed"]), 1)

    def test_fixed_window_and_bad_timestamp_are_explicit(self):
        path = self.write("a.log", "\n".join([
            "2026-10-01 00:00:00 [trader] STOP FAIL US.X @ 1: old",
            "2026-11-07 00:00:00 [trader] STOP FAIL US.X @ 1: future",
            "2026-99-99 00:00:00 [trader] STOP FAIL US.X @ 1: bad time",
            "2026-11-06 14:59:00 [trader] STOP FAIL US.X @ 0: zero stop",
        ]))
        report = monitor.parse_events([path, self.root / "missing"], start="2026-11-05T00:00:00Z", end=NOW)
        self.assertEqual(report["events"], [])
        self.assertEqual(report["coverage"]["candidate_lines"], 2)
        self.assertEqual(len(report["source_errors"]), 1)

    def config(self, mode="software", qty=10):
        state = {"US.AAPL": {"protective_stop_mode": mode, "protective_stop_price": 100, "protective_stop_order_id": "B1"}}
        positions = {"moomoo_ok": True, "observed_at": NOW, "positions": [{"ticker": "US.AAPL", "qty": qty, "source": "moomoo_live"}]}
        return state, positions

    def classify(self, state, positions, ts=NOW):
        return monitor.protection_rows(state, positions, state_observed_at=ts, now=NOW)[0]

    def test_config_modes_are_not_proof_of_effectiveness(self):
        for mode in ("broker", "software", "software_triggered", "unprotected"):
            with self.subTest(mode=mode):
                state, positions = self.config(mode)
                before = copy.deepcopy((state, positions))
                row = self.classify(state, positions)
                self.assertEqual(row["status"], mode)
                self.assertEqual(row["protection_effectiveness"], "unknown")
                self.assertEqual((state, positions), before)

    def test_flat_stale_missing_duplicate_and_state_only_positions(self):
        state, positions = self.config(qty=0)
        self.assertEqual(self.classify(state, positions)["status"], "flat")
        for mutation in ("missing_time", "future", "stale", "duplicate", "state_only", "negative", "nan"):
            state, positions = self.config()
            if mutation == "missing_time": positions.pop("observed_at")
            elif mutation == "future": positions["observed_at"] = "2026-11-06T06:01:00Z"
            elif mutation == "stale": positions["observed_at"] = "2026-11-05T06:00:00Z"
            elif mutation == "duplicate": positions["positions"] *= 2
            elif mutation == "state_only": positions["positions"][0]["source"] = "state_only_recent"
            elif mutation == "negative": positions["positions"][0]["qty"] = -1
            else: positions["positions"][0]["qty"] = float("nan")
            with self.subTest(mutation=mutation):
                self.assertEqual(self.classify(state, positions)["status"], "unknown")
                self.assertIsNone(self.classify(state, positions)["position_qty"])

    def test_missing_config_not_unprotected_and_stale_state_is_unknown(self):
        state, positions = self.config()
        self.assertEqual(self.classify({}, positions)["status"], "unknown")
        self.assertEqual(self.classify(state, positions, "2026-11-01T00:00:00Z")["status"], "unknown")
        state["US.AAPL"]["protective_stop_mode"] = "broker"
        state["US.AAPL"].pop("protective_stop_order_id")
        self.assertEqual(self.classify(state, positions)["status"], "unknown")

    def test_markouts_require_observed_market_base_not_stop_price(self):
        event = {"ts": "2026-11-06T05:59:00Z", "ticker": "US.AAPL", "stop_price": 100}
        quotes = [{"ts": NOW, "available_ts": NOW, "ticker": "US.AAPL", "kind": "observed", "source": "history:1", "price": 99}]
        self.assertIsNone(monitor.event_markouts(event, quotes)["1"]["return_pct"])
        event["market_observation"] = {"price": 100, "ts": event["ts"], "available_ts": event["ts"], "kind": "observed", "source": "history:0"}
        self.assertAlmostEqual(monitor.event_markouts(event, quotes)["1"]["return_pct"], -1)
        self.assertIsNone(monitor.event_markouts(event, quotes * 2)["1"]["return_pct"])
        quotes[0]["kind"] = "modeled"
        self.assertIsNone(monitor.event_markouts(event, quotes)["1"]["return_pct"])

    def test_file_report_is_read_only_and_does_not_guess_cache_timestamp(self):
        self.write("run_20261106.log", "2026-11-06 14:59:00 [trader] STOP EXC US.AAPL: offline")
        state, positions = self.config()
        positions.pop("observed_at")
        state_path = self.write("state.json", json.dumps(state))
        pos_path = self.write("positions.json", json.dumps(positions))
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with patch.object(Path, "write_text", side_effect=AssertionError("不得写盘")):
            report = monitor.report_from_files(logs_dir=self.root, state_path=state_path, positions_path=pos_path, now=datetime.fromisoformat(NOW))
        self.assertEqual(report["protection"][0]["status"], "unknown")
        self.assertEqual(report["markout_coverage"]["1"], {"known": 0, "total": 1})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_webui_uses_local_files_and_memory_cache_only(self):
        with patch.object(webui, "_STOP_FAIL_REPORT", {"at": 0, "data": None}), \
                patch.object(monitor, "report_from_files", return_value={"events": [{"i": i} for i in range(60)]}) as build:
            first = webui.api_stop_fail_monitor()
            self.assertEqual(len(first["events"]), 50)
            first["events"].clear()
            self.assertEqual(len(webui.api_stop_fail_monitor()["events"]), 50)
            build.assert_called_once()

    def test_positions_cache_has_observation_time_only_for_broker_success(self):
        state, _ = self.config()
        state_path = self.write("state.json", json.dumps(state))
        row = dict(code="US.AAPL", qty=10, cost_price=100, current_price=101,
                   market_val=1010, pl_val=10, pl_ratio=0.01)
        with patch.object(webui, "STATE_PATH", state_path), \
                patch.object(webui, "_WEBUI_CACHE_DIR", self.root), \
                patch.object(webui, "_fetch_moomoo_live_positions", side_effect=[[row], None]):
            self.assertTrue(webui.api_positions()["moomoo_ok"])
            cache_path = self.root / "positions_live.json"
            data = monitor.read_json(cache_path)
            observed = monitor.instant(data["observed_at"])
            rows = monitor.protection_rows(
                state, data, state_observed_at=observed, now=datetime.now(timezone.utc),
            )
            self.assertEqual(rows[0]["status"], "software")
            self.assertEqual(rows[0]["position_qty"], 10)
            cache_path.unlink()
            self.assertIsNone(webui.api_positions()["observed_at"])
