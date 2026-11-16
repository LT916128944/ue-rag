# eval/check_fps_hud.py
"""FPS HUD 链路离线护栏（20261029）。

守三条，都不连编辑器、不调模型：

1. **零模型拆句**：整句 FPS HUD 需求必须由 `agent.fps_hud.hud_windows` 直接给出窗口清单，
   不去叫理解器。20261029-115122 / 115302 两次实测里理解器返回纯文本，整句掉进求解扇失败——
   拆句是纯规则，不该被模型可用性单点卡死。
2. **模块完整性**：`build_fps_hud` / `hud_windows` / `_monolith_ui` 等入口必须真实可调用。
   20261029-120658 的 W4 报 `NameError: _monolith_ui is not defined`：一次编辑把函数头
   吞掉了，函数体变成孤立语句，`py_compile` 依然是绿的（语法合法），只有运行时才炸。
   这里用真实调用把这种结构性错误挡在离线阶段。
3. **合同一致性**：`_check_fps_hud` 期望的控件名、C++ 头里的 BindWidgetOptional 字段名、
   `HUD_LAYOUT` 里的名字三者必须一致；否则「控件都在」的判词会和 C++ 绑不上。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

QUERY = (
    "打开编辑器，在FPS游戏关卡L_NewTest3添加一个UMG到视口，左下角显示血条，满血100，"
    "暂时还没有扣血，就占位用。然后右下角显示子弹数量，还有手雷数量。"
    "右中部显示载具信息例如名字，是否上车/下车提示按键位E，还有当前车辆时速"
)

# P1 反例（20261111）：HUD 半句 + 玩法半句。这条链只认领得住整句的纯 HUD 需求。
MIXED = "给 FPS 加血条和子弹显示，同时给玩家加滑铲"

# P2 探针（20261111）：速度、按键、冷却、冷却读数四件事，表内动作一件都接不住。
SPRINT = (
    "在 L_NewTest 给玩家加冲刺：按住 Left Shift 时最大行走速度乘 1.8，"
    "松开恢复，冷却 1.5 秒，屏幕左下角显示剩余冷却。"
)


def check_hud_plan_is_deterministic() -> None:
    """整句 FPS HUD 由规则拆开：五个窗口、零模型。"""
    from agent.fps_hud import hud_windows

    windows = hud_windows(QUERY)
    assert windows, "hud_windows 没有给出窗口清单"
    actions = [str(w.get("action") or "") for w in windows]
    for expected in ("launch_editor", "open_level", "create_widget_blueprint",
                     "edit_widget_blueprint", "check_fps_hud"):
        assert expected in actions, f"FPS HUD 窗口清单缺 {expected}：{actions}"
    assert all(w.get("ok") for w in windows), f"有窗口不可实现：{windows}"

    # 依赖必须成链：检查扇抢跑到改图扇之前，会把正确结果报成「缺控件」
    # （20261029-123341 的 W5）。这里逐扇断言「依赖就是上一扇」。
    for index, window in enumerate(windows):
        expected = [windows[index - 1]["id"]] if index else []
        assert list(window.get("depends_on") or []) == expected, (
            f"{window.get('id')} 的依赖应为 {expected}，实际 {window.get('depends_on')}")

    # 非 HUD 句子不许被这条规则接走
    assert hud_windows("把按钮颜色改成红色") is None, "非 HUD 句子被 hud_windows 接走了"


def check_mixed_sentence_not_claimed_by_hud_rules() -> None:
    """门只认整句（20261111 P1）：HUD + 玩法混句不许被 hud-rules 整句领走。

    纯 HUD 句仍是上面的五扇链；多一句玩法时 `hud_windows` 必须是 None，
    `plan_windows` 的来源也不许是 hud-rules——玩法半句交回理解器/求解，
    不能像 20261029 那样 HUD 摆了、滑铲半句静默蒸发。
    """
    from agent.fps_hud import hud_windows
    from agent.plan_windows import plan_windows

    assert hud_windows(MIXED) is None, "混句被 hud_windows 整句领走了"

    # 理解器 stub：只点中表内改码动作，玩法半句进 unresolved。
    # 求解开着时来源是 solver-fallback，关着时是 understood——都不许是 hud-rules。
    intent_stub = json.dumps({
        "goals": [{"action": "edit_cpp", "slots": {"class": "AFpsPawn"}}],
        "unresolved": ["给玩家加滑铲"],
    }, ensure_ascii=False)
    planned = plan_windows(MIXED, complete=lambda _messages: intent_stub)
    assert planned.get("source") != "hud-rules", f"混句来源不许是 hud-rules：{planned.get('source')}"


def check_compile_sentence_stays_zero_model() -> None:
    """「编译工程」仍是 rules 强命中：理解器、规划者一次都不许叫。"""
    from agent.plan_windows import plan_windows

    def boom(_messages):
        raise AssertionError("不该叫模型")

    planned = plan_windows("编译工程", complete=boom)
    assert planned.get("source") == "rules", planned.get("source")
    assert planned.get("understand_called") is False
    assert planned.get("planner_called") is False


def check_uncovered_sentence_goes_to_solver() -> None:
    """P2（20261111）：表内计划盖不住整句时，只问一次。

    - 求解开着：source 进求解兜底（solver-fallback），规划者调用次数是 0，
      窗口里是一扇受控求解扇；
    - 求解关着：停在缺口——不把冷却和按键静默丢掉（unresolved 还在），
      任务书人审门必须拦下，不许只剩一扇「改单个类」的窗就直接执行。
    """
    from agent.plan_windows import plan_windows
    from agent.run_windows import _needs_brief
    from eval._pin_offline import closed_env

    intent_stub = json.dumps({
        "goals": [{"action": "edit_cpp", "slots": {"class": "AFpsPawn"}}],
        "unresolved": ["屏幕左下角显示剩余冷却（冲刺冷却读数）"],
    }, ensure_ascii=False)

    with closed_env(UE_SOLVER_ENABLED="true"):
        planned = plan_windows(SPRINT, complete=lambda _messages: intent_stub)
        assert planned.get("source") == "solver-fallback", planned.get("source")
        assert planned.get("planner_called") is False, "盖不住时不许再叫规划者"
        assert any(
            str(w.get("action") or "") == "solve" for w in planned.get("windows") or []
        ), planned.get("windows")

    with closed_env():
        planned = plan_windows(SPRINT, complete=lambda _messages: intent_stub)
        assert planned.get("source") == "understood", planned.get("source")
        assert planned.get("planner_called") is False
        unresolved = (planned.get("intent_full") or {}).get("unresolved") or []
        assert any("冷却" in str(item) for item in unresolved), unresolved
        assert _needs_brief(planned, SPRINT) is True, "盖不住且求解关着时必须停在人审门"


def check_plan_windows_uses_rules_without_model() -> None:
    """plan_windows 走 hud-rules：planner/understand 都不许被调用。"""
    from agent.plan_windows import plan_windows

    planned = plan_windows(QUERY)
    assert planned.get("source") == "hud-rules", f"来源不是 hud-rules：{planned.get('source')}"
    assert planned.get("planner_called") is False, "规划者被调用了"
    assert planned.get("understand_called") is False, "理解器被调用了"
    assert len(planned.get("windows") or []) >= 5, "窗口数不足"


def check_brief_skipped_for_deterministic_plan() -> None:
    """确定性拆句不走任务书人审门（省一次生成器调用，也少一条非 JSON 噪声）。"""
    from agent.plan_windows import plan_windows
    from agent.run_windows import _needs_brief

    planned = plan_windows(QUERY)
    assert _needs_brief(planned, QUERY) is False, "hud-rules 仍去写任务书了"
    # 非确定性来源行为不变：>3 扇仍要任务书
    assert _needs_brief({"source": "planner", "windows": [{}, {}, {}, {}]}, QUERY) is True


def check_module_entries_callable() -> None:
    """入口必须真实存在且可调用（防「函数头被吞、函数体成孤语句」）。"""
    from agent import fps_hud

    for name in ("is_fps_hud_request", "widget_ref_from", "build_fps_hud",
                 "hud_windows", "HUD_LAYOUT", "_monolith_ui"):
        assert hasattr(fps_hud, name), f"agent.fps_hud 缺 {name}"
    assert callable(fps_hud.build_fps_hud)
    assert callable(fps_hud._monolith_ui)

    step = {"action": "edit_widget_blueprint", "query": QUERY,
            "ref_path": "/Game/UI/WBP_FpsHUD.WBP_FpsHUD"}
    # 非 monolith 后端下 build_fps_hud 必须让路（返回 None），不能吞掉别的后端
    os.environ["UE_EDITOR_BACKEND"] = "official"
    try:
        from config import editor_backend

        if editor_backend() == "official":
            assert fps_hud.build_fps_hud(step) is None, "official 后端下 build_fps_hud 应让路"
    finally:
        os.environ.pop("UE_EDITOR_BACKEND", None)


def check_contract_names_match_cpp() -> None:
    """判词期望名 / HUD_LAYOUT / C++ BindWidgetOptional 三处一致。"""
    from agent.fps_hud import HUD_LAYOUT
    from agent.sequence import FPS_HUD_WIDGETS

    layout_names = [name for name, *_ in HUD_LAYOUT]
    assert sorted(layout_names) == sorted(FPS_HUD_WIDGETS), (
        f"HUD_LAYOUT 与 _check_fps_hud 合同不一致：{layout_names} vs {list(FPS_HUD_WIDGETS)}")

    try:
        from config import UE_SOURCE_ROOT

        header = Path(UE_SOURCE_ROOT) / "Public" / "FpsHUDWidget.h"
    except Exception:
        header = ROOT.parent.parent / "AIAgentProject" / "Source" / "AIAgentProject" / "Public" / "FpsHUDWidget.h"
    if not header.is_file():
        print(f"skip: 找不到 {header}（非本机布局），只校验前两条")
        return
    text = header.read_text(encoding="utf-8", errors="replace")
    declared = set(re.findall(r"TObjectPtr<UTextBlock>\s+(\w+)\s*;", text))
    missing = [name for name in FPS_HUD_WIDGETS if name not in declared]
    assert not missing, f"C++ 头里没有这些 BindWidgetOptional 字段：{missing}（实际 {sorted(declared)}）"


def main() -> None:
    check_hud_plan_is_deterministic()
    check_mixed_sentence_not_claimed_by_hud_rules()
    check_compile_sentence_stays_zero_model()
    check_uncovered_sentence_goes_to_solver()
    check_plan_windows_uses_rules_without_model()
    check_brief_skipped_for_deterministic_plan()
    check_module_entries_callable()
    check_contract_names_match_cpp()
    print("fps hud ok")


if __name__ == "__main__":
    main()
