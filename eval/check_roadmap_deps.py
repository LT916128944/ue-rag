# eval/check_roadmap_deps.py
"""路线图串行/并行：编辑器前置依赖必须写进 depends_on，独立步骤不许虚连。

背景（20261117-122532 实测）：一句话「启动编辑器，在指定关卡中的世界原点添加一个点光源。
参考关卡：/Game/Maps/L_NewTest2」被拆成 [launch_editor, open_level, place_light]，但
`windows_from_rules` 把 open_level 的 depends_on 留空。后果有两层：

1. 执行层：launch 与 open 都空依赖 ⇒ run_windows 在同一轮把它们一起派发；冷启动时
   open_level 会先跑去连 MCP，连不上（编辑器还没起来）。这一轮因为编辑器本来就在跑才没翻车。
2. 路线图层：布局按 depends_on 的拓扑深度分层，两层都是深度 0 ⇒ 画成同层并排、彼此无连线。
   用户看到的就是「启动 UE 编辑器」和「打开已有关卡」并排且不连。

修法：open_level / create_level 接在最近的 launch_editor 后面。本检查全离线，钉：

1. 启动 → 打开关卡 → 放灯，是一条串行链 W1→W2→W3（depends_on 逐级相接）；
2. 「打开编辑器，打开关卡」的 open_level 依赖 launch_editor；
3. 「打开编辑器，新建关卡」的 create_level 依赖 launch_editor；
4. 没有 launch_editor 的独立步骤（改码 / 移动）仍是空依赖，不造虚链（守住 check_upgrade
   的「去假链」口径）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _windows(text: str) -> list[dict]:
    """纯规则链（零模型）：句子 → 规则步骤 → 窗口（带 depends_on）。"""
    from agent.supervise import plan_sequence
    from agent.plan_windows import windows_from_rules

    return windows_from_rules(plan_sequence(text))


def _actions(windows: list[dict]) -> list[str]:
    return [str(w.get("action") or "") for w in windows]


def check_editor_serial_chain() -> str:
    """报告里的原句：必须拆成 launch → open → place_light 且逐级串行。"""
    windows = _windows("启动编辑器，在指定关卡中的世界原点添加一个点光源。参考关卡：/Game/Maps/L_NewTest2")
    check(_actions(windows) == ["launch_editor", "open_level", "place_light"],
          f"动作序列应为 [launch_editor, open_level, place_light]，实际 {_actions(windows)}")
    by_id = {w["id"]: w for w in windows}
    check(by_id["W1"]["depends_on"] == [],
          f"launch_editor 不该有上游依赖，实际 {by_id['W1']['depends_on']}")
    check(by_id["W2"]["depends_on"] == ["W1"],
          f"open_level 必须依赖 launch_editor（先启动再打开），实际 {by_id['W2']['depends_on']}")
    check(by_id["W3"]["depends_on"] == ["W2"],
          f"place_light 必须依赖 open_level（关卡打开后再放灯），实际 {by_id['W3']['depends_on']}")
    return "启动→打开关卡→放灯 = 串行链 W1→W2→W3"


def check_open_level_depends_on_launch() -> str:
    windows = _windows("打开编辑器，打开关卡 L_NewTest")
    check(_actions(windows) == ["launch_editor", "open_level"],
          f"动作序列应为 [launch_editor, open_level]，实际 {_actions(windows)}")
    check(windows[1]["depends_on"] == ["W1"],
          f"open_level 必须依赖 launch_editor，实际 {windows[1]['depends_on']}")
    return "打开编辑器+打开关卡：open_level 依赖 launch_editor（不再并排）"


def check_create_level_depends_on_launch() -> str:
    windows = _windows("打开编辑器，新建一个关卡，名字为L_NewTest2")
    check(_actions(windows) == ["launch_editor", "create_level"],
          f"动作序列应为 [launch_editor, create_level]，实际 {_actions(windows)}")
    check(windows[1]["depends_on"] == ["W1"],
          f"create_level 必须依赖 launch_editor，实际 {windows[1]['depends_on']}")
    return "打开编辑器+新建关卡：create_level 依赖 launch_editor"


def check_independent_not_chained() -> str:
    """没有 launch_editor 的独立读写仍为空依赖——不许把上面的修复变成「见谁都连」。"""
    windows = _windows("把场景里的 Cube 移到 (X=100,Y=200,Z=50)，并给 ATestActor 的 MoveSpeed 改成 200 再编译")
    by_action = {str(w.get("action") or ""): w for w in windows}
    check("move_actor" in by_action and "edit_cpp" in by_action, f"应拆出 move_actor 与 edit_cpp，实际 {_actions(windows)}")
    check(by_action["move_actor"]["depends_on"] == [],
          f"move_actor 独立，不该有依赖，实际 {by_action['move_actor']['depends_on']}")
    check(by_action["edit_cpp"]["depends_on"] == [],
          f"edit_cpp 独立（无 launch_editor），不该有依赖，实际 {by_action['edit_cpp']['depends_on']}")
    return "独立步骤（无 launch_editor）保持空依赖，不造虚链"


def main() -> int:
    try:
        rows = [
            check_editor_serial_chain(),
            check_open_level_depends_on_launch(),
            check_create_level_depends_on_launch(),
            check_independent_not_chained(),
        ]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    for row in rows:
        print("  " + row)
    print("roadmap deps ok（编辑器前置依赖已声明；路线图串行/并行与执行顺序一致）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
