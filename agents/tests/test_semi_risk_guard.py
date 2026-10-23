"""半导体风险开关 (2026-10-03, 用户确认): 3 个看错条件 → 半导体买入门槛自动提高."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import semi_risk_guard as srg
import thesis_config


OK = {"mega_eps_revision_30d_pct": 0.8, "dgs10_pct": 5.27, "dfii10_60d_delta_bps": 12.0,
      "hy_oas_60d_delta_bps": 10.0}


class Evaluate(unittest.TestCase):
    def test_nothing_triggered(self):
        ev = srg.evaluate(OK)
        self.assertEqual((ev["triggered"], ev["active"], ev["unknown"]), ([], False, []))

    def test_each_condition(self):
        self.assertEqual(srg.evaluate({**OK, "mega_eps_revision_30d_pct": -1.0})["triggered"],
                         ["earnings_revisions_down"])
        self.assertEqual(srg.evaluate({**OK, "dgs10_pct": 5.6})["triggered"], ["yield_5p5_real_rising"])
        # 5.6% 但实际利率在下行 → 不触发 (两个条件都要满足)
        self.assertEqual(srg.evaluate({**OK, "dgs10_pct": 5.6, "dfii10_60d_delta_bps": -5})["triggered"], [])
        self.assertEqual(srg.evaluate({**OK, "hy_oas_60d_delta_bps": 80})["triggered"], ["hy_spread_widening"])

    def test_missing_data_is_unknown_not_ok(self):
        ev = srg.evaluate({"dgs10_pct": 5.0})
        self.assertFalse(ev["active"])
        self.assertIn("earnings_revisions_down", ev["unknown"])
        self.assertIn("hy_spread_widening", ev["unknown"])

    def test_config_conditions_used(self):
        cfg = thesis_config.semi_risk_guard_config()
        self.assertEqual(set(cfg["conditions"]), set(srg.DEFAULT_CONDITIONS))
        self.assertEqual(srg.evaluate(OK, cfg["conditions"])["triggered"], [])

    def test_mega_eps_revision_cap_weighted(self):
        trends = {"A": {"cur": [11, 22], "ago30": [10, 20]},     # +10%
                  "B": {"cur": [9.5], "ago30": [10]}}            # −5%
        self.assertAlmostEqual(srg.mega_eps_revision(trends, {"A": 1, "B": 3}), (10 * 1 - 5 * 3) / 4, places=3)
        self.assertIsNone(srg.mega_eps_revision({}, {}))


class Delta(unittest.TestCase):
    END = date(2026, 10, 20)

    def test_delta_is_60_calendar_days_not_60_observations(self):
        # 每 2 天一个观测, 61 个观测跨越 120 天. 60 日历日前的观测是 index 30.
        series = [((self.END - timedelta(days=2 * i)).isoformat(), 5.0 - 0.01 * i)
                  for i in range(61)]
        self.assertEqual(srg._delta_bps(series), 30.0)   # 旧实现按 60 个观测 → 60.0

    def test_delta_requires_observation_at_or_before_cutoff(self):
        series = [(self.END.isoformat(), 5.0),
                  ((self.END - timedelta(days=35)).isoformat(), 4.9)]
        self.assertIsNone(srg._delta_bps(series))

    def test_delta_empty_or_bad(self):
        self.assertIsNone(srg._delta_bps([]))
        self.assertIsNone(srg._delta_bps([("bad-date", 5.0), ("2026-01-01", 4.0)]))


class GuardState(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.state = self.td / "semi_risk_guard.json"
        thesis_config._CACHE = {"mtime": 0, "data": None}
        self.p = patch.object(thesis_config, "_GUARD_STATE_PATH", self.state)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def _write(self, active, triggered=()):
        self.state.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                          "active": active, "triggered": list(triggered)}),
                              encoding="utf-8")

    def test_no_state_means_no_extra_block(self):
        self.assertEqual(thesis_config.semi_risk_guard_state()["status"], "unknown")
        self.assertFalse(thesis_config.is_ticker_soft_blacklisted("US.SOXL")[0])

    def test_active_guard_soft_blocks_semis_only(self):
        self._write(True, ["hy_spread_widening"])
        soft, reason, meta = thesis_config.is_ticker_soft_blacklisted("SOXL")
        self.assertTrue(soft)
        self.assertEqual(meta["min_confidence"], 10)
        self.assertTrue(meta["semi_risk_guard"])
        self.assertIn("hy_spread_widening", reason)
        self.assertFalse(thesis_config.is_ticker_soft_blacklisted("US.MSFT")[0])
        # 静态 soft (NBIS min 7) 不受影响
        self.assertEqual(thesis_config.is_ticker_soft_blacklisted("US.NBIS")[2]["min_confidence"], 7)

    def test_inactive_guard(self):
        self._write(False)
        st = thesis_config.semi_risk_guard_state()
        self.assertEqual((st["status"], st["active"]), ("inactive", False))
        self.assertFalse(thesis_config.is_ticker_soft_blacklisted("US.SOXL")[0])

    def test_stale_active_is_preserved(self):
        self._write(True, ["hy_spread_widening"])
        old = time.time() - 5 * 86400
        os.utime(self.state, (old, old))
        st = thesis_config.semi_risk_guard_state()
        self.assertEqual(st["status"], "stale_active")
        self.assertTrue(st["active"])
        soft, reason, meta = thesis_config.is_ticker_soft_blacklisted("US.SOXL")
        self.assertTrue(soft)                       # 数据降级不释放已开启的开关
        self.assertTrue(meta["stale_active"])
        self.assertIn("状态文件过期", reason)

    def test_stale_inactive_is_unknown(self):
        self._write(False)
        old = time.time() - 5 * 86400
        os.utime(self.state, (old, old))
        st = thesis_config.semi_risk_guard_state()
        self.assertEqual((st["status"], st["active"]), ("unknown", False))
        self.assertFalse(thesis_config.is_ticker_soft_blacklisted("US.SOXL")[0])

    def test_decision_filter_raises_bar(self):
        from decision_agent import _apply_thesis_filter
        with patch.dict(os.environ, {"TECHNICAL_ONLY": "1"}):
            self._write(True, ["earnings_revisions_down"])
            out = _apply_thesis_filter({"action": "WATCH_BUY", "confidence": 4, "reason": "x"}, "US.SOXL")
            self.assertEqual(out["action"], "HOLD")
            self.assertEqual(out["min_confidence_required"], 5)          # 10/10 → 5 分制满分
            self.assertEqual(out["semi_risk_guard"], ["earnings_revisions_down"])
            out = _apply_thesis_filter({"action": "WATCH_BUY", "confidence": 5, "reason": "x"}, "US.SOXL")
            self.assertEqual(out["action"], "WATCH_BUY")
            self._write(False)
            out = _apply_thesis_filter({"action": "WATCH_BUY", "confidence": 3, "reason": "x"}, "US.SOXL")
            self.assertEqual(out["action"], "WATCH_BUY")

    def test_live_context_snapshot_includes_guard(self):
        from decision_context import from_live_now
        self._write(True, ["yield_5p5_real_rising"])
        ctx = from_live_now("US.SOXL")
        soft, _, meta = ctx.is_ticker_soft_blacklisted("US.SOXL")
        self.assertTrue(soft)
        self.assertEqual(meta["min_confidence"], 10)


class Sticky(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.state = Path(self.td.name) / "semi_risk_guard.json"

    def tearDown(self):
        self.td.cleanup()

    def _write(self, active, triggered=(), ts=None, **extra):
        self.state.write_text(json.dumps({
            "ts": ts or datetime.now(timezone.utc).isoformat(), "active": active,
            "triggered": list(triggered), **extra}), encoding="utf-8")

    def _run(self, fetch, notify=False):
        return srg.run(self.state, fetch=fetch, notify=notify)

    def test_unknown_carries_previous_active(self):
        self._write(True, ["hy_spread_widening"])
        st = self._run(lambda: ({}, {"DGS10": "error: timeout"}))
        self.assertTrue(st["active"])
        self.assertEqual(st["triggered"], ["hy_spread_widening"])
        self.assertEqual(st["carried"], ["hy_spread_widening"])
        self.assertTrue(st["data_degraded"])
        self.assertEqual(st["observed_triggered"], [])

    def test_unknown_does_not_invent_trigger(self):
        self._write(False)
        st = self._run(lambda: ({}, {"DGS10": "error: timeout"}))
        self.assertFalse(st["active"])
        self.assertEqual(st["triggered"], [])
        self.assertEqual(st["carried"], [])

    def test_complete_data_can_release(self):
        self._write(True, ["hy_spread_widening"])
        st = self._run(lambda: (OK, {}))
        self.assertFalse(st["active"])
        self.assertEqual(st["triggered"], [])
        self.assertEqual(st["carried"], [])

    def test_legacy_active_without_trigger_ids_stays_sticky(self):
        self._write(True, [])
        st1 = self._run(lambda: ({}, {"DGS10": "error: timeout"}))
        self.assertTrue(st1["active"])
        self.assertEqual(st1["carried"], ["prior_active"])
        sent = []
        class N:
            @staticmethod
            def send_alert(msg, **k):
                sent.append(msg)
        with patch.dict(sys.modules, {"notifications": N}):
            st2 = self._run(lambda: ({}, {"DGS10": "error: timeout"}), notify=True)
        self.assertTrue(st2["active"], "sentinel 必须在第二天继续承接")
        self.assertEqual(st2["carried"], ["prior_active"])
        self.assertFalse(any("解除" in m for m in sent))

    def test_partial_unknown_keeps_only_unknown_trigger(self):
        self._write(True, ["hy_spread_widening", "yield_5p5_real_rising"])
        metrics = {**OK, "dfii10_60d_delta_bps": None, "hy_oas_60d_delta_bps": 10.0}
        st = self._run(lambda: (metrics, {}))
        self.assertTrue(st["active"])
        self.assertEqual(st["triggered"], ["yield_5p5_real_rising"])
        self.assertEqual(st["carried"], ["yield_5p5_real_rising"])

    def test_alert_says_maintained_not_released(self):
        self._write(True, ["hy_spread_widening"])
        sent = []
        class N:
            @staticmethod
            def send_alert(msg, **k):
                sent.append(msg)
        with patch.dict(sys.modules, {"notifications": N}):
            self._run(lambda: ({}, {"DGS10": "error: timeout"}), notify=True)
        self.assertEqual(len(sent), 1)
        self.assertIn("维持开启", sent[0])
        self.assertNotIn("解除", sent[0])

    def test_manual_review_after_max_sticky_days(self):
        old = datetime.now(timezone.utc) - timedelta(days=20)
        self._write(True, ["hy_spread_widening"], ts=old.isoformat(),
                    sticky_since=old.isoformat())
        sent = []
        class N:
            @staticmethod
            def send_alert(msg, **k):
                sent.append(msg)
        with patch.dict(sys.modules, {"notifications": N}):
            st = self._run(lambda: ({}, {"DGS10": "error: timeout"}), notify=True)
        self.assertGreaterEqual(st["sticky_days"], 19)
        self.assertTrue(any("人工复核" in m for m in sent))

    def test_last_good_written_only_on_complete_data(self):
        self._run(lambda: ({}, {"DGS10": "error: timeout"}))
        self.assertFalse(srg._last_good_path(self.state).exists())
        self._run(lambda: ({**OK, "hy_oas_60d_delta_bps": 100}, {}))
        self.assertTrue(srg._last_good_path(self.state).exists())

    def test_corrupt_state_uses_last_good(self):
        self._run(lambda: ({**OK, "hy_oas_60d_delta_bps": 100}, {}))
        self.state.write_text("{not json", encoding="utf-8")
        st = self._run(lambda: ({}, {"DGS10": "error: timeout"}))
        self.assertTrue(st["active"])
        self.assertEqual(st["carried"], ["hy_spread_widening"])


class RunWritesState(unittest.TestCase):
    def test_run_writes_and_notifies_on_change(self):
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "g.json"
            sent = []
            class N:
                @staticmethod
                def send_alert(msg, **k):
                    sent.append(msg)
            with patch.dict(sys.modules, {"notifications": N}):
                srg.run(sp, fetch=lambda: (OK, {"x": "ok"}))
                st = srg.run(sp, fetch=lambda: ({**OK, "hy_oas_60d_delta_bps": 100}, {}))
                srg.run(sp, fetch=lambda: ({**OK, "hy_oas_60d_delta_bps": 100}, {}))
            self.assertTrue(st["active"])
            self.assertEqual(json.loads(sp.read_text(encoding="utf-8"))["triggered"], ["hy_spread_widening"])
            self.assertEqual(len(sent), 1)          # 只在状态变化时通知
            self.assertIn("开启", sent[0])

    def test_watchdog_job_registered(self):
        import _webui_watchdog as wd
        job = wd.JOBS["semi_risk_guard"]
        self.assertTrue((AGENTS_DIR / job["bat"]).exists())
        self.assertEqual(job["auto_every_days"], 1)
        self.assertTrue(job["market_quiet"])


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
