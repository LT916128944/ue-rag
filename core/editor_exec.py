# core/editor_exec.py
"""编辑器工具的唯一出口。Cursor CLI 和 LangGraph 都走这里。"""
import random

from config import UE_EXEC_MODE, allow_anim, allow_blueprint, allow_material, allow_niagara, allow_umg
from core.ue_mcp_client import get_ue_mcp_client

# 与原先 nodes.py 的 WRITE_HINTS 同一份。
WRITE_HINTS = (
    "Set", "set_", "Spawn", "spawn_", "Delete", "delete_",
    "Destroy", "destroy_", "Add", "add_", "Remove", "remove_",
    "Move", "move_", "Create", "create_",
)

SAVE_TOOLS = {"save_assets", "save_actor"}
PIE_TOOLS = {"StartPIE", "StopPIE"}
BLUEPRINT_EDIT_TOOLS = {
    "write_graph_dsl",
    "create_node",
    "connect_pins",
    "add_event",
    "set_pin_value",
    "add_variable",
}

MOCK_ACTORS = {
    "EnemySpawnPoint_1": {"location": {"x": 0, "y": 0, "z": 100}},
    "EnemySpawnPoint_2": {"location": {"x": 200, "y": 0, "z": 100}},
}

_injection_counter = {"count": 0}


def is_write_tool(tool: str) -> bool:
    return any(hint in tool for hint in WRITE_HINTS)


_READ_PREFIXES = ("get", "list", "find", "read", "search", "discover")
_DOMAIN_SWITCHES = (
    ("material", "UE_ALLOW_MATERIAL", allow_material),
    ("umg", "UE_ALLOW_UMG", allow_umg),
    ("niagara", "UE_ALLOW_NIAGARA", allow_niagara),
    ("animblueprint", "UE_ALLOW_ANIM", allow_anim),
    ("animation", "UE_ALLOW_ANIM", allow_anim),
    ("controlrig", "UE_ALLOW_ANIM", allow_anim),
)


def _domain_blocked(toolset: str, tool: str) -> str:
    if tool.lower().startswith(_READ_PREFIXES):
        return ""
    lowered = toolset.lower()
    for token, key, allowed in _DOMAIN_SWITCHES:
        if token in lowered and not allowed():
            return f"{key} 不是 true"
    return ""


def _mock_tool(tool: str, args: dict) -> dict:
    import os
    inject = os.getenv("INJECT_ERRORS", "true").lower() == "true"
    error_rate = float(os.getenv("ERROR_RATE", "0.1"))
    max_injections = int(os.getenv("MAX_INJECTIONS", "2"))
    if (
        inject
        and _injection_counter["count"] < max_injections
        and random.random() < error_rate
    ):
        _injection_counter["count"] += 1
        return {"ok": False, "error": "Actor not found"}

    if tool == "find_actors":
        return {
            "ok": True,
            "data": [{"name": key, "refPath": key, "location": value["location"]} for key, value in MOCK_ACTORS.items()],
        }
    if tool == "get_actor_transform":
        actor_name = (args.get("actor") or {}).get("refPath", "")
        found = MOCK_ACTORS.get(actor_name)
        if not found:
            return {"ok": False, "error": "not found"}
        return {"ok": True, "data": {"returnValue": {"location": found["location"]}}}
    if tool == "set_actor_transform":
        actor_name = (args.get("actor") or {}).get("refPath", "")
        new_xform = args.get("xform") or {}
        if actor_name in MOCK_ACTORS and "location" in new_xform:
            MOCK_ACTORS[actor_name]["location"] = new_xform["location"]
            return {"ok": True, "data": {"success": True}}
        return {"ok": False, "error": "not found"}
    if tool == "get_current_level":
        return {"ok": True, "data": {"returnValue": "/Game/Maps/L_NewTest"}}
    if tool in ("compile_blueprint", "DiscoverTests", "RunTestsByFilter", "GetTestResults"):
        return {"ok": True, "data": None}
    return {"ok": False, "error": f"Unknown tool: {tool}"}


def _blueprint_ref(args: dict) -> str:
    blueprint = args.get("blueprint") if isinstance(args, dict) else None
    if isinstance(blueprint, dict):
        return str(blueprint.get("refPath") or "").strip()
    if isinstance(blueprint, str):
        return blueprint.strip()
    return ""


def _blueprint_edit_error(tool: str, args: dict, task: dict) -> str:
    """全局开关缺省关闭。任务文件写明的那一张图可以改，其它图仍拒绝。"""
    if tool not in BLUEPRINT_EDIT_TOOLS:
        return ""
    allowed = str(task.get("allow_blueprint_ref") or "").strip()
    if allowed:
        if _blueprint_ref(args) != allowed:
            return "blueprint edit disabled"
        return ""
    if not allow_blueprint():
        return "blueprint edit disabled"
    return ""


def exec_tool(toolset: str, tool: str, args: dict | None, *, task: dict | None = None, timeout: float | None = None) -> dict:
    """执行一个编辑器工具。返回完整 {ok, data|error}，不在这里截断。"""
    task = task or {}
    args = args or {}
    allow = set(task.get("allow_writes") or [])
    blocked = _domain_blocked(toolset, tool)
    if blocked:
        return {"ok": False, "error": blocked}
    blueprint_error = _blueprint_edit_error(tool, args, task)
    if blueprint_error:
        return {"ok": False, "error": blueprint_error}
    if tool in BLUEPRINT_EDIT_TOOLS and tool not in allow:
        return {"ok": False, "error": "write refused"}
    if UE_EXEC_MODE == "mock":
        return _mock_tool(tool, args)

    if tool in SAVE_TOOLS and not task.get("save"):
        return {"ok": False, "error": "write refused"}
    if tool in PIE_TOOLS and not task.get("pie"):
        return {"ok": False, "error": "write refused"}
    if is_write_tool(tool) and tool not in allow:
        return {"ok": False, "error": "write refused"}

    client = get_ue_mcp_client()
    result = client.call_ue_tool(toolset, tool, args, timeout=timeout)
    if not _connection_refused(result):
        return result
    from core.editor_launch import ensure_editor
    from core.ue_mcp_client import reset_ue_mcp_client

    brought = ensure_editor()
    if not brought.get("ok"):
        result["error"] = f"{result.get('error')}；{brought.get('note')}"
        return result
    reset_ue_mcp_client()
    return get_ue_mcp_client().call_ue_tool(toolset, tool, args, timeout=timeout)


def _connection_refused(result: dict) -> bool:
    if result.get("ok"):
        return False
    text = str(result.get("error") or "")
    return any(token in text for token in ("10061", "拒绝连接", "积极拒绝", "ConnectError", "Connection refused"))
