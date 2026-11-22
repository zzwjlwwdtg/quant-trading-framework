"""test_probe_veto_live_tracker.py — EVO-20261122-1 单元测试

锁死:
  · 规则入场识别 (action=WATCH_BUY_PROBE / reason 前缀)
  · 非规则入场不被识别
  · cohort_ledger 读取 (open+close 两遍扫描)
  · trade_log 影子回填 (FIFO 配对, INTC 类样本能被捕获)
  · compute_live_stats 正确计算 hit-stop 率 / win 率 / avg_pnl / 分位
  · deviation_test N < MIN_N 时不检验
  · deviation_test 阈值以下不告警
  · deviation_test 阈值以上 triggered=True, alert_message 非空
  · run_tracker 只读 (不写交易路径、不改止损)
"""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _probe_veto_live_tracker as pvt


# ── 辅助工厂 ────────────────────────────────────────────────────────────────

def _cohort_open(cohort_id: str, ticker: str, action: str = "WATCH_BUY_PROBE",
                 reason: str = "") -> dict:
    return {
        "event":      "open",
        "ts":         "2026-11-01T14:00:00+00:00",
        "ticker":     ticker,
        "cohort_id":  cohort_id,
        "price":      100.0,
        "qty":        10,
        "fired_key":  None,
        "signal_ctx": {"action": action, "confidence": 5, "reason": reason,
                       "regime": "neutral", "tag": f"[{action}]"},
    }


def _cohort_close(cohort_id: str, ticker: str, close_reason: str,
                  pnl_pct: float, hold_days: float) -> dict:
    pnl_usd = pnl_pct * 1000.0 / 100.0
    return {
        "event":            "close",
        "ts":               "2026-11-05T14:00:00+00:00",
        "ticker":           ticker,
        "cohort_id":        cohort_id,
        "close_price":      100.0 * (1 + pnl_pct / 100),
        "close_reason":     close_reason,
        "hold_days":        hold_days,
        "realized_pnl_usd": pnl_usd,
        "realized_pnl_pct": pnl_pct,
        "is_winner":        pnl_usd > 0,
        "cohort": {
            "ticker":           ticker,
            "hold_days":        hold_days,
            "realized_pnl_pct": pnl_pct,
            "realized_pnl_usd": pnl_usd,
            "is_winner":        pnl_usd > 0,
        },
    }


def _tradelog_buy(ticker: str, order_id: str, action: str = "WATCH_BUY_PROBE",
                  reason: str = "", price: float = 100.0) -> dict:
    if not reason:
        reason = f"sim_active_probe_after_claude_veto: some reason"
    return {
        "ts":       "2026-11-10T14:00:00+00:00",
        "ticker":   ticker,
        "side":     "BUY",
        "qty":      10,
        "price":    price,
        "order_id": order_id,
        "tag":      f"[{action}]",
        "dry_run":  False,
        "decision": {"action": action, "confidence": 5, "reason": reason},
        "market":   {},
    }


def _tradelog_sell(ticker: str, price: float, tag: str = "[TRAILING-STOP]") -> dict:
    return {
        "ts":       "2026-11-14T14:00:00+00:00",
        "ticker":   ticker,
        "side":     "SELL",
        "qty":      10,
        "price":    price,
        "order_id": "999",
        "tag":      tag,
        "dry_run":  False,
        "decision": {},
        "market":   {},
    }


