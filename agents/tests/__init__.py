"""Isolated regression tests for the Trading Agents runtime.

The suite exercises live order paths, so importing this package redirects every
mutable execution file to one temporary directory.  A test that forgets to
patch a path can no longer overwrite trader_state, stop lines, ledgers, AI call
logs, health state, alerts, or the orchestrator lock (2026-10-20: coupling tests
overwrote live LITE/TQQQ lines and wrote fake AI-call audit rows).

The live paths are snapshotted before the redirects; test_zz_live_files_sentinel
asserts the snapshots are byte-identical after the rest of the suite.
"""
from __future__ import annotations

import hashlib
import logging
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

_TMP = tempfile.TemporaryDirectory(prefix="fsi-agents-tests-")
TMP_DIR = Path(_TMP.name)

_REDIRECTS = (
    ("paper_trader", "STATE_PATH", "trader_state.json"),
    ("paper_trader", "TRADE_LOG_PATH", "trade_log.jsonl"),
    ("paper_trader", "EXECUTION_LOG_PATH", "execution_ledger.jsonl"),
    ("paper_trader", "COHORT_FIRED_LEDGER_PATH", "cohort_fired_ledger.jsonl"),
    ("paper_trader", "UNIVERSE_PATH", "universe_state.json"),
    ("fill_ledger", "EXEC_LEDGER_PATH", "execution_ledger.jsonl"),
    ("stop_line_guard", "LINES", "stop_lines.json"),
    ("stop_line_guard", "BLOCKS", "stop_line_blocks.jsonl"),
    ("orchestrator", "_REPORT_STATE_PATH", "report_state.json"),
    ("orchestrator", "_LOCK_PATH", ".orchestrator.lock"),
    ("_watchdog", "LOCK_PATH", ".orchestrator.lock"),
    ("_watchdog", "LOG_PATH", "watchdog.jsonl"),
    ("ai_prompt", "AI_CALL_LOG_PATH", "ai_calls.jsonl"),
    ("claude_gate", "HEALTH_STATE_PATH", "ai_gate_health.json"),
    ("claude_gate", "GATE_CACHE_PATH", "claude_gate_cache.json"),
    ("notifications", "ALERTS_PATH", "critical_alerts.jsonl"),
    ("notifications", "ALERT_DEDUPE_PATH", "alert_dedupe.json"),
    ("notifications", "DEDUP_PATH", "notify_dedup.json"),
)


def snapshot_path(path: Path) -> dict:
    """Content + mtime fingerprint used by the sentinel test."""
    try:
        stat = path.stat()
        data = path.read_bytes()
    except OSError:
        return {"exists": False}
    return {
        "exists": True,
        "mtime_ns": stat.st_mtime_ns,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


LIVE_SNAPSHOT: dict[str, dict] = {}
for _module_name, _attr, _filename in _REDIRECTS:
    _module = __import__(_module_name, fromlist=[_attr])
    _live_path = Path(getattr(_module, _attr))
    LIVE_SNAPSHOT[str(_live_path)] = snapshot_path(_live_path)

_PATCHES: list = []
for _module_name, _attr, _filename in _REDIRECTS:
    _module = __import__(_module_name, fromlist=[_attr])
    _patch = patch.object(_module, _attr, TMP_DIR / _filename)
    _patch.start()
    _PATCHES.append(_patch)

# Keep the file logger out of logs/run_YYYYMMDD.log; stdout logging still works.
import notifier  # noqa: E402  (import triggers logging.basicConfig)

_root = logging.getLogger()
for _handler in list(_root.handlers):
    if isinstance(_handler, notifier._DailyLogHandler):
        _root.removeHandler(_handler)
        _handler.close()
