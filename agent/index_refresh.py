# agent/index_refresh.py
"""新建蓝图后的索引一致性。

编辑器关着：把 shortcuts\\asset.bat 写入队列。UE_INDEX_AUTO_RUN=1 才真正拉起。
编辑器开着：只提示人执行，不关编辑器。续接由调用方跳过，这里不重复排队。"""
import json
import os
from pathlib import Path


def status_path() -> Path:
    override = os.environ.get("UE_INDEX_STATUS", "").strip()
    if override:
        return Path(override)
    from config import DATA_DIR

    return DATA_DIR / "index_status.json"


def read_status(path: Path | None = None) -> dict:
    target = Path(path) if path is not None else status_path()
    if not target.is_file():
        return {"stale": False, "hint": ""}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"stale": False, "hint": ""}
    if not isinstance(data, dict):
        return {"stale": False, "hint": ""}
    return {"stale": bool(data.get("stale")), "hint": str(data.get("hint") or "")}


def index_is_fresh(path: Path | None = None) -> bool:
    return not read_status(path).get("stale")


def mark_stale(hint: str, path: Path | None = None) -> None:
    target = Path(path) if path is not None else status_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"stale": True, "hint": str(hint or "")}
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)


def mark_fresh(path: Path | None = None) -> None:
    target = Path(path) if path is not None else status_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"stale": False, "hint": ""}
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)


def schedule_index_refresh(*, editor_open: bool, queue_path: Path, runner=None) -> dict:
    """排队 asset.bat。编辑器开着不跑。自动执行必须同时关掉编辑器且 UE_INDEX_AUTO_RUN=1。"""
    record = {
        "command": r"shortcuts\asset.bat",
        "editor_open": bool(editor_open),
        "mode": "hint" if editor_open else "queued",
        "auto_run": False,
    }
    target = Path(queue_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    auto = os.environ.get("UE_INDEX_AUTO_RUN", "").strip() == "1"
    if (not editor_open) and auto and runner is not None:
        runner()
        record["auto_run"] = True
    return record
