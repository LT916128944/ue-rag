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

# 位置锚点预设白名单 = ui.set_anchor_preset 实际接受的 preset（2026-11-18 真机 describe 核实）。
# 注意：只有 CanvasPanel 的子控件能设锚点；非 Canvas 槽位（SizeBox/Border/Overlay…）的布局只能
# 走 set_slot_property 的 h_align/v_align/padding，anchor 对它们无效（内核按 best-effort 处理，不阻断）。
ANCHORS: tuple[str, ...] = (
    "top_left", "top_center", "top_right", "center_left", "center", "center_right",
    "bottom_left", "bottom_center", "bottom_right",
    "stretch_horizontal", "stretch_vertical", "stretch_fill",
    "stretch_top", "stretch_bottom", "stretch_left", "stretch_right",
)

# 模型常按 UE 枚举/口语写锚点（BottomLeft / Fill / TopCenter…）。归一化：去掉非字母数字、
# 转小写后查表。已在白名单里的 snake_case 也直接命中。此前一律按白名单硬判 → 模型给
# “BottomLeft/Fill” 的合法 Spec 整份被拒（20261118-164628 的 W2），改这里认。
_ANCHOR_ALIASES: dict[str, str] = {
    "topleft": "top_left", "topcenter": "top_center", "topright": "top_right",
    "centerleft": "center_left", "middleleft": "center_left",
    "center": "center", "middle": "center", "centre": "center",
    "centerright": "center_right", "middleright": "center_right",
    "bottomleft": "bottom_left", "bottomcenter": "bottom_center", "bottomright": "bottom_right",
    "fill": "stretch_fill", "fullscreen": "stretch_fill", "full": "stretch_fill",
    "stretchfill": "stretch_fill", "stretch": "stretch_fill",
    "stretchhorizontal": "stretch_horizontal", "horizontal": "stretch_horizontal",
    "stretchvertical": "stretch_vertical", "vertical": "stretch_vertical",
    "stretchtop": "stretch_top", "stretchbottom": "stretch_bottom",
    "stretchleft": "stretch_left", "stretchright": "stretch_right",
}
for _preset in ANCHORS:
    _ANCHOR_ALIASES.setdefault(_preset.replace("_", ""), _preset)


def normalize_anchor(name) -> str | None:
    """任意锚点写法 → 白名单 preset；认不出返回 None（由 validate 报错，不静默改）。"""
    raw = str(name or "").strip()
    if not raw:
        return None
    key = "".join(ch for ch in raw.lower() if ch.isalnum())
    return _ANCHOR_ALIASES.get(key)


