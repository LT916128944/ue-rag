# agent/plan_windows.py
"""快路径拆不开时，才请规划者列窗口。规划者只看这一句话和动作表。"""
import json
import os
import re
from pathlib import Path

from agent.catalog import CATALOG, system_prompt

_NAME_RE = re.compile(
    r"(?<![A-Za-z0-9_])(BP_[A-Za-z0-9_]+|[AUF][A-Z][A-Za-z0-9_]*)(?![A-Za-z0-9_])"
)
_TAG_RE = re.compile(r"(?<![A-Za-z0-9_])tag(?![A-Za-z0-9_])", re.I)
_LOC_XYZ = re.compile(
    r"x\s*=\s*(-?\d+(?:\.\d+)?)\D+y\s*=\s*(-?\d+(?:\.\d+)?)\D+z\s*=\s*(-?\d+(?:\.\d+)?)",
    re.I,
)
_LOC_TRIPLE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)"
)


def routes_path() -> Path:
    override = os.environ.get("UE_LEARNED_ROUTES", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "learned_routes.json"


def extract_names(query: str) -> list[str]:
    found = []
    for match in _NAME_RE.finditer(query or ""):
        name = match.group(1)
        if name not in found:
            found.append(name)
    return found


def extract_when(query: str) -> list[str]:
    """动作词由代码从原句抽取。tag 记成「标签」，避免把某一次的英文写法写死。
    放置类措辞（放/丢/拖到场景等）统一记成「放置」，量词和「再」不挡变体。"""
    found = []
    if "标签" in (query or "") or _TAG_RE.search(query or ""):
        found.append("标签")
    if "可交互" in (query or ""):
        found.append("可交互")
    elif "交互" in (query or ""):
        found.append("交互")
    from agent.supervise import _PLACE_RE

    if _PLACE_RE.search(query or ""):
        found.append("放置")
    return found


# 需求词兜底（20261015-181645 的教训）：这些词出现说明句子带着「要实现的东西」，
# 没有改码扇接住时不能当 L0 强命中，否则需求半句被静默丢掉、测试照跑还报成功。
_DEMAND_RE = re.compile(r"实现|逻辑|随机|功能|需求|行为")


def _unbound_demand(query: str, actions) -> bool:
    """句子带着需求性说法、却没有任何 edit_cpp 扇去实现它 → 返回 True（弱命中）。
    交 L1 理解器：模型接得住就绑成动作，接不住进 unresolved → 任务书记「尚未完成」。"""
    if not _DEMAND_RE.search(query or ""):
        return False
    return "edit_cpp" not in set(actions or [])


def _query_has(query: str, word: str) -> bool:
    if word == "标签":
        return "标签" in query or _TAG_RE.search(query) is not None
    if word == "tag":
        return _TAG_RE.search(query) is not None
    if word == "放置":
        from agent.supervise import _PLACE_RE

        return _PLACE_RE.search(query or "") is not None
    return word in query


def load_routes() -> list[dict]:
    path = routes_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        rows = data.get("routes") or []
    elif isinstance(data, list):
        rows = data
    else:
        return []
    return [row for row in rows if isinstance(row, dict)]


LEARNED_ROUTE_LIMIT = 50  # LRU 护栏（20261015-1705 P3）：learned_routes 最多 50 条


def _save_routes(routes: list[dict]) -> None:
    path = routes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    routes = routes[-LEARNED_ROUTE_LIMIT:]
    payload = {"routes": routes}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _default_title(action: str, slots: dict) -> str:
    if action == "set_pickable":
        return f"把 {slots.get('target') or '物体'} 设为可拾取"
    if action == "judge_pickup":
        return f"只读裁决 {slots.get('picker') or '拾取者'} 能否拾取 {slots.get('target') or '物体'}"
    if action == "compile_project":
        return "编译工程"
    if action == "compile_blueprint":
        return "编译蓝图"
    if action == "edit_cpp":
        return "改 C++ 并编译"
    if action == "move_actor":
        return "移动 Actor"
    if action == "place_actor":
        return "放进场景"
    if action == "bind_blueprint":
        return "绑定蓝图"
    return action or "窗口"


def _action_of(step: dict) -> str:
    action = str(step.get("action") or "")
    if action == "check_pickup":
        return "judge_pickup"
    if action:
        return action
    kinds = [item.get("kind") for item in (step.get("verify") or []) if isinstance(item, dict)]
    if step.get("freeze_source") and "cpp_build" in kinds:
        return "compile_project"
    if "blueprint_compile" in kinds:
        return "compile_blueprint"
    if "cpp_build" in kinds:
        return "edit_cpp"
    if "editor_assert" in kinds:
        return "move_actor"
    return ""


def _blueprint_hits(name: str) -> list[dict]:
    from agent.supervise import _records

    found = []
    seen = set()
    for rec in _records("ue_asset"):
        meta = rec.get("metadata") or {}
        if str(meta.get("asset_name") or "") != name:
            continue
        kind = str(meta.get("asset_class") or "")
        if kind and kind != "Blueprint":
            continue
        ref = str(meta.get("object_path") or "")
        if not ref or ref in seen:
            continue
        seen.add(ref)
        found.append(meta)
    return found


def _fields_from_ref(name: str, ref: str, *, from_path: bool = False) -> dict:
    asset_path = ref.split(".", 1)[0] if "." in ref else ref
    folder, _, asset = asset_path.rpartition("/")
    return {
        "ok": True,
        "kind": "blueprint",
        "actor_name": name,
        "asset_name": asset or name,
        "asset_path": asset_path,
        "folder_path": folder or "/Game",
        "ref_path": ref if "." in ref else f"{asset_path}.{name}",
        "from_path": from_path,
    }


def _disk_blueprint(name: str) -> dict | None:
    from config import UE_PROJECT_DIR

    content = UE_PROJECT_DIR / "Content"
    if not content.is_dir():
        return None
    found = [path for path in content.rglob(name + ".uasset") if path.is_file()]
    if len(found) > 1:
        return {"ok": False, "error": f"{name} 的资产不是唯一的，不猜。"}
    if not found:
        return None
    rel = found[0].relative_to(content).with_suffix("").as_posix()
    asset_path = "/Game/" + rel
    return _fields_from_ref(name, f"{asset_path}.{name}")


def _resolve_blueprint(name: str) -> dict:
    hits = _blueprint_hits(name)
    if len(hits) > 1:
        return {"ok": False, "error": f"{name} 的蓝图不是唯一的，不猜。"}
    if len(hits) == 1:
        return _fields_from_ref(name, str(hits[0].get("object_path") or ""))
    disk = _disk_blueprint(name)
    if disk is not None:
        if disk.get("ok") is False:
            return disk
        return disk
    return {"ok": False, "error": f"{name} 没有唯一的蓝图，不猜。"}


def _resolve_cpp(name: str) -> dict:
    from agent.supervise import _resolve_class

    classes = {}
    for rec in _resolve_class(name):
        class_name = str((rec.get("metadata") or {}).get("class_name") or "")
        if class_name:
            classes[class_name] = rec
    if len(classes) != 1:
        return {"ok": False, "error": f"{name} 在 C++ 索引里不是唯一的一条，不猜。"}
    class_name = next(iter(classes))
    return {"ok": True, "kind": "class", "class_name": class_name, "actor_name": class_name}


def _resolve_actor(name: str) -> dict:
    from agent.supervise import _records

    hits = []
    for rec in _records("ue_actor"):
        label = str((rec.get("metadata") or {}).get("actor_label") or "")
        if label == name:
            hits.append(rec)
    if len(hits) > 1:
        return {"ok": False, "error": f"{name} 在场景索引里不是唯一的，不猜。"}
    if len(hits) == 1:
        return {"ok": True, "kind": "actor", "actor_name": name}
    return {"ok": False, "error": f"{name} 没有唯一的 Actor，不猜。"}


def _resolve_game_path(path: str) -> dict:
    text = path.strip().rstrip("/")
    if ".." in text.split("/"):
        return {"ok": False, "error": "路径不合法，不猜。"}
    leaf = text.split("/")[-1]
    if "." in leaf:
        asset = leaf.split(".", 1)[0]
        return _fields_from_ref(asset, text, from_path=True)
    if leaf.startswith("BP_") and text.startswith("/Game/"):
        return _fields_from_ref(leaf, f"{text}.{leaf}", from_path=True)
    return {"ok": False, "error": "路径里没有唯一的蓝图名，不猜。"}


def resolve_name(value: str) -> dict:
    text = str(value or "").strip()
    if not text:
        return {"ok": False, "error": "槽位是空的，不猜。"}
    if text.startswith("/Game/"):
        return _resolve_game_path(text)
    if text.startswith("BP_"):
        return _resolve_blueprint(text)
    if re.match(r"^[AUF][A-Z][A-Za-z0-9_]*$", text):
        return _resolve_cpp(text)
    return _resolve_actor(text)


def resolve_location(value) -> dict:
    if isinstance(value, dict):
        try:
            loc = {"x": float(value["x"]), "y": float(value["y"]), "z": float(value["z"])}
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "坐标不是唯一的一组，不猜。"}
        return {"ok": True, "location": loc}
    text = str(value or "")
    match = _LOC_XYZ.search(text) or _LOC_TRIPLE.search(text)
    if not match:
        return {"ok": False, "error": "坐标不是唯一的一组，不猜。"}
    x, y, z = (float(part) for part in match.groups())
    return {"ok": True, "location": {"x": x, "y": y, "z": z}}


