"""Quota persistence regression cases; all state belongs to per-test temp dirs."""
import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import ai_cli_cooldown as cd


class ClaudeQuotaCooldown(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "quota.json"
        redirect = patch.object(cd, "STATE_PATH", self.path)
        redirect.start()
        self.addCleanup(redirect.stop)
        self.now = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
        self.quota = "You've hit your limit · resets Nov 8, 4am (Asia/Tokyo)"

    def record(self, status=None, **kwargs):
        cd.record_result(success=False, status=status or self.quota,
                         started_at=kwargs.pop("started_at", self.now),
                         now=kwargs.pop("now", self.now), **kwargs)

    def state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_reported_weekly_reset_persists_until_exact_utc_instant(self):
        self.record()
        reset = datetime(2026, 11, 7, 19, 0, tzinfo=timezone.utc)
        self.assertEqual(self.state()["until"], reset.isoformat())
        self.assertEqual(self.state()["source"], "provider_reset")
        self.assertTrue(cd.cooldown_reason(now=reset - timedelta(microseconds=1)).startswith("quota_cooldown:"))
        self.assertEqual(cd.cooldown_reason(now=reset), "")
        self.assertEqual(cd.cooldown_reason(now=reset + timedelta(days=1)), "")
        # Reads never delete or rewrite expired state.
        self.assertEqual(self.state()["until"], reset.isoformat())

    def test_new_process_style_read_uses_file_not_memory(self):
        self.record()
        persisted = self.path.read_bytes()
        replacement = Path(self.tmp.name) / "other.json"
        replacement.write_bytes(persisted)
        with patch.object(cd, "STATE_PATH", replacement):
            self.assertIn("2026-11-07T19:00:00+00:00", cd.cooldown_reason(now=self.now))

    def test_time_only_reset_uses_reported_timezone(self):
        self.record("usage limit reached; resets 4am (Asia/Tokyo)")
        self.assertEqual(self.state()["until"], "2026-11-06T19:00:00+00:00")

    def test_time_only_reset_rolls_to_next_local_day(self):
        self.record("usage limit reached; resets 11pm (Asia/Tokyo)")
        self.assertEqual(self.state()["until"], "2026-11-07T14:00:00+00:00")

    def test_year_rollover_and_explicit_year(self):
        now = datetime(2026, 12, 31, 12, 0, tzinfo=timezone.utc)
        for hint in ("Jan 1, 4am (Asia/Tokyo)", "Jan 1, 2027, 4am (Asia/Tokyo)"):
            with self.subTest(hint=hint):
                self.assertEqual(cd._parse_reset("quota exceeded; resets " + hint, now),
                                 datetime(2026, 12, 31, 19, 0, tzinfo=timezone.utc))

    def test_offset_and_relative_resets(self):
        for hint, expected in (
            ("2026-11-08T04:00:00+09:00", "2026-11-07T19:00:00+00:00"),
            ("2026-11-07T19:00Z", "2026-11-07T19:00:00+00:00"),
            ("in 1h 30m", "2026-11-06T16:30:00+00:00"),
            ("in 15 minutes", "2026-11-06T15:15:00+00:00"),
        ):
            with self.subTest(hint=hint):
                self.assertEqual(cd._parse_reset("quota; resets " + hint, self.now).isoformat(), expected)

    def test_unparseable_reset_uses_short_backoff_not_weekly_guess(self):
        for status in ("quota exceeded", "rate limit; resets 4am",
                       "quota resets Nov 8, 99am (Asia/Tokyo)",
                       "quota resets Nov 5, 4am (Asia/Tokyo)",
                       "quota resets Dec 8, 4am (Asia/Tokyo)",
                       "quota resets Nov 8, 4am (Not/AZone)"):
            with self.subTest(status=status):
                self.record(status)
                self.assertEqual(self.state()["source"], "retry_backoff")
                self.assertEqual(self.state()["until"], (self.now + timedelta(minutes=15)).isoformat())

    def test_other_failure_classes_do_not_create_cooldown(self):
        for status in ("401 unauthorized", "network timeout", "billing unavailable",
                       "resets connection", "protocol error", "max tokens exceeded", "http 500"):
            with self.subTest(status=status):
                self.record(status)
                self.assertFalse(self.path.exists())
                self.assertEqual(cd.cooldown_reason(now=self.now), "")

    def test_rate_limit_is_short_backoff(self):
        for status in ("HTTP 429", "error: 429", "too many requests", "rate_limit_exceeded", "insufficient_quota"):
            with self.subTest(status=status):
                self.record(status)
                self.assertEqual(self.state()["source"], "retry_backoff")

    def test_success_after_reset_clears_state(self):
        self.record()
        later = self.now + timedelta(days=2)
        cd.record_result(success=True, status="ok", started_at=later, now=later)
        self.assertIsNone(self.state()["until"])
        self.assertEqual(cd.cooldown_reason(now=later), "")

    def test_late_success_from_before_quota_observation_cannot_clear(self):
        observed = self.now + timedelta(minutes=1)
        self.record(now=observed)
        cd.record_result(success=True, status="ok", started_at=self.now,
                         now=observed + timedelta(minutes=1))
        self.assertIn("provider_reset", cd.cooldown_reason(now=observed))

    def test_old_quota_result_cannot_undo_new_success(self):
        later = self.now + timedelta(minutes=1)
        cd.record_result(success=True, status="ok", started_at=later, now=later)
        self.record(started_at=self.now, now=later + timedelta(minutes=1))
        self.assertIsNone(self.state()["until"])

    def test_older_observation_and_nonquota_failure_preserve_state(self):
        later = self.now + timedelta(minutes=1)
        self.record(started_at=later, now=later)
        before = self.path.read_bytes()
        self.record(now=self.now)
        self.record("network timeout", started_at=later, now=later + timedelta(minutes=1))
        self.assertEqual(self.path.read_bytes(), before)

    def test_generic_parallel_failure_cannot_shorten_known_weekly_reset(self):
        self.record()
        later = self.now + timedelta(seconds=1)
        self.record("HTTP 429", started_at=later, now=later)
        self.assertEqual(self.state()["until"], "2026-11-07T19:00:00+00:00")
        self.assertEqual(self.state()["source"], "provider_reset")

    def test_missing_corrupt_and_malformed_state_allow_normal_retry(self):
        self.assertEqual(cd.cooldown_reason(now=self.now), "")
        for content in ("not json", "[]", "null", '{"version": 99}',
                        '{"version":1,"started_at":"x"}'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                self.assertEqual(cd.cooldown_reason(now=self.now), "")
        self.record()
        self.assertTrue(cd.cooldown_reason(now=self.now))

    def test_unknown_or_far_future_state_is_not_trusted(self):
        self.record()
        for field, value in (("until", "2027-12-01T00:00:00+00:00"),
                             ("until", "2026-11-07T19:00:00"),
                             ("source", "untrusted")):
            state = self.state()
            state[field] = value
            self.path.write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(cd.cooldown_reason(now=self.now), "")
            self.record()

    def test_raw_errors_and_credentials_are_never_persisted(self):
        self.record(self.quota + " ANTHROPIC_API_KEY=private-test-secret")
        content = self.path.read_text(encoding="utf-8")
        self.assertNotIn("private-test-secret", content)
        self.assertNotIn("ANTHROPIC", content)
        self.assertNotIn("You've", content)

    def test_io_failure_does_not_escape(self):
        with patch.object(cd, "with_file_lock", side_effect=PermissionError):
            self.record()
        with patch.object(cd, "atomic_write_json", side_effect=OSError):
            self.record()
        self.assertFalse(self.path.exists())

    def test_concurrent_results_keep_latest_started_call(self):
        def write(index):
            at = self.now + timedelta(seconds=index)
            cd.record_result(success=False, status=self.quota, started_at=at, now=at)
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(write, reversed(range(12))))
        self.assertEqual(self.state()["started_at"], (self.now + timedelta(seconds=11)).isoformat())
        self.assertTrue(cd.cooldown_reason(now=self.now + timedelta(minutes=1)))

    def test_separate_processes_share_atomic_state(self):
        # This imports only the pure quota module, never a test script or config.
        script = """
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import ai_cli_cooldown as cd
cd.STATE_PATH = Path(sys.argv[2])
at = datetime.fromisoformat(sys.argv[3])
cd.record_result(success=False, status=sys.argv[4], started_at=at, now=at)
"""
        processes = [subprocess.Popen(
            [sys.executable, "-c", script, str(Path(cd.__file__).parent), str(self.path),
             (self.now + timedelta(seconds=index)).isoformat(), self.quota],
            cwd=self.tmp.name, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ) for index in (2, 0, 1)]
        for process in processes:
            out, err = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, (out, err))
        self.assertEqual(self.state()["started_at"], (self.now + timedelta(seconds=2)).isoformat())
        self.assertIn("provider_reset", cd.cooldown_reason(now=self.now + timedelta(minutes=1)))

    def test_clock_is_injectable_and_naive_results_are_ignored(self):
        with patch.object(cd, "_utcnow", return_value=self.now):
            cd.record_result(success=False, status=self.quota, started_at=self.now)
            self.assertTrue(cd.cooldown_reason())
        before = self.path.read_bytes()
        cd.record_result(success=True, status="ok", started_at=self.now.replace(tzinfo=None), now=self.now)
        self.assertEqual(self.path.read_bytes(), before)
