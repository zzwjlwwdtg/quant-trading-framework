"""EVO-20261122-2 (2026-11-23): 日股/台股 symbol 路由与 404 升级测试。

覆盖:
  · data_feeds.is_yahoo_safe       — 非美市场符号返回 False
  · data_feeds.get_datasource_for  — JP/TW/HK/CN 符号路由到 moomoo
  · data_feeds.infer_market        — 市场推断
  · moomoo_data.to_openD_code      — .TW/.SS/.SZ 新增转换 regression
  · moomoo_data.is_non_us_market   — 非美市场检测
  · instrument_registry.normalize  — 非美后缀推断市场前缀
  · _data_source_health.record_routing_error — 路由错误升级为 data_quality 事件

regression: 3688.T / 6488.TW 不再触发 Yahoo quoteSummary（由 is_yahoo_safe 拦截）
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import data_feeds as df
import moomoo_data as md
import instrument_registry as ir
import _data_source_health as dsh


# ─────────────────────────────────────────────────────────────────────────────
# data_feeds.is_yahoo_safe
# ─────────────────────────────────────────────────────────────────────────────

class IsYahooSafeTests(unittest.TestCase):
    """非美市场符号必须返回 False，防止盲目走 Yahoo quoteSummary。"""

    def test_us_tickers_are_yahoo_safe(self):
        for sym in ("NVDA", "TQQQ", "SPY", "^VIX", "BTC-USD", "CL=F", "DX-Y.NYB"):
            with self.subTest(sym=sym):
                self.assertTrue(df.is_yahoo_safe(sym), f"{sym} 应对 Yahoo 安全")

    def test_jp_dot_t_is_not_safe(self):
        """3688.T 是 regression 案例 — 必须拦截。"""
        self.assertFalse(df.is_yahoo_safe("3688.T"))
        self.assertFalse(df.is_yahoo_safe("6762.T"))
        self.assertFalse(df.is_yahoo_safe("7203.T"))

    def test_tw_dot_tw_is_not_safe(self):
        """6488.TW 是 regression 案例 — 必须拦截。"""
        self.assertFalse(df.is_yahoo_safe("6488.TW"))
        self.assertFalse(df.is_yahoo_safe("2330.TW"))  # TSMC 台湾挂牌

    def test_hk_is_not_safe(self):
        self.assertFalse(df.is_yahoo_safe("00700.HK"))
        self.assertFalse(df.is_yahoo_safe("9988.HK"))

    def test_cn_a_share_is_not_safe(self):
        self.assertFalse(df.is_yahoo_safe("600000.SS"))   # 上证
        self.assertFalse(df.is_yahoo_safe("000001.SZ"))   # 深证

    def test_opend_prefix_is_not_safe(self):
        """已带 openD 前缀（非 US）也不应走 Yahoo。"""
        self.assertFalse(df.is_yahoo_safe("JP.6762"))
        self.assertFalse(df.is_yahoo_safe("HK.00700"))
        self.assertFalse(df.is_yahoo_safe("TW.6488"))

    def test_empty_is_safe_by_default(self):
        self.assertTrue(df.is_yahoo_safe(""))
        self.assertTrue(df.is_yahoo_safe(None))


# ─────────────────────────────────────────────────────────────────────────────
# data_feeds.get_datasource_for
# ─────────────────────────────────────────────────────────────────────────────

class GetDatasourceForTests(unittest.TestCase):

    def test_us_tickers_route_to_yahoo(self):
        for sym in ("NVDA", "SPY", "TQQQ", "^VIX", "BTC-USD"):
            with self.subTest(sym=sym):
                self.assertEqual(df.get_datasource_for(sym), "yahoo")

    def test_jp_routes_to_moomoo(self):
        self.assertEqual(df.get_datasource_for("3688.T"), "moomoo")
        self.assertEqual(df.get_datasource_for("6762.T"), "moomoo")

    def test_tw_routes_to_moomoo(self):
        self.assertEqual(df.get_datasource_for("6488.TW"), "moomoo")
        self.assertEqual(df.get_datasource_for("2330.TW"), "moomoo")

    def test_hk_routes_to_moomoo(self):
        self.assertEqual(df.get_datasource_for("00700.HK"), "moomoo")

    def test_cn_routes_to_moomoo(self):
        self.assertEqual(df.get_datasource_for("600000.SS"), "moomoo")
        self.assertEqual(df.get_datasource_for("000001.SZ"), "moomoo")

    def test_opend_prefix_routes_to_moomoo(self):
        self.assertEqual(df.get_datasource_for("JP.6762"), "moomoo")
        self.assertEqual(df.get_datasource_for("HK.00700"), "moomoo")


# ─────────────────────────────────────────────────────────────────────────────
# data_feeds.infer_market
# ─────────────────────────────────────────────────────────────────────────────

class InferMarketTests(unittest.TestCase):

    def test_us_default(self):
        self.assertEqual(df.infer_market("NVDA"), "US")
        self.assertEqual(df.infer_market("TQQQ"), "US")

    def test_jp_inferred(self):
        self.assertEqual(df.infer_market("3688.T"), "JP")
        self.assertEqual(df.infer_market("JP.3688"), "JP")

    def test_tw_inferred(self):
        self.assertEqual(df.infer_market("6488.TW"), "TW")
        self.assertEqual(df.infer_market("TW.6488"), "TW")

    def test_hk_inferred(self):
        self.assertEqual(df.infer_market("00700.HK"), "HK")

    def test_cn_inferred(self):
        self.assertEqual(df.infer_market("600000.SS"), "SH")
        self.assertEqual(df.infer_market("000001.SZ"), "SZ")


# ─────────────────────────────────────────────────────────────────────────────
# moomoo_data.to_openD_code — regression + 新增市场格式
# ─────────────────────────────────────────────────────────────────────────────

class ToOpenDCodeTests(unittest.TestCase):

    def test_us_bare(self):
        self.assertEqual(md.to_openD_code("NVDA"), "US.NVDA")

    def test_us_already_prefixed(self):
        self.assertEqual(md.to_openD_code("US.NVDA"), "US.NVDA")

    def test_jp_dot_t(self):
        """3688.T regression — 必须转为 JP.3688。"""
        self.assertEqual(md.to_openD_code("3688.T"),  "JP.3688")
        self.assertEqual(md.to_openD_code("6762.T"),  "JP.6762")
        self.assertEqual(md.to_openD_code("285A.T"),  "JP.285A")

    def test_tw_dot_tw(self):
        """6488.TW regression — 之前落入 US 分支返 None/错误，现在必须转 TW.6488。"""
        self.assertEqual(md.to_openD_code("6488.TW"), "TW.6488")
        self.assertEqual(md.to_openD_code("2330.TW"), "TW.2330")

    def test_hk_dot_hk(self):
        self.assertEqual(md.to_openD_code("00700.HK"), "HK.00700")
        self.assertEqual(md.to_openD_code("9988.HK"),  "HK.9988")

    def test_cn_ss_to_sh(self):
        self.assertEqual(md.to_openD_code("600000.SS"), "SH.600000")

    def test_cn_sz(self):
        self.assertEqual(md.to_openD_code("000001.SZ"), "SZ.000001")

    def test_already_opend_format_preserved(self):
        self.assertEqual(md.to_openD_code("JP.6762"), "JP.6762")
        self.assertEqual(md.to_openD_code("HK.00700"), "HK.00700")
        self.assertEqual(md.to_openD_code("TW.6488"), "TW.6488")
        self.assertEqual(md.to_openD_code("SH.600000"), "SH.600000")

    def test_empty_returns_none(self):
        self.assertIsNone(md.to_openD_code(""))
        self.assertIsNone(md.to_openD_code(None))

    def test_tw_no_longer_produces_us_prefix(self):
        """6488.TW 旧行为: 返回 US.6488.TW（错）→ 现在必须返回 TW.6488（对）。"""
        result = md.to_openD_code("6488.TW")
        self.assertNotEqual(result, "US.6488.TW",
                             "6488.TW 不能被路由为 US 市场（regression）")
        self.assertEqual(result, "TW.6488")


# ─────────────────────────────────────────────────────────────────────────────
# moomoo_data.is_non_us_market
# ─────────────────────────────────────────────────────────────────────────────

class IsNonUsMarketTests(unittest.TestCase):

    def test_us_tickers_are_us(self):
        for sym in ("NVDA", "US.NVDA", "TQQQ", "SPY"):
            with self.subTest(sym=sym):
                self.assertFalse(md.is_non_us_market(sym))

    def test_jp_is_non_us(self):
        self.assertTrue(md.is_non_us_market("3688.T"))
        self.assertTrue(md.is_non_us_market("JP.3688"))

    def test_tw_is_non_us(self):
        self.assertTrue(md.is_non_us_market("6488.TW"))
        self.assertTrue(md.is_non_us_market("TW.6488"))

    def test_hk_is_non_us(self):
        self.assertTrue(md.is_non_us_market("00700.HK"))
        self.assertTrue(md.is_non_us_market("HK.00700"))

    def test_cn_is_non_us(self):
        self.assertTrue(md.is_non_us_market("600000.SS"))
        self.assertTrue(md.is_non_us_market("SH.600000"))
        self.assertTrue(md.is_non_us_market("000001.SZ"))
        self.assertTrue(md.is_non_us_market("SZ.000001"))

    def test_empty(self):
        self.assertFalse(md.is_non_us_market(""))
        self.assertFalse(md.is_non_us_market(None))


# ─────────────────────────────────────────────────────────────────────────────
# instrument_registry.normalize — 非美市场后缀推断
# ─────────────────────────────────────────────────────────────────────────────

class RegistryNormalizeNonUsTests(unittest.TestCase):

    def test_jp_dot_t_infers_jp_prefix(self):
        self.assertEqual(ir.normalize("3688.T"), "JP.3688")
        self.assertEqual(ir.normalize("6762.t"), "JP.6762")  # 小写

    def test_tw_dot_tw_infers_tw_prefix(self):
        self.assertEqual(ir.normalize("6488.TW"), "TW.6488")
        self.assertEqual(ir.normalize("2330.TW"), "TW.2330")

    def test_hk_dot_hk_infers_hk_prefix(self):
        self.assertEqual(ir.normalize("00700.HK"), "HK.00700")

    def test_cn_ss_infers_sh_prefix(self):
        self.assertEqual(ir.normalize("600000.SS"), "SH.600000")

    def test_cn_sz_infers_sz_prefix(self):
        self.assertEqual(ir.normalize("000001.SZ"), "SZ.000001")

    def test_already_prefixed_preserved(self):
        self.assertEqual(ir.normalize("JP.6762"), "JP.6762")
        self.assertEqual(ir.normalize("TW.6488"), "TW.6488")
        self.assertEqual(ir.normalize("HK.00700"), "HK.00700")

    def test_us_bare_still_gets_us_prefix(self):
        self.assertEqual(ir.normalize("SOXL"), "US.SOXL")
        self.assertEqual(ir.normalize("NVDA"), "US.NVDA")

    def test_tw_normalize_no_longer_gives_us_prefix(self):
        """6488.TW 旧行为可能返回 US.6488.TW（错）→ 现在必须是 TW.6488。"""
        result = ir.normalize("6488.TW")
        self.assertFalse(result.startswith("US."),
                         f"6488.TW 不应被归类为 US 市场，得到: {result}")
        self.assertEqual(result, "TW.6488")


# ─────────────────────────────────────────────────────────────────────────────
# _data_source_health.record_routing_error
# ─────────────────────────────────────────────────────────────────────────────

class RecordRoutingErrorTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "retired_symbols.json"
        self._patch = patch.object(dsh, "_retired_state_path",
                                    return_value=self.path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_routing_error_records_as_miss(self):
        """routing_error 应在 retired_symbols.json 中留下记录。"""
        base = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
        result = dsh.record_routing_error(
            "6488.TW", source_attempted="yahoo",
            reason="non_us_to_yahoo", now=base, path=self.path,
        )
        self.assertEqual(result["symbol"], "6488.TW")
        self.assertIn(result["status"], ("watching", "retired"))
        self.assertGreaterEqual(result["miss_days"], 1)

    def test_routing_error_threshold_one_day(self):
        """routing_error 阈值 1 天 — 一次就打 retired 或 watching。"""
        base = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
        result = dsh.record_routing_error(
            "3688.T", source_attempted="yahoo", now=base, path=self.path,
        )
        # threshold=1 → 第 1 次 miss_days=1 ≥ threshold → retired
        self.assertEqual(result["miss_days"], 1)
        # 验证 newly_retired 置位（1 次即达到阈值）
        self.assertTrue(result.get("newly_retired"),
                        "routing_error 阈值=1，第一次记录应设 newly_retired=True")

    def test_routing_error_reason_stored(self):
        """routing_error 的原因应写入 retired_symbols.json。"""
        base = datetime(2026, 11, 22, tzinfo=timezone.utc)
        dsh.record_routing_error("6488.TW", reason="test_reason",
                                  now=base, path=self.path)
        import json
        state = json.loads(self.path.read_text(encoding="utf-8"))
        sym_entry = state["symbols"].get("6488.TW") or {}
        self.assertIn("routing_error", sym_entry.get("reason", ""))

    def test_regression_3688_T_and_6488_TW(self):
        """3688.T / 6488.TW 经 routing_error 路径后均可被 is_retired 检测到。"""
        base = datetime(2026, 11, 22, tzinfo=timezone.utc)
        dsh.record_routing_error("3688.T",  now=base, path=self.path)
        dsh.record_routing_error("6488.TW", now=base, path=self.path)
        self.assertTrue(dsh.is_retired("3688.T",  self.path))
        self.assertTrue(dsh.is_retired("6488.TW", self.path))


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
