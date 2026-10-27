# agent/plan_windows.py
"""快路径拆不开时，才请规划者列窗口。规划者只看这一句话和动作表。"""
import json
import os
import re
from pathlib import Path

from agent.catalog import CATALOG, DISPOSE_ACTION, allows, note_candidate, system_prompt
from config import task_policy

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


def lexicon_hint(query: str, *, detail: bool = False) -> str:
    """D3：给人读的一句——这段文本被 lexicon（术语→动作映射表）接住了什么。

    detail=False 只给一句短话（进窗口 error 文案，别把整段依据塞进用户看的错误里）；
    detail=True 连 reason / suggested_path 一起给（进候选记录的 summary）。接不住或任何
    异常一律回落空串。"""
    try:
        from agent import lexicon

        rows = lexicon.lookup(query)
        if not rows:
            return ""
        for row in rows:
            if row.get("unplannable") is not True:
                action = str(row.get("action") or "").strip()
                if action:
                    caveat = str(row.get("caveat") or "").strip()
                    head = f"术语命中动作行 {action}"
                    return f"{head}（{caveat}）" if caveat else head
        row = rows[0]
        verbs = "、".join(str(word) for word in (row.get("verbs") or [])[:3])
        reason = str(row.get("reason") or "").strip()
        path = str(row.get("suggested_path") or "").strip()
        parts = [f"{verbs} 无表内动作"]
        if detail and reason:
            parts.append(reason)
        if path:
            parts.append(f"建议路径：{path}")
        return "；".join(part for part in parts if part)
    except Exception:
        return ""


def _lexicon_note(summary: str, query: str) -> dict | None:
    """D3 只读挂接：表外分支的这段文本被 lexicon 接住时，要并进候选记录 / 窗口字段的补充信息。

    - 命中动作行：记「术语可落到这个已有动作」，但**不在这里改动作**——改动作必须走
      `materialize` 里那条原有校验路径（`allows()` 硬门 + 槽位解析），不许绕；
    - 命中 unplannable：把 reason / suggested_path / 动词带进记录。
      带 namespace（blueprint / ui / input / material / animation / niagara / mesh）的行
      不再把窗口收成零窗口；其余表外动作仍不执行。

    任何异常、或这段文本没有任何 lexicon 行接住时返回 None（调用方原样回落，形状不变）。"""
    try:
        from agent import lexicon

        rows = lexicon.lookup(query)
        if not rows:
            return None
        note: dict = {"source": "lexicon"}
        text = lexicon_hint(query, detail=True)
        if text:
            note["summary"] = f"{summary}（lexicon：{text}）"
        for row in rows:
            if row.get("unplannable") is True:
                reason = str(row.get("reason") or "").strip()
                path = str(row.get("suggested_path") or "").strip()
                if reason:
                    note["reason"] = reason
                if path:
                    note["suggested_path"] = path
                note["verbs"] = [str(word) for word in (row.get("verbs") or []) if str(word).strip()]
                break
        return note
    except Exception:
        return None


def _lexicon_retry_action(action: str, scope: str) -> str:
    """D3：表外动作先问一次 lexicon，命中「已有动作」就返回它，否则原样返回 action。

    试映射的文本只取 `scope`（标题 + 槽位值），不拿整句去试：整句里通常还有别的动作
    （「打开编辑器，新建一个关卡」），拿整句试会把一次具体失败换成另一个动作的占位窗，
    那就是可观测的行为变化，超出只读挂接的边界。"""
    try:
        from agent import lexicon

        for text in (str(action or "").strip(), str(scope or "").strip()):
            if not text:
                continue
            found = lexicon.action_for(text)
            if found and found != action:
                return found
    except Exception:
        pass
    return action


def _lexicon_scope(slots: dict, title: str) -> str:
    """D3：给 lexicon 试映射用的那一小段文本——标题 + 各槽位值，**不含整句**（理由见上）。"""
    try:
        parts = [str(title or "").strip()]
        if isinstance(slots, dict):
            for value in slots.values():
                if isinstance(value, (str, int, float)) and str(value).strip():
                    parts.append(str(value).strip())
        return " ".join(part for part in parts if part)
    except Exception:
        return ""


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
    if action == "launch_editor":
        return "启动 UE 编辑器"
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
    if action == "edit_widget_blueprint":
        name = slots.get("name") or slots.get("asset_name") or slots.get("blueprint") or "控件"
        return f"改 {name} 控件图"
    if action == "check_platform_hud":
        return "核对 HUD 合同控件名"
    if action == "move_actor":
        return "移动 Actor"
    if action == "place_actor":
        return "放进场景"
    if action == "place_batch":
        return f"按设计图批量放置 {slots.get('blueprint') or '蓝图'}"
    if action == "remove_batch":
        return f"精确删除 {len(slots.get('targets') or []) or '若干'} 个离群 Actor"
    if action == "bind_blueprint":
        return "绑定蓝图"
    if action == "create_level":
        return f"新建关卡 {slots.get('level') or ''}".strip() or "新建关卡"
    if action == "open_level":
        return f"打开关卡 {slots.get('level') or ''}".strip() or "打开关卡"
    if action == "implement_shooter":
        return f"实现射击玩法 {slots.get('level') or ''}".strip()
    if action == "set_scalar_param":
        return f"覆写 {slots.get('target') or '材质实例'} 的 {slots.get('param') or '参数'}"
    if action == "query_material":
        return f"读 {slots.get('target') or '材质实例'} 的参数"
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
    if (leaf.startswith("BP_") or leaf.startswith("WBP_")) and text.startswith("/Game/"):
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


