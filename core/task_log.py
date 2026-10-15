"""任务改过 Source 且成功时，追加当天任务记录。只读当天文件末尾。"""
from datetime import date
from pathlib import Path

from config import UE_PROJECT_DIR


def _task_dir() -> Path:
    return UE_PROJECT_DIR / ".cursor" / "memory" / "任务记录"


def append_task_record(task: dict, tools: list[str], path: Path | None = None) -> bool:
    day = date.today().isoformat()
    target = path or (_task_dir() / f"{day}.md")
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
