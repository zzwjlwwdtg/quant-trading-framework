from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd


AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import atomic_io
import backtest_engine
import claude_gate
import config
import daily_review
import decision_agent
import orchestrator
import paper_trader
import trading_contracts
import universe_picker
import _watchdog as watchdog


class ActionContractTests(unittest.TestCase):
    def test_probe_is_shared_by_execution_and_gate(self):
        self.assertIn("WATCH_BUY_PROBE", trading_contracts.BUY_ACTIONS)
        self.assertIn("WATCH_BUY_PROBE", trading_contracts.ORDER_ACTIONS)
        self.assertIn("WATCH_BUY_PROBE", trading_contracts.BULLISH_SIGNAL_ACTIONS)
        self.assertIs(paper_trader.BUY_ACTIONS, trading_contracts.BUY_ACTIONS)
        self.assertIs(claude_gate.ORDER_ACTIONS, trading_contracts.ORDER_ACTIONS)

    def test_window_threshold_scales_from_ten_to_five_points(self):
        self.assertEqual(trading_contracts.confidence_min("pre-market", 10), 7)
        self.assertEqual(trading_contracts.confidence_min("pre-market", 5), 3.5)
        self.assertEqual(trading_contracts.confidence_min("post-open", 5), 3)

    def test_position_multiplier_is_identical_across_confidence_scales(self):
        self.assertEqual(trading_contracts.confidence_multiplier(5, 5), 1.4)
        self.assertEqual(trading_contracts.confidence_multiplier(10, 10), 1.4)
        self.assertEqual(trading_contracts.confidence_multiplier(5, 5, probe=True), 0.3)

    def test_earnings_guard_blocks_probe_action(self):
        result = {
            "action": "WATCH_BUY_PROBE",
            "confidence": 5,
            "reason": "crisis-vbounce probe",
            "stop_ref": 80,
        }
        events = {
            "earnings_implied_move": {
                "US.SOXL": {
                    "stock": "NVDA",
                    "days_to_earnings": 5,
                    "implied_move_pct": 8,
                    "leverage": 3,
                }
            }
        }
        guarded = decision_agent._apply_earnings_guard(result, "US.SOXL", events)
        self.assertEqual(guarded["action"], "HOLD")
        self.assertEqual(guarded["demoted_from"], "WATCH_BUY_PROBE")

    def test_event_uncertainty_guard_blocks_probe_in_full_mode(self):
        result = {"action": "WATCH_BUY_PROBE", "confidence": 3, "reason": "test"}
        with patch.object(decision_agent, "_is_technical_only", return_value=False):
            guarded = decision_agent._apply_uncertain_guard(result, ev=2)
        self.assertEqual(guarded["action"], "HOLD")
        self.assertEqual(guarded["demoted_from"], "WATCH_BUY_PROBE")

    def test_event_uncertainty_guard_keeps_nonexecuting_long_hold_hint(self):
        result = {"action": "WATCH_BUY_LONG_HOLD", "confidence": 3, "reason": "hint"}
        with patch.object(decision_agent, "_is_technical_only", return_value=False):
            guarded = decision_agent._apply_uncertain_guard(result, ev=2)
        self.assertIs(guarded, result)

    def test_probe_rule_emits_executable_confidence_without_calibration(self):
        market = {
            "ticker": "US.SOXL",
            "price": 10.0,
            "rsi_14": 25,
            "vol_ratio": 1.0,
            "trend": "down",
            "ma_stack": "bear",
            "pct_chg": 0.0,
            "prev_pct": 0.0,
            "cum_5d_pct": -15.0,
        }
        confluence = {
            "bear_count": 0,
            "bull_count": 4,
            "bear_weighted": 0,
            "bull_weighted": 4,
            "calibrated": False,
        }
        with patch.dict(os.environ, {"CRISIS_VBOUNCE_ENABLED": "1"}), \
             patch.object(decision_agent, "_is_technical_only", return_value=True), \
             patch.object(decision_agent, "_load_calibration", return_value=None):
            result = decision_agent._etf_rules(
                market, {}, {}, regime="crisis", confluence=confluence
            )
        self.assertEqual(result["action"], "WATCH_BUY_PROBE")
        self.assertEqual(result["confidence"], 3)
        self.assertGreaterEqual(
            result["confidence"], trading_contracts.confidence_min("post-open", 5)
        )

    def test_daily_review_classifies_probe_as_bullish_five_day_signal(self):
        self.assertEqual(daily_review._HOLD_DAYS["WATCH_BUY_PROBE"], 5)
        self.assertIn("WATCH_BUY_PROBE", trading_contracts.BULLISH_SIGNAL_ACTIONS)


