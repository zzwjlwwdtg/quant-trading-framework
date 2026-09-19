"""F12 / WP08 property tests (audit 2026-09-19): 杠杆 ETF 价格映射不变量.

audit 明确验收:
- 代理价 = 锚点 → 返回目标现价
- 同一 beta > 0 / 锚点下关键位单调
- 同一代理期末价但不同日收益路径得到不同杠杆结果 (path-dependent)
- 因子非正 / 输入无效 → 返 None 不返假价
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import webui


class AnchoringInvariantTests(unittest.TestCase):
    """proxy_level = proxy_spot → instant_level = leveraged_spot."""

    def test_proxy_at_anchor_returns_leveraged_spot(self):
        r = webui._convert_proxy_level(
            proxy_level=200.0, proxy_spot=200.0,
            leveraged_spot=70.0, leverage=3.0,
        )
        self.assertIsNotNone(r)
        # 无 proxy move → instant factor = 1.0 → level = leveraged_spot
        self.assertAlmostEqual(r["spot_anchored_level"], 70.0, places=2)
        self.assertAlmostEqual(r["source_move_pct"], 0.0, places=2)

    def test_2pct_proxy_move_yields_leverage_x_move(self):
        # SOXX +2% → SOXL 应约 +6% (3x)
        r = webui._convert_proxy_level(
            proxy_level=204.0, proxy_spot=200.0,   # +2% move
            leveraged_spot=70.0, leverage=3.0,
        )
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["spot_anchored_move_pct"], 6.0, places=1)
        # 70 * 1.06 = 74.2
        self.assertAlmostEqual(r["spot_anchored_level"], 74.2, places=1)


class MonotonicityTests(unittest.TestCase):
    """beta > 0 时 proxy_level 单调递增 → instant_level 也单调递增."""

    def test_monotonic_increase(self):
        levels = []
        for proxy in [180, 190, 200, 210, 220]:
            r = webui._convert_proxy_level(
                proxy_level=proxy, proxy_spot=200.0,
                leveraged_spot=70.0, leverage=3.0,
            )
            self.assertIsNotNone(r)
            levels.append(r["spot_anchored_level"])
        # 严格递增
        for a, b in zip(levels, levels[1:]):
            self.assertLess(a, b, f"expected strictly monotonic: {levels}")

    def test_monotonic_decrease_below_anchor(self):
        levels = []
        for proxy in [220, 210, 200, 190, 180]:
            r = webui._convert_proxy_level(
                proxy_level=proxy, proxy_spot=200.0,
                leveraged_spot=70.0, leverage=3.0,
            )
            self.assertIsNotNone(r)
            levels.append(r["spot_anchored_level"])
        # 严格递减
        for a, b in zip(levels, levels[1:]):
            self.assertGreater(a, b, f"expected strictly decreasing: {levels}")


class NegativeFactorGuardTests(unittest.TestCase):
    """proxy 大跌 → 3x 计算 factor 会小于 0, 不应硬截, 返 None."""

    def test_crash_beyond_leverage_returns_none(self):
        # 3x 下 proxy -40% → factor = 1 + 3*(-0.4) = -0.2 → invalid → None
        r = webui._convert_proxy_level(
            proxy_level=120.0, proxy_spot=200.0,   # -40%
            leveraged_spot=70.0, leverage=3.0,
        )
        # 应返 None 而非把 leveraged 价格算成负数
        self.assertIsNone(r,
                          "audit 关键: factor 非正应返 None, 不能生成假价格")

    def test_marginal_negative_returns_none(self):
        # factor 恰好 = 0
        # proxy -33.33% → 1 + 3*(-0.3333) = 0.0
        r = webui._convert_proxy_level(
            proxy_level=200.0 * (1 - 1/3), proxy_spot=200.0,
            leveraged_spot=70.0, leverage=3.0,
        )
        # factor <= 0 应返 None
        self.assertIsNone(r)


class InputValidationTests(unittest.TestCase):
    """无效输入 (None / 负数 / 字符串) 应 fail-safe."""

    def test_none_proxy_level_returns_none(self):
        r = webui._convert_proxy_level(None, 200.0, 70.0, 3.0)
        self.assertIsNone(r)

    def test_none_spot_returns_none(self):
        r = webui._convert_proxy_level(200.0, None, 70.0, 3.0)
        self.assertIsNone(r)

    def test_negative_leveraged_spot_returns_none(self):
        r = webui._convert_proxy_level(200.0, 200.0, -70.0, 3.0)
        self.assertIsNone(r)

    def test_zero_leverage_returns_none(self):
        r = webui._convert_proxy_level(200.0, 200.0, 70.0, 0.0)
        self.assertIsNone(r)

    def test_string_input_returns_none(self):
        r = webui._convert_proxy_level("not_a_number", 200.0, 70.0, 3.0)
        self.assertIsNone(r)


class LeveragedOptionMapConsistencyTests(unittest.TestCase):

    def test_tqqq_map_leverage_3x(self):
        # audit 提醒: SOXL/TQQQ 都是 3x, 不能被误配成其他倍数
        self.assertEqual(webui.LEVERAGED_OPTION_PRICE_MAP["TQQQ"]["leverage"], 3.0)
        self.assertEqual(webui.LEVERAGED_OPTION_PRICE_MAP["SOXL"]["leverage"], 3.0)

    def test_soxl_source_is_soxx_not_smh(self):
        # audit 强调: SMH 期权流可作代理, 但 SOXL 价格映射必须用 SOXX
        # (SMH 是 semi ETF 但不是 SOXL 严格 3x underlying, SOXX 才是)
        self.assertEqual(webui.LEVERAGED_OPTION_PRICE_MAP["SOXL"]["source"], "SOXX")

    def test_tqqq_source_is_qqq(self):
        self.assertEqual(webui.LEVERAGED_OPTION_PRICE_MAP["TQQQ"]["source"], "QQQ")


if __name__ == "__main__":
    unittest.main()
