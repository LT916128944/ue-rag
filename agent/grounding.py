# agent/grounding.py
"""L2 归地器（20261015-1705 P2）：asset 槽位统一解析，纯代码，不猜。
把 2026-10-15 在放置路由上验证过的三段逻辑提升到这里：
unique 命中（索引/磁盘）→ linked；miss 且 C++ 索引可推父类（或句中给了 /Game 路径）→ create_planned，
自动插「创建蓝图」前置扇；都推不出 → pause_r，只问一次 r。
模型只提供原始 token（BP_ 名、类名、/Game 路径），路径、父类、refPath 一律在这里归地。"""

def _pause(kind: str, note: str) -> dict:
    try:
        from agent.clarify_policy import apply_note

        note = apply_note(kind, note)
    except Exception:
        pass
    return {"plan": "pause_r", "ok": False, "fields": None, "create": None,
            "error": note, "needs": kind}


def _linked(fields: dict) -> dict:
    fresh = True
    note = ""
    try:
        from agent.index_refresh import index_is_fresh

        fresh = index_is_fresh()
        if not fresh:
            note = "资产索引已过期。这条路径仍用当前唯一命中，不改猜别的资产。"
    except Exception:
        fresh = True
    return {"plan": "linked", "ok": True, "fields": fields, "create": None,
            "error": "", "needs": "", "index_fresh": fresh, "index_note": note}


def _unique_hit(name: str):
    """索引或磁盘唯一命中 → 蓝图字段；不唯一返回错误串；没有返回 None。"""
    from agent.plan_windows import _blueprint_hits, _disk_blueprint, _fields_from_ref

    hits = _blueprint_hits(name)
    if len(hits) > 1:
        return f"{name} 的蓝图不是唯一的，不猜。"
    if len(hits) == 1:
        return _fields_from_ref(name, str(hits[0].get("object_path") or ""))
    disk = _disk_blueprint(name)
    if disk is None:
        return None
    if disk.get("ok") is False:
        return str(disk.get("error") or f"{name} 的蓝图不是唯一的，不猜。")
    return disk


def _verified_parent(stem: str) -> str:
    """C++ 索引里正好一个 A 类对应这个 stem 时返回反射名坐标（/Script/...），否则空。"""
    from agent.plan_windows import _resolve_cpp
    from agent.supervise import script_class_path

    found = _resolve_cpp(stem)
    if found.get("ok") is not True:
        return ""
    class_name = str(found.get("class_name") or "")
    if not class_name.startswith("A"):
        return ""
    return script_class_path(class_name)


def _create_step(asset_name: str, parent: str, folder: str = "") -> dict:
    """create_blueprint 前置扇的步骤字段。没有目录时仍卡在 needs=path 等一次 r。"""
    step = {
        "ok": bool(folder),
        "blocked": not folder,
        "needs": "" if folder else "path",
        "title": "创建蓝图",
        "action": "create_blueprint",
        "asset_name": asset_name,
    }
    if parent:
        step["parent"] = parent
    if folder:
        step.update({
            "folder_path": folder,
            "actor_name": asset_name,
            "ref_path": f"{folder}/{asset_name}.{asset_name}",
            "asset_path": f"{folder}/{asset_name}",
        })
    else:
        step["error"] = "索引里没有这张蓝图，也没有 /Game 路径。只给一次：r /Game/某目录，或 r /Game/某目录/BP_名字。"
    return step


