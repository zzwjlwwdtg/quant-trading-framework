"""
_watchdog.py
─────────────
守护 orchestrator：检查 .orchestrator.lock 里的 PID 是否还活着；
不活就清 stale lock + 用同一套 env vars 重启（脱离终端）。

用法：
  · Windows Task Scheduler 每 15 或 30 分钟跑一次 watchdog.bat
  · 也可手动跑：python _watchdog.py

输出：
  signals/watchdog.jsonl（每次执行追加一条 { ts, event, pid, msg }）
  event: healthy / dead_restart / no_lock_start / launch_failed
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
LOCK_PATH  = SCRIPT_DIR / ".orchestrator.lock"
LOG_PATH   = SCRIPT_DIR / "signals" / "watchdog.jsonl"
PY         = r"C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"

# 与 run.bat 一致的运行时 env
ORCH_ENV = {
    "PYTHONUTF8":                    "1",
    "PYTHONIOENCODING":              "utf-8",
    "TRADER_DRY_RUN":                "0",
    "CLAUDE_DECISION_GATE":          "1",
    "CLAUDE_DECISION_MODE":          "gate",
    "CLAUDE_DECISION_TIMEOUT_SEC":   "180",
    "CLAUDE_DECISION_FAIL_CLOSED":   "1",
    "CLAUDE_DECISION_FALLBACK_CODEX":"0",
    "TECHNICAL_ONLY":                "1",
    "TRADER_LIVE_FRACTION":          "1.0",
    "TRADER_SIM_ACTIVE":             "1",
    "CRISIS_VBOUNCE_ENABLED":        "1",    # 2026-07-31 backtest 5d hit 80% avg +6% → 开启（probe 30%）
}


def _pid_alive(pid: int) -> bool:
    try:
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(0x0400, 0, pid)
        if not h:
            return False
        exit_code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
        kernel32.CloseHandle(h)
        return exit_code.value == 259   # STILL_ACTIVE
    except Exception:
        return False


def _log(event: str, pid=None, msg: str = "") -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts":    datetime.now(timezone.utc).isoformat(),
                "event": event,
                "pid":   pid,
                "msg":   msg,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _launch_orchestrator() -> int | None:
    """脱离父进程启动 orchestrator。返回子进程 PID。"""
    env = os.environ.copy()
    env.update(ORCH_ENV)
    try:
        DETACHED = 0x00000008
        NEW_GROUP = 0x00000200
        proc = subprocess.Popen(
            [PY, "-X", "utf8", "orchestrator.py"],
            cwd=str(SCRIPT_DIR),
            env=env,
            creationflags=DETACHED | NEW_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return proc.pid
    except Exception as e:
        _log("launch_failed", None, f"{e}")
        return None


def main() -> int:
    # 情况 1：锁存在，PID 活着 → healthy
    # 情况 2：锁存在，PID 死了 → 清 lock + restart
    # 情况 3：锁不存在 → 直接 start
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if LOCK_PATH.exists():
        try:
            old_pid = int(LOCK_PATH.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            old_pid = None
        if old_pid and _pid_alive(old_pid):
            _log("healthy", old_pid)
            print(f"[{now_str}] orchestrator PID {old_pid} 存活")
            return 0
        # stale lock
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass
        new_pid = _launch_orchestrator()
        _log("dead_restart", new_pid, f"stale lock had PID {old_pid}")
        print(f"[{now_str}] 检测到 orchestrator 死亡 (旧 PID {old_pid})，已重启 → 新 PID {new_pid}")
        try:
            from notifications import notify_watchdog
            notify_watchdog("dead_restart", old_pid=old_pid, new_pid=new_pid)
        except Exception:
            pass
        return 1
    # 无锁
    new_pid = _launch_orchestrator()
    _log("no_lock_start", new_pid)
    print(f"[{now_str}] 无锁文件，orchestrator 未在跑，已启动 → PID {new_pid}")
    try:
        from notifications import notify_watchdog
        notify_watchdog("no_lock_start", new_pid=new_pid)
    except Exception:
        pass
    return 2


if __name__ == "__main__":
    sys.exit(main())
