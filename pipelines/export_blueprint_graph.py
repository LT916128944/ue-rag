"""编辑器开着时，只读导出蓝图的图名、变量和事件。不改图。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import BP_GRAPH_JSONL
from core.editor_exec import exec_tool

_ASSET = "editor_toolset.toolsets.asset.AssetTools"
_BP = "editor_toolset.toolsets.blueprint.BlueprintTools"
_TASK = {"allow_writes": [], "save": False, "pie": False}
_SKIP = ("widget", "wbp_", "animbp", "abp_", "niagara", "umg")
_CAP = 1200


def _unwrap(data):
    if isinstance(data, dict) and "returnValue" in data:
        inner = data["returnValue"]
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except json.JSONDecodeError:
                return inner
        return _unwrap(inner)
    return data


def _as_list(data):
    data = _unwrap(data)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return [data]
    if isinstance(data, dict):
        for key in ("returnValue", "assets", "graphs", "variables", "events", "names"):
            if key in data:
                return _as_list(data[key])
        return []
    if isinstance(data, list):
        return data
    return []


def _label(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("name", "graph_name", "event_name", "variable_name", "refPath", "path"):
            if item.get(key):
                return str(item[key])
    return ""


def _ref(path: str) -> str:
    text = path.strip()
    leaf = text.rstrip("/").split("/")[-1]
    if "." in leaf:
        return text
    return f"{text}.{leaf}"


def _skip(path: str) -> bool:
    low = path.lower()
    return any(token in low for token in _SKIP)


def _names(data, limit=40) -> list[str]:
    out = []
    for item in _as_list(data):
        label = _label(item)
        if label:
            out.append(label.split(".")[-1][:80])
        if len(out) >= limit:
            break
    return out


def main() -> int:
    found = exec_tool(
        _ASSET,
        "find_assets",
        {
            "folder_path": "/Game",
            "name": "",
            "asset_type": {"refPath": "/Script/Engine.Blueprint"},
            "recursive": True,
        },
        task=_TASK,
    )
    if not found.get("ok"):
        print(f"bp index skipped: {found.get('error')}")
        return 0
    paths = []
    for item in _as_list(found.get("data")):
        label = _label(item)
        if label and not _skip(label):
            paths.append(_ref(label))
    records = []
    for ref in paths:
        bp = {"refPath": ref}
        graphs = exec_tool(_BP, "list_graphs", {"blueprint": bp}, task=_TASK)
        variables = exec_tool(_BP, "list_variables", {"blueprint": bp}, task=_TASK)
        events = exec_tool(_BP, "list_events", {"blueprint": bp}, task=_TASK)
        if not graphs.get("ok") and not variables.get("ok") and not events.get("ok"):
            continue
        graph_names = _names(graphs.get("data")) if graphs.get("ok") else []
        variable_names = _names(variables.get("data")) if variables.get("ok") else []
        event_names = _names(events.get("data")) if events.get("ok") else []
        content = "；".join([
            f"蓝图 {ref}",
            "图 " + ",".join(graph_names) if graph_names else "",
            "变量 " + ",".join(variable_names) if variable_names else "",
            "事件 " + ",".join(event_names) if event_names else "",
        ])
        content = content[:_CAP]
        records.append({
            "id": f"bpgraph:{ref}",
            "source_type": "ue_bp_graph",
            "source_path": ref,
            "content": content,
            "metadata": {
                "ref_path": ref,
                "graph_count": str(len(graph_names)),
                "variable_count": str(len(variable_names)),
                "event_count": str(len(event_names)),
            },
        })
    BP_GRAPH_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with BP_GRAPH_JSONL.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"bp index blueprints={len(paths)} records={len(records)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
