# core/build_job.py
"""一次 cpp_build 的任务记录。不是队列：只保留当前这一次。"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime

from config import DATA_DIR

JOB_PATH = DATA_DIR / "cpp_build_job.json"
STOP_PATH = DATA_DIR / "cpp_build.stop"
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_job() -> dict:
    try:
        data = json.loads(JOB_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_job(payload: dict) -> None:
    JOB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = JOB_PATH.with_suffix(JOB_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(8):
        try:
            tmp.replace(JOB_PATH)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.05)


def _clear_stop() -> None:
    try:
        STOP_PATH.unlink()
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return str(pid) in (proc.stdout or "")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kill_pid(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        _kill_pid(int(proc.pid or 0))
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def request_stop() -> bool:
    """编译还在跑时写下停止标记并结束进程树。没有在跑的任务则不动。"""
    with _LOCK:
        job = _read_job()
        if job.get("status") != "running":
            return False
        try:
            pid = int(job.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if not _pid_alive(pid):
            job["status"] = "failed"
            job["reason"] = "stopped"
            _write_job(job)
            return False
        STOP_PATH.parent.mkdir(parents=True, exist_ok=True)
        STOP_PATH.write_text("stop\n", encoding="utf-8")
        _kill_pid(pid)
        job["status"] = "failed"
        job["reason"] = "stopped"
        _write_job(job)
        return True


def _drain(pipe, sink: list) -> None:
    try:
        data = pipe.read() if pipe is not None else ""
    except Exception:
        data = ""
    sink.append(data or "")


def run_command(argv: list[str], timeout_s: float) -> dict:
    """启动进程并写下 pid、开始时间、截止时间。超时或停止标记出现时结束进程树。

    reason 为 timeout / stopped 时，调用方不得把退出码当成编译成功。
    """
    timeout_s = float(timeout_s)
    _clear_stop()
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )
    out_sink: list[str] = []
    err_sink: list[str] = []
    threads = [
        threading.Thread(target=_drain, args=(proc.stdout, out_sink), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err_sink), daemon=True),
    ]
    for thread in threads:
        thread.start()
    started_at = _now()
    deadline_at = datetime.fromtimestamp(time.time() + timeout_s).isoformat(timespec="seconds")
    job = {
        "pid": proc.pid,
        "started_at": started_at,
        "deadline": deadline_at,
        "timeout_s": timeout_s,
        "status": "running",
    }
    deadline = time.monotonic() + timeout_s
    reason = "exit"
    try:
        with _LOCK:
            _write_job(job)
        while True:
            stopped = STOP_PATH.is_file()
            if stopped:
                reason = "stopped"
                _kill_tree(proc)
                break
            if proc.poll() is not None:
                reason = "stopped" if STOP_PATH.is_file() else "exit"
                break
            if time.monotonic() >= deadline:
                reason = "timeout"
                _kill_tree(proc)
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        reason = "stopped"
        _kill_tree(proc)
    except Exception:
        _kill_tree(proc)
        raise
    for thread in threads:
        thread.join(timeout=2)
    with _LOCK:
        prior = _read_job()
        if prior.get("reason") == "stopped" or STOP_PATH.is_file():
            reason = "stopped"
        job["status"] = "failed" if reason != "exit" else "exited"
        job["reason"] = reason
        job["returncode"] = proc.returncode
        _write_job(job)
        _clear_stop()
    return {
        "reason": reason,
        "pid": proc.pid,
        "started_at": started_at,
        "deadline": deadline_at,
        "timeout_s": timeout_s,
        "returncode": proc.returncode,
        "stdout": out_sink[0] if out_sink else "",
        "stderr": err_sink[0] if err_sink else "",
    }
