"""Provider routing remembers outages without inventing failed AI call records."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import ai_cli_cooldown as cooldown
import ai_prompt as ap


class QuotaRouting(unittest.TestCase):
    QUOTA = "error: exit=1 stderr=You've hit your limit · resets Nov 8, 4am (Asia/Tokyo)"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.now = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
        self.path = Path(temp.name)
        patches = [
            patch.object(cooldown, "STATE_PATH", self.path / "cooldown.json"),
            patch.object(cooldown, "_utcnow", side_effect=lambda: self.now),
            patch.object(ap, "AI_CALL_LOG_PATH", self.path / "calls.jsonl"),
            patch.object(ap, "get_ai_cli_policy", return_value={"primary": "claude", "fallback": "codex"}),
            patch.object(ap, "datetime", wraps=datetime),
        ]
        for p in patches:
            obj = p.start()
            self.addCleanup(p.stop)
        ap.datetime.now.side_effect = lambda tz=None: self.now

    def block(self):
        cooldown.record_result(success=False, status=self.QUOTA, started_at=self.now)

    def logs(self):
        return [json.loads(row) for row in (self.path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_first_quota_attempt_next_task_skips_claude_and_logs_only_real_calls(self):
        with patch.object(ap, "_query_named_cli", side_effect=[
            (None, self.QUOTA), ("first", "ok"), ("second", "ok"),
        ]) as call:
            self.assertEqual(ap.query_ai_cli("one")[2], "Codex")
            second = ap.query_ai_cli("two", caller="evolution")
        self.assertEqual([c.args[0] for c in call.call_args_list], ["claude", "codex", "codex"])
        self.assertEqual(second[:3], ("second", "ok", "Codex"))
        self.assertIn("quota_cooldown", second[3])
        logs = self.logs()
        self.assertEqual([r["provider"] for r in logs], ["claude", "codex", "codex"])
        self.assertIn("2026-11-07T19:00:00", logs[-1]["fallback_reason"])
        self.assertEqual(logs[-1]["caller"], "evolution")

    def test_next_task_at_reset_returns_to_claude(self):
        self.block()
        self.now = datetime(2026, 11, 7, 19, 0, tzinfo=timezone.utc)
        with patch.object(ap, "_query_named_cli", return_value=("restored", "ok")) as call:
            result = ap.query_ai_cli("resume")
        self.assertEqual(result, ("restored", "ok", "Claude", ""))
        self.assertEqual(call.call_args.args[0], "claude")
        self.assertEqual(cooldown.cooldown_reason(), "")

    def test_if_reset_still_exhausted_record_new_reset(self):
        self.block()
        self.now = datetime(2026, 11, 7, 19, 0, tzinfo=timezone.utc)
        with patch.object(ap, "_query_named_cli", side_effect=[
            (None, "You've hit your limit · resets 5am (Asia/Tokyo)"), ("backup", "ok"),
        ]):
            self.assertEqual(ap.query_ai_cli("retry")[2], "Codex")
        self.assertIn("2026-11-07T20:00:00", cooldown.cooldown_reason())

    def test_failed_codex_during_cooldown_does_not_retry_claude(self):
        self.block()
        with patch.object(ap, "_query_named_cli", return_value=(None, "codex_timeout")) as call:
            output, status, provider, reason = ap.query_ai_cli("retry")
        self.assertIsNone(output)
        self.assertIn("codex_timeout", status)
        self.assertIn("quota_cooldown", reason)
        self.assertEqual(provider, "Codex")
        self.assertEqual([c.args[0] for c in call.call_args_list], ["codex"])

    def test_explicit_no_fallback_is_respected(self):
        self.block()
        with patch.object(ap, "get_ai_cli_policy", return_value={"primary": "claude", "fallback": "none"}), \
             patch.object(ap, "_query_named_cli") as call:
            result = ap.query_ai_cli("no backup", fallback_on_unavailable=True)
        call.assert_not_called()
        self.assertIsNone(result[0])
        self.assertIn("quota_cooldown", result[1])

    def test_cooldown_also_blocks_claude_when_configured_as_fallback(self):
        self.block()
        with patch.object(ap, "get_ai_cli_policy", return_value={"primary": "codex", "fallback": "claude"}), \
             patch.object(ap, "_query_named_cli", return_value=(None, "codex_timeout")) as call:
            result = ap.query_ai_cli("no quota")
        self.assertIsNone(result[0])
        self.assertIn("quota_cooldown", result[1])
        self.assertEqual([c.args[0] for c in call.call_args_list], ["codex"])
        self.assertEqual(len(self.logs()), 1)

    def test_network_or_auth_failure_does_not_create_quota_cooldown(self):
        for status in ("timeout", "error: OAuth token expired", "HTTP 503 unavailable"):
            with self.subTest(status=status), patch.object(ap, "_query_named_cli", side_effect=[
                (None, status), ("backup", "ok"), ("primary", "ok"),
            ]) as call:
                ap.query_ai_cli("first")
                self.assertEqual(ap.query_ai_cli("next")[2], "Claude")
            self.assertEqual([c.args[0] for c in call.call_args_list], ["claude", "codex", "claude"])


if __name__ == "__main__":
    raise SystemExit("Use python -m unittest discover -s tests -t . for test isolation.")
