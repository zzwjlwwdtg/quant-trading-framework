"""P0-suspect (静默失败类) guard tests (2026-09-05).

锁死 3 个 fail-loud 修法:
- S1: shadow log write fail 更新 health file + 定期 warn
- S2: FRED 拉不到时 thesis invalidation 显式 UNKNOWN 不能说"无触发"
- S3: send_alert 无 channel 时写 critical_alerts.jsonl + stderr echo
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class S1_ShadowLogHealthTests(unittest.TestCase):
    """S1: overnight_signal.log_shadow 失败时更新 health 状态."""

    def test_success_updates_health_ok(self):
        import overnight_signal
        with tempfile.TemporaryDirectory() as tmp:
            fake_log = Path(tmp) / "shadow.jsonl"
            fake_health = Path(tmp) / "health.json"
            with patch.object(overnight_signal, "_SHADOW_LOG", fake_log), \
                 patch.object(overnight_signal, "_HEALTH_PATH", fake_health):
                overnight_signal.log_shadow(
                    "US.TQQQ",
                    {"classification": "buy_dip", "why": [], "confidence": 3, "has_data": True},
                )
            h = json.loads(fake_health.read_text(encoding="utf-8"))
        self.assertGreater(h["last_success_ts"], 0)
        self.assertEqual(h["consecutive_fails"], 0)

    def test_write_fail_increments_consecutive_fails(self):
        import overnight_signal
        with tempfile.TemporaryDirectory() as tmp:
            fake_health = Path(tmp) / "health.json"
            # 让 append_jsonl 抛异常 + 让 fallback open 也失败 (path 是 dir)
            bad_log_dir = Path(tmp) / "bad_dir"
            bad_log_dir.mkdir()   # 是目录不是文件
            bad_log_path = bad_log_dir   # open("a") 目录会失败
            with patch.object(overnight_signal, "_SHADOW_LOG", bad_log_path), \
                 patch.object(overnight_signal, "_HEALTH_PATH", fake_health):
                for _ in range(3):
                    overnight_signal.log_shadow(
                        "US.TQQQ",
                        {"classification": "buy_dip", "why": [], "confidence": 3, "has_data": True},
                    )
            h = json.loads(fake_health.read_text(encoding="utf-8"))
        self.assertEqual(h["consecutive_fails"], 3)
        self.assertTrue(h["last_err"])   # 有错误信息

    def test_shadow_log_health_returns_healthy_ok(self):
        import overnight_signal
        import time as _t
        with tempfile.TemporaryDirectory() as tmp:
            fake_health = Path(tmp) / "health.json"
            fake_health.write_text(json.dumps({
                "last_success_ts": _t.time(),
                "consecutive_fails": 0,
            }), encoding="utf-8")
            with patch.object(overnight_signal, "_HEALTH_PATH", fake_health):
                h = overnight_signal.shadow_log_health()
        self.assertTrue(h["healthy"])
        self.assertEqual(h["status"], "ok")

    def test_shadow_log_health_returns_degraded_on_stale(self):
        import overnight_signal
        with tempfile.TemporaryDirectory() as tmp:
            fake_health = Path(tmp) / "health.json"
            # 超过 24h 无 success
            fake_health.write_text(json.dumps({
                "last_success_ts": 1000000000.0,   # 很老的时间
                "consecutive_fails": 50,
                "last_err": "test fail",
            }), encoding="utf-8")
            with patch.object(overnight_signal, "_HEALTH_PATH", fake_health):
                h = overnight_signal.shadow_log_health()
        self.assertFalse(h["healthy"])
        self.assertEqual(h["status"], "degraded")
        self.assertGreater(h["age_hours_since_success"], 24)


class S2_ThesisInvalidationDataUnavailableTests(unittest.TestCase):
    """S2: FRED fail 时 thesis invalidation 输出必须显式 UNKNOWN.

    Source-level test: 断言脚本里含 'DATA UNAVAILABLE' 或 'unavailable'
    的显式 branch, 而非只有 '无触发'."""

    def test_source_has_data_unavailable_branch(self):
        import inspect, _check_thesis_invalidation as m
        src = inspect.getsource(m)
        # 关键: 必须有区分 all_unavailable 的 branch
        self.assertIn("all_unavailable", src,
                      "S2 regressed: 未区分 all-data-unavailable case")
        self.assertIn("DATA UNAVAILABLE", src,
                      "S2 regressed: 缺少 DATA UNAVAILABLE 显式输出")
        # 且必须有 data_status 追踪
        self.assertIn("data_status", src,
                      "S2 regressed: 缺少 per-metric data_status 追踪")


class S3_SendAlertFallbackTests(unittest.TestCase):
    """S3: send_alert 无 channel 或全 fail 时, 高 level 消息必须写 critical_alerts.jsonl."""

    def test_no_channel_configured_writes_critical_alerts_for_crisis(self):
        import notifications
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(notifications, "SIGNALS_DIR", tmp), \
                 patch.object(notifications, "_cfg", return_value=""):
                r = notifications.send_alert("test crisis msg", level="crisis", dedup=False)
            # assertions **inside** with block — tempdir 出块就删
            self.assertEqual(r["sent"], [])
            crit_path = Path(tmp) / "critical_alerts.jsonl"
            self.assertTrue(crit_path.exists(),
                            "S3 regressed: 无 channel 时未写 critical_alerts.jsonl")
            entry = json.loads(crit_path.read_text(encoding="utf-8").strip())
            self.assertEqual(entry["level"], "crisis")
            self.assertIn("test crisis msg", entry["msg"])
            self.assertEqual(r.get("fallback"), "critical_alerts.jsonl")

    def test_no_channel_info_level_skips_fallback(self):
        """info 级别不写 critical_alerts (避免刷屏, 只关键 level 才 fallback)."""
        import notifications
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(notifications, "SIGNALS_DIR", tmp), \
                 patch.object(notifications, "_cfg", return_value=""):
                notifications.send_alert("just info", level="info", dedup=False)
            crit_path = Path(tmp) / "critical_alerts.jsonl"
            self.assertFalse(crit_path.exists(),
                             "S3 regressed: info 级别不该 fallback (只 crisis/warning/error/trade 才)")


if __name__ == "__main__":
    unittest.main()
