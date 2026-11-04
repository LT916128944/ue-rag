# core/monolith_bridge.py
"""U3a（20261027 升级计划）：monolith 后端下场景/资产原语的桥。

sequence 的场景家族按 official 原语（SceneTools / ActorTools / ObjectTools /
AssetTools / BlueprintTools / MaterialInstanceTools）书写。backend=monolith 时这些
调用原本在 exec_tool 入口就被拒绝（错误原文 `UE_EDITOR_BACKEND=monolith`），
20261027-180955 的四扇 place_batch 全败就是这么来的：10-26 切后端只迁了蓝图类
动作与 open_level / implement_shooter 等少数分支。

这里不重写动作：exec_tool 在写策略检查之后、official 客户端之前调本模块，把
`(toolset, tool, args)` 翻译成 monolith 的 `(namespace, action, params)`。写策略
语义不变——policy 检查仍按 official 原语名执行，翻译只发生在放行之后。

映射只收已验证的 monolith action（源码 Docs/specs 与 implement_shooter /
20261026-1142 实跑）：

- ``mesh.get_actor_info``   label 精确匹配，命中多条报歧义，找不到报 Actor not found
- ``blueprint.spawn_blueprint_actor``  蓝图资产路径 + label + properties（无 snap）
- ``mesh.snap_to_floor``    spawn 后补地面吸附（对齐 official 的 snap_to_ground）
- ``mesh.move_actor`` / ``mesh.delete_actors`` / ``mesh.set_actor_properties``(tags)
- ``blueprint.get_parent_class``  活体读：父类读得出 = 资产在（不查 stale 索引）
- ``blueprint.create_blueprint`` / ``blueprint.reparent_blueprint``
- ``material.get_material_parameters`` / ``material.set_instance_parameter``
- ``editor.save_packages``
- U-UMG（20261029）：控件图家族走 ``ui`` 命名空间的设计器树 action；控件事件绑定
  （official ``BindToEventProperty``）走 ``blueprint.add_node`` 的
  ``node_type=ComponentBoundEvent``；``AddWidget`` 的 ``childIndex`` 折成一次
  ``ui.set_slot_property`` 的 ``z_order``（add_widget 本身 append-only）。

表里没有的 official 原语一律结构化拒绝（`UNSUPPORTED`），明确说「后端不支持」，
不猜、不回退 8000。其中 `SetNamedSlotContent` / `GetWidgetClassInfo` 是真缺口，
原因里写清「归入人工清单」（`MANUAL_GAPS` / `manual_gap_tools()`）。纯函数部分
不连编辑器，好离线测。
"""
from __future__ import annotations

import json

# official 原语名 → (monolith 命名空间, action)。键只取 tool 名——本仓库用到的
# 这些 tool 名在各 toolset 之间不重名。
MAPPED = {
    "find_actors": ("mesh", "get_actor_info"),
    "add_to_scene_from_asset": ("blueprint", "spawn_blueprint_actor"),
    "remove_from_scene": ("mesh", "delete_actors"),
    "get_actor_transform": ("mesh", "get_actor_info"),
    "set_actor_transform": ("mesh", "move_actor"),
    "add_tag": ("mesh", "set_actor_properties"),
    "find_assets": ("blueprint", "get_parent_class"),
    "exists": ("blueprint", "get_parent_class"),
    "create": ("blueprint", "create_blueprint"),
    "set_parent": ("blueprint", "reparent_blueprint"),
    "get_parent": ("blueprint", "get_parent_class"),
    "get_scalar_parameter": ("material", "get_material_parameters"),
    "set_scalar_parameter": ("material", "set_instance_parameter"),
    "save_assets": ("editor", "save_packages"),
    # U-UMG（20261029）：控件图家族。official 的 UMGToolSet 原语在 monolith 下
    # 一律走 ui 命名空间的设计器树 action；参数名/返回形状差异由 build_params /
    # shape_result 分别收口。
    "GetWidgets": ("ui", "get_widget_tree"),
    "GetWidgetTreeDepth": ("ui", "get_widget_tree"),
    "GetNamedSlots": ("ui", "get_widget_tree"),
    "GetWidgetDescription": ("ui", "get_widget_tree"),
    "AddWidget": ("ui", "add_widget"),
    "MoveWidget": ("ui", "move_widget"),
    "RemoveWidget": ("ui", "remove_widget"),
    "RenameWidget": ("ui", "rename_widget"),
    "ToggleWidgetAsVariable": ("ui", "set_widget_is_variable"),
    "ListWidgetClasses": ("ui", "list_widget_types"),
    "CompileWidgetBlueprint": ("ui", "compile_widget"),
    # 20261029 §2 残差 1：控件事件绑定**有** monolith 落地路径，不再是「无 action」。
    # `blueprint.add_node` 的 node_type=ComponentBoundEvent（MonolithBlueprintNodeActions.cpp
    # 约 1417 行）内部走 FindComponentProperty + FindMulticastDelegateProperty，再用
    # UK2Node_ComponentBoundEvent::InitializeComponentBoundEventParams 生成绿色事件入口节点；
    # FindComponentProperty 的注释明确「UMG BP 里命名的控件」也命中，所以 official 的
    # widget(propertyName) + event(eventName) 正好能映射过去。重复绑定由它自己报错
    # （FindBoundEventForComponent，约 1447 行），我们不重复实现。
    "BindToEventProperty": ("blueprint", "add_node"),
}

