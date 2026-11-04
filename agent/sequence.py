"""按顺序跑拆开的步骤。失败或做不了时停下来，并把每一步写进 data/runs。"""
import json
import re
from datetime import datetime
from pathlib import Path

from config import PROJECT_ROOT, task_flag, task_policy

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


_STOP_LOSS = "已尝试 3 次，Monolith 不具备该能力"
_BP_NAME_RE = re.compile(r"(?<![A-Za-z0-9_])((?:WBP|BP)_[A-Za-z0-9_]+)(?![A-Za-z0-9_])")
_GAME_PATH_RE = re.compile(r"/Game(?:/[A-Za-z0-9_]+)+")
# 本地就能判定、再试也一样的解析错误。20261026-162919：空槽位 compile_blueprint
# 三次都在进 discover 之前返回，exec_count=0，仍被写成「Monolith 不具备该能力」。
_LOCAL_RESOLVE_ERRORS = (
    "没有唯一的蓝图路径",
    "蓝图路径不是唯一的一条",
    "蓝图名和路径对不上",
)


def blueprint_switch_refusal(action: str) -> dict | None:
    """写图开关关着：立刻拒绝，次数记 0。这不是试了 3 次仍不会。"""
    from config import allow_blueprint, allow_umg

    if action in ("create_widget_blueprint", "edit_widget_blueprint"):
        if not allow_umg():
            return {
                "ok": False, "paused": True, "attempts": 0, "exec_count": 0,
                "error": "UE_ALLOW_UMG 不是 true，不改控件图。",
            }
        return None
    if action == "compile_blueprint" or action == "bind_blueprint":
        if not allow_blueprint():
            return {
                "ok": False, "paused": True, "attempts": 0, "exec_count": 0,
                "error": "UE_ALLOW_BLUEPRINT 不是 true，不改蓝图。",
            }
    return None


def _monolith_transport(tool_name, arguments):
    """短超时。连不上就返回错误原文，不把 60 秒默认超时当成卡住。"""
    import httpx

    from config import monolith_url
    from core.ue_mcp_client import UEOfficialMCPClient

    client = UEOfficialMCPClient()
    client.url = monolith_url()
    client._http.timeout = httpx.Timeout(8.0)
    try:
        return client.call_tool(tool_name, arguments or {})
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        client.close()


def _unique_blueprint_ref(step: dict) -> dict:
    """0 个或多个同名资产都算失败，不挑第一个。"""
    slots = step.get("slots") if isinstance(step.get("slots"), dict) else {}
    chunks = []
    for key in ("blueprint", "name", "asset_name", "ref_path", "asset_path", "folder_path"):
        if slots.get(key):
            chunks.append(str(slots[key]))
    for key in ("ref_path", "asset_path", "asset_name", "folder_path", "title", "query"):
        if step.get(key):
            chunks.append(str(step[key]))
    blob = "\n".join(chunks)
    paths = list(dict.fromkeys(_GAME_PATH_RE.findall(blob)))
    names = list(dict.fromkeys(_BP_NAME_RE.findall(blob)))
    asset_paths = []
    for path in paths:
        leaf = path.split(".", 1)[0].rsplit("/", 1)[-1]
        if leaf.startswith(("BP_", "WBP_")):
            asset_paths.append(path.split(".", 1)[0])
    asset_paths = list(dict.fromkeys(asset_paths))
    if len(asset_paths) > 1 or len(names) > 1:
        return {"ok": False, "error": "蓝图路径不是唯一的一条"}
    if len(asset_paths) == 1:
        path = asset_paths[0]
        leaf = path.rsplit("/", 1)[-1]
        if names and names[0] != leaf:
            return {"ok": False, "error": "蓝图名和路径对不上"}
        return {"ok": True, "ref": f"{path}.{leaf}", "asset_path": path, "asset_name": leaf}
    if len(names) == 1:
        return {"ok": True, "ref": names[0], "asset_path": names[0], "asset_name": names[0]}
    return {"ok": False, "error": "没有唯一的蓝图路径"}


def _monolith_namespace(action: str) -> tuple[str, str]:
    if action in ("create_widget_blueprint", "edit_widget_blueprint"):
        namespace = "ui"
    else:
        namespace = "blueprint"
    needle = "create" if action == "create_widget_blueprint" else "compile"
    return namespace, needle


def _choose_action(actions, needle: str) -> str:
    names = []
    for item in actions or []:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict) and str(item.get("name") or "").strip():
            names.append(str(item["name"]).strip())
    hits = [name for name in names if needle in name.lower()]
    if len(hits) == 1:
        return hits[0]
    if len(names) == 1:
        return names[0]
    return ""


def repair_blueprint_once(step: dict) -> dict:
    """一次修复：解析资产，还不知道 action 才 discover，然后 describe、exec、用 exec 结果当复查。"""
    from backends import monolith

    resolved = _unique_blueprint_ref(step)
    if not resolved.get("ok"):
        return {"ok": False, "error": str(resolved.get("error") or "没有唯一的蓝图路径"), "exec_count": 0}
    namespace, needle = _monolith_namespace(str(step.get("action") or ""))
    found = monolith.discover(namespace, transport=_monolith_transport)
    if not found.get("ok"):
        return {"ok": False, "error": str(found.get("error") or "discover 失败"), "exec_count": 0}
    action_name = _choose_action(found.get("actions"), needle)
    if not action_name:
        return {"ok": False, "error": "discover 没有返回唯一的 action", "exec_count": 0}
    described = monolith.describe(namespace, action_name, transport=_monolith_transport)
    if not described.get("ok"):
        return {"ok": False, "error": str(described.get("error") or "describe 失败"), "exec_count": 0}
    params = {
        "asset_path": resolved["asset_path"],
        "path": resolved["asset_path"],
        "blueprint": resolved["ref"],
        "ref_path": resolved["ref"],
    }
    result = monolith.exec(
        namespace, action_name, params,
        task={"allow_writes": [action_name], **task_policy()},
        transport=_monolith_transport,
    )
    if not result.get("ok"):
        return {"ok": False, "error": _error_text(result) or "exec 失败", "exec_count": 1}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if data.get("compiled") is False or data.get("verified") is False:
        return {"ok": False, "error": str(data.get("error") or "复查未通过"), "exec_count": 1}
    return {
        "ok": True,
        "summary": "复查通过",
        "index_note": "复查通过",
        "ref": resolved["ref"],
        "exec_count": 1,
    }


def _local_resolve_error(result: dict) -> str:
    """解析失败且一次 exec 都没有：原样返回，不套止损句，也不再重试。"""
    if int((result or {}).get("exec_count") or 0) != 0:
        return ""
    err = str((result or {}).get("error") or "")
    if any(token in err for token in _LOCAL_RESOLVE_ERRORS):
        return err
    return ""


def repair_blueprint(step: dict, *, attempts: int = 3) -> dict:
    """同一张蓝图、同一个目标最多 attempts 次。开关关着则 0 次。

    路径解析失败不算 Monolith 尝试：那种错误不进 discover，重试结果不变。
    FPS HUD 的控件图有确定性落地（agent/fps_hud.py），一次就够，不走下面的 discover 猜。
    """
    action = str((step or {}).get("action") or "")
    refused = blueprint_switch_refusal(action)
    if refused:
        return refused
    if action == "edit_widget_blueprint":
        from agent.fps_hud import build_fps_hud

        built = build_fps_hud(step)
        if built is not None:
            built.setdefault("exec_count", 1)
            return built
    if action == "create_widget_blueprint":
        # 控件图创建也不再走 discover 猜 action：`ui` 命名空间里 create_* 一大片，
        # 针名判不出唯一（20261029-020749 的 W3）。_create_widget_blueprint 本来就
        # 幂等（存在就不重建）、带 set_parent/save/compile 裁判，直接复用它。
        out = _create_widget_blueprint(
            {**step, "folder_path": step.get("folder_path") or _widget_default_folder(step)}
        )
        out.setdefault("attempts", 1)
        out.setdefault("exec_count", 1)
        return out
    from backends.select import current_backend

    if current_backend() != "monolith":
        return {
            "ok": False, "attempts": 0, "exec_count": 0,
            "error": "UE_EDITOR_BACKEND 不是 monolith，不走这 3 次。",
        }
    last = {"ok": False, "error": "没有执行", "exec_count": 0}
    total_exec = 0
    used = 0
    for used in range(1, max(1, int(attempts)) + 1):
        last = repair_blueprint_once(step)
        total_exec += int(last.get("exec_count") or 0)
        if last.get("ok"):
            last["attempts"] = used
            last["exec_count"] = total_exec
            return last
        local = _local_resolve_error(last)
        if local:
            return {
                "ok": False,
                "error": local,
                "summary": local,
                "attempts": 1,
                "exec_count": 0,
            }
    err = str(last.get("error") or "")
    text = _STOP_LOSS if int(attempts) == 3 else f"已尝试 {int(attempts)} 次，Monolith 不具备该能力"
    if err:
        text = f"{text}：{err}"
    return {
        "ok": False,
        "error": text,
        "summary": text,
        "attempts": used,
        "exec_count": total_exec,
    }


def _run_action(step: dict) -> dict:
    from core.editor_exec import exec_tool
    from core.judge import judge

    task = {"allow_writes": ["create", "add_to_scene_from_asset"], **task_policy()}
    action = step.get("action")
    if action in ("compile_blueprint", "bind_blueprint", "create_widget_blueprint", "edit_widget_blueprint"):
        from backends.select import current_backend

        if current_backend() == "monolith":
            return repair_blueprint(step)
    if action == "launch_editor":
        return _launch_editor(step)
    if action == "create_level":
        return _create_level(step)
    if action == "open_level":
        return _open_level(step)
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
        place_note = "已放入当前关卡"
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
                place_note = f"已放入当前关卡，与已有同名实例({len(refs)} 个)错开 50 uu"
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
            saved = _save_level(step)
            if not saved.get("ok"):
                return {"ok": False, "id": step["actor_name"], "error": str(saved.get("error") or "关卡保存失败")}
            return {"ok": True, "id": step["actor_name"], "index_note": f"{place_note}，{saved.get('note')}"}
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
    if action == "query_actor":
        return _query_actor(step)
    if action == "run_automation_test":
        return _run_automation(step)
    if action == "implement_shooter":
        return _implement_shooter(step)
    if action == "implement_grenade":
        return _implement_grenade(step)
    if action == "apply_spec":
        return _apply_spec(step)
    if action == "solve":
        from agent.solver import solve

        return solve(step)
    if action == "check_platform_hud":
        return _check_platform_hud(step)
    if action == "check_fps_hud":
        return _check_fps_hud(step)
    if action == "set_scalar_param":
        return _set_scalar_param(step)
    if action == "query_material":
        return _query_material(step)
    if action == "place_batch":
        return _place_batch(step)
    if action == "remove_batch":
        return _remove_batch(step)
    if action == "create_widget_blueprint":
        return _create_widget_blueprint(step)
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
        out = _run_action(step)
        return _bind_evidence(step, out)
    return {"ok": False, "error": f"未知动作 {action}"}


def _bind_evidence(step: dict, out: dict) -> dict:
    """bind_blueprint 的验收（U1）：唯一蓝图路径真的存在，编辑器只读断言。

    bind_slot 本来只做解析，返回 ref 就算成功——「能规划、能执行、没裁判」的假成功口子。
    现在用一次 find_assets 只读核对；核对失败按失败处理，不编造成功。"""
    if not isinstance(out, dict) or not out.get("ok"):
        return out
    ref = str(out.get("ref") or "")
    if not ref:
        return out
    asset_path = ref.split(".", 1)[0]
    folder = asset_path.rsplit("/", 1)[0] if "/" in asset_path else "/Game"
    asset = asset_path.rsplit("/", 1)[-1]
    present = _blueprint_present(folder, asset, asset_path)
    if not present.get("ok"):
        out["ok"] = False
        out["error"] = f"唯一蓝图路径核对失败：{present.get('error') or '查蓝图失败'}"
        return out
    if not present.get("present"):
        out["ok"] = False
        out["error"] = f"唯一蓝图路径 {ref} 在编辑器里找不到，不猜。"
        return out
    out["test"] = {
        "ok": True, "kind": "editor_assert",
        "summary": f"唯一蓝图路径 {ref} 已确认存在", "errors": [],
    }
    out["judge_kind"] = "editor_assert"
    return out


