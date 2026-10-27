"""任务改过 Source 且成功时，追加当天任务记录。只读当天文件末尾。

人读的正本在 `.cursor/memory/任务记录/`。同一段再镜像到 `.codex/memory/任务记录/`。
显式传入 path 时只写那一个文件，给测试用。"""
from datetime import date
from pathlib import Path

from config import task_record_dirs


def _append_one(task: dict, tools: list[str], target: Path) -> bool:
    day = date.today().isoformat()
    query = str(task.get("query") or task.get("id") or "").strip().splitlines()[0][:80]
    tool_names = ", ".join(str(name) for name in tools if name) or "（无）"
    block = (
        f"## {task.get('id') or '任务'}\n\n"
        f"### 任务 1：{query or task.get('id')}\n"
        f"- **目标**：{query or task.get('id')}\n"
        "- **操作**：\n"
        f"  - 工具 {tool_names}\n"
        "- **结果**：任务成功\n"
        "- **经验**：\n"
        "  - 一次性结果留在本记录\n"
    )
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {day}\n\n{block}\n<!-- APPEND -->\n", encoding="utf-8")
        return True
    text = target.read_text(encoding="utf-8")
    tail = text[-4000:]
    if f"## {task.get('id')}" in tail:
        return False
    insertion = "\n" + block + "\n"
    marker = "<!-- APPEND -->"
    if marker in text:
        updated = text.replace(marker, insertion + marker, 1)
    else:
        updated = text.rstrip() + "\n" + insertion + marker + "\n"
    target.write_text(updated, encoding="utf-8")
    return True


def append_task_record(task: dict, tools: list[str], path: Path | None = None) -> bool:
    if path is not None:
        return _append_one(task, tools, path)
    primary, mirror = task_record_dirs()[1], task_record_dirs()[0]
    day = date.today().isoformat()
    wrote = _append_one(task, tools, primary / f"{day}.md")
    _append_one(task, tools, mirror / f"{day}.md")
    return wrote
