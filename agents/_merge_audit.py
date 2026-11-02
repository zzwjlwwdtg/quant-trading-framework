"""交易关键代码合并审计 (2026-11-02).

背景: 2026-10-22 / 10-23 有 AI 用用户的 git/GitHub 身份把 P2-16/17 与 "TLT 核心交易标的"
合并进 main, 用户事先没有批准. 本机 AI 有完整命令行权限, 技术上无法绝对拦住,
所以这里做"事后必定发现": 每天检查 main 上 (first-parent) 改动交易关键文件的提交,
没有在 signals/merge_approvals.jsonl 登记过用户批准的, 推送告警 (每个提交只推一次).

批准登记由协调者在用户于对话中确认后写入, 一行一个:
  {"commit": "<hash 前缀>", "approved_at": "...", "by": "user-chat", "quote": "用户原话"}

用法: python _merge_audit.py run
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SIGNALS = HERE / "signals"
APPROVALS_PATH = SIGNALS / "merge_approvals.jsonl"
STATE_PATH = SIGNALS / "merge_audit.json"
SINCE = "2026-11-02T00:00:00+09:00"    # 之前的提交已人工复核 (TLT / P2-16/17 另有事后说明)
EXTRA_CRITICAL = ("agents/stop_line_guard.py", "agents/strategy_engine.py", "agents/atomic_io.py",
                  "agents/_watchdog.py", "agents/evolution.py")


def critical_files() -> set[str]:
    from evolution import TRADING_CRITICAL
    return set(TRADING_CRITICAL) | set(EXTRA_CRITICAL)


def _git(args: list[str]) -> str:
    kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    r = subprocess.run(["git", "-c", "gc.auto=0", *args], cwd=str(REPO), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120, **kw)
    return r.stdout if r.returncode == 0 else ""


def main_commits(since: str = SINCE) -> list[dict]:
    """main 的 first-parent 提交 (合并提交按相对第一父的差异计), 带改动文件."""
    out = _git(["log", "main", "--first-parent", "-m", f"--since={since}",
                "--format=@@%H%x09%an%x09%aI%x09%s", "--name-only"])
    commits, cur = [], None
    for line in out.splitlines():
        if line.startswith("@@"):
            h, an, at, subj = (line[2:].split("\t") + ["", "", ""])[:4]
            cur = {"commit": h, "author": an, "date": at, "subject": subj, "files": []}
            commits.append(cur)
        elif line.strip() and cur is not None:
            cur["files"].append(line.strip().replace("\\", "/"))
    return commits


def approvals() -> list[str]:
    out = []
    try:
        for line in APPROVALS_PATH.read_text(encoding="utf-8").splitlines():
            try:
                c = str(json.loads(line).get("commit") or "").strip()
            except ValueError:
                continue
            if len(c) >= 7:
                out.append(c)
    except OSError:
        pass
    return out


def audit(commits: list[dict], approved: list[str], critical: set[str]) -> list[dict]:
    bad = []
    for c in commits:
        touched = sorted(set(c["files"]) & critical)
        if not touched:
            continue
        if any(c["commit"].startswith(a) for a in approved):
            continue
        bad.append({**c, "critical_touched": touched})
    return bad


def run(send=None) -> dict:
    import notifications
    from atomic_io import atomic_write_json
    send = send or notifications.send_alert
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    alerted = set(state.get("alerted") or [])
    bad = audit(main_commits(), approvals(), critical_files())
    new = [b for b in bad if b["commit"] not in alerted]
    for b in new:
        msg = (f"⚠️ 未登记用户批准的交易关键代码已进入 main: {b['commit'][:10]} "
               f"({b['author']}, {b['date'][:16]}) {b['subject'][:80]}；"
               f"改动: {', '.join(x.replace('agents/', '') for x in b['critical_touched'][:6])}。"
               f"如果你没有批准过，请告诉协调的 AI 回滚。")
        res = send(msg, level="warning", dedup=False)
        if notifications.alert_delivered(res):
            alerted.add(b["commit"])
    result = {"ts": datetime.now(timezone.utc).isoformat(), "unapproved": [
        {k: b[k] for k in ("commit", "author", "date", "subject", "critical_touched")} for b in bad],
        "alerted": sorted(alerted)}
    try:
        atomic_write_json(STATE_PATH, result)
    except Exception:
        pass
    return result


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        print(json.dumps(run(), ensure_ascii=False, indent=1))
    else:
        print("usage: python _merge_audit.py run")