def link_asset(token, *, paths=None) -> dict:
    """blueprint 槽位归地。返回 plan=
    linked         唯一命中，fields 里是完整蓝图字段，直接落窗；
    create_planned 命中不了但父类可推（或给了 /Game 路径）：create 是「创建蓝图」前置扇
                   步骤字段（缺目录时自身 needs=path 等一次 r），fields 是目标扇蓝图字段
                   （可能为 None，表示目标扇同样等这次 r）；
    pause_r        都推不出：ok=False，needs/error 说明只问一次 r。"""
    from agent.plan_windows import _resolve_game_path
    from agent.sequence import apply_blueprint_path

    text = str(token or "").strip()
    raw_paths = [str(item).strip() for item in (paths or []) if str(item).strip().startswith("/Game/")]
    folder_hint = raw_paths[0] if len(raw_paths) == 1 else ""

    if not text and not folder_hint:
        return _pause("path", "槽位是空的。只给一次：r /Game/某目录，或 r /Game/某目录/BP_名字。")

    # token 是 /Game 路径：拆出资产名与目录
    if text.startswith("/Game/"):
        resolved = _resolve_game_path(text)
        if resolved.get("ok") is not True:
            return _pause("path", "路径里没有唯一的蓝图名，不猜。只给一次：r /Game/某目录/BP_名字。")
        name = str(resolved.get("asset_name") or "")
        size = _unique_hit(name)
        if isinstance(size, str):
            return _pause("blueprint", size + " 只给一次：r /Game/某目录/BP_名字。")
        if size is not None:
            return _linked(size)
        parent = _verified_parent(name[3:] if name.startswith("BP_") else name)
        create = _create_step(name, parent, folder_hint or str(resolved.get("folder_path") or ""))
        if create.get("parent", "") == "" and name.startswith("BP_"):
            create["parent"] = str(resolved.get("parent") or "")
        return {"plan": "create_planned", "ok": True, "fields": _target_fields(create), "create": create,
                "error": "", "needs": ""}

    if text.startswith("BP_"):
        size = _unique_hit(text)
        if isinstance(size, str):
            return _pause("blueprint", size + " 只给一次：r /Game/某目录/BP_名字。")
        if size is not None:
            return _linked(size)
        stem = text[3:]
        parent = _verified_parent(stem)
        if folder_hint:
            filled = apply_blueprint_path({"asset_name": text, **({"parent": parent} if parent else {})}, folder_hint)
            if filled is None:
                return _pause("path", "写不出唯一的蓝图 refPath，不猜。只给一次：r /Game/某目录/BP_名字。")
            # parent 空时 apply_blueprint_path 会按 BP_ 名给反射名兜底，沿用旧行为
            create = _create_step(text, str(filled.get("parent") or parent), str(filled.get("folder_path") or ""))
            return {"plan": "create_planned", "ok": True, "fields": _target_fields(create), "create": create,
                    "error": "", "needs": ""}
        if not parent:
            return _pause("path", f"索引里没有 {text}，也推不出父类，不猜。只给一次：r /Game/某目录，或 r /Game/某目录/BP_名字。")
        return {"plan": "create_planned", "ok": True, "fields": None, "create": _create_step(text, parent),
                "error": "", "needs": ""}

    # token 是 C++ 类名（A/U/F 前缀）
    if text[:1] in ("A", "U", "F") and len(text) > 1 and text[1].isupper():
        from agent.plan_windows import _resolve_cpp
        from agent.supervise import _bare_class, _indexed_blueprint, script_class_path

        found = _resolve_cpp(text)
        if found.get("ok") is not True:
            return _pause("path", f"{text} 在 C++ 索引里不是唯一的一条，不猜。只给一次：r /Game/某目录，或 r /Game/某目录/BP_名字。")
        class_name = str(found.get("class_name") or "")
        chosen = _indexed_blueprint(class_name)
        if chosen and chosen.get("ok") is True:
            return _linked(chosen)
        if chosen and chosen.get("ok") is False:
            return _pause("blueprint", str(chosen.get("error") or f"{class_name} 的蓝图不是唯一的，不猜。"))
        asset = "BP_" + _bare_class(class_name)
        parent = script_class_path(class_name)
        if folder_hint:
            create = _create_step(asset, parent, folder_hint)
            return {"plan": "create_planned", "ok": True, "fields": _target_fields(create), "create": create,
                    "error": "", "needs": ""}
        return {"plan": "create_planned", "ok": True, "fields": None, "create": _create_step(asset, parent),
                "error": "", "needs": ""}

    return _pause("blueprint", f"{text or '空槽位'} 推不出唯一的蓝图，不猜。只给一次：r /Game/某目录/BP_名字。")


def _target_fields(create: dict) -> dict | None:
    """create 前置扇落地后目标扇要带的蓝图字段；create 还在等目录时目标同样等。"""
    if not create or create.get("ok") is not True:
        return None
    fields = {key: value for key, value in create.items() if key in (
        "folder_path", "asset_name", "actor_name", "ref_path", "asset_path", "parent")}
    fields["ok"] = True
    return fields