_LEVEL_NAME_RE = re.compile(r"(?<![A-Za-z0-9_])(L_[A-Za-z0-9_]+)(?![A-Za-z0-9_])")
_LEVEL_PATH_RE = re.compile(r"/Game(?:/[A-Za-z0-9_]+)+")
_LEVEL_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def resolve_level(value) -> dict:
    """关卡槽位 → 唯一一条 /Game 下的关卡包路径。裸名 L_NewTest2 落到 /Game/Maps/L_NewTest2；
    已给 /Game 路径原样用（去掉 .Object 后缀）；名字为空、多于一条、或名字与路径对不上就返回
    error，不猜。超集返回链由 sequence._create_level 复用。"""
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return {"ok": False, "error": "缺少关卡名，不猜。"}
    paths = list(dict.fromkeys(
        match.group(0).split(".", 1)[0].rstrip("/") for match in _LEVEL_PATH_RE.finditer(text)
    ))
    names = list(dict.fromkeys(_LEVEL_NAME_RE.findall(text)))
    if len(paths) > 1 or len(names) > 1:
        return {"ok": False, "error": "关卡名不是唯一的一条，不猜。"}
    if paths:
        package = paths[0]
        name = package.rsplit("/", 1)[-1]
        if names and names[0] != name:
            return {"ok": False, "error": "关卡名和 /Game 路径对不上，不猜。"}
    elif names:
        name = names[0]
        package = f"/Game/Maps/{name}"
    elif _LEVEL_TOKEN_RE.match(text):
        # 规划者只写了个裸名（没带 L_ 前缀）也认，落到 /Game/Maps。
        name = text
        package = f"/Game/Maps/{name}"
    else:
        return {"ok": False, "error": "槽位里没有唯一的关卡名，不猜。"}
    if not name:
        return {"ok": False, "error": "槽位里没有唯一的关卡名，不猜。"}
    return {
        "ok": True,
        "level_name": name,
        "level_path": package,
        "package_path": package,
        "asset_name": name,
        "ref_path": f"{package}.{name}",
    }


def _normalize_slots(action: str, slots: dict) -> dict:
    out = {str(key): value for key, value in slots.items()}
    if action in ("create_level", "open_level"):
        # 规划者可能把关卡名写进别的键：只补一次，认出来的原值不动。
        for alias in ("关卡", "关卡名", "名字", "名称", "name", "level_name", "map", "path"):
            if str(out.get("level") or "").strip():
                break
            if str(out.get(alias) or "").strip():
                out["level"] = out[alias]
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


_GRAPH_NAMESPACES = frozenset({
    "blueprint", "ui", "input", "material", "animation", "niagara", "mesh",
})


def _scope_monolith_action(text: str) -> str:
    """蓝图/图相关的 lexicon 行不再收成零窗口。返回一个已有动作，没有就空串。"""
    try:
        from agent import lexicon
        from agent.catalog import BLUEPRINT_ATTEMPT_ACTIONS

        action = lexicon.action_for(text) or ""
        if action in BLUEPRINT_ATTEMPT_ACTIONS or action == "create_level":
            return action
        for row in lexicon.unplannable(text):
            ns = str(row.get("namespace") or "")
            if ns == "ui":
                return "edit_widget_blueprint"
            if ns == "mesh":
                return "create_level"
            if ns in _GRAPH_NAMESPACES:
                return "compile_blueprint"
    except Exception:
        return ""
    return ""


def _missing_slots(action: str, slots: dict) -> str:
    from agent.catalog import BLUEPRINT_ATTEMPT_ACTIONS, spec_of

    if action in BLUEPRINT_ATTEMPT_ACTIONS:
        # 没有唯一名字也先放行，解析留给执行层的 3 次，不在这里判死。
        return ""
    spec = spec_of(action)
    if not isinstance(spec, dict):
        return "表外动作，不执行。"
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
    if action == "launch_editor":
        # 只启动编辑器：没有槽位，也不解析资产/路径。
        return {"ok": True}
    if action == "compile_project":
        return {"ok": True}
    if action == "check_platform_hud":
        return {"ok": True}
    if action == "integrate_verify":
        return {"ok": True}
    if action == "query_actor":
        return {"ok": True, "actor_name": str(slots.get("name") or "")}
    if action in ("create_level", "open_level"):
        # 关卡名 → 唯一一条 /Game 路径；裸名落 /Game/Maps，已给路径原样。不唯一就暂停。
        return resolve_level(
            slots.get("level") or slots.get("level_path") or slots.get("level_name") or ""
        )
    if action == "implement_shooter":
        found = resolve_level(slots.get("level") or slots.get("level_path") or slots.get("level_name") or "")
        if not found.get("ok"):
            return found
        found["automation_filter"] = "AIAgent.Fps.Hit"
        return found
    if action == "run_automation_test":
        return {"ok": True, "automation_filter": str(slots.get("filter") or "")}
    if action in ("bind_blueprint", "compile_blueprint"):
        from config import allow_blueprint

        if not allow_blueprint():
            return {
                "ok": False,
                "waiting_external": True,
                "external_dep": "UE_ALLOW_BLUEPRINT",
                "error": "UE_ALLOW_BLUEPRINT 不是 true，不改蓝图。",
            }
        token = str(slots.get("blueprint") or slots.get("class") or "").strip()
        if token.startswith("/Game/") or token.startswith("BP_") or token.startswith("WBP_"):
            found = resolve_name(token)
            if found.get("ok"):
                return found
        elif token:
            found = _resolve_cpp(token) if action == "bind_blueprint" else resolve_name(token)
            if found.get("ok") and (found.get("ref_path") or found.get("kind") == "class"):
                if found.get("kind") == "class" and not found.get("ref_path"):
                    from agent.supervise import _indexed_blueprint

                    chosen = _indexed_blueprint(found["class_name"])
                    if chosen and chosen.get("ok"):
                        chosen["kind"] = "blueprint"
                        chosen["class_name"] = found["class_name"]
                        return chosen
                elif found.get("ref_path"):
                    return found
        # 没有唯一路径：窗口仍可执行，执行层用 3 次去查，这里不暂停。
        return {"ok": True}

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

    if action in ("edit_widget_blueprint", "create_widget_blueprint"):
        from config import allow_umg

        if not allow_umg():
            verb = "不改 Widget 图" if action == "edit_widget_blueprint" else "不新建 Widget Blueprint"
            return {
                "ok": False,
                "waiting_external": True,
                "external_dep": "UE_ALLOW_UMG",
                "error": f"UE_ALLOW_UMG 不是 true，{verb}。",
            }
        return _widget_slot_fields(slots)

    if action == "edit_cpp":
        found = resolve_name(str(slots.get("class") or ""))
        if not found.get("ok"):
            return found
        if found.get("kind") != "class":
            return {"ok": False, "error": f"{slots.get('class')} 不是唯一的 C++ 类，不猜。"}
        fields["class_name"] = found["class_name"]
        fields["actor_name"] = found["class_name"]
        return fields

    if action in ("set_scalar_param", "query_material"):
        # 材质实例与蓝图本体的解析都在执行器里做：索引唯一命中，零个或多个就停。
        if action == "set_scalar_param":
            try:
                float(str(slots.get("value") or "").strip())
            except (TypeError, ValueError):
                return {"ok": False, "error": "value 不是唯一的数字，不猜。"}
        return {"ok": True}

    if action == "place_batch":
        # 蓝图与设计图的解析在执行器里做：文件与路径都要唯一命中才动场景
        return {"ok": True}

    if action == "remove_batch":
        # targets 的逐条匹配（名字 + 坐标唯一）在执行器里做：筛不出唯一一条就整批不删
        return {"ok": True}

    slot_name = "blueprint" if action == "place_actor" else "target"
    found = resolve_name(str(slots.get(slot_name) or ""))
    if not found.get("ok"):
        return found
    _merge(fields, found)
    if action == "place_actor" and not fields.get("ref_path"):
        return {"ok": False, "error": f"{slots.get(slot_name)} 没有唯一的蓝图路径，不猜。"}
    return {key: value for key, value in fields.items() if value is not None}


