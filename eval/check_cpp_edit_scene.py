# eval/check_cpp_edit_scene.py
"""`_is_cpp_edit` 的「加一个 / 添加 / 新增 + 场景物体」假阳性（20261117 修复）。

真 bug：`_CPP_EDIT_RE` 里有「新增 / 加一个 / 添加」，`_is_cpp_edit("在关卡里加一个点光源")`
曾返回 True。后果：`_is_project_compile("…加一个点光源，然后编译工程")` 被 `_is_cpp_edit`
带成 False → 编译扇从规则里丢掉 → 「加个灯，然后编译工程」整句掉给模型（模型/求解收口还
可能把灯加上了却记失败）。

修法：句里只有场景名词（灯 / 模型 / 道具 / 平台…）、没有任何 C++ 符号时，「加一个 / 添加 /
新增」不算改代码；出现 C++ 符号（属性 / 函数 / 组件…）仍按改代码。

本检查全离线（不连编辑器、不调模型），钉：
1. 场景摆放的「加一个点光源 / 添加一个聚光灯 / 新增一个方向光 / 加一个平台 / 添加一个金币」
   → `_is_cpp_edit` 为 False；
2. 真改 C++ 的「新增一个属性 / 加一个函数 / 添加一个变量 / 加一个组件 / 改成… / 修改 .cpp」
   → `_is_cpp_edit` 仍为 True（不放宽改码识别）；
3. 修编译错误仍返回 False；
4. 端到端：「在关卡里加一个点光源，然后编译工程」零模型落 rules、动作 [place_light, compile_project]。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


SCENE_ADDS = [
    "在关卡里加一个点光源",
    "添加一个聚光灯",
    "新增一个方向光",
    "在关卡里添加一个矩形光",
    "加一个平台",
    "添加一个金币",
]
CODE_EDITS = [
    "新增一个是否可被拾取的属性",
    "加一个函数",
    "添加一个变量",
    "给 Player 添加一个跳跃组件",
    "把 FpsPawn 的射速改成每秒 10 发",
    "修改 Private/TestPawn.cpp 的跳跃落地逻辑",
]


def check_guard() -> str:
    from agent.supervise import _is_cpp_edit

    for text in SCENE_ADDS:
        check(_is_cpp_edit(text) is False, f"「{text}」是场景摆放，不该判成改 C++")
    for text in CODE_EDITS:
        check(_is_cpp_edit(text) is True, f"「{text}」是改 C++，必须仍判 True（别放宽）")
    check(_is_cpp_edit("编译错误 error C2065 修一下") is False, "修编译错误不算新增改码")
    return "加一个/添加/新增+场景物体 → 不算改 C++；属性/函数/组件… → 仍算；修编译错误不算"


def check_end_to_end() -> str:
    from eval._pin_offline import closed_env

    def _stub(_messages):
        raise RuntimeError("离线检查不许叫模型（plan_windows 的 complete 替身）")

    text = "在关卡里加一个点光源，然后编译工程"
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
    source = str((planned or {}).get("source") or "")
    actions = [str(w.get("action") or "") for w in (planned.get("windows") or []) if isinstance(w, dict)]
    check(source == "rules", f"「{text}」应零模型落 rules，实际 source={source!r}")
    check(actions == ["place_light", "compile_project"],
          f"「{text}」动作应为 [place_light, compile_project]，实际 {actions}")
    return "「加一个点光源，然后编译工程」→ rules，[place_light, compile_project]"


def main() -> int:
    try:
        rows = [check_guard(), check_end_to_end()]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print("  " + row)
    print("cpp edit scene ok（场景「加一个」不再被当成改代码）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
