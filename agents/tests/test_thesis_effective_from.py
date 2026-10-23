"""Thesis 生效时间 (数据项, 2026-10-02).

Codex: 两条归档 thesis 缺 effective_from → 历史查询只能返回 unknown.
- 归档数据按可核实证据回填 (git 提交时间 = "不晚于" 上界), 见
  development/2026-10-02/THESIS_EFFECTIVE_FROM.md.
- 以后 promote: archive_thesis_for_promotion 给新 thesis 写 effective_from
  (= 旧版 retired_at, UTC 带时区), 不再产生缺字段的版本. 已有值不覆盖.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import thesis_config


class PromotionStampsEffectiveFrom(unittest.TestCase):
    def _run(self, new_thesis):
        with tempfile.TemporaryDirectory() as td:
            arch = Path(td) / "a.jsonl"
            with patch.object(thesis_config, "_ARCHIVE_PATH", arch), \
                 patch.object(thesis_config, "_load", return_value={"version": "old"}):
                thesis_config.archive_thesis_for_promotion(new_thesis, "unit")
            return json.loads(arch.read_text(encoding="utf-8").splitlines()[0])

    def test_new_thesis_gets_effective_from_equal_retired_at(self):
        new = {"version": "next"}
        entry = self._run(new)
        self.assertEqual(new["effective_from"], entry["retired_at"])
        self.assertIsNotNone(datetime.fromisoformat(entry["retired_at"]).tzinfo)

    def test_existing_effective_from_kept(self):
        new = {"version": "next", "effective_from": "2030-01-01T00:00:00+00:00"}
        self._run(new)
        self.assertEqual(new["effective_from"], "2030-01-01T00:00:00+00:00")


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
