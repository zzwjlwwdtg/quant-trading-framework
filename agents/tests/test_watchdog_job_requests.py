"""白名单后台任务的文件触发 (2026-10-02).

写 signals/job_request_<名字>.json → WebUI 看门狗下一轮 (≤5 分钟) 以隐藏窗口
后台启动白名单里对应的 .bat, 不等待结束. 只认白名单名字; 同一任务正在运行
(.running 未过期) 时不重复启动; 请求文件改名 .done, 一次请求只执行一次.
看门狗本身的 WebUI 健康检查照常进行.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import _webui_watchdog as wd


class JobRequests(unittest.TestCase):
    def test_news_job_uses_python_without_shell_and_cannot_be_suppressed_by_old_four_hour_marker(self):
        import os
        import subprocess
        (self.td / "logs").mkdir()
        with patch.object(wd, "SCRIPT_DIR", self.td), patch.object(subprocess, "Popen") as popen:
            popen.return_value.pid = 5432
            self.assertEqual(wd._launch_job("market_radar_tick"), 5432)
            args = popen.call_args.args[0]
            self.assertEqual(args[0], sys.executable)
            self.assertEqual(args[-1], "tick")
            self.assertNotIn("cmd.exe", args)
        (self.td / "job_request_market_radar_tick.json").write_text("{}", encoding="utf-8")
        marker = self.td / "job_market_radar_tick.running"
        marker.write_text("old", encoding="utf-8")
        old = time.time() - 31 * 60
        os.utime(marker, (old, old))
        _, launch = self.run_main()
        launch.assert_called_once_with("market_radar_tick")

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.log = self.td / "wd.jsonl"
        self.p = [patch.object(wd, "LOG_PATH", self.log),
                  patch.object(wd, "STATE_PATH", self.td / "st.json"),
                  patch.object(wd, "RESTART_REQUEST_PATH", self.td / "rr.json"),
                  patch.object(wd, "JOB_DIR", self.td),
                  patch.object(wd, "_in_us_market_window", return_value=False)]
        for x in self.p:
            x.start()
        # 自动周跑: 默认视为刚启动过, 只测请求文件路径
        for name in wd.JOBS:
            (self.td / f"job_{name}.last_started").write_text("x", encoding="utf-8")

    def tearDown(self):
        for x in self.p:
            x.stop()

    def events(self):
        if not self.log.exists():
            return []
        return [json.loads(l)["event"] for l in self.log.read_text(encoding="utf-8").splitlines()]

    def run_main(self):
        with patch.object(wd, "_webui_alive", return_value=True), \
             patch.object(wd, "_launch_job", return_value=4321) as launch:
            rc = wd.main()
        return rc, launch

    def test_whitelisted_job_started_once(self):
        (self.td / "job_request_eps_growth_backtest.json").write_text("{}", encoding="utf-8")
        rc, launch = self.run_main()
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[0], "eps_growth_backtest")
        self.assertTrue((self.td / "job_request_eps_growth_backtest.done").exists())
        self.assertTrue((self.td / "job_eps_growth_backtest.running").exists())
        self.assertIn("job_started", self.events())
        self.assertIn("healthy", self.events())         # WebUI 检查照常
        rc, launch = self.run_main()
        launch.assert_not_called()

    def test_unknown_job_rejected(self):
        (self.td / "job_request_rm_rf.json").write_text("{}", encoding="utf-8")
        rc, launch = self.run_main()
        launch.assert_not_called()
        self.assertIn("job_rejected", self.events())
        self.assertFalse((self.td / "job_request_rm_rf.json").exists())

    def test_running_job_not_duplicated_until_stale(self):
        (self.td / "job_eps_growth_backtest.running").write_text("123", encoding="utf-8")
        (self.td / "job_request_eps_growth_backtest.json").write_text("{}", encoding="utf-8")
        rc, launch = self.run_main()
        launch.assert_not_called()
        self.assertIn("job_already_running", self.events())
        # 2026-10-13: 请求不再被吞掉, 保留到任务结束后再启动
        self.assertTrue((self.td / "job_request_eps_growth_backtest.json").exists())
        old = time.time() - wd.JOB_STALE_HOURS * 3600 - 60
        import os
        os.utime(self.td / "job_eps_growth_backtest.running", (old, old))
        (self.td / "job_request_eps_growth_backtest.json").write_text("{}", encoding="utf-8")
        rc, launch = self.run_main()
        launch.assert_called_once()

    def test_whitelist_points_to_existing_bat(self):
        for name, job in wd.JOBS.items():
            self.assertTrue((AGENTS_DIR / job["bat"]).exists(), name)
            self.assertTrue(job["bat"].endswith(".bat"))

    def test_market_hours_defers_without_consuming(self):
        req = self.td / "job_request_eps_growth_backtest.json"
        req.write_text("{}", encoding="utf-8")
        with patch.object(wd, "_in_us_market_window", return_value=True):
            rc, launch = self.run_main()
        launch.assert_not_called()
        self.assertTrue(req.exists())
        rc, launch = self.run_main()                    # 收盘后
        launch.assert_called_once()


    def test_weekly_auto_run(self):
        import os
        last = self.td / "job_eps_growth_backtest.last_started"
        rc, launch = self.run_main()
        launch.assert_not_called()                      # 刚跑过 → 不自动
        old = time.time() - 8 * 86400
        os.utime(last, (old, old))
        rc, launch = self.run_main()
        launch.assert_called_once()                     # 超过 7 天 → 自动启动
        self.assertIn("job_auto_requested", self.events())
        self.assertGreater(last.stat().st_mtime, old + 86400)
        (self.td / "job_eps_growth_backtest.running").unlink()
        rc, launch = self.run_main()
        launch.assert_not_called()                      # 刚启动 → 不重复

    def test_auto_run_waits_for_market_close(self):
        import os
        last = self.td / "job_eps_growth_backtest.last_started"
        old = time.time() - 8 * 86400
        os.utime(last, (old, old))
        with patch.object(wd, "_in_us_market_window", return_value=True):
            rc, launch = self.run_main()
        launch.assert_not_called()
        self.assertTrue((self.td / "job_request_eps_growth_backtest.json").exists())


class MarketWindow(unittest.TestCase):
    def test_market_window_clock(self):
        from datetime import datetime, timezone
        f = wd._in_us_market_window
        self.assertTrue(f(datetime(2026, 10, 2, 13, 30, tzinfo=timezone.utc)))    # 周五盘中
        self.assertFalse(f(datetime(2026, 10, 2, 21, 5, tzinfo=timezone.utc)))    # 收盘后
        self.assertFalse(f(datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)))    # 周六


if __name__ == "__main__":
    unittest.main()