def _widget_folder(raw: str) -> str:
    """把 UI / Game/UI / /game/ui 收成 /Game 路径。空串保持空，不默认成某一份资产。"""
    text = str(raw or "").strip().replace("\\", "/").rstrip("/")
    parts = [part for part in text.split("/") if part]
    if parts and parts[0].lower() == "game":
        parts = parts[1:]
    if not parts:
        return ""
    if len(parts) == 1 and parts[0].lower() == "ui":
        return "/Game/UI"
    return "/Game/" + "/".join(parts)


def _widget_parent(raw: str) -> str:
    text = str(raw or "").strip()
    if not text or text.lower().replace(" ", "") in {"userwidget", "/script/umg.userwidget", "umg.userwidget"}:
        return "/Script/UMG.UserWidget"
    return text


def _widget_slot_fields(slots: dict) -> dict:
    """句子里有唯一 WBP_ 或 /Game 路径就填上。没有也 ok，交给执行层的 3 次。"""
    raw_name = str(slots.get("name") or slots.get("asset_name") or slots.get("blueprint") or "").strip()
    raw_path = str(slots.get("ref_path") or slots.get("asset_path") or "").strip()
    folder = _widget_folder(str(slots.get("folder") or slots.get("folder_path") or ""))
    parent = _widget_parent(str(slots.get("parent") or ""))
    if raw_path.startswith("/Game/"):
        found = resolve_name(raw_path.split(".", 1)[0] if raw_path.rsplit("/", 1)[-1].startswith(("BP_", "WBP_")) else raw_path)
        if found.get("ok") and found.get("ref_path"):
            found["parent"] = parent
            return found
    name = raw_name
    if name.startswith("/Game/"):
        leaf = name.split(".", 1)[0].rsplit("/", 1)[-1]
        if leaf.startswith(("BP_", "WBP_")):
            folder = folder or name.split(".", 1)[0].rsplit("/", 1)[0]
            name = leaf
    if name.startswith(("WBP_", "BP_")):
        if not folder:
            folder = "/Game/UI" if name.startswith("WBP_") else "/Game"
        ref = f"{folder}/{name}.{name}"
        return {
            "ok": True,
            "folder_path": folder,
            "asset_name": name,
            "parent": parent,
            "ref_path": ref,
        }
    return {"ok": True, "parent": parent}


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
        **task_policy(),
    }
    if action == DISPOSE_ACTION:
        window["error"] = "dispose_residual 只由代码追加，这一窗不执行。"
        return window
    scope = _lexicon_scope(slots, window["title"])
    if not allows(action):
        # D3 只读挂接（原地改，不新增第三个闸门）：
        # ① 表外动作先问一次 lexicon，命中**已有动作**就换成它，再走下面同一条原有校验路径
        #    （`allows()` 硬门、_missing_slots、resolve_slots 一个都不绕）；换不动就原样不动。
        # ② 仍是表外时保持「表外不执行」，只把 lexicon 的 reason / suggested_path 并进候选记录
        #    与错误文案；未命中或任何异常都回落成接入前的逐字行为。
        retried = _lexicon_retry_action(action, scope)
        if retried != action and allows(retried):
            action = retried
            window["action"] = action
            if not title:
                window["title"] = _default_title(action, slots)
            window["lexicon_action"] = retried
            scope = _lexicon_scope(slots, window["title"])
        else:
            fallback = _scope_monolith_action(scope) or _scope_monolith_action(action)
            if fallback and allows(fallback):
                action = fallback
                window["action"] = action
                if not title:
                    window["title"] = _default_title(action, slots)
                window["lexicon_action"] = fallback
                scope = _lexicon_scope(slots, window["title"])
    if not allows(action):
        window["error"] = f"表外动作 {action or '空'}，这一窗暂停，不执行。"
        note = _lexicon_note(window["error"], scope)
        summary = str((note or {}).get("summary") or "").strip()
        try:
            if summary:
                note_candidate(action, source="planner", summary=summary)
            else:
                note_candidate(action, source="planner")
        except Exception:
            pass
        if action:
            window["action_candidate"] = action
        if note:
            window["lexicon"] = dict(note)
            hint = lexicon_hint(scope)
            if hint:
                window["error"] = f"表外动作 {action or '空'}，这一窗暂停，不执行。（lexicon：{hint}）"
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
        if resolved.get("waiting_external"):
            window["waiting_external"] = True
            window["external_dep"] = str(resolved.get("external_dep") or "")
            window["blocked"] = False
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
        **task_policy(),
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
        **task_policy(),
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
        **task_policy(),
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
            if action in ("bind_blueprint", "compile_blueprint") and not window.get("waiting_external"):
                # 没有唯一路径不再暂停问人，执行层最多试 3 次。
                window["realizable"] = True
                window["ok"] = True
                window["blocked"] = False
                window.pop("error", None)
                window.pop("needs", None)
            elif not window.get("realizable"):
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
        **task_policy(),
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
    route = {"id": rid, "when": when, "roles": roles, "windows": stored}
    routes.append(route)
    _save_routes(routes)
    try:
        from agent.skill_memory import remember_shadow

        remember_shadow(route)
    except Exception:
        pass
    return True


