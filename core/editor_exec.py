# core/editor_exec.py
"""编辑器工具的唯一出口。Cursor CLI 和 LangGraph 都走这里。"""
import random

from config import (
    UE_EXEC_MODE,
    allow_anim,
    allow_blueprint,
    allow_material,
    allow_niagara,
    allow_umg,
    task_flag,
)
from core import risk
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


def _refused(task: dict, error: str) -> dict:
    from agent.harness import policy_snapshot

    return {"ok": False, "error": error, "policy": policy_snapshot(task)}


def _is_user_widget_parent(args: dict) -> bool:
    """这次 `create` 要建的是不是 Widget Blueprint。

    official 的 BlueprintTools.create 用一个 `asset_type.refPath` 表达父类；父类落在
    UMG 的 UserWidget 家族（含 UUserWidget 子类）时，monolith 的正确入口是
    `ui.create_widget_blueprint`——`blueprint.create_blueprint` 写死 UBlueprint，
    只会产出没有控件树的普通蓝图（20261029 实测：17KB 的普通 BP）。"""
    raw = (args or {}).get("asset_type")
    ref = ""
    if isinstance(raw, dict):
        ref = str(raw.get("refPath") or raw.get("path") or "")
    elif raw is not None:
        ref = str(raw)
    text = ref.strip()
    if not text:
        return False
    low = text.lower()
    return "userwidget" in low or low.startswith("/script/umg.")


def _monolith_widget_create(tool: str, args: dict, task: dict | None) -> dict | None:
    """`create` 的控件图分支。不是 Widget 就返回 None，交回原路径。"""
    if tool != "create" or not _is_user_widget_parent(args or {}):
        return None
    from backends.monolith import exec as monolith_exec

    folder = str((args or {}).get("folder_path") or "/Game").rstrip("/")
    name = str((args or {}).get("asset_name") or "").strip()
    if not name:
        return {"ok": False, "error": "没有资产名，不猜。"}
    parent_raw = (args.get("asset_type") or {}).get("refPath")
    parent = str(parent_raw or "").strip()
    params = {"save_path": f"{folder}/{name}"}
    if parent:
        # monolith 的 parent_class 认短类名或 /Script/Mod.Class；/Script/UMG.UserWidget 原样可用。
        params["parent_class"] = parent.rsplit(".", 1)[-1] if not parent.startswith("/Script/") else parent
    result = monolith_exec("ui", "create_widget_blueprint", params, task=task or {})
    if not result.get("ok"):
        return {"ok": False, "error": str(result.get("error") or "创建 Widget Blueprint 失败")}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return {
        "ok": True,
        "data": {
            "asset_path": str(data.get("asset_path") or f"{folder}/{name}"),
            "success": True,
            "widget": True,
            "parent_class": str(data.get("parent_class") or parent),
        },
    }


def exec_tool(toolset: str, tool: str, args: dict | None, *, task: dict | None = None, timeout: float | None = None) -> dict:
    """执行一个编辑器工具。返回完整 {ok, data|error}，不在这里截断。"""
    from backends.select import current_backend, official_refusal

    blocked = official_refusal()
    if blocked:
        # U3a：monolith 后端不笼统拒绝——已映射的原语走桥翻译，桥不认识才按原样拒绝。
        # mock 模式不走桥（离线检查钉 official，但双保险）。
        if current_backend() == "monolith" and UE_EXEC_MODE != "mock":
            from core import monolith_bridge

            widget_create = _monolith_widget_create(tool, args or {}, task)
            if widget_create is not None:
                return widget_create
            handled, result = monolith_bridge.exec_bridge(tool, args or {}, task or {}, timeout)
            if handled:
                return result
        return {"ok": False, "error": blocked}
    task = task or {}
    args = args or {}
    # 追加的熔断拒绝：只在现有拒绝之前，不动下面的现有文案。
    if not risk.decide(tool, task)["allow"]:
        return _refused(task, "circuit open")
    allow = set(task.get("allow_writes") or [])
    blocked = _domain_blocked(toolset, tool)
    if blocked:
        return _refused(task, blocked)
    blueprint_error = _blueprint_edit_error(tool, args, task)
    if blueprint_error:
        return _refused(task, blueprint_error)
    if tool in BLUEPRINT_EDIT_TOOLS and tool not in allow:
        return _refused(task, "write refused")
    if tool in SAVE_TOOLS and not task_flag(task, "save"):
        return _refused(task, "write refused")
    if tool in PIE_TOOLS and not task_flag(task, "pie"):
        return _refused(task, "write refused")
    if is_write_tool(tool) and tool not in allow:
        return _refused(task, "write refused")
    if UE_EXEC_MODE == "mock":
        return _mock_tool(tool, args)

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
