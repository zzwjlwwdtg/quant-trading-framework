"""WP13 regression (audit 2026-09-19): /api/health 应该回答"程序活着 vs 模型在跑".

之前 health 只 report process/port state, audit 指出 "健康接口显示正在运行的源码/
config/model 版本; PID 存活不等于刷新成功". 加入 version + calibration + AI
freshness + invalidation freshness.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


class HealthPayloadShapeTests(unittest.TestCase):

    def test_health_has_all_wp13_required_keys(self):
        import webui
        r = webui.api_health()
        for key in ["orchestrator_pid", "orchestrator_alive", "opend_alive",
                     "version", "calibration", "ai_last_call_ts",
                     "last_invalidation", "log_age_min"]:
            self.assertIn(key, r, f"missing key {key}")

    def test_version_block_has_git_head_and_dirty_flag(self):
        import webui
        r = webui.api_health()
        v = r.get("version", {})
        self.assertIn("git_head", v)
        self.assertIn("is_dirty", v)
        # dirty flag 必须是 bool
        self.assertIsInstance(v["is_dirty"], bool)

    def test_calibration_block_shape(self):
        import webui
        r = webui.api_health()
        c = r.get("calibration", {})
        # 至少 exists 字段 (可能 True/False)
        self.assertIn("exists", c)
        if c.get("exists"):
            # 存在则应有 age_days + is_stale
            self.assertIn("age_days", c)
            self.assertIn("is_stale", c)


if __name__ == "__main__":
    unittest.main()
