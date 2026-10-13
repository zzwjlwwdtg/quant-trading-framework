"""系统自我进化 (2026-10-12): 先审批后开发, 护栏, 交易相关改动二次确认."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import evolution as ev


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout


GOOD = {"review_summary": "止损在盘前被打掉后当天反弹, 反复出现",
        "proposals": [
            {"title": "止损卖出后的影子记录", "category": "research", "problem": "盘前止损后反弹",
             "evidence": ["MULL 10-07"], "logic": "先记录再检验", "bloomberg_ref": "BTCA",
             "change": {"files": ["agents/trade_postmortem.py"], "description": "加影子记录"},
             "test_plan": "预注册", "risk": "低", "effort": "S"},
            {"title": "改自我进化模块", "category": "infra", "problem": "x",
             "change": {"files": ["agents/evolution.py"]}},           # 禁区 → 丢弃
        ]}


class Base(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.agents = self.td / "agents"
        (self.agents / "signals").mkdir(parents=True)
        evo = self.agents / "signals" / "evolution"
        self.p = [patch.object(ev, "HERE", self.agents), patch.object(ev, "REPO", self.td),
                  patch.object(ev, "EVO_DIR", evo), patch.object(ev, "PROPOSALS", evo / "proposals.json"),
                  patch.object(ev, "LOG", evo / "log.jsonl"), patch.object(ev, "TOKEN", evo / ".token"),
                  patch.object(ev, "KILL_SWITCH", self.agents / ".evolution_off"),
                  patch.object(ev, "WORKTREES", self.td / ".evo_worktrees")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()


class Proposals(Base):
    def test_propose_filters_forbidden_and_dedupes(self):
        ai = lambda prompt: (json.dumps(GOOD, ensure_ascii=False), "ok", "Claude", "")
        r = ev.propose(ai=ai)
        self.assertEqual(len(r["added"]), 1)
        items = ev.load()
        self.assertEqual(items[0]["status"], "proposed")
        self.assertEqual(items[0]["title"], "止损卖出后的影子记录")
        self.assertEqual(ev.propose(ai=ai)["added"], [])                 # 同标题不重复
        summ = json.loads((ev.EVO_DIR / "last_review_summary.json").read_text(encoding="utf-8"))
        self.assertIn("反弹", summ["summary"])

    def test_prompt_mentions_bloomberg_postmortem_and_bans(self):
        p = ev.build_propose_prompt([])
        for s in ("彭博", "卖出复盘总结", "预注册检验", "拉黑", "借保证金", "只输出 JSON"):
            self.assertIn(s, p)

    def test_kill_switch_and_weekly_cap(self):
        (self.agents / ".evolution_off").write_text("")
        self.assertEqual(ev.propose(ai=lambda p: ("{}", "ok", "C", "")), {"skipped": "kill switch"})
        (self.agents / ".evolution_off").unlink()
        ev.save([{"id": f"EVO-x-{i}", "title": str(i), "created_at": ev._now(), "status": "proposed"}
                 for i in range(ev.WEEKLY_PROPOSAL_CAP)])
        self.assertEqual(ev.propose(ai=lambda p: ("{}", "ok", "C", "")), {"skipped": "weekly cap"})

    def test_approval_state_machine(self):
        ev.save([{"id": "EVO-1", "title": "t", "created_at": ev._now(), "status": "proposed", "history": []}])
        self.assertFalse(ev.act("EVO-1", "approve_merge")["ok"])         # 还没开发不能上线
        self.assertTrue(ev.act("EVO-1", "approve", note="ok")["ok"])
        self.assertTrue((self.agents / "signals" / "job_request_evo_worker.json").exists())
        self.assertFalse(ev.act("EVO-1", "reject")["ok"])                 # 已批准不能再拒
        self.assertFalse(ev.act("EVO-404", "approve")["ok"])
        self.assertEqual(ev.load()[0]["history"][-1]["by"], "user")


class Guard(unittest.TestCase):
    def test_guard_diff(self):
        self.assertEqual(ev.guard_diff(["agents/dashboard.html"], "+<div>ok</div>\n"), [])
        self.assertTrue(ev.guard_diff(["agents/evolution.py"], ""))
        self.assertTrue(ev.guard_diff(["docs/data/x.json"], ""))
        self.assertTrue(ev.guard_diff(["agents/paper_trader.py"], "-CASH_FLOOR_USD = 0.0\n+CASH_FLOOR_USD = -1e6\n"))
        self.assertTrue(ev.guard_diff(["agents/config.py"], "+TRADING_ACCOUNT_MODE = 'REAL'\n"))
        self.assertEqual(ev.guard_diff(["a.py"], "--- a/a.py\n+++ b/a.py\n"), [])

    def test_needs_review(self):
        self.assertTrue(ev.needs_review(["agents/paper_trader.py", "agents/dashboard.html"]))
        self.assertFalse(ev.needs_review(["agents/dashboard.html", "agents/tests/test_x.py"]))


class Develop(Base):
    def setUp(self):
        super().setUp()
        git(self.td, "init", "-q", "-b", "main")
        git(self.td, "config", "user.email", "t@e.com"); git(self.td, "config", "user.name", "t")
        (self.agents / "dashboard.html").write_text("<html></html>\n")
        (self.agents / "paper_trader.py").write_text("x = 1\n")
        (self.td / ".gitignore").write_text(".evo_worktrees/\nagents/signals/\n")
        git(self.td, "add", "-A"); git(self.td, "commit", "-qm", "init")

    def _approve(self, pid="EVO-1"):
        ev.save([{"id": pid, "title": "改看板", "problem": "p", "created_at": ev._now(), "status": "approved",
                  "change": {"files": ["agents/dashboard.html"]}, "history": []}])

    @staticmethod
    def _tests(fail_after=False):
        calls = {"n": 0}
        def t(agents_dir):
            calls["n"] += 1
            failed = ["test_x (tests.T)"] if (fail_after and calls["n"] > 1) else []
            return {"failed": failed, "ran": 10, "ok": not failed, "tail": "FAIL" if failed else ""}
        return t

    def _dev(self, rel, text):
        def d(wt, prompt):
            (Path(wt) / rel).write_text(text)
            return True, "改了"
        return d

    def test_git_runs_in_patched_repo_only(self):
        self.assertEqual(Path(ev._git(["rev-parse", "--show-toplevel"]).stdout.strip()).resolve(), self.td.resolve())

    def test_dashboard_change_auto_merges(self):
        self._approve()
        r = ev.develop("EVO-1", dev=self._dev("agents/dashboard.html", "<html>new</html>\n"), tests=self._tests())
        self.assertEqual(r["status"], "merged")
        self.assertIn("new", (self.agents / "dashboard.html").read_text())
        self.assertIn("[evo]", git(self.td, "log", "-1", "--format=%s"))
        self.assertFalse((self.td / ".evo_worktrees" / "EVO-1").exists())

    def test_trading_file_waits_for_second_approval(self):
        self._approve()
        r = ev.develop("EVO-1", dev=self._dev("agents/paper_trader.py", "x = 2\n"), tests=self._tests())
        self.assertEqual(r["status"], "ready_for_review")
        self.assertEqual((self.agents / "paper_trader.py").read_text(), "x = 1\n")      # 还没合并
        self.assertTrue(ev.act("EVO-1", "approve_merge")["ok"])
        res = ev.work()
        self.assertEqual(res[0]["status"], "merged")
        self.assertEqual((self.agents / "paper_trader.py").read_text(), "x = 2\n")

    def test_new_test_failures_block(self):
        self._approve()
        r = ev.develop("EVO-1", dev=self._dev("agents/dashboard.html", "<b>x</b>\n"), tests=self._tests(fail_after=True))
        self.assertEqual(r["status"], "dev_failed")
        self.assertEqual((self.agents / "dashboard.html").read_text(), "<html></html>\n")

    def test_guard_blocks_forbidden_content(self):
        self._approve()
        r = ev.develop("EVO-1", dev=self._dev("agents/paper_trader.py", "CASH_FLOOR_USD = -1\n"), tests=self._tests())
        self.assertEqual(r["status"], "blocked")
        self.assertEqual((self.agents / "paper_trader.py").read_text(), "x = 1\n")

    def test_unapproved_never_developed(self):
        ev.save([{"id": "EVO-1", "title": "t", "created_at": ev._now(), "status": "proposed", "history": []}])
        called = []
        self.assertFalse(ev.develop("EVO-1", dev=lambda *a: called.append(1), tests=self._tests())["ok"])
        self.assertEqual(called, [])


class WorkQueue(Base):
    """2026-10-13: 一次批准多条 → worker 开发完一条后自动再排下一次, 卡住时不无限重排."""
    def _items(self, n):
        ev.save([{"id": f"EVO-{i}", "title": "t", "created_at": ev._now(), "status": "approved", "history": []}
                 for i in range(1, n + 1)])

    def _req(self):
        return self.agents / "signals" / "job_request_evo_worker.json"

    def test_requeues_when_more_approved(self):
        self._items(2)
        def fake_dev(pid):
            ps = ev.load()
            for x in ps:
                if x["id"] == pid:
                    x["status"] = "merged"
            ev.save(ps)
            return {"id": pid, "status": "merged"}
        with patch.object(ev, "develop", side_effect=fake_dev):
            res = ev.work()
        self.assertEqual(res[0]["id"], "EVO-1")
        self.assertIn({"requeued": True}, res)
        self.assertTrue(self._req().exists())

    def test_no_requeue_when_stuck_or_done(self):
        self._items(2)
        with patch.object(ev, "develop", return_value={"id": "EVO-1", "status": "approved"}):
            res = ev.work()
        self.assertNotIn({"requeued": True}, res)
        self.assertFalse(self._req().exists())


class Wiring(unittest.TestCase):
    def test_jobs_and_dashboard(self):
        import _webui_watchdog as wd
        import snapshot_generator as sg
        self.assertTrue(wd.JOBS["evo_propose"]["market_quiet"])
        self.assertNotIn("auto_every_days", wd.JOBS["evo_worker"])       # 开发只由批准唤起
        self.assertIn("/api/evolution", sg.GLOBAL_ENDPOINTS)
        self.assertNotIn("/api/evolution_token", sg.GLOBAL_ENDPOINTS)
        html = (AGENTS_DIR / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("'/api/evolution_token'", html[html.index("const PRIVATE"):html.index("const PRIVATE") + 300])
        for f in ("_evolution_propose.bat", "_evolution_worker.bat"):
            self.assertTrue((AGENTS_DIR / f).exists())


if __name__ == "__main__":
    unittest.main()