SPEC_HINT = (
    "UMG（控件图）Spec，JSON：{widget_blueprint:/Game/UI/WBP_X.WBP_X, root:控件类,"
    " nodes:[{id,class,parent,anchor,position:{x,y},size:{x,y},z_order,is_variable,"
    " properties:{属性名:值}, brush:{tint_color:\"r,g,b,a\",corner_radius:{top_left,top_right,bottom_right,bottom_left}}}],"
    " verify:[...]}。"
    "class 只能取：" + "/".join(WIDGET_CLASSES) + "。"
    "anchor 用：" + "/".join(ANCHORS) + "（Fill=stretch_fill；只有 CanvasPanel 子控件的 anchor 生效）。"
    "要「里面可配置图片」这类暴露为变量时，在节点上写 is_variable:true（不要写进 properties）；"
    "parent 缺省挂 root；同 id 幂等更新不重建。"
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


def _node_properties(node: dict) -> dict:
    """节点的通用属性；把 bIsVariable/is_variable 摘出去（它们走 set_widget_is_variable，不进 allowlist）。"""
    props = node.get("properties")
    if not isinstance(props, dict):
        return {}
    return {k: v for k, v in props.items() if k not in ("bIsVariable", "is_variable")}


def _node_is_variable(node: dict) -> bool:
    """节点要不要暴露成命名变量（bIsVariable）。顶层 is_variable 或 properties 里任一为真。"""
    if bool(node.get("is_variable")):
        return True
    props = node.get("properties") if isinstance(node.get("properties"), dict) else {}
    for key in ("bIsVariable", "is_variable"):
        val = props.get(key)
        if isinstance(val, bool) and val:
            return True
    return False


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
        if anchor is not None and normalize_anchor(anchor) is None:
            errors.append(sp.spec_error("unknown_anchor", f"{nid} 的锚点 {anchor!r} 不在白名单",
                                        where=f"{where}.anchor",
                                        hint="可用：" + "/".join(ANCHORS)))
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
    """确定性、幂等、无时间/随机的抽象原语清单。

    每个 op 都带 ``target``（控件蓝图 ref）：exec_op 从 op 取 asset_path，node 级 op 也必须
    带上，否则 AddWidget/set_* 的 asset_path 为空 → 真机报 "Missing required param: asset_path"
    （20261118 真机实测；离线假 call 不看路径，掩盖了这个缺口）。"""
    root = spec.get("root", "CanvasPanel")
    wbp = spec.get("widget_blueprint")
    ops: list[dict] = [{"op": "snapshot", "target": wbp}]
    for node in spec.get("nodes") or []:
        ops.append({"op": "ensure_widget", "node": node, "root": root, "target": wbp})
        if node.get("anchor"):
            ops.append({"op": "set_anchor", "node": node, "target": wbp})
        ops.append({"op": "set_slot", "node": node, "target": wbp})
        if _node_is_variable(node):
            ops.append({"op": "set_variable", "node": node, "target": wbp})
        for prop, val in _node_properties(node).items():
            ops.append({"op": "set_property", "node": node, "prop": prop, "value": val, "target": wbp})
        if node.get("brush"):
            ops.append({"op": "set_brush", "node": node, "brush": node["brush"], "target": wbp})
    ops.append({"op": "compile", "target": wbp})
    ops.append({"op": "save", "target": wbp})
    ops.append({"op": "readback", "target": wbp})
    return ops


def _result(ok: bool, *, tools=None, readback=None, error: str = "", warning: str = "") -> dict:
    out = {"ok": ok, "tools": list(tools or []), "readback": dict(readback or {}), "error": error}
    if warning:
        out["warning"] = warning
    return out


def _soft(tools, error: str) -> dict:
    """表现层写原语（锚点/槽位/属性/画刷）失败不阻断主链——硬合同是「控件在不在 / 类对不对 /
    暴露成变量 / 编译」。但带 warning，不静默丢弃。"""
    return _result(True, tools=tools, warning=error)


def _color_text(val):
    """[r,g,b,a] / [r,g,b] → "r,g,b[,a]" 字符串（ui.set_brush 只认 hex 或浮点串）；已是字符串原样。"""
    if isinstance(val, str):
        return val
    if isinstance(val, (list, tuple)) and len(val) >= 3:
        try:
            nums = [float(x) for x in val]
        except (TypeError, ValueError):
            return val
        return ",".join(f"{n:g}" for n in nums)
    return val


def _coerce_brush(brush: dict) -> dict:
    """把模型常见的画刷写法转成 ui.set_brush 认的形状：
    corner_radius 数字 → {top_left,...}；tint_color [r,g,b,a] → "r,g,b,a"；过滤未知键。
    2026-11-18 真机：list 形态的 tint_color 与 int 形态的 corner_radius 会被判「没有画刷属性」。"""
    if not isinstance(brush, dict):
        return {}
    allowed = ("draw_type", "tint_color", "image_size", "margin", "corner_radius",
               "outline_color", "outline_width", "texture_path", "material_path")
    out: dict = {}
    for key, val in brush.items():
        if key not in allowed:
            continue
        if key == "corner_radius":
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                n = val
                out["corner_radius"] = {"top_left": n, "top_right": n,
                                        "bottom_right": n, "bottom_left": n}
            elif isinstance(val, dict):
                out["corner_radius"] = val
            continue
        if key in ("tint_color", "outline_color"):
            out[key] = _color_text(val)
            continue
        out[key] = val
    return out


def _names_from_tree(data) -> dict[str, dict]:
    out: dict[str, dict] = {}
    rows = data if isinstance(data, list) else (data.get("widgets") if isinstance(data, dict) else [])
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("widgetName") or row.get("name") or "").strip()
        if name:
            info = {"class": str(row.get("widgetClass") or row.get("class") or "")}
            if row.get("isVariable") or row.get("is_variable"):
                info["isVariable"] = True
            out[name] = info
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
        return _result(True, tools=["GetWidgets"], readback={"widgets": names})
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
    if kind == "set_variable":
        # bIsVariable 不在 set_widget_property 的 per-type allowlist 里，必须走专用 action。
        r = call("ui", "set_widget_is_variable",
                 {"wbp_path": _package(ref), "widget_name": nid, "is_variable": True}, write)
        if r.get("ok"):
            return _result(True, tools=["set_widget_is_variable"])
        return _result(False, tools=["set_widget_is_variable"],
                       error=str(r.get("error") or "暴露变量失败"))
    if kind == "set_anchor":
        anchor = node.get("anchor")
        if not anchor:
            return _result(True, tools=[])
        preset = normalize_anchor(anchor)
        if not preset:
            return _soft([], f"{nid} 的锚点 {anchor!r} 认不出，跳过")
        r = call("ui", "set_anchor_preset",
                 {"asset_path": _package(ref), "widget_name": nid, "preset": preset}, write)
        if r.get("ok"):
            return _result(True, tools=["set_anchor_preset"])
        return _soft(["set_anchor_preset"], str(r.get("error") or "设锚点失败"))
    if kind == "set_slot":
        if not node.get("position") and not node.get("size"):
            return _result(True, tools=[])
        params = {"asset_path": _package(ref), "widget_name": nid,
                  "position": node.get("position") or {"x": 0, "y": 0},
                  "size": node.get("size") or {"x": 100, "y": 30}}
        if node.get("z_order") is not None:
            params["z_order"] = node["z_order"]
        r = call("ui", "set_slot_property", params, write)
        if r.get("ok"):
            return _result(True, tools=["set_slot_property"])
        return _soft(["set_slot_property"], str(r.get("error") or "设槽位失败"))
    if kind == "set_property":
        r = call("ui", "set_widget_property",
                 {"asset_path": _package(ref), "widget_name": nid,
                  "property_name": op.get("prop"), "value": op.get("value")}, write)
        if r.get("ok"):
            return _result(True, tools=["set_widget_property"])
        return _soft(["set_widget_property"], str(r.get("error") or f"设属性 {op.get('prop')} 失败"))
    if kind == "set_brush":
        brush = _coerce_brush(op.get("brush") or {})
        if not brush:
            return _soft([], "画刷没有可识别字段，跳过")
        params = {"asset_path": _package(ref), "widget_name": nid}
        params.update(brush)
        r = call("ui", "set_brush", params, write)
        if r.get("ok"):
            return _result(True, tools=["set_brush"])
        return _soft(["set_brush"], str(r.get("error") or "设画刷失败"))
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
    """硬合同断言：编译通过 + 每个节点存在 + 类对 + （要暴露变量的）isVariable 为真。

    属性值不进硬断言：真机 list_widget_properties 返回的是分类属性表（list），不是
    {名:值} 扁平字典，按属性值硬判会假失败；通用属性在写侧 best-effort 设置，复核在
    verify_spec 里做「读得到才比、读不到就跳过」的软核对。"""
    out: list = [{"path": "compiled", "op": "eq", "value": True}]
    for node in spec.get("nodes") or []:
        nid = node.get("id")
        out.append({"path": f"widgets.{nid}", "op": "exists"})
        out.append({"path": f"widgets.{nid}.class", "op": "contains", "value": node.get("class")})
        if _node_is_variable(node):
            out.append({"path": f"widgets.{nid}.isVariable", "op": "eq", "value": True})
    return out


