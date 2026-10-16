"""按顺序跑拆开的步骤。失败或做不了时停下来，并把每一步写进 data/runs。"""
import json
import re
from datetime import datetime
from pathlib import Path

from config import PROJECT_ROOT

RUNS_DIR = PROJECT_ROOT / "data" / "runs"


def _title(step: dict, index: int) -> str:
    if step.get("title"):
        return str(step["title"])
    kinds = [item.get("kind") for item in (step.get("verify") or []) if isinstance(item, dict)]
    if kinds:
        return str(kinds[0])
    return f"步骤 {index}"


def apply_blueprint_path(step: dict, raw: str) -> dict | None:
    """r 后面的路径。只写目录就沿用步骤里的资产名；写到 BP_ 名字则拆开。"""
    text = (raw or "").strip().replace("\\", "/").rstrip("/")
    if ".." in text.split("/"):
        return None
    if text == "/Game":
        folder = "/Game"
        asset_name = str(step.get("asset_name") or "")
    elif text.startswith("/Game/"):
        parts = text.split("/")
        leaf = parts[-1]
        if leaf.startswith("BP_") and len(parts) >= 3:
            folder = "/".join(parts[:-1]) or "/Game"
            asset_name = leaf
        else:
            folder = text
            asset_name = str(step.get("asset_name") or "")
    else:
        return None
    if not asset_name or not folder.startswith("/Game"):
        return None
    filled = dict(step)
    parent = str(step.get("parent") or "")
    if not parent and asset_name.startswith("BP_") and len(asset_name) > 3:
        parent = _script_parent(asset_name, "")
    filled.update({
        "ok": True,
        "blocked": False,
        "folder_path": folder,
        "asset_name": asset_name,
        "actor_name": asset_name,
        "parent": parent,
        "ref_path": f"{folder}/{asset_name}.{asset_name}",
    })
    filled.pop("needs", None)
    return filled


def _share_blueprint(steps: list[dict], start: int, filled: dict) -> None:
    keys = ("folder_path", "asset_name", "parent", "ref_path", "actor_name")
    for later in steps[start:]:
        if later.get("needs") != "blueprint":
            continue
        for key in keys:
            if filled.get(key):
                later[key] = filled[key]
        later["ok"] = True
        later["blocked"] = False
        later.pop("needs", None)


def mark_missing_path(step: dict) -> dict:
    """不知道目录或源文件时，标成可以 r 补一次路径。拾取这种缺 PIE 的不标。"""
    if step.get("needs") or step.get("path_filled"):
        return step
    if step.get("ok") is not False and (step.get("verify") or step.get("action")):
        return step
    err = str(step.get("error") or "")
    marked = dict(step)
    if "C++" in err and "路径" in err:
        marked["needs"] = "source"
        marked["blocked"] = True
        marked["error"] = err + " 只给一次：r Public/类名.h 或 r Private/类名.cpp。"
        return marked
    if any(token in err for token in ("路径", "目录", "refPath")):
        marked["needs"] = "path"
        marked["blocked"] = True
        if "只给一次" not in err:
            marked["error"] = err + " 只给一次：r /Game/目录 或 r /Game/目录/BP_名字。"
        return marked
    return step


def apply_supplied_path(query: str, step: dict, raw: str) -> dict | None:
    if step.get("needs") == "source":
        from agent.supervise import cpp_task_for_file
        return cpp_task_for_file(query, raw)
    filled = apply_blueprint_path(step, raw)
    if filled is not None:
        filled["path_filled"] = True
    return filled


def _taken_path(step: dict, decision: str) -> str:
    if step.get("path_filled") or decision in ("stop", "skip", "retry", ""):
        return ""
    if step.get("needs") not in ("path", "blueprint", "source"):
        return ""
    return decision


def _blueprint_indexed(asset_name: str, ref_path: str) -> bool:
    from config import ASSET_JSONL
    if not ASSET_JSONL.is_file():
        return False
    for line in ASSET_JSONL.read_text(encoding="utf-8", errors="replace").splitlines():
        if ref_path in line or f'"asset_name": "{asset_name}"' in line:
            return True
    return False


def _runnable(step: dict) -> bool:
    if step.get("ok") is False:
        return False
    return bool(step.get("verify") or step.get("action"))


