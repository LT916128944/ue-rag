# eval/check_umg_spec_produce.py
"""UMG 执行前「模型产 Spec」接线守卫（20261118-152628 的续修）。

背景：`agent/umg_spec.py` 的 Spec 内核早已能确定性建 `Border/Image/…` 节点并只读复核，但线上一直
没有把「模型产 Spec」接进 run，于是非 FPS HUD 的控件需求（给血条左边加头像框）落到 discover 猜
action，做不出来。本次在 `run_windows._run_window` 的 monolith 控件图扇执行前加 `_maybe_attach_umg_spec`：
让模型按 `SPEC_HINT` 产一份 UMG Spec → 挂到窗口 → `sequence.repair_blueprint` 里的 umg_spec 内核执行 + 复核。

本检查全离线：模型用替身（给定 JSON），执行用假 `call`，不连编辑器、不联网。钉四件事：
1. `umg_spec.produce` 能解析/校验模型给的合法 Spec，坏 Spec / 非 JSON 一律返回 None（不假成功）；
2. 产出的 Spec 能被 kernel 真正建出来并复核通过（不是空转）；
3. `_maybe_attach_umg_spec` 只给「非 FPS HUD 的 edit_widget_blueprint 扇」挂 Spec，
   FPS HUD 链（带 hud_controls/hud_op）与非控件动作原样返回；
4. 模型产不出（空响应）⇒ None，交回原路径，行为不变。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AVATAR = "给 WBP_FpsHUD 左下角血条左边隔10像素加一个头像框，里面可以配置图片"

GOOD = {
    "widget_blueprint": "/Game/UI/WBP_FpsHUD.WBP_FpsHUD",
    "root": "CanvasPanel",
    "nodes": [
        {"id": "AvatarFrame", "class": "Border", "parent": "CanvasPanel", "anchor": "top_left",
         "position": {"x": -120, "y": 1000}, "size": {"x": 96, "y": 96}},
        {"id": "AvatarImage", "class": "Image", "parent": "AvatarFrame",
         "position": {"x": 4, "y": 4}, "size": {"x": 88, "y": 88}},
    ],
}
GOOD_TEXT = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _fake_call(toolset: str, tool: str, args=None, task=None) -> dict:
    """假执行：GetWidgets 返回目标节点（供只读复核），其余写原语一律 ok。"""
    if tool == "GetWidgets":
        return {"ok": True, "data": [
            {"widgetName": "AvatarFrame", "widgetClass": "Border"},
            {"widgetName": "AvatarImage", "widgetClass": "Image"},
        ]}
    if tool == "list_widget_properties":
        return {"ok": True, "data": {}}
    return {"ok": True, "data": {}}


def check_produce_parses_and_validates() -> str:
    from agent import umg_spec

    spec = umg_spec.produce({"query": AVATAR}, complete=lambda _m: GOOD_TEXT)
    check(isinstance(spec, dict), "合法 Spec JSON 没被解析出来")
    check(spec.get("widget_blueprint") == "/Game/UI/WBP_FpsHUD.WBP_FpsHUD", spec.get("widget_blueprint"))
    check(umg_spec.validate_spec(spec) == [], f"产出的 Spec 没过校验：{umg_spec.validate_spec(spec)}")
    return "produce：合法 Spec 解析 + 校验通过"


def check_produce_rejects_bad() -> str:
    from agent import umg_spec

    check(umg_spec.produce({"query": AVATAR}, complete=lambda _m: "抱歉，我做不到") is None,
          "非 JSON 响应没被拒（会假成功）")
    check(umg_spec.produce({"query": AVATAR}, complete=lambda _m: "") is None,
          "空响应没被拒（模型产不出时应交回原路径）")
    bad = dict(GOOD)
    bad["nodes"] = [{"id": "x", "class": "HyperWidget"}]
    check(umg_spec.produce({"query": AVATAR}, complete=lambda _m: json.dumps(bad)) is None,
          "表外控件类的 Spec 没被拒（会假成功）")
    return "produce：非 JSON / 空响应 / 表外类 一律 None（不假成功）"


def check_produced_spec_builds_and_verifies() -> str:
    from agent import umg_spec

    spec = umg_spec.produce({"query": AVATAR}, complete=lambda _m: GOOD_TEXT)
    built = umg_spec.build({"spec": spec, "save": True}, call=_fake_call)
    check(built.get("ok") is True, f"产出的 Spec 没建成功：{built.get('errors') or built.get('reason')}")
    widgets = (built.get("readback") or {}).get("widgets") or {}
    check("AvatarFrame" in widgets and "AvatarImage" in widgets, f"复核读回缺节点：{list(widgets)}")
    return "produced Spec → kernel 真建 AvatarFrame(AvatarImage) 并复核通过"


def check_attach_hook_scope() -> str:
    from agent.run_windows import _maybe_attach_umg_spec
    from eval._pin_offline import closed_env

    win = {"query": AVATAR, "title": "改 WBP_FpsHUD 控件图"}
    with closed_env(UE_ALLOW_UMG="true"):
        out = _maybe_attach_umg_spec(win, "edit_widget_blueprint", AVATAR, complete=lambda _m: GOOD_TEXT)
    check(isinstance(out.get("spec"), dict), "非 FPS HUD 的控件图扇没挂上 Spec")

    # FPS HUD 链（带 hud_controls）交给 build_fps_hud，不在此产通用 Spec
    hud = {"query": "左下角显示血条", "title": "摆放 WBP_FpsHUD 的 HUD 控件",
           "slots": {"hud_controls": ["txt_Health"]}}
    with closed_env(UE_ALLOW_UMG="true"):
        out2 = _maybe_attach_umg_spec(hud, "edit_widget_blueprint", hud["query"], complete=lambda _m: GOOD_TEXT)
    check("spec" not in out2, "FPS HUD 链被误挂了通用 Spec（会绕过合同判词）")

    # 非控件图动作原样返回
    out3 = _maybe_attach_umg_spec(win, "compile_blueprint", AVATAR, complete=lambda _m: GOOD_TEXT)
    check("spec" not in out3, "非控件图动作被挂了 Spec")

    # UMG 开关关着：不产 Spec
    with closed_env(UE_ALLOW_UMG="false"):
        out4 = _maybe_attach_umg_spec(win, "edit_widget_blueprint", AVATAR, complete=lambda _m: GOOD_TEXT)
    check("spec" not in out4, "UMG 开关关着仍产 Spec")

    # 模型产不出：原样返回
    with closed_env(UE_ALLOW_UMG="true"):
        out5 = _maybe_attach_umg_spec(win, "edit_widget_blueprint", AVATAR, complete=lambda _m: "")
    check("spec" not in out5, "模型产不出时仍挂了 Spec")
    return "attach：只给非 HUD 控件图扇挂 Spec；HUD 链/非控件动作/开关关/产不出 均原样返回"


def check_anchor_aliases_and_variable() -> str:
    """20261118-164628 回归：模型给 UE 枚举锚点（BottomLeft/Fill）+ bIsVariable 时，
    合法 Spec 不能再被整份拒掉，且 bIsVariable 要走 set_widget_is_variable 而非 set_widget_property。"""
    from agent import umg_spec

    spec = {
        "widget_blueprint": "/Game/UI/WBP_FpsHUD.WBP_FpsHUD",
        "root": "CanvasPanel",
        "nodes": [
            {"id": "AvatarFrame", "class": "Border", "parent": "CanvasPanel",
             "anchor": "BottomLeft", "position": {"x": 24, "y": -92}, "size": {"x": 64, "y": 64}},
            {"id": "AvatarImage", "class": "Image", "parent": "AvatarFrame",
             "anchor": "Fill", "properties": {"bIsVariable": True}},
        ],
    }
    check(umg_spec.validate_spec(spec) == [],
          f"UE 枚举锚点(BottomLeft/Fill) 仍被判错：{umg_spec.validate_spec(spec)}")
    check(umg_spec.normalize_anchor("BottomLeft") == "bottom_left", "BottomLeft 未归一到 bottom_left")
    check(umg_spec.normalize_anchor("Fill") == "stretch_fill", "Fill 未归一到 stretch_fill")
    ops = umg_spec.plan_ops(spec)
    kinds = [o.get("op") for o in ops]
    check("set_variable" in kinds, "is_variable 没产出 set_variable 原语")
    check(all(not (o.get("op") == "set_property" and o.get("prop") in ("bIsVariable", "is_variable"))
              for o in ops), "bIsVariable 仍在 properties 里走 set_widget_property")

    def fake(toolset, tool, args=None, task=None):
        if tool == "GetWidgets":
            return {"ok": True, "data": [
                {"widgetName": "AvatarFrame", "class": "Border"},
                {"widgetName": "AvatarImage", "class": "Image", "isVariable": True},
            ]}
        return {"ok": True, "data": {}}

    built = umg_spec.build({"spec": spec, "save": True}, call=fake)
    check(built.get("ok") is True, f"含 is_variable 的 Spec 没建成功：{built.get('errors') or built.get('reason')}")
    check("set_widget_is_variable" in (built.get("tools") or []), "没有调用 set_widget_is_variable")
    return "锚点别名(BottomLeft/Fill→preset) + is_variable→set_widget_is_variable 全链路通过"


def main() -> int:
    try:
        rows = [
            check_produce_parses_and_validates(),
            check_produce_rejects_bad(),
            check_produced_spec_builds_and_verifies(),
            check_attach_hook_scope(),
            check_anchor_aliases_and_variable(),
        ]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print("  " + row)
    print("umg spec produce ok（模型产 Spec → 内核建 + 复核；只对非 FPS HUD 控件扇接线）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