def _write_jsonl(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ── 识别函数测试 ────────────────────────────────────────────────────────────

class RuleEntryDetectionTests(unittest.TestCase):
    def test_watch_buy_probe_action_detected(self):
        self.assertTrue(pvt._is_rule_entry({"action": "WATCH_BUY_PROBE"}))

    def test_reason_prefix_detected(self):
        self.assertTrue(pvt._is_rule_entry({
            "action": "SOMETHING_ELSE",
            "reason": "sim_active_probe_after_claude_veto: some text",
        }))

    def test_watch_buy_not_flagged(self):
        self.assertFalse(pvt._is_rule_entry({"action": "WATCH_BUY", "reason": ""}))

    def test_buy_not_flagged(self):
        self.assertFalse(pvt._is_rule_entry({"action": "BUY", "reason": "trend up"}))

    def test_none_ctx_not_flagged(self):
        self.assertFalse(pvt._is_rule_entry(None))

    def test_empty_ctx_not_flagged(self):
        self.assertFalse(pvt._is_rule_entry({}))


class StopExitTests(unittest.TestCase):
    def test_trailing_stop(self):
        self.assertTrue(pvt._is_stop_exit("[TRAILING-STOP from high $52]"))

    def test_software_stop(self):
        self.assertTrue(pvt._is_stop_exit("[SOFTWARE-STOP trigger $20]"))

    def test_take_profit_not_stop(self):
        self.assertFalse(pvt._is_stop_exit("TAKE-PROFIT tp15"))

    def test_normal_exit_not_stop(self):
        self.assertFalse(pvt._is_stop_exit(""))


# ── cohort_ledger 读取测试 ───────────────────────────────────────────────────

class LoadCohortLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "cohorts.jsonl"

    def test_rule_cohort_close_returned(self):
        records = [
            _cohort_open("C1", "US.TQQQ"),
            _cohort_close("C1", "US.TQQQ", "[TRAILING-STOP]", -5.0, 3.0),
        ]
        _write_jsonl(self.ledger, records)
        result = pvt.load_rule_cohorts_from_ledger(self.ledger)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ticker"], "US.TQQQ")
        self.assertAlmostEqual(result[0]["realized_pnl_pct"], -5.0)
        self.assertEqual(result[0]["source"], "cohort_ledger")

    def test_non_rule_cohort_excluded(self):
        records = [
            _cohort_open("C2", "US.SOXL", action="WATCH_BUY"),
            _cohort_close("C2", "US.SOXL", "TP", 8.0, 5.0),
        ]
        _write_jsonl(self.ledger, records)
        result = pvt.load_rule_cohorts_from_ledger(self.ledger)
        self.assertEqual(len(result), 0)

    def test_open_without_close_not_returned(self):
        records = [_cohort_open("C3", "US.GLD")]
        _write_jsonl(self.ledger, records)
        result = pvt.load_rule_cohorts_from_ledger(self.ledger)
        self.assertEqual(len(result), 0)

    def test_missing_ledger_returns_empty(self):
        result = pvt.load_rule_cohorts_from_ledger(Path(self.tmp.name) / "none.jsonl")
        self.assertEqual(result, [])

    def test_multiple_cohorts(self):
        records = [
            _cohort_open("C4", "US.MSFT"),
            _cohort_open("C5", "US.SHY"),
            _cohort_close("C4", "US.MSFT", "[TRAILING-STOP]", -3.0, 2.0),
            _cohort_close("C5", "US.SHY", "TAKE-PROFIT", 5.0, 7.0),
        ]
        _write_jsonl(self.ledger, records)
        result = pvt.load_rule_cohorts_from_ledger(self.ledger)
        self.assertEqual(len(result), 2)
        tickers = {r["ticker"] for r in result}
        self.assertIn("US.MSFT", tickers)
        self.assertIn("US.SHY", tickers)


# ── trade_log 影子回填测试 ───────────────────────────────────────────────────

class LoadTradelogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "trade_log.jsonl"

    def test_intc_like_stop_loss_captured(self):
        """INTC 类样本: WATCH_BUY_PROBE BUY + STOP SELL 应被捕获."""
        records = [
            _tradelog_buy("US.INTC", "50001", price=25.0),
            _tradelog_sell("US.INTC", price=23.0, tag="[TRAILING-STOP from high $25]"),
        ]
        _write_jsonl(self.log, records)
        result = pvt.load_rule_trades_from_tradelog(self.log)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ticker"], "US.INTC")
        self.assertAlmostEqual(result[0]["realized_pnl_pct"], -8.0, places=1)
        self.assertTrue(pvt._is_stop_exit(result[0]["close_reason"]))
        self.assertEqual(result[0]["source"], "trade_log")

    def test_non_rule_buy_excluded(self):
        """WATCH_BUY (非 PROBE) 的 BUY 不应进入结果."""
        records = [
            _tradelog_buy("US.NVDA", "50002", action="WATCH_BUY",
                           reason="oversold + uptrend", price=800.0),
            _tradelog_sell("US.NVDA", price=850.0),
        ]
        _write_jsonl(self.log, records)
        result = pvt.load_rule_trades_from_tradelog(self.log)
        self.assertEqual(len(result), 0)

    def test_dry_run_excluded(self):
        buy = _tradelog_buy("US.CBRS", "50003")
        buy["dry_run"] = True
        sell = _tradelog_sell("US.CBRS", 95.0)
        _write_jsonl(self.log, [buy, sell])
        result = pvt.load_rule_trades_from_tradelog(self.log)
        self.assertEqual(len(result), 0)

    def test_winner_classified_correctly(self):
        records = [
            _tradelog_buy("US.USO", "50004", price=100.0),
            _tradelog_sell("US.USO", price=110.0, tag="TAKE-PROFIT"),
        ]
        _write_jsonl(self.log, records)
        result = pvt.load_rule_trades_from_tradelog(self.log)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["is_winner"])
        self.assertAlmostEqual(result[0]["realized_pnl_pct"], 10.0, places=1)


