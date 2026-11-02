"""Windowless launch regression; never starts production jobs or services."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import _webui_watchdog as wd


class WindowlessLaunch(unittest.TestCase):
    def assert_background_options(self, kwargs):
        expected = 0x08000200 if os.name == "nt" else 0
        self.assertEqual(kwargs["creationflags"], expected)
        self.assertFalse(kwargs["creationflags"] & 0x00000008)  # DETACHED_PROCESS
        self.assertFalse(kwargs["creationflags"] & 0x00000010)  # CREATE_NEW_CONSOLE
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(kwargs.get("shell", False))
        self.assertTrue(kwargs["close_fds"])

    def test_all_batch_jobs_use_no_window_without_detached_process(self):
        for name, job in wd.JOBS.items():
            if name in ("market_radar_tick", "market_outlook_tick"):
                continue
            with self.subTest(name=name), patch.object(wd.subprocess, "Popen") as popen:
                popen.return_value.pid = 1234
                self.assertEqual(wd._launch_job(name), 1234)
                self.assertEqual(popen.call_args.args[0],
                                 ["cmd.exe", "/c", str(wd.SCRIPT_DIR / job["bat"])])
                self.assertEqual(popen.call_args.kwargs["cwd"], str(wd.SCRIPT_DIR))
                self.assert_background_options(popen.call_args.kwargs)

    def test_webui_recovery_is_windowless_and_preserves_environment(self):
        with patch.object(wd.subprocess, "Popen") as popen:
            popen.return_value.pid = 1235
            self.assertEqual(wd._launch_webui(), 1235)
            self.assertEqual(popen.call_args.args[0], ["cmd.exe", "/c", str(wd.WEBUI_BAT)])
            self.assert_background_options(popen.call_args.kwargs)
            for key, value in wd.WEBUI_ENV.items():
                self.assertEqual(popen.call_args.kwargs["env"][key], value)

    def test_python_workers_remain_shell_free_and_windowless(self):
        with tempfile.TemporaryDirectory(prefix="fsi-windowless-unit-") as directory:
            with patch.object(wd, "SCRIPT_DIR", Path(directory)):
                for name in ("market_radar_tick", "market_outlook_tick"):
                    with self.subTest(name=name), patch.object(wd.subprocess, "Popen") as popen:
                        popen.return_value.pid = 1236
                        self.assertEqual(wd._launch_job(name), 1236)
                        self.assertEqual(popen.call_args.args[0][0], sys.executable)
                        self.assertNotIn("cmd.exe", popen.call_args.args[0])
                        self.assert_background_options(popen.call_args.kwargs)

    @unittest.skipUnless(os.name == "nt", "Windows console inheritance acceptance")
    def test_real_batch_child_is_windowless_and_outlives_pythonw_launcher(self):
        # The launcher runs under pythonw just like Task Scheduler, then exits.
        # Only a temporary synthetic .bat is allowed; no live job/main() call.
        python = Path(sys.executable).resolve().with_name("python.exe")
        pythonw = python.with_name("pythonw.exe")
        self.assertTrue(pythonw.is_file())
        with tempfile.TemporaryDirectory(prefix="fsi windowless \u4e2d\u6587 ") as directory:
            root = Path(directory)
            (root / "probe.py").write_text(
                "import ctypes,json,os,sys,time\n"
                "from pathlib import Path\n"
                "time.sleep(0.6)\n"
                "ctypes.windll.kernel32.GetConsoleWindow.restype=ctypes.c_void_p\n"
                "hwnd=ctypes.windll.kernel32.GetConsoleWindow()\n"
                "visible=bool(hwnd and ctypes.windll.user32.IsWindowVisible(ctypes.c_void_p(hwnd)))\n"
                "Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'visible':visible}))\n",
                encoding="utf-8",
            )
            (root / "probe.bat").write_text(
                '@echo off\r\n"%FSI_PROBE_PYTHON%" "%~dp0probe.py" "%~dp0result.json"\r\n',
                encoding="ascii",
            )
            (root / "launcher.py").write_text(
                "import sys\nfrom pathlib import Path\n"
                f"sys.path.insert(0, {str(Path(wd.__file__).resolve().parent)!r})\n"
                "import _webui_watchdog as wd\n"
                "wd.SCRIPT_DIR=Path(__file__).parent\n"
                "wd.JOBS={'synthetic_probe':{'bat':'probe.bat'}}\n"
                "pid=wd._launch_job('synthetic_probe')\n"
                "if pid is None: raise SystemExit(2)\n"
                "(wd.SCRIPT_DIR/'child.pid').write_text(str(pid))\n",
                encoding="utf-8",
            )
            env = dict(os.environ, FSI_PROBE_PYTHON=str(python), PYTHONDONTWRITEBYTECODE="1")
            launch = subprocess.run([str(pythonw), str(root / "launcher.py")],
                                    env=env, cwd=root, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=0x08000000, timeout=10, check=False)
            self.assertEqual(launch.returncode, 0)
            deadline = time.monotonic() + 10
            while not (root / "result.json").exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertTrue((root / "result.json").is_file(), "worker did not survive launcher exit")
            result = json.loads((root / "result.json").read_text())
            self.assertFalse(result["visible"], "batch child created a visible console")
            # Allow cmd to finish after the probe file is closed, before cleanup.
            kernel = ctypes.windll.kernel32
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x00100000, False, int((root / "child.pid").read_text()))
            if handle:
                try:
                    self.assertEqual(kernel.WaitForSingleObject(ctypes.c_void_p(handle), 5000), 0)
                finally:
                    kernel.CloseHandle(ctypes.c_void_p(handle))
