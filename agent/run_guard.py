# agent/run_guard.py
"""无主 run 与 stdin 单次消费。

入口层（`server/entry.py::_spawn`，原先的 `start.bat`）把 UE_RAG_STDIN 重定向给 run_windows。
超时转后台的 cmd 若没死干净，
会拿着同一份输入再起一个真 run。这里用一次原子改名占住文件；run 记录写 pid
和心跳，REPL 启动时把超 30 分钟无心跳的 running 改判 orphaned，并在 md 留名。
不杀进程。有活心跳时入口默认拒绝；带 --force 才放行，并在被越过的 run 的事件里
写下 run_start.forced_by。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

BUSY_TEXT = "输入已被另一个进程占用"
ORPHAN_NOTE = "此 run 无主死亡，改动证据需人工核对"
STALE_MINUTES = 30


def consumed_path(path: Path) -> Path:
    """`<原名>.consumed`：req.txt → req.txt.consumed。"""
    return Path(str(path) + ".consumed")


def _runs_dir(runs_dir: Path | None) -> Path:
    if runs_dir is not None:
        return runs_dir
    from agent.sequence import RUNS_DIR

    return RUNS_DIR


def claim_stdin(raw: str | None = None, *, runs_dir: Path | None = None) -> str:
    """占住 UE_RAG_STDIN 指向的文件。返回 skip / ok / busy。

    目标文件已经不在、只剩 .consumed：没有未过期心跳时把 .consumed 交回这次
    调用并返回 ok；有未过期心跳时仍返回 busy。
    两个活进程同时改名，只有一个 os.replace 成功，失败的一方仍是 busy。
    入口层会在重定向之前改名并设 UE_RAG_STDIN_CLAIMED=1：文件一旦被
    cmd 打开，Windows 就不许再改名（WinError 32），所以这条路径不再改第二次。
    """
    if os.environ.get("UE_RAG_STDIN_CLAIMED", "").strip() == "1":
        return "ok"
    if raw is None:
        raw = os.environ.get("UE_RAG_STDIN", "")
    text = str(raw or "").strip().strip('"')
    if not text:
        return "skip"
    path = Path(text)
    taken = consumed_path(path)
    if not path.is_file():
        if not taken.is_file():
            return "skip"
        if live_run_ids(_runs_dir(runs_dir)):
            return "busy"
        return "ok"
    try:
        os.replace(path, taken)
    except OSError:
        return "busy"
    return "ok"


def attach_claimed_stdin() -> str:
    """claim 成功且入口还没把 stdin 接上时，改从 .consumed 读。"""
    import sys

    status = claim_stdin()
    if status != "ok":
        return status
    if os.environ.get("UE_RAG_STDIN_CLAIMED", "").strip() == "1":
        return "ok"
    raw = os.environ.get("UE_RAG_STDIN", "").strip().strip('"')
    if not raw:
        return "ok"
    taken = consumed_path(Path(raw))
    if taken.is_file():
        sys.stdin = taken.open(encoding="utf-8")
    return "ok"


def _parse_ts(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _iter_run_json(runs_dir: Path):
    if not runs_dir.is_dir():
        return
    for path in runs_dir.rglob("*.json"):
        if path.name.endswith(".tmp") or ".tmp" in path.suffixes:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            yield path, data


def _heartbeat(data: dict) -> datetime | None:
    return _parse_ts(data.get("heartbeat_at") or data.get("updated_at"))


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_md(path: Path, line: str) -> None:
    md = path.with_suffix(".md")
    if md.is_file():
        text = md.read_text(encoding="utf-8")
        if line in text:
            return
        md.write_text(text.rstrip() + "\n\n" + line + "\n", encoding="utf-8")
        return
    md.write_text(f"# {path.stem}\n\n{line}\n", encoding="utf-8")


def sweep_orphan_runs(runs_dir: Path, *, now: datetime | None = None,
                      stale_minutes: int = STALE_MINUTES) -> list[str]:
    """status=running 且心跳早于 stale_minutes 的记录改判 orphaned。

    按心跳时间，不看 pid 是否还活着：测试用当前进程的 pid 配上过期时间戳，
    一样要留名。不杀进程。没有心跳时间戳的 running 也算过期。
    """
    moment = now or datetime.now()
    limit = timedelta(minutes=stale_minutes)
    marked: list[str] = []
    for path, data in _iter_run_json(runs_dir):
        if str(data.get("status") or "") != "running":
            continue
        beat = _heartbeat(data)
        if beat is not None and moment - beat <= limit:
            continue
        data["status"] = "orphaned"
        data["orphan_note"] = ORPHAN_NOTE
        _write_json(path, data)
        _append_md(path, ORPHAN_NOTE)
        marked.append(str(data.get("run_id") or path.stem))
    return marked


def live_run_ids(runs_dir: Path, *, now: datetime | None = None,
                 stale_minutes: int = STALE_MINUTES) -> list[str]:
    """仍在心跳窗口内的 running。入口只提示这些，不把过期的算成还活着。"""
    moment = now or datetime.now()
    limit = timedelta(minutes=stale_minutes)
    found: list[str] = []
    for path, data in _iter_run_json(runs_dir):
        if str(data.get("status") or "") != "running":
            continue
        beat = _heartbeat(data)
        if beat is None or moment - beat > limit:
            continue
        found.append(str(data.get("run_id") or path.stem))
    return found


def warn_live_runs(runs_dir: Path | None = None) -> int:
    """只负责发现。有活心跳返回 2 并打印 run_id，没有返回 0。不写事件。"""
    found = live_run_ids(_runs_dir(runs_dir))
    if not found:
        return 0
    print("仍在心跳的 run：" + "、".join(found))
    return 2


def _events_dir(runs_dir: Path, events_dir: Path | None) -> Path:
    if events_dir is not None:
        return events_dir
    if runs_dir.name == "runs":
        return runs_dir.parent / "events"
    return runs_dir / "events"


def admit_live_runs(runs_dir: Path | None = None, *, force: bool = False,
                    events_dir: Path | None = None) -> int:
    """菜单入口。没有活心跳返回 0。

    有活心跳且未带 force：返回 2，不写事件。
    带 force：返回 0，并在被越过的 run 的事件文件写 run_start.forced_by，
    带上本进程 pid 和该 run_id。
    """
    folder = _runs_dir(runs_dir)
    found = live_run_ids(folder)
    if not found:
        return 0
    if not force:
        print("仍在心跳的 run：" + "、".join(found))
        return 2
    sink = _events_dir(folder, events_dir)
    sink.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    moment = datetime.now().isoformat(timespec="seconds")
    for run_id in found:
        event = {
            "ts": moment,
            "type": "run_start.forced_by",
            "run_id": run_id,
            "pid": pid,
            "forced_by": {"pid": pid, "run_id": run_id},
        }
        path = sink / f"{run_id}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return 0
