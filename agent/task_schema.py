# agent/task_schema.py
import json
from pathlib import Path


def load_task(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("task must be a JSON object")
    if not data.get("id") or not data.get("query"):
        raise ValueError("task needs id and query")
    data.setdefault("verify", [])
    data.setdefault("allow_writes", [])
    data.setdefault("save", False)
    data.setdefault("pie", False)
    data.setdefault("max_retries", 3)
    return data