def _launch_editor(step: dict) -> dict:
    """只把编辑器进程拉起来并等 MCP 端口。不编译、不改场景、不保存、不进 PIE。
    「启动 UE 编辑器，其他啥也别干」这类句子走这里，不再借 compile_project 的空转。"""
    from core.editor_launch import editor_exe, editor_running, ensure_editor, launch_editor

    del step
    was_open = editor_running()
    if not was_open:
        exe = editor_exe()
        if not exe.is_file():
            text = f"找不到 {exe}"
            return {"ok": False, "error": text, "summary": text, "index_note": text}
        launched = launch_editor()
        if not launched.get("ok"):
            text = str(launched.get("note") or "启动编辑器失败")
            return {"ok": False, "error": text, "summary": text, "index_note": text}
    brought = ensure_editor()
    if not brought.get("ok"):
        text = str(brought.get("note") or "编辑器没有起来")
        return {"ok": False, "error": text, "summary": text, "index_note": text}
    note = "编辑器已在运行" if was_open else "已启动编辑器"
    note += "，MCP 已连通。未编译、未改场景、未保存、未进 PIE。"
    # synthetic（U3b，20261027-180955）：这条判词只证明「端口通了」，不证明任何关卡内容。
    # apply_judge 对 synthetic 判词只放行「条款的证明窗口恰好只有本扇」的条款，
    # 防止 kind 宽绑定把「金币已放置」这类内容条款被启动扇顺手翻绿。
    judge = {"ok": True, "kind": "editor_assert", "summary": note, "errors": [],
             "synthetic": True}
    return {
        "ok": True,
        "id": "launch_editor",
        "summary": note,
        "index_note": note,
        "test": judge,
        "judge_kind": "editor_assert",
        "save": False,
        "pie": False,
    }


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


def _monolith_active() -> bool:
    from backends.select import current_backend

    return current_backend() == "monolith"


def _save_level(step: dict) -> dict:
    """写场景之后按任务策略落盘当前关卡。save 关掉时直接返回 ok，附一句未保存。

    monolith：没有 get_current_level，用 list_dirty_packages 找出脏的地图包再
    save_packages——放置过的关卡一定脏，列不出来就是没有要落盘的改动。"""
    from core.editor_exec import exec_tool

    if not task_flag(step, "save"):
        return {"ok": True, "saved": False, "note": "未保存"}
    from backends.select import current_backend

    if current_backend() == "monolith":
        from backends.monolith import exec as monolith_exec

        listed = monolith_exec("editor", "list_dirty_packages",
                               {"include_maps": True, "include_content": False},
                               task=_read_task(), transport=_monolith_transport)
        if not listed.get("ok"):
            return {"ok": False, "saved": False,
                    "error": f"读未保存的关卡包失败：{listed.get('error') or '没有返回'}"}
        data = listed.get("data") if isinstance(listed.get("data"), dict) else {}
        # monolith list_dirty_packages 的行在 dirty_packages 键下（源码 HandleListDirtyPackages）；
        # 读错键会变成「没有未保存的关卡改动」静默跳过保存——20261021-221446 的教训换入口重现，
        # U3 重跑（20261027-191740）第一版就栽在这里，关卡落盘时间停在昨天。
        if "dirty_packages" not in data:
            return {"ok": False, "saved": False,
                    "error": "list_dirty_packages 返回里没有 dirty_packages 键，形状变了，不猜"}
        packages = data.get("dirty_packages") if isinstance(data.get("dirty_packages"), list) else []
        maps = [str(row.get("package") or "") for row in packages
                if isinstance(row, dict) and row.get("is_map") and row.get("package")]
        if not maps:
            # 键在、列表空：关卡真的没有未落盘的改动（全幂等重跑就是这种），不算失败
            return {"ok": True, "saved": False, "note": "没有未保存的关卡改动"}
        saved = monolith_exec("editor", "save_packages", {"packages": maps},
                              task={"allow_writes": [], **task_policy(save=True, pie=False)},
                              transport=_monolith_transport)
        if not saved.get("ok"):
            return {"ok": False, "saved": False,
                    "error": f"关卡保存失败：{saved.get('error') or '未存上'}"}
        return {"ok": True, "saved": True, "note": f"已保存关卡 {'、'.join(maps)}"}
    level = exec_tool(_SCENE, "get_current_level", {}, task=_read_task())
    data = level.get("data") if isinstance(level, dict) else None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            data = None
    level_path = ""
    if isinstance(data, dict):
        inner = data.get("returnValue") if "returnValue" in data else data
        if isinstance(inner, str):
            level_path = inner
        elif isinstance(inner, dict):
            level_path = str(inner.get("returnValue") or "")
    if not level_path:
        return {"ok": False, "saved": False,
                "error": "没有当前关卡路径，关卡未保存（改动还在内存里，未落盘）"}
    saved = exec_tool(
        _ASSET_TOOLS, "save_assets",
        {"asset_paths": [level_path]},
        task={"allow_writes": [], **task_policy(save=True, pie=False)},
    )
    if not saved.get("ok"):
        return {"ok": False, "saved": False,
                "error": f"关卡保存失败：{saved.get('error') or '未存上'}"}
    return {"ok": True, "saved": True, "note": f"已保存当前关卡 {level_path}"}


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
    """蓝图父类用反射名。模块名来自 project_context。"""
    from config import script_object_path

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
    return script_object_path(bare)


_WIDGET_PARENT = "/Script/UMG.UserWidget"
_WIDGET_CLASS = "/Script/UMG.WidgetBlueprint"


def _named_asset_present(folder: str, asset: str, asset_path: str, class_path: str) -> dict:
    """按给定资产类复查。找不到是 present=False，查询失败才 ok=False。"""
    from core.editor_exec import exec_tool

    found = exec_tool(
        "editor_toolset.toolsets.asset.AssetTools",
        "find_assets",
        {
            "folder_path": folder or "/Game",
            "name": asset,
            "asset_type": {"refPath": class_path},
            "recursive": False,
        },
        task=_read_task(),
    )
    if not found.get("ok"):
        err = str(found.get("error") or "查资产失败")
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


def _widget_parent_ref(raw: str) -> str:
    """User Widget 收到 UMG。其余 /Script 路径原样，短类名用当前工程模块。"""
    from config import script_object_path

    text = str(raw or "").strip()
    if not text or text.lower().replace(" ", "") in {"userwidget", "user widget"}:
        return _WIDGET_PARENT
    if text.startswith("/Script/"):
        return text
    return script_object_path(text)


def _widget_default_folder(step: dict) -> str:
    """create 步没给目录时按同一个默认口径补 /Game/UI（与 plan_windows 的命名口径一致）。"""
    from agent.fps_hud import widget_ref_from

    ref = widget_ref_from(step or {})
    package = str(ref).rsplit(".", 1)[0]
    return package.rsplit("/", 1)[0] if "/" in package else "/Game/UI"


def _create_widget_blueprint(step: dict) -> dict:
    """按步骤里的目录和名字新建 Widget Blueprint。不改控件图。资产本身保存这一份。"""
    from config import allow_umg
    from core.editor_exec import exec_tool

    if not allow_umg():
        return {
            "ok": False,
            "paused": True,
            "error": "UE_ALLOW_UMG 不是 true，不新建 Widget Blueprint。",
        }
    slots = step.get("slots") if isinstance(step.get("slots"), dict) else {}
    folder = str(step.get("folder_path") or slots.get("folder_path") or slots.get("folder") or "").strip().rstrip("/")
    name = str(step.get("asset_name") or slots.get("asset_name") or slots.get("name") or "").strip()
    parent = _widget_parent_ref(str(step.get("parent") or slots.get("parent") or ""))
    if not (folder == "/Game" or folder.startswith("/Game/")) or not name.startswith(("WBP_", "BP_")):
        return {"ok": False, "error": "没有唯一的 Widget 路径"}
    package = f"{folder}/{name}"
    ref = f"{package}.{name}"
    present = _named_asset_present(folder, name, package, _WIDGET_CLASS)
    if not present.get("ok"):
        return present
    note = f"{ref}（已存在，不重复新建）" if present.get("present") else ref
    write = {"allow_writes": ["create", "set_parent"], **task_policy()}
    if not present.get("present"):
        created = exec_tool(
            "editor_toolset.toolsets.blueprint.BlueprintTools",
            "create",
            {
                "folder_path": folder,
                "asset_name": name,
                "asset_type": {"refPath": parent},
            },
            task=write,
        )
        if not created.get("ok"):
            err = str(created.get("error") or "")
            if not any(token in err.lower() for token in ("exist", "already", "已存在")):
                return {"ok": False, "id": name, "error": err or "创建 Widget Blueprint 失败"}
            note = f"{ref}（已存在，不重复新建）"
        parented = exec_tool(
            "editor_toolset.toolsets.blueprint.BlueprintTools",
            "set_parent",
            {
                "blueprint": {"refPath": ref},
                "parent_class": {"refPath": parent},
            },
            task=write,
        )
        if not parented.get("ok"):
            return {
                "ok": False,
                "id": name,
                "error": f"资产已写到 {ref}。父类 {parent} 没设上：{parented.get('error')}",
            }
    again = _named_asset_present(folder, name, package, _WIDGET_CLASS)
    if not again.get("ok"):
        return again
    if not again.get("present"):
        plain = _named_asset_present(folder, name, package, "/Script/Engine.Blueprint")
        if plain.get("present"):
            return {
                "ok": False,
                "id": name,
                "error": f"{ref} 建成的是普通蓝图，不是 Widget Blueprint。控件图未改。",
            }
        return {"ok": False, "id": name, "error": f"复查没有找到 Widget Blueprint {ref}"}
    saved = exec_tool(
        _ASSET_TOOLS, "save_assets",
        {"asset_paths": [package]},
        task={"allow_writes": [], "save": True, "pie": False},
    )
    if not saved.get("ok"):
        return {
            "ok": False,
            "id": name,
            "error": f"{name} 已在编辑器里，保存这份资产失败：{saved.get('error') or '未存上'}。",
        }
    # U1：验收不只「资产存在」，还要这张 Widget Blueprint 编得过。编译是只读裁判，不保存。
    from core.judge import judge

    test = judge({"kind": "blueprint_compile", "refPath": ref}, {"allow_writes": [], **task_policy()})
    out = {"ok": True, "id": name, "ref": ref, "index_note": f"{note}；已保存这份资产"}
    out["test"] = test
    out["judge_kind"] = "blueprint_compile"
    if not test.get("ok"):
        out["ok"] = False
        out["error"] = str(test.get("summary") or f"{ref} 编译失败")
    return out


def _level_template_path() -> str:
    """复制关卡的源模板。UE_LEVEL_TEMPLATE 优先，否则用 project_context 的当前关卡。没有就空。"""
    import os

    from config import project_level_package

    text = os.environ.get("UE_LEVEL_TEMPLATE", "").strip().replace("\\", "/").rstrip("/")
    if text:
        return text.split(".", 1)[0]
    return project_level_package()


def _as_present(result) -> bool:
    """exists 这类只回布尔的原语：data / data.returnValue / JSON 字符串都能认，读不出当 False。"""
    data = result.get("data") if isinstance(result, dict) else result
    if isinstance(data, dict) and "returnValue" in data:
        data = data["returnValue"]
    if isinstance(data, str):
        return data.strip().lower() in ("true", "1", "yes")
    return bool(data)


def _current_level_text(result) -> str:
    """get_current_level 的读数：字符串，或 dict 里的某个路径键。读不出返回空串。"""
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, dict) and "returnValue" in data:
        data = data["returnValue"]
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("path", "level_path", "package_path", "name"):
            if data.get(key):
                return str(data[key])
        return ""
    return "" if data is None else str(data)


def _level_matches(text: str, package: str, name: str) -> bool:
    """当前关卡是不是目标关卡：先比包路径（去掉 .Object / :PersistentLevel 尾巴），
    再按词边界比关卡名——直接拿子串比会让 L_NewTest 被 L_NewTest2 顶上。"""
    normalized = str(text or "").replace("\\", "/").strip()
    if not normalized:
        return False
    head = normalized.split(":", 1)[0]
    folder, _, tail = head.rpartition("/")
    if "." in tail:
        tail = tail.split(".", 1)[0]
        head = f"{folder}/{tail}" if folder else tail
    if package and head == package:
        return True
    if name and head.rsplit("/", 1)[-1] == name:
        return True
    if not name:
        return False
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", normalized) is not None


def _monolith_payload(result: dict) -> dict:
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def _error_text(result: dict) -> str:
    err = result.get("error") if isinstance(result, dict) else ""
    if isinstance(err, dict):
        return str(err.get("message") or err.get("error") or err)
    return str(err or "")


def _level_absent(error: str) -> bool:
    """索引里没有这份资产。别的失败（连不上、子系统没有）不能当成「不存在」。"""
    low = error.lower()
    return (
        "not found" in low or "does not exist" in low
        or "未找到" in error or "不存在" in error
    )


