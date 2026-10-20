# agent/prop_snapshot.py
"""写 Actor 属性前记下字段，失败且没有 keep_source 时写回。不保存关卡，不 PIE。"""
import json
import os
from pathlib import Path

_OBJECT = "editor_toolset.toolsets.object.ObjectTools"
_ACTOR = "editor_toolset.toolsets.actor.ActorTools"


def props_path(run_id: str) -> Path:
    override = os.environ.get("UE_SNAPSHOT_DIR", "").strip()
    if override:
        root = Path(override)
    else:
        from config import DATA_DIR

        root = DATA_DIR / "snapshots"
    return root / str(run_id) / "props.json"


def _load(run_id: str) -> list[dict]:
    path = props_path(run_id)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("items") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def capture_props(run_id: str, ref: str, fields: dict, *, kind: str = "props") -> None:
    if not str(run_id or "").strip() or not str(ref or "").strip() or not isinstance(fields, dict):
        return
    rows = _load(run_id)
    rows.append({"ref": str(ref), "kind": kind, "fields": fields})
    path = props_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"items": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_back(ref: str, item: dict) -> None:
    from core.editor_exec import exec_tool

    task = {"allow_writes": ["set_properties", "set_actor_transform"], "save": False, "pie": False}
    fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
    if item.get("kind") == "location" or isinstance(fields.get("location"), dict):
        loc = fields.get("location") if isinstance(fields.get("location"), dict) else fields
        exec_tool(
            _ACTOR,
            "set_actor_transform",
            {"actor": {"refPath": ref}, "xform": {"location": loc}, "worldspace": True},
            task=task,
        )
        return
    values = {}
    if "bPickable" in fields:
        values["bPickable"] = fields.get("bPickable")
    if "Tags" in fields:
        values["Tags"] = fields.get("Tags")
    if not values:
        return
    exec_tool(
        _OBJECT,
        "set_properties",
        {"instance": {"refPath": ref}, "values": json.dumps(values, ensure_ascii=False)},
        task=task,
    )


def restore_props(run_id: str, *, writer=None) -> list[str]:
    """同一 ref 出现多次就不写回。不调用保存。"""
    rows = _load(run_id)
    seen: dict[str, dict] = {}
    ambiguous: set[str] = set()
    for item in rows:
        ref = str(item.get("ref") or "")
        if not ref:
            continue
        if ref in seen:
            ambiguous.add(ref)
            continue
        seen[ref] = item
    restored = []
    for ref, item in seen.items():
        if ref in ambiguous:
            continue
        if writer is not None:
            writer(ref, item)
        else:
            _write_back(ref, item)
        restored.append(ref)
    return restored