def verify_spec(spec: dict, readback: dict) -> list:
    # compiled 来自 compile op：把 build 时的 ok 并入 readback 由调用方负责；这里容忍缺省
    rb = dict(readback or {})
    rb.setdefault("compiled", True)
    fails = sp.eval_assertions(assertions(spec), rb)
    # 软属性核对：只有 readback 真给了扁平 props 且值与 spec 不同才报；读不到就跳过（不阻断）。
    widgets = rb.get("widgets") if isinstance(rb.get("widgets"), dict) else {}
    for node in spec.get("nodes") or []:
        nid = node.get("id")
        shard = widgets.get(nid) if isinstance(widgets.get(nid), dict) else {}
        flat = shard.get("props") if isinstance(shard.get("props"), dict) else None
        if not flat:
            continue
        for prop, val in _node_properties(node).items():
            if prop in flat and flat.get(prop) != val:
                fails.append(f"widgets.{nid}.props.{prop} 期望 {val!r}，实际 {flat.get(prop)!r}")
    return fails


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
    """交付接口：跑完整闭环。默认无 fix_model（离线/直接执行）；调用方可注入修复口。

    成功时补 ``build`` / ``test`` 判词（kind=blueprint_compile）——编译是这条链的硬裁判，
    run_windows._absorb_result 只从这两个子字典取判词去翻条款；不给的话
    ``c-blueprint_compile`` 永远翻不过来（widget_graph 由 run_windows 现造一条合成判词）。"""
    result = sp.build_domain(_module(), step, **kwargs)
    if isinstance(result, dict) and result.get("ok"):
        summary = str(result.get("summary") or f"{result.get('domain') or 'umg'} 已落地并复核通过")
        verdict = {"ok": True, "kind": JUDGE_KIND, "summary": summary, "errors": []}
        result.setdefault("judge_kind", JUDGE_KIND)
        result["build"] = dict(verdict)
        result["test"] = dict(verdict)
    return result


def produce(step: dict, *, complete=None) -> dict | None:
    """非 FPS HUD 的控件图扇：让模型按 ``SPEC_HINT`` 产一份 UMG Spec，供 ``build`` 确定性执行
    + 只读复核。产不出 / 不合法 → None（交回原路径，诚实失败，不假成功）。

    目标控件蓝图从槽位/句子现归地（``fps_hud.widget_ref_from``），并作为缺省 ``widget_blueprint``
    注入；模型若自己给了就保留模型的。这一段只决定「谁产 Spec」，执行/复核/回灌仍在内核里。"""
    step = step if isinstance(step, dict) else {}
    from agent.fps_hud import widget_ref_from

    ref = widget_ref_from(step)
    probe = dict(step)
    query = str(probe.get("query") or probe.get("title") or "").strip()
    probe["query"] = (query + f"\n目标控件蓝图：{ref}").strip()
    return sp.produce_spec(_module(), probe, complete=complete,
                           defaults={"widget_blueprint": ref, "root": "CanvasPanel"})


def _module():
    import sys

    return sys.modules[__name__]
