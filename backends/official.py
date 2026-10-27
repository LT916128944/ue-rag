"""官方编辑器入口。后端不是 official 时拒绝，不改去调 Monolith。"""
from __future__ import annotations

from backends.select import official_refusal


def official_block() -> str:
    return official_refusal()


def exec_tool(toolset: str, tool: str, args: dict | None = None, *, task: dict | None = None, timeout: float | None = None) -> dict:
    blocked = official_block()
    if blocked:
        return {"ok": False, "error": blocked}
    from core.editor_exec import exec_tool as raw

    return raw(toolset, tool, args, task=task, timeout=timeout)