class ClaudeGateTests(unittest.TestCase):
    @staticmethod
    def _fake_ai_prompt() -> types.ModuleType:
        module = types.ModuleType("ai_prompt")
        module._is_claude_quota_status = lambda status: False
        module.query_codex_cli = lambda prompt, timeout: (None, "unused")
        module.query_claude_cli = lambda prompt, timeout: (
            json.dumps({
                "verdict": "APPROVE",
                "confidence": 5,
                "reason": "contract test",
                "risk_flags": [],
            }),
            "ok",
        )
        return module

    def test_five_point_action_reaches_gate_at_scaled_threshold(self):
        decision = {"action": "WATCH_BUY_PROBE", "confidence": 3, "reason": "test"}
        with tempfile.TemporaryDirectory() as tmp_dir, \
             patch.dict(os.environ, {"CLAUDE_DECISION_GATE": "1"}), \
             patch.object(claude_gate, "SIGNALS_DIR", tmp_dir), \
             patch.object(claude_gate, "_current_conf_scale", return_value=5), \
             patch.dict(sys.modules, {"ai_prompt": self._fake_ai_prompt()}):
            result = claude_gate.apply_claude_gate(
                "US.SOXL", {"price": 10}, {}, decision, {}, "post-open"
            )
        self.assertEqual(result["action"], "WATCH_BUY_PROBE")
        self.assertEqual(result["claude_gate"]["verdict"], "APPROVE")

    def test_sim_active_converts_ai_veto_to_stopped_probe(self):
        audit = {
            "verdict": "HOLD",
            "status": "ok",
            "reason": "wait for more confirmation",
        }
        decision = {
            "action": "WATCH_BUY",
            "confidence": 4,
            "stop_ref": 92,
        }
        with patch.dict(os.environ, {"TRADER_SIM_ACTIVE": "1"}):
            result = claude_gate._sim_active_probe(decision, {"price": 100}, audit)
        self.assertEqual(result["action"], "WATCH_BUY_PROBE")
        self.assertEqual(
            result["claude_gate"]["execution_override"],
            "SIM_ACTIVE_PROBE",
        )
        self.assertEqual(result["claude_gate"]["stop_distance_pct"], 8.0)

    def test_sim_active_never_overrides_invalid_or_wide_stop(self):
        audit = {"verdict": "HOLD", "status": "ok", "reason": "bad stop"}
        with patch.dict(os.environ, {"TRADER_SIM_ACTIVE": "1"}):
            self.assertIsNone(claude_gate._sim_active_probe(
                {"action": "WATCH_BUY", "stop_ref": 101}, {"price": 100}, audit
            ))
            self.assertIsNone(claude_gate._sim_active_probe(
                {"action": "WATCH_BUY", "stop_ref": 75}, {"price": 100}, audit
            ))