# monolith 没有一等对应物的 official 原语。拒绝要带原因，让扇级错误可读、
# 覆盖矩阵可断言——不是笼统的 `UE_EDITOR_BACKEND=monolith`。
UNSUPPORTED = {
    "get_properties": "monolith 没有实例属性读入口（ObjectTools.get_properties 无对应 action）",
    "set_properties": "monolith 没有实例属性写入口（ObjectTools.set_properties 无对应 action）",
    "get_current_level": "monolith 没有 get_current_level；保存路径走 list_dirty_packages + save_packages",
    # 20261029 §2 残差 2：这两条 monolith 确无对应 action（已核 ui 命名空间全表）。
    # 不编替代品——写清能力缺口并**归入人工清单**，让人在编辑器里手工做，而不是
    # 让模型反复猜一个不存在的 action。
    "SetNamedSlotContent": (
        "monolith 没有命名槽内容写入口（NamedSlot 不是 UPanelWidget，ui.add_widget 的 "
        "AddChild 进不去；ui 命名空间无 set_named_slot_content 之类的 action）→ 归入人工清单："
        "在 UMG 设计器里把内容控件拖进 NamedSlot，或由插件补 action 后再开"
    ),
    "GetWidgetClassInfo": (
        "monolith 没有类级查询（ui.list_widget_properties 必须给 asset_path + widget_name，"
        "只能查 WBP 里的控件实例；无 get_widget_class_info/get_class_info 类 action）→ "
        "归入人工清单：类信息在编辑器 Details/类选择器里看，或由插件补类级 action 后再开"
    ),
    "StartPIE": "monolith 的 PIE 是 run_pie_smoke 会话语义，不是 StartPIE/StopPIE 原语对",
    "StopPIE": "monolith 的 PIE 是 run_pie_smoke 会话语义，不是 StartPIE/StopPIE 原语对",
}

# 归入人工清单的能力缺口：不是「后端不支持」，是「monolith 没有这条 action，交人在
# 编辑器里做」。消费方（扇级报告 / 人工清单）按这个集合展示，不比字符串。
MANUAL_GAPS = frozenset({"SetNamedSlotContent", "GetWidgetClassInfo"})


_NOT_FOUND_TOKENS = ("Actor not found", "Blueprint not found", "Failed to load", "not found")


def _leaf(ref) -> str:
    """refPath / 裸名统一成 label：`L_NewTest.Coin_1` → `Coin_1`。"""
    text = str(ref or "").strip()
    if not text:
        return ""
    return text.rsplit(".", 1)[-1].rsplit("/", 1)[-1]


def _vector(loc) -> list[float] | None:
    """official 的 {x,y,z} 或 monolith 的 [x,y,z] 统一成 [x,y,z]；读不出返回 None。"""
    if isinstance(loc, dict):
        try:
            return [float(loc["x"]), float(loc["y"]), float(loc["z"])]
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(loc, (list, tuple)) and len(loc) >= 3:
        try:
            return [float(loc[0]), float(loc[1]), float(loc[2])]
        except (TypeError, ValueError):
            return None
    return None