def _run_action(step: dict) -> dict:
    from core.editor_exec import exec_tool
    from core.judge import judge

    task = {"allow_writes": ["create", "add_to_scene_from_asset"], "save": False, "pie": False}
    action = step.get("action")
    if action == "create_blueprint":
        asset_name = str(step.get("asset_name") or "")
        parent = _script_parent(asset_name, str(step.get("parent") or ""))
        if not step.get("folder_path") or not asset_name:
            return {"ok": False, "id": asset_name, "error": "缺少目录。请用 r /Game/目录/BP_类名"}
        ref = step.get("ref_path") or f"{step['folder_path']}/{asset_name}.{asset_name}"
        note = ref
        if _blueprint_indexed(asset_name, ref):
            note = f"{ref}（索引里已有同名蓝图，不重复新建）"
        else:
            created = exec_tool(
                "editor_toolset.toolsets.blueprint.BlueprintTools",
                "create",
                {
                    "folder_path": step["folder_path"],
                    "asset_name": asset_name,
                    "asset_type": {"refPath": "/Script/Engine.Actor"},
                },
                task=task,
            )
            if not created.get("ok"):
                err = str(created.get("error") or "")
                if not any(token in err.lower() for token in ("exist", "already", "已存在")):
                    return {"ok": False, "id": asset_name, "error": err or "创建蓝图失败"}
                note = f"{ref}（已存在，不重复新建）"
        if parent:
            task["allow_writes"] = list(task["allow_writes"]) + ["set_parent"]
            parented = exec_tool(
                "editor_toolset.toolsets.blueprint.BlueprintTools",
                "set_parent",
                {
                    "blueprint": {"refPath": ref},
                    "parent_class": {"refPath": parent},
                },
                task=task,
            )
            if not parented.get("ok"):
                return {
                    "ok": False,
                    "id": asset_name,
                    "error": f"蓝图已写到 {ref}。父类 {parent} 没设上：{parented.get('error')}",
                }
        compiled = judge({"kind": "blueprint_compile", "refPath": ref}, task)
        if compiled.get("ok"):
            return {"ok": True, "id": asset_name, "index_note": note}
        err = str(compiled.get("summary") or "创建蓝图失败")
        return {"ok": False, "id": asset_name, "error": err}
    if action == "place_blueprint":
        # 场景里已有同名实例时，放在它旁边 50 uu，不叠一起也不贴地（拾取流程同款约定）
        location = {"x": 0, "y": 0, "z": 100}
        snap_to_ground = True
        place_note = "已放入当前关卡，未保存"
        name = str(step["actor_name"])
        found = exec_tool(
            _SCENE,
            "find_actors",
            {"name": name, "tag": "", "collision_channels": []},
            task=_read_task(),
        )
        if not found.get("ok"):
            err = str(found.get("error") or "查场景失败")
            if _conn_error(err):
                return {"ok": False, "id": name, "error": f"编辑器 MCP 连不上：{err}"}
            return {"ok": False, "id": name, "error": f"放置前查场景失败：{err}"}
        refs = _actor_refs(found.get("data"))
        if refs:
            anchor = _read_location(refs[0], _read_task())
            if anchor.get("ok"):
                x, y, z = anchor["location"]
                location = {"x": x + 50.0, "y": y, "z": z}
                snap_to_ground = False
                place_note = f"已放入当前关卡，与已有同名实例({len(refs)} 个)错开 50 uu，未保存"
        placed = exec_tool(
            "editor_toolset.toolsets.scene.SceneTools",
            "add_to_scene_from_asset",
            {
                "asset_path": f"{step['folder_path']}/{step['asset_name']}",
                "name": step["actor_name"],
                "xform": {"location": location},
                "snap_to_ground": snap_to_ground,
            },
            task=task,
        )
        if placed.get("ok"):
            return {"ok": True, "id": step["actor_name"], "index_note": place_note}
        return {"ok": False, "id": step["actor_name"], "error": str(placed.get("error") or "放进场景失败")}
    if action == "bind_slot":
        ref = str(step.get("ref_path") or step.get("asset_path") or "")
        if not ref:
            return {"ok": False, "error": "没有蓝图路径"}
        if step.get("from_path"):
            ensured = _ensure_blueprint(step)
            if not ensured.get("ok"):
                return ensured
            return {"ok": True, "id": step.get("actor_name"), "index_note": ensured.get("note") or ref, "ref": ref}
        return {"ok": True, "id": step.get("class_name") or step.get("actor_name"), "index_note": ref, "ref": ref}
    if action == "place_pair":
        return _place_pair(step)
    if action in ("judge_pickup", "check_pickup"):
        return _check_pickup(step, task)
    if action == "set_pickable":
        return _set_pickable(step)
    if action == "move_actor":
        return _move_actor(step)
    if action == "probe_move":
        return _probe_move(step)
    if action == "probe_look":
        return _probe_look(step)
    if action == "probe_play":
        return _probe_play(step)
    if action == "place_actor":
        step = dict(step)
        step["action"] = "place_blueprint"
        return _run_action(step)
    if action == "bind_blueprint":
        step = dict(step)
        step["action"] = "bind_slot"
        return _run_action(step)
    return {"ok": False, "error": f"未知动作 {action}"}


def _actor_refs(data) -> list[str]:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict) and "returnValue" in data:
        return _actor_refs(data["returnValue"])
    if isinstance(data, list):
        refs = []
        for item in data:
            if isinstance(item, dict) and item.get("refPath"):
                refs.append(str(item["refPath"]))
            elif isinstance(item, str):
                refs.append(item)
        return refs
    return []


_SCENE = "editor_toolset.toolsets.scene.SceneTools"
_ACTOR = "editor_toolset.toolsets.actor.ActorTools"
_OBJECT = "editor_toolset.toolsets.object.ObjectTools"
_PICKUP_RANGE = 100.0
_PICKUP_TAG = "InteractableActor"
_PICKUP_KEYS = ("bAllowPickup", "bCanBePicked", "Tags")