def _normalize_slots(action: str, slots: dict) -> dict:
    out = {str(key): value for key, value in slots.items()}
    if action == "move_actor":
        if "location" not in out and out.get("coords") is not None:
            out["location"] = out["coords"]
        if "location" not in out and out.get("坐标") is not None:
            out["location"] = out["坐标"]
        if "location" not in out and all(key in out for key in ("x", "y", "z")):
            out["location"] = {"x": out["x"], "y": out["y"], "z": out["z"]}
        if "actor" not in out and out.get("name"):
            out["actor"] = out["name"]
    if action == "bind_blueprint" and "class" not in out and out.get("类"):
        out["class"] = out["类"]
    return out


def _missing_slots(action: str, slots: dict) -> str:
    spec = CATALOG[action]
    required = spec["slots"]
    if not required:
        return ""
    if spec.get("mode") == "one":
        if any(str(slots.get(name) or "").strip() for name in required):
            return ""
        return "缺少蓝图或类，不猜。"
    missing = [name for name in required if not str(slots.get(name) or "").strip()]
    if missing:
        return "缺少槽位 " + "、".join(missing) + "，不猜。"
    return ""


def _merge(fields: dict, found: dict) -> None:
    for key, value in found.items():
        if key == "ok" or value is None:
            continue
        if key == "actor_name" and fields.get("actor_name"):
            continue
        fields.setdefault(key, value)


