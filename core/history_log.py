"""成功重试后，把错误原文追加成历史解决方案的一节。"""
from pathlib import Path

from config import DOCS_DIR


def _title(message: str) -> str:
    line = str(message or "").strip().splitlines()[0] if message else ""
    line = line.replace("#", "").strip()
    for sep in ("。", "."):
        if sep in line:
            line = line.split(sep)[0].strip()
            break
    return line[:80]


def _has_title(text: str, title: str) -> bool:
    return any(line.strip() == f"## {title}" for line in text.splitlines())


def append_history(errors: list[dict], tools: list[str], path: Path | None = None) -> int:
    """标题已存在则跳过。返回新写的节数。"""
    target = path or (DOCS_DIR / "历史解决方案.md")
    if not target.is_file():
        return 0
    text = target.read_text(encoding="utf-8")
    tool_names = [str(name) for name in tools if name]
    written = 0
    blocks = []
    seen = set()
    for err in errors:
        message = str(err.get("message") or "").strip()
        title = _title(message)
        if not title or title in seen or _has_title(text, title):
            continue
        seen.add(title)
        tools_text = ", ".join(tool_names) if tool_names else "（没有工具短名）"
        blocks.append(f"## {title}\n\n现象：{message}\n\n改法：工具 {tools_text}\n")
        written += 1
    if not blocks:
        return 0
    suffix = "\n" if text.endswith("\n") else "\n\n"
    target.write_text(text + suffix + "\n".join(blocks), encoding="utf-8")
    return written