def _loc_dict(raw) -> dict | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        return None
    try:
        return {"x": float(raw[0]), "y": float(raw[1]), "z": float(raw[2])}
    except (TypeError, ValueError):
        return None


def _data_dict(result: dict) -> dict:
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def _asset_path(ref) -> str:
    """控件/蓝图 refPath → 包路径：`/Game/UI/WBP_X.WBP_X` → `/Game/UI/WBP_X`。

    控件家族与 `_leaf` 的区别：monolith 的 blueprint/ui action 要的是**包路径**，
    传裸名会得到 `Blueprint not found: WBP_X`（20261029-004500 实测）。只有 mesh 的
    actor 家族才用 label。已经是不带对象的 `/Game/...` 时原样返回。"""
    text = str(ref or "").strip()
    if not text:
        return ""
    if text.startswith("/Game/"):
        leaf = text.rsplit("/", 1)[-1]
        if "." in leaf:
            return text.rsplit(".", 1)[0]
        return text
    return text


def _widget_name(ref) -> str:
    """控件名的 refPath / 裸名都取最后一段（`WBP_X.WidgetTree.txt_Ammo` → `txt_Ammo`）。"""
    return _leaf(ref)


def _class_token(ref) -> str:
    """控件类 → monolith 认的类令牌。

    `TextBlock` 原样；`/Script/UMG.TextBlock` 取 `TextBlock`；
    `/Script/UMG.UserWidget` 这类取叶子名。"""
    return _leaf(ref)


def _widget_class_from(args: dict) -> str:
    for key in ("widgetClass", "widget_class", "class", "widgetType"):
        raw = args.get(key)
        if isinstance(raw, dict):
            token = _class_token(raw.get("refPath") or raw.get("path") or "")
            if token:
                return token
        elif str(raw or "").strip():
            return _class_token(raw)
    return ""


def _text_arg(args: dict, *keys: str) -> str:
    """按优先级取第一个非空字符串参数（别名兼容）。dict 形态取 refPath/path/name。"""
    for key in keys:
        raw = args.get(key)
        if isinstance(raw, dict):
            token = str(raw.get("refPath") or raw.get("path") or raw.get("name") or "").strip()
            if token:
                return token
        elif raw is not None and str(raw).strip():
            return str(raw).strip()
    return ""


def _asset_ref(args: dict, *keys: str) -> str:
    """多个别名里取第一个能当包路径用的值（控件/蓝图引用）。"""
    for key in keys:
        raw = args.get(key)
        if isinstance(raw, dict):
            raw = raw.get("refPath") or raw.get("path") or ""
        text = _asset_path(raw)
        if text:
            return text
    return ""


def _input_position(args: dict) -> list | None:
    """位置：数组原样（已归一成 float），object 形态转 [x, y]；读不出返回 None。"""
    raw = args.get("position", args.get("pos"))
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            return [float(raw[0]), float(raw[1])]
        except (TypeError, ValueError):
            return None
    if isinstance(raw, dict):
        try:
            return [float(raw.get("x", 0)), float(raw.get("y", 0))]
        except (TypeError, ValueError):
            return None
    return None


