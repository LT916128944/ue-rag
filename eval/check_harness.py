# eval/check_harness.py
"""Harness 字段、交接、验收合同、上下文、蒸馏回放、只读动作、属性快照。

不叫真实模型，不写场景，不 PIE，不改蓝图 Graph。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _quiet(*_args, **_kwargs):
    return None


def check_decision_fields() -> None:
    from agent.harness import STOP_REASONS, classify_stop, run_stop
    from agent.plan_windows import plan_windows

    def _no_model(_messages):
        raise AssertionError("决策字段检查不应叫模型")

    planned = plan_windows("编译工程", complete=_no_model)
    assert planned["source"] == "rules", planned.get("source")
    intent = planned.get("intent") or {}
    assert intent.get("skill_id") == ""
    assert intent.get("status") == "rules"
    assert "version" in intent
    kinds = [item.get("evidence_kind") for item in planned.get("criteria") or []]
    assert kinds == ["cpp_build"], kinds
    assert all(item.get("passed") is False for item in planned["criteria"])

    assert classify_stop(ok=True) == "ok"
    assert classify_stop(ok=False, paused=True) == "paused"
    assert classify_stop(ok=False, retries=3, max_retries=3) == "retry_exceeded"
    assert classify_stop(ok=False, summary="build 前打转：最近 12 次") == "read_stall"
    assert classify_stop(ok=False, summary="error C3861") == "judge_failed"
    assert classify_stop(ok=False, unresolved=["半句"]) == "unresolved"
    assert run_stop(["ok", "judge_failed"], False) == "judge_failed"
    assert set(STOP_REASONS) >= {"ok", "paused", "retry_exceeded", "read_stall", "judge_failed", "unresolved"}


def check_contract() -> None:
    from agent.harness import apply_judge, build_criteria, contract_status, ignore_model_claim
    from agent.nodes import observe_node

    compile_rows = build_criteria(
        [{"id": "c-cpp_build", "evidence_kind": "cpp_build", "text": "C++ 编译通过"}],
        [{"action": "compile_project"}],
    )
    assert [item["id"] for item in compile_rows] == ["c-cpp_build"]
    passed = apply_judge(compile_rows, {"ok": True, "kind": "cpp_build", "summary": "exit 0"})
    assert passed[0]["passed"] is True
    still = apply_judge(compile_rows, {"ok": True, "kind": "blueprint_compile", "summary": "空"})
    assert still[0]["passed"] is False
    claimed = ignore_model_claim(compile_rows, "已完成")
    assert claimed[0]["passed"] is False

    # 没有 L1 合同时，才按窗口动作类型兜底
    fallback = build_criteria(windows=[{"action": "compile_project"}])
    assert [item.get("evidence_kind") for item in fallback] == ["cpp_build"]

    # L1 合同：automation 的 filter 自带，或从窗口继承；manual 不再由代码补
    rows = build_criteria(
        [{"id": "c-automation", "evidence_kind": "automation", "text": "巡逻读数"}],
        [{"action": "run_automation_test", "slots": {"filter": "AIAgent.Enemy.Patrol"}}],
    )
    assert rows[0]["filter"] == "AIAgent.Enemy.Patrol" and rows[0]["passed"] is False
    failed = apply_judge(rows, {"ok": False, "kind": "automation", "summary": "failed=1"})
    assert failed[0]["passed"] is False
    assert contract_status(failed, "成功") == "部分完成"
    flipped = apply_judge(rows, {"ok": True, "kind": "automation", "summary": "failed=0"})
    assert contract_status(flipped, "成功") == "成功"
    assert [item for item in flipped if item.get("evidence_kind") == "manual"] == []

    # 裁判只认白名单里的 evidence_kind；同 id 改成 #2，不互相顶掉
    assert build_criteria([{"id": "c-x", "evidence_kind": "拍脑袋", "text": "x"}]) == []
    dup = build_criteria([
        {"id": "c-automation", "evidence_kind": "automation", "filter": "A", "text": "a"},
        {"id": "c-automation", "evidence_kind": "automation", "filter": "B", "text": "b"},
    ])
    assert [item["id"] for item in dup] == ["c-automation", "c-automation#2"]
    assert [item["filter"] for item in dup] == ["A", "B"]

    observed = observe_node({
        "require_build": True,
        "retry_count": 0,
        "build_judge": {"ok": False, "kind": "cpp_build", "summary": "error C1", "errors": []},
        "test_judge": {"ok": True, "kind": "skip", "summary": "skip", "errors": []},
        "messages": [{"role": "assistant", "content": "已完成"}],
        "task": {"criteria": compile_rows},
    })
    assert observed["task"]["criteria"][0]["passed"] is False


def check_context() -> None:
    from agent.context import compact_tool_bodies, slice_counts, visible_messages

    task = {"verify": [{"kind": "cpp_build"}], "allow_writes": []}
    messages = [{"role": "user", "content": "修编译"}]
    messages.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "diff",
            "type": "function",
            "function": {"name": "apply_unified_diff", "arguments": "{}"},
        }],
    })
    messages.append({
        "role": "tool",
        "tool_call_id": "diff",
        "content": json.dumps({"ok": True, "diff": "KEEP_THIS_DIFF"}, ensure_ascii=False),
    })
    messages.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "move",
            "type": "function",
            "function": {"name": "set_actor_transform", "arguments": "{}"},
        }],
    })
    messages.append({"role": "tool", "tool_call_id": "move", "content": "moved-secret"})
    for index in range(8):
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": f"r{index}",
                "type": "function",
                "function": {"name": "read_source", "arguments": "{}"},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": f"r{index}",
            "content": json.dumps({"ok": True, "text": f"line-{index}"}),
        })
    shown = json.dumps(visible_messages(messages, task), ensure_ascii=False)
    assert "KEEP_THIS_DIFF" in shown
    assert "set_actor_transform" not in shown
    assert "moved-secret" not in shown
    counts = slice_counts(messages, task)
    assert set(counts) == {"user", "judge_fail", "tool_kept", "tool_dropped"}
    assert counts["tool_kept"] >= 1 and counts["tool_dropped"] >= 1
    assert "line-0" not in json.dumps(counts)
    compacted = compact_tool_bodies(messages)
    blob = json.dumps(compacted, ensure_ascii=False)
    assert "KEEP_THIS_DIFF" in blob
    assert "可再读" in blob
    assert "sha1=" in blob

    # 20261021-121520：UMG 工具以 safe name 成对返回。两条结果都要留在下一轮提示里。
    widgets = "UMGToolSet.UMGToolSet.GetWidgets".replace(".", "__")
    description = "UMGToolSet.UMGToolSet.GetWidgetDescription".replace(".", "__")
    hud = {
        "verify": [{"kind": "blueprint_compile"}],
        "allow_writes": ["GetWidgets", "GetWidgetDescription", "AddWidget"],
    }
    hud_messages = [
        {"role": "user", "content": "改 WBP_HUD"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "w", "type": "function", "function": {"name": widgets, "arguments": "{}"}},
                {"id": "d", "type": "function", "function": {"name": description, "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "w", "content": json.dumps({"ok": True, "widgets": ["Root"]})},
        {"role": "tool", "tool_call_id": "d", "content": json.dumps({"ok": True, "text": "DESC_BODY"})},
    ]
    hud_messages[1]["reasoning_content"] = "先读控件树"
    hud_shown_msgs = visible_messages(hud_messages, hud)
    hud_shown = json.dumps(hud_shown_msgs, ensure_ascii=False)
    assert "Root" in hud_shown and "DESC_BODY" in hud_shown
    hud_counts = slice_counts(hud_messages, hud)
    assert hud_counts["tool_kept"] == 2 and hud_counts["tool_dropped"] == 0, hud_counts
    # 20261021-122639：deepseek-flash 思考模式要求下一轮带回 reasoning_content，且同一次回复的工具调用不能拆开。
    assistants = [msg for msg in hud_shown_msgs if msg.get("role") == "assistant"]
    assert len(assistants) == 1, assistants
    assert assistants[0].get("reasoning_content") == "先读控件树"
    assert len(assistants[0].get("tool_calls") or []) == 2

    from types import SimpleNamespace

    from core.llm_call import reasoning_content

    assert reasoning_content(SimpleNamespace(reasoning_content="think")) == "think"
    assert reasoning_content(SimpleNamespace(model_extra={"reasoning_content": "extra"})) == "extra"
    assert reasoning_content(SimpleNamespace()) == ""


def check_handoff() -> None:
    from agent.harness import build_handoff, handoff_note, unfinished_titles, write_handoff

    with tempfile.TemporaryDirectory() as tmp_name:
        root = Path(tmp_name)
        records = root / "records"
        assert unfinished_titles(records, "2026-10-19") == []
        payload = build_handoff(
            did="编译工程",
            judges=[{"kind": "cpp_build", "ok": False, "summary": "exit 1"}],
            summary="read_source\n" + ("正文" * 80),
            error="read_source 的整段返回",
            ok=False,
            criteria=[{"id": "c-cpp_build", "text": "编译没过", "evidence_kind": "cpp_build", "passed": False}],
            next_title="下一扇",
            artifacts={"BP_A": {"ref": "Actor.A", "in_level": True, "body": "read_source secret"}},
            record_dir=records,
            day="2026-10-19",
        )
        blob = json.dumps(payload, ensure_ascii=False)
        assert "read_source" not in blob
        assert "编译没过" in blob
        assert payload["evidence"][0]["kind"] == "cpp_build"
        assert payload["artifacts"]["BP_A"] == {"ref": "Actor.A", "in_level": True}
        write_handoff("run-h", "W1", payload, root=root / "handoffs")
        note = handoff_note("run-h", ["W1"], root=root / "handoffs")
        assert "编译没过" in note and "read_source" not in note
        import shutil
        shutil.rmtree(root / "handoffs")
        assert handoff_note("run-h", ["W1"], root=root / "handoffs") == ""


def check_replay() -> None:
    from agent.plan_windows import match_learned
    from agent.skill_memory import compare_actions, load_skills, promote_from_route, replay_skill

    compared = compare_actions("给 BP_Sphere 打上可交互标签，让 BP_TestPawn 与它交互")
    assert compared["executed"] is False and compared["result_replay"] is False
    assert "不是结果回放" in compared["note"]

    route = {
        "id": "replay-shadow",
        "when": ["回放门"],
        "roles": ["target"],
        "windows": [{"action": "set_pickable", "slots": {"target": "target"}}],
    }
    run = {
        "run_id": "replay-1",
        "query": "编译工程",
        "windows": [{"action": "compile_project"}],
        "unresolved": [],
    }
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        (tmp / "runs").mkdir()
        (tmp / "runs" / "replay-1.json").write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
        saved = {
            "UE_DISTILL_ROOT": os.environ.get("UE_DISTILL_ROOT"),
            "UE_LEARNED_SKILLS": os.environ.get("UE_LEARNED_SKILLS"),
            "UE_LEARNED_ROUTES": os.environ.get("UE_LEARNED_ROUTES"),
        }
        os.environ["UE_DISTILL_ROOT"] = str(tmp)
        os.environ["UE_LEARNED_SKILLS"] = str(tmp / "learned_skills.json")
        os.environ["UE_LEARNED_ROUTES"] = str(tmp / "learned_routes.json")
        try:
            report = replay_skill(
                {"action_sequence": ["set_pickable"], "id": "replay-shadow"},
                run,
            )
            assert report["consistent"] is False and report["executed"] is False
            assert promote_from_route(route, source_run="replay-1") == "replay-shadow"
            stored = load_skills()
            assert stored and stored[0]["status"] == "shadow"
            assert match_learned("给 BP_A 打上回放门") is None
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def check_catalog_and_policy() -> None:
    from agent.catalog import CATALOG, READ_ONLY_ACTIONS
    from core.editor_exec import exec_tool, is_write_tool

    from eval._pin_backend import pinned_backend

    assert "query_actor" in CATALOG and "run_automation_test" in CATALOG
    assert "query_actor" in READ_ONLY_ACTIONS and "run_automation_test" in READ_ONLY_ACTIONS
    assert is_write_tool("query_actor") is False
    assert is_write_tool("run_automation_test") is False
    with pinned_backend("official"):
        refused = exec_tool("asset", "save_assets", {}, task={"save": False, "pie": False, "allow_writes": []})
        assert refused.get("ok") is False and refused.get("error") == "write refused"
        assert refused["policy"]["save"] is False and "UE_ALLOW_BLUEPRINT" in refused["policy"]
        graph = exec_tool(
            "editor_toolset.toolsets.blueprint.BlueprintTools",
            "create_node",
            {},
            task={"allow_writes": [], "save": False, "pie": False},
        )
    assert graph.get("error") == "write refused"
    assert "allow_writes" in graph["policy"]


def check_props() -> None:
    import core.editor_exec as editor_exec
    from agent.prop_snapshot import capture_props, props_path, restore_props
    from agent.sequence import _set_pickable

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        saved_snap = os.environ.get("UE_SNAPSHOT_DIR")
        os.environ["UE_SNAPSHOT_DIR"] = str(tmp)
        calls = []
        original = editor_exec.exec_tool

        def fake(toolset, tool, args, task=None, timeout=None):
            calls.append(tool)
            if tool == "find_actors":
                return {"ok": True, "data": [{"refPath": "Actor.BP_A"}]}
            if tool == "get_properties":
                return {"ok": True, "data": {"bPickable": False, "Tags": []}}
            return {"ok": False, "error": "写入失败"}

        editor_exec.exec_tool = fake
        try:
            result = _set_pickable({"actor_name": "BP_A", "run_id": "snap-1", "slots": {"target": "BP_A"}})
            assert result.get("ok") is False
            assert calls.index("get_properties") < calls.index("set_properties")
            stored = json.loads(props_path("snap-1").read_text(encoding="utf-8"))
            assert stored["items"][0]["fields"]["bPickable"] is False
            level = tmp / "L_NewTest.umap"
            level.write_bytes(b"level")
            stamp = level.stat().st_mtime_ns
            seen = []

            def writer(ref, item):
                seen.append((ref, item))

            assert restore_props("snap-1", writer=writer) == ["Actor.BP_A"]
            assert seen[0][1]["fields"]["bPickable"] is False
            assert level.stat().st_mtime_ns == stamp
            capture_props("snap-1", "Actor.BP_A", {"bPickable": True})
            assert restore_props("snap-1", writer=writer) == []
        finally:
            editor_exec.exec_tool = original
            if saved_snap is None:
                os.environ.pop("UE_SNAPSHOT_DIR", None)
            else:
                os.environ["UE_SNAPSHOT_DIR"] = saved_snap


def check_l1_criteria_contract() -> None:
    """L1 给的 criteria 就是合同：编译、控件图、Automation 都过了才算成功，代码不再补 c-manual。"""
    from agent.harness import apply_judge, build_criteria, contract_status
    from agent.sequence import _check_platform_hud
    from agent.umg_manual import missing_platform_hud
    from core import editor_exec

    l1 = [
        {"id": "c-cpp_build", "evidence_kind": "cpp_build", "text": "C++编译通过"},
        {"id": "c-widget_graph", "evidence_kind": "widget_graph", "text": "WBP_HUD 自动连线：Event Tick 写 txt_Timer"},
        {"id": "c-automation", "evidence_kind": "automation", "filter": "AIAgent.Platform", "text": "平台玩法读数正常"},
    ]
    rows = build_criteria(l1)
    assert [item["id"] for item in rows] == ["c-cpp_build", "c-widget_graph", "c-automation"]
    assert rows[2]["filter"] == "AIAgent.Platform"
    assert all(item["passed"] is False for item in rows)

    partial = apply_judge(rows, {"ok": True, "kind": "automation", "summary": "失败数 0"})
    partial = apply_judge(partial, {"ok": True, "kind": "cpp_build", "summary": "exit 0"})
    assert contract_status(partial, "成功") == "部分完成"
    assert all(item["passed"] is False for item in partial if item.get("evidence_kind") == "widget_graph")
    done = apply_judge(partial, {"ok": True, "kind": "widget_graph", "summary": "连线已写入"})
    assert contract_status(done, "成功") == "成功"
    assert [item for item in done if item.get("evidence_kind") == "manual"] == []

    # 裁判不看原句：同一份合同带不带业务词都认
    same = build_criteria(l1, [{"action": "run_automation_test", "slots": {"filter": "AIAgent.Platform"}}])
    assert [item["id"] for item in same] == [item["id"] for item in rows]

    # 换一个完全不同的需求，走同一套证据类型
    racing = build_criteria([
        {"id": "c-cpp_build", "evidence_kind": "cpp_build", "text": "赛车 C++ 编译通过"},
        {"id": "c-widget_graph", "evidence_kind": "widget_graph", "text": "左上角时速表：Event Tick 写 txt_Speed"},
        {"id": "c-automation", "evidence_kind": "automation", "filter": "AIAgent.Racing", "text": "三个氮气开加速门"},
    ])
    assert [item["id"] for item in racing] == ["c-cpp_build", "c-widget_graph", "c-automation"]
    assert racing[2]["filter"] == "AIAgent.Racing"

    payload = {"returnValue": {"widgets": [{"widgetName": "ScoreLabel"}, {"widgetName": "TimeLabel"}]}}
    assert missing_platform_hud(payload) == ["txt_CoinCount", "txt_Timer", "txt_Clear", "txt_Fail"]
    original = editor_exec.exec_tool

    def fake(toolset, tool, args, task=None, timeout=None):
        # check_platform_hud 现在核两件事：四个控件名 + 父类。父类读不到就算不合格。
        assert tool in ("GetWidgets", "get_parent"), tool
        assert task.get("save") is False and task.get("pie") is False
        if tool == "GetWidgets":
            assert args["widgetBlueprint"]["refPath"] == "/Game/UI/WBP_HUD.WBP_HUD"
            return {"ok": True, "data": payload}
        return {"ok": True, "data": {"returnValue": {"refPath": "/Script/AIAgentProject.PlatformHUDWidget"}}}

    editor_exec.exec_tool = fake
    try:
        result = _check_platform_hud({})
    finally:
        editor_exec.exec_tool = original
    assert result.get("ok") is False
    assert "txt_CoinCount" in str(result.get("error") or "")
    assert result.get("save") is False and result.get("pie") is False

    # 控件名齐、父类不对 → 仍不合格（HUD 绑不上，文本永远是默认值）
    full = {"returnValue": {"widgets": [{"widgetName": name} for name in
                                        ("txt_CoinCount", "txt_Timer", "txt_Clear", "txt_Fail")]}}

    def fake_wrong_parent(toolset, tool, args, task=None, timeout=None):
        if tool == "GetWidgets":
            return {"ok": True, "data": full}
        return {"ok": True, "data": {"returnValue": {"refPath": "/Script/UMG.UserWidget"}}}

    editor_exec.exec_tool = fake_wrong_parent
    try:
        wrong = _check_platform_hud({})
    finally:
        editor_exec.exec_tool = original
    assert wrong.get("ok") is False, wrong
    assert "PlatformHUDWidget" in str(wrong.get("error") or "")

    def fake_right_parent(toolset, tool, args, task=None, timeout=None):
        if tool == "GetWidgets":
            return {"ok": True, "data": full}
        return {"ok": True, "data": {"returnValue": {"refPath": "/Script/AIAgentProject.PlatformHUDWidget"}}}

    editor_exec.exec_tool = fake_right_parent
    try:
        good = _check_platform_hud({})
    finally:
        editor_exec.exec_tool = original
    assert good.get("ok") is True, good


def _criteria(count: int) -> list[dict]:
    return [
        {"id": f"c{index}", "text": f"条款{index}", "evidence_kind": "automation", "passed": False}
        for index in range(1, count + 1)
    ]


def check_handoff_totals() -> None:
    from agent.board import Board
    from agent.harness import build_handoff, handoff_note, write_handoff

    nine = build_handoff(did="九条", ok=False, criteria=_criteria(9))
    assert len(nine["unmet"]) == 8
    assert nine["unmet_total"] == 9
    assert nine["unmet_omitted"] == 1
    assert nine["unmet_ids"] == [f"c{index}" for index in range(1, 10)]
    assert "条款9" not in nine["unmet"]
    with tempfile.TemporaryDirectory() as tmp_name:
        root = Path(tmp_name)
        write_handoff("run-n", "W1", nine, root=root)
        note = handoff_note("run-n", ["W1"], root=root)
        assert '"unmet_total": 9' in note
        assert "条款9" not in note
        assert "unmet_ids" not in note
        assert "c9" not in note
    eight = build_handoff(did="八条", ok=False, criteria=_criteria(8))
    assert len(eight["unmet"]) == 8 and eight["unmet_total"] == 8 and eight["unmet_omitted"] == 0
    empty = build_handoff(did="空", ok=True)
    assert empty["unmet"] == [] and empty["unmet_total"] == 0 and empty["unmet_omitted"] == 0
    board = Board(on_log=lambda _record: None)
    board.event({"type": "handoff", "window": "W1", "unmet": nine["unmet"], "unmet_total": 44})
    assert board.windows["W1"]["note"].startswith("未完成 8/44：")
    assert len(board.windows["W1"]["note"]) <= 120


def check_disposition() -> None:
    from agent.brief import brief_system_prompt
    from agent.catalog import system_prompt
    from agent.graph import route_after_observe
    from agent.harness import contract_status, judge_disposition
    from agent.intent import validate_intent
    from agent.nodes import observe_node
    from agent.plan_windows import materialize
    from agent.understand import system_prompt as understand_prompt

    base = {
        "require_build": True,
        "retry_count": 0,
        "messages": [],
        "task": {},
        "max_retries": 3,
    }
    compile_state = {
        **base,
        "build_judge": {"ok": False, "kind": "cpp_build", "summary": "error C", "errors": [{"message": "error C"}]},
        "test_judge": {"ok": True, "kind": "skip", "summary": "skip", "errors": []},
    }
    compile_fail = observe_node(compile_state)
    assert compile_fail["is_completed"] is False
    assert route_after_observe({**compile_state, **compile_fail}) == "fix"

    editor_state = {
        **base,
        "build_judge": {"ok": True, "kind": "cpp_build", "summary": "exit 0", "errors": []},
        "test_judge": {"ok": False, "kind": "editor_assert", "summary": "位置不对", "errors": [{"message": "位置不对"}]},
    }
    editor_fail = observe_node(editor_state)
    assert editor_fail["is_completed"] is False
    assert route_after_observe({**editor_state, **editor_fail}) == "fix"

    auto_state = {
        **base,
        "build_judge": {"ok": True, "kind": "cpp_build", "summary": "exit 0", "errors": []},
        "test_judge": {
            "ok": False,
            "kind": "automation",
            "summary": "failed=1 tests=AIAgent.Pawn.Look",
            "errors": [{"message": "failed=1 tests=AIAgent.Pawn.Look"}],
        },
    }
    auto_fail = observe_node(auto_state)
    assert auto_fail["is_completed"] is True
    assert auto_fail["final_success"] is False
    assert auto_fail["retry_count"] == 1
    assert route_after_observe({**auto_state, **auto_fail}) == "end"

    rows = [{"id": "c-automation", "text": "Automation", "evidence_kind": "automation", "passed": False}]
    covered = judge_disposition(
        ["c-automation"],
        rows,
        [{"id": "c-automation", "label": "out_of_contract", "note": "已完成"}],
    )
    assert covered["ok"] is True
    assert covered["criteria"][0]["passed"] is False
    assert covered["items"][0]["note"] == "已完成"
    assert contract_status(covered["criteria"], "成功") == "部分完成"

    blind = judge_disposition(
        ["c-automation"],
        rows,
        [{"id": "c-automation", "label": "blind_spot", "note": "还要补测试"}],
    )
    assert blind["ok"] is False
    assert blind["criteria"][0]["passed"] is False

    hidden = judge_disposition(
        ["c-automation"],
        rows,
        [{"id": "c-automation", "label": "equivalent", "note": "read_source 整段"}],
    )
    assert hidden["ok"] is True and hidden["items"][0]["note"] == ""
    assert judge_disposition(["c-automation"], rows, [{"label": "equivalent"}])["ok"] is False
    assert judge_disposition(["c-automation"], rows, [
        {"id": "c-automation", "label": "equivalent", "note": "a"},
        {"id": "c-automation", "label": "blind_spot", "note": "b"},
    ])["ok"] is False
    assert judge_disposition(["c-automation"], rows, [{"id": "c-automation", "label": "其他", "note": "x"}])["ok"] is False

    passed_by_judge = judge_disposition(
        ["c-automation"],
        [{"id": "c-automation", "evidence_kind": "automation", "passed": True}],
        [],
    )
    assert passed_by_judge["ok"] is True
    assert passed_by_judge["criteria"][0]["passed"] is True

    criteria_intent = validate_intent({
        "goals": [{"action": "compile_project", "slots": {}}],
        "criteria": [
            {"id": "c-cpp_build", "evidence_kind": "cpp_build", "text": "编译"},
            {"id": "c-bad", "evidence_kind": "拍脑袋", "text": "x"},
            {"id": "c-automation", "evidence_kind": "automation", "text": "平台", "filter": "AIAgent.Platform"},
        ],
    }, "编译工程")
    assert [item["evidence_kind"] for item in criteria_intent["criteria"]] == ["cpp_build", "automation"]
    assert criteria_intent["criteria"][1]["filter"] == "AIAgent.Platform"
    assert any("拍脑袋" in note for note in criteria_intent["dropped"])
    assert "criteria" in understand_prompt()

    refused = materialize(action="dispose_residual", slots={}, title="模型写的", wid="W9", depends=[], retries=0)
    assert refused["realizable"] is False and "不执行" in refused["error"]
    intent = validate_intent({"goals": [{"action": "dispose_residual", "slots": {}}]}, "原句")
    assert intent["goals"] == []
    assert "- dispose_residual" not in system_prompt()
    assert "dispose_residual" not in understand_prompt()
    assert "dispose_residual" not in brief_system_prompt()


def check_clusters() -> None:
    from agent.harness import build_dispose_windows, cluster_residuals
    from agent.run_windows import _dependency_met, excused_dispose_parents

    two = cluster_residuals([
        {"id": "c-look", "message": "failed=1 tests=AIAgent.Pawn.Look"},
        {"id": "c-wasd", "message": "failed=1 tests=AIAgent.Pawn.Wasd"},
    ])
    assert [item["key"] for item in two] == ["AIAgent.Pawn.Look", "AIAgent.Pawn.Wasd"]
    assert [item["ids"] for item in two] == [["c-look"], ["c-wasd"]]
    unnamed = cluster_residuals([{"id": "c-x", "message": "无法解析失败数"}])
    assert unnamed == [{"key": "unnamed", "ids": ["c-x"]}]
    five = cluster_residuals([
        {"id": f"c{index}", "message": f"failed=1 tests=AIAgent.T{index}"}
        for index in range(1, 6)
    ])
    assert len(five) == 4
    assert five[3]["key"] == "overflow"
    assert five[3]["ids"] == ["c4", "c5"]
    assert [item["key"] for item in five[:3]] == ["AIAgent.T1", "AIAgent.T2", "AIAgent.T3"]

    windows = build_dispose_windows("W1", [
        {"id": "c-look", "message": "failed=1 tests=AIAgent.Pawn.Look"},
        {"id": "c-wasd", "message": "failed=1 tests=AIAgent.Pawn.Wasd"},
    ], [
        {"id": "c-look", "evidence_kind": "automation", "passed": False, "text": "look"},
        {"id": "c-wasd", "evidence_kind": "automation", "passed": False, "text": "wasd"},
    ], ["c-look", "c-wasd"])
    assert [item["action"] for item in windows] == ["dispose_residual", "dispose_residual"]
    assert windows[0]["unmet_ids"] == ["c-look"] and windows[1]["unmet_ids"] == ["c-wasd"]
    assert windows[0]["criteria"][0]["passed"] is False
    assert _dependency_met("failed", "dispose_residual") is True
    assert _dependency_met("failed", "edit_cpp") is False
    assert _dependency_met("queued", "dispose_residual") is False
    assert excused_dispose_parents([
        {"id": "W1", "action": "edit_cpp", "state": "failed", "ok": False},
        {"id": "W1-D1", "action": "dispose_residual", "depends_on": ["W1"], "state": "done", "ok": True},
    ]) == {"W1"}
    assert excused_dispose_parents([
        {"id": "W1-D1", "action": "dispose_residual", "depends_on": ["W1"], "state": "failed", "ok": False},
    ]) == set()


def check_kill_side() -> None:
    from agent.harness import assess_kill, judge_disposition
    from eval.report import collect_attributions, render_html

    root = Path(__file__).resolve().parent
    task = json.loads((root / "tasks" / "side" / "kill-check.json").read_text(encoding="utf-8"))
    assert task["id"] == "kill-check"
    assert task.get("inject_typo") is True
    assert not task.get("keep_source")
    assert task["verify"][0]["kind"] == "automation"
    assert task["verify"][0]["filter"] == "AIAgent.Pawn.Look"
    manifest = json.loads((root / "bench_manifest.json").read_text(encoding="utf-8"))
    assert all(item.get("id") != "kill-check" for item in manifest["tasks"])
    assert "kill-check" not in (root / "run_eval.py").read_text(encoding="utf-8")
    killed = assess_kill({"ok": False, "kind": "automation", "summary": "failed=1 tests=AIAgent.Pawn.Look"})
    assert killed["killed"] is True and killed["can_close"] is True
    green = assess_kill({"ok": True, "kind": "automation", "summary": "失败数 0"})
    assert green["label"] == "blind_spot" and green["can_close"] is False
    still = assess_kill({"ok": False, "kind": "automation", "summary": "failed=0 filter=AIAgent.Pawn.Look"})
    assert still["can_close"] is False and still["label"] == "blind_spot"
    rows = [{"id": "c-automation", "evidence_kind": "automation", "passed": False}]
    blocked = judge_disposition(
        ["c-automation"], rows, [{"id": "c-automation", "label": "blind_spot", "note": "失败数 0"}],
    )
    assert blocked["ok"] is False and blocked["criteria"][0]["passed"] is False

    blank = render_html({"routes": {}, "cache": {}, "classes": {}})
    assert "归因" not in blank
    page = render_html({"routes": {}, "cache": {}, "classes": {}}, attributions=[{
        "id": "c-automation",
        "key": "AIAgent.Pawn.Look",
        "label": "equivalent",
        "note": "x" * 300,
    }])
    assert "c-automation" in page and "AIAgent.Pawn.Look" in page and "equivalent" in page
    assert "x" * 240 in page and "x" * 241 not in page
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        assert collect_attributions(trace_dir=tmp, handoff_dir=tmp) == []
        handoff = tmp / "handoffs"
        handoff.mkdir()
        (handoff / "W1.attribution.json").write_text(json.dumps({
            "key": "AIAgent.Pawn.Look",
            "items": [{"id": "c-automation", "label": "out_of_contract", "note": "过滤器不覆盖这一条"}],
        }, ensure_ascii=False), encoding="utf-8")
        found = collect_attributions(handoff_dir=handoff)
        assert found and found[0]["id"] == "c-automation" and found[0]["key"] == "AIAgent.Pawn.Look"
        trace = tmp / "trace.jsonl"
        trace.write_text(json.dumps({
            "attribution": {"key": "overflow", "items": [{"id": "c9", "label": "equivalent", "note": "短"}]},
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        traced = collect_attributions(trace_dir=trace)
        assert traced[0]["key"] == "overflow" and traced[0]["label"] == "equivalent"


def check_under_delivery_guard() -> None:
    """20261021-195220 回归：19 条交付物、3 个零窗口范围、C++ 窗口一行没改，
    旧逻辑仍判「成功 / 矩阵全部通过」。这里把六条病灶各钉一条断言。"""
    import tempfile

    from agent.harness import apply_judges, build_criteria, contract_status, scope_marks
    from agent.run_windows import execute_windows
    from agent import run_task as run_task_mod
    from agent import run_windows as rw

    windows = [
        {"id": "W1", "action": "edit_cpp", "depends_on": []},
        {"id": "W5", "action": "run_automation_test", "depends_on": [], "slots": {"filter": "AIAgent.Platform"}},
        {"id": "W3", "action": "edit_widget_blueprint", "depends_on": []},
    ]
    rows = build_criteria([
        {"id": "c-cpp-build", "evidence_kind": "cpp_build", "text": "C++ 编译通过"},
        {"id": "c-widget-graph", "evidence_kind": "widget_graph", "text": "HUD 计数与倒计时接线"},
        {"id": "c-automation", "evidence_kind": "automation", "filter": "AIAgent.Platform", "text": "平台玩法"},
    ], windows)

    # 1 条款记住能证明它的窗口，别的窗口勾不动
    assert rows[1]["windows"] == ["W3"] and rows[2]["windows"] == ["W5"], rows
    assert apply_judges(rows, {"ok": True, "kind": "widget_graph", "window": "W2"})[1]["passed"] is False
    assert apply_judges(rows, {"ok": True, "kind": "widget_graph", "window": "W3"})[1]["passed"] is True

    # 2 automation 跑了 0 个测试不算证明
    zero = apply_judge_auto(rows, {"ok": True, "kind": "automation", "window": "W5", "tests": []})
    assert zero[2]["passed"] is False
    real = apply_judge_auto(rows, {
        "ok": True, "kind": "automation", "window": "W5", "filter": "AIAgent.Platform",
        "tests": ["AIAgent.Platform.Run"],
    })
    assert real[2]["passed"] is True
    other = apply_judge_auto(rows, {
        "ok": True, "kind": "automation", "window": "W5", "tests": ["AIAgent.Pawn.Look"],
    })
    assert other[2]["passed"] is False, "过滤器对不上的测试不能证明这条"

    # 3 零窗口范围永远不算完成，并且拖低总状态
    scopes = [
        {"id": "A", "window_ids": ["W1"], "criteria": [], "deliverables": ["改代码"]},
        {"id": "B", "window_ids": [], "criteria": [], "deliverables": ["金币"]},
        {"id": "C", "window_ids": ["W3"], "criteria": [], "deliverables": ["HUD"]},
    ]
    marks = {item["id"]: item for item in scope_marks(scopes)}
    assert marks["B"]["ok"] is False and "没有可执行的窗口" in marks["B"]["reason"]
    assert contract_status(rows, "成功", scopes) == "部分完成"

    # 4 窗口没跑完的范围也不算完成
    partial = [dict(scope) for scope in scopes]
    partial[0]["window_states"] = ["failed"]
    assert contract_status([], "成功", partial) == "部分完成"

    # 5 edit_cpp 没写源码不能算完成；6 矩阵不适用项不撑「全过」
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        saved_runs, saved_events, saved_archive = rw.RUNS_DIR, rw.EVENTS_DIR, rw.ARCHIVE_DIR
        rw.RUNS_DIR, rw.EVENTS_DIR, rw.ARCHIVE_DIR = tmp / "runs", tmp / "events", tmp / "archive"
        original = run_task_mod.run_task

        def hollow(task, trace_path=None, thread_id=None, on_row=None):
            # 复刻 W1：编译 exit 0、没有 source_written
            return {
                "id": task.get("id"), "ok": True, "summary": "exit 0", "thread_id": thread_id,
                "build": {"ok": True, "kind": "cpp_build", "summary": "exit 0", "errors": []},
                "test": {"ok": True, "kind": "skip", "summary": "no test verify", "errors": []},
            }

        run_task_mod.run_task = hollow
        try:
            from agent.plan_windows import materialize

            win = {"id": "W1", "title": "改 C++ 并编译", "action": "edit_cpp",
                   "slots": {"class": "ATestPawn"}, "depends_on": [], "max_retries": 0,
                   "realizable": True, "ok": True, "blocked": False, "save": False, "pie": False}
            integrate = materialize("integrate_verify", {}, title="累计集成、验证与交付",
                                    wid="W2", depends=["W1"], retries=1)
            code, run_path = execute_windows("改 C++ 并编译", planned={"source": "rules"},
                                            windows=[win, integrate], echo=lambda *_a, **_k: None)
        finally:
            run_task_mod.run_task = original
            rw.RUNS_DIR, rw.EVENTS_DIR, rw.ARCHIVE_DIR = saved_runs, saved_events, saved_archive
        assert code == 1, "改源码却零改动，不能记成功"
        data = json.loads(run_path.with_suffix(".json").read_text(encoding="utf-8"))
        assert data["windows"]["W1"]["state"] == "failed", data["windows"]["W1"]["state"]
        matrix = data["windows"]["W2"]["detail"]["matrix"]
        assert any(one.get("ok") is None for one in matrix), "本次没发生的项要记不适用"
        assert data["windows"]["W2"]["ok"] is False, "只有不适用项的矩阵不能是「全过」"
        assert data["status"] != "成功"


def apply_judge_auto(rows, judge):
    from agent.harness import apply_judge

    return apply_judge(rows, judge)


def main() -> int:
    check_decision_fields()
    check_contract()
    check_l1_criteria_contract()
    check_context()
    check_handoff()
    check_handoff_totals()
    check_disposition()
    check_clusters()
    check_kill_side()
    check_replay()
    check_catalog_and_policy()
    check_props()
    check_under_delivery_guard()
    print("harness ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
