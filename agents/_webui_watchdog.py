"""_webui_watchdog.py
─────────────────────
守护 webui.py：检查 http://127.0.0.1:8080/api/health 是否响应；
不响应就重启 webui.bat（脱离终端，daemon 模式）。

用法：
  · Windows Task Scheduler 每 5 分钟跑一次 webui_watchdog.bat
  · 也可手动跑：python _webui_watchdog.py

输出：
  signals/webui_watchdog.jsonl（每次执行追加一条 { ts, event, msg }）
  event: healthy / restart / restart_failed
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
LOG_PATH   = SCRIPT_DIR / "signals" / "webui_watchdog.jsonl"
HEALTH_URL = "http://127.0.0.1:8080/api/health"
WEBUI_BAT  = SCRIPT_DIR / "webui.bat"

# Env vars 保持与 webui.bat 一致
WEBUI_ENV = {
    "PYTHONUTF8":       "1",
    "PYTHONIOENCODING": "utf-8",
    "WEBUI_HOST":       "127.0.0.1",
    "WEBUI_PORT":       "8080",
}


def _webui_alive(timeout: int = 5) -> bool:
    try:
        req = urllib.request.Request(HEALTH_URL, headers={"User-Agent": "webui-watchdog"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _log(event: str, msg: str = "") -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts":    datetime.now(timezone.utc).isoformat(),
                "event": event,
                "msg":   msg,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _launch_webui() -> int | None:
    """脱离父进程启动 webui.bat 后台运行。"""
    env = os.environ.copy()
    env.update(WEBUI_ENV)
    try:
        DETACHED = 0x00000008
        NEW_GROUP = 0x00000200
        NO_WINDOW = 0x08000000       # CREATE_NO_WINDOW — 关键: 让 cmd/webui 完全不弹窗
        proc = subprocess.Popen(
            ["cmd.exe", "/c", str(WEBUI_BAT)],
            cwd=str(SCRIPT_DIR),
            env=env,
            creationflags=DETACHED | NEW_GROUP | NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return proc.pid
    except Exception as e:
        _log("launch_failed", f"{e}")
        return None


def main() -> int:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if _webui_alive():
        _log("healthy")
        print(f"[{now_str}] webui alive @ {HEALTH_URL}")
        return 0
    # webui 挂了 → 重启
    new_pid = _launch_webui()
    if new_pid:
        _log("restart", f"new pid={new_pid}")
        print(f"[{now_str}] webui dead → restarted (new pid {new_pid})")
        # 可选：通知
        try:
            from notifications import send_alert
            send_alert(f"webui 自愈重启 (new pid {new_pid})", level="info")
        except Exception:
            pass
        return 1
    _log("restart_failed")
    print(f"[{now_str}] webui dead + relaunch failed")
    return 2


if __name__ == "__main__":
    sys.exit(main())
