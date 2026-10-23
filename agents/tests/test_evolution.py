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

_REAL_IN_DEV_WINDOW = ev.in_dev_window


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
                  patch.object(ev, "WORKTREES", self.td / ".evo_worktrees"),
                  patch.object(ev, "in_dev_window", return_value=True)]
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


class QuotaAndWip(Develop):
    """2026-10-13: 额度用完不算失败 (退回 approved 等重试); 失败的改动保留在分支上便于汇报."""
    def test_quota_failure_returns_to_approved_without_requeue(self):
        self._approve()
        quota = lambda wt, prompt: (False, "You've hit your limit · resets 2:30pm (Asia/Tokyo)")
        with patch.object(ev, "_claude_dev", quota), patch.object(ev, "run_tests", self._tests()):
            res = ev.work()
        self.assertEqual(ev.get(ev.load(), "EVO-1")["status"], "approved")
        self.assertNotIn({"requeued": True}, res)            # 不会立刻死循环重排

    def test_failed_dev_kept_on_branch_and_retry(self):
        self._approve()
        r = ev.develop("EVO-1", dev=self._dev("agents/dashboard.html", "<b>x</b>\n"), tests=self._tests(fail_after=True))
        self.assertEqual(r["status"], "dev_failed")
        self.assertIn("[evo-wip]", git(self.td, "log", "-1", "--format=%s", "evo/EVO-1"))
        self.assertEqual((self.agents / "dashboard.html").read_text(), "<html></html>\n")   # main 未变
        self.assertIn("test_tail", ev.get(ev.load(), "EVO-1")["dev_report"])
        self.assertTrue(ev.act("EVO-1", "retry")["ok"])
        self.assertEqual(ev.get(ev.load(), "EVO-1")["status"], "approved")

    def test_worker_job_has_fallback_schedule(self):
        import _webui_watchdog as w
        self.assertTrue(w.JOBS["evo_worker"].get("auto_every_days"))


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


class DevWindow(Base):
    """2026-10-14: 开发只在美股收盘后窗口进行 (保护交易链路的 Claude 额度); 合并不受限."""
    def test_outside_window_defers_dev_but_merges(self):
        from datetime import datetime, timezone
        ev.save([{"id": "EVO-1", "title": "t", "created_at": ev._now(), "status": "approved", "history": []},
                 {"id": "EVO-2", "title": "m", "created_at": ev._now(), "status": "merge_approved", "history": []}])
        with patch.object(ev, "in_dev_window", return_value=False), \
             patch.object(ev, "develop") as dev, patch.object(ev, "merge", return_value={"id": "EVO-2", "status": "merged"}) as mg:
            res = ev.work()
        dev.assert_not_called()
        mg.assert_called_once_with("EVO-2")
        self.assertTrue(any("deferred" in r for r in res))
        self.assertFalse((self.agents / "signals" / "job_request_evo_worker.json").exists())

    def test_window_hours(self):
        from datetime import datetime, timezone
        real = ev.__dict__.get("DEV_WINDOW_UTC_HOURS")
        self.assertIn(21, real); self.assertIn(1, real)
        self.assertIn(4, real)
        self.assertNotIn(13, real); self.assertNotIn(10, real); self.assertNotIn(5, real)   # 交易前 8 小时起不开发


