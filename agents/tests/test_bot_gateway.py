"""bot_gateway: 机器人只读接入层 (2026-10-19). 临时目录 + 假数据, 不碰真实 signals."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot_gateway as g  # noqa: E402


class Gateway(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "evolution").mkdir()
        w = lambda rel, rows: (self.tmp / rel).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        w("trade_log.jsonl", [{"ts": "2026-10-16T19:49:15.4+00:00", "ticker": "US.NVDA", "side": "BUY", "qty": 152, "price": 215.56,
                               "tag": "[WATCH_BUY_PROBE]", "window": "pre-close", "dry_run": False}])
        w("execution_ledger.jsonl", [{"ts": "2026-10-16T19:49:15.9+00:00", "event": "submitted", "ticker": "US.NVDA", "side": "BUY",
                                      "requested_qty": 152, "order_price": 215.56, "order_id": "1", "plan": {"big": "x" * 1000}},
                                     {"ts": "2026-10-16T19:49:15.1+00:00", "event": "filled", "ticker": "US.NVDA", "side": "BUY",
                                      "requested_qty": 152, "dealt_qty": 152, "average_fill_price": 215.5, "order_id": "1"}])
        w("rebalance_plan.jsonl", [{"ts": "2026-10-17T04:50:29", "window": "pre-close", "dry_run": True, "nav": 1, "cash": 1,
                                    "drawdown_pct": 2, "regime": "x", "orders": []}])   # 无时区 = 日本时间
        w("critical_alerts.jsonl", [{"ts": "2026-10-19T02:36:36+00:00", "level": "crisis", "msg": "**CRISIS** NBIS\n第二行"}])
        w("evolution/log.jsonl", [{"ts": "2026-10-19T02:00:00+00:00", "event": "status", "id": "EVO-1", "status": "merged", "by": "chat"}])
        (self.tmp / "evolution" / "proposals.json").write_text(json.dumps([{"id": "EVO-2", "status": "proposed", "title": "t"}]), encoding="utf-8")
        (self.tmp / "nav_history.jsonl").write_text(json.dumps({"ts": "x", "nav": 100}), encoding="utf-8")
        self.p = [patch.object(g, "SIGNALS", self.tmp), patch.object(g, "STATE", self.tmp / "bot_feed_state.json")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_events_normalized_and_sorted(self):
        r = g.events()
        kinds = [e["kind"] for e in r["events"]]
        self.assertEqual(kinds, ["execution", "execution", "order", "rebalance_plan", "evolution", "alert"])
        self.assertTrue(all(e["ts"].endswith("+09:00") for e in r["events"]))
        reb = next(e for e in r["events"] if e["kind"] == "rebalance_plan")
        self.assertEqual(reb["ts"], "2026-10-17T04:50:29+09:00")                  # 无时区按日本时间
        ex = next(e for e in r["events"] if e["data"].get("event") == "submitted")
        self.assertNotIn("plan", ex["data"])                                       # 大字段剔除
        self.assertIn("下单 BUY US.NVDA 152 股", next(e for e in r["events"] if e["kind"] == "order")["summary"])
        self.assertNotIn("\n", next(e for e in r["events"] if e["kind"] == "alert")["summary"])

    def test_paging_no_loss_no_dup_on_same_second(self):
        full = [e["id"] for e in g.events()["events"]]
        got, since = [], None
        for _ in range(20):
            r = g.events(since=since, limit=1)
            got += [e["id"] for e in r["events"]]
            if not r["truncated"]:
                break
            since = r["next_since"]
        self.assertEqual(got, full)

    def test_kind_filter_and_since(self):
        r = g.events(since="2026-10-18T00:00:00+09:00", kinds=["alert", "evolution"])
        self.assertEqual([e["kind"] for e in r["events"]], ["evolution", "alert"])

    def test_summary_and_index(self):
        s = g.summary()
        self.assertEqual(s["evolution_pending"], [{"id": "EVO-2", "status": "proposed", "title": "t"}])
        self.assertEqual(s["account"]["nav_latest"]["nav"], 100)
        idx = g.index()
        classes = {e["path"]: e["class"] for e in idx["endpoints"]}
        self.assertEqual(classes["/api/evolution_action"], "write_forbidden")
        self.assertEqual(classes["/api/evolution_token"], "write_forbidden")
        self.assertEqual(classes["/api/bot/events"], "read")

    def test_tick_init_then_push_once(self):
        sent = []
        with patch.object(g, "_cfg", return_value="http://127.0.0.1:9/x"):
            self.assertIn("initialized", g.tick(post=lambda u, p: sent.append(p) or True))
            st = json.loads(g.STATE.read_text(encoding="utf-8"))
            st["since"] = "2026-10-16T00:00:00+09:00"                              # 假装从 10/16 开始
            g.STATE.write_text(json.dumps(st), encoding="utf-8")
            r1 = g.tick(post=lambda u, p: sent.append(p) or True)
            r2 = g.tick(post=lambda u, p: sent.append(p) or True)
        self.assertEqual(r1["pushed"], 6)
        self.assertEqual(r2.get("new"), 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("markdown", sent[0])

    def test_tick_failure_keeps_cursor(self):
        with patch.object(g, "_cfg", return_value="http://x"):
            g.tick(post=lambda u, p: True)
            st = json.loads(g.STATE.read_text(encoding="utf-8")); st["since"] = "2026-10-16T00:00:00+09:00"
            g.STATE.write_text(json.dumps(st), encoding="utf-8")
            r = g.tick(post=lambda u, p: False)
            self.assertEqual(json.loads(g.STATE.read_text(encoding="utf-8"))["since"], "2026-10-16T00:00:00+09:00")
            self.assertFalse(r["ok"])

    def test_no_webhook_configured(self):
        with patch.object(g, "_cfg", return_value=""):
            g.tick()
            self.assertIn("未配置", g.tick()["skipped"])


if __name__ == "__main__":
    unittest.main()