def build_params(tool: str, args: dict) -> dict:
    """official 参数形状 → monolith 参数形状。纯函数。"""
    args = args or {}
    if tool == "find_actors":
        return {"actor_name": str(args.get("name") or "")}
    if tool == "add_to_scene_from_asset":
        loc = _vector((args.get("xform") or {}).get("location"))
        params: dict = {
            "blueprint": str(args.get("asset_path") or ""),
            "location": loc or [0.0, 0.0, 0.0],
        }
        name = str(args.get("name") or "").strip()
        if name:
            params["label"] = name
        props = args.get("properties")
        if isinstance(props, dict) and props:
            params["properties"] = props
        return params
    if tool == "remove_from_scene":
        ref = _leaf((args.get("actor") or {}).get("refPath"))
        return {"actor_names": [ref]}
    if tool == "get_actor_transform":
        return {"actor_name": _leaf((args.get("actor") or {}).get("refPath"))}
    if tool == "set_actor_transform":
        loc = _vector((args.get("xform") or {}).get("location"))
        return {
            "actor_name": _leaf((args.get("actor") or {}).get("refPath")),
            "location": loc or [0.0, 0.0, 0.0],
        }
    if tool == "find_assets":
        folder = str(args.get("folder_path") or "/Game").rstrip("/")
        name = str(args.get("name") or "").strip()
        return {"asset_path": f"{folder}/{name}" if name else folder}
    if tool == "exists":
        return {"asset_path": str(args.get("asset_path") or "")}
    if tool == "create":
        folder = str(args.get("folder_path") or "/Game").rstrip("/")
        name = str(args.get("asset_name") or "").strip()
        parent = _leaf((args.get("asset_type") or {}).get("refPath"))
        params = {"save_path": f"{folder}/{name}"}
        if parent:
            params["parent_class"] = parent
        return params
    if tool == "set_parent":
        return {
            "asset_path": _asset_path((args.get("blueprint") or {}).get("refPath")),
            "new_parent_class": _class_token((args.get("parent_class") or {}).get("refPath")),
        }
    if tool == "get_parent":
        return {"asset_path": _asset_path((args.get("blueprint") or {}).get("refPath"))}
    # ---- U-UMG：控件图家族 ----
    if tool in ("GetWidgets", "GetWidgetTreeDepth", "GetNamedSlots", "GetWidgetDescription"):
        ref = (args.get("widgetBlueprint") or args.get("blueprint") or {})
        return {"asset_path": _asset_path(ref.get("refPath") if isinstance(ref, dict) else ref)}
    if tool == "AddWidget":
        ref = (args.get("widgetBlueprint") or args.get("blueprint") or {})
        params: dict = {
            "asset_path": _asset_path(ref.get("refPath") if isinstance(ref, dict) else ref),
            "widget_class": _widget_class_from(args),
        }
        name = str(
            args.get("widgetDisplayName") or args.get("widget_name") or args.get("displayName") or ""
        ).strip()
        if name:
            params["widget_name"] = name
        parent = args.get("parentWidget") or args.get("parent_name") or args.get("parent")
        if parent:
            params["parent_name"] = _widget_name(
                parent.get("refPath") if isinstance(parent, dict) else parent
            )
        for key, target in (("position", "position"), ("size", "size"),
                            ("anchor_preset", "anchor_preset")):
            if isinstance(args.get(key), dict) and args.get(key):
                params[target] = args[key]
        for key in ("h_align", "v_align"):
            if str(args.get(key) or "").strip():
                params[key] = str(args[key])
        if isinstance(args.get("auto_size"), bool):
            params["auto_size"] = args["auto_size"]
        # childIndex 不在这里消费：ui.add_widget 的设置器里没有 child_index/z_order
        # （AddChild append-only），塞进去只会被 K3 当未知键软警告后忽略=还是静默丢弃。
        # 顺序由 `slot_order_params` 折成一次 ui.set_slot_property 的 z_order，见 exec_bridge。
        return params
    if tool == "RemoveWidget":
        ref = (args.get("widgetBlueprint") or args.get("blueprint") or {})
        widget = args.get("widget") or args.get("widget_name") or ""
        return {
            "asset_path": _asset_path(ref.get("refPath") if isinstance(ref, dict) else ref),
            "widget_name": _widget_name(widget.get("refPath") if isinstance(widget, dict) else widget),
        }
    if tool == "MoveWidget":
        ref = (args.get("widgetBlueprint") or args.get("blueprint") or {})
        widget = args.get("widget") or args.get("widget_name") or ""
        parent = args.get("newParent") or args.get("new_parent") or args.get("new_parent_name") or ""
        return {
            "asset_path": _asset_path(ref.get("refPath") if isinstance(ref, dict) else ref),
            "widget_name": _widget_name(widget.get("refPath") if isinstance(widget, dict) else widget),
            "new_parent_name": _widget_name(parent.get("refPath") if isinstance(parent, dict) else parent),
        }
    if tool == "RenameWidget":
        ref = (args.get("widgetBlueprint") or args.get("blueprint") or {})
        old = args.get("widget") or args.get("old_name") or args.get("widget_name") or ""
        new = args.get("newDisplayName") or args.get("new_name") or ""
        return {
            "wbp_path": _asset_path(ref.get("refPath") if isinstance(ref, dict) else ref),
            "old_name": _widget_name(old.get("refPath") if isinstance(old, dict) else old),
            "new_name": str(new).strip(),
        }
    if tool == "ToggleWidgetAsVariable":
        ref = (args.get("widgetBlueprint") or args.get("blueprint") or {})
        widget = args.get("widget") or args.get("widget_name") or ""
        return {
            "wbp_path": _asset_path(ref.get("refPath") if isinstance(ref, dict) else ref),
            "widget_name": _widget_name(widget.get("refPath") if isinstance(widget, dict) else widget),
            "is_variable": bool(args.get("bIsVariable", args.get("is_variable", True))),
        }
    if tool == "ListWidgetClasses":
        params = {}
        if str(args.get("filter") or "").strip():
            params["filter"] = str(args["filter"]).strip()
        return params
    if tool == "CompileWidgetBlueprint":
        ref = (args.get("widgetBlueprint") or args.get("blueprint") or {})
        return {"asset_path": _asset_path(ref.get("refPath") if isinstance(ref, dict) else ref)}
    # ---- 控件事件绑定（20261029 §2 残差 1） ----
    if tool == "BindToEventProperty":
        # official 形状：widgetBlueprint(widgetBlueprint 引用) + propertyName(控件名) +
        # eventName(委托名) + propertyClass(声明类，monolith 侧不需要)。
        # monolith 形状：blueprint.add_node 的 ComponentBoundEvent 两支参数。
        params: dict = {
            "asset_path": _asset_ref(
                args, "widgetBlueprint", "widget_blueprint", "blueprint", "asset_path",
                "wbp_path", "widget_blueprint_path"),
            "node_type": "ComponentBoundEvent",
            "component_name": _widget_name(_text_arg(
                args, "propertyName", "property_name", "component_name", "componentName",
                "widget", "widgetName", "widget_name")),
            "delegate_property_name": _text_arg(
                args, "eventName", "event_name", "delegate_property_name", "delegateName",
                "delegate_name", "delegate", "event"),
        }
        graph = _text_arg(args, "graphName", "graph_name", "graph")
        if graph:
            params["graph_name"] = graph
        position = _input_position(args)
        if position:
            params["position"] = position
        return params
    if tool == "get_scalar_parameter":
        return {"asset_path": _leaf((args.get("instance") or {}).get("refPath"))}
    if tool == "set_scalar_parameter":
        return {
            "asset_path": _leaf((args.get("instance") or {}).get("refPath")),
            "parameter_name": str(args.get("name") or ""),
            "scalar_value": args.get("value"),
        }
    if tool == "save_assets":
        return {"packages": [str(one) for one in (args.get("asset_paths") or [])]}
    return {}


