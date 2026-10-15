# agent/ledger.py
"""范围级台账：done / partial / pending。任务书的「尚未完成」从这里取。
只给机器读；给人的记录仍走 .cursor/memory/任务记录/，两边不互相替代。"""
import json
import threading
from datetime import datetime
from pathlib import Path

from config import PROJECT_ROOT

LEDGER_PATH = PROJECT_ROOT / "data" / "ledger.json"
_LOCK = threading.Lock()


def read_ledger(path: Path | None = None) -> dict:
    target = Path(path) if path is not None else LEDGER_PATH
    empty = {"scopes": [], "external": [], "updated": ""}
    if not target.is_file():
        return empty
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("scopes", [])
    data.setdefault("external", [])
    data.setdefault("updated", "")
    return data


def update_ledger(run_id: str, scopes: list[dict] | None, path: Path | None = None) -> dict:
    """集成结果回写台账。scopes: [{id,title,status,run_id,pending,note}]。按 id 合并，原子写。"""
    with _LOCK:
        data = read_ledger(path)
        by_id: dict[str, dict] = {}
        for item in data.get("scopes") or []:
            if isinstance(item, dict) and item.get("id"):
                by_id[str(item["id"])] = item
        now = datetime.now().isoformat(timespec="seconds")
        for scope in scopes or []:
            if not isinstance(scope, dict) or not str(scope.get("id") or "").strip():
                continue
            entry = {
                "id": str(scope["id"]),
                "title": str(scope.get("title") or ""),
                "status": str(scope.get("status") or "pending"),
                "run_id": str(run_id),
                "updated": now,
                "pending": [str(x) for x in scope.get("pending") or [] if str(x).strip()],
            }
            if str(scope.get("note") or "").strip():
                entry["note"] = str(scope["note"])
            by_id[entry["id"]] = entry
        data["scopes"] = list(by_id.values())
        data["updated"] = now
        target = Path(path) if path is not None else LEDGER_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(target)
        return data


def ledger_summary_text(path: Path | None = None, limit: int = 600) -> str:
    """给任务书生成器的台账摘要：范围级 done/pending，几百字以内。"""
    data = read_ledger(path)
    lines: list[str] = []
    for scope in data.get("scopes") or []:
        if not isinstance(scope, dict) or not scope.get("id"):
            continue
        status = str(scope.get("status") or "pending")
        title = str(scope.get("title") or scope.get("id"))
        if status == "done":
            lines.append(f"{scope.get('id')} {title}：done")
            continue
        pending = [str(x) for x in scope.get("pending") or [] if str(x).strip()]
        left = "；".join(pending[:4])
        lines.append(f"{scope.get('id')} {title}：{status}" + (f"（剩：{left}）" if left else ""))
    if not lines:
        return "台账为空：还没有按范围累计的进度。"
    return "；".join(lines)[:limit]
