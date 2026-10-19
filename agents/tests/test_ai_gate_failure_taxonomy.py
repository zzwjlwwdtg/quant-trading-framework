"""EVO-20261015-1: AI Gate 故障分类 / 健康状态 / 置信度语义修复的验收断言.

覆盖提案检验方案里的核心场景:
  1. quota / protocol / network / unavailable / unknown 五类故障分类
  2. 不可用结果不展示模型高置信度 (model_confidence=N/A, ai_gate_available=False)
  3. 原始规则信号完整保留 (original_rule_action / original_rule_confidence)
  4. 双通道健康状态 + 故障持续时间 + overall 字段写入 ai_gate_health.json
  5. 单通道成功 → overall=degraded
  6. 重复同类故障合并告警 (dedup window 内不再推)
  7. 恢复仅在 _ai_cli_diag 探测成功时出现 (普通业务 success 不发恢复告警)
  8. 不改变下单路径 — fail-closed 行为保留, action=HOLD, 保留 demoted_from
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import claude_gate
import notifications


def _mk_decision(action="WATCH_BUY", confidence=7, regime="neutral_chop") -> dict:
    return {
        "action": action,
        "confidence": confidence,
        "regime": regime,
        "reason": "rule engine BUY confluence",
    }


class HealthStateFixture:
    """共用 fixture: 隔离 ai_gate_health.json + notifications dedup 到临时目录."""

    def __init__(self):
        self._tmp = tempfile.mkdtemp(prefix="evo_ai_gate_")
        self.state_path = Path(self._tmp) / "ai_gate_health.json"
        self.dedup_path = Path(self._tmp) / "notify_dedup.json"
        self._patchers = [
            patch.object(claude_gate, "HEALTH_STATE_PATH", self.state_path),
            patch.object(notifications, "DEDUP_PATH", self.dedup_path),
        ]

    def __enter__(self):
        for p in self._patchers:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self._patchers:
            p.stop()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


class ClassifyCliFailureTests(unittest.TestCase):
    def test_quota_patterns(self):
        for s in [
            "error: exit=1 stderr=usage limit reached",
            "error: 429 too many requests",
            "claude: your limit resets in 2h",
            "insufficient_quota",
            "credit balance exceeded",
        ]:
            self.assertEqual(claude_gate.classify_cli_failure(s), "quota", s)

    def test_protocol_patterns(self):
        for s in [
            "invalid_json",
            "invalid_verdict: MAYBE",
            "codex_error: exit=1 stderr=failed to load models cache: unknown variant `max`",
            "could not parse response",
            "schema violation",
        ]:
            self.assertEqual(claude_gate.classify_cli_failure(s), "protocol", s)

    def test_network_patterns(self):
        for s in [
            "codex_timeout",
            "error: connection reset",
            "dns resolve failure",
            "tls handshake failed",
            "ssl error",
        ]:
            self.assertEqual(claude_gate.classify_cli_failure(s), "network", s)

    def test_unavailable_patterns(self):
        for s in [
            "codex_not_installed",
            "claude_not_installed",
            "not_installed",
            "import_error: no module ai_prompt",
        ]:
            self.assertEqual(claude_gate.classify_cli_failure(s), "unavailable", s)

    def test_unknown_fallthrough(self):
        self.assertEqual(claude_gate.classify_cli_failure(""), "unknown")
        self.assertEqual(claude_gate.classify_cli_failure("mystery exit=137"), "unknown")


class FailDecisionSemanticsTests(unittest.TestCase):
    def _run(self, status: str, provider: str = "Claude") -> dict:
        with HealthStateFixture(), \
             patch.object(notifications, "send_alert", return_value={"sent": ["mock"]}):
            return claude_gate._fail_decision(_mk_decision(), status, provider=provider)

    def test_marks_service_unavailable_not_model_hold(self):
        out = self._run("error: exit=1 stderr=usage limit reached")
        self.assertFalse(out["ai_gate_available"])
        self.assertEqual(out["ai_gate_failure_class"], "quota")
        self.assertEqual(out["claude_gate"]["model_confidence"], "N/A")
        # 模型 confidence 字段必须为 None (不是数字), 别让 UI 读成 "N/A" 后又误显示
        self.assertIsNone(out["claude_gate"]["confidence"])
        self.assertIn("ai_gate_unavailable", out["claude_gate"]["reason"])

    def test_preserves_original_rule_signal(self):
        out = self._run("codex_timeout")
        self.assertEqual(out["original_rule_action"], "WATCH_BUY")
        self.assertEqual(out["original_rule_confidence"], 7)
        self.assertEqual(out["claude_gate"]["original_rule_action"], "WATCH_BUY")
        self.assertEqual(out["claude_gate"]["original_rule_confidence"], 7)

    def test_fail_closed_still_blocks_order(self):
        """提案明确要求: 本次不改变任何下单许可. fail-closed 仍 demote 到 HOLD."""
        out = self._run("invalid_json")
        self.assertEqual(out["action"], "HOLD")
        self.assertEqual(out["demoted_from"], "WATCH_BUY")
        self.assertEqual(out["claude_gate"]["failure_class"], "protocol")

    def test_fail_open_marks_bypass_but_still_tags_unavailable(self):
        with HealthStateFixture(), \
             patch.object(claude_gate, "_fail_closed", return_value=False), \
             patch.object(notifications, "send_alert", return_value={"sent": ["mock"]}):
            out = claude_gate._fail_decision(_mk_decision(), "codex_timeout", provider="Codex")
        self.assertEqual(out["claude_gate"]["verdict"], "BYPASS_FAIL_OPEN")
        self.assertFalse(out["ai_gate_available"])
        self.assertEqual(out["ai_gate_failure_class"], "network")
        self.assertEqual(out["original_rule_action"], "WATCH_BUY")


class HealthStateTests(unittest.TestCase):
    def test_single_channel_down_marks_degraded(self):
        with HealthStateFixture() as fx:
            claude_gate.record_cli_failure("Codex", "codex_timeout")
            claude_gate.record_cli_success("Claude")
            state = json.loads(fx.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["overall"], "degraded")
        self.assertEqual(state["channels"]["codex"]["status"], "unavailable")
        self.assertEqual(state["channels"]["codex"]["failure_class"], "network")
        self.assertEqual(state["channels"]["claude"]["status"], "ok")

    def test_both_channels_down_marks_down(self):
        with HealthStateFixture() as fx:
            claude_gate.record_cli_failure("Codex", "codex_timeout")
            claude_gate.record_cli_failure("Claude", "usage limit reached")
            state = json.loads(fx.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["overall"], "down")
        self.assertEqual(state["channels"]["codex"]["failure_class"], "network")
        self.assertEqual(state["channels"]["claude"]["failure_class"], "quota")

    def test_failure_duration_grows_until_recovery(self):
        with HealthStateFixture() as fx:
            t0 = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
            info_a = claude_gate.record_cli_failure("Claude", "codex_timeout", now=t0)
            info_b = claude_gate.record_cli_failure(
                "Claude", "codex_timeout", now=t0 + timedelta(seconds=120)
            )
            self.assertEqual(info_a["duration_s"], 0)
            # 同类故障: duration 累计, failure_count 自增, is_new_failure=False
            self.assertEqual(info_b["duration_s"], 120)
            self.assertFalse(info_b["is_new_failure"])
            self.assertEqual(
                json.loads(fx.state_path.read_text(encoding="utf-8"))
                ["channels"]["claude"]["failure_count"],
                2,
            )

    def test_classification_switch_counts_as_new_failure(self):
        with HealthStateFixture():
            a = claude_gate.record_cli_failure("Claude", "codex_timeout")
            b = claude_gate.record_cli_failure("Claude", "usage limit reached")
            self.assertTrue(a["is_new_failure"])
            self.assertTrue(b["is_new_failure"], "class 切换必须作为新故障推送告警")
            self.assertEqual(b["failure_class"], "quota")

    def test_recovery_requires_probe_flag(self):
        """提案要求: 恢复须以实际探测成功为证据."""
        with HealthStateFixture():
            claude_gate.record_cli_failure("Claude", "codex_timeout")
            # 普通业务路径的 success 不算恢复证据
            r1 = claude_gate.record_cli_success("Claude", probe=False)
            self.assertFalse(r1["recovered"])
            self.assertEqual(r1["previous_status"], "unavailable")
            # 再把它拉回不可用, 验证探测路径确实能恢复
            claude_gate.record_cli_failure("Claude", "codex_timeout")
            r2 = claude_gate.record_cli_success("Claude", probe=True)
            self.assertTrue(r2["recovered"])


class MergedAlertTests(unittest.TestCase):
    def test_same_class_within_window_merges(self):
        calls = []
        def _spy(msg, level="info", dedup=True):
            calls.append((level, msg))
            return {"sent": ["spy"]}
        with HealthStateFixture(), patch.object(notifications, "send_alert", side_effect=_spy):
            r1 = notifications.notify_ai_gate_unavailable("quota", 60, "claude", "limit reached")
            r2 = notifications.notify_ai_gate_unavailable("quota", 120, "claude", "limit reached")
        self.assertEqual(len(calls), 1, "同通道同分类必须合并")
        self.assertFalse(r1["merged"])
        self.assertTrue(r2["merged"])

    def test_different_class_recreates_alert(self):
        calls = []
        def _spy(msg, level="info", dedup=True):
            calls.append(msg)
            return {"sent": ["spy"]}
        with HealthStateFixture(), patch.object(notifications, "send_alert", side_effect=_spy):
            notifications.notify_ai_gate_unavailable("quota", 60, "claude", "limit reached")
            notifications.notify_ai_gate_unavailable("network", 60, "claude", "timeout")
        self.assertEqual(len(calls), 2, "class 切换应各推一次, 不被合并")

    def test_recovery_alert_tagged_recovery_level(self):
        calls = []
        def _spy(msg, level="info", dedup=True):
            calls.append((level, msg))
            return {"sent": ["spy"]}
        with HealthStateFixture(), patch.object(notifications, "send_alert", side_effect=_spy):
            notifications.notify_ai_recovery(channel="claude", duration_s=600,
                                              classification="quota", overall="ok")
        self.assertEqual(calls[0][0], "recovery")
        self.assertIn("恢复", calls[0][1])


class DiagProbeIntegrationTests(unittest.TestCase):
    """_ai_cli_diag._emit_probe_outcome: 失败/成功的探测结果写回 health state 并触发告警."""

    def test_probe_success_from_down_sends_recovery(self):
        import _ai_cli_diag as diag
        recovery_calls = []

        def _spy_recovery(**kw):
            recovery_calls.append(kw)
            return {"sent": ["spy"]}

        with HealthStateFixture(), \
             patch.object(notifications, "send_alert", return_value={"sent": ["spy"]}), \
             patch.object(notifications, "notify_ai_recovery", side_effect=_spy_recovery):
            # 先制造一个不可用状态
            claude_gate.record_cli_failure("Codex", "codex_timeout")
            # 再模拟 _ai_cli_diag 跑了一次成功探测
            out = diag._emit_probe_outcome("codex", True, "ok")
        self.assertTrue(out["recovered"])
        self.assertEqual(len(recovery_calls), 1)
        self.assertEqual(recovery_calls[0]["channel"], "codex")

    def test_probe_success_without_prior_failure_no_recovery(self):
        import _ai_cli_diag as diag
        with HealthStateFixture(), \
             patch.object(notifications, "notify_ai_recovery") as nar:
            out = diag._emit_probe_outcome("codex", True, "ok")
        self.assertFalse(out["recovered"])
        nar.assert_not_called()

    def test_probe_failure_records_health_without_recovery(self):
        import _ai_cli_diag as diag
        with HealthStateFixture() as fx, \
             patch.object(notifications, "notify_ai_recovery") as nar:
            out = diag._emit_probe_outcome("claude", False, "codex_timeout")
            state = json.loads(fx.state_path.read_text(encoding="utf-8"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["failure_class"], "network")
        self.assertEqual(state["channels"]["claude"]["status"], "unavailable")
        nar.assert_not_called()


class ProbeStatusParseTests(unittest.TestCase):
    def test_rc0_with_ok_is_success(self):
        import _ai_cli_diag as diag
        ok, status = diag._probe_status({"rc": 0, "stdout": "OK\n", "stderr": ""})
        self.assertTrue(ok)
        self.assertEqual(status, "ok")

    def test_timeout_marked_timeout(self):
        import _ai_cli_diag as diag
        ok, status = diag._probe_status({"rc": None, "error": "timeout"})
        self.assertFalse(ok)
        self.assertEqual(status, "timeout")

    def test_nonzero_rc_marks_failure_with_stderr_tail(self):
        import _ai_cli_diag as diag
        ok, status = diag._probe_status(
            {"rc": 1, "stdout": "", "stderr": "unknown variant `max`"}
        )
        self.assertFalse(ok)
        self.assertIn("exit=1", status)
        self.assertIn("unknown variant", status)


if __name__ == "__main__":
    unittest.main()


class BothChannelsHonestReportTests(unittest.TestCase):
    """2026-10-19 用户要求: Claude 没额度时先看 Codex, 两边都不行才如实回报 (逐通道、各自分类)."""

    BOTH = ("claude: error: exit=1 stderr=You've hit your limit · resets 4am; "
            "codex=codex_error: exit=1 stderr=failed to decode models response: unknown variant `max`")
    FB = "claude: error: exit=1 stderr=You've hit your limit · resets 4am"

    def test_split_both_channels(self):
        parts = claude_gate.split_cli_failures(self.BOTH, "Codex", self.FB)
        self.assertEqual([p for p, _ in parts], ["claude", "Codex"])
        self.assertIn("hit your limit", parts[0][1])
        self.assertTrue(parts[1][1].startswith("codex_error"))
        self.assertNotIn("hit your limit", parts[1][1])

    def test_split_single_channel_unchanged(self):
        self.assertEqual(claude_gate.split_cli_failures("timeout", "Claude", ""), [("Claude", "timeout")])

    def test_both_down_records_each_channel_with_own_class(self):
        with HealthStateFixture() as fx, \
             patch.object(notifications, "send_alert", return_value={"sent": ["mock"]}) as alert:
            out = claude_gate._fail_decision(_mk_decision(), self.BOTH, provider="Codex",
                                             fallback_reason=self.FB)
            state = json.loads(fx.state_path.read_text(encoding="utf-8"))
        self.assertEqual(out["action"], "HOLD")                      # fail-closed 不变
        cg = out["claude_gate"]
        self.assertTrue(cg["all_tried_channels_unavailable"])
        classes = {c["channel"]: c["failure_class"] for c in cg["channel_failures"]}
        self.assertEqual(classes, {"claude": "quota", "codex": "protocol"})   # 不再串类
        self.assertEqual(state["channels"]["claude"]["failure_class"], "quota")
        self.assertEqual(state["channels"]["codex"]["failure_class"], "protocol")
        self.assertEqual(state["overall"], "down")
        self.assertIn("Claude 与 Codex 均不可用", cg["reason"])
        self.assertEqual(alert.call_count, 1)                         # 合并成一条告警
        self.assertIn("claude=quota", alert.call_args[0][0])
        self.assertIn("codex=protocol", alert.call_args[0][0])

    def _gate(self, reply):
        import os, types
        mod = types.ModuleType("ai_prompt")
        mod.query_ai_cli = lambda prompt, timeout, **kw: reply
        dec = {"action": "BUY", "confidence": 5, "reason": "test"}
        with HealthStateFixture() as fx, tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"CLAUDE_DECISION_GATE": "1"}), \
             patch.object(claude_gate, "SIGNALS_DIR", tmp), \
             patch.object(claude_gate, "_current_conf_scale", return_value=5), \
             patch.object(notifications, "send_alert", return_value={"sent": ["mock"]}), \
             patch.dict(sys.modules, {"ai_prompt": mod}):
            out = claude_gate.apply_claude_gate("US.SHY", {"price": 80}, {}, dec, {}, "post-open")
            state = json.loads(fx.state_path.read_text(encoding="utf-8"))
        return out, state

    def test_end_to_end_claude_quota_codex_answers(self):
        reply = ('{"verdict": "APPROVE", "reason": "ok", "confidence": 6, "risk_flags": []}',
                 "ok", "Codex", self.FB)
        out, state = self._gate(reply)
        self.assertEqual(out["claude_gate"]["verdict"], "APPROVE")   # Codex 的真实判断照常生效
        self.assertEqual(state["channels"]["claude"]["status"], "unavailable")
        self.assertEqual(state["channels"]["claude"]["failure_class"], "quota")
        self.assertEqual(state["channels"]["codex"]["status"], "ok")
        self.assertEqual(state["overall"], "degraded")

    def test_end_to_end_both_down_reports_honestly(self):
        out, state = self._gate((None, self.BOTH, "Codex", self.FB))
        self.assertEqual(out["action"], "HOLD")
        self.assertFalse(out["ai_gate_available"])
        self.assertTrue(out["claude_gate"]["all_tried_channels_unavailable"])
        self.assertEqual(state["overall"], "down")

    def test_fallback_reason_without_channel_prefix(self):
        parts = claude_gate.split_cli_failures("x", "Codex", "error: You've hit your limit")
        self.assertEqual(parts[0], ("claude", "error: You've hit your limit"))