def _level_collision(error: str) -> bool:
    if _level_absent(error):
        return False
    low = error.lower()
    return "already" in low or "exist" in low or "已存在" in error


def _level_done(name: str, package: str, note: str) -> dict:
    summary = f"{note}；已成为当前关卡。"
    judge = {"ok": True, "kind": "editor_assert", "summary": summary, "errors": []}
    return {
        "ok": True,
        "id": name,
        "summary": summary,
        "index_note": summary,
        "ref": package,
        "level_path": package,
        "in_level": True,
        "test": judge,
        "judge_kind": "editor_assert",
    }


def _create_level_monolith(package: str, name: str, template: str, write: dict) -> dict:
    """monolith 没有通用的关卡复制。先用 project.get_asset_details 查有没有，
    没有就 editor.create_empty_map（会自己落盘），再按需要 editor.load_level。
    不调用官方 exec_tool，也不在被拒绝之后改去调另一边。"""
    from backends.monolith import exec as monolith_exec

    probed = monolith_exec(
        "project", "get_asset_details", {"asset_path": package}, task=_read_task(),
        transport=_monolith_transport,
    )
    if probed.get("ok"):
        present = True
    else:
        err = _error_text(probed)
        if not _level_absent(err):
            return {
                "ok": False,
                "id": name,
                "error": f"查关卡 {package} 是否已存在失败：{err or '没有返回'}",
            }
        present = False

    note = f"{package}（已存在，不重复新建）" if present else ""
    if not present:
        if template == package:
            return {"ok": False, "id": name, "error": f"模板关卡就是 {package}，没有可复制的新关卡。"}
        if not task_flag(write, "save"):
            return {"ok": False, "id": name, "error": f"关卡资产 {package} 落盘失败：save 未放行"}
        created = monolith_exec(
            "editor",
            "create_empty_map",
            {"path": package, "open": True, "dirty_policy": "discard"},
            task=write,
            transport=_monolith_transport,
        )
        if not created.get("ok"):
            err = _error_text(created)
            if not _level_collision(err):
                return {
                    "ok": False,
                    "id": name,
                    "error": f"新建关卡 {package} 失败：{err or '没有返回'}",
                }
            note = f"{package}（已存在，不重复新建）"
        else:
            data = _monolith_payload(created)
            world = str(data.get("current_world") or "")
            note = f"已新建空白关卡 {package}"
            if _as_bool(data.get("opened")) is True and _level_matches(world, package, name):
                return _level_done(name, package, note)

    if not task_flag(write, "save"):
        return {"ok": False, "id": name, "error": f"关卡资产 {package} 落盘失败：save 未放行"}
    loaded = monolith_exec(
        "editor",
        "load_level",
        {"path": package, "dirty_policy": "discard"},
        task=write,
        transport=_monolith_transport,
    )
    if not loaded.get("ok"):
        return {
            "ok": False,
            "id": name,
            "error": f"关卡 {package} 加载失败：{_error_text(loaded) or '没有返回'}",
        }
    data = _monolith_payload(loaded)
    flag = _as_bool(data.get("loaded"))
    if flag is None:
        flag = _as_bool(data.get("ok"))
    if flag is not True:
        return {
            "ok": False,
            "id": name,
            "error": f"关卡 {package} 加载失败：{data.get('message') or '没有返回'}",
        }
    world = str(data.get("current_world") or data.get("path") or "")
    if world and not _level_matches(world, package, name):
        return {
            "ok": False,
            "id": name,
            "error": f"加载后当前关卡是 {world}，不是 {package}。",
        }
    return _level_done(name, package, note)


def _create_level(step: dict) -> dict:
    """新建关卡并切过去。official：exists → duplicate(模板) → save_assets → load_level →
    get_current_level。monolith：get_asset_details → create_empty_map → 按需要 load_level。
    已存在就跳过新建（幂等，note 写明不重复新建）。
    任一步失败都返回 {ok: False, error: 哪一步}，不猜、不静默。"""
    from agent.plan_windows import resolve_level

    slots = step.get("slots") if isinstance(step.get("slots"), dict) else {}
    raw = (
        step.get("level_path") or step.get("package_path") or step.get("level_name")
        or step.get("level") or slots.get("level") or step.get("asset_name") or ""
    )
    found = resolve_level(raw)
    if not found.get("ok"):
        return {"ok": False, "error": str(found.get("error") or "缺少关卡名，不猜。")}
    package = str(found["level_path"])
    name = str(found["level_name"])
    template = _level_template_path()
    if not template:
        return {"ok": False, "id": name, "error": "没有关卡模板。设置 UE_LEVEL_TEMPLATE，或先导出 project_context 的 level_name。"}
    # save 跟随 task_policy()：落盘是这一步的交付，关掉 save 就让它在保存那一步如实失败。
    write = {"allow_writes": ["duplicate", "save_assets", "load_level"], **task_policy()}
    from backends.select import current_backend

    if current_backend() == "monolith":
        return _create_level_monolith(package, name, template, write)

    from core.editor_exec import exec_tool

    probed = exec_tool(
        _ASSET_TOOLS, "exists", {"path": package}, task=_read_task(),
    )
    if not probed.get("ok"):
        return {
            "ok": False,
            "id": name,
            "error": f"查关卡 {package} 是否已存在失败：{probed.get('error') or '没有返回'}",
        }
    note = f"{package}（已存在，不重复新建）" if _as_present(probed) else ""
    if not note:
        if template == package:
            return {"ok": False, "id": name, "error": f"模板关卡就是 {package}，没有可复制的新关卡。"}
        copied = exec_tool(
            _ASSET_TOOLS, "duplicate", {"path": template, "new_path": package}, task=write,
        )
        if not copied.get("ok"):
            err = str(copied.get("error") or "")
            if any(token in err.lower() for token in ("exist", "already", "已存在")):
                note = f"{package}（已存在，不重复新建）"
            else:
                return {
                    "ok": False,
                    "id": name,
                    "error": f"从模板 {template} 复制关卡 {package} 失败：{err or '没有返回'}",
                }
        else:
            note = f"已从模板 {template} 复制出 {package}"

    saved = exec_tool(_ASSET_TOOLS, "save_assets", {"asset_paths": [package]}, task=write)
    if not saved.get("ok"):
        return {
            "ok": False,
            "id": name,
            "error": f"关卡资产 {package} 落盘失败：{saved.get('error') or '没有返回'}",
        }

    loaded = exec_tool(_SCENE, "load_level", {"level_path": package}, task=write)
    if not loaded.get("ok"):
        return {
            "ok": False,
            "id": name,
            "error": f"关卡 {package} 加载失败：{loaded.get('error') or '没有返回'}",
        }

    current = exec_tool(_SCENE, "get_current_level", {}, task=_read_task())
    if not current.get("ok"):
        return {
            "ok": False,
            "id": name,
            "error": f"复查当前关卡失败：{current.get('error') or '没有返回'}",
        }
    text = _current_level_text(current)
    if not _level_matches(text, package, name):
        return {
            "ok": False,
            "id": name,
            "error": f"加载后当前关卡是 {text or '读不出'}，不是 {package}。",
        }
    summary = f"{note}；已成为当前关卡。"
    judge = {"ok": True, "kind": "editor_assert", "summary": summary, "errors": []}
    return {
        "ok": True,
        "id": name,
        "summary": summary,
        "index_note": summary,
        "ref": package,
        "level_path": package,
        "in_level": True,
        "test": judge,
        "judge_kind": "editor_assert",
    }


def _level_step_target(step: dict) -> dict:
    from agent.plan_windows import resolve_level

    slots = step.get("slots") if isinstance(step.get("slots"), dict) else {}
    raw = (
        step.get("level_path") or step.get("package_path") or step.get("level_name")
        or step.get("level") or slots.get("level") or step.get("asset_name") or ""
    )
    return resolve_level(raw)


def _level_miss(name: str, text: str) -> dict:
    judge = {"ok": False, "kind": "editor_assert", "summary": text, "errors": [{"file": "", "line": 0, "message": text}]}
    return {
        "ok": False,
        "id": name,
        "error": text,
        "summary": text,
        "index_note": text,
        "test": judge,
        "judge_kind": "editor_assert",
        "in_level": False,
        "pie": False,
        "save": False,
    }


def _load_existing_level(package: str, name: str) -> dict:
    """只加载已有关卡。不存在就失败，不复制、不新建。"""
    from backends.monolith import exec as monolith_exec
    from backends.select import current_backend

    write = {"allow_writes": ["load_level"], **task_policy(save=True, pie=False)}
    if current_backend() == "monolith":
        probed = monolith_exec(
            "project", "get_asset_details", {"asset_path": package}, task=_read_task(),
            transport=_monolith_transport,
        )
        if not probed.get("ok"):
            err = _error_text(probed)
            if _level_absent(err):
                return _level_miss(name, f"关卡 {package} 不存在，不新建。")
            return _level_miss(name, f"查关卡 {package} 是否已存在失败：{err or '没有返回'}")
        loaded = monolith_exec(
            "editor", "load_level", {"path": package, "dirty_policy": "refuse"},
            task=write, transport=_monolith_transport,
        )
        if not loaded.get("ok"):
            return _level_miss(name, f"关卡 {package} 加载失败：{_error_text(loaded) or '没有返回'}")
        data = _monolith_payload(loaded)
        flag = _as_bool(data.get("loaded"))
        if flag is None:
            flag = _as_bool(data.get("ok"))
        if flag is not True:
            return _level_miss(name, f"关卡 {package} 加载失败：{data.get('message') or '没有返回'}")
        world = str(data.get("current_world") or data.get("path") or "")
        if world and not _level_matches(world, package, name):
            return _level_miss(name, f"加载后当前关卡是 {world}，不是 {package}。")
        return _level_done(name, package, f"已打开 {package}")

    from core.editor_exec import exec_tool

    probed = exec_tool(_ASSET_TOOLS, "exists", {"path": package}, task=_read_task())
    if not probed.get("ok"):
        return _level_miss(name, f"查关卡 {package} 是否已存在失败：{probed.get('error') or '没有返回'}")
    if not _as_present(probed):
        return _level_miss(name, f"关卡 {package} 不存在，不新建。")
    loaded = exec_tool(_SCENE, "load_level", {"level_path": package}, task=write)
    if not loaded.get("ok"):
        return _level_miss(name, f"关卡 {package} 加载失败：{loaded.get('error') or '没有返回'}")
    current = exec_tool(_SCENE, "get_current_level", {}, task=_read_task())
    if not current.get("ok"):
        return _level_miss(name, f"复查当前关卡失败：{current.get('error') or '没有返回'}")
    text = _current_level_text(current)
    if not _level_matches(text, package, name):
        return _level_miss(name, f"加载后当前关卡是 {text or '读不出'}，不是 {package}。")
    return _level_done(name, package, f"已打开 {package}")


def _open_level(step: dict) -> dict:
    """打开已有关卡。没有就停，不改成新建。"""
    found = _level_step_target(step)
    if not found.get("ok"):
        text = str(found.get("error") or "缺少关卡名，不猜。")
        return _level_miss("", text)
    return _load_existing_level(str(found["level_path"]), str(found["level_name"]))


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
    task = {"allow_writes": ["create", "set_parent"], **task_policy()}
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
    task = {"allow_writes": ["add_to_scene_from_asset"], **task_policy()}
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
        saved = _save_level(slot)
        if not saved.get("ok"):
            return {"ok": False, "error": str(saved.get("error") or "关卡保存失败")}
        return {"ok": True, "note": f"已放入 {name}，{saved.get('note')}"}
    err = str(placed.get("error") or "放进场景失败")
    if _conn_error(err):
        return {"ok": False, "error": f"编辑器 MCP 连不上：{err}"}
    return {"ok": False, "error": err}


