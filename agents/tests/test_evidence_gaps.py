"""卖出复盘证据缺口分类与只读聚合测试。"""
import unittest

from trade_postmortem import classify_evidence_gaps
from _weekly_backtest_review import summarize_evidence_gaps


class EvidenceGapTests(unittest.TestCase):
    def test_examples(self):
        examples = [
            ("止损参考价到成交价偏离无法归因", {"slippage_attribution"}),
            ("缺少同期 bid/ask", {"orderbook_missing"}),
            ("没有买卖盘口", {"orderbook_missing"}),
            ("信号与现货价格不一致", {"signal_price_consistency"}),
            ("stale ref", {"signal_price_consistency", "data_freshness"}),
            ("新闻因果无法权衡", {"news_causality"}),
            ("消息面材料不足", {"news_causality"}),
            ("regime 保守映射影响未知", {"regime_mapping"}),
            ("字段新鲜度不足", {"data_freshness"}),
            ("单一指标可能误判", {"其它-需人工"}),
        ]
        for text, expected in examples:
            with self.subTest(text=text):
                self.assertEqual(
                    {g["dim"] for g in classify_evidence_gaps([text])}, expected)

    def test_aggregation_and_old_records(self):
        rows = [
            {"ok": True, "order_id": "1", "ts": "2026-11-19T00:00:00Z",
             "analysis": {"limitations": [
                 {"limitation": "缺少同期 bid/ask", "why": "盘口未记录"},
                 {"limitation": "没有买卖盘口", "why": ""},
             ]}},
            {"ok": True, "order_id": "2", "ts": "2026-11-18T00:00:00Z",
             "analysis": {"limitations": ["止损参考价到成交价偏离无法归因"]}},
        ]
        report = summarize_evidence_gaps(rows)
        self.assertEqual(report["selected"], 2)
        self.assertEqual(report["counts"]["orderbook_missing"], 1)
        self.assertTrue(report["top_gaps"])
        self.assertIn("EVO-20261112-1", [s["evo_id"] for s in report["suggestions"]])
        self.assertEqual(
            set(report["suggestions"][0]), {"evo_id", "gap_dims", "orders_by_dim"})
        self.assertEqual(summarize_evidence_gaps(rows, limit=1)["selected"], 1)


if __name__ == "__main__":
    unittest.main()
