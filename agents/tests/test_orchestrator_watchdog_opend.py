"""_watchdog.py 先守护 OpenD (2026-09-29).

任务计划 FSI-OrchestratorWatchdog 直接跑 _watchdog.py (不是 watchdog.bat),
所以 watchdog.bat 里的 _opend_watchdog.py 从未按计划运行 → OpenD 无自动恢复.

锁死:
- _watchdog.main() 先跑 OpenD 检查, 再检查 orchestrator (OpenD 先起, 顺序对).
- OpenD 检查异常不阻断 orchestrator 检查, 记 opend_check_failed.
- _opend_watchdog 的 tasklist 子进程带 CREATE_NO_WINDOW (任务用 pythonw, 否则每 30 分钟闪窗口).
- 测试只写临时日志.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import _watchdog as wd
import _opend_watchdog as ow


class _FakeNotifications:
    @staticmethod
    def notify_watchdog(*a, **k):
        pass


class OpenDCheckedFirst(unittest.TestCase):
    def setUp(self):
        td = Path(tempfile.mkdtemp())
        self.log = td / "watchdog.jsonl"
        self.lock = td / ".orchestrator.lock"
        self.lock.write_text("4242", encoding="utf-8")
        self.p = [patch.object(wd, "LOG_PATH", self.log),
                  patch.object(wd, "LOCK_PATH", self.lock),
                  patch.object(ow, "LOG_PATH", td / "opend.jsonl"),
                  patch.dict(sys.modules, {"notifications": _FakeNotifications})]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def events(self):
        return [json.loads(l)["event"] for l in self.log.read_text(encoding="utf-8").splitlines()]

    def test_opend_checked_before_orchestrator(self):
        order = []
        with patch.object(ow, "main", side_effect=lambda: order.append("opend") or 0), \
             patch.object(wd, "_pid_alive", side_effect=lambda pid: order.append("orch") or True), \
             patch.object(wd, "_launch_orchestrator") as launch:
            rc = wd.main()
        self.assertEqual(order, ["opend", "orch"])
        self.assertEqual(rc, 0)
        launch.assert_not_called()

    def test_opend_failure_does_not_block_orchestrator_check(self):
        with patch.object(ow, "main", side_effect=RuntimeError("boom")), \
             patch.object(wd, "_pid_alive", return_value=True):
            rc = wd.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self.events(), ["opend_check_failed", "healthy"])

    def test_orchestrator_dead_still_restarted_after_opend_check(self):
        with patch.object(ow, "main", return_value=0), \
             patch.object(wd, "_pid_alive", return_value=False), \
             patch.object(wd, "_launch_orchestrator", return_value=5555) as launch:
            rc = wd.main()
        launch.assert_called_once()
        self.assertEqual(rc, 1)
        self.assertEqual(self.events(), ["dead_restart"])


class TasklistHidden(unittest.TestCase):
    def test_tasklist_uses_no_window_flag(self):
        calls = []

        def fake_run(*a, **kw):
            calls.append(kw)
            class R:
                stdout = '"moomoo_OpenD.exe","75760","Console","1","100 K"\n'
                returncode = 0
            return R()
        with patch.object(ow, "_HIDDEN", {"creationflags": 0x08000000}), \
             patch.object(ow.subprocess, "run", side_effect=fake_run):
            pid = ow._opend_process_alive()
        self.assertEqual(pid, 75760)
        self.assertEqual(calls[0].get("creationflags"), 0x08000000)


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