def _place_pair(step: dict) -> dict:
    """缺的实例才放。两者都已在关卡里则不动。放完按任务策略保存关卡。"""
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
    saved = _save_level(step)
    if not saved.get("ok"):
        return {"ok": False, "error": str(saved.get("error") or "关卡保存失败")}
    return {"ok": True, "index_note": "；".join(notes) + f"。{saved.get('note')}"}


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
    return fallback


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
    """进入 PIE。移速句子再核对 PIE 里的 MoveSpeed 是否等于源码，以及是否达到所说的倍数。"""
    from core.editor_exec import exec_tool

    query = str(step.get("query") or "")
    stem = _pawn_stem(query)
    if not stem:
        return {"ok": False, "error": "句子里对不上唯一的 Pawn"}
    pie = {"allow_writes": [], **task_policy(save=False, pie=True)}
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
    """镜头自动化测试。句子要求 PIE 时再进一次，确认场景里有 Pawn，然后停掉。"""
    from core.editor_exec import exec_tool
    from core.judge import judge

    filt = str(step.get("automation_filter") or "").strip()
    if not filt:
        return {"ok": False, "error": "没有 Automation 过滤器"}
    result = judge({"kind": "automation", "filter": filt}, {"allow_writes": [], "save": False, "pie": False})
    if not result.get("ok"):
        return {"ok": False, "error": str(result.get("summary") or "镜头测试没过")}
    note = str(result.get("summary") or "镜头测试通过")
    if not step.get("pie"):
        return {"ok": True, "index_note": note}
    pie = {"allow_writes": [], **task_policy(save=False, pie=True)}
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
        stem = _pawn_stem(str(step.get("query") or ""))
        if not stem:
            return {"ok": False, "error": "句子里对不上唯一的 Pawn"}
        found = _find_named(stem, {"allow_writes": [], "save": False, "pie": False})
        if not found.get("ok") or found.get("missing"):
            return {"ok": False, "error": str(found.get("error") or f"PIE 里没有 {stem}")}
        return {"ok": True, "index_note": note + f"，PIE 里有 {stem}"}
    finally:
        exec_tool(app, "StopPIE", {}, task=pie)