def resolve_slots(action: str, slots: dict) -> dict:
    if action == "compile_project":
        return {"ok": True}
    if action == "integrate_verify":
        return {"ok": True}
    if action == "bind_blueprint":
        if slots.get("blueprint"):
            return resolve_name(str(slots["blueprint"]))
        if slots.get("class"):
            found = _resolve_cpp(str(slots["class"]))
            if not found.get("ok"):
                return found
            from agent.supervise import _indexed_blueprint

            chosen = _indexed_blueprint(found["class_name"])
            if chosen and chosen.get("ok"):
                chosen["kind"] = "blueprint"
                chosen["class_name"] = found["class_name"]
                return chosen
            return {"ok": False, "error": f"{found['class_name']} 还没有唯一蓝图路径，不猜。"}
        return {"ok": False, "error": "缺少蓝图或类，不猜。"}

    fields = {"ok": True}
    if action == "judge_pickup":
        target = resolve_name(str(slots.get("target") or ""))
        picker = resolve_name(str(slots.get("picker") or ""))
        if not target.get("ok"):
            return target
        if not picker.get("ok"):
            return picker
        fields.update({
            "actor_name": target.get("actor_name"),
            "picker_name": picker.get("actor_name"),
            "asset_name": target.get("asset_name"),
            "asset_path": target.get("asset_path"),
            "folder_path": target.get("folder_path"),
            "ref_path": target.get("ref_path"),
        })
        return {key: value for key, value in fields.items() if value is not None}

    if action == "move_actor":
        actor = resolve_name(str(slots.get("actor") or ""))
        loc = resolve_location(slots.get("location"))
        if not actor.get("ok"):
            return actor
        if not loc.get("ok"):
            return loc
        fields["actor_name"] = actor.get("actor_name")
        fields["location"] = loc["location"]
        return fields

    if action == "edit_cpp":
        found = resolve_name(str(slots.get("class") or ""))
        if not found.get("ok"):
            return found
        if found.get("kind") != "class":
            return {"ok": False, "error": f"{slots.get('class')} 不是唯一的 C++ 类，不猜。"}
        fields["class_name"] = found["class_name"]
        fields["actor_name"] = found["class_name"]
        return fields

    slot_name = "blueprint" if action in ("compile_blueprint", "place_actor") else "target"
    found = resolve_name(str(slots.get(slot_name) or ""))
    if not found.get("ok"):
        return found
    _merge(fields, found)
    if action in ("compile_blueprint", "place_actor") and not fields.get("ref_path"):
        return {"ok": False, "error": f"{slots.get(slot_name)} 没有唯一的蓝图路径，不猜。"}
    return {key: value for key, value in fields.items() if value is not None}


def materialize(action: str, slots: dict, *, title: str, wid: str, depends: list, retries: int) -> dict:
    window = {
        "id": wid,
        "title": title or _default_title(action, slots),
        "action": action,
        "slots": slots,
        "depends_on": [str(item) for item in depends if str(item).strip()],
        "max_retries": max(0, int(retries)),
        "realizable": False,
        "ok": False,
        "blocked": True,
        "save": False,
        "pie": False,
    }
    if action not in CATALOG:
        window["error"] = f"表外动作 {action or '空'}，这一窗暂停，不执行。"
        return window
    slots = _normalize_slots(action, slots)
    window["slots"] = slots
    missing = _missing_slots(action, slots)
    if missing:
        window["error"] = missing
        return window
    resolved = resolve_slots(action, slots)
    if resolved.get("ok") is False:
        window["error"] = resolved.get("error") or "槽位解析失败，不猜。"
        return window
    for key, value in resolved.items():
        if key != "ok" and value is not None:
            window[key] = value
    window["realizable"] = True
    window["ok"] = True
    window["blocked"] = False
    window.pop("error", None)
    return window


# asset 槽位动作：归地统一走 link_asset（20261015-1705 P2）。
# set_pickable / judge_pickup 依赖场景里的实例，不自动插创建扇，仍旧暂停等人工。
_ASSET_GROUND_ACTIONS = frozenset({"compile_blueprint", "place_actor", "bind_blueprint"})


def goal_default_retries(action: str) -> int:
    return 0 if action == "judge_pickup" else 1


