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
    from agent.harness import apply_judge, build_criteria, contract_status, ensure_patrol_window, ignore_model_claim
    from agent.nodes import observe_node
    from agent.plan_windows import plan_windows

    compile_rows = build_criteria("编译工程", [{"action": "compile_project"}])
    passed = apply_judge(compile_rows, {"ok": True, "kind": "cpp_build", "summary": "exit 0"})
    assert passed[0]["passed"] is True
    still = apply_judge(compile_rows, {"ok": True, "kind": "blueprint_compile", "summary": "空"})
    assert still[0]["passed"] is False
    claimed = ignore_model_claim(compile_rows, "已完成")
    assert claimed[0]["passed"] is False

    def _no_model(_messages):
        return ""

    sentence = "生成会巡逻、发现玩家后追击、近距离攻击的敌人"
    patrol = plan_windows(sentence, complete=_no_model)
    auto = [item for item in patrol.get("criteria") or [] if item.get("evidence_kind") == "automation"]
    assert auto and auto[0].get("filter") == "AIAgent.Enemy.Patrol"
    assert auto[0]["passed"] is False
    failed = apply_judge(patrol["criteria"], {"ok": False, "kind": "automation", "summary": "failed=1"})
    assert all(item["passed"] is False for item in failed if item["evidence_kind"] == "automation")
    # 拆句期望不改。执行时才追加只读 Automation，不另开 PIE。
    executed = ensure_patrol_window(sentence, patrol["windows"])
    window = next(item for item in executed if item.get("action") == "run_automation_test")
    assert window.get("pie") is False and window.get("save") is False
    manual = [item for item in patrol["criteria"] if item.get("evidence_kind") == "manual"]
    assert manual and manual[0]["passed"] is False
    flipped = apply_judge(patrol["criteria"], {"ok": True, "kind": "automation", "summary": "failed=0"})
    assert contract_status(flipped, "成功") == "部分完成"

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

    assert "query_actor" in CATALOG and "run_automation_test" in CATALOG
    assert "query_actor" in READ_ONLY_ACTIONS and "run_automation_test" in READ_ONLY_ACTIONS
    assert is_write_tool("query_actor") is False
    assert is_write_tool("run_automation_test") is False
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


def main() -> int:
    check_decision_fields()
    check_contract()
    check_context()
    check_handoff()
    check_replay()
    check_catalog_and_policy()
    check_props()
    print("harness ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
