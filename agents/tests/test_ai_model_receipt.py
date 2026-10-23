"""F11 (Codex MODEL_AUDIT 2026-09-19): AI 实际模型可追溯.

旧: 未设 CODEX_MODEL_* 时 ai_calls.jsonl 只记 "cli_internal_default", 无法知道实际模型.
新: 从 codex exec 自身输出的头部解析运行时模型 ("model: xxx"), 分别记录
model_requested (env 指定) 与 model_reported (CLI 回报). 解析不到 model_reported=None (model 保留旧标签 cli_internal_default), 不猜.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import ai_prompt as ap

HEADER = """OpenAI Codex v0.50.0 (research preview)
--------
workdir: C:\\tmp\\codex_ai_run_x
model: gpt-5-codex
provider: openai
approval: never
sandbox: read-only
--------
"""


class ParseModel(unittest.TestCase):
    def test_header_line(self):
        self.assertEqual(ap._parse_codex_model(HEADER), "gpt-5-codex")

    def test_equals_form(self):
        self.assertEqual(ap._parse_codex_model("model = o4-mini\n"), "o4-mini")

    def test_absent(self):
        self.assertIsNone(ap._parse_codex_model("no header here\nmodel_provider: openai"))


class LogRecordsReceipt(unittest.TestCase):
    def _run(self, stderr, env_model=None):
        td = Path(tempfile.mkdtemp())
        log = td / "ai_calls.jsonl"

        def fake_run(cmd, **kw):
            out = Path(cmd[cmd.index("--output-last-message") + 1])
            out.write_text("分析正文", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr=stderr)

        env = {"AI_CLI_PRIMARY": "codex", "AI_CLI_FALLBACK": "none"}
        if env_model:
            env["CODEX_MODEL_MEDIUM"] = env_model
        with patch.object(ap, "AI_CALL_LOG_PATH", log), \
             patch.object(ap, "_find_codex_cli", return_value="codex"), \
             patch.object(ap.subprocess, "run", side_effect=fake_run), \
             patch.dict(ap.os.environ, env, clear=False):
            if not env_model:
                ap.os.environ.pop("CODEX_MODEL_MEDIUM", None)
            out = ap.query_ai_cli("hi", complexity="medium")
        return out, json.loads(log.read_text(encoding="utf-8").splitlines()[-1])

    def test_reported_model_logged(self):
        out, rec = self._run(HEADER)
        self.assertEqual(out[0], "分析正文")
        self.assertEqual(rec["model_reported"], "gpt-5-codex")
        self.assertIsNone(rec["model_requested"])
        self.assertEqual(rec["model"], "gpt-5-codex")

    def test_unreported_is_explicit(self):
        out, rec = self._run("")
        self.assertEqual(rec["model_reported"], None)
        self.assertEqual(rec["model"], "cli_internal_default")   # 兼容旧标签

    def test_requested_and_reported_both_kept(self):
        out, rec = self._run(HEADER.replace("gpt-5-codex", "o4-mini"), env_model="o4-mini")
        self.assertEqual((rec["model_requested"], rec["model_reported"]), ("o4-mini", "o4-mini"))


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
