"""2026-11-02: 自我进化 —— Codex 兜底开发、审批备注进开发指令、周上限跳过留痕、机器人摘要可操作."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import bot_gateway
import evolution as evo

AUTH = "Failed to authenticate. API Error: 401 OAuth access token has expired."


class EvoTmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.p = [patch.object(evo, "PROPOSALS", self.tmp / "proposals.json"),
                  patch.object(evo, "LOG", self.tmp / "log.jsonl")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def log_rows(self):
        f = self.tmp / "log.jsonl"
        return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


class FallbackTests(EvoTmp):
    def test_auth_failure_falls_back_to_codex(self):
        with patch.object(evo, "_claude_dev", return_value=(False, AUTH)), \
             patch.object(evo, "_codex_dev", return_value=(True, "done")) as cx:
            ok, out = evo._dev_with_fallback("EVO-X", "opus", self.tmp, "prompt")
        self.assertTrue(ok)
        self.assertIn("Codex 兜底", out)
        cx.assert_called_once()
        self.assertEqual(self.log_rows()[-1]["event"], "dev_fallback")

    def test_real_dev_failure_does_not_fall_back(self):
        with patch.object(evo, "_claude_dev", return_value=(False, "SyntaxError in edit")), \
             patch.object(evo, "_codex_dev") as cx:
            ok, _ = evo._dev_with_fallback("EVO-X", "opus", self.tmp, "prompt")
        self.assertFalse(ok)
        cx.assert_not_called()

    def test_quota_and_missing_cli_fall_back(self):
        self.assertTrue(evo._claude_unavailable("You've hit your limit · resets 4am"))
        self.assertTrue(evo._claude_unavailable("claude CLI 未安装"))
        self.assertFalse(evo._claude_unavailable("timeout"))


class PromptNotesTests(unittest.TestCase):
    def test_approval_note_reaches_dev_prompt(self):
        p = {"id": "EVO-1", "title": "t", "change": {"description": "改 paper_trader"},
             "history": [{"status": "approved", "by": "chat", "note": "【仅研究】不得修改下单行为"},
                         {"status": "developing", "by": "system", "note": ""}]}
        prompt = evo.build_dev_prompt(p)
        self.assertIn("【仅研究】不得修改下单行为", prompt)
        self.assertIn("优先于上面的提案描述", prompt)

    def test_no_notes_no_section(self):
        self.assertNotIn("范围限定", evo.build_dev_prompt({"id": "EVO-2", "history": []}))


class ProposeSkipTests(EvoTmp):
    def test_weekly_cap_skip_is_logged_with_pending(self):
        now = datetime.now(timezone.utc).isoformat()
        items = [{"id": f"EVO-T-{i}", "title": "x", "status": "proposed", "created_at": now}
                 for i in range(evo.WEEKLY_PROPOSAL_CAP)]
        (self.tmp / "proposals.json").write_text(json.dumps(items), encoding="utf-8")
        with patch.object(evo, "enabled", return_value=True):
            res = evo.propose(ai=lambda prompt: ("{}", "ok", "Codex", ""))
        self.assertEqual(res["skipped"], "weekly cap")
        last = self.log_rows()[-1]
        self.assertEqual(last["event"], "propose_skipped")
        self.assertEqual(len(last["pending"]), evo.WEEKLY_PROPOSAL_CAP)


class BotSummaryTests(unittest.TestCase):
    def test_propose_summary_asks_for_approval_with_titles(self):
        with patch.object(bot_gateway, "_evo_titles", return_value={"EVO-9": "止损重试"}):
            s = bot_gateway._s_evo({"event": "propose", "added": ["EVO-9"], "provider": "Codex"})
        self.assertIn("待你批准", s)
        self.assertIn("止损重试", s)

    def test_ready_for_review_asks_for_merge_confirmation(self):
        s = bot_gateway._s_evo({"event": "status", "id": "EVO-9", "status": "ready_for_review"})
        self.assertIn("待你确认合并", s)


if __name__ == "__main__":
    raise SystemExit("use: python -m unittest discover -s tests -t .")
