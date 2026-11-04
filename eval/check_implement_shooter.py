# eval/check_implement_shooter.py
"""表外需求走分解器，不连模型、不连编辑器。

已知短语不再硬绑 implement_shooter。模型若点名已有脚本或 apply_spec，脚本接住。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from eval._pin_offline import close_now

close_now()  # 离线封闭：本机 .env 的求解/放行开关不进断言（20261028 check_upgrade 漂红教训）

CLAUSE = "在 L_NewTest3 内实现简易 FPS 射击玩法，内容自由发挥"


def _fake_split(_messages):
    user = _messages[1]["content"]
    assert len(user) <= 3500, len(user)
    assert "input_schema" not in user
    assert "Public/FpsPawn.h" not in user
    return json.dumps({
        "steps": [
            {"script": "compile_project", "slots": {}},
            {"script": "no_such_script", "slots": {}},
            {
                "script": "apply_spec",
                "spec": {"files": [{
                    "path": "Public/HelloFps.h",
                    "content": "#pragma once\n// fps\n",
                }]},
            },
            {
                "script": "apply_spec",
                "spec": {"files": [{"path": "../Secrets.h", "content": "x"}]},
            },
        ],
    }, ensure_ascii=False)


def check_packet_is_small() -> None:
    from agent.decompose import messages_for, packet

    body = packet(CLAUSE, ["AFpsPawn", "ATestPawn", "第三人称模板"])
    encoded = json.dumps(body, ensure_ascii=False)
    assert len(encoded) <= 3500, len(encoded)
    assert "执行器" in body and body["检索标题"] == ["AFpsPawn", "ATestPawn", "第三人称模板"]
    text = messages_for(CLAUSE, [])[1]["content"]
    assert "implement_shooter" in text or "compile_project" in text


def check_unknown_clause_is_not_hard_bound() -> None:
    from agent import lexicon
    from agent.plan_windows import scope_windows

    assert lexicon.action_for(CLAUSE) is None
    assert scope_windows({"id": "D", "title": CLAUSE}, "") == []


def check_missing_actor_name_is_not_a_path_pause() -> None:
    """原句没点名 Actor：query_actor 缺 name。丢掉，不要变成暂停窗去问 /Game 路径。"""
    from agent.decompose import windows_for_clause

    def fake(_messages):
        return json.dumps({
            "steps": [
                {"script": "query_actor", "slots": {}},
                {"script": "check_platform_hud", "slots": {}},
            ],
        }, ensure_ascii=False)

    made = windows_for_clause("看看射击游戏是否有问题", fake, [])
    actions = [item.get("action") for item in made]
    assert actions == ["check_platform_hud"], actions
    assert all(item.get("realizable") is True for item in made), made
    assert all("缺少槽位" not in str(item.get("error") or "") for item in made), made


def check_acked_gap_is_not_rescued_by_model() -> None:
    """人审已确认缺口不再叫模型。代码能唯一归地时收成只读 Automation，不冒出要填路径的扇。

    台账与源码用临时夹具：真实台账里「测试射击玩法」和「实现简易 FPS」两条范围都含
    「射击」，按 diagnose 的唯一性设计本来就该维持缺口，不能拿易变的生产台账当夹具。
    """
    from agent.brief import materialize_scopes
    from eval.check_diagnose import HIT, _World, _scope

    called = {"n": 0}

    def fake(_messages):
        called["n"] += 1
        return json.dumps({
            "steps": [
                {"script": "query_actor", "slots": {}},
                {"script": "check_platform_hud", "slots": {}},
            ],
        }, ensure_ascii=False)

    brief = {
        "query": "编译代码，打开编辑器，打开level3关卡，看看射击游戏是否有问题",
        "scopes": [{
            "id": "C",
            "title": "检查射击游戏是否有问题（只读）",
            "deliverables": ["给出一份只读检查结论"],
            "windows": [],
            "depends_on": [],
        }],
        "gap_ack": {"acknowledged": True, "scopes": ["C"], "answer": "确认"},
    }
    with _World((HIT,), [_scope("A", "在 L_NewTest3 中实现简易 FPS 射击玩法")]):
        windows, scopes = materialize_scopes(brief, complete=fake)
        actions = [item.get("action") for item in windows]
        assert called["n"] == 0, called
        assert "query_actor" not in actions, actions
        assert "check_platform_hud" not in actions, actions
        assert "implement_shooter" not in actions, actions
        auto = [item for item in windows if item.get("action") == "run_automation_test"]
        assert len(auto) == 1, actions
        assert auto[0].get("automation_filter") == HIT, auto
        assert auto[0].get("realizable") is True, auto
        scope_c = next(item for item in scopes if item.get("id") == "C")
        assert not scope_c.get("capability_gap"), scope_c
        assert scope_c.get("window_ids"), scope_c


def check_fake_model_is_caught() -> None:
    from agent.plan_windows import scope_windows

    made = scope_windows(
        {"id": "D", "title": CLAUSE},
        "",
        complete=_fake_split,
        titles=[],
    )
    actions = [item.get("action") for item in made]
    assert actions == ["compile_project", "apply_spec"], actions
    assert made[1].get("realizable") is True, made[1]
    files = (made[1].get("spec") or {}).get("files") or []
    assert files and files[0]["path"] == "Public/HelloFps.h", files
    assert made[1].get("depends_on") == [made[0]["id"]], made


def check_apply_spec_writes_only_safe_paths() -> None:
    import config
    from agent.sequence import _apply_spec

    original = config.UE_SOURCE_ROOT
    try:
        root = ROOT / "data" / "tmp_apply_spec"
        if root.exists():
            for path in root.rglob("*"):
                if path.is_file():
                    path.unlink()
        config.UE_SOURCE_ROOT = root
        bad = _apply_spec({"spec": {"files": [{"path": "../Secrets.h", "content": "x"}]}})
        assert bad.get("ok") is False, bad
        assert not (root / "Secrets.h").exists()
        good = _apply_spec({"spec": {"files": [{
            "path": "Public/HelloFps.h",
            "content": "#pragma once\n",
        }]}})
        assert good.get("ok") is True, good
        assert (root / "Public" / "HelloFps.h").is_file()
    finally:
        config.UE_SOURCE_ROOT = original
        root = ROOT / "data" / "tmp_apply_spec"
        if root.exists():
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()


def _with_editor(exec_fn, auto_fn):
    import backends.monolith as monolith
    import backends.select as select
    from agent import sequence

    original_exec = monolith.exec
    original_backend = select.current_backend
    original_auto = sequence._run_automation
    monolith.exec = exec_fn
    select.current_backend = lambda: "monolith"
    sequence._run_automation = auto_fn
    return monolith, select, sequence, original_exec, original_backend, original_auto


def _restore(monolith, select, sequence, original_exec, original_backend, original_auto) -> None:
    monolith.exec = original_exec
    select.current_backend = original_backend
    sequence._run_automation = original_auto


def _loaded(path="/Game/Maps/L_NewTest3"):
    return {"ok": True, "data": {"loaded": True, "current_world": path, "path": path}}


def _stats(counts):
    return {"ok": True, "data": {"actor_count": counts}}


def _saved():
    return {"ok": True, "data": {"ok": True, "saved": 1, "requested": 1}}


def check_empty_level_is_not_a_pass() -> None:
    """临时世界测试会过，也不能把空关卡写成完成。"""
    calls = []
    automation = {"called": False}

    def fake_exec(namespace, action, params=None, task=None, transport=None):
        calls.append(action)
        if action == "load_level":
            return _loaded()
        if action == "get_scene_statistics":
            return _stats({"WorldSettings": 1, "Brush": 1})
        if action == "spawn_actor":
            return {"ok": True, "data": {"actor_name": "FpsFloor"}}
        if action == "author_map_settings":
            return {"ok": True, "data": {"actor_instances": [{
                "class": "/Script/AIAgentProject.FpsPawn",
                "spawned": False,
                "error": "could not resolve actor class",
            }]}}
        if action == "save_packages":
            return _saved()
        return {"ok": False, "error": f"unexpected {action}"}

    def fake_auto(_step):
        automation["called"] = True
        return {"ok": True, "summary": "失败数 0（1 个测试）", "test": {"ok": True, "kind": "automation", "tests": ["AIAgent.Fps.Hit"]}}

    state = _with_editor(fake_exec, fake_auto)
    try:
        out = state[2]._implement_shooter({"slots": {"level": "L_NewTest3"}, "save": True})
    finally:
        _restore(*state)
    assert out.get("ok") is False, out
    assert automation["called"] is False, calls
    assert out.get("test", {}).get("ok") is False, out
    assert "临时世界" in str(out.get("error") or ""), out
    assert "失败数 0" not in str(out.get("summary") or ""), out


def check_level_contents_then_test() -> None:
    """关卡里已经有人和靶，才允许用 AIAgent.Fps.Hit 收口。"""
    calls = []

    def fake_exec(namespace, action, params=None, task=None, transport=None):
        calls.append(action)
        if action == "load_level":
            return _loaded()
        if action == "get_scene_statistics":
            return _stats({
                "FpsPawn": 1, "FpsTarget": 3, "StaticMeshActor": 1,
                "DirectionalLight": 1, "PlayerStart": 1, "WorldSettings": 1,
            })
        if action == "author_map_settings":
            override = (params or {}).get("game_mode_override")
            assert override == "/Script/AIAgentProject.FpsGameMode", params
            return {"ok": True, "data": {
                "game_mode_override_set": True,
                "game_mode_override": override,
                "saved": True,
            }}
        return {"ok": False, "error": f"不该调用 {action}"}

    def fake_auto(_step):
        calls.append("automation")
        return {
            "ok": True,
            "summary": "失败数 0（1 个测试） tests=AIAgent.Fps.Hit",
            "test": {"ok": True, "kind": "automation", "tests": ["AIAgent.Fps.Hit"], "summary": "失败数 0"},
            "judge_kind": "automation",
        }

    state = _with_editor(fake_exec, fake_auto)
    try:
        out = state[2]._implement_shooter({"slots": {"level": "L_NewTest3"}, "save": True})
    finally:
        _restore(*state)
    assert calls == ["load_level", "get_scene_statistics", "author_map_settings", "automation"], calls
    assert out.get("ok") is True, out
    assert out.get("in_level") is True, out
    assert "FpsPawn=1" in str(out.get("summary") or ""), out
    assert "FpsGameMode" in str(out.get("summary") or ""), out
    assert out.get("judge_kind") == "automation", out


def check_unset_gamemode_is_not_a_pass() -> None:
    """关卡里有人和靶，但 GameMode 没指到 FpsGameMode，不能当成能玩。"""
    automation = {"called": False}

    def fake_exec(namespace, action, params=None, task=None, transport=None):
        if action == "load_level":
            return _loaded()
        if action == "get_scene_statistics":
            return _stats({
                "FpsPawn": 1, "FpsTarget": 1, "StaticMeshActor": 1,
                "DirectionalLight": 1, "PlayerStart": 1,
            })
        if action == "author_map_settings":
            return {"ok": True, "data": {
                "game_mode_override_set": False,
                "game_mode_override_error": "could not resolve",
            }}
        return {"ok": False, "error": f"不该调用 {action}"}

    def fake_auto(_step):
        automation["called"] = True
        return {"ok": True, "summary": "失败数 0", "test": {"ok": True, "kind": "automation", "tests": ["AIAgent.Fps.Hit"]}}

    state = _with_editor(fake_exec, fake_auto)
    try:
        out = state[2]._implement_shooter({"slots": {"level": "L_NewTest3"}, "save": True})
    finally:
        _restore(*state)
    assert out.get("ok") is False, out
    assert automation["called"] is False
    assert "FpsGameMode" in str(out.get("error") or ""), out


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    check_packet_is_small()
    check_unknown_clause_is_not_hard_bound()
    check_missing_actor_name_is_not_a_path_pause()
    check_acked_gap_is_not_rescued_by_model()
    check_fake_model_is_caught()
    check_apply_spec_writes_only_safe_paths()
    check_empty_level_is_not_a_pass()
    check_level_contents_then_test()
    check_unset_gamemode_is_not_a_pass()
    print("decompose ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