def remember_intent_route(query: str, intent: dict | None, windows: list[dict], source_run: str = "") -> str | None:
    """P3 L3 学习回写：整句集成成功才由调用方调用。unresolved 非空的意图不回写（半猜测不进缓存）。
    不记资产名：只记 when 触发词、roles、goals（动作 + 槽位角色 + 参数形状）和 depends 形状；
    重放时 token 按角色代入，再走 L2 归地，资产在哪台机器现归地。旧路由 LRU 上限 50，已提升技能上限 200。
    source_run 为空时 promote_from_route 停在 shadow，不提升。"""
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
    existing = next((item for item in routes if str(item.get("id") or "") == rid), None)
    if existing is None:
        existing = {"id": rid, "when": when, "roles": roles, "goals": goals}
        routes.append(existing)
        _save_routes(routes)
    try:
        from agent.skill_memory import promote_from_route

        promote_from_route(existing, source_run)
    except Exception:
        pass
    return rid


def _learned_sources() -> list[dict]:
    """已提升技能在前，旧路由在后。同一 id 只留技能那条。影子技能不参与。"""
    try:
        from agent.skill_memory import promoted_as_routes

        skills = promoted_as_routes()
    except Exception:
        skills = []
    seen = set()
    ordered = []
    for route in list(skills) + load_routes():
        rid = str(route.get("id") or "")
        if rid:
            if rid in seen:
                continue
            seen.add(rid)
        ordered.append(route)
    return ordered


def _skill_meta(route: dict) -> dict:
    rid = str(route.get("id") or "")
    version = 1
    status = "route"
    try:
        from agent.skill_memory import load_skills

        for skill in load_skills():
            if str(skill.get("id") or "") == rid:
                version = int(skill.get("version") or 1)
                status = str(skill.get("status") or "promoted")
                break
    except (TypeError, ValueError, Exception):
        pass
    return {"skill_id": rid, "version": version, "status": status}


def match_learned(query: str, meta: dict | None = None) -> list[dict] | None:
    query = (query or "").strip()
    names = extract_names(query)
    for route in _learned_sources():
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
            windows = _materialize_goals({"goals": goals}, query)
            if meta is not None:
                meta.clear()
                meta.update(_skill_meta(route))
            return windows
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
        if meta is not None:
            meta.clear()
            meta.update(_skill_meta(route))
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
        # 规则路径的「只编译」扇自带 freeze_source；空转算通过只对它们成立，
        # 其余改码扇（edit_cpp）必须真写源码，见 run_windows._run_window 的判定。
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
            **task_policy(pie=True if step.get("pie") else None),
        }
        if step.get("freeze_source"):
            window["judge_first"] = True
            window["freeze_source"] = True
        if action == "launch_editor":
            # 只启动编辑器：不保存、不进 PIE，别让 run 记录写成「PIE=进入」
            window.update(save=False, pie=False)
        if action == "compile_project":
            # 编译本身不进 PIE。窗口 pie 为真会让记录写成「PIE=进入」，哪怕测试被跳过。
            window["pie"] = False
        if step.get("error"):
            window["error"] = step["error"]
        windows.append(window)
        stems_list.append(stems)
        actions.append(action)
    return windows


def _complete_live(messages: list[dict]) -> str:
    from core.llm_call import complete

    response, _record = complete(messages, temperature=0, caller="planner")
    return response.choices[0].message.content or ""


_FILLER_RE = re.compile(
    r"[\s，。、的了后并再以及和把将在里中也就都吧呢啊吗去来让使得可以能够进行普通当前整个这个一下请帮我]+"
)


def _residue_blank(text: str) -> bool:
    return _FILLER_RE.sub("", text or "") == ""


def _strip_owned_spans(clause: str, steps: list[dict]) -> str:
    """抹掉这条半句里、已经被拆出的基础动作接住的那一段。剩下的字另算。"""
    from agent.supervise import (
        _LAUNCH_EDITOR_RE, _LEVEL_CREATE_RE, _PROJECT_COMPILE_RE, _wants_create_level,
    )

    text = clause or ""
    actions = {_action_of(step) for step in steps}
    launches = "launch_editor" in actions or any(step.get("launch_editor") for step in steps)
    if "compile_project" in actions:
        text = _PROJECT_COMPILE_RE.sub(" ", text)
    if "edit_cpp" in actions:
        text = re.sub(r"编译(?:一下)?(?:工程|项目|代码)?|error\s*C\d*|构建", " ", text, flags=re.I)
    if "compile_blueprint" in actions:
        text = re.sub(r"编译|蓝图|BP_[A-Za-z0-9_]+", " ", text)
    if launches and not re.search(r"关卡|地图", clause or ""):
        text = _LAUNCH_EDITOR_RE.sub(" ", text)
    if "create_level" in actions and _wants_create_level(clause):
        text = _LEVEL_CREATE_RE.sub(" ", text)
        text = re.sub(r"名字为|名为|叫做|叫|L_[A-Za-z0-9_]+|/Game(?:/[A-Za-z0-9_]+)+", " ", text)
    return text


