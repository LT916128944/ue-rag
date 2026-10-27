"""规则盖不住的半句交给理解器分配，不按业务词补动作。

不打开编辑器、不调真实模型。理解器用假的 complete。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

QUERY = "编译代码，打开编辑器，打开level3关卡，看看射击游戏是否有问题"


def _intent(goals: list[dict], unresolved: list[str]) -> str:
    return json.dumps({
        "query": QUERY,
        "goals": goals,
        "criteria": [
            {"id": "c-cpp_build", "evidence_kind": "cpp_build", "text": "编译通过"},
        ],
        "constraints": {"editor": True},
        "unresolved": unresolved,
    }, ensure_ascii=False)


def check_rules_do_not_swallow() -> None:
    from agent.catalog import CATALOG
    from agent.brief import needs_brief
    from agent.plan_windows import _action_of, _rules_cover, plan_windows
    from agent.supervise import _plan_rules

    assert "check_shooter" not in CATALOG
    ruled = _plan_rules(QUERY)
    assert [_action_of(step) for step in ruled or []] == ["compile_project"]
    assert _rules_cover(QUERY, ruled) is False, ruled

    seen = {"n": 0}

    def model(_messages):
        seen["n"] += 1
        return _intent(
            [
                {"action": "compile_project", "slots": {}},
                {"action": "open_level", "slots": {"level": "level3"}},
            ],
            ["看看射击游戏是否有问题"],
        )

    planned = plan_windows(QUERY, complete=model)
    assert seen["n"] >= 1
    assert planned["source"] == "understood", planned.get("source")
    assert planned["planner_called"] is False
    actions = [window.get("action") for window in planned["windows"]]
    assert actions == ["compile_project", "open_level"], actions
    assert "check_shooter" not in actions
    assert planned["windows"][1].get("slots", {}).get("level") == "level3"
    unresolved = (planned.get("intent_full") or planned.get("intent") or {}).get("unresolved") or []
    assert unresolved == ["看看射击游戏是否有问题"], unresolved
    assert needs_brief(QUERY, planned["windows"], planned.get("intent_full")) is True


def check_plain_compile_stays_rules() -> None:
    from agent.plan_windows import plan_windows

    def boom(_messages):
        raise AssertionError("整句已被规则接住，不应再叫模型")

    plain = plan_windows("编译工程后启动 UE 编辑器", complete=boom)
    assert plain["source"] == "rules"
    assert [window.get("action") for window in plain["windows"]] == ["compile_project"]

    both = plan_windows("编译代码，打开编辑器", complete=boom)
    assert both["source"] == "rules", both.get("source")
    assert [window.get("action") for window in both["windows"]] == ["compile_project"]

    from agent.supervise import _plan_rules
    from agent.plan_windows import _rules_cover

    glued = "编译代码看看有没有问题"
    glued_rules = _plan_rules(glued)
    assert _rules_cover(glued, glued_rules) is False, glued_rules
    seen = {"n": 0}

    def model(_messages):
        seen["n"] += 1
        return json.dumps({
            "query": glued,
            "goals": [{"action": "compile_project", "slots": {}}],
            "criteria": [],
            "constraints": {},
            "unresolved": ["看看有没有问题"],
        }, ensure_ascii=False)

    planned = plan_windows(glued, complete=model)
    assert seen["n"] >= 1
    assert planned["source"] == "understood"
    assert "probe_play" not in [window.get("action") for window in planned["windows"]]
    assert (planned.get("intent_full") or {}).get("unresolved") == ["看看有没有问题"]

    created = plan_windows("打开编辑器，新建一个关卡 L_NewTest2", complete=boom)
    assert created["source"] == "rules"
    assert [window.get("action") for window in created["windows"]] == ["launch_editor", "create_level"]


if __name__ == "__main__":
    check_rules_do_not_swallow()
    check_plain_compile_stays_rules()
    print("ok")
