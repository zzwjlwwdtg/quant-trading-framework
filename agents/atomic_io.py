"""Crash-safe helpers for small local state files."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


def _replace_with_retry(src: str, dst: Path) -> None:
    """os.replace with short retries (审计 P2-13, 2026-11-02).

    Windows 上目标文件正被另一进程/线程以不共享 DELETE 的方式打开时 (例如 webui
    每分钟读 trader_state), os.replace 会抛 PermissionError(WinError 5/32).
    原来没有重试: 券商已下单但本地状态落盘失败, 幂等标记丢失. 这里最多重试约 1.5 秒,
    仍失败才抛出, 调用方行为不变.
    """
    for delay in _REPLACE_RETRY_DELAYS:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(src, dst)


def atomic_write_json(path: str | Path, value: Any, *, indent: int = 2) -> None:
    """Atomically replace *path* with durable UTF-8 JSON.

    The temporary file is created beside the destination so os.replace() stays
    on one filesystem. Readers therefore see either the old complete document
    or the new complete document, never a truncated in-place write.
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            json.dump(value, tmp, indent=indent, ensure_ascii=False)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        _replace_with_retry(tmp_name, destination)
        tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                Path(tmp_name).unlink()
            except OSError:
                pass


# ── file lock helpers (cross-platform) ────────────────────────────────────────
# Windows: msvcrt.locking (byte-range lock on a sidecar .lock file)
# POSIX:   fcntl.flock (whole-file advisory lock)
# 用途: 多进程写同一 jsonl 时避免行撕裂 / hash chain read-then-append race.

if sys.platform == "win32":
    import msvcrt

    def _acquire(fh, timeout_s: float = 5.0) -> None:
        deadline = time.time() + timeout_s
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.time() > deadline:
                    raise TimeoutError("acquire lock timeout")
                time.sleep(0.05)

    def _release(fh) -> None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _acquire(fh, timeout_s: float = 5.0) -> None:
        deadline = time.time() + timeout_s
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.time() > deadline:
                    raise TimeoutError("acquire lock timeout")
                time.sleep(0.05)

    def _release(fh) -> None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def with_file_lock(target_path: str | Path, fn: Callable[[], Any], *,
                   timeout_s: float = 5.0) -> Any:
    """在 <target>.lock 上加互斥锁, 执行 fn(), 保证跨进程串行.
    fn 内部可自由 read/append 目标文件; 锁保证 read-then-append 原子性."""
    lock_path = Path(str(target_path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # r+ 需要文件存在, a+ 保证创建
    with open(lock_path, "a+") as lf:
        _acquire(lf, timeout_s=timeout_s)
        try:
            return fn()
        finally:
            _release(lf)


def append_jsonl(target_path: str | Path, entry: dict, *,
                 timeout_s: float = 5.0) -> None:
    """加锁 append 一行 JSON. 用于多进程写 nav_history / execution_ledger 等."""
    def _do():
        with open(target_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    with_file_lock(target_path, _do, timeout_s=timeout_s)