def _clauses_accounted(query: str, steps: list[dict]) -> bool:
    """每一半句都要被已经拆出的规则动作接住。没逗号的单句也看剩下的字。

    判别盖不住的剩余文字交给理解器，不在这里按玩法词补动作。"""
    clauses = [part.strip() for part in re.split(r"[，,；;。]", query or "") if part.strip()]
    if not clauses:
        return True
    return all(_clause_accounted(clause, steps) for clause in clauses)


def _clause_accounted(clause: str, steps: list[dict]) -> bool:
    from agent.supervise import _is_cpp_edit, _is_source_fix, _wants_scene_move

    if _residue_blank(_strip_owned_spans(clause, steps)):
        return True
    actions = {_action_of(step) for step in steps}
    if "edit_cpp" in actions and (_is_cpp_edit(clause) or _is_source_fix(clause)):
        return True
    if "move_actor" in actions and _wants_scene_move(clause):
        return True
    if ({"set_pickable", "judge_pickup", "check_pickup"} & actions) and re.search(r"拾取|可交互|标签|交互", clause):
        return True
    if ({"place_actor", "place_blueprint", "place_batch"} & actions) and re.search(r"放进|放入|放置|摆", clause):
        return True
    # 上一扇的槽位续句：名字、L_、/Game。不是新的要求。
    if re.fullmatch(r"[\s名字为叫叫做：:，,._/A-Za-z0-9\-]+", clause) and re.search(r"L_|/Game|名字|名为", clause):
        return True
    # 「其他啥也别干」这种限制，不是还没分配的半句。
    if re.search(r"别|不要|无需|不用", clause) and not re.search(r"关卡|地图|蓝图|编译|测试", clause):
        return True
    return False


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
    # 规则判别盖不住的字，不算强命中。交给理解器分配。
    return _clauses_accounted(query, steps)


def _window_launches(window: dict) -> bool:
    """这扇自己会不会把编辑器拉起来：专门的 launch_editor 扇，或编译/改码扇带的 launch_editor。"""
    if str(window.get("action") or "") == "launch_editor":
        return True
    sources = [window]
    if isinstance(window.get("task"), dict):
        sources.append(window["task"])
    for item in sources:
        if any(bool(item.get(key)) for key in ("launch_editor", "launch_without_source", "wait_mcp")):
            return True
    return False


def _ensure_requested_launch(query: str, windows: list[dict]) -> list[dict]:
    """句子里要求启动编辑器，但窗口清单里没有一扇会启动它时，补一扇 launch_editor。
    已经有编译/改码扇的，按现有约定给那扇挂上 launch 标志（编译通过后启动），不另加一扇。
    （20261022-113402：只要求启动编辑器，整句拆出 0 扇，报「没有窗口」。）"""
    from agent.supervise import _wants_launch_editor

    if not _wants_launch_editor(query):
        return windows
    current = [window for window in (windows or []) if isinstance(window, dict)]
    if any(_window_launches(window) for window in current):
        return windows
    # 「一个动作都没拆出来」的占位窗（blocked、action 为空）不挡住补窗：它只说明上一次拆句失败
    current = [window for window in current if str(window.get("action") or "").strip()]
    compiles = [
        window for window in current
        if str(window.get("action") or "") in _COMPILE_ACTIONS and window.get("ok") is not False
    ]
    if compiles:
        for window in compiles:
            window["launch_editor"] = True
            window["launch_without_source"] = True
            window["wait_mcp"] = True
        return current
    dep = str(current[-1]["id"]) if current else ""
    window = materialize(
        "launch_editor", {},
        title="启动 UE 编辑器", wid=_fresh_window_id(current), depends=[dep], retries=0,
    )
    window.update(save=False, pie=False)
    return current + [window]


def _without_unasked_project_compile(query: str, windows: list[dict]) -> list[dict]:
    """没说编译工程时，规划者写出的 compile_project 直接拿掉。
    留着会变成「这一窗暂停」，再问人要 /Game 路径或类名，问题对不上，后面的窗口也开不了。"""
    from agent.supervise import _is_project_compile

    if _is_project_compile(query):
        return windows
    dropped = {str(window.get("id")) for window in windows if window.get("action") == "compile_project"}
    if not dropped:
        return windows
    kept = []
    for window in windows:
        if str(window.get("id")) in dropped:
            continue
        window["depends_on"] = [str(dep) for dep in (window.get("depends_on") or []) if str(dep) not in dropped]
        kept.append(window)
    if not kept:
        # 全拿掉后不能留空清单：空清单在集成段只报「没有窗口」，看不出是「这句话根本没有
        # 可执行动作」还是「规划者写错了动作被拿掉」。留一扇暂停窗把原因写清楚。
        # （20261022-113402：只要求启动编辑器，规划者写了 compile_project，被拿掉后 0 扇 → 静默失败。）
        return [_paused(
            "这一句没有可执行的动作：只写了 compile_project，但这句没要求编译工程，已拿掉。"
            "只要求启动编辑器时用 launch_editor。"
        )]
    return kept


_COMPILE_ACTIONS = frozenset({"edit_cpp", "compile_project", "compile_blueprint"})


def _fresh_window_id(windows: list) -> str:
    """下一个窗口 id：取现有 W 编号的最大值 +1。不能用 len()+1——中间有扇被拿掉
    （比如没说编译工程时的 compile_project）时 len()+1 会和已有的 id 撞上。"""
    highest = 0
    for window in windows or []:
        if not isinstance(window, dict):
            continue
        found = re.fullmatch(r"W(\d+)", str(window.get("id") or "").strip())
        if found:
            highest = max(highest, int(found.group(1)))
    return f"W{highest + 1}"


