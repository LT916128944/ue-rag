# eval/check_dsh_lock.py
"""离线检查 DSH 网关的编辑器写锁：并发互斥 / stale 900 秒 / 伪造 pid / 只读不占锁。

mock 模式，数据目录全部重定向到临时目录，不连编辑器、不调模型、不碰真实 data/locks。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "server" / "dsh_bridge.py"
ACTOR_TOOLSET = "editor_toolset.toolsets.actor.ActorTools"
WRITE_TOOL = "set_actor_transform"
READ_TOOL = "get_actor_transform"
WRITE_ARGS = {"actor": {"refPath": "EnemySpawnPoint_1"}, "xform": {"location": {"x": 1, "y": 2, "z": 3}}}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _clear_lock(lock: Path) -> None:
    """每个用例之间清掉锁文件，避免互相污染。"""
    lock.unlink(missing_ok=True)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dsh-lock-"))
    (tmp / "runs").mkdir(parents=True, exist_ok=True)
    (tmp / "locks").mkdir(parents=True, exist_ok=True)
    task_path = tmp / "session_task.json"
    os.environ["UE_EXEC_MODE"] = "mock"
    os.environ["INJECT_ERRORS"] = "false"
    os.environ["UE_DSH_DATA_DIR"] = str(tmp)
    os.environ["UE_DSH_TASK_PATH"] = str(task_path)
    os.environ["UE_DSH_RUNS_DIR"] = str(tmp / "runs")

    # 写策略：只放行本用例要用的写工具，不保存、不进 PIE。
    task_path.write_text(
        json.dumps(
            {"allow_writes": [WRITE_TOOL], "save": False, "pie": False, "allow_blueprint_ref": ""},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    spec = importlib.util.spec_from_file_location("dsh_bridge", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # 0) 数据目录必须落在临时目录，真实 data/locks 一概不碰
    check(module._data_dir() == tmp, f"数据目录没重定向: {module._data_dir()}")
    lock = module._data_dir() / "locks" / "editor.lock"
    check(lock == tmp / "locks" / "editor.lock", f"锁文件路径异常: {lock}")

    # 1) 并发互斥：两个线程同时写，恰好一个拿到锁，另一个 editor busy
    #    把 exec_tool 换成 sleep 0.3 秒的假函数（mock，不碰编辑器），拉长临界区。
    original_exec = module.editor_exec.exec_tool

    def fake_exec(toolset, tool, args, *, task=None, timeout=None):
        time.sleep(0.3)
        return {"ok": True, "summary": "mock"}

    module.editor_exec.exec_tool = fake_exec
    try:
        barrier = threading.Barrier(2)
        results: list[dict] = []

        def worker() -> None:
            barrier.wait()
            results.append(module.exec_editor_tool(ACTOR_TOOLSET, WRITE_TOOL, dict(WRITE_ARGS)))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        check(len(results) == 2, f"并发用例没拿到两个结果: {results}")
        ok_count = sum(1 for item in results if item.get("ok") is True)
        busy = [item for item in results if item.get("ok") is False]
        check(ok_count == 1, f"并发写应恰好一个成功: {results}")
        check(len(busy) == 1 and "editor busy" in str(busy[0].get("error")), f"另一个应是 editor busy: {results}")
        check(not lock.exists(), "并发用例结束后锁文件残留")
    finally:
        module.editor_exec.exec_tool = original_exec
        _clear_lock(lock)

    # 2) stale 900 秒：锁记的 pid 活着但时间戳过旧，应被当作 stale 抢过来
    now = time.time()
    old = now - 1000
    lock.write_text(json.dumps({"pid": os.getpid(), "ts": old}), encoding="utf-8")
    os.utime(lock, (old, old))
    stale_by_age = module.exec_editor_tool(ACTOR_TOOLSET, WRITE_TOOL, dict(WRITE_ARGS))
    check(stale_by_age.get("ok") is True, f"超 900 秒的锁没被当 stale: {stale_by_age}")
    check(not lock.exists(), "stale(900s) 用例结束后锁文件没删")
    _clear_lock(lock)

    # 3) 伪造 pid：pid 不存在（999999），时间戳新鲜，也应视为 stale
    lock.write_text(json.dumps({"pid": 999999, "ts": time.time()}), encoding="utf-8")
    check(lock.exists(), "伪造 pid 用例没造出锁文件")
    stale_by_pid = module.exec_editor_tool(ACTOR_TOOLSET, WRITE_TOOL, dict(WRITE_ARGS))
    check(stale_by_pid.get("ok") is True, f"死 pid 的锁没被当 stale: {stale_by_pid}")
    check(not lock.exists(), "stale(死 pid) 用例结束后锁文件没删")
    _clear_lock(lock)

    # 4) 只读不占锁：持锁期间读工具仍能执行，不被 editor busy 挡住
    with module._editor_lock():
        check(lock.exists(), "持锁期间锁文件不存在")
        read = module.exec_editor_tool(ACTOR_TOOLSET, READ_TOOL, {"actor": {"refPath": "EnemySpawnPoint_1"}})
        check(read.get("ok") is True, f"持锁期间读工具被挡: {read}")
    check(not lock.exists(), "持锁结束锁文件没删")

    print("dsh-lock ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
