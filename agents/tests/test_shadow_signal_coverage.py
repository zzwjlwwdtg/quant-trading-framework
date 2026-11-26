"""主动卖出影子研究的复盘展示测试。"""
import unittest

import trade_postmortem


class ShadowSignalCoverageTests(unittest.TestCase):
    def test_缺少研究材料时明确标记且不授权交易(self):
        sell = {"order_id": "order-1", "ticker": "US.TEST"}
        result = trade_postmortem.shadow_signal_coverage(sell)

        self.assertEqual(result["order_id"], "order-1")
        self.assertEqual(result["status"], "missing")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["trading_authorized"])
        self.assertIn("历史信号快照", result["reason"])


if __name__ == "__main__":
    unittest.main()
