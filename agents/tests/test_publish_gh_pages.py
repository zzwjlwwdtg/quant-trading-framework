"""公开快照发布到 gh-pages (2026-10-11 用户决定: 旧快照没有意义, 不进历史)."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import _publish_gh_pages as gp


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class PublishGhPages(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        d = self.repo / "docs"
        (d / "data").mkdir(parents=True)
        (d / "index.html").write_text("<html></html>", encoding="utf-8")
        (d / "data" / "_meta.json").write_text("{}", encoding="utf-8")
        (d / ".nojekyll").write_text("", encoding="utf-8")
        (d / "dashboard-full.png").write_bytes(b"\x89PNG")
        (self.repo / "code.py").write_text("x = 1\n", encoding="utf-8")
        git(self.repo, "add", "-A"); git(self.repo, "commit", "-qm", "main")

    def test_orphan_commit_with_docs_at_root_without_png(self):
        with patch.object(gp, "REPO", self.repo), patch.object(gp, "DOCS", self.repo / "docs"):
            commit, n = gp.build_commit("pages snapshot test")
        files = set(git(self.repo, "ls-tree", "-r", "--name-only", commit).splitlines())
        self.assertEqual(files, {"index.html", "data/_meta.json", ".nojekyll"})
        self.assertEqual(n, 3)
        parents = git(self.repo, "rev-list", "--parents", "-n", "1", commit).split()
        self.assertEqual(len(parents), 1)                      # 没有父提交 = 历史不保留
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")   # main 的 index / 工作区不受影响
        self.assertFalse((self.repo / ".git" / "gh-pages-publish.index").exists())

    def test_refuses_incomplete_docs(self):
        (self.repo / "docs" / "data" / "_meta.json").unlink()
        with patch.object(gp, "REPO", self.repo), patch.object(gp, "DOCS", self.repo / "docs"):
            with self.assertRaises(RuntimeError):
                gp.build_commit("x")

    def test_restore_from_gh_pages_when_new_payload_is_empty(self):
        import snapshot_generator as sg
        # gh-pages 上有一份健康的旧快照
        with patch.object(gp, "REPO", self.repo), patch.object(gp, "DOCS", self.repo / "docs"):
            (self.repo / "docs" / "data" / "positions.json").write_text('{"positions": [' + '1,' * 400 + '1]}', encoding="utf-8")
            commit, _ = gp.build_commit("x")
        git(self.repo, "update-ref", "refs/heads/gh-pages", commit)
        (self.repo / "docs" / "data" / "positions.json").unlink()
        with patch.object(sg, "REPO_ROOT", self.repo), patch.object(sg, "DATA_DIR", self.repo / "docs" / "data"), \
             patch.object(sg, "_PRE_SNAPSHOT_SIZES", {"positions.json": 2000}):
            r = sg._save("/api/positions", {"error": "backend down"})
        self.assertIsNone(r)
        self.assertIn('"positions": [1,', (self.repo / "docs" / "data" / "positions.json").read_text(encoding="utf-8"))

    def test_snap_public_publishes_and_has_transition_switch(self):
        bat = (AGENTS_DIR / "snap_public.bat").read_text(encoding="utf-8")
        self.assertIn("_publish_gh_pages.py", bat)
        self.assertIn(".pages_gh_only", bat)
        self.assertLess(bat.index("_publish_gh_pages.py"), bat.index("git push origin main"))
        gh_only = bat[bat.index('if exist "%SCRIPT_DIR%.pages_gh_only"'):]
        self.assertLess(gh_only.index("git push origin main"), gh_only.index("goto :end"))   # 代码提交照常推送
        self.assertTrue((AGENTS_DIR / ".pages_gh_only").exists())


if __name__ == "__main__":
    unittest.main()
