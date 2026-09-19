"""F11 regression (audit 2026-09-19): AI 调用必须留下实际 model/provider audit log.

之前 ai_prompt.py 里注释说"默认按用户 codex CLI 全局设置", 但代码带
--ignore-user-config → 忽略 ~/.codex/config.toml. 实际用什么 model 无法从
注释/代码断言, 只能靠回执. 现在每次 query_ai_cli 后落 signals/ai_calls.jsonl.
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

import ai_prompt


class AICallAuditLogTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "ai_calls.jsonl"
        self._orig = ai_prompt._AI_CALL_LOG_PATH
        ai_prompt._AI_CALL_LOG_PATH = self.log_path

    def tearDown(self):
        ai_prompt._AI_CALL_LOG_PATH = self._orig
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read_log(self):
        if not self.log_path.exists():
            return []
        return [json.loads(l) for l in self.log_path.read_text(encoding="utf-8").splitlines()]

    def test_successful_primary_call_logs_metadata(self):
        with patch.object(ai_prompt, "get_ai_cli_policy",
                            return_value={"primary": "codex", "fallback": "none"}), \
             patch.object(ai_prompt, "_query_named_cli",
                            return_value=("real output", "ok")), \
             patch.object(ai_prompt, "_resolve_codex_model", return_value="gpt-5-codex"):
            r = ai_prompt.query_ai_cli("test prompt", complexity="medium")
        entries = self._read_log()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["provider"], "codex")
        self.assertEqual(e["model"], "gpt-5-codex")
        self.assertEqual(e["complexity"], "medium")
        self.assertEqual(e["status"], "ok")
        self.assertIn("duration_s", e)
        self.assertGreaterEqual(e["duration_s"], 0)

    def test_unset_model_logged_as_cli_internal_default(self):
        # 关键 F11 fix: 未设 CODEX_MODEL_* env 时不应显示为空/None,
        # 应显示 'cli_internal_default' 让人明白系统实际用的是 codex 打包默认
        with patch.object(ai_prompt, "get_ai_cli_policy",
                            return_value={"primary": "codex", "fallback": "none"}), \
             patch.object(ai_prompt, "_query_named_cli",
                            return_value=("out", "ok")), \
             patch.object(ai_prompt, "_resolve_codex_model", return_value=None):
            ai_prompt.query_ai_cli("x", complexity="simple")
        entries = self._read_log()
        self.assertEqual(entries[0]["model"], "cli_internal_default")

    def test_failed_call_still_logged(self):
        # 失败也应留 audit — 便于查看 CLI 挂了/超时
        with patch.object(ai_prompt, "get_ai_cli_policy",
                            return_value={"primary": "codex", "fallback": "none"}), \
             patch.object(ai_prompt, "_query_named_cli",
                            return_value=(None, "codex_not_installed")), \
             patch.object(ai_prompt, "_resolve_codex_model", return_value=None):
            r = ai_prompt.query_ai_cli("x", complexity="medium")
        entries = self._read_log()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "codex_not_installed")

    def test_fallback_call_logs_both_reason_and_final(self):
        # primary 失败 + fallback 成功 → 记录 fallback 那次 + fallback_reason
        call_count = {"n": 0}
        def _fake_named(name, prompt, timeout, ws, cx):
            call_count["n"] += 1
            if name == "codex":
                return (None, "codex_not_installed")
            return ("claude output", "ok")
        with patch.object(ai_prompt, "get_ai_cli_policy",
                            return_value={"primary": "codex", "fallback": "claude"}), \
             patch.object(ai_prompt, "_query_named_cli", side_effect=_fake_named), \
             patch.object(ai_prompt, "_resolve_codex_model", return_value=None):
            r = ai_prompt.query_ai_cli("x", complexity="complex")
        entries = self._read_log()
        # 期望 2 条 (primary fail 一次, fallback success 一次)
        self.assertEqual(len(entries), 2)
        # 第一条 primary 失败
        self.assertEqual(entries[0]["provider"], "codex")
        self.assertEqual(entries[0]["status"], "codex_not_installed")
        # 第二条 fallback 成功 + 带 fallback_reason
        self.assertEqual(entries[1]["provider"], "claude")
        self.assertEqual(entries[1]["status"], "ok")
        self.assertIn("codex:", entries[1]["fallback_reason"])


if __name__ == "__main__":
    unittest.main()
