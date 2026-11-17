# eval/check_place_light.py
"""place_light 的拆窗不再被「启动编辑器 / 打开关卡 / 编译」同句吃掉（20261117-1015 修复）。

真 bug（离线可复现）：`supervise._plan_rules` 的 `launch_only` 分支排在 place_light 分支
之前，且 `launch_only` 的排除项里没有「放灯」。于是：

    「打开编辑器，在关卡世界原点添加一个点光源」

launch_only 命中 → 只拆出 `[launch_editor]`，放灯半句丢失 → `_rules_cover` 判弱命中 →
掉进理解器/规划者；离线下（没有模型）落成 `[launch_editor, 空 action 收口窗]`，灯根本不会放。
`plan_windows` 里给 place_light 的注释恰恰把这一句当成「可确定性拆」的样板，实际却没接住。

修法：`launch_only` 增加 `and not place_light_hit`；放灯与「建关卡 / 开关卡 / 编译 / 改码」
同句时，在 `_plan_rules` 的通用步骤里追加一扇 place_light（顺序交给 windows_from_rules 的依赖）。

本检查全离线（`complete` 替身抛异常，`_complete_live` 也被禁），钉：
1. 纯放灯/启动+放灯整句走 rules 快路径（零模型），窗口动作序列正确，place_light 槽位带 light_type/location；
2. launch_only 没被改坏：只要求启动编辑器时仍是单扇 launch_editor；
3. 不相关规则（编译工程）不受影响；
4. 放灯与 open_level / 编译同句时，`_plan_rules` 的动作集合里**必须仍有 place_light**（不许被丢掉）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _offline_plan(text: str) -> dict:
    """封闭环境里跑真规划链；`complete` 是抛异常的替身（一次模型调用都不许发生）。"""
    from eval._pin_offline import closed_env

    def _stub(_messages):
        raise RuntimeError("离线检查不许叫模型（plan_windows 的 complete 替身）")

    with closed_env(UE_SOLVER_ENABLED="true"):
        from agent import plan_windows as module

        saved = module._complete_live

        def _forbid(_messages):
            raise AssertionError("离线检查里不许走 _complete_live（它会真的调模型）")

        module._complete_live = _forbid
        try:
            planned = module.plan_windows(text, complete=_stub)
        finally:
            module._complete_live = saved
    return planned or {}


def _actions(planned: dict) -> list[str]:
    return [str(window.get("action") or "") for window in (planned.get("windows") or []) if isinstance(window, dict)]


def _window_by_action(planned: dict, action: str):
    for window in planned.get("windows") or []:
        if isinstance(window, dict) and str(window.get("action") or "") == action:
            return window
    return None


def check_launch_plus_light() -> str:
    """核心回归：启动 + 放灯同句，必须 rules 且两扇都在，灯槽位正确。"""
    text = "打开编辑器，在关卡世界原点添加一个点光源"
    planned = _offline_plan(text)
    check(str(planned.get("source")) == "rules",
          f"「{text}」应走 rules 快路径（零模型），实际 source={planned.get('source')!r}")
    check(_actions(planned) == ["launch_editor", "place_light"],
          f"「{text}」窗口动作应为 [launch_editor, place_light]，实际 {_actions(planned)}")
    light = _window_by_action(planned, "place_light") or {}
    slots = light.get("slots") or {}
    check(str(slots.get("light_type") or "") == "点光源",
          f"place_light.light_type 应为「点光源」，实际 {slots.get('light_type')!r}")
    check(list(slots.get("location") or []) == [0, 0, 0],
          f"句里点名「世界原点」时 location 应为 [0,0,0]，实际 {slots.get('location')!r}")
    check(list(light.get("depends_on") or []),
          f"place_light 必须依赖 launch_editor（先启动再放灯），实际 depends_on={light.get('depends_on')}")
    return "启动+放灯 → rules [launch_editor, place_light]，灯槽位与依赖正确"


def check_simple_lights() -> str:
    """纯放灯各类别 → 单扇 place_light，light_type 归地正确。"""
    cases = {
        "在关卡里放一盏聚光灯": "聚光灯",
        "添加一个方向光": "方向光",
        "在关卡里添加一个矩形光": "矩形光",
    }
    for text, want in cases.items():
        planned = _offline_plan(text)
        check(str(planned.get("source")) == "rules",
              f"「{text}」应走 rules 快路径，实际 {planned.get('source')!r}")
        check(_actions(planned) == ["place_light"],
              f"「{text}」应只拆出一扇 place_light，实际 {_actions(planned)}")
        light = _window_by_action(planned, "place_light") or {}
        got = str((light.get("slots") or {}).get("light_type") or "")
        check(got == want, f"「{text}」light_type 应为「{want}」，实际 {got!r}")
        check(str(light.get("action") or "") == "place_light", "place_light 扇的 action 不该是空")
    return "聚光灯 / 方向光 / 矩形光 都单扇 place_light，light_type 正确"


def check_launch_only_not_broken() -> str:
    """launch_only 不能被「放灯」修复带坏：只说启动编辑器时仍是单扇 launch_editor。"""
    for text in ("打开编辑器", "启动 UE 编辑器"):
        planned = _offline_plan(text)
        check(str(planned.get("source")) == "rules",
              f"「{text}」应走 rules，实际 {planned.get('source')!r}")
        check(_actions(planned) == ["launch_editor"],
              f"「{text}」应只拆出一扇 launch_editor，实际 {_actions(planned)}")
    return "只要求启动编辑器仍是单扇 launch_editor（launch_only 没有被带坏）"


def check_unrelated_rule_intact() -> str:
    """不相关规则不受影响：编译工程仍是单扇 compile_project。"""
    planned = _offline_plan("编译工程")
    check(str(planned.get("source")) == "rules", f"「编译工程」应走 rules，实际 {planned.get('source')!r}")
    check(_actions(planned) == ["compile_project"], f"「编译工程」应只拆出一扇 compile_project，实际 {_actions(planned)}")
    return "编译工程仍单扇 compile_project（无关规则未受影响）"


def check_place_light_not_dropped_when_combined() -> str:
    """放灯与 open_level / 编译同句时，`_plan_rules` 里仍必须有 place_light 扇。

    这两句可能因别的半句覆盖不全而落到 L1（不强制 source），但「放灯扇被整条丢掉」是真 bug。
    只看 `_plan_rules` 的动作集合：包含 place_light 即可。open_level 句子不要求它 resolve 成功
    （CI 上没有索引时 open_level 扇会 blocked，但 place_light 扇仍要在）。
    """
    from eval._pin_offline import closed_env

    with closed_env(UE_SOLVER_ENABLED="true"):
        from agent.supervise import _plan_rules

        for text in (
            "打开关卡 L_NewTest3，在关卡世界原点添加一个点光源",
            "在关卡里加一个点光源，然后编译工程",
        ):
            steps = _plan_rules(text) or []
            actions = [str(step.get("action") or "") for step in steps]
            check("place_light" in actions,
                  f"「{text}」的 _plan_rules 动作 {actions} 里丢了 place_light（放灯扇被同句其它动作吃掉）")
    return "放灯与 open_level / 编译同句时仍保留 place_light 扇"


def main() -> int:
    try:
        rows = [
            check_launch_plus_light(),
            check_simple_lights(),
            check_launch_only_not_broken(),
            check_unrelated_rule_intact(),
            check_place_light_not_dropped_when_combined(),
        ]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print("  " + row)
    print("place_light plan ok（放灯扇不再被启动/开关卡/编译同句吃掉）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
