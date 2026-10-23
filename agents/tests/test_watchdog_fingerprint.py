"""2026-10-20 audit P1-4: watchdog 不能只看 PID.

锁文件首行保留裸 PID (run.bat 兼容), 后续行记录进程启动时间与镜像名.
watchdog 只有在 PID 存活且启动时间/镜像名与锁一致时才认为 orchestrator
健康; PID 被系统复用给别的进程时立即重启.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import _watchdog as wd


class _FakeNotifications:
    @staticmethod
    def notify_watchdog(*a, **k):
        pass


def _info(pid=4242, started="2026-10-20T03:00:00+00:00", image="python.exe"):
    return {"pid": pid, "started_utc": started, "image": image}


class LockFormat(unittest.TestCase):
    def test_write_and_read_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".orchestrator.lock"
            with patch.object(wd, "process_info", return_value=_info()):
                wd.write_lock_metadata(path, pid=4242)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "4242")
            meta = wd.read_lock(path)
            self.assertEqual(meta["pid"], 4242)
            self.assertEqual(meta["started_utc"], "2026-10-20T03:00:00+00:00")
            self.assertEqual(meta["image"], "python.exe")
            self.assertEqual(meta["script"], "orchestrator.py")

    def test_legacy_pid_only_lock_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".orchestrator.lock"
            path.write_text("4242\n", encoding="utf-8")
            self.assertEqual(wd.read_lock(path), {"pid": 4242})

    def test_unreadable_lock_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".orchestrator.lock"
            path.write_text("not-a-pid\n", encoding="utf-8")
            self.assertEqual(wd.read_lock(path), {})


class FingerprintMatch(unittest.TestCase):
    def test_matching_fingerprint(self):
        meta = {"pid": 4242, "started_utc": "2026-10-20T03:00:00+00:00",
                "image": "python.exe"}
        with patch.object(wd, "process_info", return_value=_info()):
            self.assertTrue(wd.lock_matches_process(meta))

    def test_recycled_pid_start_time_mismatch(self):
        meta = {"pid": 4242, "started_utc": "2026-10-20T02:00:00+00:00",
                "image": "python.exe"}
        with patch.object(wd, "process_info",
                          return_value=_info(started="2026-10-20T03:00:00+00:00")):
            self.assertFalse(wd.lock_matches_process(meta))

    def test_image_mismatch(self):
        meta = {"pid": 4242, "started_utc": "2026-10-20T03:00:00+00:00",
                "image": "other.exe"}
        with patch.object(wd, "process_info", return_value=_info()):
            self.assertFalse(wd.lock_matches_process(meta))

    def test_legacy_lock_alive_process_matches(self):
        with patch.object(wd, "process_info", return_value=_info()):
            self.assertTrue(wd.lock_matches_process({"pid": 4242}))

    def test_dead_process_never_matches(self):
        with patch.object(wd, "process_info", return_value=None):
            self.assertFalse(wd.lock_matches_process({"pid": 4242}))

    @unittest.skipUnless(os.name == "nt", "Windows process fingerprint")
    def test_current_process_fingerprint_readable(self):
        info = wd.process_info(os.getpid())
        self.assertIsNotNone(info)
        self.assertEqual(info["pid"], os.getpid())
        self.assertTrue(info["started_utc"])
        self.assertTrue(info["image"])


class WatchdogRestart(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lock = self.tmp / ".orchestrator.lock"
        self.log_path = self.tmp / "watchdog.jsonl"
        self.patches = [
            patch.object(wd, "LOCK_PATH", self.lock),
            patch.object(wd, "LOG_PATH", self.log_path),
            patch.object(wd, "_ensure_opend", return_value=0),
            patch.dict(sys.modules, {"notifications": _FakeNotifications}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _events(self):
        return [json.loads(line) for line in
                self.log_path.read_text(encoding="utf-8").splitlines()]

    def test_live_but_recycled_pid_is_restarted(self):
        self.lock.write_text(
            "4242\nstarted_utc=2026-10-20T02:00:00+00:00\nimage=python.exe\n",
            encoding="utf-8")
        with patch.object(wd, "process_info",
                          return_value=_info(started="2026-10-20T03:00:00+00:00")), \
             patch.object(wd, "_launch_orchestrator", return_value=5555) as launch:
            rc = wd.main()
        self.assertEqual(rc, 1)
        launch.assert_called_once()
        self.assertFalse(self.lock.exists())
        events = self._events()
        self.assertEqual(events[-1]["event"], "dead_restart")
        self.assertIn("fingerprint mismatch", events[-1]["msg"])

    def test_matching_lock_is_healthy(self):
        self.lock.write_text(
            "4242\nstarted_utc=2026-10-20T03:00:00+00:00\nimage=python.exe\n",
            encoding="utf-8")
        with patch.object(wd, "process_info", return_value=_info()), \
             patch.object(wd, "_launch_orchestrator") as launch:
            rc = wd.main()
        self.assertEqual(rc, 0)
        launch.assert_not_called()
        self.assertEqual(self._events()[-1]["event"], "healthy")

    def test_dead_pid_is_restarted(self):
        self.lock.write_text("4242\n", encoding="utf-8")
        with patch.object(wd, "process_info", return_value=None), \
             patch.object(wd, "_launch_orchestrator", return_value=5555):
            rc = wd.main()
        self.assertEqual(rc, 1)
        self.assertEqual(self._events()[-1]["event"], "dead_restart")


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