# ── 去重合并测试 ─────────────────────────────────────────────────────────────

class LoadAllSamplesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "cohorts.jsonl"
        self.log    = Path(self.tmp.name) / "trade_log.jsonl"

    def test_tradelog_supplements_ledger_no_duplicate(self):
        # cohort_ledger: US.TQQQ; trade_log: US.TQQQ + US.MSFT
        # US.TQQQ の cohort_id が ledger と tradelog で異なるため両方残る
        # (ただし tradelog 側の cohort_id は tradelog_ prefix)
        _write_jsonl(self.ledger, [
            _cohort_open("C_TQQQ", "US.TQQQ"),
            _cohort_close("C_TQQQ", "US.TQQQ", "STOP", -4.0, 3.0),
        ])
        _write_jsonl(self.log, [
            _tradelog_buy("US.MSFT", "60001", price=500.0),
            _tradelog_sell("US.MSFT", 490.0, "[TRAILING-STOP]"),
        ])
        result = pvt.load_all_rule_samples(self.ledger, self.log)
        tickers = [r["ticker"] for r in result]
        self.assertIn("US.TQQQ", tickers)
        self.assertIn("US.MSFT", tickers)
        self.assertEqual(len(result), 2)

    def test_no_sources_returns_empty(self):
        result = pvt.load_all_rule_samples(
            Path(self.tmp.name) / "x.jsonl",
            Path(self.tmp.name) / "y.jsonl",
        )
        self.assertEqual(result, [])


# ── 統計計算テスト ─────────────────────────────────────────────────────────