def shape_result(tool: str, args: dict, result: dict) -> dict:
    """monolith 返回 → official 消费方认得的形状。翻译失败按失败透传，不编成功。"""
    if not result.get("ok"):
        err = str(result.get("error") or "")
        if tool in ("find_actors", "find_assets", "exists") and any(
                token in err for token in _NOT_FOUND_TOKENS):
            # 「不存在」不是查询失败：find_actors 的空命中、find_assets 的 present=False
            if tool == "find_actors":
                return {"ok": True, "data": []}
            if tool == "exists":
                return {"ok": True, "data": {"returnValue": False}}
            return {"ok": True, "data": []}
        return {"ok": False, "error": err or "monolith 调用失败"}
    data = _data_dict(result)
    if tool == "find_actors":
        row = {
            "refPath": str(data.get("label") or data.get("name") or ""),
            "location": _loc_dict(((data.get("transform") or {}).get("location"))),
        }
        tags = data.get("tags")
        if isinstance(tags, list):
            row["tags"] = tags
        return {"ok": True, "data": [row]}
    if tool == "get_actor_transform":
        loc = _loc_dict(((data.get("transform") or {}).get("location")))
        if loc is None:
            return {"ok": False, "error": "get_actor_info 返回里没有 location"}
        return {"ok": True, "data": {"location": loc}}
    if tool == "set_actor_transform":
        return {"ok": True, "data": {"success": True}}
    if tool == "remove_from_scene":
        return {"ok": True, "data": {"success": True}}
    if tool == "add_to_scene_from_asset":
        return {"ok": True, "data": data}
    if tool in ("find_assets",):
        # get_parent_class 读得出父类 = 资产在：给 find_assets 消费方一条 refPath
        return {"ok": True, "data": [{"refPath": str(args.get("_asset_path") or "")}]}
    if tool == "exists":
        return {"ok": True, "data": {"returnValue": True}}
    if tool in ("create", "set_parent", "save_assets"):
        return {"ok": True, "data": data}
    if tool == "get_parent":
        parent = data.get("parent_class")
        if isinstance(parent, dict):
            return {"ok": True, "data": {"returnValue": {"refPath": str(parent.get("path") or "")}}}
        return {"ok": True, "data": {"returnValue": {}}}
    if tool == "get_scalar_parameter":
        want = str((args or {}).get("name") or "")
        for row in data.get("scalar_parameters") or []:
            if isinstance(row, dict) and str(row.get("name") or "") == want:
                value = row.get("value")
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    return {"ok": True, "data": {"returnValue": float(value)}}
        return {"ok": False, "error": f"{want} 读不到数值"}
    # ---- U-UMG：控件图家族 ----
    if tool in ("GetWidgets", "GetWidgetDescription", "GetWidgetTreeDepth", "GetNamedSlots"):
        rows = _flatten_widget_tree(data.get("root"), data)
        if tool == "GetWidgets":
            return {"ok": True, "data": rows}
        if tool == "GetWidgetDescription":
            start = _widget_name(
                (args.get("startWidget") or {}).get("refPath")
                if isinstance(args.get("startWidget"), dict) else args.get("startWidget")
            )
            picked = _subtree_rows(rows, start) if start else rows
            if start and not picked:
                return {"ok": False, "error": f"控件树里没有 {start}"}
            return {"ok": True, "data": picked}
        if tool == "GetWidgetTreeDepth":
            return {"ok": True, "data": {"returnValue": max((int(r.get("depth") or 0) for r in rows), default=0) + 1}}
        if tool == "GetNamedSlots":
            slots = [r for r in rows if str(r.get("class") or "").endswith("NamedSlot")]
            return {"ok": True, "data": slots}
    if tool in ("AddWidget", "MoveWidget", "RemoveWidget", "RenameWidget",
                "ToggleWidgetAsVariable", "CompileWidgetBlueprint"):
        return {"ok": True, "data": data}
    if tool == "BindToEventProperty":
        # `blueprint.add_node` 的返回：{id, class, title, pos, component_name,
        # delegate_property_name, event_name, asset_path, graph, position_source}。
        # 消费方要的是「绑上了哪一个事件 + 节点 id」；返回里没有 id 就不编一个。
        out: dict = {
            "success": True,
            "componentName": str(
                data.get("component_name") or (build_params(tool, args or {}) or {}).get(
                    "component_name") or ""),
            "eventName": str(
                data.get("event_name") or data.get("delegate_property_name")
                or (build_params(tool, args or {}) or {}).get("delegate_property_name") or ""),
            "asset_path": str(data.get("asset_path") or ""),
        }
        # official 入参名回显：调用方按 propertyName 传的，就用同一个名字读回来。
        out["propertyName"] = out["componentName"]
        node_id = str(data.get("id") or data.get("node_id") or "")
        if node_id:
            out["nodeId"] = node_id
        out["nodeClass"] = str(data.get("class") or "K2Node_ComponentBoundEvent")
        if data.get("graph"):
            out["graph"] = str(data.get("graph"))
        if data.get("position") is not None:
            out["position"] = data.get("position")
        elif isinstance(data.get("pos"), list):
            out["position"] = data.get("pos")
        if data.get("warning"):
            out["warning"] = str(data.get("warning"))
        return {"ok": True, "data": out}
    if tool == "ListWidgetClasses":
        types = data.get("types") or data.get("widget_types") or data.get("classes") or []
        return {"ok": True, "data": types if isinstance(types, list) else []}
    return {"ok": True, "data": data}


