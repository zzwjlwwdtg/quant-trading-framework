"""创建 agents 目录快照 zip。排除大数据缓存目录。"""
from __future__ import annotations
import sys
import zipfile
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

ROOT = Path(__file__).parent.resolve()
PARENT = ROOT.parent
BACKUP_DIR = PARENT / "backups"
BACKUP_DIR.mkdir(exist_ok=True)

STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
ZIP_PATH = BACKUP_DIR / f"agents_backup_{STAMP}_with_AB.zip"

# 排除：大缓存 + 临时文件 + 日志
EXCLUDE_DIRS = {
    "__pycache__",
    "logs",
    "signals/news_cache",
    "signals/trump_cache",
    "_archive",
}
EXCLUDE_FILES_PREFIX = (
    "option_walls_",   # 期权墙每日缓存
    "backtest_",       # 回测大文件
    "signal_history",  # 信号历史
)
EXCLUDE_EXACT = {
    ".orchestrator.lock",
    ".snapshot_today.lock",
}


def should_skip(p: Path) -> bool:
    rel = p.relative_to(ROOT).as_posix()
    if any(part in EXCLUDE_DIRS for part in rel.split("/")):
        return True
    # signals 子目录里特定前缀
    if "signals/" in rel:
        name = p.name
        if any(name.startswith(pre) for pre in EXCLUDE_FILES_PREFIX):
            return True
    if p.name in EXCLUDE_EXACT:
        return True
    return False


def main():
    print(f"Source: {ROOT}")
    print(f"Output: {ZIP_PATH}")
    print()
    # 原子写: 先写到 .tmp, 全部成功后 os.replace 到最终名. 防止中途 Ctrl+C
    # 或磁盘满导致半截 zip 被误认为有效备份.
    import os
    tmp_path = ZIP_PATH.with_suffix(".zip.tmp")
    count, total_size = 0, 0
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p in sorted(ROOT.rglob("*")):
                if not p.is_file() or should_skip(p):
                    continue
                try:
                    rel = p.relative_to(ROOT)
                    zf.write(p, arcname=str(rel))
                    count += 1
                    total_size += p.stat().st_size
                except (OSError, ValueError) as _e:
                    # 文件在 rglob 期间被删/写 → 跳过该文件, 继续 (best-effort)
                    print(f"  [warn] skip {p.name}: {_e}")
        os.replace(tmp_path, ZIP_PATH)
    except Exception:
        # 失败清理 tmp, 保证不留半截 zip
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    print(f"  archived: {count} files")
    print(f"  uncompressed total: {total_size/(1024*1024):.1f} MB")
    print(f"  zip size: {ZIP_PATH.stat().st_size/(1024*1024):.1f} MB")
    print(f"[done] {ZIP_PATH}")


if __name__ == "__main__":
    main()