def _ensure_requested_create_level(query: str, windows: list[dict]) -> list[dict]:
    """句子里要求新建关卡、但清单里没有 create_level 扇时，由代码按同一 materialize 路径补一扇。
    补在各路径的 _ensure_requested_launch 之后：launch_editor 先起来，建关卡才有地方落。
    （20261024-192858：整句只拆出 launch_editor，「新建关卡 L_NewTest2」那半句静默蒸发。）"""
    from agent.supervise import _level_token, _wants_create_level

    if not _wants_create_level(query):
        return windows
    current = [window for window in (windows or []) if isinstance(window, dict)]
    if any(str(window.get("action") or "") == "create_level" for window in current):
        return windows
    # 「一个动作都没拆出来」的占位窗（blocked、action 为空）不挡住补窗
    current = [window for window in current if str(window.get("action") or "").strip()]
    token = _level_token(query)
    dep = str(current[-1]["id"]) if current else ""
    made = materialize_goal(
        "create_level",
        {"level": token} if token else {},
        title="",
        wid=_fresh_window_id(current),
        depends=[dep] if dep else [],
        retries=1,
    )
    return current + made


def _ensure_requested(query: str, windows: list[dict]) -> list[dict]:
    """句子里点名的启动、建关卡，在规划之后由代码补上。
    测试和其余半句不在这里按词补动作，交给理解器分配。"""
    windows = _ensure_requested_launch(query, windows)
    return _ensure_requested_create_level(query, windows)


def _scope_text(scope: dict) -> str:
    """范围自己的话：标题 + 待办说明 + 交付清单。不带原句，避免把别的范围的需求混进来。"""
    parts = [str(scope.get("title") or ""), str(scope.get("pending_note") or "")]
    deliverables = scope.get("deliverables")
    if isinstance(deliverables, (list, tuple)):
        parts.extend(str(item) for item in deliverables)
    elif deliverables:
        parts.append(str(deliverables))
    return " ".join(part for part in parts if str(part).strip())


def _scope_prefix(scope: dict) -> str:
    """窗口 id 前缀：S + 范围 id + W（范围 B → SBW1）。范围没写 id 时退成 SW。"""
    sid = str(scope.get("id") or "").strip()
    return f"S{sid}W" if sid else "SW"


def _scope_reid(windows: list[dict], prefix: str) -> list[dict]:
    """把窗口 id 换成「范围 id 前缀 + 本调用内序号」，依赖只在本调用返回的列表内重映射，
    指向列表外的依赖一律丢掉（契约：不引用返回列表之外的 id）。每扇都记 goal=True。"""
    mapping = {
        str(window.get("id") or ""): f"{prefix}{index}"
        for index, window in enumerate(windows or [], 1)
    }
    out = []
    for window in windows or []:
        item = dict(window)
        wid = mapping.get(str(window.get("id") or ""), "")
        if not wid:
            continue
        item["id"] = wid
        deps = []
        for dep in window.get("depends_on") or []:
            mapped = mapping.get(str(dep), "")
            if mapped and mapped != wid and mapped not in deps:
                deps.append(mapped)
        item["depends_on"] = deps
        item["goal"] = True
        out.append(item)
    return out


def _ambiguous_filter_window(filters: list[str]) -> dict:
    """多条过滤器都不选。问一次全名，不把任何一条写进槽位。"""
    shown = "、".join(str(name) for name in filters)
    return {
        "id": "W1",
        "title": "跑 Automation",
        "action": "run_automation_test",
        "slots": {},
        "depends_on": [],
        "max_retries": 1,
        "realizable": False,
        "ok": False,
        "blocked": True,
        "error": f"过滤器不是唯一的一条：{shown}。只给一次：r 过滤器全名。",
        **task_policy(),
    }


def scope_windows(scope: dict, query: str = "", complete=None, titles=None) -> list[dict]:
    """范围补窗（T2 接口）：任务书或理解器把「新建关卡 X」这类范围写成 0 扇窗口时，
    代码按同一 materialize 路径补出窗口，窗口带 goal=True。
    规则：「新建/创建/建一个/做一个关卡」→ create_level（名字从范围文字取，
    取不到再从 query 取）；「新建/创建蓝图」→ 复用已有的 create_blueprint 归地
    （supervise._try_blueprint_create + windows_from_rules）。
    「看看 / 检查 / 核对 / 是否有问题」且不含实现、新建、创建、修改、摆、放置：
    归地到唯一的 Automation 过滤器，收成 run_automation_test。
    零条维持 []，多条暂停问过滤器全名，不再调用 complete。
    匹配不上返回 []；任何异常也返回 []，绝不抛出。

    id 契约（T2 依赖）：窗口 id = S<范围 id>W<本调用内序号>，例如范围 B → SBW1、SBW2，
    范围 C → SCW1；范围没写 id 时用 SW1 起。id 只在本调用内唯一，
    depends_on 只引用本次返回列表里的 id。"""
    try:
        scope = scope if isinstance(scope, dict) else {}
        if str(scope.get("id") or "").strip() == "INT" or str(scope.get("kind") or "") == "integration":
            return []
        text = _scope_text(scope)
        if not str(text).strip():
            text = str(query or "")
        prefix = _scope_prefix(scope)
        from agent.supervise import _level_token, _try_blueprint_create, _wants_create_level

        if _wants_create_level(text):
            token = _level_token(text) or _level_token(query)
            made = materialize_goal(
                "create_level",
                {"level": token} if token else {},
                title="",
                wid=f"{prefix}1",
                depends=[],
                retries=1,
            )
            return _scope_reid(made, prefix)
        blueprint_steps = _try_blueprint_create(text)
        if (
            blueprint_steps
            and any(step.get("blocked") for step in blueprint_steps)
            and query
            and not re.search(r"/Game(?:/[A-Za-z0-9_]+)+", text)
        ):
            # 范围自己只写了「新建蓝图」、没写 /Game 路径时，路径提示从原句借一次；
            # 意图仍然只认范围自己的话，别把别的范围的需求混进来。
            widened = _try_blueprint_create(f"{text} {query}")
            if widened and not any(step.get("blocked") for step in widened):
                blueprint_steps = widened
        if blueprint_steps:
            return _scope_reid(windows_from_rules(blueprint_steps), prefix)
        from agent.diagnose import ground_diagnostic, is_diagnostic

        if is_diagnostic(text):
            # 诊断句不再交给分解器。唯一过滤器收成现成的只读动作；多条只问全名；零条维持缺口。
            grounded = ground_diagnostic(text)
            if grounded.get("ok") and str(grounded.get("filter") or "").strip():
                made = materialize_goal(
                    "run_automation_test",
                    {"filter": str(grounded["filter"]).strip()},
                    title="",
                    wid=f"{prefix}1",
                    depends=[],
                    retries=1,
                )
                return _scope_reid(made, prefix)
            names = [str(item) for item in (grounded.get("filters") or []) if str(item).strip()]
            if len(names) > 1:
                return _scope_reid([_ambiguous_filter_window(names)], prefix)
            return []
        # 对不上新建关卡、新建蓝图、只读诊断时，交给分解回调。不按术语命名空间收成某一个动作。
        if callable(complete):
            from agent.decompose import windows_for_clause

            made = windows_for_clause(text, complete, titles if titles is not None else [])
            if made:
                return _scope_reid(made, prefix)
        return []
    except Exception:
        return []


