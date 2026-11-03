"""EVO-20261028-1：合成材料只验证研究逻辑，不构成真实回测结论。"""
from __future__ import annotations

import copy
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import research_validation as rv
import trade_postmortem as pm


def fixture():
    """短交易时段用于压缩测试路径，不冒充真实交易所日历。"""
    sessions = []
    day = date(2021, 11, 1)
    while day <= date(2026, 10, 15):
        if day.weekday() < 5:
            opening = datetime(day.year, day.month, day.day, 14, 30,
                               tzinfo=timezone.utc)
            sessions.append({
                "date": day.isoformat(), "open": opening.isoformat(),
                "close": (opening + timedelta(minutes=3)).isoformat(),
            })
        day += timedelta(days=1)
    data = {
        "data_version": "合成数据-v1", "calendar_version": "合成日历-v1",
        "sessions": sessions, "inventory": [], "events": [],
        "replay": {"complete": True, "source_sha256": "a" * 64,
                   "rule_versions": ["历史规则-v1"]},
    }
    for period, start in (("train", "2022-02-01"), ("oos", "2025-02-03")):
        first = next(i for i, s in enumerate(sessions) if s["date"] >= start)
        for number in range(60):
            index = first + number * 5
            session = sessions[index]
            trigger = datetime.fromisoformat(session["open"])
            oversold = (number // 2) % 2 == 0
            ticker = "AAA" if number % 2 == 0 else "BBB"
            eid = f"{period}-{number:03d}"
            identity = {
                "event_id": eid, "event_date": session["date"],
                "ticker": ticker, "rule_version": "历史规则-v1",
            }
            daily = []
            for offset, prior in enumerate(sessions[index - 30:index]):
                daily.append({
                    "close_ts": prior["close"], "available_at": prior["close"],
                    "close": 150 - offset if oversold else 100 + offset,
                })
            minutes = []
            target = 105.0 if oversold else 100.0
            for future in sessions[index:index + 5]:
                opening = datetime.fromisoformat(future["open"])
                for minute in range(3):
                    ts = opening + timedelta(minutes=minute)
                    minutes.append({
                        "ts": ts.isoformat(), "open": 100.0, "low": 99.0,
                        "close": target,
                        "benchmark_open": 100.0, "benchmark_close": 100.0,
                    })
            data["inventory"].append(dict(identity))
            data["events"].append({
                **identity, "trigger_ts": trigger.isoformat(),
                "daily": daily, "minutes": minutes,
                "minute_reliable": True, "sector_benchmark": "合成板块",
            })
    return data


def protocol(data, **changes):
    kwargs = {
        "registered_at": "2026-10-28T00:00:00+00:00",
        "development_started_at": "2026-11-03T00:00:00+00:00",
        "registration_receipt": "仅测试登记凭据",
        "data_version": data["data_version"],
        "data_sha256": rv.oversold_digest(data),
        "code_version": "仅测试代码版本",
        "calendar_version": data["calendar_version"],
        "rule_versions": ["历史规则-v1"],
        "historical_oos_used": False,
    }
    kwargs.update(changes)
    return rv.make_oversold_protocol(**kwargs)


class OversoldStudyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = fixture()

    def evaluate(self, data=None, registration=None, code_version="仅测试代码版本"):
        data = self.base if data is None else data
        return rv.evaluate_oversold_study(
            registration or protocol(data), data, code_version=code_version)

    def test_positive_two_periods_and_no_mutation(self):
        before = rv.oversold_digest(self.base)
        result = self.evaluate()
        self.assertTrue(result["passed"])
        self.assertFalse(result["trading_authorized"])
        self.assertFalse(result["execution_improvement_verified"])
        self.assertEqual(before, rv.oversold_digest(self.base))
        for period in result["periods"].values():
            self.assertEqual(period["independent"], 60)
            self.assertEqual(period["oversold"]["n"], 30)
            self.assertEqual(period["control"]["n"], 30)
            self.assertEqual(len(period["matched_pairs"]), 30)
            self.assertGreater(period["primary"]["ci95"][0], 0)
            event = period["events"][0]
            self.assertEqual(event["entry_ts"][11:16], "14:31")
            self.assertEqual(set(event["returns"]), {"1", "3", "5"})
            self.assertLess(event["mae_pct"]["3"], 0)
            self.assertEqual(len(event["path"]), 14)

    def test_protocol_and_snapshot_tampering_fail_closed(self):
        frozen = protocol(self.base)
        changed = copy.deepcopy(frozen)
        changed["spec"]["minimum"]["total"] = 1
        self.assertFalse(self.evaluate(registration=changed)["passed"])
        data = copy.deepcopy(self.base)
        data["events"][0]["minutes"][0]["close"] += 1
        result = self.evaluate(data, registration=frozen)
        self.assertTrue(result["errors"])
        self.assertFalse(result["passed"])
        self.assertFalse(self.evaluate(code_version="另一版本")["passed"])

    def test_registration_must_precede_development(self):
        with self.assertRaises(ValueError):
            protocol(self.base, registered_at="2026-11-04T00:00:00Z")

    def test_contaminated_oos_requires_future_period(self):
        with self.assertRaises(ValueError):
            protocol(self.base, historical_oos_used=True)
        frozen = protocol(self.base, historical_oos_used=True,
                          prospective_end="2027-12-31")
        self.assertEqual(frozen["oos"][0], "2026-10-29")
        future = copy.deepcopy(self.base)
        future["baseline_sha256"] = frozen["data_sha256"]
        result = self.evaluate(future, registration=frozen)
        self.assertFalse(result["passed"])
        self.assertEqual(result["periods"]["oos"]["eligible"], 0)

    def test_future_or_unavailable_daily_bars_do_not_change_rsi(self):
        row = self.base["events"][0]
        trigger = rv._study_time(row["trigger_ts"])
        expected = rv._daily_state(row["daily"], trigger)
        daily = copy.deepcopy(row["daily"])
        daily.append({
            "close_ts": (trigger + timedelta(days=1)).isoformat(),
            "available_at": (trigger + timedelta(days=1)).isoformat(),
            "close": 10000,
        })
        daily.append({
            "close_ts": (trigger - timedelta(minutes=1)).isoformat(),
            "available_at": (trigger + timedelta(minutes=1)).isoformat(),
            "close": 10000,
        })
        self.assertEqual(rv._daily_state(daily, trigger), expected)

    def test_replay_inventory_missing_or_duplicate_is_rejected(self):
        for mutation in ("missing", "duplicate", "incomplete"):
            data = copy.deepcopy(self.base)
            if mutation == "missing":
                data["events"].pop()
            elif mutation == "duplicate":
                data["inventory"].append(dict(data["inventory"][0]))
            else:
                data["replay"]["complete"] = False
            with self.subTest(mutation=mutation):
                result = self.evaluate(data)
                self.assertFalse(result["passed"])
                self.assertTrue(result["errors"])

    def test_exclusion_rate_above_ten_percent_blocks_pass(self):
        data = copy.deepcopy(self.base)
        for row in data["events"][:7]:
            row["minute_reliable"] = False
        result = self.evaluate(data)
        train = result["periods"]["train"]
        self.assertEqual(len(train["excluded"]), 7)
        self.assertGreater(train["exclusion_rate"], 0.10)
        self.assertFalse(result["passed"])

    def test_missing_timestamp_and_minute_are_separately_reported(self):
        data = copy.deepcopy(self.base)
        data["events"][0]["trigger_ts"] = "时间不明"
        data["events"][1]["minutes"].pop(1)
        result = self.evaluate(data)
        excluded = result["periods"]["train"]["excluded"]
        self.assertEqual({r["event_id"] for r in excluded},
                         {"train-000", "train-001"})

    def test_same_ticker_within_five_sessions_is_merged(self):
        data = copy.deepcopy(self.base)
        row = copy.deepcopy(data["events"][0])
        row["event_id"] = "重复止损"
        data["events"].append(row)
        data["inventory"].append({
            k: row[k] for k in ("event_id", "event_date", "ticker", "rule_version")
        })
        result = self.evaluate(data)
        train = result["periods"]["train"]
        self.assertEqual(len(train["merged_event_ids"]), 1)
        self.assertEqual(train["independent"], 60)

    def test_negative_oos_cannot_be_rescued_by_training(self):
        data = copy.deepcopy(self.base)
        for row in data["events"][60:]:
            if row["daily"][-1]["close"] < row["daily"][0]["close"]:
                for bar in row["minutes"]:
                    bar["close"] = 99.5
        result = self.evaluate(data)
        self.assertTrue(result["periods"]["train"]["passed"])
        self.assertLess(result["periods"]["oos"]["primary"]["difference_pct"], 0)
        self.assertFalse(result["passed"])

    def test_matching_requires_same_ticker_and_trading_minute(self):
        data = copy.deepcopy(self.base)
        for item, row in zip(data["inventory"], data["events"]):
            if row["daily"][-1]["close"] > row["daily"][0]["close"]:
                item["ticker"] = row["ticker"] = "只有对照"
        result = self.evaluate(data)
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["periods"]["train"]["matched_pairs"]), 0)

    def test_cluster_shortage_does_not_create_confidence_interval(self):
        data = copy.deepcopy(self.base)
        for item, row in zip(data["inventory"], data["events"]):
            item["ticker"] = row["ticker"] = "单一标的"
        result = self.evaluate(data)
        self.assertFalse(result["passed"])
        self.assertIsNone(result["periods"]["train"]["primary"]["ci95"])

    def test_fifth_day_is_descriptive_only(self):
        data = copy.deepcopy(self.base)
        for row in data["events"]:
            first_day = row["event_date"]
            index = next(i for i, s in enumerate(data["sessions"])
                         if s["date"] == first_day)
            cutoff = rv._study_time(data["sessions"][index + 3]["open"])
            row["minutes"] = [
                bar for bar in row["minutes"] if rv._study_time(bar["ts"]) < cutoff
            ]
        result = self.evaluate(data)
        self.assertTrue(result["passed"])
        self.assertEqual(result["periods"]["train"]["oversold"]["5"]["n"], 0)


