# eval/check_orphan_run.py
"""无主 run 与 stdin 单次消费。

临时目录里造一条 status=running、pid=当前进程、心跳超 30 分钟的记录，
断言被改判 orphaned，md 补上留名。两个进程抢同一份输入，改名只成功一次。
没有活心跳时，残留的 .consumed 可以交回；有活心跳时仍拒绝。
有活心跳且不带 --force 的入口返回 2；带 --force 返回 0，事件里有 forced_by。
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
    empty = tmp / "norun"
    empty.mkdir()
    source = tmp / "req.txt"
    source.write_text("改射速\n", encoding="utf-8")
    assert claim_stdin(str(source), runs_dir=empty) == "ok"
    taken = consumed_path(source)
    assert taken.is_file() and not source.exists()
    assert taken.read_text(encoding="utf-8") == "改射速\n"
    # 没有未过期心跳：残留的 .consumed 交回这次调用。
    assert claim_stdin(str(source), runs_dir=empty) == "ok"
    assert taken.is_file() and taken.read_text(encoding="utf-8") == "改射速\n"
    # 有未过期心跳：仍拒绝。同时改名失败的那条路仍是 busy，这里钉的是残留文件。
    runs = tmp / "runs" / "20261111"
    runs.mkdir(parents=True)
    live_id = "20261111-050505"
    (runs / f"{live_id}.json").write_text(json.dumps({
        "run_id": live_id,
        "status": "running",
        "pid": os.getpid(),
        "heartbeat_at": datetime.now().isoformat(timespec="seconds"),
    }), encoding="utf-8")
    assert claim_stdin(str(source), runs_dir=tmp / "runs") == "busy"
    assert BUSY_TEXT == "输入已被另一个进程占用"
    assert claim_stdin("") == "skip"
    os.environ["UE_RAG_STDIN_CLAIMED"] = "1"
    try:
        assert claim_stdin(str(taken)) == "ok"
        assert taken.is_file()
    finally:
        os.environ.pop("UE_RAG_STDIN_CLAIMED", None)


def check_live_run_requires_force(tmp: Path) -> None:
    from agent.run_guard import admit_live_runs

    runs = tmp / "runs"
    day = runs / "20261111"
    day.mkdir(parents=True)
    run_id = "20261111-040404"
    (day / f"{run_id}.json").write_text(json.dumps({
        "run_id": run_id,
        "status": "running",
        "pid": os.getpid(),
        "heartbeat_at": datetime.now().isoformat(timespec="seconds"),
    }), encoding="utf-8")
    events = tmp / "events"
    assert admit_live_runs(runs, force=False, events_dir=events) == 2
    assert not events.exists() or not any(events.glob("*.jsonl"))
    assert admit_live_runs(runs, force=True, events_dir=events) == 0
    text = (events / f"{run_id}.jsonl").read_text(encoding="utf-8")
    assert "forced_by" in text
    assert str(os.getpid()) in text
    assert run_id in text


def main() -> int:
    import tempfile

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        check_stale_running_is_orphaned(tmp)
        check_stdin_claim_is_exclusive(tmp / "stdin")
        check_live_run_requires_force(tmp / "gate")
    print("orphan_run ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
