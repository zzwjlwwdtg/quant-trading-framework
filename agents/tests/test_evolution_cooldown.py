"""Development shares the persisted Claude quota state with other AI callers."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import ai_cli_cooldown as cooldown
import evolution as evo


NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
RESET = datetime(2026, 11, 7, 19, 0, tzinfo=timezone.utc)
QUOTA = "You've hit your limit · resets Nov 8, 4am (Asia/Tokyo)"


class DevelopmentCooldown(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for target, name, value in (
            (cooldown, "STATE_PATH", self.tmp / "quota.json"),
            (evo, "HERE", self.tmp),
            (evo, "PROPOSALS", self.tmp / "proposals.json"),
            (evo, "LOG", self.tmp / "events.jsonl"),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        clock = patch.object(cooldown, "_utcnow", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        dt = patch.object(evo, "datetime", wraps=datetime)
        self.clock = dt.start()
        self.clock.now.return_value = NOW
        self.addCleanup(dt.stop)

    def rows(self):
        return [json.loads(line) for line in evo.LOG.read_text(encoding="utf-8").splitlines()]

    def test_first_quota_failure_makes_next_development_skip_claude(self):
        with patch.object(evo, "_claude_dev", return_value=(False, QUOTA)) as claude, \
             patch.object(evo, "_codex_dev", return_value=(True, "changed")) as codex, \
             patch.object(evo, "_save_dev_output") as output:
            first = evo._dev_with_fallback("EVO-1", "opus", self.tmp, "first")
            second = evo._dev_with_fallback("EVO-2", "sonnet", self.tmp, "second")
        self.assertTrue(first[0])
        self.assertTrue(second[0])
        claude.assert_called_once()
        self.assertEqual(codex.call_count, 2)
        self.assertEqual([r["claude_skipped"] for r in self.rows()], [False, True])
        self.assertIn("quota_cooldown", self.rows()[-1]["reason"])
        # A skipped invocation must not be recorded as a Claude failure/output.
        self.assertEqual([c.args[1] for c in output.call_args_list], ["claude", "codex", "codex"])

    def test_shared_quota_state_routes_directly_and_keeps_declared_files(self):
        cooldown.record_result(success=False, status=QUOTA, started_at=NOW, now=NOW)
        evo.save([{"id": "EVO-1", "change": {"files": ["agents/dashboard.html"]}}])
        with patch.object(evo, "_claude_dev") as claude, \
             patch.object(evo, "_codex_dev", return_value=(True, "changed")) as codex:
            ok, _ = evo._dev_with_fallback("EVO-1", "opus", self.tmp, "prompt")
        self.assertTrue(ok)
        claude.assert_not_called()
        codex.assert_called_once_with(self.tmp, "prompt", files=["agents/dashboard.html"])

    def test_reset_allows_claude_again_on_next_task(self):
        cooldown.record_result(success=False, status=QUOTA, started_at=NOW, now=NOW)
        self.clock.now.return_value = RESET
        with patch.object(cooldown, "_utcnow", return_value=RESET), \
             patch.object(evo, "_claude_dev", return_value=(True, "changed")) as claude, \
             patch.object(evo, "_codex_dev") as codex:
            ok, out = evo._dev_with_fallback("EVO-1", "opus", self.tmp, "prompt")
        self.assertTrue(ok)
        self.assertEqual(out, "changed")
        claude.assert_called_once()
        codex.assert_not_called()
        self.assertEqual(cooldown.cooldown_reason(now=RESET), "")

    def test_codex_failure_during_cooldown_remains_quota_retryable(self):
        cooldown.record_result(success=False, status=QUOTA, started_at=NOW, now=NOW)
        with patch.object(evo, "_claude_dev") as claude, \
             patch.object(evo, "_codex_dev", return_value=(False, "codex login expired")):
            ok, out = evo._dev_with_fallback("EVO-1", "opus", self.tmp, "prompt")
        self.assertFalse(ok)
        claude.assert_not_called()
        self.assertTrue(evo._quota_hit(out))
        self.assertIn("codex login expired", out)
        self.assertTrue(cooldown.cooldown_reason(now=NOW))
