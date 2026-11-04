# eval/check_widget_slot_gaps.py
"""NamedSlot / WidgetClassInfo 的能力缺口护栏（20261029 升级计划 §2 残差 2）。

这两条 monolith **确实没有**对应 action，不许假装支持。与其让它们在
`agent/plan_windows.py:_WIDGET_EDIT_TOOLS` 里被当成「可写原语」发出去、最后由桥笼统
拒绝，不如钉成「人工清单」：UNSUPPORTED 里带原因、原因里写清归人工、断言它们永远
不出现在 MAPPED。

核对依据（只读源码，不连编辑器）：

- `ui` 命名空间的 action 全表里没有命名槽内容写入（`MonolithUIActions.cpp` 的
  `NamedSlot` 只是 `list_widget_types` 的一行类名 + `MonolithUICommon.cpp` 的类映射，
  `add_widget` 走 `UPanelWidget::AddChild`，`UNamedSlot` 不是 UPanelWidget，进不去）。
- 没有类级查询：`MonolithUIBindingActions.cpp` 的 `ui.list_widget_properties` 必须给
  `asset_path` + `widget_name`，只能按 WBP 里的控件实例查，查不了类信息。

守四条：

1. 两条都在 UNSUPPORTED，且不在 MAPPED（也不和 MAPPED 重叠）。
2. 原因非空，且明确提到「人工」（归入人工清单，不是笼统「不支持」）。
3. `exec_bridge` 对它们返回结构化拒绝：handled=True、ok=False、error=原因原文，
   并且**不会**去调 monolith（缺能力不是去远端试一次再说）。
4. 缺口清单可被消费方读到：`manual_gap_tools()` 只回这两条相关的，供人工清单展示；
   同时确认 `ui` 侧那三条真实存在的 action 名字没被编进来当替代品。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval._pin_offline import close_now  # noqa: E402

close_now()

# 这两条是计划里点名的「真的没有对应 action」，必须是人工清单常客。
MANUAL_TOOLS = ("SetNamedSlotContent", "GetWidgetClassInfo")


def check_still_unsupported() -> None:
    """① 仍在 UNSUPPORTED，且不在 MAPPED。"""
    from core.monolith_bridge import MAPPED, UNSUPPORTED

    for tool in MANUAL_TOOLS:
        assert tool in UNSUPPORTED, f"{tool} 不在 UNSUPPORTED——要么真落了 monolith 路径并改了断言，要么漏了"
        assert tool not in MAPPED, f"{tool} 出现在 MAPPED：monolith 没有这个能力，不许假装支持"
    overlap = set(MAPPED) & set(UNSUPPORTED)
    assert not overlap, f"原语同时出现在 MAPPED 和 UNSUPPORTED：{overlap}"


def check_reason_mentions_manual() -> None:
    """② 原因非空，且写清「归人工」，不是笼统拒绝。"""
    from core.monolith_bridge import UNSUPPORTED, unsupported_reason

    for tool in MANUAL_TOOLS:
        reason = str(UNSUPPORTED[tool] or "")
        assert reason.strip(), f"{tool} 的不支持声明没有原因"
        assert "人工" in reason, f"{tool} 的原因没提「人工清单」：{reason}"
        assert unsupported_reason(tool) == reason, f"{tool} 的 unsupported_reason 与表不一致"
        # 拒绝理由要可读：太短/太长都不行，且不能只是复述「后端不支持」
        assert 8 <= len(reason) <= 200, f"{tool} 的原因长度不合适（{len(reason)}）：{reason}"
        assert "UE_EDITOR_BACKEND" not in reason, (
            f"{tool} 的原因还是笼统的后端拒绝，没写清能力缺口：{reason}")


def check_exec_bridge_refuses_offline() -> None:
    """③ 结构化拒绝，且不去调 monolith。"""
    from core.monolith_bridge import UNSUPPORTED, exec_bridge

    def _boom(*_args, **_kwargs):
        raise AssertionError("能力缺口不该去调 monolith（会把缺能力报成远端错误）")

    import backends.monolith as monolith_mod

    saved = monolith_mod.exec
    monolith_mod.exec = _boom
    try:
        for tool in MANUAL_TOOLS:
            handled, result = exec_bridge(tool, {"widgetBlueprint": {"refPath": "/Game/UI/WBP_X.WBP_X"}}, {})
            assert handled is True, f"{tool} 没被桥拒绝（handled={handled}），会掉进笼统的后端拒绝"
            assert result.get("ok") is False, f"{tool} 报成功了：{result}"
            assert str(result.get("error") or "") == UNSUPPORTED[tool], (
                f"{tool} 的拒绝原文应等于 UNSUPPORTED 原因，实际 {result.get('error')!r}")
    finally:
        monolith_mod.exec = saved


def check_manual_list_exposed() -> None:
    """④ 人工清单可读，且没有把 ui.* 的真实 action 名字编进来。"""
    from core.monolith_bridge import MAPPED, UNSUPPORTED, manual_gap_tools

    gaps = manual_gap_tools()
    assert isinstance(gaps, dict), f"manual_gap_tools 应回 dict，实际 {type(gaps).__name__}"
    for tool in MANUAL_TOOLS:
        assert tool in gaps, f"manual_gap_tools 里没有 {tool}：{sorted(gaps)}"
        assert gaps[tool] == UNSUPPORTED[tool], f"{tool} 的人工清单原因与 UNSUPPORTED 不一致"
    assert set(gaps) <= set(UNSUPPORTED), f"人工清单里混进了非 UNSUPPORTED 原语：{set(gaps) - set(UNSUPPORTED)}"
    assert not (set(gaps) & set(MAPPED)), f"人工清单与 MAPPED 撞了：{set(gaps) & set(MAPPED)}"

    # 缺口不是「把 ui 的既有 action 换个名字」。这些真实存在的 action 不能被
    # 当作这两条 official 原语的替代品写进桥。
    for real_action in ("list_widget_properties", "list_widget_events", "get_widget_bindings"):
        assert real_action not in MAPPED, f"{real_action} 是 monolith action 名，不是 official 原语，不该进 MAPPED"
        assert real_action not in UNSUPPORTED, f"{real_action} 不该出现在 UNSUPPORTED（它真实存在）"


def main() -> None:
    check_still_unsupported()
    check_reason_mentions_manual()
    check_exec_bridge_refuses_offline()
    check_manual_list_exposed()
    print("widget slot gaps ok")


if __name__ == "__main__":
    main()
