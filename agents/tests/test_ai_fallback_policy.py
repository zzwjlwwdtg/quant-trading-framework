"""2026-10-08 用户决定: 默认 Claude (Codex 需要经常换账号), Claude 失败或限额时退回 Codex."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import ai_prompt


class ClaudeFirstPolicy(unittest.TestCase):
    def test_all_launchers_claude_first(self):
        for f in ("_watchdog.py", "_webui_watchdog.py"):
            src = (AGENTS_DIR / f).read_text(encoding="utf-8")
            self.assertRegex(src, r'"AI_CLI_PRIMARY":\s+"claude"', f)
            self.assertRegex(src, r'"AI_CLI_FALLBACK":\s+"codex"', f)
        for bat in ("run.bat", "run_ja.bat", "snap.bat", "webui.bat"):
            t = (AGENTS_DIR / bat).read_text(encoding="utf-8")
            self.assertIn('set "AI_CLI_PRIMARY=claude"', t, bat)
            self.assertIn('set "AI_CLI_FALLBACK=codex"', t, bat)

    def test_code_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ai_prompt.get_ai_cli_policy(), {"primary": "claude", "fallback": "codex"})
        with patch.dict(os.environ, {"AI_CLI_PRIMARY": "codex", "AI_CLI_FALLBACK": "none"}, clear=True):
            self.assertEqual(ai_prompt.get_ai_cli_policy(), {"primary": "codex", "fallback": "none"})

    def test_claude_error_routes_to_codex(self):
        with patch.dict(os.environ, {"AI_CLI_PRIMARY": "claude", "AI_CLI_FALLBACK": "codex"}), \
             patch.object(ai_prompt, "_query_named_cli",
                          side_effect=[(None, "error: exit=1 stderr=usage limit reached"),
                                       ('{"verdict":"APPROVE"}', "ok")]), \
             patch.object(ai_prompt, "_log_ai_call"):
            out, status, provider, reason = ai_prompt.query_ai_cli("p", timeout=5)
        self.assertEqual((out, provider), ('{"verdict":"APPROVE"}', "Codex"))
        self.assertIn("claude", reason)

    def test_claude_ok_never_calls_codex(self):
        with patch.dict(os.environ, {"AI_CLI_PRIMARY": "claude", "AI_CLI_FALLBACK": "codex"}), \
             patch.object(ai_prompt, "_query_named_cli", return_value=("x", "ok")) as q, \
             patch.object(ai_prompt, "_log_ai_call"):
            self.assertEqual(ai_prompt.query_ai_cli("p")[2], "Claude")
            self.assertEqual(q.call_count, 1)


if __name__ == "__main__":
    unittest.main()
