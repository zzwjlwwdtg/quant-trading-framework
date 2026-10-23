"""账户收益曲线 (2026-10-06): 按美东交易日取值、断档标记、SPY 同起点对比、公开快照."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import equity_curve as ec


def _row(ts, nav, peak=200.0, dd=-10.0):
    return {"ts": ts, "nav": nav, "peak": peak, "dd_pct": dd}


ROWS = [
    _row("2026-07-01T13:00:00+00:00", 90.0),
    _row("2026-07-01T19:45:00+00:00", 100.0),   # 7/1 ET 最后一条
    _row("2026-07-02T02:00:00+00:00", 101.0),   # = 7/1 22:00 ET → 仍属 7/1
    _row("2026-07-02T19:45:00+00:00", 110.0),
    _row("2026-07-04T19:45:00+00:00", 110.0),   # 周六 → 丢弃
    _row("2026-07-20T19:45:00+00:00", 88.0),    # 断档 18 天
    _row("2026-07-21T19:45:00+00:00", 99.0, dd=-50.5),
    {"ts": "bad", "nav": 1},
    _row("2026-07-22T19:45:00+00:00", -1),      # 非法净值丢弃
]
SPY = {"2026-07-01": 500.0, "2026-07-02": 505.0, "2026-07-17": 490.0, "2026-07-21": 510.0}


class EquityCurve(unittest.TestCase):
    def test_daily_grouping_by_et_and_weekend_drop(self):
        days = ec.daily_nav(ROWS)
        self.assertEqual([d["date"] for d in days],
                         ["2026-07-01", "2026-07-02", "2026-07-20", "2026-07-21"])
        self.assertEqual(days[0]["nav"], 101.0)

    def test_returns_gaps_and_drawdown(self):
        r = ec.build_equity_curve(ROWS, SPY)
        self.assertTrue(r["exists"])
        self.assertEqual(r["start_nav"], 101.0)
        self.assertAlmostEqual(r["period_return_pct"], (99 / 101 - 1) * 100, places=2)
        # 记录期内峰值 110 → 低点 88 = -20%
        self.assertAlmostEqual(r["max_dd_in_record_pct"], -20.0, places=2)
        self.assertEqual(r["max_dd_date"], "2026-07-20")
        self.assertEqual(r["gaps"], [{"from": "2026-07-02", "to": "2026-07-20", "days": 18}])
        self.assertEqual(r["points"][2]["gap_before_days"], 18)
        self.assertEqual(r["points"][1]["gap_before_days"], 0)
        # 历史峰值与"距峰值"来自原记录, 不和记录期回撤混在一起
        self.assertEqual(r["all_time_peak"], 200.0)
        self.assertEqual(r["dd_from_all_time_peak_pct"], -50.5)

    def test_spy_scaled_to_same_start_and_holiday_fallback(self):
        r = ec.build_equity_curve(ROWS, SPY)
        p = r["points"]
        self.assertEqual(p[0]["spy_scaled"], 101.0)
        # 7/20 没有 SPY 价 → 用之前最近的 7/17
        self.assertAlmostEqual(p[2]["spy_scaled"], 101 * 490 / 500, places=2)
        self.assertAlmostEqual(r["spy_return_pct"], 2.0, places=2)
        self.assertAlmostEqual(r["excess_pp"], r["period_return_pct"] - 2.0, places=2)

    def test_without_benchmark(self):
        r = ec.build_equity_curve(ROWS, None)
        self.assertIsNone(r["spy_return_pct"])
        self.assertIsNone(r["points"][0]["spy_scaled"])
        self.assertEqual(ec.build_equity_curve([], None), {"exists": False, "points": []})

    def test_spy_fetch_failure_falls_back_to_old_cache(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "spy.json"
            cache.write_text(json.dumps({"fetched_at": 0, "start": "2026-01-01",
                                         "closes": {"2026-07-01": 1.0}}), encoding="utf-8")
            fake = mock.MagicMock()
            fake.Ticker.side_effect = RuntimeError("offline")
            with mock.patch.dict(sys.modules, {"yfinance": fake}):
                self.assertEqual(ec.fetch_spy_closes("2026-07-01", cache), {"2026-07-01": 1.0})

    def test_public_snapshot_and_dashboard(self):
        import snapshot_generator as sg
        self.assertIn("/api/equity_curve", sg.GLOBAL_ENDPOINTS)
        self.assertNotIn("/api/equity_curve", sg.PRIVATE_ENDPOINTS)
        self.assertIn("/api/nav", sg.PRIVATE_ENDPOINTS)
        html = (AGENTS_DIR / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('id="equity-chart"', html)
        # 卡片在持仓卡之后、其他卡之前; 且在公开 promises 里加载
        self.assertLess(html.index('id="positions"'), html.index('id="equity-chart"'))
        self.assertLess(html.index('id="equity-chart"'), html.index('id="top-picks"'))
        promises = html[html.index("async function loadAll()"):html.index("// Owner-only endpoints")]
        self.assertIn("loadEquityCurve()", promises)


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