def _flatten_widget_tree(root, data: dict | None = None) -> list[dict]:
    """`ui.get_widget_tree` 的嵌套 root → official 消费方要的扁平行。

    行契约（`agent/umg_manual.py:platform_hud_names` 与 `sequence._check_platform_hud`
    都按它取数）：`widgetName` 必填，`parentWidget`/`class`/`slot`/`depth` 有就带。
    根节点也进列表——控件名核对不排除根。"""
    rows: list[dict] = []

    def walk(node, parent: str, depth: int) -> None:
        if not isinstance(node, dict):
            return
        name = str(node.get("name") or node.get("widgetName") or node.get("widget_name") or "").strip()
        row = {
            "widgetName": name,
            "parentWidget": parent,
            "class": str(node.get("class") or node.get("widget_class") or ""),
            "depth": depth,
        }
        slot = node.get("slot")
        if isinstance(slot, dict) and slot:
            row["slot"] = slot
        if node.get("is_variable"):
            row["isVariable"] = True
        if node.get("visibility"):
            row["visibility"] = str(node.get("visibility"))
        rows.append(row)
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                walk(child, name, depth + 1)

    walk(root, "", 0)
    if not rows and isinstance(data, dict):
        # 形状兜底：万一 monolith 改回扁平行，也按 official 形状透传，不编空树。
        for item in data.get("widgets") or []:
            if isinstance(item, dict):
                rows.append({
                    "widgetName": str(item.get("widgetName") or item.get("name") or ""),
                    "parentWidget": str(item.get("parentWidget") or ""),
                    "class": str(item.get("class") or ""),
                    "depth": int(item.get("depth") or 0),
                })
    return rows


