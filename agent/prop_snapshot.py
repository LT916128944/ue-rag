# agent/prop_snapshot.py
"""写前快照。失败时按记录做补偿，不是编辑器 Undo。

每条记录带动作、目标 ref、逆操作、幂等键，方式字段是 compensating。
同一 ref、同一幂等键只回滚一次。同一 ref 旧值不一致则一份都不做，写 manual_intervention。
有编辑器原生 Transaction 时才标 native；本模块不发现、不调用那种 API。
"""
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


def manual_path(run_id: str) -> Path:
    return props_path(run_id).parent / "manual_intervention.json"


def level_diff_path(run_id: str) -> Path:
    return props_path(run_id).parent / "level_diff.json"


def _read_doc(run_id: str) -> dict:
    path = props_path(run_id)
    if not path.is_file():
        return {"items": [], "consumed": False, "method": "compensating", "outcome": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items": [], "consumed": False, "method": "compensating", "outcome": ""}
    if isinstance(data, list):
        data = {"items": data}
    if not isinstance(data, dict):
        return {"items": [], "consumed": False, "method": "compensating", "outcome": ""}
    rows = data.get("items") if isinstance(data.get("items"), list) else []
    data["items"] = [row for row in rows if isinstance(row, dict)]
    data["method"] = "compensating"
    data["consumed"] = bool(data.get("consumed"))
    data["outcome"] = str(data.get("outcome") or "")
    return data


def _write_doc(run_id: str, doc: dict) -> None:
    path = props_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(doc)
    payload["method"] = "compensating"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load(run_id: str) -> list[dict]:
    return list(_read_doc(run_id).get("items") or [])


def _default_action(kind: str, fields: dict) -> str:
    if kind == "scalar":
        return "set_scalar_param"
    if kind == "created":
        return "place_batch"
    if kind == "location":
        return "move_actor"
    if "bPickable" in fields or "Tags" in fields:
        return "set_pickable"
    return "set_properties"


def _default_inverse(kind: str) -> str:
    if kind == "scalar":
        return "set_scalar_parameter"
    if kind == "created":
        return "remove_created"
    if kind == "location":
        return "set_actor_transform"
    return "set_properties"


def _default_key(ref: str, kind: str, fields: dict) -> str:
    if kind == "scalar":
        return f"scalar:{ref}:{fields.get('parameter')}"
    if kind == "created":
        return f"created:{ref}"
    if kind == "location":
        return f"location:{ref}"
    return f"props:{ref}"


def _normalize(row: dict) -> dict:
    fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
    kind = str(row.get("kind") or "props")
    ref = str(row.get("ref") or "")
    item = dict(row)
    item["ref"] = ref
    item["kind"] = kind
    item["fields"] = fields
    item["action"] = str(item.get("action") or _default_action(kind, fields))
    item["inverse"] = str(item.get("inverse") or _default_inverse(kind))
    item["idempotency_key"] = str(item.get("idempotency_key") or _default_key(ref, kind, fields))
    item["method"] = "compensating"
    item["consumed"] = bool(item.get("consumed"))
    return item


def capture_props(run_id: str, ref: str, fields: dict, *, kind: str = "props",
                  action: str = "", inverse: str = "", idempotency_key: str = "") -> None:
    if not str(run_id or "").strip() or not str(ref or "").strip() or not isinstance(fields, dict):
        return
    doc = _read_doc(run_id)
    row = _normalize({
        "ref": str(ref),
        "kind": kind,
        "fields": fields,
        "action": action,
        "inverse": inverse,
        "idempotency_key": idempotency_key,
        "method": "compensating",
        "consumed": False,
    })
    doc["items"].append(row)
    doc["consumed"] = False
    doc["outcome"] = ""
    _write_doc(run_id, doc)


def _field_sig(fields: dict) -> str:
    return json.dumps(fields, ensure_ascii=False, sort_keys=True)


def _plan_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """逆序执行计划，以及旧值冲突的条目。同一幂等键只留第一次，已消费的键不再做。"""
    all_items = [_normalize(row) for row in rows if str(row.get("ref") or "")]
    pending = [item for item in all_items if not item["consumed"]]
    by_ref: dict[str, list[dict]] = {}
    for item in pending:
        if item["kind"] == "created":
            continue
        by_ref.setdefault(item["ref"], []).append(item)
    conflict_refs = set()
    conflicts: list[dict] = []
    for ref, items in by_ref.items():
        if len({_field_sig(item["fields"]) for item in items}) > 1:
            conflict_refs.add(ref)
            conflicts.extend(items)
    selected: list[dict] = []
    seen = {item["idempotency_key"] for item in all_items if item["consumed"]}
    for item in pending:
        if item["kind"] != "created" and item["ref"] in conflict_refs:
            continue
        key = item["idempotency_key"]
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
    selected.reverse()
    return selected, conflicts


def _write_manual(run_id: str, reasons: list[dict]) -> None:
    path = manual_path(run_id)
    existing: list[dict] = []
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict) and isinstance(data.get("reasons"), list):
            existing = [row for row in data["reasons"] if isinstance(row, dict)]
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "manual_intervention",
        "method": "compensating",
        "open": True,
        "reasons": existing + reasons,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def manual_open(run_id: str) -> bool:
    path = manual_path(run_id)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return bool(isinstance(data, dict) and data.get("open"))