def _gate_scene(query: str, windows: list[dict], criteria: list | None,
                unresolved: list | None = None) -> dict:
    """场景内容闸门：句子里点名要摆进关卡的东西，理解器或已学模板没接住时由代码补窗。
    20261021-221446：理解器把「金币、门、平台的摆放位置」整段扔进 unresolved，
    窗口清单里一扇放置都没有，关卡是空的却仍记 6/7 扇成功。补不出来就进 unmet，不静默。"""
    empty = {"windows": windows, "criteria": criteria, "added": [], "unmet": [],
             "unresolved": [str(item) for item in (unresolved or [])]}
    try:
        from agent.scene_gate import ensure_scene_windows

        return ensure_scene_windows(query, windows, criteria, unresolved)
    except Exception as exc:  # 闸门自己坏了也不能把整句规划打断，但要说出来
        empty["unmet"] = [f"场景内容闸门没跑通：{exc}"]
        return empty


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


_WIDGET_EDIT_TOOLS = (
    "GetWidgets",
    "GetWidgetDescription",
    "GetWidgetTreeDepth",
    "GetNamedSlots",
    "ListWidgetClasses",
    "GetWidgetClassInfo",
    "AddWidget",
    "MoveWidget",
    "RemoveWidget",
    "RenameWidget",
    "ToggleWidgetAsVariable",
    "SetNamedSlotContent",
    "BindToEventProperty",
    "CompileWidgetBlueprint",
)


def widget_edit_task(query: str, wid: str = "W2") -> dict:
    """开关打开时交给图去改句子里那份控件。没有唯一名字也不在这里判死。"""
    from config import allow_umg

    if not allow_umg():
        return {
            "id": wid,
            "ok": False,
            "paused": True,
            "error": "UE_ALLOW_UMG 不是 true，不改 Widget 图。",
        }
    text = (query or "").strip()
    names = list(dict.fromkeys(re.findall(r"(?<![A-Za-z0-9_])(WBP_[A-Za-z0-9_]+)", text)))
    paths = [
        item for item in dict.fromkeys(re.findall(r"/Game(?:/[A-Za-z0-9_]+)+", text))
        if item.rsplit("/", 1)[-1].startswith("WBP_")
    ]
    probe = {}
    if len(paths) == 1:
        probe["ref_path"] = paths[0]
    elif len(names) == 1:
        probe["name"] = names[0]
    fields = _widget_slot_fields(probe)
    ref = str(fields.get("ref_path") or "")
    asset = str(fields.get("asset_name") or "")
    folder = str(fields.get("folder_path") or "")
    named = ref or "句子里的 Widget Blueprint"
    task = {
        "id": wid,
        "action": "edit_widget_blueprint",
        "title": f"改 {asset or '控件'} 图" if asset else "改控件图",
        "query": (
            f"{text}\n"
            f"可以改 Widget Blueprint {named} 的控件树和事件绑定。\n"
            "先读现有控件树，再最小改动。"
            "改完不要再调用工具，让裁判编译并保存这份资产。编译失败会进入 fix。"
            "达到 max_retries 后停止，不要换一张蓝图。"
        ),
        "allow_writes": list(_WIDGET_EDIT_TOOLS),
        **task_policy(),
        "max_retries": 3,
    }
    if ref:
        task["folder_path"] = folder
        task["asset_name"] = asset
        task["ref_path"] = ref
        task["verify"] = [{
            "kind": "blueprint_compile",
            "refPath": ref,
            "toolset": "UMGToolSet.UMGToolSet",
            "tool": "CompileWidgetBlueprint",
        }]
    return task


