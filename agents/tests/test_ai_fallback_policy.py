"""2026-10-08 用户决定: Codex 跑不通时由 Claude 辅助 (交易进程); 30 分钟公开快照仍只用 Codex."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import ai_prompt


class FallbackPolicy(unittest.TestCase):
    def test_orchestrator_env_enables_claude_fallback(self):
        src = (AGENTS_DIR / "_watchdog.py").read_text(encoding="utf-8")
        self.assertIn('"AI_CLI_FALLBACK":               "claude"', src)
        self.assertIn('"AI_CLI_PRIMARY":                "codex"', src)
        for bat in ("run.bat", "run_ja.bat"):
            self.assertIn('set "AI_CLI_FALLBACK=claude"', (AGENTS_DIR / bat).read_text(encoding="utf-8"))
        self.assertIn('set "AI_CLI_FALLBACK=none"', (AGENTS_DIR / "snap_public.bat").read_text(encoding="utf-8"))

    def test_codex_error_routes_to_claude(self):
        with patch.dict(os.environ, {"AI_CLI_PRIMARY": "codex", "AI_CLI_FALLBACK": "claude"}), \
             patch.object(ai_prompt, "_query_named_cli",
                          side_effect=[(None, "codex_error: exit=1 stderr=models cache unknown variant `max`"),
                                       ('{"verdict":"APPROVE"}', "ok")]), \
             patch.object(ai_prompt, "_log_ai_call"):
            out, status, provider, reason = ai_prompt.query_ai_cli("p", timeout=5)
        self.assertEqual((out, status, provider), ('{"verdict":"APPROVE"}', "ok", "Claude"))
        self.assertIn("codex", reason)

    def test_codex_ok_never_calls_claude(self):
        with patch.dict(os.environ, {"AI_CLI_PRIMARY": "codex", "AI_CLI_FALLBACK": "claude"}), \
             patch.object(ai_prompt, "_query_named_cli", return_value=("x", "ok")) as q, \
             patch.object(ai_prompt, "_log_ai_call"):
            self.assertEqual(ai_prompt.query_ai_cli("p")[2], "Codex")
            self.assertEqual(q.call_count, 1)


if __name__ == "__main__":
    unittest.main()
