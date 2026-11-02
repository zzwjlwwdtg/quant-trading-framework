"""2026-11-02: 交易关键代码合并审计."""
import unittest
from unittest.mock import patch

import _merge_audit as ma
import notifications

CRIT = {"agents/paper_trader.py", "agents/config.py"}


def _c(h, files):
    return {"commit": h, "author": "x", "date": "2026-11-02T10:00:00+09:00", "subject": "s", "files": files}


class AuditTests(unittest.TestCase):
    def test_unapproved_critical_commit_flagged(self):
        bad = ma.audit([_c("abc1234567", ["agents/paper_trader.py"]),
                        _c("def7654321", ["agents/README.md"])], [], CRIT)
        self.assertEqual([b["commit"] for b in bad], ["abc1234567"])
        self.assertEqual(bad[0]["critical_touched"], ["agents/paper_trader.py"])

    def test_approved_prefix_clears(self):
        self.assertEqual(ma.audit([_c("abc1234567", ["agents/config.py"])], ["abc1234"], CRIT), [])

    def test_short_prefix_ignored(self):
        with patch.object(ma, "APPROVALS_PATH") as p:
            p.read_text.return_value = '{"commit": "abc"}\n{"commit": "abc1234"}\n'
            self.assertEqual(ma.approvals(), ["abc1234"])

    def test_critical_list_includes_guard_and_trader(self):
        crit = ma.critical_files()
        self.assertIn("agents/stop_line_guard.py", crit)
        self.assertIn("agents/paper_trader.py", crit)

    def test_run_alerts_once_per_commit(self):
        sent = []
        send = lambda msg, level, dedup: sent.append(msg) or {"sent": [], "skipped": ["discord_no_config"]}
        with patch.object(ma, "main_commits", return_value=[_c("abc1234567", ["agents/paper_trader.py"])]), \
             patch.object(ma, "critical_files", return_value=CRIT):
            ma.run(send=send)
            ma.run(send=send)
        self.assertEqual(len(sent), 1)
        self.assertIn("abc1234567", sent[0])


if __name__ == "__main__":
    raise SystemExit("use: python -m unittest discover -s tests -t .")
