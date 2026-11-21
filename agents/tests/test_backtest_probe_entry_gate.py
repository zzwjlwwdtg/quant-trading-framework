"""双 AI Gate 联合状态研究辅助函数的测试。"""
import unittest

from _backtest_probe_entry_gate import gate_state, joint_state


def gate(action, confidence=6, confidence_min=6):
    return {
        "action": action,
        "confidence": confidence,
        "confidence_min": confidence_min,
    }


class ProbeEntryGateResearchTest(unittest.TestCase):
    def test_joint_states(self):
        cases = (
            ("BUY", "BUY", False),
            ("BUY", "WATCH_BUY", False),
            ("HOLD", "WATCH_BUY", True),
            ("HOLD", "HOLD", True),
            ("SELL", "STRONG_BUY", False),
            ("WATCH_BUY", "SELL", True),
        )
        for claude, codex, expected in cases:
            with self.subTest(claude=claude, codex=codex):
                result = joint_state(gate(claude), gate(codex))
                self.assertEqual(result["bucket"], f"{claude}/{codex}")
                self.assertIs(result["would_block"], expected)

    def test_low_confidence_and_boundary(self):
        self.assertEqual(gate_state("BUY", 5, 6), "LOW_CONF")
        self.assertEqual(gate_state("BUY", 6, 6), "BUY")
        self.assertTrue(joint_state(gate("BUY", 5), gate("WATCH_BUY"))["would_block"])
        self.assertFalse(joint_state(gate("BUY", 5), gate("BUY"))["would_block"])

    def test_missing_or_invalid_evidence_is_unknown(self):
        for confidence in (None, True, float("nan"), float("inf"), -1):
            with self.subTest(confidence=confidence):
                self.assertEqual(gate_state("BUY", confidence, 6), "UNKNOWN")
        self.assertEqual(gate_state("APPROVE", 8, 6), "UNKNOWN")
        self.assertEqual(gate_state("BUY", 8, 0), "UNKNOWN")
        self.assertIsNone(joint_state(gate("HOLD"), None)["would_block"])


if __name__ == "__main__":
    unittest.main()
