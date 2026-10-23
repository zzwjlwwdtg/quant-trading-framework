"""EVO-20261014-3 (2026-10-19): 退市/404 proxy 符号自动隔离 + PCA/data_quality
对缺数据鲁棒化的回归测试.

覆盖:
  · _data_source_health: 连续 N 天 404 自动打 retired; 同一天内多次 miss 只
    累加 1 次; 命中成功会清零并复活
  · pca_sox.fetch_returns: 读 retired list, 剔除后以 N-k 维继续; 不整轮跳过
  · data_quality.audit_latest_signal_files: retired 文件单独计数, 不计 critical
  · moomoo_pool.is_symbol_retired: 单一入口返回 True
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import _data_source_health as dsh  # noqa: E402
import data_quality  # noqa: E402


class RetiredStateTests(unittest.TestCase):
    """_data_source_health 的 retired 登记簿基础行为."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "retired_symbols.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_retired_empty_when_no_file(self):
        self.assertEqual(dsh.list_retired(self.path), [])
        self.assertFalse(dsh.is_retired("6488.TW", self.path))

    def test_three_consecutive_days_promote_to_retired(self):
        base = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        for i in range(3):
            r = dsh.record_fetch_result(
                "6488.TW", False, reason="404",
                now=base + timedelta(days=i), path=self.path,
            )
        self.assertEqual(r["status"], "retired")
        self.assertTrue(r["newly_retired"])
        self.assertEqual(r["miss_days"], 3)
        self.assertIn("6488.TW", dsh.list_retired(self.path))

    def test_same_day_multiple_misses_count_once(self):
        base = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        # 高频调用 10 次都是同一天, 不应 trigger retire
        for i in range(10):
            r = dsh.record_fetch_result(
                "6488.TW", False, now=base + timedelta(minutes=i),
                path=self.path,
            )
        self.assertEqual(r["status"], "watching")
        self.assertEqual(r["miss_days"], 1)
        self.assertFalse(dsh.is_retired("6488.TW", self.path))

    def test_success_resets_and_revives_retired(self):
        base = datetime(2026, 10, 10, tzinfo=timezone.utc)
        # 先 retire
        for i in range(3):
            dsh.record_fetch_result(
                "6488.TW", False, now=base + timedelta(days=i),
                path=self.path,
            )
        self.assertTrue(dsh.is_retired("6488.TW", self.path))
        # 第 4 天成功 → 清零并复活
        r = dsh.record_fetch_result(
            "6488.TW", True, now=base + timedelta(days=4), path=self.path,
        )
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["miss_days"], 0)
        self.assertFalse(dsh.is_retired("6488.TW", self.path))

    def test_newly_retired_flag_only_fires_once(self):
        base = datetime(2026, 10, 10, tzinfo=timezone.utc)
        for i in range(3):
            r = dsh.record_fetch_result(
                "3688.T", False, now=base + timedelta(days=i), path=self.path,
            )
        self.assertTrue(r["newly_retired"])
        # 第 4 天继续 miss → newly_retired 不应再 True
        r2 = dsh.record_fetch_result(
            "3688.T", False, now=base + timedelta(days=3), path=self.path,
        )
        self.assertFalse(r2["newly_retired"])

    def test_mark_many_bulk(self):
        base = datetime(2026, 10, 10, tzinfo=timezone.utc)
        for i in range(3):
            dsh.mark_many(
                ["6488.TW", "3688.T"], False,
                now=base + timedelta(days=i), path=self.path,
            )
        retired = dsh.list_retired(self.path)
        self.assertIn("6488.TW", retired)
        self.assertIn("3688.T", retired)


