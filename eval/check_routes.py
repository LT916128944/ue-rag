# eval/check_routes.py
"""离线检查：编译通过后的测试失败回到 fix；角色工具不串。"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.graph import route_after_build, route_after_observe
from agent.roles import plan_role, tool_allowed


def main() -> int:
    assert route_after_build({"build_judge": {"ok": True}}) == "test"
    assert route_after_build({"build_judge": {"ok": False}}) == "observe"
    failed = {
        "is_completed": False,
        "retry_count": 0,
        "max_retries": 3,
        "build_judge": {"ok": True, "kind": "cpp_build"},
        "test_judge": {"ok": False, "kind": "automation", "errors": [{"file": "", "line": 0, "message": "failed=1 tests=Example.Test"}]},
    }
    assert route_after_observe(failed) == "fix"
    passed = dict(failed)
    passed["test_judge"] = {"ok": True, "kind": "skip"}
    assert route_after_observe(passed) == "plan"

    cpp = {"verify": [{"kind": "cpp_build"}], "allow_writes": []}
    assert plan_role(cpp) == "cpp"
    assert tool_allowed("read_source", cpp)
    assert tool_allowed("apply_unified_diff", cpp)
    assert not tool_allowed("set_actor_transform", cpp)
    assert not tool_allowed("find_actors", cpp)

    editor = {"verify": [{"kind": "editor_assert"}], "allow_writes": ["set_actor_transform"]}
    assert plan_role(editor) == "editor"
    assert tool_allowed("set_actor_transform", editor)
    assert tool_allowed("find_actors", editor)
    assert not tool_allowed("apply_unified_diff", editor)

    assert plan_role({}) == "loop"
    assert tool_allowed("apply_unified_diff", {})

    from agent.supervise import supervise
    vague = supervise("编译蓝图")
    assert vague.get("ok") is False
    spoken = (
        "帮我操作UE编辑器场景的Cube到(X=120.000000,Y=400.000000,Z=150.000000)位置，"
        "并且验证一次即可保存退出"
    )
    moved = supervise(spoken)
    assert moved["verify"][0]["kind"] == "editor_assert"
    assert moved["verify"][0]["actor_name"] == "Cube"
    assert moved["verify"][0]["expect"]["location"] == {"x": 120.0, "y": 400.0, "z": 150.0}
    assert moved["save"] is True
    assert moved["allow_writes"] == ["set_actor_transform", "save_assets"]
    spoken_actor = spoken.replace("的Cube", "的Actor Cube")
    assert supervise(spoken_actor)["verify"][0]["actor_name"] == "Cube"
    fixed = supervise("修掉 TestActor 的编译错误")
    assert fixed["verify"][0]["kind"] == "cpp_build"
    assert fixed.get("inject_typo") is True
    assert fixed.get("judge_first") is True
    assert fixed.get("typo_path") == "Private/TestActor.cpp"
    assert not fixed.get("keep_source")
    assert not fixed.get("launch_editor")
    from core.editor_launch import task_touches_cpp
    assert task_touches_cpp(fixed)
    assert not task_touches_cpp({"verify": [{"kind": "editor_assert"}]})
    added = supervise("给 ATestEnemy 加一个打印日志的函数")
    assert added["verify"][0]["kind"] == "cpp_build"
    assert added.get("judge_first") is False
    assert added.get("launch_editor") is True
    assert "可以新增这次要求的函数" in added["query"]
    assert "不要新增函数" not in added["query"]
    component = supervise("给 UTestEnemyPatrolComponent 加一个日志函数")
    assert component["verify"][0]["kind"] == "cpp_build"
    assert component.get("launch_editor") is True
    from agent.supervise import plan_sequence
    long_steps = plan_sequence(
        "给 ATestActor 新增一个是否可被拾取的属性，tag=InteractableActor，"
        "修改完编译后启动UE编辑器并创建蓝图丢到场景中测试是否可以被pawn拾取"
    )
    assert long_steps[0]["title"] == "改 C++ 并编译"
    assert long_steps[0]["verify"][0]["kind"] == "cpp_build"
    tight = plan_sequence(
        "给项目中的ATestActor类，新增一个是否可被拾取的属性，tag=InteractableActor，修改完编译后创建蓝图"
    )
    assert tight[0]["title"] == "改 C++ 并编译"
    assert next(step for step in tight if step.get("title") == "创建蓝图").get("parent") == "/Script/AIAgentProject.TestActor"
    assert long_steps[0].get("launch_editor") is True
    create = next(step for step in long_steps if step.get("title") == "创建蓝图")
    assert create.get("needs") == "path" and create.get("blocked") is True
    assert create.get("asset_name") == "BP_TestActor"
    assert "folder_path" not in create
    place = next(step for step in long_steps if step.get("title") == "放进场景")
    assert place.get("needs") == "blueprint"
    assert any(step.get("action") == "check_pickup" and step.get("actor_name") == "BP_TestActor" for step in plan_sequence("用 BP_TestPawn 测试拾取BP_TestActor"))
    assert not any("Automation" in str(step.get("error") or "") for step in long_steps)
    from agent.sequence import apply_blueprint_path, apply_supplied_path, mark_missing_path
    filled = apply_blueprint_path(create, "/Game/Blueprints")
    assert filled["folder_path"] == "/Game/Blueprints"
    assert filled["ref_path"] == "/Game/Blueprints/BP_TestActor.BP_TestActor"
    named = apply_blueprint_path(create, "/Game/Blueprints/BP_Pickup")
    assert named["asset_name"] == "BP_Pickup"
    assert named["folder_path"] == "/Game/Blueprints"
    assert apply_blueprint_path(create, "Blueprints") is None
    supplied = apply_blueprint_path({"ok": False, "asset_name": "BP_TestActor"}, "/Game/BP_TestActor")
    assert supplied["parent"] == "/Script/AIAgentProject.TestActor"
    assert supplied["ref_path"] == "/Game/BP_TestActor.BP_TestActor"
    missing_bp = mark_missing_path({"ok": False, "error": "写不出唯一的蓝图 refPath，不猜。"})
    assert missing_bp.get("needs") == "path"
    missing_cpp = mark_missing_path({"ok": False, "error": "写不出唯一的 C++ 路径，不猜。"})
    assert missing_cpp.get("needs") == "source"
    assert mark_missing_path({"ok": False, "needs": "pie", "error": "要进 PIE"}).get("needs") == "pie"
    sourced = apply_supplied_path("给 ATestActor 新增一个属性", missing_cpp, "Public/TestActor.h")
    assert sourced is not None and sourced["verify"][0]["kind"] == "cpp_build"
    assert "Public/TestActor.h" in sourced["query"]
    both_steps = plan_sequence("编译 BP_Gun 并给 ATestActor 新增一个属性")
    assert [step["verify"][0]["kind"] for step in both_steps] == ["cpp_build", "blueprint_compile"]
    both = supervise("编译 BP_Gun 并给 ATestActor 新增一个属性")
    assert both.get("ok") is False
    assert "多种目标" in (both.get("error") or "")
    previous = os.environ.get("UE_ALLOW_BLUEPRINT")
    plan_path = None
    try:
        os.environ["UE_ALLOW_BLUEPRINT"] = "false"
        manual = supervise("在 BP_Cube 的事件图里加一个节点")
        assert manual.get("ok") is False
        assert "改蓝图已关闭" in (manual.get("error") or "")
        assert str(manual.get("plan") or "").startswith("Plan/")
        from config import UE_PROJECT_DIR
        plan_path = UE_PROJECT_DIR / manual["plan"]
        text = plan_path.read_text(encoding="utf-8")
        assert "Event BeginPlay" in text
        assert "白色执行引脚" in text
        other = supervise("在材质图里加一个节点")
        assert other.get("ok") is False
        assert other.get("plan") is None
        from core.editor_exec import exec_tool
        blocked = exec_tool(
            "editor_toolset.toolsets.blueprint.BlueprintTools",
            "create_node",
            {},
            task={"allow_writes": ["create_node"]},
        )
        assert blocked.get("error") == "blueprint edit disabled"
        os.environ["UE_ALLOW_BLUEPRINT"] = "true"
        opened = supervise("在 BP_Cube 的事件图里加一个节点")
        assert "改蓝图已关闭" not in (opened.get("error") or "")
        if opened.get("ok") is False:
            assert "不猜" in (opened.get("error") or "")
        else:
            assert opened["verify"][0]["kind"] == "blueprint_compile"
            assert "create_node" in opened["allow_writes"]
            assert opened["max_retries"] == 3
        refused = exec_tool(
            "editor_toolset.toolsets.blueprint.BlueprintTools",
            "connect_pins",
            {},
            task={"allow_writes": []},
        )
        assert refused.get("error") == "write refused"
    finally:
        if plan_path is not None:
            plan_path.unlink(missing_ok=True)
        if previous is None:
            os.environ.pop("UE_ALLOW_BLUEPRINT", None)
        else:
            os.environ["UE_ALLOW_BLUEPRINT"] = previous
    from pipelines.export_cpp_metadata import extract_classes
    sample = """