def _create_and_goal(action: str, slots: dict, linked: dict, wid: str, title: str,
                     depends=None) -> list[dict]:
    """「创建蓝图」前置扇 + 目标扇（L2 归地产物）。create 缺目录时两扇都暂停，
    只问一次 r（沿用规则扇的文案）；create 有目录时目标扇直接可跑。
    目标扇记 goal=True，回写快路径模板时不把 create 前置扇写进去。"""
    create = dict(linked.get("create") or {})
    create_ok = bool(create.get("ok"))
    create_id = f"{wid}c"
    asset_name = str(create.get("asset_name") or "")
    parent = str(create.get("parent") or "")
    create_task = {
        "id": create_id,
        "action": "create_blueprint",
        "title": "创建蓝图",
        "query": f"创建蓝图 {asset_name}" + (f"（父类 {parent}）" if parent else "") + "，不改图",
        "save": False,
        "pie": False,
        "max_retries": 1,
    }
    for key in ("folder_path", "asset_name", "actor_name", "ref_path", "asset_path", "parent"):
        if create.get(key) is not None:
            create_task[key] = create[key]
    create_window = {
        "id": create_id,
        "title": str(create.get("title") or "创建蓝图"),
        "action": "create_blueprint",
        "slots": {},
        "depends_on": [str(dep) for dep in (depends or []) if str(dep).strip()],
        "max_retries": 1 if create_ok else 0,
        "realizable": create_ok,
        "ok": create_ok,
        "blocked": not create_ok,
        "save": False,
        "pie": False,
        "task": create_task,
    }
    if not create_ok:
        create_window["error"] = str(create.get("error") or
                                     "索引里没有这张蓝图，也没有 /Game 路径。只给一次：r /Game/某目录，或 r /Game/某目录/BP_名字。")
        create_task["needs"] = "path"
        create_task["error"] = create_window["error"]
        create_task["ok"] = False
        create_task["blocked"] = True

    goal_ok = create_ok
    goal_window = {
        "id": wid,
        "title": title,
        "action": action,
        "slots": dict(slots),
        "depends_on": [create_id],
        "max_retries": goal_default_retries(action),
        "realizable": goal_ok,
        "ok": goal_ok,
        "blocked": not goal_ok,
        "save": False,
        "pie": False,
        "goal": True,
    }
    if create_ok:
        for key in ("folder_path", "asset_name", "actor_name", "ref_path", "asset_path", "parent"):
            if create.get(key) is not None:
                goal_window[key] = create[key]
    else:
        goal_window["error"] = "等蓝图路径。只给一次：r /Game/某目录/BP_名字。创建蓝图时填过的目录会带到这里。"
    return [create_window, goal_window]


def materialize_goal(action: str, slots: dict, *, title: str, wid: str, depends: list, retries: int,
                     paths: list | None = None) -> list[dict]:
    """单扇归地入口（P2）：asset 槽位动作统一走 link_asset——索引唯一命中直连；
    miss 且父类可推（或句中给了 /Game 路径）自动插「创建蓝图」前置扇；推不出照旧暂停等 r。
    paths 是句中的 /Game 路径（最多一条当目录提示）。其余动作原路 materialize。
    供 _materialize_goals 与 materialize_scopes 共用。"""
    if action in CATALOG and action in _ASSET_GROUND_ACTIONS and isinstance(slots, dict):
        from agent.grounding import link_asset

        token = str(slots.get("blueprint") or slots.get("class") or "")
        linked = link_asset(token, paths=paths)
        if linked.get("plan") == "create_planned":
            return _create_and_goal(action, slots, linked, wid, title, depends)
        if linked.get("plan") == "pause_r":
            window = materialize(action, slots, title=title, wid=wid, depends=depends, retries=retries)
            window["goal"] = True
            if not window.get("realizable"):
                window["realizable"] = False
                window["ok"] = False
                window["blocked"] = True
                window["error"] = str(linked.get("error") or window.get("error") or "推不出唯一的蓝图，不猜。")
                window["needs"] = str(linked.get("needs") or "path")
            return [window]
    window = materialize(action, slots, title=title, wid=wid, depends=depends, retries=retries)
    window["goal"] = True
    return [window]


def _intent_query_paths(query: str) -> list[str]:
    """句中至多一条 /Game 路径给归地当目录提示；多条就不猜，交给唯一性规则。"""
    found = re.findall(r"/Game(?:/[A-Za-z0-9_]+)+", query or "")
    return [found[0]] if len(found) == 1 else []


def _materialize_goals(intent: dict, query: str = "") -> list[dict]:
    """L1 意图 → L2 归地 → 窗口清单（P1 主链）。goals 的先后顺序即执行顺序，
    具体依赖按名字数据流连：同目标才连，独立目标不虚连；
    create 前置扇与目标扇的依赖已由 materialize_goal 定死。"""
    paths = _intent_query_paths(query)
    windows: list[dict] = []
    for goal in intent.get("goals") or []:
        action = str(goal.get("action") or "")
        slots = dict(goal.get("slots") or {})
        try:
            retries = int(goal.get("max_retries", goal_default_retries(action)))
        except (TypeError, ValueError):
            retries = goal_default_retries(action)
        made = materialize_goal(
            action,
            slots,
            title=str(goal.get("title") or "") or _default_title(action, slots),
            wid=f"W{len(windows) + 1}",
            depends=[],
            retries=retries,
            paths=paths,
        )
        if goal.get("args") and isinstance(goal.get("args"), dict):
            made[0]["args"] = {str(k): str(v)[:120] for k, v in goal["args"].items()}
        windows.extend(made)
    _link_by_names(windows)
    return windows


def _link_by_names(windows: list[dict]) -> None:
    """名字数据流依赖：与规则拆链同一套口径（_name_stem 去前缀小写）。
    只回连排在自己前面的扇，不产生环；已有依赖（create 前置扇）原样保留。"""
    stems_list: list[set[str]] = []
    for index, window in enumerate(windows, 1):
        stems = _step_stems(window)
        deps = list(dict.fromkeys(str(dep) for dep in (window.get("depends_on") or [])))
        if stems:
            for prev in range(1, index):
                if stems & stems_list[prev - 1]:
                    pid = f"W{prev}"
                    if pid not in deps:
                        deps.append(pid)
        window["depends_on"] = deps
        stems_list.append(stems)


