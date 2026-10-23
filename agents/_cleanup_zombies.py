"""
_cleanup_zombies.py
───────────────────
杀掉本项目残留的 python.exe（卡在 pause / 半死状态的测试/手动跑的进程）。

保护策略（**不杀**）：
  · `.orchestrator.lock` 里的 PID（如果还活着）—— 真正在跑的主程序
  · `.snapshot_today.lock` 里的 PID（如果还活着）—— 正在跑的 snap
  · 当前进程自己
  · 命令行不含本项目路径的 python（陌生 python 不动）

由 run.bat 启动时自动调用；也可 tools menu 手动触发。

退出码：0 总是成功。失败的 kill 仅警告，不抛错。
"""
from __future__ import annotations

import csv
import ctypes
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()  # f:\fsi-skills\agents
LOCK_FILES = (
    PROJECT_ROOT / ".orchestrator.lock",
    PROJECT_ROOT / ".snapshot_today.lock",
)

# 项目脚本列表 + 项目路径子串：命令行里命中任一即认为是"本项目 python"
_PROJECT_MARKERS = [
    "fsi-skills",  # 路径
    "orchestrator.py",
    "_snapshot_today.py",
    "_log_watch.py",
    "_cleanup_zombies.py",
    "trump_signal.py",
    "paper_trader.py",
    "regime_today.py",
    "ai_prompt.py",
    "backtest_menu.py",
    "tools_menu.py",
    "universe_picker.py",
    "pca_sox.py",
    "_backtest_",  # 任何回测脚本
]


def _pid_alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_INFORMATION = 0x0400
            h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, 0, pid)
            if not h:
                return False
            exit_code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
            kernel32.CloseHandle(h)
            return exit_code.value == 259   # STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _protected_pids() -> set[int]:
    pids = {os.getpid()}
    for lock in LOCK_FILES:
        if not lock.exists():
            continue
        try:
            pid = int(lock.read_text(encoding="utf-8").splitlines()[0].strip())
        except (OSError, ValueError, IndexError):
            continue
        if _pid_alive(pid):
            pids.add(pid)
    return pids


def _list_python_processes() -> list[tuple[int, str]]:
    """通过 PowerShell + WMI 拿所有 python.exe 的 (PID, command_line)。"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-WmiObject Win32_Process -Filter \"Name='python.exe'\" | "
             "Select-Object ProcessId, CommandLine | "
             "ConvertTo-Csv -NoTypeInformation"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15,
        )
    except Exception as exc:
        print(f"[cleanup] PowerShell 调用失败: {exc}")
        return []
    if proc.returncode != 0:
        print(f"[cleanup] WMI 查询失败 exit={proc.returncode}: {proc.stderr[:200]}")
        return []
    lines = proc.stdout.strip().splitlines()
    if len(lines) < 2:
        return []
    try:
        reader = csv.DictReader(lines)
        return [
            (int(row["ProcessId"]), row.get("CommandLine") or "")
            for row in reader
            if row.get("ProcessId", "").isdigit()
        ]
    except Exception as exc:
        print(f"[cleanup] 解析失败: {exc}")
        return []


def _is_project_python(cmdline: str) -> bool:
    if not cmdline:
        return False
    low = cmdline.lower()
    return any(m.lower() in low for m in _PROJECT_MARKERS)


def cleanup(verbose: bool = True) -> dict:
    """实际执行清理。返回 {killed: [...], kept: [...], total_py: N}。"""
    protected = _protected_pids()
    all_py = _list_python_processes()
    killed: list[tuple[int, str]] = []
    kept: list[tuple[int, str, str]] = []  # (pid, reason, cmd_preview)

    for pid, cmd in all_py:
        cmd_short = (cmd[:90] + "...") if len(cmd) > 90 else cmd
        if pid in protected:
            kept.append((pid, "active lock holder", cmd_short))
            continue
        if not _is_project_python(cmd):
            kept.append((pid, "not project python", cmd_short))
            continue
        # zombie — kill it
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True, timeout=10,
            )
            killed.append((pid, cmd_short))
        except Exception as exc:
            print(f"[cleanup] kill PID {pid} 失败: {exc}")

    if verbose:
        print(f"[cleanup] protected: {sorted(protected)}")
        print(f"[cleanup] 扫描 {len(all_py)} 个 python.exe → "
              f"杀 {len(killed)}，保留 {len(kept)}")
        for pid, cmd in killed:
            print(f"  ✗ killed PID {pid:>6}  {cmd}")
        for pid, reason, cmd in kept:
            print(f"  ✓ kept   PID {pid:>6}  [{reason}]  {cmd}")

    return {"killed": killed, "kept": kept, "total_py": len(all_py)}


if __name__ == "__main__":
    cleanup(verbose=True)
