"""验收用例：美元主线期限与市场分项核对 (EVO-20261107-1).

验收场景（提案锁定，不得删减）:
1. DXY 20日走弱与60日走强 — 期限分化必须分开展示且方向不同
2. FXI 相对收益为正 — 方向=相反（非美市场未跑输）
3. 亚洲市场分化 — 至少一个支持、一个相反
4. 全部同向（全支持）
5. 数值缺失 — verifiable=False，note 注明数值缺失
6. 数据时点缺失 — verifiable=False，note 注明时点缺失
7. 混合基准 — 有基准（DXY/ETF）vs 无跨市场基准（亚洲绝对涨跌）
8. 交易信号不变 — theme_strength / evidence_audit 互不干扰
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import usd_theme

AS_OF = "2026-11-19T10:00:00+00:00"


def _base_metrics(**overrides):
    m = {
        "dxy_20d_pct": 1.0,
        "dxy_60d_pct": 2.0,
        "eem_vs_spy_20d_pp": -1.0,
        "fxi_vs_spy_20d_pp": -0.5,
        "ewj_vs_spy_20d_pp": -0.3,
        "asia_5d_pct": {"N225": -1.0, "HSI": -0.5, "SSE": -0.8, "KOSPI": -0.3},
    }
    m.update(overrides)
    return m


class TestDxyPeriodDivergence(unittest.TestCase):
    """验收1: DXY 20日走弱 vs 60日走强 — 两条记录，方向相反。"""

    def test_dxy_20d_weak_60d_strong_shown_separately(self):
        m = _base_metrics(dxy_20d_pct=-1.5, dxy_60d_pct=3.2)
        rows = usd_theme.evidence_audit(m, AS_OF)
        dxy = [r for r in rows if r["name"] == "DXY 涨跌"]
        self.assertEqual(len(dxy), 2, "必须有 20日 和 60日 两条 DXY 记录")
        d20 = next(r for r in dxy if r["period"] == "20日")
        d60 = next(r for r in dxy if r["period"] == "60日")
        self.assertEqual(d20["direction"], "相反", "20日走弱应标注相反")
        self.assertEqual(d60["direction"], "支持", "60日走强应标注支持")
        self.assertTrue(d20["verifiable"])
        self.assertTrue(d60["verifiable"])
        self.assertNotEqual(d20["direction"], d60["direction"], "分化时两期方向必须不同")

    def test_dxy_values_stored_correctly(self):
        m = _base_metrics(dxy_20d_pct=-1.5, dxy_60d_pct=3.2)
        rows = usd_theme.evidence_audit(m, AS_OF)
        d20 = next(r for r in rows if r["name"] == "DXY 涨跌" and r["period"] == "20日")
        d60 = next(r for r in rows if r["name"] == "DXY 涨跌" and r["period"] == "60日")
        self.assertAlmostEqual(d20["value"], -1.5)
        self.assertAlmostEqual(d60["value"], 3.2)

    def test_dxy_benchmark_field_present(self):
        rows = usd_theme.evidence_audit(_base_metrics(), AS_OF)
        for r in [r for r in rows if r["name"] == "DXY 涨跌"]:
            self.assertIsNotNone(r["benchmark"])
            self.assertEqual(r["as_of"], AS_OF)


class TestFxiPositiveRelativeReturn(unittest.TestCase):
    """验收2: FXI 相对收益为正 → 方向=相反（非美市场未跑输）。"""

    def test_fxi_positive_excess_is_opposing(self):
        m = _base_metrics(fxi_vs_spy_20d_pp=2.5)
        rows = usd_theme.evidence_audit(m, AS_OF)
        fxi = next(r for r in rows if "FXI" in r["name"])
        self.assertEqual(fxi["direction"], "相反", "FXI 正超额=非美跑赢=相反主线")
        self.assertTrue(fxi["verifiable"])

    def test_fxi_negative_excess_is_supporting(self):
        m = _base_metrics(fxi_vs_spy_20d_pp=-1.8)
        rows = usd_theme.evidence_audit(m, AS_OF)
        fxi = next(r for r in rows if "FXI" in r["name"])
        self.assertEqual(fxi["direction"], "支持", "FXI 负超额=非美跑输=支持主线")

    def test_fxi_zero_excess_is_opposing(self):
        """超额=0 视为非美未跑输=相反。"""
        m = _base_metrics(fxi_vs_spy_20d_pp=0.0)
        rows = usd_theme.evidence_audit(m, AS_OF)
        fxi = next(r for r in rows if "FXI" in r["name"])
        self.assertEqual(fxi["direction"], "相反")


class TestAsianMarketDivergence(unittest.TestCase):
    """验收3: 亚洲市场分化 — 至少一个支持，一个相反。"""

    def test_divergence_shown_per_market(self):
        m = _base_metrics(asia_5d_pct={
            "N225": 1.5,   # 上涨=相反
            "HSI": -2.3,   # 下跌=支持
            "SSE": -1.5,   # 下跌=支持
            "KOSPI": 0.8,  # 上涨=相反
        })
        rows = usd_theme.evidence_audit(m, AS_OF)
        asia = [r for r in rows if any(k in r["name"] for k in ("N225", "HSI", "SSE", "KOSPI"))]
        self.assertEqual(len(asia), 4, "四个亚洲市场各独立一条记录")
        directions = {r["name"]: r["direction"] for r in asia if r["direction"] is not None}
        self.assertIn("支持", directions.values(), "应有至少一个支持方向")
        self.assertIn("相反", directions.values(), "应有至少一个相反方向")

    def test_all_asian_markets_have_no_cross_benchmark(self):
        rows = usd_theme.evidence_audit(_base_metrics(), AS_OF)
        asia = [r for r in rows if any(k in r["name"] for k in ("N225", "HSI", "SSE", "KOSPI"))]
        for r in asia:
            self.assertIsNone(r["benchmark"], "亚洲市场无跨市场比较基准")
            self.assertFalse(r["verifiable"], "无基准时 verifiable 应为 False")
            self.assertIsNotNone(r["note"])
            self.assertIn("无跨市场比较基准", r["note"])

    def test_asian_market_directions_still_computed_despite_no_benchmark(self):
        """无基准不影响方向计算，仅影响 verifiable。"""
        m = _base_metrics(asia_5d_pct={"N225": 1.0, "HSI": -1.0, "SSE": -0.5, "KOSPI": 0.5})
        rows = usd_theme.evidence_audit(m, AS_OF)
        n225 = next(r for r in rows if "N225" in r["name"])
        hsi = next(r for r in rows if "HSI" in r["name"])
        self.assertEqual(n225["direction"], "相反")
        self.assertEqual(hsi["direction"], "支持")


class TestAllSupporting(unittest.TestCase):
    """验收4: 全部有基准的指标同向（全支持）。"""

    def test_all_dxy_and_etf_supporting(self):
        m = _base_metrics(
            dxy_20d_pct=2.0, dxy_60d_pct=3.5,
            eem_vs_spy_20d_pp=-2.5, fxi_vs_spy_20d_pp=-1.8, ewj_vs_spy_20d_pp=-1.2,
        )
        rows = usd_theme.evidence_audit(m, AS_OF)
        benchmarked = [r for r in rows if r["benchmark"] is not None]
        for r in benchmarked:
            self.assertEqual(r["direction"], "支持",
                             f"{r['name']} {r['period']} 应为支持")


class TestMissingValues(unittest.TestCase):
    """验收5: 数值缺失时 verifiable=False，note 注明数值缺失。"""

    def test_none_dxy_20d_is_not_verifiable(self):
        m = _base_metrics(dxy_20d_pct=None)
        rows = usd_theme.evidence_audit(m, AS_OF)
        d20 = next(r for r in rows if r["name"] == "DXY 涨跌" and r["period"] == "20日")
        self.assertFalse(d20["verifiable"])
        self.assertIsNone(d20["direction"])
        self.assertIsNotNone(d20["note"])
        self.assertIn("数值缺失", d20["note"])

    def test_none_etf_excess_not_verifiable(self):
        m = _base_metrics(eem_vs_spy_20d_pp=None)
        rows = usd_theme.evidence_audit(m, AS_OF)
        eem = next(r for r in rows if "EEM" in r["name"])
        self.assertFalse(eem["verifiable"])
        self.assertIn("数值缺失", eem["note"])

    def test_none_asia_market_not_verifiable(self):
        m = _base_metrics(asia_5d_pct={"N225": None, "HSI": -1.0, "SSE": None, "KOSPI": -0.5})
        rows = usd_theme.evidence_audit(m, AS_OF)
        n225 = next(r for r in rows if "N225" in r["name"])
        self.assertFalse(n225["verifiable"])
        self.assertIn("数值缺失", n225["note"])

    def test_non_none_dxy_60d_still_verifiable(self):
        """仅 20日缺失时 60日不受影响。"""
        m = _base_metrics(dxy_20d_pct=None, dxy_60d_pct=2.5)
        rows = usd_theme.evidence_audit(m, AS_OF)
        d60 = next(r for r in rows if r["name"] == "DXY 涨跌" and r["period"] == "60日")
        self.assertTrue(d60["verifiable"])

    def test_empty_metrics_all_not_verifiable(self):
        rows = usd_theme.evidence_audit({}, AS_OF)
        for r in rows:
            self.assertFalse(r["verifiable"])


class TestMissingTimestamp(unittest.TestCase):
    """验收6: 数据时点缺失 (as_of=None) 时，有基准且有值的记录 verifiable=False。"""

    def test_missing_as_of_makes_benchmarked_rows_not_verifiable(self):
        m = _base_metrics()
        rows = usd_theme.evidence_audit(m, as_of=None)
        for r in rows:
            if r["benchmark"] is not None and r["value"] is not None:
                self.assertFalse(r["verifiable"],
                                 f"{r['name']} {r['period']}: 缺时点应为不可核对")
                self.assertIsNotNone(r["note"])
                self.assertIn("时点", r["note"])

    def test_direction_still_computed_without_timestamp(self):
        """缺时点时方向仍可计算，但 verifiable=False。"""
        m = _base_metrics(dxy_20d_pct=1.5)
        rows = usd_theme.evidence_audit(m, as_of=None)
        d20 = next(r for r in rows if r["name"] == "DXY 涨跌" and r["period"] == "20日")
        self.assertEqual(d20["direction"], "支持", "方向仍可从数值推导")
        self.assertFalse(d20["verifiable"])

    def test_as_of_field_is_none_in_rows(self):
        rows = usd_theme.evidence_audit(_base_metrics(), as_of=None)
        for r in rows:
            self.assertIsNone(r["as_of"])


class TestMixedBenchmark(unittest.TestCase):
    """验收7: 混合基准 — 有基准（DXY/ETF）vs 无跨市场基准（亚洲绝对涨跌）。"""

    def test_dxy_and_etf_have_benchmarks(self):
        m = _base_metrics()
        rows = usd_theme.evidence_audit(m, AS_OF)
        with_bm = [r for r in rows if r["benchmark"] is not None]
        self.assertGreaterEqual(len(with_bm), 5, "DXY×2 + ETF×3 = 至少5条有基准")

    def test_asian_markets_have_no_benchmark(self):
        m = _base_metrics()
        rows = usd_theme.evidence_audit(m, AS_OF)
        without_bm = [r for r in rows if r["benchmark"] is None]
        self.assertEqual(len(without_bm), 4, "四个亚洲市场无跨市场基准")

    def test_verifiable_requires_value_as_of_and_benchmark(self):
        """verifiable 当且仅当 value、as_of、benchmark 三者均非 None。"""
        m = _base_metrics()
        rows = usd_theme.evidence_audit(m, AS_OF)
        for r in rows:
            expected = (r["value"] is not None
                        and r["as_of"] is not None
                        and r["benchmark"] is not None)
            self.assertEqual(r["verifiable"], expected,
                             f"{r['name']} {r['period']}: verifiable 判断不一致")

    def test_note_is_none_when_fully_verifiable(self):
        m = _base_metrics()
        rows = usd_theme.evidence_audit(m, AS_OF)
        for r in rows:
            if r["verifiable"]:
                self.assertIsNone(r["note"])


class TestTradingSignalsUnchanged(unittest.TestCase):
    """验收8: evidence_audit 调用不影响 theme_strength 及主线失效结论 (提案要求)."""

    def test_theme_strength_unaffected_by_evidence_audit(self):
        m = {
            "dxy_20d_rank_1y": 0.92, "dxy_60d_rank_1y": 0.85,
            "eem_vs_spy_20d_pp": -2.0, "fxi_vs_spy_20d_pp": -1.5,
            "dxy_20d_pct": 2.0,
        }
        before = usd_theme.theme_strength(m)
        usd_theme.evidence_audit(m, AS_OF)
        after = usd_theme.theme_strength(m)
        self.assertEqual(before, after)

    def test_evidence_audit_does_not_mutate_metrics(self):
        m = _base_metrics()
        m_before = copy.deepcopy(m)
        usd_theme.evidence_audit(m, AS_OF)
        self.assertEqual(m, m_before, "evidence_audit 不得修改传入的 metrics 字典")

    def test_all_original_theme_strength_cases_unchanged(self):
        """原有 theme_strength 四种结论边界不变。"""
        # 兑现中
        m1 = {"dxy_20d_rank_1y": 0.92, "dxy_60d_rank_1y": 0.85,
               "eem_vs_spy_20d_pp": -2.0, "fxi_vs_spy_20d_pp": -1.5, "dxy_20d_pct": 2.0}
        self.assertEqual(usd_theme.theme_strength(m1), "兑现中: 美元强势且非美市场跑输")
        # 美元强势但非美未明显跑输
        m2 = {"dxy_20d_rank_1y": 0.92, "dxy_60d_rank_1y": 0.85,
               "eem_vs_spy_20d_pp": 1.0, "fxi_vs_spy_20d_pp": 0.5, "dxy_20d_pct": 2.0}
        self.assertEqual(usd_theme.theme_strength(m2), "美元强势, 但非美市场尚未明显跑输")
        # 美元回落
        m3 = {"dxy_20d_rank_1y": 0.5, "dxy_60d_rank_1y": 0.5,
               "eem_vs_spy_20d_pp": -1.0, "dxy_20d_pct": -1.0}
        self.assertEqual(usd_theme.theme_strength(m3), "美元回落, 主线暂未兑现")
        # 中性
        m4 = {"dxy_20d_rank_1y": 0.5, "dxy_20d_pct": 0.5}
        self.assertEqual(usd_theme.theme_strength(m4), "中性")
        # 数据不足
        m5 = {"dxy_20d_pct": 1.0}
        self.assertEqual(usd_theme.theme_strength(m5), "数据不足")


class TestReturnStructure(unittest.TestCase):
    """每条记录必须包含提案要求的全部字段。"""

    def test_required_keys_present_in_every_row(self):
        required = {"name", "period", "benchmark", "value", "as_of",
                    "direction", "verifiable", "note"}
        rows = usd_theme.evidence_audit(_base_metrics(), AS_OF)
        self.assertGreater(len(rows), 0)
        for r in rows:
            self.assertEqual(required, set(r.keys()),
                             f"{r.get('name')}: 字段集合不符")

    def test_total_row_count(self):
        """DXY×2 + ETF×3 + 亚洲×4 = 9 条。"""
        rows = usd_theme.evidence_audit(_base_metrics(), AS_OF)
        self.assertEqual(len(rows), 9)


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