def _normalize_raw(raw, index: int) -> dict:
    if not isinstance(raw, dict):
        return {"id": f"W{index}", "title": "", "action": "", "slots": {}, "depends_on": [], "max_retries": 1}
    slots = raw.get("slots") if isinstance(raw.get("slots"), dict) else {}
    depends = raw.get("depends_on") if isinstance(raw.get("depends_on"), list) else []
    try:
        retries = int(raw.get("max_retries", 1))
    except (TypeError, ValueError):
        retries = 1
    return {
        "id": str(raw.get("id") or f"W{index}").strip() or f"W{index}",
        "title": str(raw.get("title") or "").strip(),
        "action": str(raw.get("action") or "").strip(),
        "slots": slots,
        "depends_on": [str(item) for item in depends if str(item).strip()],
        "max_retries": max(0, retries),
    }


def parse_planner(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("windows"), list):
        return None
    return data


def _paused(message: str) -> dict:
    return {
        "id": "W1",
        "title": "规划者",
        "action": "",
        "slots": {},
        "depends_on": [],
        "max_retries": 0,
        "realizable": False,
        "ok": False,
        "blocked": True,
        "error": message,
        "save": False,
        "pie": False,
    }


def route_id(actions: list[str], when: list[str]) -> str:
    if actions == ["set_pickable", "judge_pickup"] and ("可交互" in when or "标签" in when):
        return "mark-interactable"
    slug = "-".join(action for action in actions if action) or "route"
    return slug[:80]


def find_cycle(windows: list[dict]) -> list[str] | None:
    """依赖环检测：返回环上的窗口 id 序列（如 W1→W2→W1）。无环返回 None。"""
    by_id = {window["id"]: window for window in windows}
    state: dict[str, int] = {}

    def visit(wid: str, stack: list[str]) -> list[str] | None:
        mark = state.get(wid)
        if mark == 2:
            return None
        if mark == 1:
            return stack[stack.index(wid):] + [wid]
        state[wid] = 1
        stack.append(wid)
        for dep in by_id.get(wid, {}).get("depends_on") or []:
            if dep in by_id:
                found = visit(dep, stack)
                if found:
                    return found
        stack.pop()
        state[wid] = 2
        return None

    for window in windows:
        found = visit(window["id"], [])
        if found:
            return found
    return None


def mark_dangling(windows: list[dict]) -> list[tuple[str, list[str]]]:
    """depends_on 写了不存在的窗口 id：这一窗暂停，报「未知窗口 id」，不拖到集成段。"""
    known = {window["id"] for window in windows}
    bad: list[tuple[str, list[str]]] = []
    for window in windows:
        unknown = [dep for dep in window.get("depends_on") or [] if dep not in known]
        if not unknown:
            continue
        window["realizable"] = False
        window["ok"] = False
        window["blocked"] = True
        window["error"] = "依赖了未知窗口 id " + "、".join(unknown) + "，不猜。"
        bad.append((window["id"], unknown))
    return bad


def remember_route(query: str, windows: list[dict]) -> bool:
    """验收通过才写。槽位记角色，不记这一次的资产名。同一 id 不追加。"""
    when = extract_when(query)
    names = extract_names(query)
    if not when or not names:
        return False
    identities = set(names)
    for window in windows:
        for value in (window.get("slots") or {}).values():
            if str(value).strip() not in identities:
                return False
    value_to_role = {}
    for window in windows:
        for key, value in (window.get("slots") or {}).items():
            value_to_role.setdefault(str(value), str(key))
    roles = []
    for name in names:
        role = value_to_role.get(name)
        if not role:
            return False
        roles.append(role)
    if len(set(roles)) != len(roles):
        return False
    id_map = {}
    for index, window in enumerate(windows, 1):
        id_map[str(window.get("id") or f"W{index}")] = f"W{index}"
    stored = []
    for index, window in enumerate(windows, 1):
        action = str(window.get("action") or "")
        slot_roles = {}
        for key, value in (window.get("slots") or {}).items():
            role = value_to_role.get(str(value))
            if not role:
                return False
            slot_roles[str(key)] = role
        item = {"action": action, "slots": slot_roles}
        depends = [id_map[item_id] for item_id in (window.get("depends_on") or []) if item_id in id_map]
        if depends:
            item["depends_on"] = depends
        if window.get("max_retries") is not None and action == "judge_pickup":
            item["max_retries"] = int(window["max_retries"])
        stored.append(item)
    rid = route_id([item["action"] for item in stored], when)
    routes = load_routes()
    if any(str(item.get("id") or "") == rid for item in routes):
        return False
    routes.append({"id": rid, "when": when, "roles": roles, "windows": stored})
    _save_routes(routes)
    return True