class PcaRetiredIntegrationTests(unittest.TestCase):
    """pca_sox.fetch_returns 剔除 retired 后继续跑, 不整轮跳过."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "retired_symbols.json"
        # 让 _data_source_health 读写我们的临时文件
        self._patch = patch.object(dsh, "_retired_state_path",
                                    return_value=self.path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_retired_filtered_before_download(self):
        """fetch_returns 应把 retired 票先剔掉, 不传给 yfinance."""
        import pca_sox

        base = datetime(2026, 10, 10, tzinfo=timezone.utc)
        for i in range(3):
            dsh.record_fetch_result(
                "6488.TW", False, now=base + timedelta(days=i), path=self.path,
            )
            dsh.record_fetch_result(
                "3688.T", False, now=base + timedelta(days=i), path=self.path,
            )

        called_with = {}

        class _FakeYF:
            def download(self, tickers, **kwargs):
                called_with["tickers"] = list(tickers)
                import pandas as pd
                # 返回空 → fetch_returns 应优雅返回 None (不 crash)
                return pd.DataFrame()

        with patch.dict(sys.modules, {"yfinance": _FakeYF()}):
            pca_sox.fetch_returns(
                tickers=["NVDA", "AMD", "6488.TW", "3688.T", "AVGO",
                         "TSM", "MU", "AMAT"],
                min_assets=5,
            )
        # retired 的两个必须不在 download 调用里
        self.assertNotIn("6488.TW", called_with.get("tickers", []))
        self.assertNotIn("3688.T",  called_with.get("tickers", []))
        # 保留的 live tickers 应全部传入
        for tk in ("NVDA", "AMD", "AVGO", "TSM", "MU", "AMAT"):
            self.assertIn(tk, called_with.get("tickers", []))

    def test_insufficient_live_tickers_returns_none(self):
        import pca_sox

        base = datetime(2026, 10, 10, tzinfo=timezone.utc)
        # retire 4 只, 剩下 3 只 < min_assets=5
        for tk in ("A", "B", "C", "D"):
            for i in range(3):
                dsh.record_fetch_result(
                    tk, False, now=base + timedelta(days=i), path=self.path,
                )

        downloaded = {"called": False}

        class _FakeYF:
            def download(self, *a, **kw):
                downloaded["called"] = True
                import pandas as pd
                return pd.DataFrame()

        with patch.dict(sys.modules, {"yfinance": _FakeYF()}):
            result = pca_sox.fetch_returns(
                tickers=["A", "B", "C", "D", "E", "F", "G"], min_assets=5,
            )
        self.assertIsNone(result)
        # 剔除后 live=3 < min_assets=5, 应在 download 前短路, 一次 yf 调用都不发
        self.assertFalse(downloaded["called"])


class PcaStaleSeriesTests(unittest.TestCase):
    """停更序列 (QRVO-like) 不参与矩阵, 避免 dropna 截断整个窗口."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "retired_symbols.json"
        self._patch = patch.object(dsh, "_retired_state_path",
                                   return_value=self.path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stale_series_excluded_and_recorded(self):
        import pandas as pd
        import pca_sox

        tickers = ["AAA", "BBB", "CCC", "DDD", "EEE", "QRVO"]
        idx = pd.bdate_range(end="2026-10-21", periods=90)
        cols = pd.MultiIndex.from_product([tickers, ["Close"]])
        frame = pd.DataFrame(index=idx, columns=cols, dtype=float)
        for n, tk in enumerate(tickers):
            frame[(tk, "Close")] = (
                100.0 + n + pd.Series(range(len(idx)), index=idx) * 0.1
            )
        # QRVO 停更 10 个交易日: 非空序列, 但最后成交日远落后于同批
        frame.loc[idx[-10:], ("QRVO", "Close")] = float("nan")

        class _FakeYF:
            @staticmethod
            def download(*args, **kwargs):
                return frame

        with patch.dict(sys.modules, {"yfinance": _FakeYF()}):
            returns = pca_sox.fetch_returns(tickers=tickers, min_assets=5)

        self.assertIsNotNone(returns)
        self.assertNotIn("QRVO", returns.columns)
        self.assertEqual(set(returns.columns), set(tickers) - {"QRVO"})
        # 若未剔除, dropna 会把 90 天矩阵截到 QRVO 的 80 天 (再少 1 天收益)
        self.assertGreater(len(returns), 80)

        state = json.loads(self.path.read_text(encoding="utf-8"))
        qrvo = state["symbols"]["QRVO"]
        self.assertEqual(qrvo["status"], "watching")
        self.assertEqual(qrvo["miss_days"], 1)
        self.assertEqual(qrvo["reason"], "yfinance_stale")


class DataQualityRetiredTests(unittest.TestCase):
    """data_quality.audit_latest_signal_files: retired 文件不计 critical."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sig_dir = Path(self.tmp) / "signals"
        self.sig_dir.mkdir(parents=True)
        self.retired_path = self.sig_dir / "retired_symbols.json"
        self._patch = patch.object(dsh, "_retired_state_path",
                                    return_value=self.retired_path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_signal(self, name: str, market: dict | None):
        payload = {"market": market} if market is not None else {}
        (self.sig_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8",
        )

    def test_retired_file_shown_as_retired_not_critical(self):
        # 两个 stale 文件: 6488.TW retired, NVDA 真坏
        self._write_signal("6488.TW_latest.json", {"price": 10.0})      # missing_ts
        self._write_signal("NVDA_latest.json",    {"price": 100.0})     # missing_ts
        # 标 6488.TW 为 retired
        base = datetime(2026, 10, 10, tzinfo=timezone.utc)
        for i in range(3):
            dsh.record_fetch_result(
                "6488.TW", False, now=base + timedelta(days=i),
                path=self.retired_path,
            )

        report = data_quality.audit_latest_signal_files(self.sig_dir)
        by_file = {f["file"]: f for f in report["files"]}
        self.assertEqual(by_file["6488.TW_latest.json"]["status"], "retired")
        self.assertEqual(by_file["NVDA_latest.json"]["status"],    "critical")
        self.assertEqual(report["retired_files"], 1)
        # critical 只计真问题
        self.assertEqual(report["critical_files"], 1)
        self.assertEqual(report["status"], "critical")

    def test_all_retired_report_is_ok(self):
        self._write_signal("6488.TW_latest.json", {"price": 10.0})
        base = datetime(2026, 10, 10, tzinfo=timezone.utc)
        for i in range(3):
            dsh.record_fetch_result(
                "6488.TW", False, now=base + timedelta(days=i),
                path=self.retired_path,
            )
        report = data_quality.audit_latest_signal_files(self.sig_dir)
        self.assertEqual(report["critical_files"], 0)
        self.assertEqual(report["warning_files"],  0)
        self.assertEqual(report["retired_files"],  1)
        self.assertEqual(report["status"], "ok")


class DataQualityAuditScopingTests(unittest.TestCase):
    """legacy 副本 / 非行情文件 / 空文件不应冒充 critical."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sig_dir = Path(self.tmp) / "signals"
        self.sig_dir.mkdir(parents=True)
        self.retired_path = self.sig_dir / "retired_symbols.json"
        self._patch = patch.object(dsh, "_retired_state_path",
                                   return_value=self.retired_path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, payload) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (self.sig_dir / name).write_text(text, encoding="utf-8")

    def _fresh_market(self) -> dict:
        return {
            "price": 100.0,
            "ts": datetime.now(timezone.utc).astimezone().isoformat(),
            "bid": 99.9, "ask": 100.1,
            "rsi_14": 50, "ma20": 99, "ma50": 98, "vol_ratio": 1.0,
        }

    def test_legacy_us_prefix_duplicate_is_not_counted(self):
        self._write("NVDA_latest.json", {"market": self._fresh_market()})
        self._write("US.NVDA_latest.json", {"market": {"price": 100.0}})
        report = data_quality.audit_latest_signal_files(self.sig_dir)
        by_file = {f["file"]: f for f in report["files"]}
        self.assertEqual(by_file["US.NVDA_latest.json"]["status"], "legacy")
        self.assertEqual(by_file["NVDA_latest.json"]["status"], "ok")
        self.assertEqual(report["critical_files"], 0)
        self.assertEqual(report["legacy_files"], 1)

    def test_retired_lookup_matches_us_prefixed_name(self):
        base = datetime(2026, 10, 10, tzinfo=timezone.utc)
        for i in range(3):
            dsh.record_fetch_result(
                "6488.TW", False, now=base + timedelta(days=i),
                path=self.retired_path,
            )
        self._write("US.6488.TW_latest.json", {"market": {"price": 10.0}})
        report = data_quality.audit_latest_signal_files(self.sig_dir)
        by_file = {f["file"]: f for f in report["files"]}
        self.assertEqual(by_file["US.6488.TW_latest.json"]["status"], "retired")
        self.assertEqual(report["retired_files"], 1)
        self.assertEqual(report["critical_files"], 0)

    def test_non_market_payload_is_ignored(self):
        self._write("policy_toolkit_latest.json", {"as_of": "2026-08-29", "tools": []})
        report = data_quality.audit_latest_signal_files(self.sig_dir)
        self.assertEqual(report["files"][0]["status"], "ignored")
        self.assertEqual(report["ignored_files"], 1)
        self.assertEqual(report["critical_files"], 0)

    def test_empty_file_is_warning_not_critical(self):
        self._write("US.0000_latest.json", "")
        report = data_quality.audit_latest_signal_files(self.sig_dir)
        entry = report["files"][0]
        self.assertEqual(entry["status"], "warning")
        self.assertEqual(entry["issues"][0]["code"], "empty_file")
        self.assertEqual(report["critical_files"], 0)
        self.assertEqual(report["warning_files"], 1)

    def test_malformed_json_stays_critical(self):
        self._write("BAD_latest.json", "{not json")
        report = data_quality.audit_latest_signal_files(self.sig_dir)
        entry = report["files"][0]
        self.assertEqual(entry["status"], "critical")
        self.assertEqual(entry["issues"][0]["code"], "invalid_json")
        self.assertEqual(report["critical_files"], 1)

    def test_flat_legacy_market_payload_still_audited(self):
        self._write("OLD_latest.json", self._fresh_market())
        report = data_quality.audit_latest_signal_files(self.sig_dir)
        entry = report["files"][0]
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(report["ignored_files"], 0)


class MoomooPoolRetiredTests(unittest.TestCase):
    """moomoo_pool.is_symbol_retired 作为单一入口返回正确."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "retired_symbols.json"
        self._patch = patch.object(dsh, "_retired_state_path",
                                    return_value=self.path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_is_symbol_retired_true_after_three_misses(self):
        base = datetime(2026, 10, 10, tzinfo=timezone.utc)
        for i in range(3):
            dsh.record_fetch_result(
                "6488.TW", False, now=base + timedelta(days=i), path=self.path,
            )
        self.assertTrue(dsh.is_retired("6488.TW", self.path))
        self.assertFalse(dsh.is_retired("NVDA", self.path))

        # moomoo_pool 的 helper 应直接走单一源, 返回同样结果
        try:
            import moomoo_pool
        except Exception:
            self.skipTest("moomoo SDK unavailable in this env")
        self.assertTrue(moomoo_pool.is_symbol_retired("6488.TW"))
        self.assertFalse(moomoo_pool.is_symbol_retired("NVDA"))
        self.assertFalse(moomoo_pool.is_symbol_retired(""))


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
