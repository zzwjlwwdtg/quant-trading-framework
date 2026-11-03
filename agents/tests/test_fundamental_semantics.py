"""EVO-20261031-1：固定响应验收，仅检查数据语义与既有质量门不变。"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from data_quality import (
    assess_market_snapshot,
    audit_latest_signal_files,
    diagnose_fundamental_fields,
    order_data_gate,
)


class FundamentalSemantics(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
        self.market = {
            "price": 100, "bid": 99.9, "ask": 100.1,
            "quote_ts": self.now.isoformat(),
            "rsi_14": 50, "ma20": 100, "ma50": 100, "vol_ratio": 1,
        }

    def test_five_fixed_responses_preserve_quality_and_order_gates(self):
        # 验收预期固定：404 不证明失效；独立失效依据与行情异常分别保留。
        cases = [
            ("ETF基本面404且行情正常", "TLT", {}, {}, "bond", "unverified", "ok"),
            ("股票基本面缺失", "AAPL", {}, {}, "equity", "unverified", "ok"),
            ("未知资产类型", "UNKNOWN", {}, {}, "unknown", "unverified", "ok"),
            ("真实失效标的", "OLD", {}, {
                "status": "invalid", "evidence": "固定夹具：交易所退市通知",
            }, "unknown", "invalid", "ok"),
            ("行情同时异常", "TLT", {"price": -1}, {},
             "bond", "unverified", "critical"),
        ]
        for label, ticker, changes, identity, asset_class, status, health in cases:
            with self.subTest(label=label):
                market = {**self.market, **changes}
                response = {
                    "http_status": 404 if ticker != "AAPL" else 200,
                    "error": "固定原始响应：基本面数据未返回",
                    "fields": {"trailingPE": None},
                    "identity": identity,
                }
                original_market = deepcopy(market)
                original_response = deepcopy(response)
                baseline_quality = assess_market_snapshot(market, now=self.now)
                baseline_gates = {
                    side: order_data_gate(market, side, now=self.now)
                    for side in ("BUY", "SELL")
                }
                result = diagnose_fundamental_fields(
                    ticker, market, response, now=self.now,
                )
                self.assertEqual(result["asset_identity"]["asset_class"], asset_class)
                self.assertEqual(result["asset_identity"]["status"], status)
                self.assertEqual(result["asset_identity"]["evidence"], identity.get("evidence"))
                self.assertEqual(result["market_health"]["status"], health)
                self.assertEqual(result["market_health"], baseline_quality)
                self.assertEqual(
                    result["fundamental_fields"]["trailingPE"]["status"],
                    "missing_pending_verification",
                )
                self.assertEqual(result["raw_response"], original_response)
                self.assertEqual(market, original_market)
                self.assertEqual(response, original_response)
                for side, baseline in baseline_gates.items():
                    self.assertEqual(
                        order_data_gate(market, side, now=self.now), baseline,
                    )

    def test_not_applicable_requires_asset_and_capability_evidence(self):
        capability = {
            "asset_class": "bond", "supported": False,
            "source": "固定夹具：接口能力说明明确排除该资产类型字段",
        }
        for ticker, evidence, expected in [
            ("TLT", capability, "not_applicable"),
            ("UNKNOWN", capability, "missing_pending_verification"),
            ("AAPL", capability, "missing_pending_verification"),
            ("TLT", {**capability, "source": ""}, "missing_pending_verification"),
            ("TLT", {}, "missing_pending_verification"),
        ]:
            with self.subTest(ticker=ticker, evidence=evidence):
                result = diagnose_fundamental_fields(
                    ticker, self.market, {
                        "http_status": 404,
                        "fields": {"trailingPE": None},
                        "field_capabilities": {"trailingPE": evidence},
                    }, now=self.now,
                )
                self.assertEqual(
                    result["fundamental_fields"]["trailingPE"]["status"], expected,
                )

    def test_endpoint_error_without_field_list_remains_traceable(self):
        response = {"http_status": 404, "error": {"body": "固定原始404"}}
        result = diagnose_fundamental_fields(
            "TLT", self.market, response, now=self.now,
        )
        self.assertEqual(
            result["fundamental_fields"]["*"]["status"],
            "missing_pending_verification",
        )
        result["raw_response"]["error"]["body"] = "修改诊断副本"
        self.assertEqual(response["error"]["body"], "固定原始404")

    def test_audit_adds_diagnostics_without_changing_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "TLT_latest.json"
            # 固定缺失时间戳，避免测试依赖实际时钟，也验证异常不被降级。
            market = {**self.market, "quote_ts": None}
            payload = {"market": market}
            path.write_text(json.dumps(payload), encoding="utf-8")
            baseline = audit_latest_signal_files(Path(directory))
            payload["fundamentals"] = {
                "http_status": 404, "error": "固定404",
                "fields": {"trailingPE": None},
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            actual = audit_latest_signal_files(Path(directory))
            diagnostic = actual["files"][0].pop("fundamental_diagnostics")
            self.assertEqual(diagnostic["raw_response"], payload["fundamentals"])
            # 生成时间随调用变化；其他汇总及每个质量字段必须完全一致。
            baseline.pop("generated_at")
            actual.pop("generated_at")
            self.assertEqual(actual, baseline)
            self.assertEqual(actual["status"], "critical")
