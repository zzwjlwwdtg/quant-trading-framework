"""券商历史基线 + 来源归属 (2026-09-24).

- 券商历史中本地账本缺失的成交 → 独立文件 signals/broker_history_fills.jsonl,
  读取时合并; 不改 execution_ledger (hash 链不动). 同 order_id 以本地为准.
- 每笔成交带 origin: system (本地账本或系统日志有该订单) / manual.
- 已实现盈亏按 **被消耗的买入层** 的 origin 归属 (谁开的仓算谁的).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import cohort_tracker as ct
import fill_ledger as fl


def _f(oid, ts, ticker, side, qty, px, **kw):
    return {"event": "filled", "order_id": oid, "ts": ts, "ticker": ticker,
            "side": side, "dealt_qty": qty, "average_fill_price": px, **kw}


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


class MergeOnRead(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.exec = self.td / "exec.jsonl"
        self.hist = self.td / "hist.jsonl"
        _write(self.exec, [
            {"event": "submitted", "order_id": "S1", "ticker": "US.X", "side": "SELL"},
            _f("S1", "2099-01-02T15:00:00+00:00", "US.X", "SELL", 10, 110.0)])
        _write(self.hist, [
            _f("B0", "2099-01-01T15:00:00+00:00", "US.X", "BUY", 10, 100.0,
               source="broker_history", origin="manual"),
            _f("S1", "2099-01-02T15:00:00+00:00", "US.X", "SELL", 10, 999.0,
               source="broker_history", origin="system")])   # dup oid → ignored

    def _patched(self):
        return patch.multiple(fl, EXEC_LEDGER_PATH=self.exec, BROKER_HISTORY_PATH=self.hist)

    def test_default_load_merges_missing_orders_only(self):
        with self._patched():
            rows = fl._load_ledger()
        fills = [r for r in rows if r["event"] == "filled"]
        self.assertEqual(sorted(r["order_id"] for r in fills), ["B0", "S1"])
        s1 = [r for r in fills if r["order_id"] == "S1"][0]
        self.assertEqual(s1["average_fill_price"], 110.0)   # local wins

    def test_can_exclude_broker_history(self):
        with self._patched():
            rows = fl._load_ledger(include_broker_history=False)
        self.assertNotIn("B0", [r["order_id"] for r in rows])

    def test_position_uses_baseline(self):
        with self._patched():
            p = fl.get_position("US.X")
        self.assertEqual((p["realized_pnl"], p["unreconciled_sells"]), (100.0, 0))


class OriginAttribution(unittest.TestCase):
    def test_pnl_attributed_to_layer_origin(self):
        ev = [_f("B0", "2099-01-01T14:00:00+00:00", "US.T", "BUY", 10, 100.0,
                 source="broker_history", origin="manual"),
              _f("B1", "2099-01-01T15:00:00+00:00", "US.T", "BUY", 10, 200.0),  # local → system
              _f("S1", "2099-01-02T15:00:00+00:00", "US.T", "SELL", 20, 150.0)]
        with patch.object(fl, "_load_ledger", return_value=ev), \
             patch.object(fl, "_load_corporate_actions", return_value=[]):
            s = ct.stats_from_fills(since_days=30)
        self.assertEqual(s["pnl_by_origin"], {"manual": 500.0, "system": -500.0})
        self.assertEqual(s["total_pnl_usd"], 0.0)

    def test_default_origin_for_local_rows_is_system(self):
        inc, _ = fl.fill_increments(
            [_f("B1", "2099-01-01T15:00:00+00:00", "US.T", "BUY", 1, 1.0)], [])
        self.assertEqual(inc[0]["origin"], "system")

    def test_broker_row_without_origin_is_unknown(self):
        inc, _ = fl.fill_increments(
            [_f("B1", "2099-01-01T15:00:00+00:00", "US.T", "BUY", 1, 1.0,
                source="broker_history")], [])
        self.assertEqual(inc[0]["origin"], "unknown")


class ImportBaseline(unittest.TestCase):
    def test_import_tags_origin_from_logs(self):
        import _broker_history_reconcile as br
        td = Path(tempfile.mkdtemp())
        src = td / "broker_fills.jsonl"
        _write(src, [
            _f("100", "2099-01-01T15:00:00+00:00", "US.X", "BUY", 5, 10.0, source="broker_history"),
            _f("200", "2099-01-01T15:00:00+00:00", "US.X", "BUY", 5, 10.0, source="broker_history"),
            _f("300", "2099-01-02T15:00:00+00:00", "US.X", "SELL", 5, 11.0, source="broker_history")])
        exec_ = td / "exec.jsonl"
        _write(exec_, [_f("300", "2099-01-02T15:00:00+00:00", "US.X", "SELL", 5, 11.0)])
        logs = td / "logs"
        logs.mkdir()
        (logs / "run_1.log").write_text(
            "x [trader-LIVE] BUY     5 US.X  @ 10.00 (ref 10)  order=100 [tag]\n", encoding="utf-8")
        out = td / "hist.jsonl"
        n = br.import_baseline(src, out, exec_path=exec_, logs_dir=logs)
        rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(n, 2)
        self.assertEqual({r["order_id"]: r["origin"] for r in rows}, {"100": "system", "200": "manual"})


if __name__ == "__main__":
    unittest.main()


class FormatShowsOrigin(unittest.TestCase):
    def test_origin_breakdown_line(self):
        fills = {"n": 2, "n_roundtrips": 2, "n_winners": 1, "n_losers": 1,
                 "win_rate": 50.0, "total_pnl_usd": -100.0, "n_events": 4,
                 "n_tickers": 2, "authority": "fills_replay", "warning": None,
                 "unreconciled_tickers": [], "unreconciled_sells_qty": 0,
                 "pnl_by_origin": {"manual": -300.0, "system": 200.0}, "positions": {}}
        with patch.object(ct, "stats_from_fills", return_value=fills), \
             patch.object(ct, "stats", return_value={"n": 0, "total_pnl_usd": 0.0}), \
             patch.object(ct, "all_active_cohorts", return_value=[]):
            text = ct.format_stats(30)
        self.assertIn("系统开仓 $+200.00", text)
        self.assertIn("手动开仓 $-300.00", text)
