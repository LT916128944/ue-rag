# eval/check_orphan_run.py
"""无主 run 与 stdin 单次消费（20261028-2058 P0-1）。

临时目录里造一条 status=running、pid=当前进程、心跳超 30 分钟的记录，
断言被改判 orphaned，md 补上留名。已消费的输入文件第二次 claim 必须拒绝。
不连编辑器、不杀进程。
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eval._pin_offline import close_now

close_now()


def check_stale_running_is_orphaned(tmp: Path) -> None:
    from agent.run_guard import ORPHAN_NOTE, sweep_orphan_runs, warn_live_runs

    day = tmp / "20261028"
    day.mkdir()
    stale_id = "20261028-010101"
    stale = {
        "run_id": stale_id,
        "status": "running",
        "pid": os.getpid(),
        "heartbeat_at": (datetime.now() - timedelta(minutes=31)).isoformat(timespec="seconds"),
    }
    stale_path = day / f"{stale_id}.json"
    stale_path.write_text(json.dumps(stale), encoding="utf-8")
    (day / f"{stale_id}.md").write_text(f"# {stale_id}\n", encoding="utf-8")

    marked = sweep_orphan_runs(tmp)
    assert marked == [stale_id], marked
    data = json.loads(stale_path.read_text(encoding="utf-8"))
    assert data["status"] == "orphaned", data
    assert ORPHAN_NOTE in (day / f"{stale_id}.md").read_text(encoding="utf-8")
    # 再扫一次：已经留名的不再改写。
    assert sweep_orphan_runs(tmp) == []

    fresh_id = "20261028-020202"
    fresh = {
        "run_id": fresh_id,
        "status": "running",
        "pid": os.getpid(),
        "heartbeat_at": datetime.now().isoformat(timespec="seconds"),
    }
    fresh_path = day / f"{fresh_id}.json"
    fresh_path.write_text(json.dumps(fresh), encoding="utf-8")
    assert sweep_orphan_runs(tmp) == [], "心跳还在的 run 不能改判"
    assert json.loads(fresh_path.read_text(encoding="utf-8"))["status"] == "running"
    assert warn_live_runs(tmp) == 2


def check_stdin_claim_is_exclusive(tmp: Path) -> None:
    from agent.run_guard import BUSY_TEXT, claim_stdin, consumed_path

    tmp.mkdir(parents=True, exist_ok=True)
    source = tmp / "req.txt"
    source.write_text("改射速\n", encoding="utf-8")
    assert claim_stdin(str(source)) == "ok"
    taken = consumed_path(source)
    assert taken.is_file() and not source.exists()
    assert taken.read_text(encoding="utf-8") == "改射速\n"
    assert claim_stdin(str(source)) == "busy"
    assert BUSY_TEXT == "输入已被另一个进程占用"
    assert claim_stdin("") == "skip"
    os.environ["UE_RAG_STDIN_CLAIMED"] = "1"
    try:
        assert claim_stdin(str(taken)) == "ok"
        assert taken.is_file()
    finally:
        os.environ.pop("UE_RAG_STDIN_CLAIMED", None)


def main() -> int:
    import tempfile

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        check_stale_running_is_orphaned(tmp)
        check_stdin_claim_is_exclusive(tmp / "stdin")
    print("orphan_run ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
