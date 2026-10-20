"""WebUI 单实例 (2026-09-29 事故).

9/28 12:18 WebUI 一次响应慢 → watchdog 每 5 分钟判 "dead" 再拉一个; Windows 上
ThreadingHTTPServer 默认 SO_REUSEADDR 允许多个进程同绑 8080 → 叠出 5 个实例.

锁死:
- WebUI 服务端独占端口: 第二个实例 bind 失败并立即退出 (os._exit, 不被后台线程拖住).
- watchdog: 端口仍在监听但 health 失败 = "unresponsive", 不拉新实例;
  连续 UNRESPONSIVE_LIMIT 次才重启, 且先只结束监听 8080 的 webui.py 进程.
- 端口没人监听 = dead → 立即拉起 (原行为).
- health 成功重置计数.
- 测试不写生产日志 (LOG_PATH / STATE_PATH 指向临时目录).
"""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import _webui_watchdog as wd


class _FakeNotifications:
    """测试绝不发真实通知."""
    sent: list = []

    @staticmethod
    def send_alert(msg, level="info"):
        _FakeNotifications.sent.append(msg)


class ExclusiveServer(unittest.TestCase):
    def test_second_bind_on_same_port_fails(self):
        import webui
        from http.server import BaseHTTPRequestHandler
        first = webui._ExclusiveHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        try:
            port = first.server_address[1]
            with self.assertRaises(OSError):
                webui._ExclusiveHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
        finally:
            first.server_close()

    def test_no_reuse_address_on_windows(self):
        import webui
        with patch.object(webui.os, "name", "nt"):
            self.assertFalse(webui._reuse_address_allowed())

    def test_main_exits_hard_when_port_taken(self):
        import webui
        with patch.object(webui, "_ExclusiveHTTPServer", side_effect=OSError("in use")), \
             patch.object(webui.os, "_exit", side_effect=SystemExit(3)) as hard_exit:
            with self.assertRaises(SystemExit):
                webui.main()
        hard_exit.assert_called_once_with(3)


class WatchdogDecisions(unittest.TestCase):
    def setUp(self):
        td = Path(tempfile.mkdtemp())
        self.log = td / "wd.jsonl"
        self.state = td / "wd_state.json"
        self.p = [patch.object(wd, "LOG_PATH", self.log),
                  patch.object(wd, "STATE_PATH", self.state),
                  patch.object(wd, "_process_job_requests", lambda: None)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def events(self):
        return [json.loads(l)["event"] for l in self.log.read_text(encoding="utf-8").splitlines()]

    def run_main(self, alive, listening, pids=(111,)):
        with patch.object(wd, "_webui_alive", return_value=alive), \
             patch.object(wd, "_port_listening", return_value=listening), \
             patch.object(wd, "_webui_listener_pids", return_value=list(pids)), \
             patch.object(wd, "_kill_pid", return_value=True) as kill, \
             patch.object(wd, "_launch_webui", return_value=999) as launch, \
             patch.dict(sys.modules, {"notifications": _FakeNotifications}):
            rc = wd.main()
        return rc, kill, launch

    def test_healthy(self):
        rc, kill, launch = self.run_main(True, True)
        self.assertEqual(rc, 0)
        launch.assert_not_called()
        self.assertEqual(self.events(), ["healthy"])

    def test_dead_port_closed_relaunches(self):
        rc, kill, launch = self.run_main(False, False)
        launch.assert_called_once()
        kill.assert_not_called()
        self.assertEqual(self.events(), ["restart"])

    def test_unresponsive_does_not_spawn_duplicate(self):
        for _ in range(wd.UNRESPONSIVE_LIMIT - 1):
            rc, kill, launch = self.run_main(False, True)
            launch.assert_not_called()
            kill.assert_not_called()
        self.assertEqual(self.events(), ["unresponsive"] * (wd.UNRESPONSIVE_LIMIT - 1))

    def test_hung_long_enough_kills_listener_then_relaunches(self):
        for _ in range(wd.UNRESPONSIVE_LIMIT - 1):
            self.run_main(False, True)
        rc, kill, launch = self.run_main(False, True, pids=[111, 222])
        self.assertEqual([c.args[0] for c in kill.call_args_list], [111, 222])
        launch.assert_called_once()
        self.assertEqual(self.events()[-1], "hung_restart")

    def test_health_ok_resets_counter(self):
        for _ in range(wd.UNRESPONSIVE_LIMIT - 1):
            self.run_main(False, True)
        self.run_main(True, True)
        rc, kill, launch = self.run_main(False, True)
        launch.assert_not_called()      # counter restarted from 0

    def test_hung_but_no_webui_listener_found_does_not_launch(self):
        # 端口被非 webui 进程占用 → 不乱杀, 也不叠新实例
        for _ in range(wd.UNRESPONSIVE_LIMIT - 1):
            self.run_main(False, True)
        rc, kill, launch = self.run_main(False, True, pids=[])
        kill.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(self.events()[-1], "hung_no_webui_pid")


class HiddenSubprocesses(unittest.TestCase):
    """任务计划用 pythonw 跑 watchdog: 子进程必须带 CREATE_NO_WINDOW, 不闪控制台窗口."""

    def test_all_subprocess_calls_hidden_on_windows(self):
        import importlib
        with patch.object(wd.os, "name", "nt"):
            flags = {"creationflags": 0x08000000} if wd.os.name == "nt" else {}
        self.assertEqual(flags, {"creationflags": 0x08000000})
        calls = []
        def fake_run(*a, **kw):
            calls.append(kw)
            class R:
                stdout = "  TCP    127.0.0.1:8080   0.0.0.0:0   LISTENING   4242\n"
                returncode = 0
            return R()
        with patch.object(wd, "_HIDDEN", {"creationflags": 0x08000000}), \
             patch.object(wd.subprocess, "run", side_effect=fake_run):
            wd._webui_listener_pids()
            wd._kill_pid(4242)
        self.assertGreaterEqual(len(calls), 3)
        for kw in calls:
            self.assertEqual(kw.get("creationflags"), 0x08000000)


class PortProbe(unittest.TestCase):
    def test_port_listening(self):
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            self.assertTrue(wd._port_listening("127.0.0.1", srv.getsockname()[1]))

    def test_port_not_listening(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.assertFalse(wd._port_listening("127.0.0.1", port))


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