def remember_intent_route(query: str, intent: dict | None, windows: list[dict]) -> str | None:
    """P3 L3 学习回写：整句集成成功才由调用方调用。unresolved 非空的意图不回写（半猜测不进缓存）。
    不记资产名：只记 when 触发词、roles、goals（动作 + 槽位角色 + 参数形状）和 depends 形状；
    重放时 token 按角色代入，再走 L2 归地，资产在哪台机器现归地。条目 LRU 上限 50。"""
    if not isinstance(intent, dict) or (intent.get("unresolved") or []):
        return None
    when = extract_when(query)
    names = extract_names(query)
    if not when or not names:
        return None
    goal_windows = [window for window in (windows or []) if isinstance(window, dict) and window.get("goal")]
    if not goal_windows:
        return None
    identities = set(names)
    for window in goal_windows:
        for value in (window.get("slots") or {}).values():
            if str(value).strip() not in identities:
                return None
    value_to_role: dict[str, str] = {}
    for window in goal_windows:
        for key, value in (window.get("slots") or {}).items():
            value_to_role.setdefault(str(value), str(key))
    roles = []
    for name in names:
        role = value_to_role.get(name)
        if not role:
            return None
        roles.append(role)
    if len(set(roles)) != len(roles):
        return None
    goal_id_by_wid: dict[str, str] = {
        str(window.get("id") or ""): f"G{index}"
        for index, window in enumerate(goal_windows, 1)
    }
    goals: list[dict] = []
    for window in goal_windows:
        slot_roles = {
            str(key): value_to_role.get(str(value), str(value))
            for key, value in (window.get("slots") or {}).items()
        }
        spec: dict = {
            "action": str(window.get("action") or ""),
            "slots": slot_roles,
            "max_retries": int(window.get("max_retries") or 0),
        }
        depends = [goal_id_by_wid[str(dep)] for dep in (window.get("depends_on") or []) if str(dep) in goal_id_by_wid]
        if depends:
            spec["depends_on"] = depends
        goals.append(spec)
    rid = route_id([item["action"] for item in goals], when)
    routes = load_routes()
    if any(str(item.get("id") or "") == rid for item in routes):
        return rid
    routes.append({"id": rid, "when": when, "roles": roles, "goals": goals})
    _save_routes(routes)
    return rid


def match_learned(query: str) -> list[dict] | None:
    query = (query or "").strip()
    names = extract_names(query)
    for route in load_routes():
        when = [str(word) for word in (route.get("when") or []) if str(word).strip()]
        roles = [str(role) for role in (route.get("roles") or []) if str(role).strip()]
        specs = route.get("goals") or route.get("windows") or []
        if not when or not roles or not specs:
            continue
        if len(names) != len(roles):
            continue
        if not all(_query_has(query, word) for word in when):
            continue
        binding = dict(zip(roles, names))
        if route.get("goals"):
            # P3 新条目：命中时把句中 token 按角色代入，再走 L2 归地产出窗口，
            # 资产是否存在由当下现场决定（linked / create_planned / pause_r）。
            goals = []
            for spec in specs:
                if not isinstance(spec, dict):
                    return None
                action = str(spec.get("action") or "")
                raw_slots = spec.get("slots") if isinstance(spec.get("slots"), dict) else {}
                goals.append({
                    "action": action,
                    "slots": {str(key): binding.get(str(role), str(role)) for key, role in raw_slots.items()},
                    "max_retries": spec.get("max_retries"),
                })
            return _materialize_goals({"goals": goals}, query)
        windows = []
        for index, spec in enumerate(specs, 1):
            if not isinstance(spec, dict):
                return None
            action = str(spec.get("action") or "")
            raw_slots = spec.get("slots") if isinstance(spec.get("slots"), dict) else {}
            concrete = {str(key): binding.get(str(role), str(role)) for key, role in raw_slots.items()}
            try:
                retries = int(spec["max_retries"]) if spec.get("max_retries") is not None else (0 if action == "judge_pickup" else 1)
            except (TypeError, ValueError):
                retries = 0 if action == "judge_pickup" else 1
            windows.append(materialize(
                action,
                concrete,
                title=_default_title(action, concrete),
                wid=f"W{index}",
                depends=list(spec.get("depends_on") or []),
                retries=retries,
            ))
        return windows
    return None


_RULE_COMPILE_ACTIONS = frozenset({"edit_cpp", "compile_project", "compile_blueprint"})
_RULE_PROBE_ACTIONS = frozenset({"probe_move", "probe_look", "probe_play"})


def _name_stem(text: str) -> str:
    """BP_TestActor / ATestActor / TestActor 都算 TestActor，避免前缀挡住数据流。"""
    name = str(text or "").strip()
    if not name:
        return ""
    if name.startswith("BP_"):
        name = name[3:]
    elif len(name) > 1 and name[0] in "AUF" and name[1].isupper():
        name = name[1:]
    return name.lower()


def _step_stems(step: dict) -> set[str]:
    """这一步触及的名字（去前缀小写）。用来判两扇是否同一目标，独立目标不虚连。"""
    stems: set[str] = set()

    def add(value) -> None:
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            stem = _name_stem(str(item or ""))
            if stem and stem not in ("game", "none", "null") and len(stem) <= 64:
                stems.add(stem)

    for key in ("actor_name", "asset_name", "ref_path", "class_name", "picker_name"):
        if step.get(key):
            add(step[key])
    slots = step.get("slots") if isinstance(step.get("slots"), dict) else {}
    for value in slots.values():
        add(value)
    for key in ("picker", "target"):
        inner = step.get(key) if isinstance(step.get(key), dict) else {}
        if isinstance(inner, dict):
            for sub in ("actor_name", "asset_name", "ref_path"):
                if inner.get(sub):
                    add(inner[sub])
    for item in step.get("verify") or []:
        if not isinstance(item, dict):
            continue
        if item.get("actor_name"):
            add(item["actor_name"])
        if item.get("refPath"):
            add(str(item["refPath"]).split("/")[-1])
    if not stems:
        # 改码/编译步骤不带名字字段，回退到它自己的任务句里抽名字（BP_ 名与 A/U/F 前缀类名）
        query_text = str(step.get("query") or "")
        if query_text:
            for match in _NAME_RE.finditer(query_text):
                add(match.group(1))
    return stems