def _write_level_diff(run_id: str, refs: list[str], error: str) -> None:
    path = level_diff_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "method": "compensating",
        "reason": "保存失败",
        "refs": list(refs),
        "error": error,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _mark_consumed(run_id: str, keys: set[str]) -> None:
    doc = _read_doc(run_id)
    for row in doc["items"]:
        item = _normalize(row)
        if item["idempotency_key"] in keys:
            row["consumed"] = True
            row["method"] = "compensating"
    rows = [_normalize(row) for row in doc["items"]]
    doc["consumed"] = bool(rows) and all(row.get("consumed") for row in rows)
    doc["method"] = "compensating"
    _write_doc(run_id, doc)


def note_compile_failure(run_id: str) -> None:
    """编译失败：快照留着，不标成已消费，也不做逆操作。"""
    doc = _read_doc(run_id)
    if not doc["items"] and not props_path(run_id).is_file():
        return
    for row in doc["items"]:
        row["consumed"] = False
        row["method"] = "compensating"
    doc["consumed"] = False
    doc["outcome"] = "compile_failed"
    doc["method"] = "compensating"
    _write_doc(run_id, doc)


def _write_back(ref: str, item: dict) -> dict:
    from core.editor_exec import exec_tool

    task = {"allow_writes": ["set_properties", "set_actor_transform", "set_scalar_parameter", "remove_from_scene"],
            "save": False, "pie": False}
    fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
    kind = str(item.get("kind") or "")
    if kind == "scalar":
        name = fields.get("parameter")
        value = fields.get("value")
        if isinstance(name, str) and name.strip() and isinstance(value, (int, float)):
            result = exec_tool(
                "editor_toolset.toolsets.material_instance.MaterialInstanceTools",
                "set_scalar_parameter",
                {"instance": {"refPath": ref}, "name": name, "value": float(value)},
                task=task,
            )
            return result if isinstance(result, dict) else {"ok": False, "error": "写回没有返回"}
        return {"ok": True, "skipped": True}
    if kind == "created":
        return _remove_created(ref, task)
    if kind == "location" or isinstance(fields.get("location"), dict):
        loc = fields.get("location") if isinstance(fields.get("location"), dict) else fields
        result = exec_tool(
            _ACTOR,
            "set_actor_transform",
            {"actor": {"refPath": ref}, "xform": {"location": loc}, "worldspace": True},
            task=task,
        )
        return result if isinstance(result, dict) else {"ok": False, "error": "写回没有返回"}
    values = {}
    if "bPickable" in fields:
        values["bPickable"] = fields.get("bPickable")
    if "Tags" in fields:
        values["Tags"] = fields.get("Tags")
    if not values:
        return {"ok": True, "skipped": True}
    result = exec_tool(
        _OBJECT,
        "set_properties",
        {"instance": {"refPath": ref}, "values": json.dumps(values, ensure_ascii=False)},
        task=task,
    )
    return result if isinstance(result, dict) else {"ok": False, "error": "写回没有返回"}


