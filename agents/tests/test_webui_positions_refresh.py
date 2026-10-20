"""持仓刷新卡死修复 (2026-10-06).

现象: /api/positions 停在 10/3 的缓存 (refreshing=True 3 天), 10/5 系统新买的
MULL / DRAM / CBRS 在看板上看不到. 原因: 后台刷新线程卡在 moomoo 调用里,
标志永不复位; 且持仓查询不加 _TRADER_LOCK.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import webui
import paper_trader


class HungRefreshRecovery(unittest.TestCase):
    def test_new_refresh_after_hung_thread(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            (cache / "x.json").write_text(json.dumps({"v": "old"}), encoding="utf-8")
            import os
            old = time.time() - 3600
            os.utime(cache / "x.json", (old, old))
            block = threading.Event()
            calls = []
            def hung():
                calls.append("hung")
                block.wait(5)
                return {"v": "never"}
            def ok():
                calls.append("ok")
                return {"v": "new"}
            with patch.object(webui, "_WEBUI_CACHE_DIR", cache):
                webui._refresh_flag.pop("x", None)
                r1 = webui._cached("x", 60, hung)
                time.sleep(0.2)
                self.assertEqual(r1["v"], "old")
                self.assertTrue(webui._refresh_flag.get("x"))
                webui._cached("x", 60, ok)                     # 未超时: 不重复刷新
                time.sleep(0.2)
                self.assertEqual(calls, ["hung"])
                webui._refresh_started["x"] = time.time() - webui.HUNG_REFRESH_SEC - 1
                webui._cached("x", 60, ok)                     # 判定卡死 → 新线程
                for _ in range(20):
                    if "ok" in calls:
                        break
                    time.sleep(0.1)
                time.sleep(0.2)
                self.assertEqual(json.loads((cache / "x.json").read_text(encoding="utf-8"))["v"], "new")
                block.set()                                    # 让卡住的线程结束后再清理临时目录
                for _ in range(30):
                    if not webui._refresh_flag.get("x"):
                        break
                    time.sleep(0.1)
                time.sleep(0.2)


class MoomooQueryTimeout(unittest.TestCase):
    def test_timeout_returns_none_and_closes_ctx(self):
        closed = []
        with patch.object(paper_trader, "_ctx_close", lambda: closed.append(1)):
            r = webui._moomoo_query(lambda: time.sleep(2) or "late", timeout=0.2)
        self.assertIsNone(r)
        self.assertEqual(closed, [1])
        # 锁已释放, 下一次正常调用可用
        self.assertEqual(webui._moomoo_query(lambda: "ok", timeout=1), "ok")

    def test_lock_busy_returns_none(self):
        held = threading.Event()
        release = threading.Event()
        def holder():
            with paper_trader._TRADER_LOCK:
                held.set()
                release.wait(3)
        t = threading.Thread(target=holder)
        t.start()
        held.wait(1)
        try:
            self.assertIsNone(webui._moomoo_query(lambda: "x", timeout=0.2))
        finally:
            release.set()
            t.join()

    def test_dashboard_shows_stale_note(self):
        html = (AGENTS_DIR / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("未能刷新", html)


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