def _subtree_rows(rows: list[dict], start: str) -> list[dict]:
    """以 start 为根的子树上摘出来（含自己），按 parentWidget 链走。"""
    if not start:
        return list(rows)
    keep = {start}
    changed = True
    while changed:
        changed = False
        for row in rows:
            parent = str(row.get("parentWidget") or "")
            name = str(row.get("widgetName") or "")
            if name not in keep and parent in keep:
                keep.add(name)
                changed = True
    return [row for row in rows if str(row.get("widgetName") or "") in keep]


def unsupported_reason(tool: str) -> str:
    return UNSUPPORTED.get(tool, "")


def manual_gap_tools() -> dict:
    """归入人工清单的能力缺口（official 原语 → 原因）。人工清单按这个展示。"""
    return {tool: UNSUPPORTED[tool] for tool in sorted(MANUAL_GAPS) if tool in UNSUPPORTED}


# official 原语里 monolith 侧**必填**的字段（缺了就别发请求）。
# 只收已经在跑的原语：缺参的本地拒绝比远端报「requires 'component_name' …」更省一次往返。
_REQUIRED = {
    # (monolith 参数名, 面向调用方的 official 参数名)
    "BindToEventProperty": (
        ("asset_path", "widgetBlueprint"),
        ("component_name", "propertyName"),
        ("delegate_property_name", "eventName"),
    ),
}


def _missing_required(tool: str, params: dict) -> str:
    """必填缺失 → 拒绝原文；齐了返回 ""。不发请求、不编成功。"""
    pairs = _REQUIRED.get(tool) or ()
    missing = [official for key, official in pairs if not str(params.get(key) or "").strip()]
    if not missing:
        return ""
    return (
        f"{tool} 参数不全，缺 {', '.join(missing)}。monolith 侧是 "
        "blueprint.add_node(node_type=ComponentBoundEvent, component_name, "
        "delegate_property_name)，两边字段必须都指到具体控件与委托。"
    )


