# core/contract_tools.py
"""六个高层工具。包住现有查找、cpp_build 和 Automation，不复制官方工具。"""
import json
from pathlib import Path

from config import PROJECT_ROOT, UE_MCP_CALL_TIMEOUT
from core.editor_exec import exec_tool

_CONTRACT = Path(__file__).resolve().parent.parent / "contract" / "tools.json"
_LAST_BUILD = PROJECT_ROOT / "data" / "last_cpp_build.json"
_TASK = {"allow_writes": [], "save": False, "pie": False}
_ASSET = "editor_toolset.toolsets.asset.AssetTools"
_BP = "editor_toolset.toolsets.blueprint.BlueprintTools"


def contract() -> dict:
    return json.loads(_CONTRACT.read_text(encoding="utf-8"))


def tool_names() -> list[str]:
    return [item["name"] for item in contract()["tools"]]


def _timeout(name: str, default: float) -> float:
    for item in contract()["tools"]:
        if item["name"] == name:
            return float(item["timeout"])
    return default


def _err(message: str, **extra) -> dict:
    out = {"ok": False, "error": message}
    out.update(extra)
    return out


def _unwrap(data):
    if isinstance(data, str):
        text = data.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return data
    if isinstance(data, dict) and "returnValue" in data and len(data) == 1:
        return _unwrap(data["returnValue"])
    return data


def _as_list(data) -> list:
    data = _unwrap(data)
    if isinstance(data, dict):
        for key in ("returnValue", "assets", "actors", "graphs", "variables", "events"):
            if key in data:
                return _as_list(data[key])
        return []
    if isinstance(data, list):
        return data
    if data in (None, ""):
        return []
    return [data]


def _label(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("refPath", "path", "name", "graph_name", "event_name", "variable_name"):
            if item.get(key):
                return str(item[key])
    return ""


def _ref(path: str) -> str:
    text = (path or "").strip()
    leaf = text.rstrip("/").split("/")[-1]
    if "." in leaf:
        return text
    return f"{text}.{leaf}" if text else ""


def _names(data) -> list[str]:
    out = []
    for item in _as_list(data):
        label = _label(item)
        if label and label not in out:
            out.append(label.split(".")[-1][:80])
    return out


def query_actor(name: str) -> dict:
    from core.judge import _find_unique_actor, _read_actor_location

    actor_name = (name or "").strip()
    if not actor_name:
        return _err("name 为空")
    ref, err = _find_unique_actor(actor_name, _TASK)
    if err:
        return _err(str(err.get("summary") or "query_actor 失败"))
    loc, err = _read_actor_location({}, ref, _TASK)
    if err:
        return _err(str(err.get("summary") or "没有 location"))
    return {
        "ok": True,
        "name": actor_name,
        "ref_path": ref,
        "location": {
            "x": float(loc["x"]),
            "y": float(loc["y"]),
            "z": float(loc["z"]),
        },
    }


def query_blueprint(ref_path: str) -> dict:
    ref = _ref(ref_path)
    if not ref.startswith("/Game/"):
        return _err("ref_path 不是 /Game 路径")
    timeout = _timeout("query_blueprint", UE_MCP_CALL_TIMEOUT)
    bp = {"refPath": ref}
    graphs = exec_tool(_BP, "list_graphs", {"blueprint": bp}, task=_TASK, timeout=timeout)
    variables = exec_tool(_BP, "list_variables", {"blueprint": bp}, task=_TASK, timeout=timeout)
    events = exec_tool(_BP, "list_events", {"blueprint": bp}, task=_TASK, timeout=timeout)
    if not graphs.get("ok") and not variables.get("ok") and not events.get("ok"):
        return _err(str(graphs.get("error") or variables.get("error") or events.get("error") or "蓝图读取失败"))
    return {
        "ok": True,
        "ref_path": ref,
        "graphs": _names(graphs.get("data")) if graphs.get("ok") else [],
        "variables": _names(variables.get("data")) if variables.get("ok") else [],
        "events": _names(events.get("data")) if events.get("ok") else [],
    }


def find_asset(name: str) -> dict:
    asset_name = (name or "").strip()
    if not asset_name:
        return _err("name 为空")
    found = exec_tool(
        _ASSET,
        "find_assets",
        {"folder_path": "/Game", "name": asset_name, "recursive": True},
        task=_TASK,
        timeout=_timeout("find_asset", UE_MCP_CALL_TIMEOUT),
    )
    if not found.get("ok"):
        return _err(str(found.get("error") or "find_assets 失败"))
    paths = []
    for item in _as_list(found.get("data")):
        label = _label(item)
        if label and label not in paths:
            paths.append(_ref(label) if label.startswith("/Game") else label)
    if len(paths) != 1:
        return _err("资产不唯一", candidates=paths)
    return {"ok": True, "name": asset_name, "ref_path": paths[0]}


def _from_judge(result: dict) -> dict:
    errors = result.get("errors") or []
    if result.get("ok"):
        return {"ok": True, "summary": str(result.get("summary") or ""), "errors": errors}
    return _err(str(result.get("summary") or "失败"), errors=errors)


def _write_last(result: dict) -> None:
    _LAST_BUILD.parent.mkdir(parents=True, exist_ok=True)
    _LAST_BUILD.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


def build_project() -> dict:
    from core.judge import judge

    result = judge({"kind": "cpp_build"})
    _write_last(result)
    return _from_judge(result)


def run_automation_test(filter: str) -> dict:
    from core.judge import judge

    filt = (filter or "").strip()
    if not filt:
        return _err("filter 为空")
    result = judge({"kind": "automation", "filter": filt}, task=_TASK)
    if result.get("ok"):
        return {"ok": True, "summary": str(result.get("summary") or "")}
    return _err(str(result.get("summary") or "测试失败"))


def call_named(name: str, arguments: dict | None = None) -> dict:
    """只转发契约里的六个名字。表外直接拒绝，不进编辑器。"""
    arguments = arguments if isinstance(arguments, dict) else {}
    tool = (name or "").strip()
    if tool not in set(tool_names()):
        return _err(f"不在契约里：{tool or '空'}")
    if tool == "query_actor":
        return query_actor(str(arguments.get("name") or ""))
    if tool == "query_blueprint":
        return query_blueprint(str(arguments.get("ref_path") or ""))
    if tool == "find_asset":
        return find_asset(str(arguments.get("name") or ""))
    if tool == "build_project":
        return build_project()
    if tool == "run_automation_test":
        return run_automation_test(str(arguments.get("filter") or ""))
    if tool == "get_build_error":
        return get_build_error()
    return _err(f"不在契约里：{tool}")


def get_build_error() -> dict:
    if not _LAST_BUILD.is_file():
        return _err("还没有编译记录")
    result = json.loads(_LAST_BUILD.read_text(encoding="utf-8"))
    errors = result.get("errors") or []
    if result.get("ok") and not errors:
        return {"ok": True, "summary": str(result.get("summary") or "exit 0"), "errors": []}
    if not errors:
        return _err(str(result.get("summary") or "编译失败但没有错误原文"))
    return {"ok": True, "summary": str(result.get("summary") or ""), "errors": errors}