class SimActiveProfileTests(unittest.TestCase):
    def test_active_flag_is_hard_limited_to_simulation_account(self):
        with patch.dict(os.environ, {"TRADER_SIM_ACTIVE": "1"}), \
             patch.object(config, "TRADING_ACCOUNT_MODE", "SIMULATE"):
            self.assertTrue(config.is_sim_active_trading())
        with patch.dict(os.environ, {"TRADER_SIM_ACTIVE": "1"}), \
             patch.object(config, "TRADING_ACCOUNT_MODE", "REAL"):
            self.assertFalse(config.is_sim_active_trading())

    def test_deep_drawdown_keeps_quarter_budget_only_in_sim_active(self):
        state = {"__nav_peak": {"peak_nav": 100, "current_nav": 70}}
        with patch.object(paper_trader, "_state_load", return_value=state), \
             patch.dict(os.environ, {"TRADER_SIM_ACTIVE": "0"}):
            self.assertEqual(paper_trader._drawdown_multiplier(), 0.0)
        with patch.object(paper_trader, "_state_load", return_value=state), \
             patch.dict(os.environ, {"TRADER_SIM_ACTIVE": "1"}):
            self.assertEqual(paper_trader._drawdown_multiplier(), 0.25)

    def test_watch_buy_uses_probe_sizing_in_sim_active(self):
        with patch.dict(os.environ, {"TRADER_SIM_ACTIVE": "1"}), \
             patch.object(paper_trader, "_get_account_power", return_value=100_000), \
             patch.object(paper_trader, "_annual_vol", return_value=0.5), \
             patch.object(paper_trader, "_vix_multiplier", return_value=1.0), \
             patch.object(paper_trader, "_drawdown_multiplier", return_value=1.0), \
             patch.object(paper_trader, "_kelly_mult", return_value=1.0), \
             patch.object(paper_trader, "_group_cap_usd", return_value=(None, None)), \
             patch.object(paper_trader, "confidence_multiplier", wraps=trading_contracts.confidence_multiplier) as multiplier:
            size = paper_trader._position_size_usd("US.TQQQ", conf=4, action="WATCH_BUY")
        self.assertGreater(size, 0)
        self.assertTrue(multiplier.call_args.kwargs["probe"])

    def test_active_neutral_universe_accepts_moderate_positive_z(self):
        with patch.dict(os.environ, {"TRADER_SIM_ACTIVE": "0"}):
            self.assertIsNone(universe_picker._direction_for_regime(1.7, "neutral"))
        with patch.dict(os.environ, {"TRADER_SIM_ACTIVE": "1"}):
            self.assertEqual(
                universe_picker._direction_for_regime(1.7, "neutral"), "BUY"
            )

    def test_active_profile_offsets_duplicate_hmm_and_sector_tightening(self):
        market = {
            "ticker": "US.TQQQ",
            "price": 100,
            "rsi_14": 55,
            "vol_ratio": 1.0,
            "trend": "up",
            "ma_stack": "bull",
            "pct_chg": 1.0,
            "prev_pct": 0.0,
            "cum_5d_pct": 2.0,
            "bb_zone": "normal",
        }
        confluence = {
            "bull_count": 3,
            "bear_count": 0,
            "bull_weighted": 3.0,
            "bear_weighted": 0.0,
            "calibrated": False,
        }
        with patch.object(decision_agent, "_get_hmm_meta_state", return_value="bear_or_correction"), \
             patch.object(decision_agent, "_get_sector_regime", return_value="sector_weak"), \
             patch.object(decision_agent, "_is_technical_only", return_value=True), \
             patch.dict(os.environ, {"TRADER_SIM_ACTIVE": "0"}):
            conservative = decision_agent._etf_rules(
                market, {}, {}, regime="neutral", confluence=confluence, quant={}
            )
        with patch.object(decision_agent, "_get_hmm_meta_state", return_value="bear_or_correction"), \
             patch.object(decision_agent, "_get_sector_regime", return_value="sector_weak"), \
             patch.object(decision_agent, "_is_technical_only", return_value=True), \
             patch.dict(os.environ, {"TRADER_SIM_ACTIVE": "1"}):
            active = decision_agent._etf_rules(
                market, {}, {}, regime="neutral", confluence=confluence, quant={}
            )
        self.assertEqual(conservative["action"], "HOLD")
        self.assertEqual(active["action"], "WATCH_BUY")


