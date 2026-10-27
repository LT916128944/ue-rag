"""Monolith 写许可。只读前缀放行；拿不准的 action 当写。不回退官方 MCP。"""
from __future__ import annotations

from config import (
    allow_ai,
    allow_anim,
    allow_blueprint,
    allow_material,
    allow_monolith_write,
    allow_niagara,
    allow_umg,
    task_flag,
)

_READ_PREFIXES = (
    "get", "list", "find", "read", "search", "discover",
    "validate", "compare", "audit", "explain", "resolve",
)
_COMPILE = {"compile_blueprint", "compile_widget"}
_PIE = {"pie", "start_pie", "stop_pie"}
_DOMAIN = {
    "blueprint": ("UE_ALLOW_BLUEPRINT", allow_blueprint),
    "material": ("UE_ALLOW_MATERIAL", allow_material),
    "ui": ("UE_ALLOW_UMG", allow_umg),
    "animation": ("UE_ALLOW_ANIM", allow_anim),
    "niagara": ("UE_ALLOW_NIAGARA", allow_niagara),
    "ai": ("UE_ALLOW_AI", allow_ai),
}
_GRAPH_FILTERS = ("filter", "graph_name", "node_id", "node_ids")


def is_read(action: str) -> bool:
    name = (action or "").strip().lower()
    if name in _COMPILE:
        return True
    return name.startswith(_READ_PREFIXES)


def unfiltered_graph(action: str, params: dict | None) -> str:
    """get_graph_data 不带过滤时拒绝，让调用方改用 get_graph_summary。"""
    if (action or "").strip().lower() != "get_graph_data":
        return ""
    body = params or {}
    if any(body.get(key) not in (None, "", [], {}) for key in _GRAPH_FILTERS):
        return ""
    return "get_graph_data 不带过滤已拒绝，先用 get_graph_summary"


def write_block(namespace: str, action: str, task: dict | None = None) -> str:
    """返回拒绝原文。空字符串表示这次可以执行。"""
    task = task or {}
    ns = (namespace or "").strip().lower()
    name = (action or "").strip().lower()
    if ns == "pcg":
        return "Monolith 无此命名空间"
    if name == "run_python":
        if task.get("allow_python") is True:
            return ""
        return "allow_python 未写，拒绝 run_python"
    if name in _PIE:
        if task_flag(task, "pie"):
            return ""
        return "pie 未放行"
    if name == "save" or name.startswith("save_"):
        if task_flag(task, "save"):
            return ""
        return "save 未放行"
    if name == "load_level":
        if task_flag(task, "save"):
            return ""
        return "load_level 看任务书的 save，不跟 UE_ALLOW_MONOLITH_WRITE"
    if is_read(name):
        return ""
    key, reader = _DOMAIN.get(ns, ("UE_ALLOW_MONOLITH_WRITE", allow_monolith_write))
    if reader():
        return ""
    return f"{key} 不是 true"
