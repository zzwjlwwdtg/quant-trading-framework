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
  event: healthy / dead_restart / no_lock_start / launch_failed / opend_check_failed

2026-09-29: 先调用 _opend_watchdog.main() 守护 OpenD (任务计划直接跑本脚本,
watchdog.bat 里的 OpenD 步骤从未按计划执行). OpenD 检查失败不阻断 orchestrator 检查.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
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
    # 2026-10-08 用户决定: 默认用 Claude (Codex 需要经常换账号), Claude 失败/限额时退回 Codex
    "AI_CLI_PRIMARY":                "claude",
    "AI_CLI_FALLBACK":               "codex",
    "CLAUDE_DECISION_GATE":          "1",
    "CLAUDE_DECISION_MODE":          "gate",
    "CLAUDE_DECISION_TIMEOUT_SEC":   "180",
    "CLAUDE_DECISION_FAIL_CLOSED":   "1",
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


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong),
                ("dwHighDateTime", ctypes.c_ulong)]


def process_info(pid: int | None) -> dict | None:
    """Return {pid, started_utc, image} for a live PID, else None.

    Start time + image name are used together with the PID so a recycled PID
    cannot make the watchdog report a dead orchestrator as healthy (P1-4).
    """
    if not pid:
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if not _pid_alive(pid):
        return None
    info = {"pid": pid, "started_utc": None, "image": None}
    if os.name != "nt":
        return info
    handle = None
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, 0, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return info
        created, exited = _FILETIME(), _FILETIME()
        kernel, user = _FILETIME(), _FILETIME()
        if kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user)):
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            started = (datetime(1601, 1, 1, tzinfo=timezone.utc)
                       + timedelta(microseconds=ticks // 10))
            info["started_utc"] = started.isoformat()
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            info["image"] = Path(buf.value).name
    except Exception:
        pass
    finally:
        if handle:
            try:
                ctypes.windll.kernel32.CloseHandle(handle)
            except Exception:
                pass
    return info


def write_lock_metadata(path: Path, pid: int | None = None) -> dict:
    """Claim marker: first line stays the bare PID for run.bat compatibility."""
    pid = int(pid or os.getpid())
    info = process_info(pid) or {}
    lines = [str(pid)]
    for key in ("started_utc", "image"):
        if info.get(key):
            lines.append(f"{key}={info[key]}")
    lines.append("script=orchestrator.py")
    lines.append(f"created_utc={datetime.now(timezone.utc).isoformat()}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return info


def read_lock(path: Path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return {}
    try:
        meta = {"pid": int(lines[0])}
    except ValueError:
        return {}
    for line in lines[1:]:
        key, sep, value = line.partition("=")
        if sep and value:
            meta[key.strip()] = value.strip()
    return meta


def lock_matches_process(meta: dict) -> bool:
    """True only when the lock belongs to the currently running process."""
    pid = (meta or {}).get("pid")
    info = process_info(pid)
    if not info:
        return False
    expected_start = meta.get("started_utc")
    actual_start = info.get("started_utc")
    if expected_start and actual_start:
        try:
            a = datetime.fromisoformat(expected_start.replace("Z", "+00:00"))
            b = datetime.fromisoformat(actual_start.replace("Z", "+00:00"))
        except ValueError:
            return False
        if abs((a - b).total_seconds()) > 2:
            return False
    expected_image = meta.get("image")
    actual_image = info.get("image")
    if expected_image and actual_image and expected_image.lower() != actual_image.lower():
        return False
    return True


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


def _ensure_opend() -> int | None:
    """先守护 OpenD (orchestrator 依赖它). 任何异常只记日志, 不阻断后续检查."""
    try:
        import _opend_watchdog
        return _opend_watchdog.main()
    except Exception as e:
        _log("opend_check_failed", None, f"{type(e).__name__}: {e}")
        return None


def main() -> int:
    _ensure_opend()
    # 情况 1：锁存在，PID 活着 → healthy
    # 情况 2：锁存在，PID 死了 → 清 lock + restart
    # 情况 3：锁不存在 → 直接 start
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if LOCK_PATH.exists():
        meta = read_lock(LOCK_PATH)
        old_pid = meta.get("pid")
        if old_pid and lock_matches_process(meta):
            _log("healthy", old_pid)
            print(f"[{now_str}] orchestrator PID {old_pid} alive (fingerprint match)")
            return 0
        stale_info = process_info(old_pid) if old_pid else None
        reason = f"stale lock had PID {old_pid}"
        if stale_info:
            reason = (f"lock fingerprint mismatch for live PID {old_pid} "
                      f"(lock={meta.get('started_utc')}/{meta.get('image')}, "
                      f"process={stale_info.get('started_utc')}/"
                      f"{stale_info.get('image')})")
        # stale lock (dead PID, recycled PID, or unreadable metadata)
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass
        new_pid = _launch_orchestrator()
        _log("dead_restart", new_pid, reason)
        print(f"[{now_str}] orchestrator dead/lock mismatch (old PID {old_pid}), "
              f"restarted -> PID {new_pid}")
        try:
            from notifications import notify_watchdog
            notify_watchdog("dead_restart", old_pid=old_pid, new_pid=new_pid)
        except Exception:
            pass
        return 1
    # 无锁
    new_pid = _launch_orchestrator()
    _log("no_lock_start", new_pid)
    print(f"[{now_str}] no lock file, orchestrator not running, started -> PID {new_pid}")
    try:
        from notifications import notify_watchdog
        notify_watchdog("no_lock_start", new_pid=new_pid)
    except Exception:
        pass
    return 2


if __name__ == "__main__":
    sys.exit(main())