UCLASS(ClassGroup = (Test), meta = (BlueprintSpawnableComponent))
class AIAGENTPROJECT_API UFirst : public UActorComponent
{
};

UCLASS()
class AIAGENTPROJECT_API
ASecond : public AActor
{
};
"""
    classes = extract_classes(sample)
    assert [item["name"] for item in classes] == ["UFirst", "ASecond"]
    assert classes[0]["parent"] == "UActorComponent"
    assert classes[1]["parent"] == "AActor"

    from pipelines.actor_snapshot import actor_text, with_snapshot
    text = actor_text("L_NewTest", "Cube", "StaticMeshActor", "(1.0, 2.0, 3.0)")
    assert "位置快照" in text and "坐标不是实时位置" in text
    tagged = with_snapshot({
        "source_type": "ue_actor",
        "content": "关卡 L 中的 Actor Cube；类 StaticMeshActor；位置 (1.0, 2.0, 3.0)",
        "metadata": {},
    })
    assert tagged["metadata"]["location_kind"] == "snapshot"
    assert "实时位置" not in tagged["content"].replace("不是实时位置", "")

    from agent.context import visible_messages
    cpp_task = {"verify": [{"kind": "cpp_build"}], "allow_writes": []}
    ctx = [
        {"role": "user", "content": "修编译"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "set_actor_transform", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "1", "content": "moved"},
        {"role": "user", "content": "裁判失败。\n原文: error C1"},
    ]
    shown = json.dumps(visible_messages(ctx, cpp_task), ensure_ascii=False)
    assert "set_actor_transform" not in shown
    assert "修编译" in shown and "裁判失败" in shown

    import importlib.util
    hook_path = UE_PROJECT_DIR / ".cursor" / "hooks" / "refuse_graph_writes.py"
    spec = importlib.util.spec_from_file_location("refuse_graph_writes", hook_path)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    denied = hook.decide(
        'python scripts/call_mcp_tool.py --toolset editor_toolset.toolsets.blueprint.BlueprintTools --tool create_node --args {}',
        False,
    )
    assert denied["permission"] == "deny"
    allowed = hook.decide(
        'python scripts/call_mcp_tool.py --toolset editor_toolset.toolsets.blueprint.BlueprintTools --tool compile_blueprint --args {}',
        False,
    )
    assert allowed["permission"] == "allow"
    material = hook.decide(
        'python scripts/call_mcp_tool.py --toolset editor_toolset.toolsets.material.MaterialTools --tool add_expressions --args {}',
        True,
    )
    assert material["permission"] == "deny"
    assert "UE_ALLOW_MATERIAL" in material["agent_message"]
    opened_mat = hook.decide(
        'python scripts/call_mcp_tool.py --toolset editor_toolset.toolsets.material.MaterialTools --tool add_expressions --args {}',
        False,
        {"material": True},
    )
    assert opened_mat["permission"] == "allow"
    rig = hook.decide(
        'python scripts/call_mcp_tool.py --toolset animation_toolset.toolsets.controlrig.ControlRigTools --tool set_pin_value --args {}',
        False,
        {"anim": True},
    )
    assert rig["permission"] == "allow"
    read_mat = hook.decide(
        'python scripts/call_mcp_tool.py --toolset editor_toolset.toolsets.material.MaterialTools --tool get_expressions --args {}',
        False,
    )
    assert read_mat["permission"] == "allow"
    assert hook.blueprint_allowed(UE_PROJECT_DIR / ".env-missing-for-test") is False
    from core.editor_exec import exec_tool as _exec_tool
    blocked_mat = _exec_tool(
        "editor_toolset.toolsets.material.MaterialTools",
        "add_expressions",
        {},
        task={"allow_writes": ["add_expressions"]},
    )
    assert blocked_mat.get("error") == "UE_ALLOW_MATERIAL 不是 true"

    from eval.report import classify_row
    assert classify_row({"ok": False, "judge_kind": "cpp_build", "error": "error C1"}) == "编译"
    assert classify_row({"ok": False, "node": "precheck", "error": "请打开关卡 L"}) == "关卡预检"
    assert classify_row({"ok": False, "error": "WinError 10061"}) == "MCP 连不上"
    assert classify_row({"ok": False, "error": "write refused"}) == "工具拒绝"
    assert classify_row({"ok": False, "judge_kind": "automation", "error": "failed=1"}) == "测试"
    assert classify_row({"ok": False, "retry_count": 3, "error": "still"}) == "超过重试"
    assert classify_row({"ok": False, "error": "unknown"}) == "其他"
    assert classify_row({"ok": True, "judge_kind": "cpp_build"}) is None

    import tempfile
    from core.history_log import append_history
    from core.task_log import append_task_record
    with tempfile.TemporaryDirectory() as tmp:
        hist = Path(tmp) / "历史解决方案.md"
        hist.write_text("# 历史解决方案\n\n## 已有\n\n现象：旧\n", encoding="utf-8")
        assert append_history([{"message": "已有。细节"}], ["read_source"], hist) == 0
        assert append_history([{"message": "error C9999: missing"}], ["apply_unified_diff"], hist) == 1
        assert "工具 apply_unified_diff" in hist.read_text(encoding="utf-8")
        record = Path(tmp) / "2026-10-13.md"
        assert append_task_record({"id": "sample", "query": "编译通过"}, ["apply_unified_diff"], record)
        assert "任务成功" in record.read_text(encoding="utf-8")
        assert append_task_record({"id": "sample", "query": "编译通过"}, ["apply_unified_diff"], record) is False

    from agent.sequence import judge_pickup, run_sequence
    import inspect
    from agent import sequence as sequence_mod
    assert "set_properties" not in inspect.getsource(sequence_mod._check_pickup)
    assert "set_properties" not in inspect.getsource(sequence_mod._place_pair)
    no_tag = judge_pickup({"bAllowPickup": True, "bCanBePicked": True, "Tags": []}, 40)
    assert no_tag["verdict"] == "不能" and "标签" in no_tag["index_note"]
    far = judge_pickup(
        {"bAllowPickup": True, "bCanBePicked": True, "Tags": ["InteractableActor"]},
        150,
    )
    assert far["verdict"] == "不能" and "距离" in far["index_note"]
    near = judge_pickup(
        {"bAllowPickup": True, "bCanBePicked": True, "Tags": ["InteractableActor"]},
        100,
    )
    assert near["verdict"] == "能"
    unread = judge_pickup({"bAllowPickup": True}, 10)
    assert unread.get("ok") is False and unread.get("verdict") != "不能"
    side = json.loads((Path(__file__).parent / "tasks" / "side" / "pickup-sentence.json").read_text(encoding="utf-8"))
    sentence = plan_sequence(side["query"])
    by_title = {step.get("title"): step for step in sentence}
    assert by_title["编译工程"]["verify"][0]["kind"] == "cpp_build"
    assert by_title["编译工程"].get("freeze_source") is True
    assert by_title["编译工程"].get("judge_first") is True
    assert by_title["编译工程"].get("launch_without_source") is True
    assert by_title["编译工程"].get("inject_typo") is not True
    assert "不要修改源码" in by_title["编译工程"]["query"]
    assert "apply_unified_diff" not in by_title["编译工程"]["query"]
    assert by_title["绑定拾取者"].get("ref_path") == side["pawn_ref"]
    assert by_title["绑定物体"].get("ok") is False
    assert "只给一次" in (by_title["绑定物体"].get("error") or "")
    assert by_title["放进场景"].get("ok") is False
    assert by_title["只读裁决拾取"].get("read_only") is True
    assert by_title["只读裁决拾取"].get("ok") is False
    assert not any(step.get("action") == "create_blueprint" for step in sentence)
    unknown = plan_sequence(side["unknown_query"])
    assert not any(step.get("action") == "create_blueprint" for step in unknown)
    assert any(step.get("ok") is False for step in unknown)
    code, run_path = run_sequence(side["unknown_query"], lambda *_args: "stop")
    assert code == 1
    run_path.unlink(missing_ok=True)
    edited = plan_sequence("编译工程并给 ATestActor 新增一个函数")
    assert edited[0]["title"] == "改 C++ 并编译"
    assert not edited[0].get("freeze_source")

    check_windows()

    from eval.check_upgrade import main as upgrade_main

    assert upgrade_main() == 0

    from eval.run_bench import validate_manifest

    bench_errors = validate_manifest()
    assert not bench_errors, bench_errors

    print("routes ok")
    return 0


def _cleanup_run(path) -> None:
    """多窗口 run 现在同时落 .md / .json / events .jsonl，离线测试三件一起清。"""
    from agent import run_windows as rw

    Path(path).unlink(missing_ok=True)
    Path(path).with_suffix(".json").unlink(missing_ok=True)
    (rw.EVENTS_DIR / f"{Path(path).stem}.jsonl").unlink(missing_ok=True)


def check_windows() -> None:
    """多窗口离线验收。不放进 run_eval.py，不叫真实模型，不写场景。"""
    import tempfile

    from agent.plan_windows import (
        load_routes,
        match_learned,
        plan_windows,
        remember_route,
        routes_path,
    )
    from agent.run_windows import execute_windows
    from agent.sequence import _set_pickable
    from agent.supervise import plan_sequence
    from config import UE_PROJECT_DIR

    mark = "给场景的 BP_TestActor 的 tag 标记为可交互物，使得 BP_TestPawn 可以与它交互"
    second = "给 BP_Sphere 打上可交互标签，让 BP_TestPawn 与它交互"
    good = json.dumps({
        "windows": [
            {
                "id": "W1",
                "title": "把 BP_TestActor 设为可拾取",
                "action": "set_pickable",
                "slots": {"target": "BP_TestActor"},
                "depends_on": [],
                "max_retries": 1,
            },
            {
                "id": "W2",
                "title": "只读裁决能否被 BP_TestPawn 拾取",
                "action": "judge_pickup",
                "slots": {"target": "BP_TestActor", "picker": "BP_TestPawn"},
                "depends_on": ["W1"],
                "max_retries": 0,
            },
        ]
    }, ensure_ascii=False)
    previous = os.environ.get("UE_LEARNED_ROUTES")
    quiet = lambda *_args, **_kwargs: None
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UE_LEARNED_ROUTES"] = str(Path(tmp) / "learned_routes.json")
        try:
            called = {"n": 0}

            def boom(messages):
                called["n"] += 1
                raise AssertionError("快路径不应叫规划者")

            compiled = plan_windows("编译工程后启动 UE 编辑器", complete=boom)
            assert called["n"] == 0
            assert compiled["planner_called"] is False
            assert compiled["windows"][0]["title"] == "编译工程"
            assert compiled["windows"][0]["action"] == "compile_project"

            def market(_messages):
                return json.dumps({
                    "windows": [
                        {"id": "W1", "title": "商店", "action": "open_marketplace", "slots": {}, "depends_on": [], "max_retries": 0},
                        {
                            "id": "W2",
                            "title": "裁决",
                            "action": "judge_pickup",
                            "slots": {"target": "BP_TestActor", "picker": "BP_TestPawn"},
                            "depends_on": ["W1"],
                            "max_retries": 0,
                        },
                    ]
                }, ensure_ascii=False)

            blocked = plan_windows("打开虚幻商城看道具", complete=market)
            assert blocked["planner_called"] is True
            assert blocked["wrote_route"] is False
            assert blocked["windows"][0]["realizable"] is False
            assert "表外" in (blocked["windows"][0].get("error") or "")
            assert not routes_path().is_file()

            seen = {}

            def planner(messages):
                seen["messages"] = messages
                return good

            planned = plan_windows(mark, complete=planner)
            assert planned["planner_called"] is True
            assert planned["wrote_route"] is True
            assert [window["action"] for window in planned["windows"]] == ["set_pickable", "judge_pickup"]
            assert planned["windows"][1]["depends_on"] == ["W1"]
            assert planned["windows"][0]["realizable"] is True
            assert planned["windows"][1]["realizable"] is True
            assert len(seen["messages"]) == 2
            assert seen["messages"][0]["role"] == "system"
            assert seen["messages"][1] == {"role": "user", "content": mark}
            blob = json.dumps(seen["messages"], ensure_ascii=False)
            assert "set_pickable" in blob and "judge_pickup" in blob
            for banned in ("search_ue_project", "input_schema", "toolset", "ue_mcp_index"):
                assert banned not in blob
            raw = routes_path().read_text(encoding="utf-8")
            assert "BP_TestActor" not in raw
            assert "BP_TestPawn" not in raw
            saved = load_routes()
            assert len(saved) == 1
            assert saved[0]["id"] == "mark-interactable"
            assert saved[0]["roles"] == ["target", "picker"]
            assert saved[0]["when"] == ["标签", "可交互"]
            assert saved[0]["windows"][0]["slots"] == {"target": "target"}
            assert saved[0]["windows"][1]["slots"] == {"target": "target", "picker": "picker"}
            assert saved[0]["windows"][1]["depends_on"] == ["W1"]
            assert remember_route(mark, planned["windows"]) is False
            assert len(load_routes()) == 1

            again = plan_windows(mark, complete=boom)
            assert again["planner_called"] is False
            assert again["source"] == "learned"
            assert called["n"] == 0

            steps = plan_sequence(second)
            assert [step["action"] for step in steps] == ["set_pickable", "judge_pickup"]
            assert steps[0]["slots"]["target"] == "BP_Sphere"
            assert steps[1]["slots"]["picker"] == "BP_TestPawn"
            assert steps[0]["realizable"] is True
            learned_second = plan_windows(second, complete=boom)
            assert learned_second["planner_called"] is False
            assert match_learned("让 BP_A 与 BP_B 交互") is None
            assert match_learned("把今天的笔记整理一下") is None

            from agent import run_task as run_task_mod
            original = run_task_mod.run_task
            calls = []

            def fake_ok(task, trace_path=None, thread_id=None, on_row=None):
                calls.append((task.get("action"), thread_id, task.get("target_ref")))
                if task.get("action") in ("judge_pickup", "check_pickup"):
                    return {
                        "id": task.get("id"),
                        "ok": True,
                        "summary": "不能拾取：距离 150.0 uu，超过 100",
                        "judge": "不能",
                        "thread_id": thread_id,
                    }
                return {
                    "id": task.get("id"),
                    "ok": True,
                    "summary": "bPickable 为真，带 InteractableActor",
                    "ref": "ActorRef",
                    "in_level": True,
                    "thread_id": thread_id,
                }

            run_task_mod.run_task = fake_ok
            try:
                code, run_path = execute_windows(mark, complete=boom, echo=quiet)
                assert code == 0
                assert [item[0] for item in calls] == ["set_pickable", "judge_pickup"]
                assert calls[0][1] and calls[0][1] != calls[1][1]
                assert calls[1][2] == "ActorRef"
                text = run_path.read_text(encoding="utf-8")
                assert "依赖：W1" in text
                assert "不能" in text
                assert "集成不调用模型" in text
                _cleanup_run(run_path)

                calls.clear()

                def fake_missing(task, trace_path=None, thread_id=None, on_row=None):
                    calls.append(task.get("action"))
                    return {
                        "id": task.get("id"),
                        "ok": False,
                        "summary": "场景里没有 BP_TestActor",
                        "error": "场景里没有 BP_TestActor",
                        "thread_id": thread_id,
                    }

                run_task_mod.run_task = fake_missing
                code, run_path = execute_windows(mark, complete=boom, echo=quiet)
                assert code == 1
                assert calls == ["set_pickable", "set_pickable"]
                assert "judge_pickup" not in calls
                _cleanup_run(run_path)

                calls.clear()
                run_task_mod.run_task = fake_ok
                code, run_path = execute_windows("打开虚幻商城看道具", complete=market, echo=quiet)
                assert code == 1
                assert calls == []
                _cleanup_run(run_path)
            finally:
                run_task_mod.run_task = original

            import agent.sequence as sequence_mod
            import core.editor_exec as editor_exec
            old_find = sequence_mod._find_named
            old_exec = editor_exec.exec_tool

            def missing_actor(_name, _task):
                return {"ok": True, "missing": True, "refs": []}

            def refuse_write(*_args, **_kwargs):
                raise AssertionError("场景里没有实例时不应写")

            sequence_mod._find_named = missing_actor
            editor_exec.exec_tool = refuse_write
            try:
                missed = _set_pickable({"actor_name": "BP_TestActor", "slots": {"target": "BP_TestActor"}})
                assert missed["ok"] is False
                assert "场景里没有" in missed["error"]
            finally:
                sequence_mod._find_named = old_find
                editor_exec.exec_tool = old_exec

            complex_query = (
                "把场景里的 Cube 移到 (X=180,Y=260,Z=90)，给 BP_TestActor 打上可交互标签，"
                "让 BP_TestPawn 与它交互，再编译 BP_Gun"
            )

            def partial(_messages):
                return json.dumps({
                    "windows": [{
                        "id": "W1",
                        "title": "商店",
                        "action": "open_marketplace",
                        "slots": {},
                        "depends_on": [],
                        "max_retries": 0,
                    }]
                }, ensure_ascii=False)

            mixed = plan_windows(complex_query, complete=partial)
            assert mixed["planner_called"] is True
            assert mixed["source"] == "planner"
            assert mixed["wrote_route"] is False
            concrete = [
                {"id": "W1", "action": "move_actor", "slots": {"actor": "Cube", "location": "180,260,90"}, "depends_on": []},
                {"id": "W2", "action": "set_pickable", "slots": {"target": "BP_TestActor"}, "depends_on": []},
                {"id": "W3", "action": "judge_pickup", "slots": {"target": "BP_TestActor", "picker": "BP_TestPawn"}, "depends_on": ["W2"]},
                {"id": "W4", "action": "compile_blueprint", "slots": {"blueprint": "BP_Gun"}, "depends_on": []},
            ]
            assert remember_route(complex_query, concrete) is False

            wasd = (
                "修改ATestPawn代码，使得它可以通过WASD前后左右移动，"
                "目前测试下来是无法移动的，你帮我修复它并且测试"
            )
            fixed = plan_windows(wasd, complete=partial)
            assert fixed["planner_called"] is False
            assert fixed["source"] == "rules"
            assert [window["action"] for window in fixed["windows"]] == ["edit_cpp", "probe_move"]
            assert all(window["realizable"] for window in fixed["windows"])
            assert fixed["windows"][1]["depends_on"] == ["W1"]
            assert "写不出唯一的 Actor" not in str(fixed["windows"])

            look_query = (
                "检查ATestPawn代码，使得可以满足跟随鼠标旋转镜头的需求，"
                "编译后启动编辑器，并进行PIE测试是否跟随鼠标移动视角了"
            )
            look = plan_windows(look_query, complete=partial)
            assert look["planner_called"] is False
            assert [window["action"] for window in look["windows"]] == ["edit_cpp", "probe_look"]
            assert all(window["realizable"] for window in look["windows"])
            assert look["windows"][1]["depends_on"] == ["W1"]
            assert look["windows"][1]["pie"] is True
            assert "写不出唯一的 Actor" not in str(look["windows"])

            def only_edit(_messages):
                return json.dumps({
                    "windows": [{
                        "id": "W1",
                        "title": "修改 ATestPawn 的移速为十倍",
                        "action": "edit_cpp",
                        "slots": {"class": "ATestPawn"},
                        "depends_on": [],
                        "max_retries": 1,
                    }]
                }, ensure_ascii=False)

            speed = plan_windows("将ATestPawn的移速提升十倍并测试", complete=only_edit)
            assert speed["planner_called"] is True
            assert [window["action"] for window in speed["windows"]] == ["edit_cpp", "probe_play"]
            assert speed["windows"][1]["pie"] is True
            assert speed["windows"][1]["depends_on"] == ["W1"]
            assert speed["windows"][0].get("wait_mcp") is True

            bat = (UE_PROJECT_DIR / "start.bat").read_text(encoding="utf-8")
            assert "run_multi.py" in bat
            assert "run_windows.py" in bat
            assert "run_agent.py" not in bat
            guide = (UE_PROJECT_DIR / "WORKFLOW.md").read_text(encoding="utf-8")
            assert "单循环" in guide
            assert "多窗口" in guide
            assert "learned_routes.json" in guide
            assert "工具原文" in guide
        finally:
            if previous is None:
                os.environ.pop("UE_LEARNED_ROUTES", None)
            else:
                os.environ["UE_LEARNED_ROUTES"] = previous


if __name__ == "__main__":
    raise SystemExit(main())
