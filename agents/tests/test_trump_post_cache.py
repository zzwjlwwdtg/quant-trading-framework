"""Regression: Trump parsing must cache per post id, not per prompt timestamp.

The old cache key included the current minute, so every run after the first
minute re-sent the whole 24h feed (~10 opus calls per run).  These tests pin the
per-post behaviour: cache hits make no AI call, only unseen/edited posts are
sent, and legacy parsed_*.json files are migrated once.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import trump_signal


def _post(pid: str, content: str = "content") -> dict:
    return {"id": pid, "created_at": "2026-10-20T00:00:00Z", "content": content}


def _raw_item() -> dict:
    return {
        "event_type": "NOISE",
        "direction": "neutral",
        "magnitude": "small",
        "tickers_affected": [],
        "confidence": 1,
        "is_overnight": False,
        "verbatim_evidence": "",
    }


class TrumpPostCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)
        self._orig_cache_dir = trump_signal.CACHE_DIR
        trump_signal.CACHE_DIR = self.cache_dir

    def tearDown(self):
        trump_signal.CACHE_DIR = self._orig_cache_dir
        self._tmp.cleanup()

    def _fake_ai(self, calls: list[str]):
        def fake(prompt: str, timeout: int = 300, *, caller: str = "", **kwargs):
            calls.append(caller)
            ids = re.findall(r'"id": "([^"]+)"', prompt)
            return (json.dumps({"items": [_raw_item() for _ in ids]}),
                    "ok", "Claude", "")
        return fake

    def test_second_run_reuses_per_post_cache_without_ai_call(self):
        calls: list[str] = []
        posts = [_post("1"), _post("2")]
        with patch("ai_prompt.query_ai_cli", side_effect=self._fake_ai(calls)):
            first = trump_signal.analyze_posts(posts)
            second = trump_signal.analyze_posts(posts)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], "trump")
        self.assertFalse(first.get("cached"))
        self.assertTrue(second.get("cached"))
        self.assertEqual([it["post_id"] for it in second["items"]], ["1", "2"])
        self.assertEqual(second["items"], first["items"])

    def test_only_unseen_posts_are_sent(self):
        calls: list[str] = []
        prompts: list[str] = []
        fake = self._fake_ai(calls)
        real_fake = fake

        def capturing(prompt, timeout=300, *, caller="", **kwargs):
            prompts.append(prompt)
            return real_fake(prompt, timeout, caller=caller, **kwargs)

        with patch("ai_prompt.query_ai_cli", side_effect=capturing):
            trump_signal.analyze_posts([_post("1"), _post("2")])
            second = trump_signal.analyze_posts(
                [_post("1"), _post("2"), _post("3")])

        self.assertEqual(len(calls), 2)
        self.assertNotIn('"id": "1"', prompts[1])
        self.assertIn('"id": "3"', prompts[1])
        self.assertEqual([it["post_id"] for it in second["items"]],
                         ["1", "2", "3"])

    def test_legacy_batch_cache_is_migrated(self):
        item = trump_signal._normalize_item(_raw_item(), _post("legacy-1", "hello world"))
        (self.cache_dir / "parsed_abc123.json").write_text(
            json.dumps({"items": [item]}), encoding="utf-8")

        with patch("ai_prompt.query_ai_cli",
                   side_effect=AssertionError("AI must not be called")):
            result = trump_signal.analyze_posts([_post("legacy-1", "hello world")])

        self.assertTrue(result.get("cached"))
        self.assertEqual(result["items"][0]["post_id"], "legacy-1")
        self.assertTrue((self.cache_dir / ".post_cache_migrated").exists())

    def test_edited_post_is_reparsed(self):
        calls: list[str] = []
        with patch("ai_prompt.query_ai_cli", side_effect=self._fake_ai(calls)):
            trump_signal.analyze_posts([_post("1", "old text")])
            trump_signal.analyze_posts([_post("1", "new text")])

        self.assertEqual(len(calls), 2)

    def test_parse_failure_keeps_cached_items(self):
        calls: list[str] = []
        fake = self._fake_ai(calls)
        with patch("ai_prompt.query_ai_cli", side_effect=fake):
            trump_signal.analyze_posts([_post("1")])
        with patch("ai_prompt.query_ai_cli", return_value=(None, "timeout", "Claude", "")):
            result = trump_signal.analyze_posts([_post("1"), _post("2")])

        self.assertTrue(result["fallback"])
        self.assertEqual([it["post_id"] for it in result["items"]], ["1"])


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
