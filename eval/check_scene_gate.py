# eval/check_scene_gate.py
"""场景内容闸门检查（20261021-221446 的教训）。

句子里点名要摆进关卡的东西（金币 / 平台 / 门 / 进度中枢），理解器把它扔进 unresolved 时，
`plan_windows` 必须由代码补出 `place_batch` 窗口和 `editor_assert` 条款，不能只记一句「未完成」
就收工——那次 run 的 7 扇里一扇放置都没有，关卡是空的却记了 6 扇成功。

纯函数 + 打桩，不调模型、不动编辑器、不读真实 learned_routes。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

QUERY = (
    "在 UE 5.8 里做一个能玩的平台跳跃关卡：玩家操纵 ATestPawn，去碰旋转发光的金币（碰到后金币隐藏）、"
    "左上角 UMG 计数碰到的金币数，以及 90 秒倒计时，收齐金币开门显示通关、"
    "前段用于放金币的平台不动，后段的平台会动增加难度"
)

# 20261021-221446 那次 run 真实的窗口清单：只有 C++ 与 UI，一扇放置都没有
RUN_WINDOWS = [
    {"id": "W1", "action": "edit_cpp", "slots": {"class": "ATestPawn"}, "depends_on": []},
    {"id": "W2", "action": "create_widget_blueprint", "slots": {"name": "WBP_HUD"}, "depends_on": ["W1"]},
    {"id": "W3", "action": "edit_widget_blueprint", "slots": {}, "depends_on": ["W2"]},
    {"id": "W4", "action": "check_platform_hud", "slots": {}, "depends_on": ["W2"]},
    {"id": "W5", "action": "run_automation_test", "slots": {"filter": "AIAgent.Platform"}, "depends_on": ["W1"]},
    {"id": "W6", "action": "probe_play", "slots": {}, "depends_on": ["W5"]},
]

INTENT = {
    "query": QUERY,
    "ok": True,
    "goals": [{"action": "edit_cpp", "slots": {"class": "ATestPawn"}}],
    "criteria": [{"id": "c-cpp_build", "evidence_kind": "cpp_build", "text": "编译通过"}],
    "constraints": {"editor": False, "pie": True, "save": True},
    "unresolved": [
        "旋转发光的金币的旋转与发光视觉表现",
        "金币、门、平台的具体蓝图资产名与摆放位置",
    ],
}


def check_layouts() -> None:
    from agent.scene_gate import layouts

    found = {item["blueprint"] for item in layouts()}
    for name in ("BP_Coin", "BP_Platform", "BP_Door", "BP_Progress"):
        assert name in found, f"data/harness/level_layout 缺 {name} 的设计图"
    for item in layouts():
        assert item["names"], f"{item['file']} 里没有带 name 的条目"


def check_demand() -> None:
    from agent.scene_gate import demanded

    got = {item["blueprint"] for item in demanded(QUERY)}
    for name in ("BP_Coin", "BP_Platform", "BP_Door", "BP_Progress"):
        assert name in got, f"平台跳跃整句没认出要点 {name}"
    assert demanded("把 MI_Coin 的 Emissive 调到 5") == [], "只调材质的句子不该补放置扇"
    assert demanded("编译整个工程") == [], "「编译整个工程」不该补放置扇"
    assert demanded("把金币发光调亮一点") == [], "没有关卡语义时不该补放置扇"


def check_windows() -> None:
    from agent.scene_gate import ensure_scene_windows

    result = ensure_scene_windows(QUERY, RUN_WINDOWS, [])
    windows = result["windows"]
    placed = [window for window in windows if window.get("action") == "place_batch"]
    assert len(placed) >= 3, f"至少补金币/平台/门三扇，实际 {len(placed)}"
    assert len(placed) == len(result["added"]), "added 要和补上的扇数对得上"
    blueprints = {str((window.get("slots") or {}).get("blueprint")) for window in placed}
    for name in ("BP_Coin", "BP_Platform", "BP_Door", "BP_Progress"):
        assert name in blueprints, f"补窗里缺 {name}"

    ids = [str(window.get("id") or "") for window in windows]
    assert len(ids) == len(set(ids)), f"补窗后窗口 id 重复：{ids}"

    play_at = next((i for i, window in enumerate(windows) if window.get("action") == "probe_play"), None)
    assert play_at is not None
    last_place = max(i for i, window in enumerate(windows) if window.get("action") == "place_batch")
    assert last_place < play_at, "放置扇必须排在 PIE 之前，先摆好再玩"
    deps = [str(dep) for dep in (windows[play_at].get("depends_on") or [])]
    assert str(windows[last_place].get("id")) in deps, "PIE 扇要等最后一块放置"

    kinds = [str(item.get("evidence_kind")) for item in result["criteria"]]
    assert kinds.count("editor_assert") == len(placed), "每条设计图配一条 editor_assert"
    owners = [str((item.get("windows") or [""])[0]) for item in result["criteria"]
              if item.get("evidence_kind") == "editor_assert"]
    assert set(owners) == {str(window.get("id")) for window in placed}, \
        "场景条款要显式绑到它那一扇放置，不能被别的 editor_assert 扇勾上"


def check_idempotent() -> None:
    from agent.scene_gate import ensure_scene_windows

    once = ensure_scene_windows(QUERY, RUN_WINDOWS, [])
    twice = ensure_scene_windows(QUERY, once["windows"], once["criteria"])
    assert not twice["added"], f"已经补过的窗口被重复补：{twice['added']}"
    assert len(twice["windows"]) == len(once["windows"]), "第二次过闸门改了窗口清单"
    assert len(twice["criteria"]) == len(once["criteria"]), "第二次过闸门改了合同"

    plain = ensure_scene_windows("编译整个工程", RUN_WINDOWS, [])
    assert not plain["added"] and len(plain["windows"]) == len(RUN_WINDOWS), \
        "没有场景需求时闸门必须原样返回"


def check_plan_wiring() -> None:
    """打桩理解器，确认 plan_windows 真的把补出来的放置扇放进窗口清单和合同。"""
    import agent.understand as understand_mod
    import agent.plan_windows as pw

    saved_understand = understand_mod.understand
    saved_learned = pw.match_learned
    saved_planner = pw._complete_live

    def _no_planner(*_args, **_kwargs):
        raise AssertionError("场景闸门没接住，又去叫老规划者了")

    understand_mod.understand = lambda query, complete=None: dict(INTENT)
    pw.match_learned = lambda query, meta=None: None
    pw._complete_live = _no_planner
    try:
        planned = pw.plan_windows(QUERY, complete=None)
    finally:
        understand_mod.understand = saved_understand
        pw.match_learned = saved_learned
        pw._complete_live = saved_planner

    actions = [str(window.get("action") or "") for window in planned["windows"]]
    assert "place_batch" in actions, f"理解器漏了场景需求，代码也没补：{actions}"
    assert actions.count("place_batch") >= 3, f"放置扇太少：{actions}"
    kinds = [str(item.get("evidence_kind") or "") for item in planned["criteria"]]
    assert "editor_assert" in kinds, "合同里没有「关卡里有东西」的条款"
    unresolved = " ".join(planned.get("intent", {}).get("unresolved") or [])
    assert "摆放位置" not in unresolved, "已接住的摆放需求不该还留在 unresolved"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    check_layouts()
    check_demand()
    check_windows()
    check_idempotent()
    check_plan_wiring()
    print("scene gate ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