class ResumeAndVerdicts(Develop):
    """2026-10-18: 重试时续做 [evo-wip]; 预注册 verdict 不被当运行时文件清掉; 修复轮拿到真 traceback."""
    def test_retry_resumes_from_wip_without_fresh_dev(self):
        self._approve()
        r = ev.develop("EVO-1", dev=self._dev("agents/dashboard.html", "<html>wip</html>\n"),
                       tests=self._tests(fail_after=True))
        self.assertEqual(r["status"], "dev_failed")
        self.assertTrue(ev.act("EVO-1", "retry")["ok"])
        calls = []
        r2 = ev.develop("EVO-1", dev=lambda wt, prompt: (calls.append(prompt), (True, ""))[1], tests=self._tests())
        self.assertEqual(calls, [])                          # 没有新增失败 → 不再调用 Claude
        self.assertEqual(r2["status"], "merged")
        self.assertIn("wip", (self.agents / "dashboard.html").read_text())
        self.assertTrue(ev.get(ev.load(), "EVO-1")["dev_report"]["resumed_from_wip"])

    def test_resume_merges_latest_main_first(self):
        self._approve()
        ev.develop("EVO-1", dev=self._dev("agents/dashboard.html", "<html>wip</html>\n"), tests=self._tests(fail_after=True))
        (self.agents / "portfolio_analytics.py").write_text("X = 1\n")          # main 上后补的模块
        git(self.td, "add", "-A"); git(self.td, "commit", "-qm", "add module")
        ev.act("EVO-1", "retry")
        seen = {}
        def t(agents_dir):
            seen.setdefault("has_module", []).append((Path(agents_dir) / "portfolio_analytics.py").exists())
            return {"failed": [], "ran": 1, "ok": True, "tail": "", "text": ""}
        r = ev.develop("EVO-1", dev=lambda wt, pr: (True, ""), tests=t)
        self.assertEqual(r["status"], "merged")
        self.assertTrue(all(seen["has_module"]))

    def test_preregistered_verdict_kept_runtime_dropped(self):
        (self.td / ".gitignore").write_text(".evo_worktrees/\n")
        git(self.td, "add", "-A"); git(self.td, "commit", "-qm", "unignore signals")
        self._approve()
        def d(wt, prompt):
            (Path(wt) / "agents/signals/backtest_verdicts").mkdir(parents=True, exist_ok=True)
            (Path(wt) / "agents/signals/backtest_verdicts/v.json").write_text("{}")
            (Path(wt) / "agents/signals/runtime_state.json").write_text("{}")
            (Path(wt) / "agents/dashboard.html").write_text("<html>v</html>\n")
            return True, "ok"
        r = ev.develop("EVO-1", dev=d, tests=self._tests())
        self.assertEqual(r["status"], "merged")
        files = git(self.td, "ls-files", "agents/signals")
        self.assertIn("backtest_verdicts/v.json", files)
        self.assertNotIn("runtime_state.json", files)

    def test_failure_details_extracts_traceback(self):
        t = ("noise\n" + "=" * 70 + "\nERROR: tests.test_a (unittest.loader._FailedTest.tests.test_a)\n" + "-" * 70 +
             "\nImportError: cannot import name 'X'\n\n" + "=" * 70 + "\nFAIL: test_b (tests.test_b.T)\nboom\n")
        out = ev.failure_details(t, ["tests.test_a (unittest.loader._FailedTest.tests.test_a)"])
        self.assertIn("ImportError", out)
        self.assertNotIn("boom", out)


class WeekendWindow(unittest.TestCase):
    def test_weekend_always_open_weekday_respects_window(self):
        from datetime import datetime, timezone
        self.assertTrue(_REAL_IN_DEV_WINDOW(datetime(2026, 10, 18, 15, tzinfo=timezone.utc)))   # 周日
        self.assertTrue(_REAL_IN_DEV_WINDOW(datetime(2026, 10, 17, 9, tzinfo=timezone.utc)))    # 周六
        self.assertFalse(_REAL_IN_DEV_WINDOW(datetime(2026, 10, 20, 15, tzinfo=timezone.utc)))  # 周二交易时段
        self.assertTrue(_REAL_IN_DEV_WINDOW(datetime(2026, 10, 20, 22, tzinfo=timezone.utc)))   # 周二收盘后


class Wiring(unittest.TestCase):
    def test_jobs_and_dashboard(self):
        import _webui_watchdog as wd
        import snapshot_generator as sg
        self.assertTrue(wd.JOBS["evo_propose"]["market_quiet"])
        # 2026-10-13: worker 有 6 小时兜底 (额度恢复后重试); 只开发 approved, 见 test_unapproved_never_developed
        self.assertLessEqual(wd.JOBS["evo_worker"]["auto_every_days"], 0.5)
        self.assertTrue(wd.JOBS["evo_worker"]["market_quiet"])
        self.assertIn("/api/evolution", sg.GLOBAL_ENDPOINTS)
        self.assertNotIn("/api/evolution_token", sg.GLOBAL_ENDPOINTS)
        html = (AGENTS_DIR / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("'/api/evolution_token'", html[html.index("const PRIVATE"):html.index("const PRIVATE") + 300])
        for f in ("_evolution_propose.bat", "_evolution_worker.bat"):
            self.assertTrue((AGENTS_DIR / f).exists())


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )


class DevModelTier(unittest.TestCase):
    """2026-10-19: 框架/交易相关用 Opus, 只有不碰交易代码的小改动用 Sonnet."""

    def test_trading_category_uses_high(self):
        self.assertEqual(ev.dev_model({"category": "trading", "effort": "S"}), ev.DEV_MODEL_HIGH)

    def test_trading_file_uses_high(self):
        p = {"category": "infra", "effort": "S", "change": {"files": ["agents/claude_gate.py"]}}
        self.assertEqual(ev.dev_model(p), ev.DEV_MODEL_HIGH)

    def test_large_effort_uses_high(self):
        p = {"category": "research", "effort": "M", "change": {"files": ["agents/news_analyzer.py"]}}
        self.assertEqual(ev.dev_model(p), ev.DEV_MODEL_HIGH)

    def test_small_infra_uses_light(self):
        p = {"category": "infra", "effort": "S", "change": {"files": ["agents/_data_source_health.py"]}}
        self.assertEqual(ev.dev_model(p), ev.DEV_MODEL_LIGHT)
