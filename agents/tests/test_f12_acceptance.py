"""F12 (MODEL_AUDIT 2026-09-19) 端到端一致性验收.

1. 角色分离: SOXL 期权流 = SMH 主 (流动性) + SOXX 确认; 杠杆价格映射 = SOXX.
   instrument_registry 的 price_proxy / options_proxy 必须与两张表一致;
   用 SMH 期权链不得生成 SOXL 挂单价.
2. 关键位同一锚点/同一时刻: 一份映射里所有关键位共用同一对现价, 并记录两边
   现价的来源与观测时间; 来源是兜底 (strike 中位数 / 信号文件) 或两边观测
   日期不同 → 不生成可挂单价.
3. 单调 + 历史残差只计一次 + 远期估值有路径范围.
4. 看板严格区分 候选 / 待触发计划 / 已提交 / 已成交; broker 不可用时不冒充
   "候选"; 决策时间 / 行情观测时间 / broker 查询时间 / 成交时间分开给出.
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import webui
import option_flow
import instrument_registry as ir

DECAY = {"available": True, "combined_residual_log_daily": -0.001,
         "residual_std_daily_pct": 1.0}
ANCHOR_OK = dict(proxy_spot_ts="2026-10-01T19:59:00+00:00", proxy_spot_source="yfinance_history",
                 leveraged_spot_ts="2026-10-01T19:59:30+00:00", leveraged_spot_source="yfinance_history")
LEVELS = {"put_wall": 190.0, "pin": 200.0, "lower_support": 195.0, "call_wall": 215.0,
          "upper_resistance": 210.0}


def _map(**kw):
    args = dict(ticker="SOXL", source="SOXX", proxy_spot=200.0, leveraged_spot=30.0,
                expiry="2099-01-16", decay=DECAY, levels=dict(LEVELS), **ANCHOR_OK)
    args.update(kw)
    return webui._build_leveraged_option_mapping(**args)


class RoleSeparation(unittest.TestCase):
    def test_soxl_option_flow_roles(self):
        srcs = {d["source"]: d for d in option_flow.POSITION_PROXY_MAP["SOXL"]}
        self.assertEqual(srcs["SMH"]["weight"], 1.0)
        self.assertIn("liquid", srcs["SMH"]["role"])
        self.assertIn("confirmation", srcs["SOXX"]["role"])

    def test_registry_matches_runtime_maps(self):
        for lev, cfg in webui.LEVERAGED_OPTION_PRICE_MAP.items():
            inst = ir.get(f"US.{lev}") if hasattr(ir, "get") else ir.REGISTRY[f"US.{lev}"]
            if inst and inst.price_proxy:
                self.assertEqual(inst.price_proxy, f"US.{cfg['source']}", lev)
        soxl = ir.get("US.SOXL") if hasattr(ir, "get") else ir.REGISTRY["US.SOXL"]
        primary = max(option_flow.POSITION_PROXY_MAP["SOXL"], key=lambda d: d["weight"])
        self.assertEqual(soxl.options_proxy, f"US.{primary['source']}")
        self.assertNotEqual(soxl.options_proxy, soxl.price_proxy)

    def test_smh_chain_never_maps_to_soxl_price(self):
        self.assertIsNone(_map(source="SMH"))


class SameAnchor(unittest.TestCase):
    def test_all_levels_share_one_anchor_and_record_it(self):
        m = _map()
        a = m["anchor"]
        self.assertEqual((a["proxy_spot"], a["leveraged_spot"]), (200.0, 30.0))
        self.assertEqual(a["proxy_spot_ts"], ANCHOR_OK["proxy_spot_ts"])
        self.assertEqual(a["leveraged_spot_ts"], ANCHOR_OK["leveraged_spot_ts"])
        self.assertLessEqual(a["skew_seconds"], 60)
        # 每个关键位都能由这一对锚点独立复算
        for k, v in m["levels"].items():
            exp = 30.0 * (1 + 3 * (LEVELS[k] / 200.0 - 1))
            self.assertAlmostEqual(v["spot_anchored_level"], round(exp, 2), places=2, msg=k)

    def test_fallback_spot_rejected(self):
        for kw in ({"proxy_spot_source": "strike_median_fallback"},
                   {"leveraged_spot_source": "signal_file_fallback"}):
            self.assertIsNone(_map(**kw), kw)

    def test_anchor_from_different_sessions_rejected(self):
        self.assertIsNone(_map(leveraged_spot_ts="2026-09-30T19:59:00+00:00"))

    def test_missing_anchor_ts_rejected(self):
        self.assertIsNone(_map(proxy_spot_ts=None))


class LevelsMath(unittest.TestCase):
    def test_monotonic_instant_and_expiry(self):
        m = _map()
        rows = sorted(m["levels"].values(), key=lambda r: r["source_level"])
        inst = [r["spot_anchored_level"] for r in rows]
        expi = [r["expiry_estimate"] for r in rows]
        self.assertEqual(inst, sorted(inst))
        self.assertEqual(expi, sorted(expi))

    def test_residual_counted_once(self):
        r = webui._convert_proxy_level(200.0, 200.0, 30.0, 3.0, calendar_days=365, decay=DECAY)
        n = r["estimated_trading_days"]
        self.assertEqual(n, 252)
        self.assertAlmostEqual(r["expiry_estimate"], round(30.0 * math.exp(-0.001 * n), 2), places=2)
        r2 = webui._convert_proxy_level(220.0, 200.0, 30.0, 3.0, calendar_days=365, decay=DECAY)
        want = 30.0 * math.exp(3 * math.log(1.1) - 0.001 * n)
        self.assertAlmostEqual(r2["expiry_estimate"], round(want, 2), places=2)

    def test_forward_valuation_has_path_range_widening_with_time(self):
        a = webui._convert_proxy_level(210.0, 200.0, 30.0, 3.0, calendar_days=10, decay=DECAY)
        b = webui._convert_proxy_level(210.0, 200.0, 30.0, 3.0, calendar_days=60, decay=DECAY)
        for r in (a, b):
            self.assertLess(r["expiry_range_low"], r["expiry_estimate"])
            self.assertGreater(r["expiry_range_high"], r["expiry_estimate"])
        self.assertGreater(b["expiry_range_high"] - b["expiry_range_low"],
                           a["expiry_range_high"] - a["expiry_range_low"])


BUY = {"BUY", "WATCH_BUY", "WATCH_BUY_PROBE", "CRISIS_PROBE"}


def _cls(**kw):
    base = dict(broker_ok=True, ticker_orders=[], positions_ok=True, has_position=False,
                system_action="WATCH_BUY", ai_buy_plan=False, entry_ref=None, price=30.0,
                last_fill_ts=None, decision_ts="2026-10-01T14:00:00+00:00", buy_actions=BUY)
    base.update(kw)
    return webui._classify_execution_status(**base)


class DashboardStates(unittest.TestCase):
    def test_candidate(self):
        self.assertEqual(_cls(), "candidate")

    def test_pending_trigger_when_plan_price_not_reached(self):
        self.assertEqual(_cls(ai_buy_plan=True, entry_ref=28.0, price=30.0), "pending_trigger")
        self.assertEqual(_cls(ai_buy_plan=True, entry_ref=31.0, price=30.0), "candidate")

    def test_submitted_and_partial(self):
        self.assertEqual(_cls(ticker_orders=[{"dealt_qty": 0}]), "submitted")
        self.assertEqual(_cls(ticker_orders=[{"dealt_qty": 5}]), "partially_filled")

    def test_filled_since_decision(self):
        self.assertEqual(_cls(has_position=True, last_fill_ts="2026-10-01T14:30:00+00:00"), "filled")
        self.assertEqual(_cls(has_position=True, last_fill_ts="2026-09-01T14:30:00+00:00"),
                         "position_open")

    def test_broker_unknown_never_shown_as_candidate(self):
        self.assertEqual(_cls(broker_ok=False), "broker_unknown")
        self.assertEqual(_cls(positions_ok=False, system_action="HOLD"), "broker_unknown")

    def test_inactive(self):
        self.assertEqual(_cls(system_action="HOLD"), "inactive")
        self.assertEqual(_cls(system_action="HOLD", ai_buy_plan=True, entry_ref=28.0),
                         "inactive_signal")

    def test_dashboard_labels_every_status(self):
        html = (AGENTS_DIR / "dashboard.html").read_text(encoding="utf-8")
        for s in ("candidate", "pending_trigger", "submitted", "partially_filled", "filled",
                  "position_open", "broker_unknown", "inactive_signal", "inactive"):
            self.assertRegex(html, rf"\b{s}\s*:", s)

    def test_times_kept_separate(self):
        t = webui._row_times(signal={"ts": "2026-10-02T04:48:45", "market": {"ts": "2026-10-02 04:48"}},
                             signal_mtime=1790000000.0, broker_checked_at="2026-10-02T00:00:00+00:00",
                             last_fill_ts=None)
        self.assertEqual(set(t), {"decision_written_at", "market_observed_at",
                                  "broker_checked_at", "last_fill_at"})
        self.assertEqual(t["market_observed_at"], "2026-10-02 04:48")
        self.assertTrue(t["decision_written_at"].endswith("+00:00"))


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