class OversoldPostmortemTests(unittest.TestCase):
    def test_default_evidence_and_forged_pass_fail_closed(self):
        self.assertFalse(pm.oversold_review_evidence()["passed"])
        evidence = pm.oversold_review_evidence({"passed": True})
        self.assertFalse(evidence["passed"])
        self.assertFalse(evidence["trading_authorized"])

    def test_ai_assertions_are_marked_and_verdict_is_downgraded(self):
        analysis = {
            "context": {"technical": "超卖意味着高概率反弹", "news": "材料不足"},
            "reasons": [
                {"type": "技术面", "reason": "规则触发", "evidence": "日志"}
                for _ in range(5)
            ],
            "limitations": [{"limitation": "材料不足", "why": "缺报价"}
                            for _ in range(3)],
            "hindsight": {"summary": "卖在底部", "verdict": "不合理"},
        }
        parsed = pm.parse_ai(json.dumps(analysis, ensure_ascii=False))
        self.assertEqual(parsed["hindsight"]["verdict"], "难判断")
        self.assertEqual(len(parsed["oversold_evidence_labels"]), 2)
        self.assertTrue(parsed["hindsight"]["summary"].startswith("【未验证假设】"))
        self.assertEqual(analysis["hindsight"]["summary"], "卖在底部")

    def test_review_archives_default_evidence_without_fetching_or_writing(self):
        sell = {
            "order_id": "合成订单", "ticker": "AAA", "qty": 1, "price": 100,
            "decision_ts": "2026-10-20T14:30:00Z", "tag": "STOP",
        }
        answer = {
            "context": {"technical": "规则触发", "news": "材料不足"},
            "reasons": [{"type": "技术面", "reason": "触发止损"}
                        for _ in range(5)],
            "limitations": [{"limitation": "缺报价"} for _ in range(3)],
            "hindsight": {"summary": "材料不足", "verdict": "难判断"},
        }
        prompts = []

        def ai(prompt):
            prompts.append(prompt)
            return json.dumps(answer, ensure_ascii=False), "ok", "测试", False

        with patch.object(pm, "load_log_window", return_value=[]), \
                patch.object(pm, "news_from_cache", return_value=[]), \
                patch.object(pm, "closes_after") as closes, \
                patch.object(pm, "news_from_rss") as rss, \
                patch.object(pm.Path, "write_text") as write:
            result = pm.review_one(sell, ai=ai, live_fetch=False)
        self.assertTrue(result["ok"])
        self.assertFalse(result["oversold_research"]["passed"])
        self.assertIn("未验证假设", prompts[0])
        self.assertIn("不得据此建议放松或暂停止损", prompts[0])
        closes.assert_not_called()
        rss.assert_not_called()
        write.assert_not_called()