def _read_task() -> dict:
    return {"allow_writes": [], "save": False, "pie": False}


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    return None


def _has_tag(value, tag: str):
    if value is None:
        return None
    if isinstance(value, str):
        return tag in value
    if isinstance(value, list):
        found = False
        for item in value:
            if item == tag:
                found = True
            elif isinstance(item, str) and tag in item:
                found = True
            elif isinstance(item, dict) and tag in " ".join(str(part) for part in item.values()):
                found = True
        return found
    return None


def judge_pickup(values: dict, distance: float) -> dict:
    """只读裁决。缺字段是失败，不是「不能拾取」。四条都满足才是「能」。"""
    missing = [key for key in _PICKUP_KEYS if key not in values]
    if missing:
        return {"ok": False, "error": "读属性失败，缺 " + "、".join(missing)}
    allow = _as_bool(values.get("bAllowPickup"))
    can = _as_bool(values.get("bCanBePicked"))
    tagged = _has_tag(values.get("Tags"), _PICKUP_TAG)
    if allow is None or can is None or tagged is None:
        return {"ok": False, "error": "属性值无法解析，不裁决能否拾取"}
    reasons = []
    if not allow:
        reasons.append(f"bAllowPickup={allow}")
    if not can:
        reasons.append(f"bCanBePicked={can}")
    if not tagged:
        reasons.append("没有 InteractableActor 标签")
    if distance > _PICKUP_RANGE:
        reasons.append(f"距离 {distance:.1f} uu，超过 100")
    if reasons:
        note = "不能拾取：" + "；".join(reasons)
        return {"ok": True, "verdict": "不能", "reasons": reasons, "index_note": note}
    note = (
        f"能拾取。距离 {distance:.1f} uu，bAllowPickup 与 bCanBePicked 为真，带 InteractableActor。"
    )
    return {"ok": True, "verdict": "能", "reasons": [], "index_note": note}


def _parse_props(data) -> dict | None:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    if isinstance(data, dict) and "returnValue" in data:
        inner = data["returnValue"]
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except json.JSONDecodeError:
                return None
        data = inner if isinstance(inner, dict) else data
    if isinstance(data, dict) and isinstance(data.get("properties"), dict):
        return data["properties"]
    if isinstance(data, dict):
        return data
    return None


def _xyz(data) -> tuple[float, float, float] | None:
    from core.judge import _location

    loc = _location(data)
    if not loc:
        return None
    try:
        return float(loc["x"]), float(loc["y"]), float(loc["z"])
    except (KeyError, TypeError, ValueError):
        return None