def slot_order_params(tool: str, args: dict) -> dict:
    """`AddWidget` 的 childIndex → `ui.set_slot_property` 的 z_order（纯函数）。

    `ui.add_widget` 是 append-only（`UPanelWidget::AddChild`），它的参数表里没有
    child_index/z_order；official 的 childIndex 要改顺序只能事后写 slot。
    这里产出那一次 follow-up 的参数，由 exec_bridge 决定发不发、发失败怎么说。
    childIndex 缺省或 -1（official 的「追加到末尾」）不产生调用。"""
    if tool != "AddWidget":
        return {}
    raw = args.get("childIndex", args.get("child_index"))
    if raw is None or isinstance(raw, bool):
        return {}
    try:
        order = int(raw)
    except (TypeError, ValueError):
        return {}
    if order < 0:
        return {}
    base = build_params("AddWidget", args or {})
    name = str(base.get("widget_name") or "").strip()
    asset = str(base.get("asset_path") or "").strip()
    if not name:
        # 连不上具体控件：宁可不发，也不对一个猜出来的名字设 z 序。
        return {}
    out = {"asset_path": asset, "widget_name": name, "z_order": order}
    return out


def exec_bridge(tool: str, args: dict, task: dict, timeout=None) -> tuple[bool, dict]:
    """monolith 后端下的一次原语翻译。

    返回 (handled, result)：handled=False 表示桥不认识这个原语，调用方按原样拒绝；
    handled=True 时 result 就是 exec_tool 的返回值（成功、业务失败或「后端不支持」）。
    """
    reason = UNSUPPORTED.get(tool)
    if reason:
        return True, {"ok": False, "error": reason}
    mapping = MAPPED.get(tool)
    if not mapping:
        return False, {}
    namespace, action = mapping
    from backends.monolith import exec as monolith_exec

    params = build_params(tool, args or {})
    missing = _missing_required(tool, params)
    if missing:
        return True, {"ok": False, "error": missing}
    # find_assets 的 shim 要回填资产路径；monolith 参数里没有这个字段，单独带上
    if tool == "find_assets":
        params = dict(params)
        params["asset_path"] = params.get("asset_path") or ""
        args = dict(args or {})
        args["_asset_path"] = params["asset_path"]
    result = monolith_exec(namespace, action, params, task=task, transport=None)
    if not result.get("ok"):
        return True, shape_result(tool, args or {}, result)
    # snap_to_ground：spawn 成功后补一次贴地，对齐 official 的放置语义
    if tool == "add_to_scene_from_asset" and (args or {}).get("snap_to_ground"):
        label = str((args or {}).get("name") or "").strip()
        if label:
            snapped = monolith_exec("mesh", "snap_to_floor",
                                    {"actor_names": [label]}, task=task, transport=None)
            if not snapped.get("ok"):
                return True, {"ok": False, "error": f"已生成 {label}，贴地失败：{snapped.get('error')}"}
    shaped = shape_result(tool, args or {}, result)
    # childIndex → z_order：add_widget 是 append-only，顺序只能事后写 slot。
    # 控件已经加上了，所以这里失败不翻转整体 ok（否则「控件确实在」会被报成没加），
    # 但必须带 warning——不许静默丢弃。
    order_params = slot_order_params(tool, args or {})
    if order_params:
        shaped = dict(shaped or {})
        data = dict(shaped.get("data") or {}) if isinstance(shaped.get("data"), dict) else {}
        slot_type = str(data.get("slot_type") or "")
        if slot_type and slot_type != "CanvasPanelSlot":
            # z_order 只有 UCanvasPanelSlot 有（MonolithUISlotActions.cpp 的 SetZOrder
            # 在 Canvas 分支里）。别的槽位发过去必然是「no slot properties were set」，
            # 不值得白跑一次远端：直接如实说明顺序没生效。
            shaped["warning"] = (
                f"childIndex={order_params['z_order']} 没落成 z_order：父槽位是 {slot_type}，"
                "z 序只有 CanvasPanelSlot 有；ui.add_widget 是 append-only，"
                "顺序按当前追加结果为准"
            )
        else:
            ordered = monolith_exec("ui", "set_slot_property", order_params,
                                    task=task, transport=None)
            if ordered.get("ok"):
                data["z_order"] = order_params["z_order"]
            else:
                shaped["warning"] = (
                    f"控件已加上，但 childIndex={order_params['z_order']} 没落成 z_order："
                    f"{ordered.get('error') or 'set_slot_property 失败'}"
                    "（ui.add_widget 是 append-only，父槽位不是 CanvasPanelSlot 时没有 z 序可写）"
                )
        shaped["data"] = data
    return True, shaped
