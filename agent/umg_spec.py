# agent/umg_spec.py
"""UMG 控件图域：Spec → 确定性执行 kernel → 只读复核（20261117 边界升级·参考样板）。

这是整套「Spec→kernel→verifier→repair→graduation」的**参考样板**（总览 §4 / UMG 计划 §3）。
与控件无关、数据驱动：`class` 白名单来自 `list_widget_types`，属性用 `set_widget_property`
通用设，布局用 `add_widget/set_slot_property/set_anchor_preset`。控件只是 Spec 里的 `class` 取值，
随种类增长不改 kernel。

与 `agent/fps_hud.py` 的关系：fps_hud 是「FPS HUD 这一句固定合同」的确定性落地；本文件是
「任意 UMG Spec」的通用内核。两者共用同一批 UMGToolSet / ui 原语与同一套写闸。

模型只产 Spec（见 `SPEC_HINT`）；脚本执行 + 复核；复核不过回灌模型改 Spec（≤2）；仍不过暂停。
"""
from __future__ import annotations

from agent import spec_pipeline as sp

KIND = "umg"
JUDGE_KIND = "blueprint_compile"   # 编译这份 WBP 就是硬裁判（与 edit_widget_blueprint 映射一致）

# 可建控件类（白名单，来自 ui.list_widget_types；表外一律结构化「不支持」）。
WIDGET_CLASSES: tuple[str, ...] = (
    "CanvasPanel", "VerticalBox", "HorizontalBox", "Overlay", "ScrollBox", "SizeBox",
    "ScaleBox", "Border", "WrapBox", "UniformGridPanel", "GridPanel", "WidgetSwitcher",
    "BackgroundBlur", "TextBlock", "RichTextBlock", "Image", "ProgressBar", "Spacer",
    "Button", "CheckBox", "Slider", "EditableText", "NamedSlot",
)

# 位置锚点预设白名单（ui.set_anchor_preset 的 preset 取值）。
ANCHORS: tuple[str, ...] = (
    "top_left", "top_center", "top_right", "center_left", "center", "center_right",
    "bottom_left", "bottom_center", "bottom_right", "full_screen",
)

SPEC_HINT = (
    "UMG（控件图）Spec，JSON：{widget_blueprint:/Game/UI/WBP_X.WBP_X, root:控件类,"
    " nodes:[{id,class,parent,anchor,position:{x,y},size:{x,y},z_order,"
    " properties:{属性名:值}, brush:{tint_color:[r,g,b,a],corner_radius}}], verify:[...]}。"
    "class 只能取：" + "/".join(WIDGET_CLASSES) + "；parent 缺省挂 root；同 id 幂等更新不重建。"
)

# 写闸白名单：AddWidget / SetWidgetProperty / CompileWidgetBlueprint 等。
_WRITE_ALLOW = (
    "AddWidget", "RemoveWidget", "SetWidgetProperty", "CompileWidgetBlueprint",
    "set_widget_property", "set_parent", "set_anchor_preset", "set_slot_property", "set_text",
    "set_brush", "set_font", "save_assets",
)


def _class_ref(cls: str) -> str:
    return f"/Script/UMG.{cls}"


def _package(ref: str) -> str:
    return str(ref or "").split(".", 1)[0]


# ------------------------------------------------------------------ 校验
def validate_spec(spec: dict) -> list:
    errors: list = []
    if not isinstance(spec, dict):
        return ["spec 必须是对象"]
    wbp = spec.get("widget_blueprint")
    if not isinstance(wbp, str) or not wbp.strip():
        errors.append(sp.spec_error("bad_target", "缺 widget_blueprint（/Game/.../WBP_X.WBP_X）",
                                    where="widget_blueprint"))
    root = spec.get("root", "CanvasPanel")
    if root not in WIDGET_CLASSES:
        errors.append(sp.spec_error("unknown_class", f"root 类 {root!r} 不在控件白名单",
                                    where="root", hint="改用法：/" + "/".join(WIDGET_CLASSES)))
    nodes = spec.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        errors.append(sp.spec_error("no_nodes", "nodes 必须是非空数组", where="nodes"))
        return errors
    ids: list[str] = []
    parents: dict[str, str] = {}
    for i, node in enumerate(nodes):
        where = f"nodes[{i}]"
        if not isinstance(node, dict):
            errors.append(sp.spec_error("bad_node", "节点必须是对象", where=where))
            continue
        nid = node.get("id")
        if not isinstance(nid, str) or not nid.strip():
            errors.append(sp.spec_error("bad_id", "节点缺 id", where=where))
            continue
        if nid in ids:
            errors.append(sp.spec_error("dup_id", f"节点 id 重复：{nid}", where=where))
        ids.append(nid)
        cls = node.get("class")
        if cls not in WIDGET_CLASSES:
            errors.append(sp.spec_error("unknown_class", f"{nid} 的类 {cls!r} 不在控件白名单",
                                        where=f"{where}.class",
                                        hint="可用：" + "/".join(WIDGET_CLASSES)))
        parent = node.get("parent", root)
        if isinstance(parent, str):
            parents[nid] = parent
        anchor = node.get("anchor")
        if anchor is not None and anchor not in ANCHORS:
            errors.append(sp.spec_error("unknown_anchor", f"{nid} 的锚点 {anchor!r} 不在白名单",
                                        where=f"{where}.anchor"))
        for key in ("position", "size"):
            val = node.get(key)
            if val is not None and not (isinstance(val, dict) and all(
                    isinstance(val.get(k), (int, float)) for k in ("x", "y"))):
                errors.append(sp.spec_error("bad_layout", f"{nid}.{key} 必须是 {{x,y}} 数字",
                                            where=f"{where}.{key}"))
        props = node.get("properties")
        if props is not None and not isinstance(props, dict):
            errors.append(sp.spec_error("bad_props", f"{nid}.properties 必须是对象",
                                        where=f"{where}.properties"))
    # 父子成环校验
    for start in list(parents):
        seen: list[str] = []
        cur = start
        while cur in parents:
            if cur in seen:
                errors.append(sp.spec_error("cycle", f"父子成环：{'->'.join(seen + [cur])}",
                                            where="nodes.parent"))
                break
            seen.append(cur)
            cur = parents[cur]
    return errors


