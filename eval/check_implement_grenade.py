# eval/check_implement_grenade.py
"""implement_grenade 的三态断言：缺 FPS 前置不算过、补拾取物幂等、automation 才收口。

仿 check_implement_shooter 的假 exec：不连编辑器、不调模型。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _loaded(path="/Game/Maps/L_NewTest3"):
    return {"ok": True, "data": {"loaded": True, "current_world": path, "path": path}}


def _stats(counts):
    return {"ok": True, "data": {"actor_count": counts}}


def _saved():
    return {"ok": True, "data": {"ok": True, "saved": 1, "requested": 1}}


def _authored(classes):
    return {"ok": True, "data": {"actor_instances": [
        {"class": cls, "spawned": True} for cls in classes
    ]}}


def _no_editor():
    def fake_exec(namespace, action, params=None, task=None, transport=None):
        return {"ok": False, "error": f"不该调用 {action}"}

    return fake_exec


def _fake_auto(result):
    def fake_auto(_step):
        return {
            "ok": result,
            "summary": ("失败数 0（1 个测试） tests=AIAgent.Fps.Grenade" if result else "失败数 1"),
            "test": {"ok": result, "kind": "automation", "tests": ["AIAgent.Fps.Grenade"]},
            "judge_kind": "automation",
        }
    return fake_auto


def check_missing_level_slot_is_not_a_guess() -> None:
    """level 槽位缺失照旧暂停，不猜。"""
    from agent import sequence

    out = sequence._implement_grenade({"slots": {}, "save": True})
    assert out.get("ok") is False, out
    assert "不猜" in str(out.get("error") or ""), out


def check_missing_fps_prerequisite_guides_shooter() -> None:
    """关卡里没有 FpsPawn/靶/地面/灯：引导先跑 implement_shooter，不算过、不跑 automation。"""
    from agent import sequence
    from eval.check_implement_shooter import _restore, _with_editor

    automation = {"called": False}

    def fake_exec(namespace, action, params=None, task=None, transport=None):
        if action == "load_level":
            return _loaded()
        if action == "get_scene_statistics":
            return _stats({"WorldSettings": 1, "Brush": 1})
        return _no_editor()(namespace, action, params)

    state = _with_editor(fake_exec, _fake_auto(False))
    try:
        out = state[2]._implement_grenade({"slots": {"level": "L_NewTest3"}, "save": True})
    finally:
        _restore(*state)
    assert out.get("ok") is False, out
    assert "implement_shooter" in str(out.get("error") or ""), out
    assert automation["called"] is False, out


def check_spawns_pickups_then_automation() -> None:
    """有 FPS 前置没拾取物：补足两个、保存、以 automation 收口。"""
    from agent import sequence
    from eval.check_implement_shooter import _restore, _with_editor

    calls = []
    saved = {"n": 0}
    spawned = {"pickups": 0}

    def fake_exec(namespace, action, params=None, task=None, transport=None):
        calls.append(action)
        if action == "load_level":
            return _loaded()
        if action == "get_scene_statistics":
            counts = {
                "FpsPawn": 1, "FpsTarget": 3, "StaticMeshActor": 1,
                "DirectionalLight": 1, "PlayerStart": 1,
            }
            if spawned["pickups"]:
                counts["FpsGrenadePickup"] = spawned["pickups"]
            return _stats(counts)
        if action == "author_map_settings":
            actors = (params or {}).get("actors") or []
            classes = [str(row.get("class")) for row in actors]
            assert len(classes) == 2, classes
            assert all(cls.endswith("FpsGrenadePickup") for cls in classes), classes
            spawned["pickups"] += len(classes)
            return _authored(classes)
        if action == "save_packages":
            saved["n"] += 1
            return _saved()
        return _no_editor()(namespace, action, params)

    state = _with_editor(fake_exec, _fake_auto(True))
    try:
        out = state[2]._implement_grenade({"slots": {"level": "L_NewTest3"}, "save": True})
    finally:
        _restore(*state)
    assert out.get("ok") is True, out
    assert out.get("in_level") is True, out
    assert out.get("judge_kind") == "automation", out
    assert saved["n"] == 1, saved
    assert calls == [
        "load_level", "get_scene_statistics", "author_map_settings",
        "save_packages", "get_scene_statistics",
    ], calls
    assert "FpsGrenadePickup=2" in str(out.get("summary") or ""), out


def check_idempotent_when_pickups_already_there() -> None:
    """拾取物已够：不重复生成、不保存，直接 automation 收口。"""
    from agent import sequence
    from eval.check_implement_shooter import _restore, _with_editor

    calls = []

    def fake_exec(namespace, action, params=None, task=None, transport=None):
        calls.append(action)
        if action == "load_level":
            return _loaded()
        if action == "get_scene_statistics":
            return _stats({
                "FpsPawn": 1, "FpsTarget": 3, "StaticMeshActor": 1,
                "DirectionalLight": 1, "FpsGrenadePickup": 2,
            })
        return _no_editor()(namespace, action, params)

    state = _with_editor(fake_exec, _fake_auto(True))
    try:
        out = state[2]._implement_grenade({"slots": {"level": "L_NewTest3"}, "save": True})
    finally:
        _restore(*state)
    assert out.get("ok") is True, out
    assert calls == ["load_level", "get_scene_statistics"], calls


def check_kind_mapping_is_declared() -> None:
    """_KIND_BY_ACTION 有 implement_grenade，且指向 automation。"""
    from agent.harness import _KIND_BY_ACTION

    assert _KIND_BY_ACTION.get("implement_grenade") == "automation", _KIND_BY_ACTION


def check_resolve_slots_fills_level_and_filter() -> None:
    """规划路径（20261028-151850 的 W3 卡点）：槽位填了 level 却被通用兜底判成「槽位是空的」。
    resolve_slots 必须有 implement_grenade 分支：解析关卡 + automation_filter，空槽位仍诚实暂停。"""
    from agent.plan_windows import resolve_slots

    out = resolve_slots("implement_grenade", {"level": "level3"})
    assert out.get("ok") is True, out
    assert out.get("automation_filter") == "AIAgent.Fps.Grenade", out
    assert "槽位是空的" not in str(out.get("error") or ""), out
    empty = resolve_slots("implement_grenade", {})
    assert empty.get("ok") is False and "缺少关卡名" in str(empty.get("error") or ""), empty


def check_level_alias_resolves_to_existing() -> None:
    """「level3」这类裸名昵称：不是现有关卡时按尾号唯一匹配到现有关卡（L_NewTest3）。
    CI 上没有工程磁盘（_level_on_disk 为 False）时别名无从谈起，跳过别名断言；
    create_level 不走别名——新建关卡本来就允许是不存在的名字。"""
    from agent.plan_windows import _level_on_disk, resolve_existing_level, resolve_slots

    if _level_on_disk("/Game/Maps/L_NewTest3"):
        found = resolve_existing_level("level3")
        assert found.get("ok") is True and found.get("level_name") == "L_NewTest3", found
        assert found.get("alias_note"), found
    created = resolve_slots("create_level", {"level": "level3"})
    assert created.get("ok") is True and created.get("level_path") == "/Game/Maps/level3", created
    assert not created.get("alias_note"), created


def check_criteria_binding_for_compound() -> None:
    """L1 合同把「关卡内容」写成 editor_assert 条款时，复合动作扇也要能当证据窗口；
    且复合扇的 automation_filter 没有对应条款时要补一行，否则收口裁判没条款可勾。"""
    from agent.harness import build_criteria

    windows = [
        {"id": "W1", "action": "compile_project"},
        {"id": "W2", "action": "launch_editor"},
        {"id": "W3", "action": "implement_grenade",
         "automation_filter": "AIAgent.Fps.Grenade", "level_name": "L_NewTest3"},
    ]
    criteria = [
        {"id": "c-cpp_build", "text": "工程编译通过", "evidence_kind": "cpp_build"},
        {"id": "c-editor-assert", "text": "关卡里有 FpsGrenadePickup（数量为 2）",
         "evidence_kind": "editor_assert"},
    ]
    rows = build_criteria(criteria, windows)
    by_id = {str(item.get("id")): item for item in rows}
    assert by_id["c-editor-assert"].get("windows") == ["W2", "W3"], by_id["c-editor-assert"]
    auto = [item for item in rows if item.get("evidence_kind") == "automation"]
    assert auto and auto[0].get("filter") == "AIAgent.Fps.Grenade", auto
    assert auto[0].get("windows") == ["W3"], auto
    # 复合扇的 automation 判词能勾上补出来的条款
    from agent.harness import apply_judge

    ticked = apply_judge(rows, {"ok": True, "kind": "automation", "window": "W3",
                                "filter": "AIAgent.Fps.Grenade",
                                "tests": ["AIAgent.Fps.Grenade"]})
    ticked_by_id = {str(item.get("id")): item for item in ticked}
    assert ticked_by_id["c-automation#2" if "c-automation#2" in ticked_by_id else
                        next(k for k in ticked_by_id if k.startswith("c-automation"))]["passed"] is True, ticked_by_id
    # editor_assert 条款只认 W3 的副判词；W2 的 synthetic 判词勾不动宽绑定条款
    ticked2 = apply_judge(rows, {"ok": True, "kind": "editor_assert", "window": "W3",
                                 "summary": "关卡内容核对", "errors": []})
    ticked2_by_id = {str(item.get("id")): item for item in ticked2}
    assert ticked2_by_id["c-editor-assert"]["passed"] is True, ticked2_by_id


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    check_missing_level_slot_is_not_a_guess()
    check_missing_fps_prerequisite_guides_shooter()
    check_spawns_pickups_then_automation()
    check_idempotent_when_pickups_already_there()
    check_kind_mapping_is_declared()
    check_resolve_slots_fills_level_and_filter()
    check_level_alias_resolves_to_existing()
    check_criteria_binding_for_compound()
    print("implement_grenade ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