class PaperTraderControlFlowTests(unittest.TestCase):
    def test_trailing_stop_runs_before_signal_confidence_and_sizing(self):
        saved = []
        state = {
            "US.TQQQ": {
                "entry_price": 100,
                "entry_high": 100,
                "first_entry_utc": "2026-07-01T00:00:00+00:00",
            }
        }
        with patch.object(paper_trader, "_position_size_usd", return_value=1_000) as sizing, \
             patch.object(paper_trader, "_position_qty", return_value=10), \
             patch.object(paper_trader, "_state_load", return_value=state), \
             patch.object(paper_trader, "_state_save", side_effect=lambda value: saved.append(value)), \
             patch.object(paper_trader, "_place", return_value="DRY"):
            paper_trader.execute(
                "US.TQQQ",
                {"action": "HOLD", "confidence": 1},
                {"price": 80},
                "post-open",
            )
        sizing.assert_not_called()
        self.assertEqual(len(saved), 1)
        self.assertRegex(saved[0]["US.TQQQ"]["last_window_key"], r"^\d{4}-\d{2}-\d{2}:post-open$")

    def test_retry_dedup_blocks_second_discipline_order(self):
        today = datetime.now(timezone.utc).date().isoformat()
        state = {
            "US.TQQQ": {
                "entry_price": 100,
                "entry_high": 100,
                "last_window_key": f"{today}:post-open",
            }
        }
        with patch.object(paper_trader, "_state_load", return_value=state), \
             patch.object(paper_trader, "_position_qty") as position_qty, \
             patch.object(paper_trader, "_place") as place:
            paper_trader.execute(
                "US.TQQQ",
                {"action": "HOLD", "confidence": 1},
                {"price": 80},
                "post-open",
            )
        position_qty.assert_not_called()
        place.assert_not_called()

    def test_high_water_mark_persists_on_hold(self):
        saved = []
        state = {"US.TQQQ": {"entry_price": 100, "entry_high": 100}}
        with patch.object(paper_trader, "_position_qty", return_value=10), \
             patch.object(paper_trader, "_state_load", return_value=state), \
             patch.object(paper_trader, "_state_save", side_effect=lambda value: saved.append(value)), \
             patch.object(paper_trader, "_place") as place:
            paper_trader.execute(
                "US.TQQQ",
                {"action": "HOLD", "confidence": 1},
                {"price": 110},
                "post-open",
            )
        place.assert_not_called()
        self.assertEqual(saved[-1]["US.TQQQ"]["entry_high"], 110)

    def test_legacy_five_point_state_can_pyramid_at_scaled_threshold(self):
        saved = []
        state = {"US.TQQQ": {"entry_price": 100, "entry_high": 100}}
        with patch.object(decision_agent, "_conf_scale", return_value=5), \
             patch.object(paper_trader, "_position_size_usd", return_value=1_000), \
             patch.object(paper_trader, "_position_qty", return_value=10), \
             patch.object(paper_trader, "_state_load", return_value=state), \
             patch.object(paper_trader, "_state_save", side_effect=lambda value: saved.append(value)), \
             patch.object(paper_trader, "_place", return_value="DRY") as place:
            paper_trader.execute(
                "US.TQQQ",
                {"action": "BUY", "confidence": 4},
                {"price": 100},
                "post-open",
            )
        self.assertTrue(place.called)
        self.assertEqual(saved[-1]["US.TQQQ"]["entry_conf"], 4)
        self.assertEqual(saved[-1]["US.TQQQ"]["entry_conf_scale"], 5)
        self.assertEqual(saved[-1]["US.TQQQ"]["pyramid_layer"], 2)

    def test_legacy_ten_point_entry_is_converted_before_five_point_pyramid(self):
        saved = []
        state = {
            "US.TQQQ": {
                "entry_price": 100,
                "entry_high": 100,
                "entry_conf": 7,
                "pyramid_layer": 1,
            }
        }
        with patch.object(decision_agent, "_conf_scale", return_value=5), \
             patch.object(paper_trader, "_position_size_usd", return_value=1_000), \
             patch.object(paper_trader, "_position_qty", return_value=10), \
             patch.object(paper_trader, "_state_load", return_value=state), \
             patch.object(paper_trader, "_state_save", side_effect=lambda value: saved.append(value)), \
             patch.object(paper_trader, "_place", return_value="DRY"):
            paper_trader.execute(
                "US.TQQQ",
                {"action": "BUY", "confidence": 5},
                {"price": 100},
                "post-open",
            )
        self.assertEqual(saved[-1]["US.TQQQ"]["pyramid_layer"], 2)
        self.assertEqual(saved[-1]["US.TQQQ"]["entry_conf_scale"], 5)

    def test_legacy_five_point_entry_does_not_pyramid_early_on_ten_point_scale(self):
        state = {
            "US.TQQQ": {
                "entry_price": 100,
                "entry_high": 100,
                "entry_conf": 5,
                "pyramid_layer": 1,
            }
        }
        with patch.object(decision_agent, "_conf_scale", return_value=10), \
             patch.object(paper_trader, "_position_size_usd", return_value=1_000), \
             patch.object(paper_trader, "_position_qty", return_value=10), \
             patch.object(paper_trader, "_state_load", return_value=state), \
             patch.object(paper_trader, "_place") as place:
            paper_trader.execute(
                "US.TQQQ",
                {"action": "BUY", "confidence": 6},
                {"price": 100},
                "post-open",
            )
        place.assert_not_called()

    def test_sell_signal_cannot_enter_rebuy_branch(self):
        state = {
            "US.TQQQ": {
                "last_action": "REDUCE",
                "last_price": 90,
                "last_time_utc": datetime.now(timezone.utc).isoformat(),
                "entry_price": 100,
                "entry_high": 100,
            }
        }
        with patch.object(paper_trader, "_position_qty", return_value=5), \
             patch.object(paper_trader, "_state_load", return_value=state), \
             patch.object(paper_trader, "_state_save"), \
             patch.object(paper_trader, "_position_size_usd") as sizing, \
             patch.object(paper_trader, "_within_sitting_window", return_value=False), \
             patch.object(paper_trader, "_place", return_value="DRY") as place:
            paper_trader.execute(
                "US.TQQQ",
                {"action": "SELL", "confidence": 5},
                {"price": 100},
                "post-open",
            )
        sizing.assert_not_called()
        self.assertEqual(place.call_args.args[1], paper_trader.TrdSide.SELL)

    def test_rebuy_records_window_key_and_retry_is_idempotent(self):
        state = {
            "US.TQQQ": {
                "last_action": "REDUCE",
                "last_price": 90,
                "last_time_utc": datetime.now(timezone.utc).isoformat(),
            }
        }
        with patch.object(paper_trader, "_position_qty", return_value=0), \
             patch.object(paper_trader, "_state_load", return_value=state), \
             patch.object(paper_trader, "_state_save"), \
             patch.object(paper_trader, "_position_size_usd", return_value=1_000), \
             patch.object(paper_trader, "_place", return_value="DRY") as place:
            for _ in range(2):
                paper_trader.execute(
                    "US.TQQQ",
                    {"action": "BUY", "confidence": 5},
                    {"price": 100},
                    "post-open",
                )
        place.assert_called_once()
        self.assertRegex(
            state["US.TQQQ"]["last_window_key"],
            r"^\d{4}-\d{2}-\d{2}:post-open$",
        )

    def test_live_sell_checks_loss_pause_after_trade_state_save(self):
        events = []
        state = {"US.TQQQ": {"entry_price": 100, "entry_high": 100}}
        with patch.object(paper_trader, "_position_qty", return_value=5), \
             patch.object(paper_trader, "_state_load", return_value=state), \
             patch.object(paper_trader, "_state_save", side_effect=lambda value: events.append("save")), \
             patch.object(paper_trader, "_within_sitting_window", return_value=False), \
             patch.object(paper_trader, "_place", return_value="order-1"), \
             patch.object(
                 paper_trader,
                 "_apply_loss_streak_pause_after_sell",
                 side_effect=lambda: events.append("pause"),
             ):
            paper_trader.execute(
                "US.TQQQ",
                {"action": "SELL", "confidence": 5},
                {"price": 100},
                "post-open",
            )
        self.assertEqual(events, ["save", "pause"])

    def test_corrupt_state_fails_closed_before_order(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "trader_state.json"
            state_path.write_text('{"US.TQQQ":', encoding="utf-8")
            with patch.object(paper_trader, "STATE_PATH", state_path), \
                 patch.object(paper_trader, "_place") as place, \
                 patch.object(paper_trader.logger, "error"):
                with self.assertRaises(paper_trader.StateLoadError):
                    paper_trader.execute(
                        "US.TQQQ",
                        {"action": "BUY", "confidence": 5},
                        {"price": 100},
                        "post-open",
                    )
            place.assert_not_called()


class BacktestContractTests(unittest.TestCase):
    def test_mid_backtest_accepts_three_of_five_confidence(self):
        dates = pd.date_range("2026-07-27", periods=3, freq="B")
        frame = pd.DataFrame({
            "close": [100.0, 101.0, 102.0],
            "ma50": [100.0, 100.0, 100.0],
            "rsi_14": [40.0, 40.0, 40.0],
            "ma20": [100.0, 100.0, 100.0],
            "bb_pct": [0.5, 0.5, 0.5],
            "cci_20": [0.0, 0.0, 0.0],
        }, index=dates)
        with patch.object(backtest_engine, "load_history", return_value=frame.copy()), \
             patch.object(backtest_engine, "add_indicators", side_effect=lambda value: value), \
             patch.object(backtest_engine, "build_mkt", return_value={"ticker": "US.GLD"}), \
             patch.object(decision_agent, "_conf_scale", return_value=5), \
             patch.object(decision_agent, "get_decision", return_value={"action": "BUY", "confidence": 3}):
            result = backtest_engine.run_mid(tickers=["GLD"], days=3)
        self.assertGreater(result["n_trades"], 0)

    def test_mid_backtest_pyramid_uses_five_point_multiplier(self):
        dates = pd.date_range("2026-07-27", periods=3, freq="B")
        frame = pd.DataFrame({
            "close": [100.0, 101.0, 102.0],
            "ma50": [100.0, 100.0, 100.0],
            "rsi_14": [40.0, 40.0, 40.0],
            "ma20": [100.0, 100.0, 100.0],
            "bb_pct": [0.5, 0.5, 0.5],
            "cci_20": [0.0, 0.0, 0.0],
        }, index=dates)
        decisions = [
            {"action": "BUY", "confidence": 3},
            {"action": "BUY", "confidence": 5},
            {"action": "HOLD", "confidence": 3},
        ]
        with patch.object(backtest_engine, "load_history", return_value=frame.copy()), \
             patch.object(backtest_engine, "add_indicators", side_effect=lambda value: value), \
             patch.object(backtest_engine, "build_mkt", return_value={"ticker": "US.GLD"}), \
             patch.object(decision_agent, "_conf_scale", return_value=5), \
             patch.object(decision_agent, "get_decision", side_effect=decisions), \
             patch.object(
                 backtest_engine,
                 "confidence_multiplier",
                 wraps=trading_contracts.confidence_multiplier,
             ) as multiplier:
            result = backtest_engine.run_mid(tickers=["GLD"], days=3)
        multiplier.assert_any_call(5, 5, probe=False)
        self.assertGreaterEqual(result["n_trades"], 2)

    def test_mid_backtest_sell_signal_cannot_rebuy_after_reduce(self):
        dates = pd.date_range("2026-07-27", periods=3, freq="B")
        frame = pd.DataFrame({
            "close": [100.0, 100.0, 104.0],
            "ma50": [100.0, 100.0, 100.0],
            "rsi_14": [40.0, 40.0, 40.0],
            "ma20": [100.0, 100.0, 100.0],
            "bb_pct": [0.5, 0.5, 0.5],
            "cci_20": [0.0, 0.0, 0.0],
        }, index=dates)
        decisions = [
            {"action": "BUY", "confidence": 3},
            {"action": "REDUCE", "confidence": 3},
            {"action": "SELL", "confidence": 3},
        ]
        with patch.object(backtest_engine, "load_history", return_value=frame.copy()), \
             patch.object(backtest_engine, "add_indicators", side_effect=lambda value: value), \
             patch.object(backtest_engine, "build_mkt", return_value={"ticker": "US.GLD"}), \
             patch.object(decision_agent, "_conf_scale", return_value=5), \
             patch.object(decision_agent, "get_decision", side_effect=decisions):
            result = backtest_engine.run_mid(tickers=["GLD"], days=3)
        self.assertEqual([trade["side"] for trade in result["history"]], ["BUY", "SELL", "SELL"])
        self.assertNotIn("REBUY", " ".join(trade["reason"] for trade in result["history"]))


class LauncherContractTests(unittest.TestCase):
    def test_all_orchestrator_launchers_enable_probe_consistently(self):
        expected = 'set "CRISIS_VBOUNCE_ENABLED=1"'
        for name in ("run.bat", "run_ja.bat"):
            with self.subTest(name=name):
                text = (AGENTS_DIR / name).read_text(encoding="utf-8")
                self.assertIn(expected, text)

    def test_all_orchestrator_launchers_enable_sim_active(self):
        expected = 'set "TRADER_SIM_ACTIVE=1"'
        for name in ("run.bat", "run_ja.bat"):
            with self.subTest(name=name):
                text = (AGENTS_DIR / name).read_text(encoding="utf-8")
                self.assertIn(expected, text)
        self.assertEqual(watchdog.ORCH_ENV["TRADER_SIM_ACTIVE"], "1")


class SingleInstanceLockTests(unittest.TestCase):
    def test_lock_creation_is_exclusive_and_released_by_owner(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = Path(tmp_dir) / ".orchestrator.lock"
            with patch.object(orchestrator, "_LOCK_PATH", lock_path), \
                 patch.object(orchestrator.atexit, "register"), \
                 patch("builtins.print"):
                orchestrator._check_lock_or_exit()
                self.assertEqual(lock_path.read_text(encoding="utf-8"), str(os.getpid()))
                with patch.object(orchestrator, "_pid_alive", return_value=True):
                    with self.assertRaises(SystemExit):
                        orchestrator._check_lock_or_exit()
                orchestrator._release_lock()
                self.assertFalse(lock_path.exists())


class SchedulerCommitTests(unittest.TestCase):
    class _FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 8, 3, 10, 5, tzinfo=ZoneInfo("America/New_York"))
            return value if tz is None else value.astimezone(tz)

    def setUp(self):
        self.previous = dict(orchestrator._last_window_run)
        for window in orchestrator._last_window_run:
            orchestrator._last_window_run[window] = None

    def tearDown(self):
        orchestrator._last_window_run.clear()
        orchestrator._last_window_run.update(self.previous)

    def test_failed_window_remains_pending_then_success_commits(self):
        with patch.object(orchestrator, "datetime", self._FixedDateTime), \
             patch.object(orchestrator, "run_cycle", side_effect=RuntimeError("boom")), \
             patch.object(orchestrator.logger, "exception"):
            orchestrator._tick()
        self.assertIsNone(orchestrator._last_window_run["post-open"])

        with patch.object(orchestrator, "datetime", self._FixedDateTime), \
             patch.object(orchestrator, "run_cycle"), \
             patch.object(orchestrator, "_run_report"):
            orchestrator._tick()
        self.assertEqual(orchestrator._last_window_run["post-open"], "2026-08-03")


class AtomicStateTests(unittest.TestCase):
    def test_atomic_write_round_trip_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "state.json"
            atomic_io.atomic_write_json(path, {"状态": "完整", "n": 2})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"状态": "完整", "n": 2})
            self.assertEqual(list(Path(tmp_dir).glob(".state.json.*.tmp")), [])

    def test_failed_replace_preserves_previous_state(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "state.json"
            path.write_text('{"version": 1}\n', encoding="utf-8")
            with patch.object(atomic_io.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    atomic_io.atomic_write_json(path, {"version": 2})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"version": 1})
            self.assertEqual(list(Path(tmp_dir).glob(".state.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
