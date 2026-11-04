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
    check_plan_windows_uses_rules_without_model()
    check_brief_skipped_for_deterministic_plan()
    check_module_entries_callable()
    check_contract_names_match_cpp()
    print("fps hud ok")


if __name__ == "__main__":
    main()
