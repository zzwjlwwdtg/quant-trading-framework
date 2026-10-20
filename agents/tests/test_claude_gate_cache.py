"""Claude gate: window-skips and same-input cache across restarts.

Outside trade windows the gate is display-only, so it must not spend a CLI
call; the decision is only marked as unreviewed.  Inside a window, a restart
with identical inputs reuses the recorded verdict, and every hit leaves a
status=cache_hit row in ai_calls.jsonl.  Failures and malformed replies are
never cached.
"""
from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_prompt
import claude_gate
import tests as test_pkg

APPROVE = json.dumps({
    "verdict": "APPROVE",
    "confidence": 8,
    "reason": "consistent",
    "risk_flags": [],
})
HOLD = json.dumps({
    "verdict": "HOLD",
    "confidence": 7,
    "reason": "contradictory indicators",
    "risk_flags": ["m15_bear"],
})


class GateCacheTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.cache_path = self.dir / "claude_gate_cache.json"
        self.health_path = self.dir / "ai_gate_health.json"
        self.ai_calls_path = self.dir / "ai_calls.jsonl"
        self.signals_dir = self.dir / "signals"
        self.signals_dir.mkdir()
        self._reloaded = False
        self._patches = [
            patch.object(claude_gate, "GATE_CACHE_PATH", self.cache_path),
            patch.object(claude_gate, "HEALTH_STATE_PATH", self.health_path),
            patch.object(claude_gate, "SIGNALS_DIR", str(self.signals_dir)),
            patch.object(claude_gate, "_current_conf_scale", return_value=10),
            patch.object(ai_prompt, "AI_CALL_LOG_PATH", self.ai_calls_path),
            patch.dict(os.environ, {"CLAUDE_DECISION_GATE": "1"}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        if self._reloaded:
            # reload() dropped tests/__init__ redirections for this module.
            claude_gate.GATE_CACHE_PATH = test_pkg.TMP_DIR / "claude_gate_cache.json"
            claude_gate.HEALTH_STATE_PATH = test_pkg.TMP_DIR / "ai_gate_health.json"
            claude_gate.SIGNALS_DIR = str(test_pkg.TMP_DIR)
            claude_gate._current_conf_scale = lambda: 10
        self._tmp.cleanup()

    def _decision(self, **overrides):
        decision = {
            "action": "BUY",
            "confidence": 9,
            "regime": "neutral",
            "reason": "rule test",
        }
        decision.update(overrides)
        return decision

    def _query(self, reply=APPROVE, provider="Claude", status="ok"):
        calls = []

        def fake(prompt_text, timeout=None, complexity=None, caller=None):
            calls.append((prompt_text, caller))
            return reply, status, provider, ""

        return fake, calls

    def _reload_gate(self):
        importlib.reload(claude_gate)
        self._reloaded = True
        claude_gate.GATE_CACHE_PATH = self.cache_path
        claude_gate.HEALTH_STATE_PATH = self.health_path
        claude_gate.SIGNALS_DIR = str(self.signals_dir)

    def _cache(self):
        if not self.cache_path.exists():
            return {}
        return json.loads(self.cache_path.read_text(encoding="utf-8"))

    def _ai_rows(self):
        if not self.ai_calls_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.ai_calls_path.read_text(encoding="utf-8").splitlines()
        ]

    def test_outside_window_skips_query_and_marks_decision(self):
        fake, calls = self._query()
        with patch.object(ai_prompt, "query_ai_cli", side_effect=fake):
            result = claude_gate.apply_claude_gate(
                "US.TEST", {"price": 10}, {}, self._decision(), {}, None
            )
        self.assertEqual(calls, [])
        self.assertEqual(result["action"], "BUY")
        self.assertEqual(result["confidence"], 9)
        self.assertEqual(result["ai_gate_status"], "skipped_outside_window")
        self.assertFalse(self.cache_path.exists())

    def test_in_window_approve_is_cached(self):
        fake, calls = self._query()
        with patch.object(ai_prompt, "query_ai_cli", side_effect=fake):
            result = claude_gate.apply_claude_gate(
                "US.TEST", {"price": 10}, {}, self._decision(), {}, "post-open"
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["claude_gate"]["verdict"], "APPROVE")
        cache = self._cache()
        self.assertEqual(len(cache), 1)
        self.assertEqual(next(iter(cache.values()))["verdict"], "APPROVE")

    def test_restart_same_inputs_reuses_verdict_and_audits_cache_hit(self):
        fake, calls = self._query()
        with patch.object(ai_prompt, "query_ai_cli", side_effect=fake):
            claude_gate.apply_claude_gate(
                "US.TEST", {"price": 10}, {}, self._decision(), {}, "post-open"
            )
        self.assertEqual(len(calls), 1)

        self._reload_gate()
        hit_query, hit_calls = self._query()
        with patch.object(claude_gate, "_current_conf_scale", return_value=10), \
             patch.object(ai_prompt, "query_ai_cli", side_effect=hit_query):
            result = claude_gate.apply_claude_gate(
                "US.TEST", {"price": 10}, {}, self._decision(), {}, "post-open"
            )
        self.assertEqual(hit_calls, [])
        self.assertEqual(result["claude_gate"]["status"], "cache_hit")
        self.assertTrue(result["claude_gate"]["cached"])
        self.assertEqual(result["claude_gate"]["verdict"], "APPROVE")

        rows = self._ai_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "cache_hit")
        self.assertEqual(rows[0]["provider"], "Claude")

    def test_changed_input_misses_cache(self):
        fake, calls = self._query()
        with patch.object(ai_prompt, "query_ai_cli", side_effect=fake):
            claude_gate.apply_claude_gate(
                "US.TEST", {"price": 10}, {}, self._decision(), {}, "post-open"
            )
        hold_query, hold_calls = self._query(HOLD)
        with patch.object(ai_prompt, "query_ai_cli", side_effect=hold_query):
            result = claude_gate.apply_claude_gate(
                "US.TEST", {"price": 10}, {},
                self._decision(reason="rule test changed"), {}, "post-open"
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(hold_calls), 1)
        self.assertEqual(result["action"], "HOLD")
        self.assertEqual(len(self._cache()), 2)

    def test_window_is_part_of_cache_identity(self):
        fake, calls = self._query()
        with patch.object(ai_prompt, "query_ai_cli", side_effect=fake):
            claude_gate.apply_claude_gate(
                "US.TEST", {"price": 10}, {}, self._decision(), {}, "post-open"
            )
        fake2, calls2 = self._query()
        with patch.object(ai_prompt, "query_ai_cli", side_effect=fake2):
            claude_gate.apply_claude_gate(
                "US.TEST", {"price": 10}, {}, self._decision(), {}, "midday"
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls2), 1)
        self.assertEqual(len(self._cache()), 2)

    def test_failures_are_not_cached(self):
        fail_query, fail_calls = self._query(reply=None, status="timeout")
        with patch.object(ai_prompt, "query_ai_cli", side_effect=fail_query), \
             patch("notifications.notify_ai_gate_unavailable"):
            failed = claude_gate.apply_claude_gate(
                "US.TEST", {"price": 10}, {}, self._decision(), {}, "post-open"
            )
        self.assertFalse(failed["ai_gate_available"])
        self.assertEqual(len(fail_calls), 1)
        self.assertEqual(self._cache(), {})

        ok_query, ok_calls = self._query()
        with patch.object(ai_prompt, "query_ai_cli", side_effect=ok_query):
            claude_gate.apply_claude_gate(
                "US.TEST", {"price": 10}, {}, self._decision(), {}, "post-open"
            )
        self.assertEqual(len(ok_calls), 1)
        self.assertEqual(len(self._cache()), 1)

    def test_malformed_verdict_is_not_cached(self):
        bad = json.dumps({"verdict": "MAYBE", "reason": "x"})
        fake, calls = self._query(reply=bad)
        with patch.object(ai_prompt, "query_ai_cli", side_effect=fake), \
             patch("notifications.notify_ai_gate_unavailable"):
            claude_gate.apply_claude_gate(
                "US.TEST", {"price": 10}, {}, self._decision(), {}, "post-open"
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._cache(), {})

    def test_cached_hold_is_demoted_again_after_restart(self):
        hold_query, hold_calls = self._query(HOLD)
        with patch.object(ai_prompt, "query_ai_cli", side_effect=hold_query):
            first = claude_gate.apply_claude_gate(
                "US.TEST", {"price": 10}, {}, self._decision(), {}, "post-open"
            )
        self.assertEqual(first["action"], "HOLD")
        self.assertEqual(len(hold_calls), 1)

        self._reload_gate()
        hit_query, hit_calls = self._query()
        with patch.object(claude_gate, "_current_conf_scale", return_value=10), \
             patch.object(ai_prompt, "query_ai_cli", side_effect=hit_query):
            second = claude_gate.apply_claude_gate(
                "US.TEST", {"price": 10}, {}, self._decision(), {}, "post-open"
            )
        self.assertEqual(hit_calls, [])
        self.assertEqual(second["action"], "HOLD")
        self.assertEqual(second["claude_gate"]["status"], "cache_hit")


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
