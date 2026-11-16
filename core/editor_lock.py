# core/editor_lock.py
"""编辑器写锁。DSH 与菜单 2 / 菜单 3 共用这一把，不按后端拆开。

锁文件仍是 data/locks/editor.lock（UE_DSH_DATA_DIR 覆盖数据目录时跟着走）。
只读不进这里。超过 900 秒，或锁里的 pid 已经不在，算过期，下一次写可以抢走。
同一线程重入不再占第二次，避免网关和执行器各锁一次把自己挡死。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

STALE_SECONDS = 900.0

_local = threading.local()


def data_dir() -> Path:
    override = os.environ.get("UE_DSH_DATA_DIR", "").strip()
    if override:
        return Path(override)
    from config import DATA_DIR

    return DATA_DIR


def lock_path() -> Path:
    return data_dir() / "locks" / "editor.lock"


def _pid_alive(pid: int) -> bool:
    """持锁进程还在不在。Windows 不能用 os.kill(pid, 0)（那会真把进程杀掉），走 tasklist。

    查不出来时按「还活着」算：宁可多等 900 秒，也不抢活进程的锁造成并发写编辑器。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return True
        return str(pid) in proc.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _lock_stale(lock: Path) -> bool:
    """超过 900 秒，或记的 pid 已经不在了。读不懂锁文件就当它还持有。"""
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return True
    if age > STALE_SECONDS:
        return True
    try:
        held = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(held, dict):
        return False
    try:
        pid = int(held.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    return not _pid_alive(pid)


def _holder(lock: Path) -> dict:
    """读持有者。创建和写入之间有一小段空窗，忙的一方多等几次再报。"""
    last: dict = {}
    for _ in range(20):
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and data.get("pid"):
            return data
        if isinstance(data, dict):
            last = data
        time.sleep(0.01)
    return last


def _busy_error(lock: Path) -> RuntimeError:
    held = _holder(lock)
    pid = held.get("pid", "")
    backend = held.get("backend", "")
    run_id = held.get("run_id", "")
    return RuntimeError(f"editor busy pid={pid} backend={backend} run_id={run_id}")


def _lock_body() -> dict:
    from config import editor_backend

    now = time.time()
    return {
        "pid": os.getpid(),
        "ts": now,
        "heartbeat_at": now,
        "backend": editor_backend(),
        "run_id": os.environ.get("UE_RAG_RUN_ID", "").strip(),
    }


@contextmanager
def editor_lock():
    depth = getattr(_local, "depth", 0)
    if depth:
        _local.depth = depth + 1
        try:
            yield
        finally:
            _local.depth -= 1
        return

    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_stale(path):
                path.unlink(missing_ok=True)
                continue
            raise _busy_error(path) from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(_lock_body(), handle)
        acquired = True
        break
    if not acquired:
        raise _busy_error(path)
    _local.depth = 1
    try:
        yield
    finally:
        _local.depth = 0
        path.unlink(missing_ok=True)
