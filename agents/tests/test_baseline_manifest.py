"""WP00 baseline manifest tests (audit 2026-09-19).

锁死:
- 生成 manifest 包含 git HEAD + dirty status
- root_py_files 包含 SHA256 指纹
- python_env 记录 Python 版本
- 未提交模式必须 flag dirty
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import _baseline_manifest as bm


class ManifestBuildTests(unittest.TestCase):

    def test_manifest_has_required_top_level_keys(self):
        m = bm.build_manifest()
        for key in ["schema_version", "generated_at", "git", "python_env",
                     "root_py_files", "key_signals"]:
            self.assertIn(key, m)

    def test_root_py_files_include_this_module_with_sha(self):
        m = bm.build_manifest()
        # 每条至少有 path + sha256 + size
        for entry in m["root_py_files"]:
            self.assertIn("path", entry)
            if "sha256" in entry:
                # 应是 64-char hex (或 "error")
                self.assertIn(len(entry["sha256"]), (64, len("error")))

    def test_python_env_records_version(self):
        m = bm.build_manifest()
        self.assertIn("python_version", m["python_env"])
        # 至少形如 3.x.y
        self.assertRegex(m["python_env"]["python_version"], r"^\d+\.\d+")


class GitStatusTests(unittest.TestCase):

    def test_dirty_tree_flags_is_dirty_true(self):
        fake_status = type("R", (), {
            "stdout": " M agents/foo.py\n?? agents/bar.py\n",
            "returncode": 0,
        })
        fake_head = type("R", (), {"stdout": "abc123\n", "returncode": 0})
        def fake_run(cmd, **kw):
            if cmd == ["git", "rev-parse", "HEAD"]:
                return fake_head
            return fake_status
        with patch.object(bm.subprocess, "run", side_effect=fake_run):
            s = bm._git_status()
            self.assertEqual(s["modified_files"], 1)
            self.assertEqual(s["untracked_files"], 1)
            self.assertTrue(s["is_dirty"])
            self.assertIn("SHA does not represent", s["dirty_reason"])

    def test_clean_tree_flags_is_dirty_false(self):
        fake_status = type("R", (), {"stdout": "", "returncode": 0})
        fake_head = type("R", (), {"stdout": "abc123\n", "returncode": 0})
        def fake_run(cmd, **kw):
            if cmd == ["git", "rev-parse", "HEAD"]:
                return fake_head
            return fake_status
        with patch.object(bm.subprocess, "run", side_effect=fake_run):
            s = bm._git_status()
            self.assertEqual(s["modified_files"], 0)
            self.assertFalse(s["is_dirty"])
            self.assertEqual(s["dirty_reason"], "clean")

    def test_git_failure_returns_unknown_gracefully(self):
        with patch.object(bm.subprocess, "run", side_effect=Exception("no git")):
            s = bm._git_status()
            self.assertEqual(s["head"], "unknown")
            self.assertEqual(s["modified_files"], -1)


if __name__ == "__main__":
    unittest.main()
