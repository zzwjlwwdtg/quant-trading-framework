"""Scheduled diagnostics share the quota cooldown without inventing probe results."""
import json
import tempfile
import unittest
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _ai_cli_diag as diag


class DiagnosticCooldownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "diag.json"
        patches = {
            "out": patch.object(diag, "OUT", self.out),
            "help_out": patch.object(diag, "CLAUDE_HELP_OUT", Path(self.tmp.name) / "help.txt"),
            "env": patch.dict(diag.os.environ, {"DIAG_SKIP_CODEX": "1"}),
            "find_codex": patch.object(diag.ai_prompt, "_find_codex_cli", return_value="codex-test"),
            "find_claude": patch.object(diag.ai_prompt, "_find_claude_cli", return_value="claude-test"),
            "run": patch.object(diag, "_run", return_value={"rc": 0, "stdout": "OK", "stderr": ""}),
            "subprocess": patch.object(diag.subprocess, "run", return_value=SimpleNamespace(stdout="help", stderr="")),
            "reason": patch.object(diag.ai_cli_cooldown, "cooldown_reason", return_value=""),
            "record": patch.object(diag.ai_cli_cooldown, "record_result"),
            "emit": patch.object(diag, "_emit_probe_outcome", return_value={}),
            "health": patch.object(diag.claude_gate, "_load_health_state", return_value={}),
        }
        self.mocks = {}
        for name, item in patches.items():
            self.mocks[name] = item.start()
            self.addCleanup(item.stop)

    def result(self):
        return json.loads(self.out.read_text(encoding="utf-8"))

    def claude_probes(self):
        return [c for c in self.mocks["run"].call_args_list if "-p" in c.args[0]]

    def claude_health_calls(self):
        return [c for c in self.mocks["emit"].call_args_list if c.args[0] == "claude"]

    def test_active_cooldown_skips_request_and_health_mutation(self):
        reason = "quota_cooldown until 2026-11-07T19:00:00+00:00"
        self.mocks["reason"].return_value = reason
        diag.main()
        self.mocks["run"].assert_not_called()
        self.mocks["subprocess"].assert_not_called()
        self.mocks["record"].assert_not_called()
        self.assertEqual(self.claude_health_calls(), [])
        self.assertEqual(self.result()["claude"]["reason"], reason)
        self.assertTrue(self.result()["health"]["claude"]["skipped"])
        self.assertNotIn("ok", self.result()["health"]["claude"])

    def test_next_diagnostic_after_expiry_retries_claude(self):
        self.mocks["reason"].side_effect = ["quota_cooldown until reset", ""]
        diag.main()
        self.assertEqual(self.claude_probes(), [])
        diag.main()
        self.assertEqual(len(self.claude_probes()), 1)
        self.assertTrue(self.result()["claude"]["probe_ok"])
        self.assertEqual(self.claude_health_calls()[0].args, ("claude", True, "ok"))
        call = self.mocks["record"].call_args
        self.assertTrue(call.kwargs["success"])
        self.assertEqual(call.kwargs["status"], "ok")
        self.assertEqual(call.kwargs["started_at"].tzinfo, timezone.utc)

    def test_quota_probe_updates_shared_cooldown_once(self):
        quota = "You've hit your limit · resets Nov 8, 4am (Asia/Tokyo)"
        self.mocks["run"].return_value = {"rc": 1, "stdout": quota, "stderr": ""}
        diag.main()
        self.assertEqual(len(self.claude_probes()), 1)
        self.mocks["record"].assert_called_once()
        record = self.mocks["record"].call_args.kwargs
        self.assertFalse(record["success"])
        self.assertIn(quota, record["status"])
        self.assertFalse(self.result()["claude"]["probe_ok"])
        self.assertFalse(self.claude_health_calls()[0].args[1])

    def test_missing_cli_is_not_recorded_as_a_quota_result(self):
        self.mocks["find_claude"].return_value = None
        diag.main()
        self.mocks["run"].assert_not_called()
        self.mocks["record"].assert_not_called()
        self.assertEqual(self.claude_health_calls()[0].args,
                         ("claude", False, "not_installed"))

    def test_token_error_does_not_count_as_probe_success(self):
        self.mocks["run"].return_value = {"rc": 0, "stdout": "token limit reached", "stderr": ""}
        diag.main()
        self.assertFalse(self.mocks["record"].call_args.kwargs["success"])
        self.assertFalse(self.claude_health_calls()[0].args[1])

    def test_success_requires_exact_ok_including_codex_last_message(self):
        for message in ("token expired", "not OK", "OK but unavailable", ""):
            with self.subTest(message=message):
                self.assertFalse(diag._probe_status({"rc": 0, "stdout": message})[0])
        self.assertTrue(diag._probe_status({"rc": 0, "stdout": "  ok\n"})[0])
        self.assertFalse(diag._probe_status({"rc": 1, "stdout": "OK"})[0])
        self.assertFalse(diag._probe_status({"rc": 0, "stdout": "OK"}, "token expired")[0])


if __name__ == "__main__":
    raise SystemExit("Run from agents/: python -m unittest discover -s tests -t .")
