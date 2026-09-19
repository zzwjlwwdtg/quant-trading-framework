"""_opend_watchdog.py — 守护 moomoo OpenD.

检查 moomoo_OpenD.exe 进程是否活着 + port 11111 是否监听. 死了就 detached 拉起
exe. OpenD 需要在 GUI 里预先配置 "记住密码 + 自动登录", 本脚本只负责 exe 启动.

用法:
  · Windows Task Scheduler 每 5-15 分钟跑一次 (可以塞进 watchdog.bat 与
    _watchdog.py 串行执行 — OpenD 先起, orchestrator 再起, 顺序对)
  · 手动: python _opend_watchdog.py

输出:
  signals/opend_watchdog.jsonl (每次执行 append: { ts, event, pid, msg })
  event: healthy / port_dead_restart / process_dead_restart / launch_failed
       / launched_waiting_login / already_launching
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import OPEND_HOST, OPEND_PORT, _cfg

SCRIPT_DIR = Path(__file__).parent
LOG_PATH   = SCRIPT_DIR / "signals" / "opend_watchdog.jsonl"

# OpenD 冷启动 + auto-login 大约 15-30s. watchdog.bat 里 orchestrator 紧随其后跑,
# 若不等 port bound 就返回, orchestrator 起来时会连到未 login 的 OpenD → 静默 hang
# (这正是 opend_watchdog 本来要修的 bug). 所以启动后必须等 port bound 才返回.
POST_LAUNCH_POLL_INTERVAL_SEC = 3
POST_LAUNCH_POLL_MAX_SEC      = 45

# exe 位置: 默认 moomoo_OpenD 10.10.7008 installer path. 可用 env override.
DEFAULT_OPEND_EXE = r"C:\Users\masa\AppData\Roaming\moomoo_OpenD\moomoo_OpenD.exe"
OPEND_EXE = _cfg("MOOMOO_OPEND_EXE", DEFAULT_OPEND_EXE)

# process image name for tasklist 匹配 (moomoo installer 用大小写敏感 exe 名)
OPEND_PROC_NAMES = ("moomoo_OpenD.exe", "FutuOpenD.exe")


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """OpenD 端口可否建立 TCP 连接."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _opend_process_alive() -> int | None:
    """tasklist 查 moomoo_OpenD/FutuOpenD 进程. 返 PID 或 None."""
    try:
        r = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
    except Exception:
        return None
    for line in r.stdout.splitlines():
        for name in OPEND_PROC_NAMES:
            if name.lower() in line.lower():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 2 and parts[1].isdigit():
                    return int(parts[1])
    return None


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


def _launch_opend() -> int | None:
    """脱离父进程启动 OpenD. 返 PID. auto-login 由 OpenD 自身处理."""
    if not Path(OPEND_EXE).exists():
        _log("launch_failed", None, f"exe not found: {OPEND_EXE}")
        return None
    try:
        DETACHED = 0x00000008
        NEW_GROUP = 0x00000200
        proc = subprocess.Popen(
            [OPEND_EXE],
            cwd=str(Path(OPEND_EXE).parent),
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
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pid = _opend_process_alive()
    port_ok = _port_open(OPEND_HOST, OPEND_PORT)

    # healthy: 进程活 + 端口通 (登录成功)
    if pid and port_ok:
        _log("healthy", pid)
        print(f"[{now_str}] OpenD healthy PID={pid} port {OPEND_PORT} ok")
        return 0

    # 进程活但端口未通: 大概率还在启动 / 登录中, 不重复 launch
    if pid and not port_ok:
        _log("launched_waiting_login", pid,
             f"process alive but port {OPEND_PORT} not listening (登录中?)")
        print(f"[{now_str}] OpenD PID={pid} 存在但端口未通 (登录中?), 不重复启动")
        return 3

    # 进程不存在: 启动 + 轮询 port bound (阻塞 max 45s), 防止 orchestrator watchdog
    # 紧随其后启动 orchestrator 时 OpenD 还没登录完 → 静默 hang
    new_pid = _launch_opend()
    if not new_pid:
        _log("launch_failed", None, f"exe={OPEND_EXE}")
        print(f"[{now_str}] OpenD 启动失败 exe={OPEND_EXE}")
        return 4
    print(f"[{now_str}] OpenD 已启动 PID={new_pid}, 等 port {OPEND_PORT} bound...")
    deadline = time.monotonic() + POST_LAUNCH_POLL_MAX_SEC
    poll_n = 0
    while time.monotonic() < deadline:
        poll_n += 1
        if _port_open(OPEND_HOST, OPEND_PORT, timeout=1.0):
            elapsed = int(POST_LAUNCH_POLL_MAX_SEC - (deadline - time.monotonic()))
            _log("process_dead_restart", new_pid,
                 f"launched + port bound after {elapsed}s ({poll_n} polls)")
            print(f"[{now_str}] OpenD PID={new_pid} port bound after {elapsed}s")
            return 1
        time.sleep(POST_LAUNCH_POLL_INTERVAL_SEC)
    # timeout: OpenD 起来了但 port 一直没通 (auto-login 失败? 网络?) — 不阻死后续 watchdog
    _log("launched_but_port_never_bound", new_pid,
         f"waited {POST_LAUNCH_POLL_MAX_SEC}s, port {OPEND_PORT} 仍未监听 (login 失败?)")
    print(f"[{now_str}] ⚠ OpenD PID={new_pid} 起了但等 {POST_LAUNCH_POLL_MAX_SEC}s "
          f"port 未 bound (login 失败? 手动 check GUI)")
    return 5


if __name__ == "__main__":
    sys.exit(main())