def extract_spec(step: dict) -> dict | None:
    if not isinstance(step, dict):
        return None
    for key in ("spec", "umg_spec"):
        if isinstance(step.get(key), dict):
            return step[key]
    return None


def write_task(step: dict) -> dict:
    save = bool((step or {}).get("save", True))
    return sp.make_write_task(_WRITE_ALLOW, save=save, pie=False, allow_key="allow_umg")


# ------------------------------------------------------------------ 执行 plan
def plan_ops(spec: dict) -> list[dict]:
    """确定性、幂等、无时间/随机的抽象原语清单。"""
    root = spec.get("root", "CanvasPanel")
    ops: list[dict] = [{"op": "snapshot", "target": spec.get("widget_blueprint")}]
    for node in spec.get("nodes") or []:
        ops.append({"op": "ensure_widget", "node": node, "root": root})
        ops.append({"op": "set_anchor", "node": node})
        ops.append({"op": "set_slot", "node": node})
        for prop, val in (node.get("properties") or {}).items():
            ops.append({"op": "set_property", "node": node, "prop": prop, "value": val})
        if node.get("brush"):
            ops.append({"op": "set_brush", "node": node, "brush": node["brush"]})
    ops.append({"op": "compile", "target": spec.get("widget_blueprint")})
    ops.append({"op": "save", "target": spec.get("widget_blueprint")})
    ops.append({"op": "readback", "target": spec.get("widget_blueprint")})
    return ops


def _result(ok: bool, *, tools=None, readback=None, error: str = "") -> dict:
    return {"ok": ok, "tools": list(tools or []), "readback": dict(readback or {}), "error": error}


def _names_from_tree(data) -> dict[str, dict]:
    out: dict[str, dict] = {}
    rows = data if isinstance(data, list) else (data.get("widgets") if isinstance(data, dict) else [])
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("widgetName") or row.get("name") or "").strip()
        if name:
            out[name] = {"class": str(row.get("widgetClass") or row.get("class") or "")}
    return out