def _remove_created(name: str, task: dict) -> dict:
    """按名字找这次放置出来的实例，恰好一个才删。找不到或不止一个都不动。"""
    from agent.sequence import _actor_refs
    from core.editor_exec import exec_tool

    found = exec_tool(
        "editor_toolset.toolsets.scene.SceneTools",
        "find_actors",
        {"name": name, "tag": "", "collision_channels": []},
        task={"allow_writes": [], "save": False, "pie": False},
    )
    if not isinstance(found, dict) or not found.get("ok"):
        return found if isinstance(found, dict) else {"ok": False, "error": "find_actors 失败"}
    refs = _actor_refs(found.get("data"))
    if len(refs) != 1:
        return {"ok": True, "skipped": True}
    removed = exec_tool(
        "editor_toolset.toolsets.scene.SceneTools",
        "remove_from_scene",
        {"actor": {"refPath": refs[0]}},
        task=task,
    )
    return removed if isinstance(removed, dict) else {"ok": False, "error": "删除没有返回"}


def _apply_one(ref: str, item: dict, writer) -> dict:
    if writer is not None:
        result = writer(ref, item)
        if result is None:
            return {"ok": True}
        return result if isinstance(result, dict) else {"ok": True}
    return _write_back(ref, item)


def restore_props(run_id: str, *, writer=None) -> list[str]:
    """按快照逆序做逆操作。同一幂等键只做一次。

    旧值冲突的 ref 一份都不做，并写 manual_intervention。
    逆操作失败（含连接失败）时写 manual_intervention，不再做下一条。
    保存失败时快照不标成已消费，关卡差异留在快照目录。"""
    rows = _load(run_id)
    selected, conflicts = _plan_rows(rows)
    if conflicts:
        _write_manual(run_id, [
            {
                "ref": item["ref"],
                "reason": "同一 ref 的旧值不一致，两份都不回滚",
                "idempotency_key": item["idempotency_key"],
            }
            for item in conflicts
        ])
    restored: list[str] = []
    done_keys: set[str] = set()
    stopped = False
    for item in selected:
        ref = item["ref"]
        try:
            result = _apply_one(ref, item, writer)
        except Exception as exc:
            _write_manual(run_id, [{
                "ref": ref,
                "reason": f"逆操作失败：{exc}",
                "idempotency_key": item["idempotency_key"],
            }])
            stopped = True
            break
        if isinstance(result, dict) and result.get("ok") is False:
            _write_manual(run_id, [{
                "ref": ref,
                "reason": str(result.get("error") or "逆操作失败"),
                "idempotency_key": item["idempotency_key"],
            }])
            stopped = True
            break
        restored.append(ref)
        done_keys.add(item["idempotency_key"])
    if stopped or conflicts:
        if done_keys:
            _mark_consumed(run_id, done_keys)
        return restored
    if restored and writer is None:
        from agent.sequence import _save_level

        saved = _save_level({})
        if not saved.get("ok"):
            _write_level_diff(run_id, restored, str(saved.get("error") or "保存失败"))
            return restored
    if done_keys:
        _mark_consumed(run_id, done_keys)
    return restored


def compensate_write_failure(run_id: str, write_result: dict) -> dict:
    """写属性调用返回失败时，按快照把旧值写回去。逆操作再失败则停。"""
    if not isinstance(write_result, dict) or write_result.get("ok") is not False:
        return {"ok": True, "restored": []}
    restored = restore_props(run_id)
    return {"ok": not manual_open(run_id), "restored": restored}


def resume_orphaned(run_id: str) -> dict:
    """无主 run 续接前先按快照逆序恢复。恢复失败则停，不开始新的写。"""
    if manual_open(run_id):
        return {"ok": False, "stopped": True, "restored": [], "manual": True}
    restored = restore_props(run_id)
    if manual_open(run_id):
        return {"ok": False, "stopped": True, "restored": restored, "manual": True}
    return {"ok": True, "stopped": False, "restored": restored}