def windows_from_rules(steps: list[dict]) -> list[dict]:
    from agent.catalog import GRAPH_ACTIONS

    windows = []
    stems_list: list[set[str]] = []
    actions: list[str] = []
    for index, step in enumerate(steps, 1):
        action = _action_of(step)
        wid = f"W{index}"
        realizable = step.get("ok") is not False and not step.get("blocked")
        stems = _step_stems(step)
        deps: list[str] = []
        if action in _RULE_COMPILE_ACTIONS:
            # 编译强制单线：排在最近的同目标扇或最近的编译扇后面
            anchor = 0
            for prev in range(index - 1, 0, -1):
                if stems and stems & stems_list[prev - 1]:
                    anchor = prev
                    break
                if actions[prev - 1] in _RULE_COMPILE_ACTIONS:
                    anchor = prev
                    break
            if anchor:
                deps = [f"W{anchor}"]
        elif action in _RULE_PROBE_ACTIONS:
            # 测试接最近的编译/改码扇；上游没绑上动作、解析失败暂停时同样接上，不抢跑
            for prev in range(index - 1, 0, -1):
                if actions[prev - 1] in _RULE_COMPILE_ACTIONS or (
                    not actions[prev - 1] and steps[prev - 1].get("ok") is False
                ):
                    deps = [f"W{prev}"]
                    break
        # 名字数据流：同目标的扇才有依赖，独立读写之间不虚连
        for prev in range(1, index):
            if stems and stems & stems_list[prev - 1] and f"W{prev}" not in deps:
                deps.append(f"W{prev}")
        window = {
            "id": wid,
            "title": step.get("title") or _default_title(action, {}) or f"步骤 {index}",
            "action": action,
            "slots": {},
            "depends_on": deps,
            "max_retries": int(step.get("max_retries") or (3 if action in GRAPH_ACTIONS else 0)),
            "realizable": realizable,
            "ok": realizable,
            "blocked": not realizable,
            "task": step,
            "save": False,
            "pie": bool(step.get("pie")),
        }
        if step.get("error"):
            window["error"] = step["error"]
        windows.append(window)
        stems_list.append(stems)
        actions.append(action)
    return windows


def _complete_live(messages: list[dict]) -> str:
    from openai import OpenAI

    from config import UE_LLM_MODEL

    key = os.getenv("DEEPSEEK_API_KEY") or ""
    if not key.strip():
        raise RuntimeError("没有 DEEPSEEK_API_KEY")
    client = OpenAI(api_key=key, base_url="https://api.deepseek.com")
    response = client.chat.completions.create(model=UE_LLM_MODEL, messages=messages, temperature=0)
    return response.choices[0].message.content or ""


def _rules_cover(query: str, steps: list[dict]) -> bool:
    """手写规则的强命中（20261015-1705 P1 语义收紧）：每一步都能落地才算 L0 命中。
    拆出任何 ok=False / blocked / needs 的步骤算弱命中，交 L1 理解器接管，
    不再往正则里加措辞。标签 / 交互那两条老护栏保留。"""
    actions = {_action_of(step) for step in steps if _action_of(step)}
    if _unbound_demand(query, actions):
        return False
    if "可交互" in query and "set_pickable" not in actions:
        return False
    if "交互" in query and "拾取" not in query and "judge_pickup" not in actions:
        return False
    if not steps:
        return False
    for step in steps:
        if step.get("ok") is False or step.get("blocked") or step.get("needs"):
            return False
    return True


def _without_unasked_project_compile(query: str, windows: list[dict]) -> list[dict]:
    """没有说编译工程时，规划者若写出 compile_project，这一窗暂停，不拖住别的窗口。"""
    from agent.supervise import _is_project_compile

    if _is_project_compile(query):
        return windows
    dropped = {window["id"] for window in windows if window.get("action") == "compile_project"}
    if not dropped:
        return windows
    for window in windows:
        if window["id"] in dropped:
            window["realizable"] = False
            window["ok"] = False
            window["blocked"] = True
            window["error"] = "这句话没有要编译整个工程，这一窗暂停。"
            continue
        window["depends_on"] = [dep for dep in (window.get("depends_on") or []) if dep not in dropped]
    return windows


_TEST_ACTIONS = frozenset({
    "probe_move", "probe_look", "probe_play", "judge_pickup", "check_pickup",
})
_COMPILE_ACTIONS = frozenset({"edit_cpp", "compile_project", "compile_blueprint"})


def _needs_play_test(query: str) -> bool:
    """用户说了测试。拾取那类已经写明不进 PIE 的句子除外。"""
    if not re.search(r"测试|PIE", query or "", re.I):
        return False
    if re.search(r"不进\s*PIE|不要\s*PIE|不\s*PIE", query, re.I):
        return False
    return True