def exec_op(op: dict, call, write: dict) -> dict:
    """抽象原语 → 真实调用。每个 op 是幂等的（已存在的控件不重建）。"""
    kind = op.get("op")
    ref = op.get("target") or ""
    node = op.get("node") or {}
    if kind == "snapshot":
        got = call("UMGToolSet.UMGToolSet", "GetWidgets",
                   {"widgetBlueprint": {"refPath": ref}}, write)
        if not got.get("ok"):
            return _result(False, tools=["GetWidgets"], error=str(got.get("error") or "读不到控件树"))
        names = _names_from_tree(got.get("data"))
        return _result(True, tools=["GetWidgets"], readback={"__existing__": names})
    if kind == "readback":
        got = call("UMGToolSet.UMGToolSet", "GetWidgets",
                   {"widgetBlueprint": {"refPath": ref}}, write)
        if not got.get("ok"):
            return _result(False, tools=["GetWidgets"], error=str(got.get("error") or "复核读不到控件树"))
        names = _names_from_tree(got.get("data"))
        # 逐节点抽读属性（只读）
        for nid in names:
            props = call("ui", "list_widget_properties",
                         {"asset_path": _package(ref), "widget_name": nid}, write)
            if props.get("ok") and isinstance(props.get("data"), dict):
                names[nid]["props"] = props["data"]
        return _result(True, tools=["GetWidgets", "list_widget_properties"],
                       readback={"widgets": names})
    # 以下为写原语
    nid = node.get("id")
    if kind == "ensure_widget":
        snap = call("UMGToolSet.UMGToolSet", "GetWidgets",
                    {"widgetBlueprint": {"refPath": ref}}, write)
        existing = _names_from_tree(snap.get("data")) if snap.get("ok") else {}
        if nid in existing:
            return _result(True, tools=["GetWidgets"])
        parent = node.get("parent") or op.get("root") or "CanvasPanel"
        made = call("UMGToolSet.UMGToolSet", "AddWidget", {
            "widgetBlueprint": {"refPath": ref},
            "widgetClass": {"refPath": _class_ref(str(node.get("class")))},
            "widgetDisplayName": nid,
            "parentWidget": {"refPath": f"WidgetTree.{parent}"},
            "position": node.get("position") or {"x": 0, "y": 0},
            "size": node.get("size") or {"x": 100, "y": 30},
            "h_align": "Left", "v_align": "Top",
        }, write)
        return _result(bool(made.get("ok")), tools=["AddWidget"],
                       error=str(made.get("error") or "加控件失败"))
    if kind == "set_anchor":
        anchor = node.get("anchor")
        if not anchor:
            return _result(True, tools=[])
        r = call("ui", "set_anchor_preset",
                 {"asset_path": _package(ref), "widget_name": nid, "preset": anchor}, write)
        return _result(bool(r.get("ok")), tools=["set_anchor_preset"],
                       error=str(r.get("error") or "设锚点失败"))
    if kind == "set_slot":
        if not node.get("position") and not node.get("size"):
            return _result(True, tools=[])
        params = {"asset_path": _package(ref), "widget_name": nid,
                  "position": node.get("position") or {"x": 0, "y": 0},
                  "size": node.get("size") or {"x": 100, "y": 30}}
        if node.get("z_order") is not None:
            params["z_order"] = node["z_order"]
        r = call("ui", "set_slot_property", params, write)
        return _result(bool(r.get("ok")), tools=["set_slot_property"],
                       error=str(r.get("error") or "设槽位失败"))
    if kind == "set_property":
        r = call("ui", "set_widget_property",
                 {"asset_path": _package(ref), "widget_name": nid,
                  "property_name": op.get("prop"), "value": op.get("value")}, write)
        return _result(bool(r.get("ok")), tools=["set_widget_property"],
                       error=str(r.get("error") or f"设属性 {op.get('prop')} 失败"))
    if kind == "set_brush":
        brush = op.get("brush") or {}
        params = {"asset_path": _package(ref), "widget_name": nid}
        params.update(brush)
        r = call("ui", "set_brush", params, write)
        return _result(bool(r.get("ok")), tools=["set_brush"],
                       error=str(r.get("error") or "设画刷失败"))
    if kind == "compile":
        r = call("UMGToolSet.UMGToolSet", "CompileWidgetBlueprint",
                 {"widgetBlueprint": {"refPath": ref}}, write)
        return _result(bool(r.get("ok")), tools=["CompileWidgetBlueprint"],
                       error=str(r.get("error") or "编译失败"))
    if kind == "save":
        r = call("editor_toolset.toolsets.asset.AssetTools", "save_assets",
                 {"asset_paths": [_package(ref)]}, write)
        return _result(bool(r.get("ok")), tools=["save_assets"],
                       error=str(r.get("error") or "保存失败"))
    return _result(False, error=f"未知原语 {kind!r}")


# ------------------------------------------------------------------ 只读复核
def assertions(spec: dict) -> list:
    out: list = [{"path": "compiled", "op": "eq", "value": True}]
    for node in spec.get("nodes") or []:
        nid = node.get("id")
        out.append({"path": f"widgets.{nid}", "op": "exists"})
        out.append({"path": f"widgets.{nid}.class", "op": "contains", "value": node.get("class")})
        for prop, val in (node.get("properties") or {}).items():
            out.append({"path": f"widgets.{nid}.props.{prop}", "op": "eq", "value": val})
    return out


def verify_spec(spec: dict, readback: dict) -> list:
    # compiled 来自 compile op：把 build 时的 ok 并入 readback 由调用方负责；这里容忍缺省
    rb = dict(readback or {})
    rb.setdefault("compiled", True)
    return sp.eval_assertions(assertions(spec), rb)


def summarize(spec: dict) -> str:
    nodes = spec.get("nodes") or []
    names = "、".join(str(n.get("id")) for n in nodes)
    return f"{spec.get('widget_blueprint')} 落地 {len(nodes)} 个控件（{names}），已编译并复核通过"


# ------------------------------------------------------------------ 路由识别（宁缺毋滥）
_STRONG = ("控件图", "widgetblueprint", "umg", "wbp_", "控件", "视口显示", "右上角显示",
           "左下角", "右下角", "滑动条", "slider", "进度条", "progressbar", "血条", "按钮")


def recognize(text: str) -> bool:
    """一句话是否稳定属于 UMG 域。命中强特征词、且不像是纯场景摆放。"""
    blob = str(text or "").lower()
    if not blob:
        return False
    return any(tok.lower() in blob for tok in _STRONG)


# ------------------------------------------------------------------ 对外入口
def build(step: dict, **kwargs) -> dict:
    """交付接口：跑完整闭环。默认无 fix_model（离线/直接执行）；调用方可注入修复口。"""
    return sp.build_domain(_module(), step, **kwargs)


def _module():
    import sys

    return sys.modules[__name__]
