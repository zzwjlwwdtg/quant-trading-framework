"""Tests for _backtest_reviewer.py (2026-09-09).

锁死:
- checklist 里 8 类 category 都存在 (regression 别人 refactor 时不能悄悄丢)
- _extract_json 能处理直接 JSON / code fence / 嵌入 text
- audit_script 在 AI CLI 缺失时不 crash, 返 UNKNOWN verdict
- _save_report 正确生成 markdown 报告
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import _backtest_reviewer as br


class ChecklistCompletenessTests(unittest.TestCase):
    """8 类 category 必须都在 prompt 里 (regression: 有人重构 checklist 时保护)."""

    def test_all_8_categories_present(self):
        required = ["DATA_LEAKAGE", "LOOK_AHEAD", "WALK_FORWARD_STRICTNESS",
                    "MULTIPLE_TESTING", "SURVIVOR_BIAS", "TRANSACTION_COST",
                    "IN_SAMPLE_TUNING", "SAMPLE_SIZE"]
        for cat in required:
            self.assertIn(cat, br._CHECKLIST,
                          f"缺少 category {cat} 在 _CHECKLIST 里 (regression)")

    def test_verdict_options_are_defined(self):
        for v in ["PASS", "PASS_WITH_CAVEATS", "SUSPICIOUS", "REJECT"]:
            self.assertIn(v, br._CHECKLIST)


class JsonExtractionTests(unittest.TestCase):

    def test_direct_json(self):
        r = br._extract_json('{"verdict": "PASS", "findings": []}')
        self.assertEqual(r["verdict"], "PASS")

    def test_json_in_code_fence(self):
        text = '这里是解释文字\n```json\n{"verdict": "REJECT"}\n```\n后续文字'
        r = br._extract_json(text)
        self.assertEqual(r["verdict"], "REJECT")

    def test_json_embedded_in_text(self):
        text = '好的, 我给你 JSON: {"verdict":"SUSPICIOUS","summary":"x"}. 完毕.'
        r = br._extract_json(text)
        self.assertEqual(r["verdict"], "SUSPICIOUS")

    def test_no_json_returns_none(self):
        self.assertIsNone(br._extract_json("just a plain sentence"))
        self.assertIsNone(br._extract_json(""))
        self.assertIsNone(br._extract_json(None))


class AuditScriptTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.script = Path(self.tmpdir) / "_backtest_dummy.py"
        self.script.write_text("# dummy backtest\nprint('hi')\n", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ai_cli_success_returns_parsed_verdict(self):
        fake_module = MagicMock()
        fake_module.query_ai_cli = MagicMock(return_value=(
            '{"verdict": "PASS", "summary": "clean", "findings": []}',
            "ok", "Codex", "",
        ))
        with patch.dict(sys.modules, {"ai_prompt": fake_module}):
            r = br.audit_script(self.script)
        self.assertEqual(r["verdict"], "PASS")
        self.assertEqual(r["summary"], "clean")

    def test_ai_cli_failure_returns_unknown(self):
        fake_module = MagicMock()
        fake_module.query_ai_cli = MagicMock(side_effect=Exception("no cli"))
        with patch.dict(sys.modules, {"ai_prompt": fake_module}):
            r = br.audit_script(self.script)
        self.assertEqual(r["verdict"], "UNKNOWN")
        self.assertIn("no cli", r["summary"])

    def test_unreadable_script_returns_unknown_no_crash(self):
        bad_path = Path(self.tmpdir) / "_backtest_missing.py"
        r = br.audit_script(bad_path)   # file doesn't exist
        self.assertEqual(r["verdict"], "UNKNOWN")

    def test_ai_returns_junk_still_gets_report(self):
        """CLI 返 non-JSON gibberish 也不 crash, verdict fallback."""
        fake_module = MagicMock()
        fake_module.query_ai_cli = MagicMock(return_value=(
            "some non-json output text",
            "ok", "Codex", "",
        ))
        with patch.dict(sys.modules, {"ai_prompt": fake_module}):
            r = br.audit_script(self.script)
        # verdict fallback UNKNOWN (JSON 解析失败)
        self.assertEqual(r["verdict"], "UNKNOWN")


class SaveReportTests(unittest.TestCase):

    def test_save_report_writes_markdown(self):
        report = {
            "script":     "_backtest_dummy",
            "verdict":    "PASS_WITH_CAVEATS",
            "audited_at": "2026-09-09T00:00:00Z",
            "summary":    "test summary",
            "findings":   [{"severity": "medium", "category": "DATA_LEAKAGE",
                             "line": 42, "description": "x", "fix_suggestion": "y"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(br, "_AUDIT_DIR", Path(tmp)):
                p = br._save_report(report)
            self.assertTrue(p.exists())
            content = p.read_text(encoding="utf-8")
            self.assertIn("PASS_WITH_CAVEATS", content)
            self.assertIn("DATA_LEAKAGE", content)
            self.assertIn("test summary", content)


if __name__ == "__main__":
    unittest.main()
