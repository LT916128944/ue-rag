# eval/check_widget_event_binding.py
"""控件事件绑定桥 + childIndex 顺序的离线护栏（20261029 升级计划 §2 残差 1、3）。

先写这条断言，再把 `BindToEventProperty` 的开关打开——计划原话「先写一条
eval/check_* 离线断言再开开关」。所以本文件在 MAPPED 还没加它时必须是红的。

事实来源（只读内核，不连编辑器，9316 端口离线也照样跑）：

- `AIAgentProject/Plugins/Monolith/Source/MonolithBlueprint/Private/
  MonolithBlueprintNodeActions.cpp` 的 `blueprint.add_node` **已支持**
  `node_type=ComponentBoundEvent`（约 1417 行），必填 `component_name` +
  `delegate_property_name`，内部 `FindComponentProperty`（UGM BP 里命名的控件
  即可命中，见 MonolithBlueprintInternal.h 约 744 行的注释）+
  `FindMulticastDelegateProperty`，并对重复绑定直接报错（约 1447 行）。
  所以 official 的 `BindToEventProperty` 有真正的 monolith 落地路径，
  不再是「无对应 action」。

守五条：

1. `BindToEventProperty` 已在 MAPPED → `blueprint.add_node`，且不在 UNSUPPORTED；
   MAPPED/UNSUPPORTED 不重叠。
2. official 入参（widgetBlueprint/eventName/propertyName/propertyClass）翻成
   `{asset_path, node_type:"ComponentBoundEvent", component_name,
   delegate_property_name}`；别名写法（snake_case / 直接写 monolith 参数名）也认。
3. 缺控件名或事件名时**结构化拒绝**，不把半截参数发到编辑器、不编成功。
4. 失败透传：monolith 报「delegate not found」「already exists」时原样返回 ok=False，
   不伪造成功；数据里没有 node id 时也不编一个。
5. `childIndex` 不再被静默丢弃：official 的 `childIndex` 落到
   `ui.set_slot_property` 的 `z_order`（`ui.add_widget` 没有 child_index/z_order
   参数，塞进去只会是被 K3 软警告后忽略的未知键），落不成要带 `warning` 说明。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval._pin_offline import close_now  # noqa: E402

# 进程级封闭：即使本机 .env 写了 UE_EDITOR_BACKEND=monolith，这条检查也不会
# 摸到编辑器；monolith 的 exec 全程被替身接住。
close_now()

WBP_REF = "/Game/UI/WBP_FpsHud.WBP_FpsHud"
WBP_PACKAGE = "/Game/UI/WBP_FpsHud"
OFFICIAL = {
    "widgetBlueprint": {"refPath": WBP_REF},
    "eventName": "OnClicked",
    "propertyName": "btn_Start",
    "propertyClass": {"refPath": "/Script/UMG.Button"},
}


def check_mapping_tables() -> None:
    """① BindToEventProperty 已映射，且两张表不重叠。"""
    from core.monolith_bridge import MAPPED, UNSUPPORTED, unsupported_reason

    assert "BindToEventProperty" in MAPPED, (
        "BindToEventProperty 不在 MAPPED——桥还没开，事件绑定会继续被笼统拒绝")
    assert MAPPED["BindToEventProperty"] == ("blueprint", "add_node"), (
        f"BindToEventProperty 应映射到 blueprint.add_node，实际 {MAPPED['BindToEventProperty']!r}")
    assert "BindToEventProperty" not in UNSUPPORTED, "BindToEventProperty 还在 UNSUPPORTED"
    assert unsupported_reason("BindToEventProperty") == "", "unsupported_reason 仍拒 BindToEventProperty"
    overlap = set(MAPPED) & set(UNSUPPORTED)
    assert not overlap, f"原语同时出现在 MAPPED 和 UNSUPPORTED：{overlap}"
    for tool, pair in MAPPED.items():
        assert isinstance(pair, tuple) and len(pair) == 2, f"{tool} 的映射不是二元组：{pair!r}"
        namespace, action = pair
        assert str(namespace).strip() and str(action).strip(), f"{tool} 的映射有空段：{pair!r}"


def check_build_params_official_shape() -> None:
    """② official 形状 → monolith 的 ComponentBoundEvent 参数。"""
    from core.monolith_bridge import build_params

    params = build_params("BindToEventProperty", dict(OFFICIAL))
    assert params.get("asset_path") == WBP_PACKAGE, (
        f"asset_path 应是包路径 {WBP_PACKAGE}，实际 {params.get('asset_path')!r}")
    assert params.get("node_type") == "ComponentBoundEvent", params
    assert params.get("component_name") == "btn_Start", params
    assert params.get("delegate_property_name") == "OnClicked", params
    for leaked in ("propertyName", "eventName", "widgetBlueprint", "propertyClass"):
        assert leaked not in params, f"official 键 {leaked} 泄漏进 monolith 参数：{params}"
    # 可选参数：不给就不带，给了才带
    assert "graph_name" not in params and "position" not in params, params


def check_build_params_aliases() -> None:
    """② 别名写法都认（别只认一种）。"""
    from core.monolith_bridge import build_params

    variants = [
        # snake_case official 变体
        {"widget_blueprint": WBP_REF, "event_name": "OnClicked",
         "property_name": "btn_Start", "property_class": "/Script/UMG.Button"},
        # 直接写 monolith 参数名（模型按 pinned schema 填的那种）
        {"asset_path": WBP_PACKAGE, "component_name": "btn_Start",
         "delegate_property_name": "OnClicked"},
        # widgetName / delegate 别名 + blueprint 包路径
        {"blueprint": {"refPath": WBP_REF}, "widgetName": "btn_Start",
         "delegate_name": "OnClicked"},
        # 控件名带 WidgetTree 前缀：取值取最后一段
        {"widgetBlueprint": {"refPath": WBP_REF}, "widget_name": "WidgetTree.btn_Start",
         "event": "OnClicked"},
    ]
    for variant in variants:
        params = build_params("BindToEventProperty", dict(variant))
        assert params.get("asset_path") == WBP_PACKAGE, f"{variant} → {params}"
        assert params.get("component_name") == "btn_Start", f"{variant} → {params}"
        assert params.get("delegate_property_name") == "OnClicked", f"{variant} → {params}"
        assert params.get("node_type") == "ComponentBoundEvent", f"{variant} → {params}"

    # graph / position 也要过：official 没有图名，给了就得带上（默认 EventGraph 由编辑器定）
    with_extra = build_params("BindToEventProperty", {
        **OFFICIAL, "graphName": "EventGraph", "pos": [320, 160],
    })
    assert with_extra.get("graph_name") == "EventGraph", with_extra
    assert with_extra.get("position") == [320, 160], with_extra


def check_missing_required_refused_offline() -> None:
    """③ 缺控件名/事件名：结构化拒绝，不发请求、不编成功。"""
    from core.monolith_bridge import exec_bridge

    def _boom(*_args, **_kwargs):
        raise AssertionError("缺参数时不该调 monolith（会白烧一次远端错误）")

    import backends.monolith as monolith_mod

    saved = monolith_mod.exec
    monolith_mod.exec = _boom
    try:
        for broken, missing_hint in (
            ({"widgetBlueprint": {"refPath": WBP_REF}, "eventName": "OnClicked"}, "propertyName"),
            ({"widgetBlueprint": {"refPath": WBP_REF}, "propertyName": "btn_Start"}, "eventName"),
            ({}, "propertyName"),
        ):
            handled, result = exec_bridge("BindToEventProperty", dict(broken), {})
            assert handled is True, f"{broken} 没被桥接住"
            assert result.get("ok") is False, f"{broken} 缺参数却报成功：{result}"
            assert missing_hint in str(result.get("error") or ""), (
                f"{broken} 的拒绝理由没点出缺哪一项：{result}")
    finally:
        monolith_mod.exec = saved


def check_success_shape_and_failure_passthrough() -> None:
    """④ 成功给 node id / ok；失败透传原因，不编。"""
    from core.monolith_bridge import shape_result

    shaped = shape_result("BindToEventProperty", dict(OFFICIAL), {
        "ok": True,
        "data": {
            "id": "K2Node_ComponentBoundEvent_3",
            "class": "K2Node_ComponentBoundEvent",
            "component_name": "btn_Start",
            "delegate_property_name": "OnClicked",
            "event_name": "OnClicked",
            "asset_path": WBP_PACKAGE,
            "graph": "EventGraph",
            "position_source": "auto",
        },
    })
    assert shaped.get("ok") is True, shaped
    data = shaped.get("data") or {}
    assert data.get("success") is True, data
    assert data.get("nodeId") == "K2Node_ComponentBoundEvent_3", data
    assert data.get("eventName") == "OnClicked", data
    assert data.get("propertyName") == "btn_Start", data
    assert data.get("graph") == "EventGraph", data

    # 返回里没有 id 就不许编 nodeId
    bare = shape_result("BindToEventProperty", dict(OFFICIAL),
                        {"ok": True, "data": {"component_name": "btn_Start",
                                              "delegate_property_name": "OnClicked"}})
    assert bare.get("ok") is True, bare
    assert "nodeId" not in (bare.get("data") or {}), f"没有 id 却编了 nodeId：{bare}"
    assert (bare["data"]).get("eventName") == "OnClicked", bare

    # 失败（尤其是重复绑定 / delegate 不存在）原样透传，绝不返回 ok=True
    for error in (
        "ComponentBoundEvent for 'btn_Start.OnClicked' already exists in this Blueprint "
        "(node: K2Node_ComponentBoundEvent_1)",
        "BlueprintAssignable multicast delegate 'OnClick' not found on class 'Button'",
        "Component variable 'btn_Start' not found on Blueprint 'WBP_FpsHud' "
        "(must be a named subobject on the BP — SCS component for Actor BPs, named widget for UMG BPs)",
    ):
        failed = shape_result("BindToEventProperty", dict(OFFICIAL), {"ok": False, "error": error})
        assert failed.get("ok") is False, f"失败被改成了成功：{failed}"
        assert str(failed.get("error") or "") == error, (
            f"失败原因应原样透传，实际 {failed.get('error')!r}")

    # 空错误也不能编一个「成功」
    empty = shape_result("BindToEventProperty", dict(OFFICIAL), {"ok": False})
    assert empty.get("ok") is False and str(empty.get("error") or "").strip(), empty


def check_child_index_lands_as_z_order() -> None:
    """⑤ childIndex 不再静默丢弃：落成 ui.set_slot_property 的 z_order。"""
    from core import monolith_bridge
    from core.monolith_bridge import build_params, slot_order_params

    base = {
        "widgetBlueprint": {"refPath": WBP_REF},
        "widgetClass": {"refPath": "/Script/UMG.TextBlock"},
        "widgetDisplayName": "txt_Ammo",
        "parentWidget": {"refPath": "WidgetTree.Canvas"},
    }

    # `ui.add_widget` 的设置器里没有 child_index/z_order（append-only + AddChild），
    # 把 childIndex 混进 add_widget 的参数=被 K3 软警告后忽略=还是静默丢弃。
    params = build_params("AddWidget", {**base, "childIndex": 2})
    for leaked in ("childIndex", "child_index", "z_order"):
        assert leaked not in params, f"{leaked} 混进了 add_widget 参数（会被无声忽略）：{params}"
    assert params.get("widget_name") == "txt_Ammo" and params.get("parent_name") == "Canvas", params

    # 纯函数：childIndex → set_slot_property 的 z_order（同值）
    order = slot_order_params("AddWidget", {**base, "childIndex": 2})
    assert order.get("z_order") == 2, order
    assert order.get("widget_name") == "txt_Ammo", order
    assert order.get("asset_path") == WBP_PACKAGE, order
    # -1 是 official 的默认值（append 到末尾），不是顺序请求；缺省同理
    assert slot_order_params("AddWidget", {**base, "childIndex": -1}) == {}
    assert slot_order_params("AddWidget", dict(base)) == {}
    # 别的原语不该产出这个 follow-up
    assert slot_order_params("GetWidgets", {"childIndex": 2}) == {}

    # 端到端（替身接住 monolith）：add_widget 之后真的补一次 set_slot_property
    calls: list[tuple[str, str, dict]] = []

    def fake_exec(namespace, action, params_, task=None, transport=None):
        calls.append((namespace, action, dict(params_ or {})))
        if action == "add_widget":
            return {"ok": True, "data": {"widget_name": "txt_Ammo", "widget_class": "TextBlock",
                                         "parent_name": "Canvas", "slot_type": "CanvasPanelSlot"}}
        return {"ok": True, "data": {"widget": "txt_Ammo", "slot_type": "CanvasPanelSlot",
                                     "properties_set": 1}}

    import backends.monolith as monolith_mod

    saved = monolith_mod.exec
    monolith_mod.exec = fake_exec
    try:
        handled, result = monolith_bridge.exec_bridge("AddWidget", {**base, "childIndex": 2}, {})
        assert handled is True and result.get("ok") is True, result
        assert [call[1] for call in calls] == ["add_widget", "set_slot_property"], (
            f"childIndex 没有落到 set_slot_property：{[call[1] for call in calls]}")
        assert calls[1][0] == "ui", calls
        assert calls[1][2].get("z_order") == 2, calls[1]
        assert calls[1][2].get("widget_name") == "txt_Ammo", calls[1]
        assert calls[1][2].get("asset_path") == WBP_PACKAGE, calls[1]
        assert (result.get("data") or {}).get("z_order") == 2, result

        # 没有 childIndex 时不许多发一次调用
        calls.clear()
        handled, result = monolith_bridge.exec_bridge("AddWidget", dict(base), {})
        assert handled is True and result.get("ok") is True, result
        assert [call[1] for call in calls] == ["add_widget"], [call[1] for call in calls]

        # 落不成（例如父槽位不是 CanvasPanelSlot）也必须说出来，不许静默
        def failing_exec(namespace, action, params_, task=None, transport=None):
            calls.append((namespace, action, dict(params_ or {})))
            if action == "add_widget":
                return {"ok": True, "data": {"widget_name": "txt_Ammo"}}
            return {"ok": False, "error": "No slot properties were set. Provide at least one "
                                          "slot property parameter."}

        calls.clear()
        monolith_mod.exec = failing_exec
        handled, result = monolith_bridge.exec_bridge("AddWidget", {**base, "childIndex": 5}, {})
        assert handled is True, result
        assert result.get("ok") is True, f"控件加上了就不该整体报失败：{result}"
        warning = str(result.get("warning") or "")
        assert "5" in warning and "z_order" in warning, (
            f"z_order 没落成却没有可见说明（静默丢弃）：{result}")

        # 非 Canvas 槽位：z 序根本不存在，别白跑一次必然失败的 set_slot_property，
        # 但同样要说出来（warning 里点名槽位类型）。
        def box_exec(namespace, action, params_, task=None, transport=None):
            calls.append((namespace, action, dict(params_ or {})))
            return {"ok": True, "data": {"widget_name": "txt_Ammo",
                                         "slot_type": "VerticalBoxSlot"}}

        calls.clear()
        monolith_mod.exec = box_exec
        handled, result = monolith_bridge.exec_bridge("AddWidget", {**base, "childIndex": 0}, {})
        assert handled is True and result.get("ok") is True, result
        assert [call[1] for call in calls] == ["add_widget"], (
            f"VerticalBoxSlot 上不该去写 z_order：{[call[1] for call in calls]}")
        warning = str(result.get("warning") or "")
        assert "VerticalBoxSlot" in warning and "z_order" in warning, (
            f"非 Canvas 槽位的顺序没生效却没说：{result}")
    finally:
        monolith_mod.exec = saved


def main() -> None:
    check_mapping_tables()
    check_build_params_official_shape()
    check_build_params_aliases()
    check_missing_required_refused_offline()
    check_success_shape_and_failure_passthrough()
    check_child_index_lands_as_z_order()
    print("widget event binding ok")


if __name__ == "__main__":
    main()