class ComputeStatsTests(unittest.TestCase):
    def _make_samples(self, pnl_pcts: list, stop_flags: list,
                      hold_days: float = 3.0) -> list[dict]:
        result = []
        for i, (pnl, is_stop) in enumerate(zip(pnl_pcts, stop_flags)):
            result.append({
                "cohort_id":        f"C{i}",
                "ticker":           f"US.T{i}",
                "close_reason":     "[TRAILING-STOP]" if is_stop else "TAKE-PROFIT",
                "realized_pnl_pct": pnl,
                "is_winner":        pnl > 0,
                "hold_days":        hold_days,
                "source":           "test",
            })
        return result

    def test_empty_samples(self):
        s = pvt.compute_live_stats([])
        self.assertEqual(s["n"], 0)
        self.assertIsNone(s["hit_stop_rate"])

    def test_hit_stop_rate(self):
        # 4 samples: 2 stop, 2 normal
        samples = self._make_samples([-5, 3, -2, 8], [True, False, True, False])
        s = pvt.compute_live_stats(samples)
        self.assertEqual(s["n"], 4)
        self.assertEqual(s["n_stop"], 2)
        self.assertAlmostEqual(s["hit_stop_rate"], 0.5, places=4)

    def test_win_rate(self):
        samples = self._make_samples([-5, 3, -2, 8], [True, False, True, False])
        s = pvt.compute_live_stats(samples)
        self.assertEqual(s["n_winners"], 2)
        self.assertAlmostEqual(s["win_rate"], 0.5, places=4)

    def test_avg_pnl(self):
        samples = self._make_samples([-4.0, 6.0], [True, False])
        s = pvt.compute_live_stats(samples)
        self.assertAlmostEqual(s["avg_pnl_pct"], 1.0, places=4)

    def test_median_pnl_odd(self):
        samples = self._make_samples([-3, 1, 5], [True, False, False])
        s = pvt.compute_live_stats(samples)
        self.assertAlmostEqual(s["median_pnl_pct"], 1.0, places=4)

    def test_median_pnl_even(self):
        samples = self._make_samples([-4, -2, 2, 8], [True, True, False, False])
        s = pvt.compute_live_stats(samples)
        self.assertAlmostEqual(s["median_pnl_pct"], 0.0, places=4)

    def test_tickers_collected(self):
        samples = [
            {"cohort_id": "A", "ticker": "US.X", "close_reason": "",
             "realized_pnl_pct": 1.0, "is_winner": True, "hold_days": 1.0},
            {"cohort_id": "B", "ticker": "US.Y", "close_reason": "",
             "realized_pnl_pct": -1.0, "is_winner": False, "hold_days": 2.0},
        ]
        s = pvt.compute_live_stats(samples)
        self.assertIn("US.X", s["tickers"])
        self.assertIn("US.Y", s["tickers"])


# ── 偏離検証テスト ───────────────────────────────────────────────────────────

class DeviationTestTests(unittest.TestCase):
    def _stats(self, n: int, n_stop: int) -> dict:
        return {
            "n": n,
            "hit_stop_rate": n_stop / n if n else None,
        }

    def test_min_n_not_met(self):
        result = pvt.deviation_test(self._stats(5, 2), {"min_n": 20})
        self.assertFalse(result["triggered"])
        self.assertFalse(result["min_n_met"])
        self.assertIsNone(result["z_score"])
        self.assertIn("暂不检验", result["alert_message"])

    def test_below_threshold_no_alert(self):
        # OOS p0=0.35, N=25, sigma=sqrt(0.35*0.65/25)=0.0953
        # threshold=0.35+1.96*0.0953≈0.537; live 0.40 < threshold
        result = pvt.deviation_test(
            self._stats(25, 10),  # hit_stop=0.4
            {"oos_hit_stop_rate": 0.35, "min_n": 20},
        )
        self.assertTrue(result["min_n_met"])
        self.assertFalse(result["triggered"])
        self.assertIsNone(result["alert_message"])

    def test_above_threshold_triggers_alert(self):
        # OOS p0=0.35, N=25, sigma≈0.0953, threshold≈0.537
        # live hit_stop=0.80 >> threshold → triggered
        result = pvt.deviation_test(
            self._stats(25, 20),  # hit_stop=0.8
            {"oos_hit_stop_rate": 0.35, "min_n": 20},
        )
        self.assertTrue(result["triggered"])
        self.assertIsNotNone(result["alert_message"])
        self.assertIn("[ALERT]", result["alert_message"])
        self.assertGreater(result["z_score"], pvt.Z_THRESHOLD)

    def test_exactly_at_threshold_no_trigger(self):
        # 恰好等于 OOS 预期 (p_hat = p0) → z=0 → 不告警
        result = pvt.deviation_test(
            self._stats(25, 9),   # 9/25=0.36 ≈ p0=0.35, 低于 threshold
            {"oos_hit_stop_rate": 0.35, "min_n": 20},
        )
        self.assertFalse(result["triggered"])

    def test_sigma_calculation(self):
        # sigma = sqrt(0.4 * 0.6 / 100) = sqrt(0.0024) ≈ 0.049
        result = pvt.deviation_test(
            self._stats(100, 50),
            {"oos_hit_stop_rate": 0.4, "min_n": 20},
        )
        expected_sigma = math.sqrt(0.4 * 0.6 / 100)
        self.assertAlmostEqual(result["sigma"], expected_sigma, places=3)


