"""_cache_prune.py — 周期清理 signals/ 下的缓存, 防止无限增长.

规则:
  trump_cache/parsed_*.json  — 删 mtime > 30 天的
  trump_cache/truth_live.csv — 保留 (追加型日志, 后续会加 rotation)
  options_flow/state.json    — 若 > 20 MB, 归档到 state.YYYYMMDD.json.gz 再重开一个空的
  news_cache/*.json          — 删 mtime > 14 天的

用法: python _cache_prune.py (weekly.bat 自动调用, 或手动)
"""
from __future__ import annotations

import gzip
import shutil
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

from config import SIGNALS_DIR

SIG = Path(SIGNALS_DIR)
NOW = time.time()

PRUNE_RULES = [
    # (glob, max_age_days, human_desc)
    ("trump_cache/parsed_*.json", 30, "trump parsed 缓存"),
    ("news_cache/*.json",         14, "news 缓存"),
    ("options_flow/*_snapshot_*.json", 7, "options snapshot 缓存"),
    ("*claude_gate_prompt_*",     30, "gate prompt 快照"),
    ("*claude_gate_raw_*",        30, "gate raw 输出"),
]

# 大文件归档阈值
ROTATE_RULES = [
    # (glob, max_mb, human_desc)
    ("options_flow/state.json",    20, "options_flow state"),
    ("point_in_time_market.jsonl", 50, "point-in-time market"),
    ("signal_history.json",        10, "signal_history"),
    ("trump_cache/truth_live.csv", 10, "trump truth_live"),
    ("policy_toolkit_history.jsonl", 20, "policy toolkit history"),
    ("thesis_history.jsonl",       20, "thesis history"),
    ("rebalance_plan.jsonl",       20, "rebalance plan"),
]


def _prune_by_age():
    total_freed = 0
    total_deleted = 0
    for pattern, max_days, desc in PRUNE_RULES:
        max_age_sec = max_days * 86400
        deleted = 0
        freed_bytes = 0
        for p in SIG.glob(pattern):
            try:
                age_sec = NOW - p.stat().st_mtime
                if age_sec > max_age_sec:
                    freed_bytes += p.stat().st_size
                    p.unlink()
                    deleted += 1
            except Exception:
                continue
        if deleted:
            mb = freed_bytes / (1024 * 1024)
            print(f"  [prune] {desc}: 删 {deleted} 个文件, 释放 {mb:.2f} MB")
            total_freed += freed_bytes
            total_deleted += deleted
    return total_freed, total_deleted


def _rotate_by_size():
    total_rotated = 0
    for rel_path, max_mb, desc in ROTATE_RULES:
        p = SIG / rel_path
        if not p.exists():
            continue
        try:
            size_mb = p.stat().st_size / (1024 * 1024)
        except Exception:
            continue
        if size_mb <= max_mb:
            continue
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(p.stat().st_mtime))
        archive_path = p.with_name(f"{p.stem}.{stamp}{p.suffix}.gz")
        try:
            with open(p, "rb") as src, gzip.open(archive_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            # 清空原文件 (append-only 场景保持文件存在, 让后续写不失败)
            p.write_text("", encoding="utf-8")
            print(f"  [rotate] {desc}: {size_mb:.1f} MB → 归档 {archive_path.name}, 原文件已清空")
            total_rotated += 1
        except Exception as exc:
            print(f"  [rotate] {desc}: 归档失败 {exc}")
    return total_rotated


def main():
    print("=" * 60)
    print(f"cache prune @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    freed, deleted = _prune_by_age()
    rotated = _rotate_by_size()
    total_freed_mb = freed / (1024 * 1024)
    print(f"\n汇总: 删除 {deleted} 个过期文件 (释放 {total_freed_mb:.2f} MB), 归档 {rotated} 个大文件")


if __name__ == "__main__":
    main()
