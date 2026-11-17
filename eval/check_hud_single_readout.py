# eval/check_hud_single_readout.py
"""单读数 HUD 请求（只加一条血条）也要走真控件路线 —— 20261117-140229 的内容路由修复。

真事：任务「给该UI资产左下角添加一个血条，上限100，样式你自己定义，可仿照常见FPS的设计。
参考资产：/Game/UI/WBP_FpsHUD」只提了「血条」一类读数。旧口径下 `_clauses_accounted` 把
「上限100 / 样式你自己定义 / 可仿照…设计」判成 uncovered ⇒ `hud_windows` 返回 None ⇒ 整句掉进
通用受控求解，用无关的 AIAgent.Fps.Hit automation 收口：既没进控件路线，也不证明血条真的加上。

修法（只动这层）：
1. `_HUD_CLAUSE_RE` 认「上限/样式/设计/仿照/常见」等血条箱的收尾半句，单读数请求也能被这条链接住；
2. `requested_hud_controls` 按读数词挑子集：单读数（血条）⇒ 只落地 txt_Health，
   不越权补齐整套 HUD；两类以上读数仍按整份七控件合同（既有行为不变）。

本检查全离线（不连编辑器、不调模型）：封闭环境跑 `plan_windows`，只比规则拆句结果。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SINGLE = (
    "给该UI资产左下角添加一个血条，上限100，样式你自己定义，可仿照市面上常见FPS的设计。"
    "参考资产：/Game/UI/WBP_FpsHUD"
)
FULL = (
    "打开编辑器，在FPS游戏关卡L_NewTest3添加一个UMG到视口，左下角显示血条，满血100，"
    "暂时还没有扣血，就占位用。然后右下角显示子弹数量，还有手雷数量。"
    "右中部显示载具信息例如名字，是否上车/下车提示按键位E，还有当前车辆时速"
)
# 20261117-152319：先删旧血条再加一条新的。旧口径只当「补控件」，删除半句被静默丢掉
# （W2 记「新增 无，原本就在」却没删），用户指出「理解有问题」。
DELETE_ADD = (
    "删除该UI资产左下角的血条，然后给该UI资产左下角添加一个血条，上限100，"
    "样式你自己定义，可仿照市面上常见FPS的设计。参考资产：/Game/UI/WBP_FpsHUD"
)
DELETE_ONLY = "删除该UI资产左下角的血条。参考资产：/Game/UI/WBP_FpsHUD"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_single_routes_to_hud_rules() -> str:
    from agent.fps_hud import requested_hud_controls
    from agent.plan_windows import plan_windows
    from eval._pin_offline import closed_env

    check(requested_hud_controls(SINGLE) == ["txt_Health"],
          f"单读数「血条」应只挑 txt_Health，实际 {requested_hud_controls(SINGLE)}")
    with closed_env(UE_ALLOW_UMG="true", UE_ALLOW_BLUEPRINT="true"):
        planned = plan_windows(SINGLE)
    check(planned.get("source") == "hud-rules", f"单读数血条应走 hud-rules，实际 {planned.get('source')}")
    actions = [str(w.get("action") or "") for w in planned.get("windows") or []]
    check(actions == ["create_widget_blueprint", "edit_widget_blueprint", "check_fps_hud"],
          f"单读数血条窗口链不对：{actions}")
    for w in planned.get("windows") or []:
        check(bool(w.get("ok")), f"窗口不可实现：{w}")
    # 依赖成链：复核扇不许抢跑到改图扇之前
    ws = planned.get("windows") or []
    for i, w in enumerate(ws):
        expected = [ws[i - 1]["id"]] if i else []
        check(list(w.get("depends_on") or []) == expected, f"{w.get('id')} 依赖应为 {expected}")
    return "单读数「血条」→ hud-rules 三扇（create→edit→check），零模型"


def check_single_scope_is_health_only() -> str:
    from agent.fps_hud import hud_windows

    ws = hud_windows(SINGLE)
    check(ws is not None, "hud_windows 没接住单读数血条")
    edit = next(w for w in ws if w["action"] == "edit_widget_blueprint")
    check((edit.get("slots") or {}).get("hud_controls") == ["txt_Health"],
          f"edit 扇应只带 txt_Health，实际 {edit.get('slots')}")
    chk = next(w for w in ws if w["action"] == "check_fps_hud")
    check("txt_Health" in str(chk.get("title") or ""), f"复核扇标题要带 txt_Health：{chk.get('title')}")
    check((chk.get("slots") or {}).get("hud_controls") == ["txt_Health"], chk.get("slots"))
    return "单读数只落地/复核 txt_Health，不越权补其它读数"


def check_full_hud_still_full() -> str:
    from agent.fps_hud import requested_hud_controls, HUD_LAYOUT

    all_names = [n for n, *_ in HUD_LAYOUT]
    check(requested_hud_controls(FULL) == all_names,
          f"整句 HUD 仍要全七控件，实际 {requested_hud_controls(FULL)}")
    # 反例守卫：没点 FPS HUD 资产的纯「血条」句不该被这条规则接走（目标不明，交回模型）
    from agent.fps_hud import hud_windows
    check(hud_windows("在关卡里加一个血条") is None, "无 FPS HUD 目标的纯血条句被 hud_windows 接走了")
    return "整句 HUD 仍按整份合同；无 HUD 目标的纯血条句仍交回模型"


def check_delete_then_add_intent() -> str:
    """「先删血条再加一条」= 删+加两件都做，不是只当补控件。"""
    from agent.fps_hud import (
        hud_expectation, hud_windows, requested_hud_controls,
        wants_add_controls, wants_remove_controls,
    )
    from agent.plan_windows import plan_windows
    from eval._pin_offline import closed_env

    check(requested_hud_controls(DELETE_ADD) == ["txt_Health"], requested_hud_controls(DELETE_ADD))
    check(wants_remove_controls(DELETE_ADD) is True, "没识别出「删除」意图")
    check(wants_add_controls(DELETE_ADD) is True, "没识别出「添加」意图")
    check(hud_expectation(DELETE_ADD) == "present", "先删后加复核口径应是 present（删完还要加回来）")
    ws = hud_windows(DELETE_ADD)
    # 先删后加拆成两扇独立编辑窗（删除扇、摆放扇）：删除动作在窗口图上看得见（20261117-185234）。
    check(ws is not None and [w["action"] for w in ws] ==
          ["create_widget_blueprint", "edit_widget_blueprint", "edit_widget_blueprint",
           "check_fps_hud"], ws)
    edits = [w for w in ws if w["action"] == "edit_widget_blueprint"]
    ops = [str((w.get("slots") or {}).get("hud_op") or "") for w in edits]
    check(ops == ["remove", "add"], f"先删后加应是「删除扇→摆放扇」两步，实际 {ops}")
    check("删除" in str(edits[0].get("title") or ""), f"第一步应为删除扇：{edits[0].get('title')}")
    # 依赖成链：删除扇在摆放扇之前，不许并行
    check(list(edits[1].get("depends_on") or []) == [edits[0]["id"]],
          f"摆放扇应依赖删除扇：{edits[1].get('depends_on')}")
    with closed_env(UE_ALLOW_UMG="true", UE_ALLOW_BLUEPRINT="true"):
        planned = plan_windows(DELETE_ADD)
    check(planned.get("source") == "hud-rules", planned.get("source"))
    return "先删后加 → 删除扇 + 摆放扇两扇，删除意图可见（复核仍 present）"


def check_delete_only_expects_absent() -> str:
    """纯「删除血条」：只删不加，复核口径 absent（不能反过来又加回去）。"""
    from agent.fps_hud import hud_expectation, hud_windows, wants_add_controls, wants_remove_controls

    check(wants_remove_controls(DELETE_ONLY) is True, "没识别出删除意图")
    check(wants_add_controls(DELETE_ONLY) is False, "纯删除却被判成「要加」")
    check(hud_expectation(DELETE_ONLY) == "absent", "纯删除复核口径应是 absent")
    ws = hud_windows(DELETE_ONLY)
    check(ws is not None, "纯删除血条没被 hud_windows 接住")
    ck = next(w for w in ws if w["action"] == "check_fps_hud")
    check((ck.get("slots") or {}).get("hud_expect") == "absent", ck.get("slots"))
    check("应已删除" in str(ck.get("title") or ""), ck.get("title"))
    return "纯删除 → 复核 absent，不会把血条又加回去"


def check_visual_bar_contract() -> str:
    """「血条」必须是图形血条：系统要建 底条(Border)+填充条(ProgressBar)+数值；
    只挂一个文本 '100' 不算数（复核扇要拦住）。合同名(FPS_HUD_WIDGETS)一个字都不动。"""
    import inspect

    from agent import fps_hud
    from agent import sequence

    check(tuple(fps_hud.HEALTH_BAR_VISUALS) == ("HealthBarBG", "HealthBarFill"),
          fps_hud.HEALTH_BAR_VISUALS)
    # 图形控件是展示层附加物，不进 C++ BindWidgetOptional 合同
    contract = set(sequence.FPS_HUD_WIDGETS)
    for name in fps_hud.HEALTH_BAR_VISUALS:
        check(name not in contract, f"图形控件 {name} 不该进 FPS_HUD_WIDGETS 合同")
    check([n for n, *_ in fps_hud.HUD_LAYOUT] == list(sequence.FPS_HUD_WIDGETS),
          "HUD_LAYOUT 合同名被动了")
    # 复核扇必须真的引用这套图形控件来拦截「只有文本」的假血条
    src = inspect.getsource(sequence._check_fps_hud)
    check("HEALTH_BAR_VISUALS" in src, "复核扇没接图形血条判据：文本 '100' 会被当成血条通过")
    # 建血条的代码路径里要真的出现 Border/ProgressBar 两类控件
    bsrc = inspect.getsource(fps_hud.build_fps_hud)
    check("/Script/UMG.Border" in bsrc and "/Script/UMG.ProgressBar" in bsrc,
          "build_fps_hud 没建 Border/ProgressBar 图形血条")
    return "血条=图形控件(Border+ProgressBar+数值)，复核扇拦截纯文本假血条；合同名不变"


def main() -> int:
    try:
        rows = [
            check_single_routes_to_hud_rules(),
            check_single_scope_is_health_only(),
            check_full_hud_still_full(),
            check_delete_then_add_intent(),
            check_delete_only_expects_absent(),
            check_visual_bar_contract(),
        ]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print("  " + row)
    print("hud single readout ok（单读数走控件路线且只落地所需控件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
