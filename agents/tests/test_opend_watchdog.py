"""Tests for _opend_watchdog.py.

锁死:
- healthy path (process + port both up) → return 0, log 'healthy'
- process alive but port dead → return 3, log 'launched_waiting_login', 不重复启动
- process dead → 调用 _launch_opend → return 1, log 'process_dead_restart'
- exe 不存在 → 不 crash, return 4, log 'launch_failed'
- _port_open 拒绝连接时不抛异常
- _opend_process_alive 空 tasklist 返 None
"""
from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import _opend_watchdog as ow


class PortCheckTests(unittest.TestCase):

    def test_port_closed_returns_false(self):
        # 65535 通常没监听
        self.assertFalse(ow._port_open("127.0.0.1", 65535, timeout=0.5))

    def test_port_open_returns_true(self):
        # 启一个 listener 然后连
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            self.assertTrue(ow._port_open("127.0.0.1", port, timeout=1.0))


class ProcessDetectionTests(unittest.TestCase):

    def test_tasklist_empty_returns_none(self):
        fake_result = MagicMock(stdout="", returncode=0)
        with patch("subprocess.run", return_value=fake_result):
            self.assertIsNone(ow._opend_process_alive())

    def test_tasklist_finds_moomoo_opend(self):
        # tasklist /FO CSV /NH 输出: "image","pid","session","...","mem"
        csv = '"moomoo_OpenD.exe","12345","Console","1","500,000 K"'
        fake = MagicMock(stdout=csv, returncode=0)
        with patch("subprocess.run", return_value=fake):
            self.assertEqual(ow._opend_process_alive(), 12345)

    def test_tasklist_finds_futu_opend_legacy(self):
        # 老版 installer 用 FutuOpenD.exe
        csv = '"FutuOpenD.exe","67890","Console","1","200,000 K"'
        fake = MagicMock(stdout=csv, returncode=0)
        with patch("subprocess.run", return_value=fake):
            self.assertEqual(ow._opend_process_alive(), 67890)

    def test_tasklist_case_insensitive(self):
        csv = '"MOOMOO_OPEND.EXE","999","Console","1","x"'
        fake = MagicMock(stdout=csv, returncode=0)
        with patch("subprocess.run", return_value=fake):
            self.assertEqual(ow._opend_process_alive(), 999)

    def test_tasklist_exception_returns_none(self):
        with patch("subprocess.run", side_effect=Exception("no tasklist")):
            self.assertIsNone(ow._opend_process_alive())


class MainFlowTests(unittest.TestCase):
    """4 个状态: healthy / process-alive-port-dead / dead / exe-missing."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "opend_watchdog.jsonl"
        self._log_patch = patch.object(ow, "_LOG_PATH_UNUSED", None)  # placeholder for pattern
        # 直接 patch 模块的 LOG_PATH
        self._orig_log_path = ow.LOG_PATH
        ow.LOG_PATH = self.log_path

    def tearDown(self):
        ow.LOG_PATH = self._orig_log_path
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read_log(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(l) for l in self.log_path.read_text(encoding="utf-8").splitlines()]

    def test_healthy_path(self):
        with patch.object(ow, "_opend_process_alive", return_value=555), \
             patch.object(ow, "_port_open", return_value=True), \
             patch.object(ow, "_launch_opend") as fake_launch:
            rc = ow.main()
        self.assertEqual(rc, 0)
        self.assertEqual(fake_launch.call_count, 0, "healthy 时不该 launch")
        events = [e["event"] for e in self._read_log()]
        self.assertIn("healthy", events)

    def test_process_alive_but_port_dead_waits_no_relaunch(self):
        with patch.object(ow, "_opend_process_alive", return_value=777), \
             patch.object(ow, "_port_open", return_value=False), \
             patch.object(ow, "_launch_opend") as fake_launch:
            rc = ow.main()
        self.assertEqual(rc, 3)
        self.assertEqual(fake_launch.call_count, 0,
                         "process 在跑就不该重启, 避免多实例")
        events = [e["event"] for e in self._read_log()]
        self.assertIn("launched_waiting_login", events)

    def test_process_dead_triggers_launch(self):
        with patch.object(ow, "_opend_process_alive", return_value=None), \
             patch.object(ow, "_port_open", return_value=False), \
             patch.object(ow, "_launch_opend", return_value=8888) as fake_launch:
            rc = ow.main()
        self.assertEqual(rc, 1)
        self.assertEqual(fake_launch.call_count, 1)
        events = [e["event"] for e in self._read_log()]
        self.assertIn("process_dead_restart", events)

    def test_launch_failure_returns_4(self):
        with patch.object(ow, "_opend_process_alive", return_value=None), \
             patch.object(ow, "_port_open", return_value=False), \
             patch.object(ow, "_launch_opend", return_value=None):
            rc = ow.main()
        self.assertEqual(rc, 4)
        events = [e["event"] for e in self._read_log()]
        self.assertIn("launch_failed", events)


class LaunchExeTests(unittest.TestCase):

    def test_launch_fails_gracefully_when_exe_missing(self):
        with patch.object(ow, "OPEND_EXE", r"C:\does\not\exist.exe"):
            r = ow._launch_opend()
        self.assertIsNone(r)

    def test_launch_uses_detached_flags(self):
        # verify Popen 被以 detached 标志调用 (不阻塞父进程)
        fake_proc = MagicMock(pid=12321)
        with patch("_opend_watchdog.Path.exists", return_value=True), \
             patch("subprocess.Popen", return_value=fake_proc) as fake_popen:
            r = ow._launch_opend()
        self.assertEqual(r, 12321)
        self.assertEqual(fake_popen.call_count, 1)
        kwargs = fake_popen.call_args.kwargs
        # DETACHED=0x8 | NEW_GROUP=0x200 = 0x208
        self.assertEqual(kwargs.get("creationflags"), 0x208)


if __name__ == "__main__":
    unittest.main()