# ── run_tracker 读写隔离测试 ──────────────────────────────────────────────

class RunTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger  = Path(self.tmp.name) / "cohorts.jsonl"
        self.log     = Path(self.tmp.name) / "trade_log.jsonl"
        self.verdict = Path(self.tmp.name) / "verdicts" / \
                       "sim_active_probe_after_claude_veto.json"

    def test_no_samples_returns_zero_n(self):
        # update_live_followup 在 backtest_verdicts 模块中动态导入; 用该路径 patch
        with patch("backtest_verdicts.update_live_followup"):
            result = pvt.run_tracker(self.ledger, self.log, verbose=False)
        self.assertEqual(result["samples_n"], 0)
        self.assertFalse(result["alert_triggered"])

    def test_tradelog_samples_aggregated(self):
        # 3 WATCH_BUY_PROBE: 2 stop, 1 profit
        _write_jsonl(self.log, [
            _tradelog_buy("US.A", "1", price=100.0),
            _tradelog_sell("US.A", 92.0, "[TRAILING-STOP]"),
            _tradelog_buy("US.B", "2", price=100.0),
            _tradelog_sell("US.B", 95.0, "[SOFTWARE-STOP trigger]"),
            _tradelog_buy("US.C", "3", price=100.0),
            _tradelog_sell("US.C", 112.0, "TAKE-PROFIT"),
        ])
        with patch("backtest_verdicts.update_live_followup"):
            result = pvt.run_tracker(self.ledger, self.log, verbose=False)
        self.assertEqual(result["samples_n"], 3)
        ls = result["live_stats"]
        self.assertEqual(ls["n_stop"], 2)
        self.assertAlmostEqual(ls["hit_stop_rate"], 2 / 3, places=3)

    def test_alert_not_triggered_for_small_n(self):
        # N=1 < MIN_N=20 → 不触发告警
        _write_jsonl(self.log, [
            _tradelog_buy("US.D", "4", price=100.0),
            _tradelog_sell("US.D", 80.0, "[TRAILING-STOP]"),
        ])
        with patch("backtest_verdicts.update_live_followup"):
            result = pvt.run_tracker(self.ledger, self.log, verbose=False)
        self.assertFalse(result["alert_triggered"])
        self.assertFalse(result["deviation"]["min_n_met"])

    def test_result_has_expected_keys(self):
        """run_tracker 返回结构检验 (只读: 不含下单 / 止损 / 黑名单字段)."""
        with patch("backtest_verdicts.update_live_followup"):
            result = pvt.run_tracker(self.ledger, self.log, verbose=False)
        expected_keys = {"live_stats", "deviation", "alert_triggered", "samples_n"}
        self.assertEqual(set(result.keys()), expected_keys)
        # 返回值里不含任何下单 / 止损 / 黑名单字段
        for forbidden in ("order", "stop_price", "blacklist", "action", "side"):
            self.assertNotIn(forbidden, result)

    def test_live_followup_written_to_verdict(self):
        import backtest_verdicts as bv
        verdicts_dir = Path(self.tmp.name) / "verdicts"
        verdicts_dir.mkdir()

        with patch.object(bv, "_VERDICTS_DIR", verdicts_dir):
            _write_jsonl(self.log, [
                _tradelog_buy("US.E", "5", price=100.0),
                _tradelog_sell("US.E", 95.0, "[TRAILING-STOP]"),
            ])
            pvt.run_tracker(self.ledger, self.log, verbose=False)

        verdict_file = verdicts_dir / "sim_active_probe_after_claude_veto.json"
        self.assertTrue(verdict_file.exists(),
                        "live_followup 应写入 backtest_verdicts 文件")
        data = json.loads(verdict_file.read_text(encoding="utf-8"))
        self.assertIn("live_followup", data)
        self.assertIn("updated_at", data["live_followup"])
        self.assertIn("live_stats",  data["live_followup"])
        self.assertIn("deviation",   data["live_followup"])


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