def _ensure_requested_test(query: str, windows: list[dict]) -> list[dict]:
    """规划者或手写规则把「测试」留在标题里时，补一扇真正进入 PIE 的窗口。"""
    if not _needs_play_test(query) or not windows:
        return windows
    if any(str(window.get("action") or "") in _TEST_ACTIONS for window in windows):
        return windows
    # PIE 接在最后一扇后面：前面的编译/改码/建蓝图/放置都先落地，上游暂停时也不抢跑
    dep = windows[-1]["id"]
    already = False
    from agent.sequence import _pawn_stem, _source_move_speed, _times_factor

    times = _times_factor(query)
    if times is not None:
        source = _source_move_speed(_pawn_stem(query))
        already = source is not None and abs(source - 100.0 * times) <= 0.1
    for window in windows:
        if window.get("action") in _COMPILE_ACTIONS and not window.get("blocked"):
            window["launch_editor"] = True
            window["launch_without_source"] = True
            window["wait_mcp"] = True
            if already and window.get("action") == "edit_cpp":
                window["judge_first"] = True
    wid = f"W{len(windows) + 1}"
    windows.append({
        "id": wid,
        "title": "PIE 测试",
        "action": "probe_play",
        "slots": {},
        "depends_on": [dep],
        "max_retries": 1,
        "realizable": True,
        "ok": True,
        "blocked": False,
        "save": False,
        "pie": True,
        "task": {
            "id": wid,
            "action": "probe_play",
            "title": "PIE 测试",
            "query": query,
            "pie": True,
            "save": False,
            "verify": [],
            "max_retries": 1,
        },
    })
    return windows


def _try_understand(query: str, complete) -> dict | None:
    """L1 理解器，含一次重试上限。说不出合法意图（表外动作、解析失败、没 key）返回 None。"""
    from agent.understand import understand

    try:
        intent = understand(query, complete=complete)
        if intent is None:
            intent = understand(query, complete=complete)
        if intent is not None and intent.get("ok"):
            return intent
    except Exception:
        return None
    return None


def plan_windows(query: str, complete=None) -> dict:
    """拆窗口主链（20261015-1705 P1）：
    L0 快路径（离线零成本）：手写规则强命中（每一步都能落地）、learned_routes 已学句式；
    弱命中或未命中 → L1 理解器（一次模型调用，只输出意图 JSON）→ L2 归地成窗口；
    L1 说不出合法意图才回退老规划者（窗口清单 JSON），重试上限 1 次。"""
    query = (query or "").strip()
    from agent.supervise import _plan_rules

    ruled = _plan_rules(query)
    if ruled is not None and _rules_cover(query, ruled):
        windows = _ensure_requested_test(query, windows_from_rules(ruled))
        mark_dangling(windows)
        return {
            "source": "rules",
            "planner_called": False,
            "understand_called": False,
            "route_hit": True,
            "messages": None,
            "windows": windows,
            "wrote_route": False,
        }
    learned = match_learned(query)
    if learned is not None:
        # 已学模板同样不许静默丢需求半句：句里有需求说法而模板没有改码扇 → 弱命中走 L1
        learned_actions = {str(window.get("action") or "") for window in learned}
        if _unbound_demand(query, learned_actions):
            learned = None
    if learned is not None:
        windows = _ensure_requested_test(query, learned)
        mark_dangling(windows)
        return {
            "source": "learned",
            "planner_called": False,
            "understand_called": False,
            "route_hit": True,
            "messages": None,
            "windows": windows,
            "wrote_route": False,
        }
    intent = _try_understand(query, complete)
    understood = intent is not None
    if intent is not None:
        from agent.intent import intent_decision

        windows = _materialize_goals(intent, query)
        # 句尾说了测试但模型把「测试」扔进 unresolved 时，测试扇由代码兜底补上
        windows = _ensure_requested_test(query, _without_unasked_project_compile(query, windows))
        mark_dangling(windows)
        decision = intent_decision(intent)
        decision.update({
            "understand_called": True,
            "route_hit": False,
            "wrote_route": None,
        })
        if intent.get("tokens"):
            decision.update(intent["tokens"])
        return {
            "source": "understood",
            "planner_called": False,
            "understand_called": True,
            "route_hit": False,
            "messages": None,
            "windows": windows,
            "wrote_route": False,
            "intent": decision,
            "intent_full": intent,
        }
    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": query},
    ]
    try:
        text = complete(messages) if complete is not None else _complete_live(messages)
    except Exception as exc:
        return {
            "source": "planner",
            "planner_called": True,
            "understand_called": understood,
            "route_hit": False,
            "messages": messages,
            "planner_text": "",
            "windows": [_paused(f"规划者没有写出窗口清单：{exc}")],
            "wrote_route": False,
        }
    parsed = parse_planner(text)
    if not parsed or not parsed.get("windows"):
        return {
            "source": "planner",
            "planner_called": True,
            "understand_called": understood,
            "route_hit": False,
            "messages": messages,
            "planner_text": text or "",
            "windows": [_paused("规划者没有写出窗口清单。")],
            "wrote_route": False,
        }
    windows = []
    for index, raw in enumerate(parsed["windows"], 1):
        item = _normalize_raw(raw, index)
        windows.append(materialize(
            item["action"],
            item["slots"],
            title=item["title"],
            wid=item["id"],
            depends=item["depends_on"],
            retries=item["max_retries"],
        ))
    windows = _ensure_requested_test(query, _without_unasked_project_compile(query, windows))
    mark_dangling(windows)
    wrote = False
    if windows and all(window.get("realizable") for window in windows):
        wrote = remember_route(query, windows)
    return {
        "source": "planner",
        "planner_called": True,
        "understand_called": understood,
        "route_hit": False,
        "messages": messages,
        "planner_text": text or "",
        "windows": windows,
        "wrote_route": wrote,
    }