def _dist(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _conn_error(text: str) -> bool:
    low = text.lower()
    return "10061" in text or "connect" in low or "refused" in low


def _find_named(name: str, task: dict) -> dict:
    from core.editor_exec import exec_tool

    found = exec_tool(
        _SCENE,
        "find_actors",
        {"name": name, "tag": "", "collision_channels": []},
        task=task,
    )
    if not found.get("ok"):
        err = str(found.get("error") or f"找不到 {name}")
        if _conn_error(err):
            return {"ok": False, "error": f"编辑器 MCP 连不上：{err}"}
        return {"ok": False, "error": err}
    refs = _actor_refs(found.get("data"))
    if not refs:
        return {"ok": True, "missing": True, "refs": []}
    if len(refs) != 1:
        return {"ok": False, "error": f"{name} 在场景里不是唯一的，不裁决能否拾取。候选 {len(refs)} 个"}
    return {"ok": True, "missing": False, "ref": refs[0]}


def _read_location(ref: str, task: dict) -> dict:
    from core.editor_exec import exec_tool

    got = exec_tool(_ACTOR, "get_actor_transform", {"actor": {"refPath": ref}}, task=task)
    if not got.get("ok"):
        err = str(got.get("error") or "读位置失败")
        if _conn_error(err):
            return {"ok": False, "error": f"编辑器 MCP 连不上：{err}"}
        return {"ok": False, "error": f"读位置失败：{err}"}
    loc = _xyz(got.get("data"))
    if loc is None:
        return {"ok": False, "error": "读位置失败，返回里没有 location"}
    return {"ok": True, "location": loc}


def _script_parent(asset_name: str, parent: str) -> str:
    """蓝图父类用反射名。ATestActor 是 /Script/AIAgentProject.TestActor。"""
    text = (parent or "").strip()
    if text.startswith("/Script/") and "." in text:
        module, name = text.rsplit(".", 1)
        if len(name) > 1 and name[0] in "AUF" and name[1].isupper():
            name = name[1:]
        return f"{module}.{name}"
    bare = asset_name[3:] if asset_name.startswith("BP_") and len(asset_name) > 3 else asset_name
    if len(bare) > 1 and bare[0] in "AUF" and bare[1].isupper():
        bare = bare[1:]
    if not bare:
        return ""
    return f"/Script/AIAgentProject.{bare}"


def _blueprint_present(folder: str, asset: str, asset_path: str) -> dict:
    from core.editor_exec import exec_tool

    found = exec_tool(
        "editor_toolset.toolsets.asset.AssetTools",
        "find_assets",
        {
            "folder_path": folder or "/Game",
            "name": asset,
            "asset_type": {"refPath": "/Script/Engine.Blueprint"},
            "recursive": False,
        },
        task=_read_task(),
    )
    if not found.get("ok"):
        err = str(found.get("error") or "查蓝图失败")
        if _conn_error(err):
            return {"ok": False, "error": f"编辑器 MCP 连不上：{err}"}
        return {"ok": False, "error": err}
    refs = _actor_refs(found.get("data"))
    wanted = asset_path.rstrip("/")
    for ref in refs:
        short = ref.split(".", 1)[0]
        if short == wanted or ref == wanted or short.endswith("/" + asset):
            return {"ok": True, "present": True}
    return {"ok": True, "present": False}


def _ensure_blueprint(slot: dict) -> dict:
    """路径上没有蓝图时按类新建，再编译。不改图。已有则不动。"""
    from core.editor_exec import exec_tool
    from core.judge import judge

    asset = str(slot.get("asset_name") or slot.get("actor_name") or "")
    folder = str(slot.get("folder_path") or "")
    asset_path = str(slot.get("asset_path") or "")
    if not asset_path and folder and asset:
        asset_path = f"{folder}/{asset}"
    if not folder and "/" in asset_path:
        folder = asset_path.rsplit("/", 1)[0]
    ref = str(slot.get("ref_path") or "")
    if not ref and asset_path and asset:
        ref = f"{asset_path}.{asset}"
    if not folder or not asset or not ref:
        return {"ok": False, "error": "创建蓝图缺少目录或名字"}
    present = _blueprint_present(folder, asset, asset_path)
    if not present.get("ok"):
        return present
    if present.get("present"):
        return {"ok": True, "note": f"{ref} 已存在"}
    parent = _script_parent(asset, str(slot.get("parent") or ""))
    task = {"allow_writes": ["create", "set_parent"], "save": False, "pie": False}
    created = exec_tool(
        "editor_toolset.toolsets.blueprint.BlueprintTools",
        "create",
        {
            "folder_path": folder,
            "asset_name": asset,
            "asset_type": {"refPath": "/Script/Engine.Actor"},
        },
        task=task,
    )
    if not created.get("ok"):
        err = str(created.get("error") or "")
        if not any(token in err.lower() for token in ("exist", "already", "已存在")):
            return {"ok": False, "error": err or "创建蓝图失败"}
    if parent:
        parented = exec_tool(
            "editor_toolset.toolsets.blueprint.BlueprintTools",
            "set_parent",
            {
                "blueprint": {"refPath": ref},
                "parent_class": {"refPath": parent},
            },
            task=task,
        )
        if not parented.get("ok"):
            return {"ok": False, "error": f"蓝图已写到 {ref}。父类 {parent} 没设上：{parented.get('error')}"}
    compiled = judge({"kind": "blueprint_compile", "refPath": ref}, task)
    if not compiled.get("ok"):
        return {"ok": False, "error": str(compiled.get("summary") or "蓝图编译失败")}
    return {"ok": True, "note": f"已新建 {ref}，父类 {parent}"}


def _place_one(slot: dict, location: tuple[float, float, float], snap: bool) -> dict:
    from core.editor_exec import exec_tool

    name = str(slot.get("actor_name") or "")
    asset_path = str(slot.get("asset_path") or "")
    if not name or not asset_path:
        return {"ok": False, "error": "放置缺少资产路径或名字"}
    ensured = _ensure_blueprint(slot)
    if not ensured.get("ok"):
        return ensured
    task = {"allow_writes": ["add_to_scene_from_asset"], "save": False, "pie": False}
    placed = exec_tool(
        _SCENE,
        "add_to_scene_from_asset",
        {
            "asset_path": asset_path,
            "name": name,
            "xform": {"location": {"x": location[0], "y": location[1], "z": location[2]}},
            "snap_to_ground": snap,
        },
        task=task,
    )
    if placed.get("ok"):
        return {"ok": True, "note": f"已放入 {name}，未保存"}
    err = str(placed.get("error") or "放进场景失败")
    if _conn_error(err):
        return {"ok": False, "error": f"编辑器 MCP 连不上：{err}"}
    return {"ok": False, "error": err}


def _place_pair(step: dict) -> dict:
    """缺的实例才放。两者都已在关卡里则不动。不保存，不 PIE。"""
    task = _read_task()
    picker = step.get("picker") or {}
    target = step.get("target") or {}
    notes = []
    found = {}
    for key, slot in (("picker", picker), ("target", target)):
        name = str(slot.get("actor_name") or "")
        if not name:
            return {"ok": False, "error": "放置缺少 Actor 名"}
        one = _find_named(name, task)
        if not one.get("ok"):
            return one
        found[key] = one
    picker_missing = bool(found["picker"].get("missing"))
    target_missing = bool(found["target"].get("missing"))
    if picker_missing and target_missing:
        first = _place_one(picker, (0.0, 0.0, 100.0), True)
        if not first.get("ok"):
            return first
        second = _place_one(target, (50.0, 0.0, 100.0), True)
        if not second.get("ok"):
            return second
        notes.extend([first["note"], second["note"]])
    elif target_missing or picker_missing:
        anchor_key = "picker" if not picker_missing else "target"
        anchor = _read_location(found[anchor_key]["ref"], task)
        if not anchor.get("ok"):
            return anchor
        x, y, z = anchor["location"]
        if target_missing:
            placed = _place_one(target, (x + 50.0, y, z), False)
        else:
            placed = _place_one(picker, (x + 50.0, y, z), False)
        if not placed.get("ok"):
            return placed
        notes.append(placed["note"])
    else:
        notes.append("两个实例都已在关卡里，不移动")
    return {"ok": True, "index_note": "；".join(notes) + "。未保存"}


_SPEED_BASE = 100.0
_CN_TIMES = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _times_factor(query: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*倍", query)
    if match:
        return float(match.group(1))
    match = re.search(r"([一二两三四五六七八九十])\s*倍", query)
    if match:
        return float(_CN_TIMES[match.group(1)])
    return None


def _pawn_stem(query: str) -> str:
    """PIE 里要找的对象。只认 C++ 索引里真有的类；父类是 Pawn/Character 的优先。
    没有 Pawn 类时，句子里唯一的 BP_ 名（会被放进场景的那个）优先于普通类名。"""
    from agent.supervise import _BP_RE, _cpp_tokens, _resolve_class

    pawn = ""
    fallback = ""
    for token in _cpp_tokens(query):
        for rec in _resolve_class(token):
            meta = rec.get("metadata") or {}
            stem = Path(str(meta.get("file_name") or "")).stem
            if not stem:
                continue
            if not fallback:
                fallback = stem
            if str(meta.get("parent_class") or "") in ("APawn", "ACharacter") and not pawn:
                pawn = stem
    if pawn:
        return pawn
    bp_names = [name for name in _BP_RE.findall(query)]
    if len(bp_names) == 1:
        return bp_names[0]
    return fallback or "TestPawn"


def _source_move_speed(stem: str) -> float | None:
    from config import UE_SOURCE_ROOT

    path = UE_SOURCE_ROOT / "Private" / f"{stem}.cpp"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    found = re.findall(r"MoveSpeed\s*=\s*(-?\d+(?:\.\d+)?)", text)
    if not found:
        return None
    return float(found[-1])


def _live_move_speed(ref: str) -> float | None:
    from core.editor_exec import exec_tool

    got = exec_tool(
        _OBJECT,
        "get_properties",
        {"instance": {"refPath": ref}, "properties": ["MoveSpeed"]},
        task={"allow_writes": [], "save": False, "pie": False},
    )
    if not got.get("ok"):
        return None
    data = got.get("data")
    if isinstance(data, dict) and "returnValue" in data:
        data = data["returnValue"]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    if isinstance(data, dict) and "MoveSpeed" in data:
        return float(data["MoveSpeed"])
    return None


def _probe_play(step: dict) -> dict:
    """进入 PIE。移速句子再核对 PIE 里的 MoveSpeed 是否等于源码，以及是否达到所说的倍数。不保存。"""
    from core.editor_exec import exec_tool

    query = str(step.get("query") or "")
    stem = _pawn_stem(query)
    pie = {"allow_writes": [], "save": False, "pie": True}
    app = "EditorToolset.EditorAppToolset"
    started = exec_tool(
        app,
        "StartPIE",
        {"options": {"bSimulate": False, "playMode": "PlayMode_InViewPort", "warmupSeconds": 1}},
        task=pie,
    )
    if not started.get("ok"):
        return {"ok": False, "error": str(started.get("error") or "PIE 没有启动")}
    try:
        found = _find_named(stem, {"allow_writes": [], "save": False, "pie": False})
        if not found.get("ok") or found.get("missing"):
            return {"ok": False, "error": str(found.get("error") or f"PIE 里没有 {stem}")}
        note = f"PIE 里有 {stem}"
        if re.search(r"移速|速度", query):
            source = _source_move_speed(stem)
            live = _live_move_speed(found["ref"])
            if source is None:
                return {"ok": False, "error": f"源码里读不到 {stem} 的 MoveSpeed"}
            if live is None:
                return {"ok": False, "error": "PIE 里读不到 MoveSpeed"}
            times = _times_factor(query)
            if times is not None:
                expect = _SPEED_BASE * times
                if abs(source - expect) > 0.1:
                    return {"ok": False, "error": f"源码移速是 {source:g}，{times:g} 倍应是 {expect:g}"}
            if abs(live - source) > 0.1:
                return {"ok": False, "error": f"PIE 里移速是 {live:g}，源码是 {source:g}"}
            note = f"PIE 里 {stem} 移速 {live:g}"
        return {"ok": True, "index_note": note}
    finally:
        exec_tool(app, "StopPIE", {}, task=pie)


def _probe_look(step: dict) -> dict:
    """镜头自动化测试。句子要求 PIE 时再进一次，确认场景里有 Pawn，然后停掉。不保存。"""
    from core.editor_exec import exec_tool
    from core.judge import judge

    filt = str(step.get("automation_filter") or "AIAgent.Pawn.Look")
    result = judge({"kind": "automation", "filter": filt}, {"allow_writes": [], "save": False, "pie": False})
    if not result.get("ok"):
        return {"ok": False, "error": str(result.get("summary") or "镜头测试没过")}
    note = str(result.get("summary") or "镜头测试通过")
    if not step.get("pie"):
        return {"ok": True, "index_note": note}
    pie = {"allow_writes": [], "save": False, "pie": True}
    app = "EditorToolset.EditorAppToolset"
    started = exec_tool(
        app,
        "StartPIE",
        {"options": {"bSimulate": False, "playMode": "PlayMode_InViewPort", "warmupSeconds": 1}},
        task=pie,
    )
    if not started.get("ok"):
        return {"ok": False, "error": str(started.get("error") or "PIE 没有启动")}
    try:
        found = _find_named("TestPawn", {"allow_writes": [], "save": False, "pie": False})
        if not found.get("ok") or found.get("missing"):
            return {"ok": False, "error": str(found.get("error") or "PIE 里没有 TestPawn")}
        return {"ok": True, "index_note": note + "，PIE 里有 TestPawn"}
    finally:
        exec_tool(app, "StopPIE", {}, task=pie)


def _probe_move(step: dict) -> dict:
    """编译之后跑移动自动化测试。不保存关卡。"""
    from core.judge import judge

    filt = str(step.get("automation_filter") or "AIAgent.Pawn.Wasd")
    spec = {}
    for item in step.get("verify") or []:
        if isinstance(item, dict) and item.get("kind") == "automation":
            spec = item
            break
    if not spec:
        spec = {"kind": "automation", "filter": filt}
    result = judge(spec, {"allow_writes": [], "save": False, "pie": False})
    if not result.get("ok"):
        return {"ok": False, "error": str(result.get("summary") or "移动测试没过")}
    return {"ok": True, "index_note": str(result.get("summary") or "移动测试通过")}


def _move_actor(step: dict) -> dict:
    """按已经解析好的名字和坐标移动。不进模型，不保存。写完再读位置。"""
    from core.editor_exec import exec_tool

    slots = step.get("slots") if isinstance(step.get("slots"), dict) else {}
    name = str(step.get("actor_name") or slots.get("actor") or "")
    loc = step.get("location")
    if not isinstance(loc, dict):
        return {"ok": False, "error": "坐标不是唯一的一组，不猜。"}
    if not name:
        return {"ok": False, "error": "没有要移动的 Actor"}
    try:
        target = {"x": float(loc["x"]), "y": float(loc["y"]), "z": float(loc["z"])}
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "坐标不是唯一的一组，不猜。"}
    read = _read_task()
    found = _find_named(name, read)
    if not found.get("ok"):
        return found
    if found.get("missing"):
        return {"ok": False, "id": name, "in_level": False, "error": f"场景里没有 {name}"}
    ref = found["ref"]
    write = {"allow_writes": ["set_actor_transform"], "save": False, "pie": False}
    moved = exec_tool(
        _ACTOR,
        "set_actor_transform",
        {
            "actor": {"refPath": ref},
            "xform": {"location": target},
            "worldspace": True,
        },
        task=write,
    )
    if not moved.get("ok"):
        return {"ok": False, "id": name, "ref": ref, "in_level": True, "error": str(moved.get("error") or "移动失败")}
    got = _read_location(ref, read)
    if not got.get("ok"):
        return got
    x, y, z = got["location"]
    if max(abs(x - target["x"]), abs(y - target["y"]), abs(z - target["z"])) > 0.1:
        return {
            "ok": False,
            "id": name,
            "ref": ref,
            "in_level": True,
            "error": f"复查位置 ({x:g}, {y:g}, {z:g})，没有到目标",
        }
    return {
        "ok": True,
        "id": name,
        "ref": ref,
        "in_level": True,
        "index_note": f"已移到 ({target['x']:g}, {target['y']:g}, {target['z']:g})，未保存",
    }


def _set_pickable(step: dict) -> dict:
    """让实例变成 SetPickable(true) 的结果。场景里没有这个实例则失败。

    2026-10-14 扫描 MCP，没有调用 BlueprintCallable 的工具。
    于是只写 bPickable 和 InteractableActor，写完再读，不把「写过了」当成成功。
    """
    from core.editor_exec import exec_tool

    read = _read_task()
    slots = step.get("slots") if isinstance(step.get("slots"), dict) else {}
    name = str(step.get("actor_name") or slots.get("target") or "")
    if not name:
        return {"ok": False, "error": "没有要标记的物体"}
    found = _find_named(name, read)
    if not found.get("ok"):
        return found
    if found.get("missing"):
        return {"ok": False, "id": name, "in_level": False, "error": f"场景里没有 {name}"}
    ref = found["ref"]
    write = {"allow_writes": ["set_properties", "add_tag"], "save": False, "pie": False}
    wrote = exec_tool(
        _OBJECT,
        "set_properties",
        {"instance": {"refPath": ref}, "values": json.dumps({"bPickable": True})},
        task=write,
    )
    tagged = exec_tool(
        _ACTOR,
        "add_tag",
        {"actor": {"refPath": ref}, "tag": _PICKUP_TAG},
        task=write,
    )
    got = exec_tool(
        _OBJECT,
        "get_properties",
        {"instance": {"refPath": ref}, "properties": ["bPickable", "Tags"]},
        task=read,
    )
    if not got.get("ok"):
        err = str(got.get("error") or "读属性失败")
        if _conn_error(err):
            return {"ok": False, "id": name, "ref": ref, "in_level": True, "error": f"编辑器 MCP 连不上：{err}"}
        return {"ok": False, "id": name, "ref": ref, "in_level": True, "error": f"读属性失败：{err}"}
    values = _parse_props(got.get("data"))
    pickable = _as_bool(values.get("bPickable")) if isinstance(values, dict) else None
    has_tag = _has_tag(values.get("Tags"), _PICKUP_TAG) if isinstance(values, dict) else None
    if pickable is True and has_tag is True:
        return {
            "ok": True,
            "id": name,
            "ref": ref,
            "in_level": True,
            "index_note": "bPickable 为真，带 InteractableActor",
        }
    bits = []
    shown = values if isinstance(values, dict) else {}
    if pickable is not True:
        bits.append(f"bPickable={shown.get('bPickable', '未读到')}")
    if has_tag is not True:
        bits.append("没有 InteractableActor")
    write_err = []
    if not wrote.get("ok"):
        write_err.append(str(wrote.get("error") or "bPickable 没写上"))
    if not tagged.get("ok"):
        write_err.append(str(tagged.get("error") or "标签没写上"))
    extra = ("；写入：" + "；".join(write_err)) if write_err else ""
    return {
        "ok": False,
        "id": name,
        "ref": ref,
        "in_level": True,
        "error": "复查未通过：" + "；".join(bits) + extra,
    }


def _check_pickup(step: dict, task: dict) -> dict:
    """按 CanBePickedByPawn 与 1 米距离做只读裁决。不写属性，不补标签。"""
    from core.editor_exec import exec_tool

    task = _read_task()
    slots = step.get("slots") if isinstance(step.get("slots"), dict) else {}
    name = str(step.get("actor_name") or slots.get("target") or "")
    picker_name = str(step.get("picker_name") or slots.get("picker") or "")
    if not name or not picker_name:
        return {"ok": False, "error": "没有物体或拾取者，不裁决能否拾取"}
    if step.get("target_ref"):
        if step.get("target_in_level") is False:
            return {"ok": False, "id": name, "in_level": False, "error": f"场景里没有 {name}"}
        target = {"ok": True, "missing": False, "ref": step["target_ref"]}
    else:
        target = _find_named(name, task)
    if not target.get("ok"):
        return target
    if target.get("missing"):
        return {"ok": False, "id": name, "in_level": False, "error": f"场景里没有 {name}"}
    picker = _find_named(picker_name, task)
    if not picker.get("ok"):
        return picker
    if picker.get("missing"):
        return {"ok": False, "id": picker_name, "error": f"场景里没有 {picker_name}"}
    target_loc = _read_location(target["ref"], task)
    picker_loc = _read_location(picker["ref"], task)
    if not target_loc.get("ok"):
        return target_loc
    if not picker_loc.get("ok"):
        return picker_loc
    got = exec_tool(
        _OBJECT,
        "get_properties",
        {"instance": {"refPath": target["ref"]}, "properties": list(_PICKUP_KEYS)},
        task=task,
    )
    if not got.get("ok"):
        err = str(got.get("error") or "读属性失败")
        if _conn_error(err):
            return {"ok": False, "error": f"编辑器 MCP 连不上：{err}"}
        return {"ok": False, "id": name, "error": f"读属性失败：{err}"}
    values = _parse_props(got.get("data"))
    if not isinstance(values, dict):
        return {"ok": False, "id": name, "error": "读属性失败，返回无法解析"}
    distance = _dist(picker_loc["location"], target_loc["location"])
    verdict = judge_pickup(values, distance)
    if not verdict.get("ok"):
        return {"ok": False, "id": name, "ref": target["ref"], "in_level": True, "error": verdict.get("error") or "读属性失败"}
    return {
        "ok": True,
        "id": name,
        "ref": target["ref"],
        "in_level": True,
        "verdict": verdict.get("verdict"),
        "index_note": verdict.get("index_note") or verdict.get("verdict"),
    }


def refresh_pickup(steps: list[dict]) -> None:
    """两个槽位都绑上之后，才允许放置和只读裁决。"""
    slots = [step for step in steps if step.get("slot")]
    if not slots:
        return
    if any(step.get("ok") is False for step in slots):
        return
    picker = next((step for step in slots if step.get("slot") == "picker"), None)
    target = next((step for step in slots if step.get("slot") == "target"), None)
    if picker is None or target is None:
        return
    def _view(slot: dict) -> dict:
        folder = str(slot.get("folder_path") or "")
        asset = str(slot.get("asset_name") or "")
        asset_path = str(slot.get("asset_path") or "")
        if not asset_path and folder and asset:
            asset_path = f"{folder}/{asset}"
        ref = str(slot.get("ref_path") or "")
        if not asset_path and "." in ref:
            asset_path = ref.split(".", 1)[0]
        return {
            "asset_path": asset_path,
            "actor_name": slot.get("actor_name") or asset,
            "ref_path": ref,
            "asset_name": asset,
            "folder_path": folder,
            "parent": _script_parent(asset, str(slot.get("parent") or "")),
        }

    for step in steps:
        if step.get("action") == "place_pair":
            step["ok"] = True
            step["blocked"] = False
            step.pop("error", None)
            step.pop("needs", None)
            step["picker"] = _view(picker)
            step["target"] = _view(target)
        if step.get("action") == "check_pickup" and step.get("read_only"):
            step["ok"] = True
            step["blocked"] = False
            step.pop("error", None)
            step.pop("needs", None)
            step["picker_name"] = picker.get("actor_name") or ""
            step["actor_name"] = target.get("actor_name") or ""


def _write(path: Path, query: str, rows: list[dict]) -> None:
    lines = [f"# {path.stem}", "", f"原话：{query}", ""]
    for row in rows:
        lines.append(f"## {row['index']}. {row['title']}")
        lines.append(f"- 状态：{row['status']}")
        if row.get("detail"):
            lines.append(f"- 说明：{row['detail']}")
        if row.get("decision"):
            lines.append(f"- 裁决：{row['decision']}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _emit_choice(events, window: str, decision, path: str = "") -> None:
    if events is None:
        return
    from agent.run_windows import confirm_fields

    option, supplied = confirm_fields(decision, path)
    events.emit("confirm", window=window, option=option, path=supplied)


def run_sequence(query: str, decide, *, choices: bool = False) -> tuple[int, Path]:
    """decide(step, summary) -> stop / skip / retry。summary 为空表示这一步还跑不了。
    choices 为真时，每次 s/c/r 写一条 confirm 事件。"""
    from agent.run_task import run_task
    from agent.supervise import plan_sequence

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RUNS_DIR / stamp.split("-", 1)[0] / f"{stamp}.md"
    events = None
    if choices:
        from agent.run_windows import _EventBus

        events = _EventBus(stamp)
    steps = plan_sequence(query)
    rows = []
    print(f"拆成 {len(steps)} 步。记录写在 {path}")
    index = 0
    while index < len(steps):
        step = steps[index]
        number = index + 1
        title = _title(step, number)
        print(f"\n步骤 {number}/{len(steps)} {title}")
        if not _runnable(step):
            step = mark_missing_path(step)
            steps[index] = step
            detail = str(step.get("error") or "这一步解决不了")
            print(detail)
            decision = decide(step, None)
            supplied = _taken_path(step, decision)
            _emit_choice(events, f"S{number}", decision, supplied)
            if supplied:
                filled = apply_supplied_path(query, step, supplied)
                if filled is None:
                    print("路径无效。蓝图用 /Game/...，源文件用 Public/类名.h 或 Private/类名.cpp。")
                    continue
                steps[index] = filled
                _share_blueprint(steps, index + 1, filled)
                refresh_pickup(steps)
                rows.append({
                    "index": number,
                    "title": title,
                    "status": "已补路径",
                    "detail": filled.get("ref_path") or filled.get("query", "")[:80],
                    "decision": supplied,
                })
                _write(path, query, rows)
                continue
            rows.append({"index": number, "title": title, "status": "暂停", "detail": detail, "decision": str(decision)})
            _write(path, query, rows)
            if decision != "skip":
                for later_i, later in enumerate(steps[index + 1:], number + 1):
                    rows.append({"index": later_i, "title": _title(later, later_i), "status": "未开始", "detail": ""})
                _write(path, query, rows)
                return 1, path
            index += 1
            continue
        if step.get("action"):
            try:
                summary = _run_action(step)
            except Exception as exc:
                summary = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        else:
            summary = run_task(step)
        detail = json.dumps(
            {key: summary.get(key) for key in ("id", "ok", "retry_count", "index_note", "editor", "error") if summary.get(key) not in (None, "")},
            ensure_ascii=False,
        )
        print(detail)
        if summary.get("ok"):
            rows.append({"index": number, "title": title, "status": "成功", "detail": detail})
            _write(path, query, rows)
            index += 1
            continue
        decision = decide(step, summary)
        _emit_choice(events, f"S{number}", decision, "")
        rows.append({"index": number, "title": title, "status": "失败", "detail": detail, "decision": decision})
        _write(path, query, rows)
        if decision == "retry":
            continue
        if decision == "skip":
            index += 1
            continue
        for later_i, later in enumerate(steps[index + 1:], number + 1):
            rows.append({"index": later_i, "title": _title(later, later_i), "status": "未开始", "detail": ""})
        _write(path, query, rows)
        return 1, path
    return 0, path
