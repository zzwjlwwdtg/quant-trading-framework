"""卖出复盘分母回归测试；通过 tests 包发现运行，不访问实盘材料。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import trade_postmortem as pm


class SellReviewReconciliationTests(unittest.TestCase):
    def sell(self, oid, **extra):
        return {"order_id": oid, "ticker": "US.TEST", "qty": 1,
                "price": 10, "ts": "2026-10-30T14:00:00+00:00",
                **extra}

    def review(self, oid, verdict="合理"):
        return {"order_id": oid, "ok": True,
                "analysis": {"hindsight": {"verdict": verdict}}}

    def test_thirty_orders_twenty_four_verdicts(self):
        # 合成验收样本，不冒充实际历史30笔核实结果。
        sells = [self.sell(str(i)) for i in range(30)]
        reviews = [self.review(str(i)) for i in range(24)]
        result = pm.reconcile_sell_reviews(sells, reviews)
        self.assertEqual((result["total"], result["judged"],
                          result["unjudged"]), (30, 24, 6))
        self.assertEqual(result["coverage"], .8)
        self.assertEqual(sum(result["categories"].values()), 24)
        self.assertTrue(all(r["verdict"] or r["missing_reason"]
                            for r in result["records"]))

    def test_missing_failure_and_rule_archive_are_not_verdicts(self):
        reviews = [
            {"order_id": "failed", "ok": False},
            {"order_id": "cash", "ok": True, "provider": "rule"},
            self.review("uncertain", "难判断"),
        ]
        result = pm.reconcile_sell_reviews(
            [self.sell(oid) for oid in
             ("pending", "failed", "cash", "indexed", "uncertain")],
            reviews, {"indexed": "旧索引"})
        states = {r["order_id"]: r["processing_status"]
                  for r in result["records"]}
        self.assertEqual(states["pending"], "未完成")
        self.assertEqual(states["failed"], "处理失败")
        self.assertEqual(states["cash"], "证据不足")
        self.assertEqual(states["indexed"], "证据不足")
        self.assertEqual(result["categories"]["难判断"], 1)
        self.assertEqual(result["judged"], 1)

    def test_duplicate_and_partial_fills_share_one_order(self):
        sells = [self.sell("one"), self.sell("one"),
                 self.sell("one", qty=2, price=11)]
        reviews = [self.review("one"), self.review("one")]
        first = pm.reconcile_sell_reviews(sells, reviews)
        second = pm.reconcile_sell_reviews(sells, reviews)
        self.assertEqual(first, second)
        self.assertEqual(first["total"], 1)
        self.assertEqual(first["judged"], 1)
        self.assertEqual(len(first["records"][0]["source_rows"]), 3)

    def test_conflict_and_missing_identifier_are_explicit(self):
        result = pm.reconcile_sell_reviews(
            [self.sell("one"), self.sell(None)],
            [self.review("one"), self.review("one", "不合理")])
        self.assertEqual(result["judged"], 0)
        self.assertEqual(result["unjudged"], 2)
        self.assertFalse(result["identities_verified"])
        self.assertTrue(all(r["missing_reason"] for r in result["records"]))

    def test_rerun_preserves_archived_manual_conclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "archive.jsonl"
            reviewed = root / "reviewed.json"
            latest = root / "latest.json"
            manual = self.review("one", "不合理")
            manual["provider"] = "manual"
            archive.write_text(json.dumps(manual) + "\n", encoding="utf-8")
            reviewed.write_text('{"one": "人工复核"}', encoding="utf-8")
            original = archive.read_bytes()
            sell = self.sell(
                "one", mechanical_cash=False, tag="",
                decision_ts="2026-10-30T14:00:00+00:00")
            with patch.multiple(pm, OUT_DIR=root, ARCHIVE=archive,
                                REVIEWED=reviewed, LATEST=latest):
                with patch.object(pm, "collect_sells", return_value=[sell, sell]):
                    with patch.object(pm, "review_one") as reviewer:
                        first = pm.run(live_fetch=False)
                        second = pm.run(live_fetch=False)
                        reviewer.assert_not_called()
            self.assertEqual(archive.read_bytes(), original)
            self.assertEqual(first["sell_review_coverage"]["judged"], 1)
            self.assertEqual(second["sell_review_coverage"]["categories"]["不合理"], 1)

    def test_archive_bad_line_is_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "archive.jsonl"
            archive.write_text("invalid\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                pm.load_review_archive(archive)