def _plan_windows_body(query: str, complete=None) -> dict:
    """拆窗口主链（20261015-1705 P1）：
    L0 快路径（离线零成本）：手写规则强命中（每一步都能落地）、learned_routes 已学句式；
    弱命中或未命中 → L1 理解器（一次模型调用，只输出意图 JSON）→ L2 归地成窗口；
    L1 说不出合法意图才回退老规划者（窗口清单 JSON），重试上限 1 次。"""
    query = (query or "").strip()
    from agent.supervise import _plan_rules

    ruled = _plan_rules(query)
    if ruled is not None and _rules_cover(query, ruled):
        windows = _ensure_requested(query, _gate_scene(query, windows_from_rules(ruled), None)["windows"])
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
    learned_meta: dict = {}
    learned = match_learned(query, meta=learned_meta)
    if learned is not None:
        # 已学模板同样不许静默丢需求半句：句里有需求说法而模板没有改码扇 → 弱命中走 L1
        learned_actions = {str(window.get("action") or "") for window in learned}
        if _unbound_demand(query, learned_actions):
            learned = None
    if learned is not None:
        windows = _ensure_requested(query, _gate_scene(query, learned, None)["windows"])
        mark_dangling(windows)
        return {
            "source": "learned",
            "planner_called": False,
            "understand_called": False,
            "route_hit": True,
            "messages": None,
            "windows": windows,
            "wrote_route": False,
            "skill": dict(learned_meta),
        }
    intent = _try_understand(query, complete)
    understood = intent is not None
    if intent is not None:
        from agent.intent import intent_decision

        windows = _materialize_goals(intent, query)
        windows = _without_unasked_project_compile(query, windows)
        # 句子里点名要摆进关卡的东西，理解器扔进 unresolved 时由代码补放置扇（不静默丢需求）
        gated = _gate_scene(query, windows, intent.get("criteria"), intent.get("unresolved"))
        windows = gated["windows"]
        if gated["criteria"] is not None:
            intent["criteria"] = gated["criteria"]
        if gated["added"]:
            intent["scene_gate"] = {"added": list(gated["added"])}
        unresolved = list(gated.get("unresolved") or [])
        unresolved.extend(item for item in gated["unmet"] if item not in unresolved)
        intent["unresolved"] = unresolved[:4]
        # 句尾说了测试、打开关卡或看结果，模型没写进窗口时由代码补上
        windows = _ensure_requested(query, windows)
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
            "action_candidates": list(intent.get("candidates") or []),
        }
    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": query},
    ]
    try:
        from agent.cli_log import status

        status("正在规划窗口")
    except Exception:
        pass
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
    windows = _gate_scene(query, windows, None)["windows"]
    windows = _ensure_requested(query, _without_unasked_project_compile(query, windows))
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


def _stamp_decision(planned: dict) -> None:
    source = str(planned.get("source") or "")
    windows = [item for item in (planned.get("windows") or []) if isinstance(item, dict)]
    actions = [str(item.get("action") or "") for item in windows]
    intent = dict(planned.get("intent") or {})
    if source == "rules":
        planned["intent"] = {
            "actions": actions,
            "unresolved": list(intent.get("unresolved") or []),
            "skill_id": "",
            "version": 0,
            "status": "rules",
        }
        return
    if source == "learned":
        meta = planned.get("skill") if isinstance(planned.get("skill"), dict) else {}
        planned["intent"] = {
            "actions": actions,
            "unresolved": list(intent.get("unresolved") or []),
            "skill_id": str(meta.get("skill_id") or ""),
            "version": int(meta.get("version") or 0),
            "status": str(meta.get("status") or "learned"),
        }
        return
    if not intent and source not in ("understood", "planner"):
        return
    if not intent:
        return
    intent.setdefault("actions", actions)
    intent.setdefault("unresolved", [])
    intent.setdefault("skill_id", "")
    intent.setdefault("version", 0)
    intent.setdefault("status", source or "understood")
    planned["intent"] = intent


def _finish_plan(planned: dict) -> dict:
    if not isinstance(planned, dict):
        return planned
    planned = dict(planned)
    windows = planned.get("windows")
    if isinstance(windows, list):
        from agent.harness import build_criteria

        understood = planned.get("intent_full") if isinstance(planned.get("intent_full"), dict) else {}
        planned["criteria"] = build_criteria(understood.get("criteria"), windows)
    _stamp_decision(planned)
    return planned


def plan_windows(query: str, complete=None) -> dict:
    """拆窗口主链。验收合同取自 L1 的 criteria，没有才按窗口动作类型兜底；不为此再叫一次模型。"""
    return _finish_plan(_plan_windows_body(query, complete))


def _planner_failed(planned: dict) -> bool:
    if str(planned.get("source") or "") != "planner":
        return False
    windows = planned.get("windows") or []
    if not windows:
        return True
    text = " ".join(str(window.get("error") or "") for window in windows if isinstance(window, dict))
    return "规划者没有写出窗口清单" in text


def _unfinished_tail(query: str, ruled: list[dict]) -> dict:
    """弱命中回退的收尾步（WF-3）：手写规则只拆出几个动作，却没有任何一步用带 spec 的验证
    把整句需求收口。标 ok=False / blocked=True，让 run_sequence 暂停问人、退出码非 0，
    不让「平台跳跃整句只拆出 1 步 place_blueprint」这种半拆结果跑完就 return 0。"""
    actions: list[str] = []
    for step in ruled:
        action = _action_of(step)
        if action and action not in actions:
            actions.append(action)
    listed = "、".join(actions) if actions else "空"
    return {
        "id": f"W{len(ruled) + 1}",
        "title": "整句需求还没有收口",
        "action": "",
        "slots": {},
        "depends_on": [],
        "max_retries": 0,
        "realizable": False,
        "ok": False,
        "blocked": True,
        "error": (
            f"手写规则只拆出 {len(ruled)} 步（动作 {listed}），"
            "没有一次带 spec 的验证把整句需求收口，当成未完成，不要报成功。"
        ),
        **task_policy(),
    }


def steps_for_menu2(query: str, complete=None) -> list[dict]:
    """菜单 2 和菜单 3 共用 plan_windows。理解器失败时退回手写步骤，再不行才是老规划者留下的暂停窗。
    回退分支同样要过 _rules_cover（WF-3）：强命中才原样返回规则步骤；弱命中保留可执行步骤，
    末尾补一扇未完成窗，免得半拆的规则结果被当成整句成功。"""
    planned = plan_windows(query, complete=complete)
    if _planner_failed(planned):
        from agent.supervise import _plan_rules

        ruled = _plan_rules(query)
        if ruled:
            if _rules_cover(query, ruled):
                return ruled
            return list(ruled) + [_unfinished_tail(query, ruled)]
    steps = []
    for window in planned.get("windows") or []:
        if not isinstance(window, dict):
            continue
        task = window.get("task")
        if isinstance(task, dict):
            steps.append(task)
        else:
            steps.append(window)
    return steps
