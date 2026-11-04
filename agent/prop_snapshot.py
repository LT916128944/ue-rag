# agent/prop_snapshot.py
"""写 Actor 属性前记下字段，失败且没有 keep_source 时写回。写回后按任务策略保存关卡。"""
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

    task = {"allow_writes": ["set_properties", "set_actor_transform", "set_scalar_parameter", "remove_from_scene"],
            "save": False, "pie": False}
    fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
    kind = str(item.get("kind") or "")
    if kind == "scalar":
        # U4：材质实例标量参数写回旧值。parameter/value 都在才写，缺了不动。
        name = fields.get("parameter")
        value = fields.get("value")
        if isinstance(name, str) and name.strip() and isinstance(value, (int, float)):
            exec_tool(
                "editor_toolset.toolsets.material_instance.MaterialInstanceTools",
                "set_scalar_parameter",
                {"instance": {"refPath": ref}, "name": name, "value": float(value)},
                task=task,
            )
        return
    if kind == "created":
        # U4：这扇新建出来的 Actor，失败时删掉（还原到放置前的场景）。
        _remove_created(ref, task)
        return
    if kind == "location" or isinstance(fields.get("location"), dict):
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


def _remove_created(name: str, task: dict) -> None:
    """按名字找这次放置出来的实例，恰好一个才删。找不到/多个都不动（宁可留待人工）。"""
    from agent.sequence import _actor_refs
    from core.editor_exec import exec_tool

    found = exec_tool(
        "editor_toolset.toolsets.scene.SceneTools",
        "find_actors",
        {"name": name, "tag": "", "collision_channels": []},
        task={"allow_writes": [], "save": False, "pie": False},
    )
    if not found.get("ok"):
        return
    refs = _actor_refs(found.get("data"))
    if len(refs) != 1:
        return
    exec_tool(
        "editor_toolset.toolsets.scene.SceneTools",
        "remove_from_scene",
        {"actor": {"refPath": refs[0]}},
        task=task,
    )


def restore_props(run_id: str, *, writer=None) -> list[str]:
    """写回快照里的字段；kind=created 的条目是这扇新建的 Actor，删除是安全的，重复出现也删。

    字段写回维持旧规则：同一 ref 出现多次就不写回（怕用旧值盖新值）。写回后按任务策略保存关卡。"""
    rows = _load(run_id)
    seen: dict[str, dict] = {}
    ambiguous: set[str] = set()
    created: list[str] = []
    for item in rows:
        ref = str(item.get("ref") or "")
        if not ref:
            continue
        if str(item.get("kind") or "") == "created":
            if ref not in created:
                created.append(ref)
            continue
        if ref in seen:
            ambiguous.add(ref)
            continue
        seen[ref] = item
    restored = []
    for ref in created:
        if writer is not None:
            writer(ref, {"kind": "created", "fields": {}})
        else:
            _write_back(ref, {"kind": "created", "fields": {}})
        restored.append(ref)
    for ref, item in seen.items():
        if ref in ambiguous:
            continue
        if writer is not None:
            writer(ref, item)
        else:
            _write_back(ref, item)
        restored.append(ref)
    if restored and writer is None:
        from agent.sequence import _save_level

        _save_level({})
    return restored