def _probe_move(step: dict) -> dict:
    """编译之后跑移动自动化测试。"""
    from core.judge import judge

    filt = str(step.get("automation_filter") or "").strip()
    if not filt:
        return {"ok": False, "error": "没有 Automation 过滤器"}
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
    """按已经解析好的名字和坐标移动。不进模型。写完再读位置，按任务策略保存关卡。"""
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
    before = _read_location(ref, read)
    if before.get("ok") and step.get("run_id"):
        from agent.prop_snapshot import capture_props

        x, y, z = before["location"]
        capture_props(str(step.get("run_id")), ref, {"location": {"x": x, "y": y, "z": z}}, kind="location")
    write = {"allow_writes": ["set_actor_transform"], **task_policy()}
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
    saved = _save_level(step)
    if not saved.get("ok"):
        return {"ok": False, "id": name, "ref": ref, "in_level": True, "error": str(saved.get("error") or "关卡保存失败")}
    return {
        "ok": True,
        "id": name,
        "ref": ref,
        "in_level": True,
        "index_note": f"已移到 ({target['x']:g}, {target['y']:g}, {target['z']:g})，{saved.get('note')}",
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
    before = exec_tool(
        _OBJECT,
        "get_properties",
        {"instance": {"refPath": ref}, "properties": ["bPickable", "Tags"]},
        task=read,
    )
    if before.get("ok") and step.get("run_id"):
        from agent.prop_snapshot import capture_props

        values = _parse_props(before.get("data"))
        if isinstance(values, dict):
            capture_props(
                str(step.get("run_id")),
                ref,
                {"bPickable": values.get("bPickable"), "Tags": values.get("Tags")},
            )
    write = {"allow_writes": ["set_properties", "add_tag"], **task_policy()}
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
        saved = _save_level(step)
        if not saved.get("ok"):
            return {"ok": False, "id": name, "ref": ref, "in_level": True,
                    "error": str(saved.get("error") or "关卡保存失败")}
        return {
            "ok": True,
            "id": name,
            "ref": ref,
            "in_level": True,
            "index_note": f"bPickable 为真，带 InteractableActor；{saved.get('note')}",
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


def _query_actor(step: dict) -> dict:
    """契约里的只读查询。不写属性，不 PIE。"""
    from core.contract_tools import query_actor

    slots = step.get("slots") if isinstance(step.get("slots"), dict) else {}
    name = str(step.get("actor_name") or slots.get("name") or "")
    result = query_actor(name)
    summary = str(result.get("summary") or result.get("error") or "")
    judge = {"ok": bool(result.get("ok")), "kind": "editor_assert", "summary": summary, "errors": []}
    out = {
        "ok": bool(result.get("ok")),
        "id": name,
        "summary": summary,
        "index_note": summary,
        "test": judge,
        "judge_kind": "editor_assert",
    }
    if result.get("ref_path"):
        out["ref"] = result["ref_path"]
        out["in_level"] = True
    if not out["ok"]:
        out["error"] = summary or "query_actor 失败"
    return out


_WIDGET_REF = "/Game/UI/WBP_HUD.WBP_HUD"


def _hud_parent_class() -> str:
    from config import script_object_path

    return script_object_path("PlatformHUDWidget")


def _check_platform_hud(step: dict) -> dict:
    """只读核对四个合同控件名 + 父类是不是 UPlatformHUDWidget。缺了就失败。不改控件图。

    HUD 的计数、倒计时、通关/失败已经不靠蓝图连线：`UPlatformHUDWidget`（C++）按名字绑
    四个文本，`ATestPawn::EnsureHud` 负责创建并挂视口。所以这里核两件事：
    控件名在、父类是那个 C++ 类。两条都过，HUD 才是活的。"""
    from core.editor_exec import exec_tool

    from agent.umg_manual import missing_platform_hud

    del step
    read = {"allow_writes": [], "save": False, "pie": False}
    called = exec_tool(
        "UMGToolSet.UMGToolSet",
        "GetWidgets",
        {"widgetBlueprint": {"refPath": _WIDGET_REF}},
        task=read,
    )
    if not called.get("ok"):
        summary = "GetWidgets 失败：" + str(called.get("error") or "没有返回")
        return {"ok": False, "summary": summary, "error": summary, "index_note": summary, "pie": False, "save": False}
    missing = missing_platform_hud(called.get("data"))
    problems = [f"缺控件 {name}" for name in missing]

    parent = exec_tool(
        "editor_toolset.toolsets.blueprint.BlueprintTools",
        "get_parent",
        {"blueprint": {"refPath": _WIDGET_REF}},
        task=read,
    )
    parent_ref = ""
    data = parent.get("data")
    if isinstance(data, dict) and isinstance(data.get("returnValue"), dict):
        parent_ref = str(data["returnValue"].get("refPath") or "")
    hud_parent = _hud_parent_class()
    if parent.get("ok") and parent_ref and not parent_ref.endswith(hud_parent):
        problems.append(f"父类是 {parent_ref.rsplit('.', 1)[-1]}，不是 {hud_parent.rsplit('.', 1)[-1]}")

    if problems:
        summary = f"{_WIDGET_REF} 不合格：" + "；".join(problems) + "。控件图能编过，不等于 HUD 真的会动。"
    else:
        summary = (
            f"{_WIDGET_REF} 四个合同控件名都在，父类是 {hud_parent.rsplit('.', 1)[-1]}；"
            "计数、倒计时、通关/失败由 C++ 驱动，Pawn 的 EnsureHud 负责挂视口。"
        )
    judge = {"ok": not problems, "kind": "editor_assert", "summary": summary, "errors": []}
    out = {
        "ok": not problems,
        "id": _WIDGET_REF,
        "summary": summary,
        "index_note": summary,
        "test": judge,
        "judge_kind": "editor_assert",
        "ref": _WIDGET_REF,
        "in_level": False,
        "pie": False,
        "save": False,
    }
    if problems:
        out["error"] = summary
    return out


_FPS_FILTER = "AIAgent.Fps.Hit"
_FPS_SOURCES = (
    "Public/FpsPawn.h",
    "Public/FpsTarget.h",
    "Public/FpsHealthComponent.h",
)

# FPS HUD 的合同（20261029-002746 的收口口子：HUD 只有「控件图能编过」，没有一条
# 能证明「血条/子弹/手雷/载具那几块真在图上、并且有个 C++ 类会把它挂进视口」）。
# 名字是 FpsHUDWidget 的 BindWidgetOptional 字段名，改 C++ 时两处一起改。
FPS_HUD_REF = "/Game/UI/WBP_FpsHUD.WBP_FpsHUD"
FPS_HUD_PARENT = "FpsHUDWidget"
FPS_HUD_WIDGETS = (
    "txt_Health", "txt_Ammo", "txt_Grenade",
    "txt_VehicleName", "txt_EnterPrompt", "txt_VehicleSpeed",
)


def _fps_hud_ref(text: str) -> str:
    """句子里的 WBP_ 名或 /Game 路径优先；没有就用 FPS HUD 的固定合同路径。"""
    import re as _re

    blob = str(text or "")
    paths = [
        item for item in dict.fromkeys(_re.findall(r"/Game(?:/[A-Za-z0-9_]+)+", blob))
        if item.rsplit("/", 1)[-1].startswith("WBP_")
    ]
    if len(paths) == 1:
        package = paths[0].split(".", 1)[0]
        name = package.rsplit("/", 1)[-1]
        return f"{package}.{name}"
    names = list(dict.fromkeys(_re.findall(r"(?<![A-Za-z0-9_])(WBP_[A-Za-z0-9_]+)", blob)))
    if len(names) == 1:
        return f"/Game/UI/{names[0]}.{names[0]}"
    return FPS_HUD_REF


def _fps_hud_widget_names(data) -> list[str]:
    """GetWidgets 的返回（官方扁平行 / monolith 扁平行都认）里取出控件名。"""
    names: list[str] = []
    rows = data
    if isinstance(data, dict):
        for key in ("widgets", "Widgets", "data", "rows"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
        else:
            rows = []
    if isinstance(rows, (list, tuple)):
        for row in rows:
            if isinstance(row, dict):
                name = str(
                    row.get("widgetName") or row.get("name") or row.get("widget_name") or ""
                ).strip()
            else:
                name = str(row or "").strip()
            if name:
                names.append(name)
    return names


def _fps_hud_mount_entry() -> tuple[bool, str]:
    """C++ 侧有没有把 HUD 挂进视口的入口。只看源码，不连编辑器。

    判据：FpsPawn 里有 CreateWidget + AddToViewport（TestPawn::EnsureHud 那套写法）。
    没有就说明「控件图做对了但永远不会显示」，这一条必须算不合格。"""
    from config import UE_SOURCE_ROOT

    pawn_cpp = UE_SOURCE_ROOT / "Private" / "FpsPawn.cpp"
    try:
        text = pawn_cpp.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"读不到 {pawn_cpp.name}（{exc}）"
    has_create = "CreateWidget" in text
    has_mount = "AddToViewport" in text
    if has_create and has_mount:
        return True, "AFpsPawn 有 CreateWidget + AddToViewport 入口"
    missing = []
    if not has_create:
        missing.append("CreateWidget")
    if not has_mount:
        missing.append("AddToViewport")
    return False, f"AFpsPawn 没有 {'/'.join(missing)}，HUD 不会被挂进视口"


def _check_fps_hud(step: dict) -> dict:
    """只读核对 FPS HUD：六个合同控件名、父类、C++ 挂载入口。缺一条就不合格。

    这一扇不改控件图、不进 PIE、不保存；它只回答「用户要的那几块信息是不是真的在
    HUD 上，而且有东西会把它显示出来」。控件图能编过不等于 HUD 会动。"""
    from core.editor_exec import exec_tool

    text = str((step or {}).get("widget") or (step or {}).get("query") or "")
    ref = _fps_hud_ref(text)
    read = {"allow_writes": [], "save": False, "pie": False}
    problems: list[str] = []

    called = exec_tool(
        "UMGToolSet.UMGToolSet", "GetWidgets",
        {"widgetBlueprint": {"refPath": ref}}, task=read,
    )
    found: list[str] = []
    if not called.get("ok"):
        problems.append("GetWidgets 失败：" + str(called.get("error") or "没有返回"))
    else:
        found = _fps_hud_widget_names(called.get("data"))
        missing = [name for name in FPS_HUD_WIDGETS if name not in found]
        for name in missing:
            problems.append(f"缺控件 {name}")

    parent_ok = False
    parent_name = ""
    parent = exec_tool(
        "editor_toolset.toolsets.blueprint.BlueprintTools", "get_parent",
        {"blueprint": {"refPath": ref}}, task=read,
    )
    data = parent.get("data")
    parent_ref = ""
    if isinstance(data, dict) and isinstance(data.get("returnValue"), dict):
        parent_ref = str(data["returnValue"].get("refPath") or "")
    if parent_ref:
        parent_name = parent_ref.rsplit(".", 1)[-1].rsplit("/", 1)[-1]
        parent_ok = parent_name == FPS_HUD_PARENT
    if parent.get("ok") and parent_ref and not parent_ok:
        problems.append(f"父类是 {parent_name}，不是 {FPS_HUD_PARENT}")
    elif not parent_ref and parent.get("ok"):
        problems.append(f"读不到 {ref} 的父类")

    mounted, mount_note = _fps_hud_mount_entry()
    if not mounted:
        problems.append(mount_note)

    if problems:
        summary = f"{ref} 不合格：" + "；".join(problems) + "。控件图能编过，不等于 HUD 会动。"
    else:
        summary = (
            f"{ref} 六个合同控件都在，父类是 {FPS_HUD_PARENT}，{mount_note}；"
            "血条占位 100、子弹/手雷数量、载具名字/上下车提示 E/时速都由该控件显示。"
        )
    judge = {"ok": not problems, "kind": "editor_assert", "summary": summary, "errors": []}
    out = {
        "ok": not problems,
        "id": ref,
        "summary": summary,
        "index_note": summary,
        "test": judge,
        "judge_kind": "editor_assert",
        "ref": ref,
        "in_level": False,
        "pie": False,
        "save": False,
        "widgets_found": found,
    }
    if problems:
        out["error"] = summary
    return out


def _apply_spec(step: dict) -> dict:
    """拆分结果里的新 C++ 文件。路径已经由 decompose.safe_files 收过，这里再收一次再写。

    写完强制编译（P0-3 编译回灌）：失败时把 get_build_error 的错误原文当 fix 上下文
    进下一轮，编译轮次 ≤3；烧尽还没过就带着原文失败，不假成功。"""
    from agent.build_error import apply_spec_loop
    from agent.decompose import safe_files

    spec = step.get("spec") if isinstance(step.get("spec"), dict) else {}
    files, error = safe_files(spec.get("files"))
    if error:
        return {"ok": False, "error": error, "summary": error, "index_note": error}
    return apply_spec_loop(files, wid=str(step.get("id") or ""))


_FPS_PAWN_CLASS = "/Script/AIAgentProject.FpsPawn"
_FPS_TARGET_CLASS = "/Script/AIAgentProject.FpsTarget"
_FPS_GAMEMODE = "/Script/AIAgentProject.FpsGameMode"
_FPS_FLOOR_MESH = "/Engine/BasicShapes/Cube"
# 相机在胶囊中心上方 64uu。Pawn 放在 z=96 时眼睛高度是 160，靶心对齐这条水平射线。
_FPS_TARGET_POINTS = ([400, 0, 160], [400, 250, 160], [700, 0, 160])


def _fps_fail(level: str, text: str) -> dict:
    judge = {
        "ok": False,
        "kind": "editor_assert",
        "summary": text,
        "errors": [{"file": "", "line": 0, "message": text}],
    }
    return {
        "ok": False,
        "id": level,
        "error": text,
        "summary": text,
        "index_note": text,
        "test": judge,
        "judge_kind": "editor_assert",
        "in_level": False,
        "pie": False,
        "save": False,
    }


def _fps_stats(result: dict) -> dict:
    """get_scene_statistics 的类计数。没有 actor_count 就当读失败，不把空字典当成「关卡是空的」。"""
    data = _monolith_payload(result)
    if "actor_count" not in data and isinstance(data.get("returnValue"), dict):
        data = data["returnValue"]
    raw = data.get("actor_count")
    if not isinstance(raw, dict):
        return {}
    counts: dict[str, int] = {}
    for key, value in raw.items():
        try:
            counts[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return counts


def _fps_count(counts: dict, *names: str) -> int:
    return sum(int(counts.get(name, 0) or 0) for name in names)


def _fps_ready(counts: dict) -> bool:
    return (
        _fps_count(counts, "FpsPawn", "AFpsPawn") >= 1
        and _fps_count(counts, "FpsTarget", "AFpsTarget") >= 1
        and _fps_count(counts, "StaticMeshActor") >= 1
        and _fps_count(counts, "DirectionalLight") >= 1
    )


def _fps_needs(counts: dict) -> dict:
    return {
        "pawn": _fps_count(counts, "FpsPawn", "AFpsPawn") < 1,
        "target": _fps_count(counts, "FpsTarget", "AFpsTarget") < 1,
        "floor": _fps_count(counts, "StaticMeshActor") < 1,
        "light": _fps_count(counts, "DirectionalLight") < 1,
        "start": _fps_count(counts, "PlayerStart") < 1,
    }


def _fps_count_text(counts: dict) -> str:
    parts = (
        ("FpsPawn", _fps_count(counts, "FpsPawn", "AFpsPawn")),
        ("FpsTarget", _fps_count(counts, "FpsTarget", "AFpsTarget")),
        ("地面", _fps_count(counts, "StaticMeshActor")),
        ("灯", _fps_count(counts, "DirectionalLight")),
    )
    return "、".join(f"{label}={count}" for label, count in parts)


def _fps_actor_specs(needs: dict) -> list[dict]:
    actors: list[dict] = []
    if needs.get("light"):
        actors.append({
            "class": "/Script/Engine.DirectionalLight",
            "location": [0, 0, 400],
            "rotation": [-50, 30, 0],
            "folder": "FPS",
        })
    if needs.get("start"):
        actors.append({
            "class": "/Script/Engine.PlayerStart",
            "location": [0, 0, 96],
            "rotation": [0, 0, 0],
            "folder": "FPS",
        })
    if needs.get("pawn"):
        actors.append({
            "class": _FPS_PAWN_CLASS,
            "location": [0, 0, 96],
            "rotation": [0, 0, 0],
            "folder": "FPS",
        })
    if needs.get("target"):
        for point in _FPS_TARGET_POINTS:
            actors.append({
                "class": _FPS_TARGET_CLASS,
                "location": list(point),
                "rotation": [0, 0, 0],
                "folder": "FPS",
            })
    return actors


def _fps_spawn_notes(payload: dict) -> str:
    rows = payload.get("actor_instances") if isinstance(payload, dict) else None
    bits = []
    for row in rows or []:
        if isinstance(row, dict) and row.get("spawned") is False:
            bits.append(f"{row.get('class')}: {row.get('error') or '没生成'}")
    return "；".join(bits)


def _implement_shooter(step: dict) -> dict:
    """把简易射击放进唯一关卡。AIAgent.Fps.Hit 在临时世界里测命中，不能单独当成关卡已做好。"""
    from config import UE_SOURCE_ROOT
    from agent.plan_windows import resolve_existing_level
    from backends.monolith import exec as monolith_exec
    from backends.select import current_backend

    missing = [rel for rel in _FPS_SOURCES if not (UE_SOURCE_ROOT / rel).is_file()]
    if missing:
        text = "射击玩法源文件不在：" + "、".join(missing)
        return {"ok": False, "error": text, "summary": text, "index_note": text}
    slots = step.get("slots") if isinstance(step.get("slots"), dict) else {}
    raw = (
        step.get("level_path") or step.get("level_name")
        or slots.get("level") or step.get("level") or ""
    )
    found = resolve_existing_level(raw)
    if not found.get("ok"):
        text = str(found.get("error") or "缺少关卡名，不猜。")
        return {"ok": False, "error": text, "summary": text, "index_note": text}
    level = str(found.get("level_name") or "")
    package = str(found.get("level_path") or "")
    if current_backend() != "monolith":
        return _fps_fail(level, "当前后端不是 monolith，不能核对关卡里有没有射击内容。不把临时世界测试当成完成。")
    if not task_flag(step, "save"):
        return _fps_fail(level, "save 未放行，不能加载关卡核对场景。不把临时世界测试当成完成。")

    write = {"allow_writes": [], **task_policy(save=True, pie=False)}

    def call(namespace: str, action: str, params: dict) -> dict:
        return monolith_exec(namespace, action, params, task=write, transport=_monolith_transport)

    loaded = call("editor", "load_level", {"path": package, "dirty_policy": "refuse"})
    if not loaded.get("ok"):
        return _fps_fail(level, f"关卡 {package} 加载失败：{_error_text(loaded) or '没有返回'}")
    loaded_data = _monolith_payload(loaded)
    if _as_bool(loaded_data.get("loaded")) is not True:
        return _fps_fail(
            level,
            f"关卡 {package} 加载失败：{loaded_data.get('message') or _error_text(loaded) or '没有返回'}",
        )
    world = str(loaded_data.get("current_world") or loaded_data.get("path") or "")
    if world and not _level_matches(world, package, level):
        return _fps_fail(level, f"加载后当前关卡是 {world}，不是 {package}。")

    counted = call("mesh", "get_scene_statistics", {})
    if not counted.get("ok"):
        return _fps_fail(level, f"读 {level} 的场景统计失败：{_error_text(counted) or '没有返回'}")
    counts = _fps_stats(counted)
    if not counts:
        return _fps_fail(level, f"读 {level} 的场景统计失败：没有按类给出的 actor 计数。")

    needs = _fps_needs(counts)
    if any(needs.values()):
        if needs["floor"]:
            floor = call("mesh", "spawn_actor", {
                "class_or_mesh": _FPS_FLOOR_MESH,
                "location": [0, 0, -50],
                "scale": [40, 40, 1],
                "name": "FpsFloor",
                "folder": "FPS",
            })
            if not floor.get("ok"):
                return _fps_fail(level, f"往 {level} 放地面失败：{_error_text(floor) or '没有返回'}")
        notes = ""
        specs = _fps_actor_specs(needs)
        if specs:
            authored = call("editor", "author_map_settings", {"actors": specs})
            if not authored.get("ok"):
                return _fps_fail(level, f"往 {level} 放射击内容失败：{_error_text(authored) or '没有返回'}")
            notes = _fps_spawn_notes(_monolith_payload(authored))
        saved = call("editor", "save_packages", {"packages": [package]})
        if not saved.get("ok"):
            return _fps_fail(level, f"关卡 {package} 保存失败：{_error_text(saved) or '没有返回'}")
        saved_data = _monolith_payload(saved)
        if _as_bool(saved_data.get("ok")) is not True:
            return _fps_fail(level, f"关卡 {package} 保存失败：{saved_data.get('message') or '没有写上'}")
        counted = call("mesh", "get_scene_statistics", {})
        if not counted.get("ok"):
            return _fps_fail(level, f"放完后读 {level} 的场景统计失败：{_error_text(counted) or '没有返回'}")
        counts = _fps_stats(counted)
        if notes or not _fps_ready(counts):
            shown = _fps_count_text(counts)
            detail = f"{shown}；{notes}" if notes else shown
            return _fps_fail(
                level,
                f"{level} 放完后仍缺射击内容（{detail}）。"
                "AIAgent.Fps.Hit 只在临时世界里测命中，不能当成关卡已做好。",
            )

    if not _fps_ready(counts):
        return _fps_fail(
            level,
            f"{level} 里没有射击内容（{_fps_count_text(counts)}）。"
            "AIAgent.Fps.Hit 只在临时世界里测命中，不能当成关卡已做好。",
        )

    mode = call("editor", "author_map_settings", {
        "game_mode_override": _FPS_GAMEMODE,
        "save": True,
    })
    if not mode.get("ok"):
        return _fps_fail(level, f"设置 {level} 的 GameMode 失败：{_error_text(mode) or '没有返回'}")
    mode_data = _monolith_payload(mode)
    if _as_bool(mode_data.get("game_mode_override_set")) is not True:
        return _fps_fail(
            level,
            f"{level} 的 GameMode 没有指到 FpsGameMode："
            f"{mode_data.get('game_mode_override_error') or '没有设上'}。"
            "开始游戏仍会用项目默认模式，左键打不了这关的靶。",
        )
    if _as_bool(mode_data.get("saved")) is not True:
        return _fps_fail(level, f"关卡 {package} 保存 GameMode 失败：{mode_data.get('save_error') or '没有写上'}")

    result = _run_automation({
        "slots": {"filter": _FPS_FILTER},
        "automation_filter": _FPS_FILTER,
    })
    note = f"{level} 关卡内 {_fps_count_text(counts)}，GameMode=FpsGameMode；{result.get('summary') or _FPS_FILTER}"
    result["summary"] = note
    result["index_note"] = note
    result["id"] = level
    result["level_path"] = package
    result["in_level"] = True
    # 关卡内容核对单独落一条 editor_assert 判词：L1 合同常把「关卡里有 Pawn/靶/地面」
    # 写成 editor_assert 条款绑到本扇，automation 判词勾不动它（20261028-151850 的教训）。
    result["build"] = {
        "ok": True, "kind": "editor_assert",
        "summary": f"{level} 关卡内容核对：{_fps_count_text(counts)}，GameMode=FpsGameMode",
        "errors": [],
    }
    if not result.get("ok"):
        result["error"] = note
    return result


_FPS_GRENADE_FILTER = "AIAgent.Fps.Grenade"
_FPS_GRENADE_CLASS = "/Script/AIAgentProject.FpsGrenadePickup"
# 拾取物点位：地面顶在 z=0（地面立方体 scale 40 在 z=-50），球体缩 0.3 半径 15，坐地上
_FPS_GRENADE_POINTS = ([300, 100, 20], [-200, 150, 20])
_FPS_GRENADE_WANT = 2


def _implement_grenade(step: dict) -> dict:
    """把手榴弹拾取物放进唯一关卡并收口 AIAgent.Fps.Grenade。
    前置是这一关已经有 FPS 玩法（FpsPawn/靶/地面/灯）：缺了引导先跑 implement_shooter，不算过。"""
    from config import UE_SOURCE_ROOT
    from agent.plan_windows import resolve_existing_level
    from backends.monolith import exec as monolith_exec
    from backends.select import current_backend

    sources = _FPS_SOURCES + ("Public/FpsGrenadeProjectile.h", "Public/FpsGrenadePickup.h")
    missing = [rel for rel in sources if not (UE_SOURCE_ROOT / rel).is_file()]
    if missing:
        text = "手榴弹源文件不在：" + "、".join(missing)
        return {"ok": False, "error": text, "summary": text, "index_note": text}
    slots = step.get("slots") if isinstance(step.get("slots"), dict) else {}
    raw = (
        step.get("level_path") or step.get("level_name")
        or slots.get("level") or step.get("level") or ""
    )
    found = resolve_existing_level(raw)
    if not found.get("ok"):
        text = str(found.get("error") or "缺少关卡名，不猜。")
        return {"ok": False, "error": text, "summary": text, "index_note": text}
    level = str(found.get("level_name") or "")
    package = str(found.get("level_path") or "")
    if current_backend() != "monolith":
        return _fps_fail(level, "当前后端不是 monolith，不能核对关卡里有没有手榴弹前置。不把临时世界测试当成完成。")
    if not task_flag(step, "save"):
        return _fps_fail(level, "save 未放行，不能加载关卡核对场景。不把临时世界测试当成完成。")

    write = {"allow_writes": [], **task_policy(save=True, pie=False)}

    def call(namespace: str, action: str, params: dict) -> dict:
        return monolith_exec(namespace, action, params, task=write, transport=_monolith_transport)

    loaded = call("editor", "load_level", {"path": package, "dirty_policy": "refuse"})
    if not loaded.get("ok"):
        return _fps_fail(level, f"关卡 {package} 加载失败：{_error_text(loaded) or '没有返回'}")
    loaded_data = _monolith_payload(loaded)
    if _as_bool(loaded_data.get("loaded")) is not True:
        return _fps_fail(
            level,
            f"关卡 {package} 加载失败：{loaded_data.get('message') or _error_text(loaded) or '没有返回'}",
        )
    world = str(loaded_data.get("current_world") or loaded_data.get("path") or "")
    if world and not _level_matches(world, package, level):
        return _fps_fail(level, f"加载后当前关卡是 {world}，不是 {package}。")

    counted = call("mesh", "get_scene_statistics", {})
    if not counted.get("ok"):
        return _fps_fail(level, f"读 {level} 的场景统计失败：{_error_text(counted) or '没有返回'}")
    counts = _fps_stats(counted)
    if not counts:
        return _fps_fail(level, f"读 {level} 的场景统计失败：没有按类给出的 actor 计数。")

    if not _fps_ready(counts):
        return _fps_fail(
            level,
            f"{level} 里没有 FPS 前置（{_fps_count_text(counts)}）。"
            "手榴弹挂在射击玩法上，先跑 implement_shooter 把这一关布置好，再来加手榴弹。",
        )

    notes = ""
    have = _fps_count(counts, "FpsGrenadePickup", "AFpsGrenadePickup")
    if have < _FPS_GRENADE_WANT:
        actors = [{
            "class": _FPS_GRENADE_CLASS,
            "location": list(point),
            "rotation": [0, 0, 0],
            "folder": "FPS",
        } for point in _FPS_GRENADE_POINTS[have:_FPS_GRENADE_WANT]]
        authored = call("editor", "author_map_settings", {"actors": actors})
        if not authored.get("ok"):
            return _fps_fail(level, f"往 {level} 放手榴弹拾取物失败：{_error_text(authored) or '没有返回'}")
        notes = _fps_spawn_notes(_monolith_payload(authored))
        saved = call("editor", "save_packages", {"packages": [package]})
        if not saved.get("ok"):
            return _fps_fail(level, f"关卡 {package} 保存失败：{_error_text(saved) or '没有返回'}")
        saved_data = _monolith_payload(saved)
        if _as_bool(saved_data.get("ok")) is not True:
            return _fps_fail(level, f"关卡 {package} 保存失败：{saved_data.get('message') or '没有写上'}")
        counted = call("mesh", "get_scene_statistics", {})
        if not counted.get("ok"):
            return _fps_fail(level, f"放完后读 {level} 的场景统计失败：{_error_text(counted) or '没有返回'}")
        counts = _fps_stats(counted)

    have = _fps_count(counts, "FpsGrenadePickup", "AFpsGrenadePickup")
    if have < _FPS_GRENADE_WANT or notes:
        shown = f"{_fps_count_text(counts)}，FpsGrenadePickup={have}"
        detail = f"{shown}；{notes}" if notes else shown
        return _fps_fail(
            level,
            f"{level} 放完后拾取物仍不足（{detail}）。"
            "AIAgent.Fps.Grenade 只在临时世界里测爆炸链路，不能当成关卡已做好。",
        )

    result = _run_automation({
        "slots": {"filter": _FPS_GRENADE_FILTER},
        "automation_filter": _FPS_GRENADE_FILTER,
    })
    note = f"{level} 关卡内 {_fps_count_text(counts)}，FpsGrenadePickup={have}；{result.get('summary') or _FPS_GRENADE_FILTER}"
    result["summary"] = note
    result["index_note"] = note
    result["id"] = level
    result["level_path"] = package
    result["in_level"] = True
    # 关卡内容核对单独落一条 editor_assert 判词（与 _implement_shooter 同口径）。
    result["build"] = {
        "ok": True, "kind": "editor_assert",
        "summary": f"{level} 关卡内容核对：{_fps_count_text(counts)}，FpsGrenadePickup={have}",
        "errors": [],
    }
    if not result.get("ok"):
        result["error"] = note
    return result


def _run_automation(step: dict) -> dict:
    """契约里的 Automation。不另开 PIE。"""
    from core.contract_tools import run_automation_test

    slots = step.get("slots") if isinstance(step.get("slots"), dict) else {}
    filt = str(step.get("automation_filter") or slots.get("filter") or "")
    result = run_automation_test(filt)
    summary = str(result.get("summary") or result.get("error") or "")
    judge = {
        "ok": bool(result.get("ok")),
        "kind": "automation",
        "summary": summary,
        "errors": list(result.get("errors") or []),
    }
    if isinstance(result.get("tests"), list):
        judge["tests"] = list(result["tests"])
    if result.get("filter"):
        judge["filter"] = str(result["filter"])
    out = {
        "ok": bool(result.get("ok")),
        "id": filt,
        "summary": summary,
        "index_note": summary,
        "test": judge,
        "judge_kind": "automation",
        "pie": False,
        "save": False,
    }
    if not out["ok"]:
        out["error"] = summary or "Automation 没过"
    return out


_MATERIAL_INSTANCE = "editor_toolset.toolsets.material_instance.MaterialInstanceTools"


def _asset_ref_by_name(name: str, asset_class: str) -> str:
    """资产索引里唯一命中才返回 object_path，零个或多个返回空字符串，不猜。"""
    from config import ASSET_JSONL

    if not ASSET_JSONL.is_file():
        return ""
    needle = f'"asset_name": "{name}"'
    cls = f'"asset_class": "{asset_class}"'
    refs: set[str] = set()
    for line in ASSET_JSONL.read_text(encoding="utf-8", errors="replace").splitlines():
        if needle in line and cls in line:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ref = str((rec.get("metadata") or {}).get("object_path") or "")
            if ref:
                refs.add(ref)
    if len(refs) == 1:
        return next(iter(refs))
    return ""


def _disk_blueprint_ref(name: str) -> str:
    """资产索引落后时的兜底：Content 下唯一一个 <name>.uasset → /Game/... 路径。
    20261021：BP_Coin/BP_Platform/BP_Door/BP_Progress 已存盘但不在 ue_assets.jsonl 里，
    只认索引会让 place_batch 直接失败。零个或多个仍然返回空，不猜。"""
    from config import UE_PROJECT_DIR

    content = UE_PROJECT_DIR / "Content"
    if not name or not content.is_dir():
        return ""
    found = [path for path in content.rglob(name + ".uasset") if path.is_file()]
    if len(found) != 1:
        return ""
    rel = found[0].relative_to(content).with_suffix("").as_posix()
    return f"/Game/{rel}.{name}"


def _material_instance_ref(target: str) -> str:
    """/Game 或 /Engine 路径直接归一成 object_path；名字查材质实例索引唯一命中。
    资产索引里材质实例的类名是 MaterialInstanceConstant（不是 MaterialInstance），两个都认。"""
    text = str(target or "").strip().replace("\\", "/").rstrip("/").rstrip(".")
    if not text:
        return ""
    if text.startswith("/Game/") or text.startswith("/Engine/"):
        leaf = text.rsplit("/", 1)[-1]
        if "." in leaf:
            return text
        return f"{text}.{leaf}"
    for asset_class in ("MaterialInstanceConstant", "MaterialInstance"):
        found = _asset_ref_by_name(text, asset_class)
        if found:
            return found
    return ""


def _blueprint_ref(target: str) -> str:
    """/Game 路径归一；蓝图名查索引唯一命中，索引落后时回落到 Content 磁盘。"""
    text = str(target or "").strip().replace("\\", "/").rstrip("/").rstrip(".")
    if not text:
        return ""
    if text.startswith("/Game/"):
        leaf = text.rsplit("/", 1)[-1]
        if "." in leaf:
            return text
        return f"{text}.{leaf}"
    return _asset_ref_by_name(text, "Blueprint") or _disk_blueprint_ref(text)


def _scalar_value(data) -> float | None:
    """get_scalar_parameter 的读数： returnValue / value / 裸数字，读不出算失败。"""
    value = data
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("returnValue", "value", "scalar", "Value"):
            inner = value.get(key)
            if isinstance(inner, bool):
                continue
            if isinstance(inner, (int, float)):
                return float(inner)
    if isinstance(value, str):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _set_scalar_param(step: dict) -> dict:
    """覆写材质实例的一个标量参数。材质域写闸：UE_ALLOW_MATERIAL 人审门挡在 editor_exec，
    allow_writes 只放 set_scalar_parameter。写完复读；blueprint 槽位给了蓝图就再编译它，
    裁判 blueprint_compile。不改材质图；save 开时保存这份材质实例。"""
    from core.editor_exec import exec_tool
    from core.judge import judge

    read = _read_task()
    slots = step.get("slots") if isinstance(step.get("slots"), dict) else {}
    target = str(slots.get("target") or "").strip()
    param = str(slots.get("param") or "").strip()
    raw_value = str(slots.get("value") or "").strip()
    blueprint = str(slots.get("blueprint") or "").strip()
    if not target:
        return {"ok": False, "error": "没有要覆写的材质实例"}
    if not param:
        return {"ok": False, "id": target, "error": "没有参数名，不猜。"}
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return {"ok": False, "id": target, "error": "value 不是唯一的数字，不猜。"}
    instance_ref = _material_instance_ref(target)
    if not instance_ref:
        return {"ok": False, "id": target, "error": f"{target} 没有唯一的材质实例路径，不猜。"}
    instance = {"refPath": instance_ref}
    # U4：覆写前把旧值记进 props.json，这一扇失败且没有 keep_source 时写回。
    # 参数读不出旧值（从未设置过）就不记——没有可信的「之前」，不编造。
    if step.get("run_id"):
        old = exec_tool(
            _MATERIAL_INSTANCE,
            "get_scalar_parameter",
            {"instance": instance, "name": param},
            task=_read_task(),
        )
        old_value = _scalar_value(old.get("data")) if old.get("ok") else None
        if old_value is not None:
            from agent.prop_snapshot import capture_props

            capture_props(str(step.get("run_id")), instance_ref,
                          {"parameter": param, "value": old_value}, kind="scalar")
    write = {"allow_writes": ["set_scalar_parameter"], **task_policy()}
    wrote = exec_tool(
        _MATERIAL_INSTANCE,
        "set_scalar_parameter",
        {"instance": instance, "name": param, "value": value},
        task=write,
    )
    if not wrote.get("ok"):
        err = str(wrote.get("error") or "覆写失败")
        if _conn_error(err):
            err = f"编辑器 MCP 连不上：{err}"
        out = {"ok": False, "id": instance_ref, "ref": instance_ref, "error": err}
        if wrote.get("policy"):
            out["policy"] = wrote["policy"]
        return out
    got = exec_tool(
        _MATERIAL_INSTANCE,
        "get_scalar_parameter",
        {"instance": instance, "name": param},
        task=read,
    )
    if not got.get("ok"):
        return {"ok": False, "id": instance_ref, "ref": instance_ref,
                "error": f"复读失败：{got.get('error') or '读不到参数'}"}
    current = _scalar_value(got.get("data"))
    if current is None or abs(current - value) > 1e-3:
        shown = current if current is not None else "未读到"
        return {"ok": False, "id": instance_ref, "ref": instance_ref,
                "error": f"复读未通过：{param}={shown}，没有到 {value:g}"}
    note = f"{instance_ref.rsplit('/', 1)[-1].split('.')[0]}.{param}={value:g} 已覆写并复读一致"
    saved_note = ""
    if task_flag(step, "save"):
        package = instance_ref.split(".", 1)[0]
        saved = exec_tool(
            _ASSET_TOOLS, "save_assets",
            {"asset_paths": [package]},
            task={"allow_writes": [], **task_policy(save=True, pie=False)},
        )
        if not saved.get("ok"):
            return {"ok": False, "id": instance_ref, "ref": instance_ref,
                    "error": f"材质实例保存失败：{saved.get('error') or '未存上'}"}
        saved_note = "，已保存这份材质实例"
    out = {"ok": True, "id": target, "ref": instance_ref, "in_level": False, "index_note": note + saved_note}
    if blueprint:
        bp_ref = _blueprint_ref(blueprint)
        if not bp_ref:
            return {"ok": False, "id": target, "ref": instance_ref,
                    "error": f"蓝图 {blueprint} 没有唯一的路径，不猜。"}
        test = judge({"kind": "blueprint_compile", "refPath": bp_ref}, read)
        out["test"] = test
        out["judge_kind"] = "blueprint_compile"
        if test.get("ok"):
            out["index_note"] = note + f"；{bp_ref.rsplit('/', 1)[-1].split('.')[0]} 编译通过"
        else:
            out["ok"] = False
            out["error"] = str(test.get("summary") or "蓝图编译失败")
    return out


_ASSET_TOOLS = "editor_toolset.toolsets.asset.AssetTools"
_LAYOUT_DIR_PARTS = ("data", "harness", "level_layout")


def _layout_file(layout: str) -> Path:
    """设计图：绝对/相对路径，或 level_layout 目录下的文件名（补 .json）。找不到返回 None。"""
    from config import DATA_DIR

    text = str(layout or "").strip()
    if not text:
        return None
    candidates = []
    direct = Path(text)
    candidates.append(direct)
    if not direct.is_absolute():
        if not text.lower().endswith(".json"):
            text += ".json"
        candidates.append(DATA_DIR / "harness" / "level_layout" / text)
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _layout_entries(data) -> list:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return []
    if not isinstance(data, dict):
        return []
    rows = data.get("actors")
    if not isinstance(rows, list):
        return []
    out = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        loc = row.get("location") if isinstance(row.get("location"), dict) else row
        try:
            x = float(loc.get("x"))
            y = float(loc.get("y"))
            z = float(loc.get("z"))
        except (TypeError, ValueError):
            continue
        name = str(row.get("name") or "").strip()
        out.append({"name": name, "x": x, "y": y, "z": z, "props": _entry_props(row.get("props"))})
    return out


_MAX_ENTRY_PROPS = 8


def _entry_props(raw) -> dict:
    """设计图条目上的 props：放完之后写一次并复读。只收标量，最多 8 个。
    前段平台 `bStartActive: false` 就是靠这一条表达「不动」。"""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key, value in list(raw.items())[:_MAX_ENTRY_PROPS]:
        if isinstance(value, (bool, int, float, str)):
            out[str(key)] = value
    return out


def _props_match(got, want) -> bool:
    if isinstance(want, bool):
        flag = _as_bool(got)
        return flag is want
    if isinstance(want, (int, float)):
        try:
            return abs(float(got) - float(want)) <= 1e-3
        except (TypeError, ValueError):
            return False
    return str(got) == str(want)


def _write_entry_props(ref: str, props: dict, write: dict) -> dict:
    """写一条实例属性并复读。读不回或读回不等于给定值都算失败，不静默。"""
    from core.editor_exec import exec_tool

    wrote = exec_tool(
        _OBJECT,
        "set_properties",
        {"instance": {"refPath": ref}, "values": json.dumps(props, ensure_ascii=False)},
        task=write,
    )
    if not wrote.get("ok"):
        return {"ok": False, "error": str(wrote.get("error") or "写属性失败")}
    got = exec_tool(
        _OBJECT,
        "get_properties",
        {"instance": {"refPath": ref}, "properties": list(props)},
        task=_read_task(),
    )
    values = _parse_props(got.get("data")) if got.get("ok") else None
    if not isinstance(values, dict):
        return {"ok": False, "error": "写属性后复读失败"}
    bad = [key for key, want in props.items() if not _props_match(values.get(key), want)]
    if bad:
        return {"ok": False, "error": "复读不等于给定值：" + "、".join(bad)}
    return {"ok": True}


def _sync_entry_location(ref: str, entry: dict, write: dict) -> dict:
    """设计图改了坐标时，把已存在的实例挪过去。误差 1uu 以内算一致，不动。
    幂等：同坐标重跑不写。"""
    from core.editor_exec import exec_tool

    now = _read_location(ref, _read_task())
    if not now.get("ok"):
        return now
    x, y, z = now["location"]
    if abs(x - entry["x"]) <= 1.0 and abs(y - entry["y"]) <= 1.0 and abs(z - entry["z"]) <= 1.0:
        return {"ok": True, "moved": False}
    moved = exec_tool(
        _ACTOR,
        "set_actor_transform",
        {"actor": {"refPath": ref},
         "xform": {"location": {"x": entry["x"], "y": entry["y"], "z": entry["z"]}}},
        task=write,
    )
    if not moved.get("ok"):
        return {"ok": False, "error": str(moved.get("error") or "挪位置失败")}
    return {"ok": True, "moved": True}


def _place_batch(step: dict) -> dict:
    """按设计图 JSON 批量放置同一蓝图。幂等：条目点名已存在的实例就跳过，不重复放。
    放完逐条复查唯一性。save 开时对当前关卡存盘一次（缺省开）。"""
    from core.editor_exec import exec_tool

    read = _read_task()
    slots = step.get("slots") if isinstance(step.get("slots"), dict) else {}
    blueprint = str(slots.get("blueprint") or "").strip()
    layout = str(slots.get("layout") or "").strip()
    if not blueprint or not layout:
        return {"ok": False, "error": "没有蓝图或设计图，不猜。"}
    bp_ref = _blueprint_ref(blueprint)
    if not bp_ref:
        return {"ok": False, "id": blueprint, "error": f"{blueprint} 没有唯一的蓝图路径，不猜。"}
    asset_path = bp_ref.rsplit(".", 1)[0]
    file = _layout_file(layout)
    if file is None:
        return {"ok": False, "id": blueprint, "error": f"找不到设计图 {layout}，不放。"}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "id": blueprint, "error": f"设计图读不动：{exc}"}
    entries = _layout_entries(data)
    if not entries:
        return {"ok": False, "id": blueprint, "error": "设计图里没有合法条目，不放。"}
    base_name = asset_path.rsplit("/", 1)[-1]
    write = {"allow_writes": ["add_to_scene_from_asset", "set_properties", "set_actor_transform"],
             **task_policy(save=task_flag(step, "save"), pie=False)}
    placed = skipped = corrected = 0
    created: list[str] = []
    props_at_spawn: dict[str, bool] = {}

    def _mark_created_for_rollback() -> None:
        """U4：这扇已经放进场景的实例，失败时登记进 props.json（kind=created），
        由失败还原路径逐个删掉，场景回到放置前。没跑过 run_id（离线/评测）就不记。"""
        if not created or not step.get("run_id"):
            return
        from agent.prop_snapshot import capture_props

        for name in created:
            capture_props(str(step.get("run_id")), name, {}, kind="created")

    for index, entry in enumerate(entries):
        name = entry["name"] or f"{base_name}_{index + 1}"
        found = exec_tool(
            _SCENE, "find_actors",
            {"name": name, "tag": "", "collision_channels": []},
            task=read,
        )
        existing = _actor_refs(found.get("data")) if found.get("ok") else []
        if existing:
            # 已存在：按设计图校正坐标（设计图改了就该生效），一致则记跳过
            sync = _sync_entry_location(existing[0], entry, write)
            if not sync.get("ok"):
                _mark_created_for_rollback()
                return {"ok": False, "id": blueprint,
                        "error": f"{name} 坐标校正失败：{sync.get('error')}"}
            if sync.get("moved"):
                corrected += 1
            else:
                skipped += 1
            continue
        spawn_extra: dict = {}
        props = entry.get("props") or {}
        if props and _monolith_active():
            # U3a：monolith 没有实例属性写入口，属性只能在生成时一起写（spawn_blueprint_actor
            # 的 properties）。official 路径不传这个键，避免官方 schema 拒绝未知参数。
            spawn_extra = {"properties": props}
        placed_actor = exec_tool(
            _SCENE, "add_to_scene_from_asset",
            {
                "asset_path": asset_path,
                "name": name,
                "xform": {"location": {"x": entry["x"], "y": entry["y"], "z": entry["z"]}},
                "snap_to_ground": False,
                **spawn_extra,
            },
            task=write,
        )
        if placed_actor.get("ok"):
            if spawn_extra:
                skipped = (placed_actor.get("data") if isinstance(placed_actor.get("data"), dict) else {}).get("properties_skipped")
                if isinstance(skipped, list) and skipped:
                    _mark_created_for_rollback()
                    names = "、".join(str(one) for one in skipped[:6])
                    return {"ok": False, "id": blueprint,
                            "error": f"{name} 生成时属性没全写上（{names}），不算放好"}
                props_at_spawn[name] = True
            placed += 1
            created.append(name)
        else:
            err = str(placed_actor.get("error") or "放置失败")
            if _conn_error(err):
                _mark_created_for_rollback()
                return {"ok": False, "id": blueprint, "error": f"编辑器 MCP 连不上：{err}"}
            _mark_created_for_rollback()
            return {"ok": False, "id": blueprint, "error": f"{name} 放置失败：{err}"}
    # 复查：每个条目在关卡里唯一存在；带 props 的条目顺手写属性并复读
    written = 0
    for index, entry in enumerate(entries):
        name = entry["name"] or f"{base_name}_{index + 1}"
        found = exec_tool(
            _SCENE, "find_actors",
            {"name": name, "tag": "", "collision_channels": []},
            task=read,
        )
        refs = _actor_refs(found.get("data")) if found.get("ok") else []
        if len(refs) != 1:
            _mark_created_for_rollback()
            return {"ok": False, "id": blueprint,
                    "error": f"复查未通过：{name} 命中 {len(refs)} 个（要 1 个）"}
        props = entry.get("props") or {}
        if not props:
            continue
        if _monolith_active():
            # U3a：monolith 属性只在生成时写。已有实例带 props 的幂等复读写不上，
            # 诚实失败——不把「跳过属性」记成写好。
            if not props_at_spawn.get(name):
                return {"ok": False, "id": blueprint,
                        "error": (f"{name} 带属性但已是场景里的旧实例：monolith 只能在生成时写属性，"
                                  "这条复读不了，不猜。")}
            written += 1
            continue
        wrote = _write_entry_props(refs[0], props, write)
        if not wrote.get("ok"):
            _mark_created_for_rollback()
            return {"ok": False, "id": blueprint,
                    "error": f"{name} 属性没写上：{wrote.get('error')}"}
        written += 1
    note = f"{base_name} 按设计图 {file.name} 复核 {len(entries)} 条唯一（放置 {placed}，幂等跳过 {skipped}）"
    if corrected:
        note += f"，按设计图校正坐标 {corrected} 条"
    if written:
        note += f"，写实例属性 {written} 条"
    saved = _save_level(step)
    if not saved.get("ok"):
        _mark_created_for_rollback()
        return {"ok": False, "id": blueprint, "saved": False,
                "error": f"{saved.get('error')}（放置与复查已完成：{note}）"}
    save_note = f"；{saved.get('note')}" if saved.get("note") else ""
    summary = note + save_note
    return {
        "ok": True,
        "id": base_name,
        "ref": asset_path,
        "in_level": True,
        "index_note": summary,
        "test": {"ok": True, "kind": "editor_assert", "summary": summary, "errors": []},
        "judge_kind": "editor_assert",
    }


_REMOVE_TOLERANCE = 2.0
_MAX_REMOVE_TARGETS = 32


def _ref_path_of(item) -> str:
    """命中行或裸字符串统一成 refPath。"""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("refPath") or item.get("ref") or "").strip()
    return ""


def _coords_of(item) -> tuple[float, float, float] | None:
    """条目或命中行上的坐标。location 字典和顶层 x/y/z 两种写法都认，读不出返回 None。"""
    if not isinstance(item, dict):
        return None
    loc = item.get("location") if isinstance(item.get("location"), dict) else item
    try:
        return float(loc["x"]), float(loc["y"]), float(loc["z"])
    except (KeyError, TypeError, ValueError):
        return None


def pick_removal_target(refs, entry, tolerance: float = _REMOVE_TOLERANCE) -> dict:
    """从 find_actors 的命中里挑出唯一要删的那个 Actor。纯函数，不连编辑器，好离线测。

    名字那一道由 find_actors 做（label 子串、大小写不敏感），这里做第二道：
    把带坐标的命中按容差筛一遍，**恰好一条**才算数。
    - 零条命中（empty）→ 不删：可能已经删掉了，也可能名字根本没对上；
    - 有命中但坐标一条都对不上（mismatch）→ 不删：名字对上、位置不对，不是要删的那个；
    - 坐标对上不止一条（ambiguous）→ 不删：分不清是哪条，不猜；
    - 命中行读不出坐标（unreadable）→ 不删：确认不了，不猜。

    返回 {"ok": True, "ref", "reason": "unique", "hits"}，或
    {"ok": False, "reason", "error", "hits"}。调用方只按 ok 决定删不删。
    """
    hits = [item for item in (refs or []) if _ref_path_of(item)]
    want = _coords_of(entry)
    if want is None:
        return {"ok": False, "reason": "no_target_coord", "hits": len(hits),
                "error": "条目没有坐标"}
    matched: list[str] = []
    unreadable = 0
    try:
        span = float(tolerance)
    except (TypeError, ValueError):
        span = _REMOVE_TOLERANCE
    for item in hits:
        got = _coords_of(item)
        if got is None:
            unreadable += 1
            continue
        if _dist(got, want) <= span:
            matched.append(_ref_path_of(item))
    name = str((entry or {}).get("name") or "").strip() if isinstance(entry, dict) else ""
    label = name or "目标"
    if not hits:
        return {"ok": False, "reason": "empty", "hits": 0,
                "error": f"{label} 零命中"}
    if unreadable:
        return {"ok": False, "reason": "unreadable", "hits": len(hits),
                "error": f"{label} 命中 {len(hits)} 个，其中 {unreadable} 个读不出坐标"}
    if not matched:
        return {"ok": False, "reason": "mismatch", "hits": len(hits),
                "error": f"{label} 命中 {len(hits)} 个，没有一个在目标坐标 {span:g}uu 内"}
    if len(matched) > 1:
        return {"ok": False, "reason": "ambiguous", "hits": len(hits),
                "candidates": matched,
                "error": f"{label} 命中的 {len(hits)} 个里有 {len(matched)} 个坐标都对得上，分不清"}
    return {"ok": True, "reason": "unique", "hits": len(hits), "ref": matched[0],
            "index_note": f"{label} 命中 {len(hits)} 个，坐标只对上一个"}


def _removal_targets(raw) -> tuple[list[dict], str]:
    """把 targets 归一成 [{"name","x","y","z"}]。缺名字、缺坐标、超量都整条失败，不猜。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return [], "targets 不是合法 JSON"
    if not isinstance(raw, list):
        return [], "targets 必须是 [{name, x, y, z}] 的列表"
    if not raw:
        return [], "targets 是空的"
    if len(raw) > _MAX_REMOVE_TARGETS:
        return [], f"targets 超过 {_MAX_REMOVE_TARGETS} 条"
    out: list[dict] = []
    for index, row in enumerate(raw, start=1):
        if not isinstance(row, dict):
            return [], f"第 {index} 条不是 {{name, x, y, z}}"
        name = str(row.get("name") or "").strip()
        if not name:
            return [], f"第 {index} 条没有 name"
        coords = _coords_of(row)
        if coords is None:
            return [], f"第 {index} 条（{name}）没有坐标"
        out.append({"name": name, "x": coords[0], "y": coords[1], "z": coords[2]})
    return out, ""


def _actor_hits(name: str, task: dict) -> dict:
    """按 label 子串找 Actor，并把每条命中的坐标读回来。
    读不回坐标就失败：确认不了位置就不许删。"""
    from core.editor_exec import exec_tool

    found = exec_tool(
        _SCENE, "find_actors",
        {"name": name, "tag": "", "collision_channels": []},
        task=task,
    )
    if not found.get("ok"):
        err = str(found.get("error") or f"查找 {name} 失败")
        if _conn_error(err):
            return {"ok": False, "error": f"编辑器 MCP 连不上：{err}"}
        return {"ok": False, "error": f"查找 {name} 失败：{err}"}
    hits: list[dict] = []
    for ref in _actor_refs(found.get("data")):
        loc = _read_location(ref, task)
        if not loc.get("ok"):
            return {"ok": False, "error": f"读 {ref} 的坐标失败：{loc.get('error')}"}
        x, y, z = loc["location"]
        hits.append({"refPath": ref, "location": {"x": x, "y": y, "z": z}})
    return {"ok": True, "hits": hits}


def _remove_batch(step: dict) -> dict:
    """按 {name, x, y, z} 条目精确删 Actor，与 _place_batch 对称。
    名字那一道交给 find_actors（label 子串），坐标再筛（容差 2uu）：只有恰好一个对得上才删，
    筛不出唯一一条就失败、一个都不删。全部删完逐条复查该坐标已归零，
    最后按 save 策略把当前关卡存一次。"""
    from core.editor_exec import exec_tool

    read = _read_task()
    slots = step.get("slots") if isinstance(step.get("slots"), dict) else {}
    raw = slots.get("targets") if slots.get("targets") is not None else step.get("targets")
    targets, why = _removal_targets(raw)
    if not targets:
        return {"ok": False, "error": f"没有要删的条目：{why}，不猜。"}
    write = {"allow_writes": ["remove_from_scene"],
             **task_policy(save=task_flag(step, "save"), pie=False)}
    removed: list[dict] = []
    for entry in targets:
        name = entry["name"]
        hits = _actor_hits(name, read)
        if not hits.get("ok"):
            return {"ok": False, "id": name, "removed": len(removed),
                    "error": f"{hits.get('error')}（已删 {len(removed)} 条）"}
        picked = pick_removal_target(hits["hits"], entry)
        if not picked.get("ok"):
            return {"ok": False, "id": name, "removed": len(removed),
                    "error": f"{name} 没有唯一命中：{picked.get('error')}，不删。"}
        gone = exec_tool(
            _SCENE, "remove_from_scene",
            {"actor": {"refPath": picked["ref"]}},
            task=write,
        )
        if not gone.get("ok"):
            err = str(gone.get("error") or "删除失败")
            if _conn_error(err):
                return {"ok": False, "id": name, "removed": len(removed),
                        "error": f"编辑器 MCP 连不上：{err}"}
            return {"ok": False, "id": name, "removed": len(removed),
                    "error": f"{name} 删除失败：{err}"}
        removed.append({"name": name, "ref": picked["ref"]})
    # 复查：每条目标在那个坐标上必须已经归零（还有命中就是没删干净，不静默）
    left_counts: list[str] = []
    for entry in targets:
        name = entry["name"]
        hits = _actor_hits(name, read)
        if not hits.get("ok"):
            return {"ok": False, "id": name, "removed": len(removed),
                    "error": f"复查读不到场景：{hits.get('error')}"}
        # 名字剩余总量照报（同名前缀的正确实例本来就该留着，所以只报数、不拿它当闸）
        left_counts.append(f"{name} 剩 {len(hits['hits'])}")
        left = pick_removal_target(hits["hits"], entry)
        if left.get("ok") or left.get("reason") not in ("empty", "mismatch"):
            return {"ok": False, "id": name, "removed": len(removed),
                    "error": f"复查未通过：{name} 在 "
                             f"({entry['x']:g}, {entry['y']:g}, {entry['z']:g}) 附近仍有命中"
                             f"（{left.get('error') or '未归零'}）"}
    note = (f"按 {len(targets)} 条目标精确删掉 {len(removed)} 个 Actor，坐标复查已归零"
            f"（目标坐标处剩余 0；{'、'.join(left_counts)}）")
    saved = _save_level(step)
    if not saved.get("ok"):
        return {"ok": False, "id": "remove_batch", "saved": False,
                "error": f"{saved.get('error')}（删除与复查已完成：{note}）"}
    save_note = f"；{saved.get('note')}" if saved.get("note") else ""
    summary = note + save_note
    return {
        "ok": True,
        "id": "remove_batch",
        "in_level": True,
        "removed": [item["ref"] for item in removed],
        "remaining": left_counts,
        "index_note": summary,
        "test": {"ok": True, "kind": "editor_assert", "summary": summary, "errors": []},
        "judge_kind": "editor_assert",
    }


def _query_material(step: dict) -> dict:
    """只读。读材质实例一个标量参数的当前值，裁判 editor_assert。不写。"""
    from core.editor_exec import exec_tool

    read = _read_task()
    slots = step.get("slots") if isinstance(step.get("slots"), dict) else {}
    target = str(slots.get("target") or "").strip()
    param = str(slots.get("param") or "").strip()
    if not target:
        return {"ok": False, "error": "没有要读的材质实例"}
    if not param:
        return {"ok": False, "id": target, "error": "没有参数名，不猜。"}
    instance_ref = _material_instance_ref(target)
    if not instance_ref:
        return {"ok": False, "id": target, "error": f"{target} 没有唯一的材质实例路径，不猜。"}
    got = exec_tool(
        _MATERIAL_INSTANCE,
        "get_scalar_parameter",
        {"instance": {"refPath": instance_ref}, "name": param},
        task=read,
    )
    if not got.get("ok"):
        err = str(got.get("error") or "读参数失败")
        if _conn_error(err):
            err = f"编辑器 MCP 连不上：{err}"
        return {"ok": False, "id": instance_ref, "ref": instance_ref, "error": err}
    current = _scalar_value(got.get("data"))
    if current is None:
        return {"ok": False, "id": instance_ref, "ref": instance_ref, "error": f"{param} 读不到数值"}
    summary = f"{instance_ref.rsplit('/', 1)[-1].split('.')[0]}.{param}={current:g}"
    return {
        "ok": True,
        "id": target,
        "ref": instance_ref,
        "in_level": False,
        "summary": summary,
        "index_note": summary,
        "test": {"ok": True, "kind": "editor_assert", "summary": summary, "errors": []},
        "judge_kind": "editor_assert",
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


def run_sequence(query: str, decide, *, choices: bool = False, layered: bool = False, complete=None) -> tuple[int, Path]:
    """decide(step, summary) -> stop / skip / retry。summary 为空表示这一步还跑不了。
    choices 为真时，每次 s/c/r 写一条 confirm 事件。
    layered 为真时和菜单 3 共用 plan_windows：强规则仍是 L0，弱命中走理解器。"""
    from agent.run_task import run_task

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RUNS_DIR / stamp.split("-", 1)[0] / f"{stamp}.md"
    events = None
    if choices:
        from agent.run_windows import _EventBus

        events = _EventBus(stamp)
    from backends.select import bind_run

    bind_run(None if events is None else events.emit)
    if layered:
        from agent.plan_windows import steps_for_menu2

        steps = steps_for_menu2(query, complete=complete)
    else:
        from agent.supervise import plan_sequence

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
        action = step.get("action")
        try:
            from agent.catalog import BLUEPRINT_ATTEMPT_ACTIONS, GRAPH_ACTIONS
            from backends.select import current_backend

            if action in BLUEPRINT_ATTEMPT_ACTIONS and current_backend() == "monolith":
                summary = repair_blueprint(step)
            elif action in GRAPH_ACTIONS:
                task = step.get("task") if isinstance(step.get("task"), dict) and step["task"].get("verify") else None
                if task is None:
                    from agent.run_windows import _build_graph_task

                    task = _build_graph_task(step, query)
                summary = task if task.get("ok") is False and not task.get("verify") else run_task(task)
            elif action:
                summary = _run_action(step)
            else:
                summary = run_task(step)
        except Exception as exc:
            summary = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
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
