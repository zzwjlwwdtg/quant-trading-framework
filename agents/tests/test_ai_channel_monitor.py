"""2026-11-02: AI 通道持续故障告警 + auth 分类 + 危机告警送达后才记去重."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import ai_channel_monitor as mon
import ai_prompt
import claude_gate
import notifications

NOW = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
AUTH = "error: exit=1 stderr=Failed to authenticate. API Error: 401 OAuth access token has expired."


def _row(minutes_ago, provider="claude", status=AUTH):
    return {"ts": (NOW - timedelta(minutes=minutes_ago)).isoformat(), "provider": provider,
            "status": status}


def _write(rows):
    ai_prompt.AI_CALL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ai_prompt.AI_CALL_LOG_PATH.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


class AuthClassTests(unittest.TestCase):
    def test_401_oauth_is_auth_not_unknown(self):
        self.assertEqual(claude_gate.classify_cli_failure(AUTH), "auth")
        self.assertIn("auth", claude_gate.FAILURE_CLASSES)

    def test_quota_still_quota(self):
        self.assertEqual(claude_gate.classify_cli_failure("You've hit your limit · resets 4am"), "quota")


class ChannelStatusTests(unittest.TestCase):
    def test_long_streak_is_failing(self):
        rows = [_row(200, status="ok")] + [_row(m) for m in (90, 60, 30, 5)]
        st = mon.channel_status(rows, "claude", NOW)
        self.assertEqual(st["state"], "failing")
        self.assertEqual(st["failure_class"], "auth")
        self.assertEqual(st["fail_count"], 4)

    def test_short_streak_is_only_degraded(self):
        st = mon.channel_status([_row(20), _row(10), _row(5)], "claude", NOW)
        self.assertEqual(st["state"], "degraded")

    def test_last_ok_means_ok(self):
        st = mon.channel_status([_row(90), _row(1, status="ok")], "claude", NOW)
        self.assertEqual(st["state"], "ok")

    def test_other_provider_ignored(self):
        st = mon.channel_status([_row(m, provider="codex", status="ok") for m in (90, 5)], "claude", NOW)
        self.assertEqual(st["state"], "no_data")


class TickTests(unittest.TestCase):
    def setUp(self):
        for p in (mon.STATE_PATH, notifications.ALERT_DEDUPE_PATH):
            try:
                p.unlink()
            except OSError:
                pass

    def test_alert_once_per_day_and_recovery(self):
        _write([_row(m) for m in (120, 90, 60, 30)] + [_row(10, provider="codex", status="ok")])
        sent = []
        ok = lambda msg, level, dedup: sent.append((level, msg)) or {"sent": [], "skipped": ["discord_no_config"]}
        with patch.object(notifications, "datetime", wraps=datetime):
            mon.tick(now=NOW, send=ok)
            mon.tick(now=NOW + timedelta(minutes=30), send=ok)
        self.assertEqual([lv for lv, _ in sent], ["warning"])
        self.assertIn("/login", sent[0][1])
        _write([_row(m) for m in (120, 90)] + [_row(-40, status="ok")])
        mon.tick(now=NOW + timedelta(minutes=60), send=ok)
        self.assertEqual([lv for lv, _ in sent], ["warning", "recovery"])

    def test_failed_delivery_is_retried(self):
        _write([_row(m) for m in (120, 90, 60, 30)])
        calls = []
        bad = lambda msg, level, dedup: calls.append(1) or {"sent": [], "skipped": ["telegram_failed"]}
        mon.tick(now=NOW, send=bad)
        mon.tick(now=NOW + timedelta(minutes=30), send=bad)
        self.assertEqual(len(calls), 2)


class CrisisDeliveryTests(unittest.TestCase):
    def setUp(self):
        try:
            notifications.ALERT_DEDUPE_PATH.unlink()
        except OSError:
            pass

    def test_failed_push_not_marked_sent(self):
        with patch.object(notifications, "send_alert",
                          return_value={"sent": [], "skipped": ["discord_failed"]}) as sa:
            notifications.notify_crisis("US.TEST", -6.0, reason="t")
            notifications.notify_crisis("US.TEST", -6.0, reason="t")
        self.assertEqual(sa.call_count, 2)

    def test_unconfigured_counts_as_delivered(self):
        with patch.object(notifications, "send_alert",
                          return_value={"sent": [], "skipped": ["discord_no_config", "telegram_no_config"]}) as sa:
            notifications.notify_crisis("US.TEST2", -6.0, reason="t")
            notifications.notify_crisis("US.TEST2", -6.0, reason="t")
        self.assertEqual(sa.call_count, 1)


if __name__ == "__main__":
    raise SystemExit("use: python -m unittest discover -s tests -t .")
