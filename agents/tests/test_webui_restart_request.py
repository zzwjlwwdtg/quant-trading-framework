"""WebUI 文件触发重启 (2026-10-02).

用户要求不再通过控制电脑来重启 WebUI. 方案: 写入
signals/webui_restart_request.json, 任务计划每 5 分钟运行的 _webui_watchdog
发现后: 只结束监听 8080 且命令行含 webui.py 的进程 → 等端口释放 → 拉起新实例
→ 请求文件改名为 .done (一次请求只执行一次, 不会反复重启).
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

import _webui_watchdog as wd


class _FakeNotifications:
    @staticmethod
    def send_alert(*a, **k):
        pass


class RestartRequest(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.log = self.td / "wd.jsonl"
        self.req = self.td / "webui_restart_request.json"
        self.p = [patch.object(wd, "LOG_PATH", self.log),
                  patch.object(wd, "STATE_PATH", self.td / "st.json"),
                  patch.object(wd, "RESTART_REQUEST_PATH", self.req),
                  patch.object(wd.time, "sleep", lambda s: None),
                  patch.object(wd, "_process_job_requests", lambda: None)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def events(self):
        return [json.loads(l)["event"] for l in self.log.read_text(encoding="utf-8").splitlines()]

    def run_main(self, alive=True, listening=(True, False), pids=(111,)):
        seq = list(listening)
        def _listen(*a, **k):
            return seq.pop(0) if len(seq) > 1 else seq[0]
        with patch.object(wd, "_webui_alive", return_value=alive), \
             patch.object(wd, "_port_listening", side_effect=_listen), \
             patch.object(wd, "_webui_listener_pids", return_value=list(pids)), \
             patch.object(wd, "_kill_pid", return_value=True) as kill, \
             patch.object(wd, "_launch_webui", return_value=999) as launch, \
             patch.dict(sys.modules, {"notifications": _FakeNotifications}):
            rc = wd.main()
        return rc, kill, launch

    def test_no_request_keeps_old_behaviour(self):
        rc, kill, launch = self.run_main()
        launch.assert_not_called()
        self.assertEqual(self.events(), ["healthy"])

    def test_request_restarts_webui_once(self):
        self.req.write_text('{"reason": "deploy F12"}', encoding="utf-8")
        rc, kill, launch = self.run_main(listening=(True, False))
        self.assertEqual([c.args[0] for c in kill.call_args_list], [111])
        launch.assert_called_once()
        self.assertFalse(self.req.exists())
        self.assertTrue(self.req.with_suffix(".done").exists())
        self.assertEqual(self.events()[-3:], ["requested_restart_begin", "requested_restart_killed", "requested_restart"])
        # 下一轮: 请求已消费, 正常健康检查, 不再重启
        rc, kill, launch = self.run_main(listening=(True,))
        launch.assert_not_called()
        self.assertEqual(self.events()[-1], "healthy")

    def test_request_when_webui_not_running_just_launches(self):
        self.req.write_text("{}", encoding="utf-8")
        rc, kill, launch = self.run_main(alive=False, listening=(False,), pids=[])
        kill.assert_not_called()
        launch.assert_called_once()
        self.assertEqual(self.events()[-1], "requested_restart")

    def test_port_held_by_other_process_not_killed(self):
        self.req.write_text("{}", encoding="utf-8")
        rc, kill, launch = self.run_main(listening=(True,), pids=[])
        kill.assert_not_called()
        launch.assert_not_called()
        self.assertFalse(self.req.exists())
        self.assertEqual(self.events()[-1], "requested_restart_skipped")

    def test_port_not_released_no_second_instance(self):
        self.req.write_text("{}", encoding="utf-8")
        rc, kill, launch = self.run_main(listening=(True,), pids=[111])
        kill.assert_called_once()
        launch.assert_not_called()
        self.assertEqual(self.events()[-1], "requested_restart_failed")


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )


class ListenerPidsDecoding(unittest.TestCase):
    """2026-10-02 生产故障: pythonw -X utf8 下 netstat 输出是 cp932 (日文表头),
    text=True 解码在读线程里失败 → stdout=None → AttributeError, 重启请求中途崩溃.
    现在按字节读取并容错解码."""

    def test_cp932_netstat_output(self):
        netstat = ("\r\nアクティブな接続\r\n\r\n  プロトコル  ローカル アドレス  外部アドレス  状態  PID\r\n"
                   "  TCP  127.0.0.1:8080  0.0.0.0:0  LISTENING  4242\r\n"
                   "  TCP  127.0.0.1:18080  0.0.0.0:0  LISTENING  5555\r\n").encode("cp932")
        calls = []

        class R:
            def __init__(self, out):
                self.stdout, self.returncode = out, 0

        def fake_run(args, **kw):
            calls.append(kw)
            if args[0] == "netstat":
                return R(netstat)
            return R("C:\\Python312\\python.exe -X utf8 -u webui.py\r\n".encode("cp932"))

        with patch.object(wd.subprocess, "run", side_effect=fake_run):
            self.assertEqual(wd._webui_listener_pids(), [4242])
        self.assertTrue(all("text" not in kw for kw in calls))

    def test_stdout_none_does_not_crash(self):
        class R:
            stdout, returncode = None, 0
        with patch.object(wd.subprocess, "run", return_value=R()):
            self.assertEqual(wd._webui_listener_pids(), [])
