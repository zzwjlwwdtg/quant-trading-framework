"""止损成本与复盘覆盖的隔离回放；不访问行情、不写交易状态。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import execution_analytics as analytics
import fill_ledger
import trade_postmortem


class ExecutionCostReplayTests(unittest.TestCase):
    def quote(self, ts, bid):
        return {"ts": ts, "available_ts": ts, "bid": bid, "ask": bid + 0.02}

    def evidence(self):
        trigger = "2026-10-21T14:00:00+00:00"
        submit = "2026-10-21T14:00:01+00:00"
        return {
            "order_id": "S", "qty": 10, "price": 97.0, "fee": 0.25,
            "stop_line": 100.0,
            "trigger_ts": trigger, "submitted_ts": submit,
            "confirmed_ts": "2026-10-21T14:00:02+00:00",
            "first_fill_ts": "2026-10-21T14:00:03+00:00",
            "last_fill_ts": "2026-10-21T14:00:04+00:00",
            "trigger_quote": self.quote(trigger, 99),
            "submit_quote": self.quote(submit, 98),
        }

    def write_rows(self, path, rows):
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def test_cost_components_reconcile_without_double_counting(self):
        result = analytics.sell_cost_breakdown(self.evidence())
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["components"], {
            "stop_to_trigger_usd": 10.0,
            "trigger_to_submit_usd": 10.0,
            "submit_to_fill_usd": 10.0,
            "fees_usd": 0.25,
        })
        self.assertEqual(result["total_cost_usd"], 30.25)
        self.assertIsNone(result["gap_usd"])
        self.assertEqual(result["submission_delay_seconds"], 1)

    def test_missing_stale_or_future_quote_never_claims_exact_slippage(self):
        for mode in ("missing", "stale", "future", "crossed"):
            with self.subTest(mode=mode):
                item = self.evidence()
                if mode == "missing":
                    item.pop("submit_quote")
                elif mode == "stale":
                    item["submit_quote"]["ts"] = item["trigger_ts"]
                elif mode == "future":
                    item["submit_quote"]["available_ts"] = item["last_fill_ts"]
                else:
                    item["submit_quote"]["ask"] = 1
                result = analytics.sell_cost_breakdown(item)
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["execution_slippage_bps"])
                self.assertIsNone(result["total_cost_usd"])

    def test_time_reversal_invalidates_price_costs(self):
        item = self.evidence()
        item["confirmed_ts"] = item["last_fill_ts"]
        result = analytics.sell_cost_breakdown(item)
        self.assertEqual(result["status"], "invalid")
        self.assertIn("time_order:confirmed_ts>first_fill_ts", result["anomalies"])
        self.assertIsNone(result["execution_slippage_bps"])

    def test_discrete_window_observations_do_not_prove_causal_loss(self):
        item = self.evidence()
        item.update({
            "previous_window_end_ts": "2026-10-21T13:00:00+00:00",
            "window_start_ts": "2026-10-21T13:59:00+00:00",
            "between_window_quotes": [
                self.quote("2026-10-21T13:30:00+00:00", 99),
                self.quote("2026-10-21T13:20:00+00:00", 99.5),
            ],
        })
        result = analytics.trailing_stop_measurement(item)
        self.assertEqual(result["minutes_from_window_start"], 1)
        self.assertEqual(result["first_observed_breach_ts"], "2026-10-21T13:20:00+00:00")
        self.assertIsNone(result["earliest_breach_ts"])
        self.assertIsNone(result["window_extra_loss_usd"])
        self.assertEqual(result["stop_to_fill_usd"], 30)

    def test_cumulative_partial_fills_and_fee_match_ledger_precision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger, fees = root / "ledger.jsonl", root / "fees.jsonl"
            self.write_rows(ledger, [
                {"event": "submitted", "order_id": "S",
                 "ts": "2026-10-21T14:00:00+00:00"},
                {"event": "partial", "order_id": "S", "ticker": "US.QQQ",
                 "side": "SELL", "dealt_qty": 4, "average_fill_price": 100,
                 "broker_time": "2026-10-21T14:00:01+00:00"},
                {"event": "filled", "order_id": "S", "ticker": "US.QQQ",
                 "side": "SELL", "dealt_qty": 10, "average_fill_price": 99.1234,
                 "broker_time": "2026-10-21T14:00:02+00:00"},
            ])
            self.write_rows(fees, [{"order_id": "S", "fee_amount": 0.123456}])
            before = ledger.read_bytes(), fees.read_bytes()
            rows = fill_ledger.sell_execution_evidence(ledger, fees)
            self.assertEqual(len(rows), 1)
            result = rows[0]
            self.assertEqual(result["qty"], 10)
            self.assertEqual(round(result["price"], 4), 99.1234)
            self.assertEqual(result["fee"], 0.123456)
            self.assertEqual(result["anomalies"], [])
            self.assertAlmostEqual(
                sum(r["qty"] * r["price"] for r in result["fills"]) / 10,
                99.1234,
            )
            self.assertEqual(before, (ledger.read_bytes(), fees.read_bytes()))

    def test_qty_reversal_and_missing_broker_time_are_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger, fees = root / "ledger.jsonl", root / "fees.jsonl"
            self.write_rows(ledger, [
                {"event": "partial", "order_id": "S", "ticker": "QQQ",
                 "side": "SELL", "dealt_qty": 10, "average_fill_price": 100},
                {"event": "filled", "order_id": "S", "ticker": "QQQ",
                 "side": "SELL", "dealt_qty": 8, "average_fill_price": 100},
            ])
            self.write_rows(fees, [])
            item = fill_ledger.sell_execution_evidence(ledger, fees)[0]
            self.assertIn("qty_reversal", item["anomalies"])
            self.assertIsNone(item["first_fill_ts"])
            self.assertIsNone(item["fee"])

    def test_replay_covers_thirty_and_includes_older_trailing_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {name: root / f"{name}.jsonl"
                     for name in ("ledger", "fees", "trades", "archive")}
            reviewed = root / "reviewed.json"
            events, trades = [], []
            for index in range(31):
                oid = str(index)
                # 第 0 笔早于覆盖窗口，但必须仍在全历史跟踪止损统计中。
                ts = f"2026-10-21T14:{index:02d}:00+00:00"
                events.extend([
                    {"event": "submitted", "order_id": oid, "ts": ts},
                    {"event": "filled", "order_id": oid, "ticker": "QQQ",
                     "side": "SELL", "dealt_qty": 1, "average_fill_price": 99,
                     "broker_time": ts},
                ])
                trades.append({"order_id": oid, "tag": "TRAILING_STOP",
                               "stop_line": 100, "oversold": index == 0})
            self.write_rows(paths["ledger"], events)
            self.write_rows(paths["fees"], [])
            self.write_rows(paths["trades"], trades)
            self.write_rows(paths["archive"], [{"order_id": "30", "ok": True}])
            reviewed.write_text(json.dumps({"29": "旧索引"}), encoding="utf-8")
            watched = list(paths.values()) + [reviewed]
            before = {p: p.read_bytes() for p in watched}
            kwargs = {
                "ledger_path": paths["ledger"], "fees_path": paths["fees"],
                "trade_log_path": paths["trades"], "archive_path": paths["archive"],
                "reviewed_path": reviewed,
            }
            result = trade_postmortem.replay_execution_audit(**kwargs)
            self.assertTrue(result["validation_passed"])
            self.assertEqual(result["coverage"]["found"], 30)
            self.assertEqual(result["coverage"]["reviewed"], 1)
            self.assertEqual(len(result["all_historical_trailing_stops"]), 31)
            missing = next(r for r in result["coverage"]["records"] if r["order_id"] == "29")
            self.assertIn("缺少成功归档", missing["missing_reason"])
            self.assertIsNone(result["trailing_stop_summary"]["window_extra_loss_usd"])
            self.assertFalse(result["publication_authorized"])
            self.assertEqual(before, {p: p.read_bytes() for p in watched})
            self.write_rows(paths["ledger"], events[:2])
            result = trade_postmortem.replay_execution_audit(**kwargs)
            self.assertFalse(result["validation_passed"])

    def test_bad_source_line_blocks_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger, fees = root / "ledger", root / "fees"
            trades, archive, reviewed = root / "trades", root / "archive", root / "reviewed"
            ledger.write_text("{坏行\n", encoding="utf-8")
            for path in (fees, trades, archive):
                path.write_text("", encoding="utf-8")
            reviewed.write_text("{}", encoding="utf-8")
            result = trade_postmortem.replay_execution_audit(
                ledger_path=ledger, fees_path=fees, trade_log_path=trades,
                archive_path=archive, reviewed_path=reviewed,
            )
            self.assertFalse(result["validation_passed"])
            self.assertTrue(any("invalid_json" in error for error in result["source_errors"]))
