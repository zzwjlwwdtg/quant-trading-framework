"""把公开快照 (docs/) 发布到 gh-pages 分支, 每次覆盖成只有 1 个提交 (2026-10-11 用户决定).

为什么: 快照每 30 分钟提交到 main 会让 main 的历史不停增长 (旧快照没有任何意义).
做法: 用临时 index 把 docs/ 的内容 (不含 README 用的截图 *.png) 打成一棵树, 生成一个
没有父提交的孤立提交, 强制推到 gh-pages. 旧快照随之从分支上消失; main 的代码历史不受影响.
不碰工作区, 不碰 main 的 index.

用法: python _publish_gh_pages.py [--dry-run]   (--dry-run 只建提交不推送)
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
BRANCH = "gh-pages"
EXCLUDE_SUFFIXES = (".png",)          # README 截图只在 main 里用, 网页不需要
REQUIRED = ("index.html", "data/_meta.json", ".nojekyll")


def _git(args, env=None, cwd=None, input_text=None) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd or REPO), env=env, input=input_text,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:500]}")
    return r.stdout.strip()


def build_commit(message: str) -> tuple[str, int]:
    missing = [p for p in REQUIRED if not (DOCS / p).exists()]
    if missing:
        raise RuntimeError(f"docs/ 缺少 {missing}, 不发布")
    idx = REPO / ".git" / "gh-pages-publish.index"
    env = dict(os.environ, GIT_INDEX_FILE=str(idx))
    try:
        if idx.exists():
            idx.unlink()
        _git(["read-tree", "--empty"], env=env)
        files = [p.relative_to(DOCS).as_posix() for p in DOCS.rglob("*")
                 if p.is_file() and p.suffix.lower() not in EXCLUDE_SUFFIXES]
        # --work-tree=docs: 路径以 docs/ 为根; -f: 不受 .gitignore 影响
        _git(["--work-tree", str(DOCS), "add", "-f", "--pathspec-from-file=-"],
             env=env, input_text="\n".join(files) + "\n")
        tree = _git(["write-tree"], env=env)
        commit = _git(["commit-tree", tree, "-m", message], env=env)
        return commit, len(files)
    finally:
        try:
            idx.unlink()
        except OSError:
            pass


def main(argv) -> int:
    dry = "--dry-run" in argv
    msg = f"pages snapshot {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} (single commit, history not kept)"
    commit, n = build_commit(msg)
    print(f"[gh-pages] commit {commit[:9]} files={n}")
    if dry:
        print("[gh-pages] dry-run: not pushed")
        return 0
    _git(["push", "-f", "origin", f"{commit}:refs/heads/{BRANCH}"])
    print(f"[gh-pages] pushed to {BRANCH}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:  # noqa: BLE001
        print(f"[gh-pages] FAILED: {e}")
        sys.exit(1)
