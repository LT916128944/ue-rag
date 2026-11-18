# eval/check_widget_route.py
"""控件（UMG）需求不许掉进受控求解，也不许拿 FPS HUD 合同冒充完成 —— 20261118-152628。

真事：原句「打开编辑器，给UI左下角血条左边隔10像素，添加一个头像框，里面可以配置图片。
参考资产：/Game/UI/WBP_FpsHUD」。
- 理解器（大模型）已经正确给出 edit_widget_blueprint，但纯规则 `_clauses_accounted` 认不出
  「添加一个头像框 / 里面可以配置图片」→ `_covers_after_gate` 判没盖住 → 整句掉进 solver-fallback。
  受控求解收口固定是 automation，UMG 改控件产不出过滤器 → W2 必诚实失败（judge_failed）。
- stitch 补回的 S1 edit_widget_blueprint 又被 build_fps_hud 当成「FPS HUD 请求」（「血条」+
  资产名里 HUD 字面量凑两票，或「不遮挡 txt_RocketAmmo」里的控件名），拿合同控件「新增
  无，原本就在」报成功——头像框一个字都没加，却记成成功。同一句一处假成功、一处假失败。

本检查钉住「接不住就以已问过的大模型为准」这条口径（**不新增动作表、也不加关键词表**）：
1. 规则词典**仍认不出**控件元素半句（`_clause_accounted(...) is False`）——绝不靠关键词表冒充覆盖；
2. 但整句**不掉进受控求解**：理解器（大模型）给出的 `edit_widget_blueprint` 窗口被保留
   （source=understood、`planner_called=False`，不额外多叫一次模型），头像框走控件图路线；
3. `is_fps_hud_request` 遇合同外控件返回 False，build_fps_hud 不再拿 HUD 合同冒充；
4. 真 HUD（单读数 / 整句）仍走 hud-rules，不被误收紧；
5. 动作表里没有「头像 / 图片」类新动作（没新增动作）。

全离线：理解器用替身，检索给空标题，不连编辑器、不联网。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AVATAR = (
    "打开编辑器，给UI左下角血条左边隔10像素，添加一个头像框，里面可以配置图片。"
    "参考资产：/Game/UI/WBP_FpsHUD"
)
SINGLE_HUD = (
    "给该UI资产左下角添加一个血条，上限100，样式你自己定义，可仿照市面上常见FPS的设计。"
    "参考资产：/Game/UI/WBP_FpsHUD"
)
FULL_HUD = (
    "打开编辑器，在FPS游戏关卡L_NewTest3添加一个UMG到视口，左下角显示血条，满血100，"
    "暂时还没有扣血，就占位用。然后右下角显示子弹数量，还有手雷数量。"
    "右中部显示载具信息例如名字，是否上车/下车提示按键位E，还有当前车辆时速"
)

# 理解器替身：大模型正确给出 launch_editor + edit_widget_blueprint，unresolved 空（它认为盖住了整句）
INTENT_AVATAR = json.dumps({
    "goals": [
        {"action": "launch_editor", "slots": {}},
        {"action": "edit_widget_blueprint", "slots": {"folder": "/Game/UI", "name": "WBP_FpsHUD"}},
    ],
    "unresolved": [],
}, ensure_ascii=False)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _calls():
    """包一个 complete 替身，统计被调用次数（验证「不再额外多问一次模型」）。"""
    box = {"n": 0}

    def complete(_messages):
        box["n"] += 1
        return INTENT_AVATAR

    return complete, box


def _plan_offline(query: str, complete) -> dict:
    from agent import understand
    from agent.plan_windows import plan_windows
    from eval._pin_offline import closed_env

    saved = understand._titles
    understand._titles = lambda _query: []
    try:
        with closed_env(UE_SOLVER_ENABLED="true", UE_ALLOW_UMG="true", UE_ALLOW_BLUEPRINT="true"):
            return plan_windows(query, complete=complete)
    finally:
        understand._titles = saved


def check_rules_cannot_catch_but_model_plan_kept() -> str:
    from agent.plan_windows import _clause_accounted

    # 1) 规则词典确实认不出控件元素半句（不靠关键词表冒充覆盖）
    check(_clause_accounted("添加一个头像框", [{"action": "edit_widget_blueprint"}]) is False,
          "规则词典居然认出了「头像框」——说明又用关键词表硬覆盖了，应改成以大模型为准")
    # 2) 但整句不掉进受控求解：理解器（大模型）给的窗口被保留，不额外多叫模型
    complete, box = _calls()
    planned = _plan_offline(AVATAR, complete)
    source = planned.get("source")
    check(source != "solver-fallback", f"头像框需求又掉进受控求解：source={source}")
    check(source == "understood", f"应以理解器(大模型)的窗口为准，实际 source={source}")
    check(planned.get("planner_called") is False, "不该额外再叫规划者（理解器已经问过了）")
    check(box["n"] == 1, f"理解器应只问一次，实际调用 {box['n']} 次")
    actions = [str(w.get("action") or "") for w in planned.get("windows") or []]
    check("solve" not in actions, f"窗口里不该出现 solve：{actions}")
    check(actions == ["launch_editor", "edit_widget_blueprint"],
          f"大模型给的窗口应是 launch_editor → edit_widget_blueprint，实际 {actions}")
    return "规则认不出控件半句→不否决大模型→保留 launch_editor→edit_widget_blueprint，不进 solve、不额外多问"


def check_fps_hud_not_claim_out_of_contract_widget() -> str:
    from agent.fps_hud import hud_windows, is_fps_hud_request

    check(is_fps_hud_request(AVATAR) is False, "头像框需求被误判成 FPS HUD 请求（会拿合同冒充完成）")
    check(hud_windows(AVATAR) is None, "头像框需求被 hud_windows 接走了")
    return "头像框/图片需求不再被判成 FPS HUD，build_fps_hud 不会拿合同控件冒充完成"


def check_real_hud_still_works() -> str:
    from agent.fps_hud import hud_windows, is_fps_hud_request, requested_hud_controls
    from agent.plan_windows import plan_windows
    from eval._pin_offline import closed_env

    check(is_fps_hud_request(SINGLE_HUD) is True, "单读数血条被误判成非 HUD")
    check(requested_hud_controls(SINGLE_HUD) == ["txt_Health"], requested_hud_controls(SINGLE_HUD))
    check(hud_windows(SINGLE_HUD) is not None, "单读数血条没被 hud_windows 接住")
    with closed_env(UE_ALLOW_UMG="true", UE_ALLOW_BLUEPRINT="true"):
        single = plan_windows(SINGLE_HUD)
    check(single.get("source") == "hud-rules", f"单读数血条应走 hud-rules，实际 {single.get('source')}")
    with closed_env(UE_ALLOW_UMG="true", UE_ALLOW_BLUEPRINT="true"):
        full = plan_windows(FULL_HUD)
    check(full.get("source") == "hud-rules", f"整句 HUD 应走 hud-rules，实际 {full.get('source')}")
    return "真 HUD（单读数 / 整句）仍走 hud-rules，未被误收紧"


def check_no_action_table_growth() -> str:
    from agent.catalog import CATALOG

    check("edit_widget_blueprint" in CATALOG, "动作表里应有 edit_widget_blueprint")
    bad = [name for name in CATALOG if any(t in name for t in ("头像", "图片", "avatar", "image"))]
    check(not bad, f"动作表被塞了新动作：{bad}")
    return "未新增动作：头像框仍由 edit_widget_blueprint + UMG Spec 完成，动作表无头像/图片动作"


def main() -> int:
    try:
        rows = [
            check_rules_cannot_catch_but_model_plan_kept(),
            check_fps_hud_not_claim_out_of_contract_widget(),
            check_real_hud_still_works(),
            check_no_action_table_growth(),
        ]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print("  " + row)
    print("widget route ok（以已问过的大模型为准、不新增动作/关键词表、不被 HUD 合同冒充、真 HUD 不回归）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
