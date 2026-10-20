# eval/check_distill.py
"""蒸馏闭环离线验收。不叫真实模型，不写场景，不 PIE，不改蓝图 Graph。"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _quiet(*_args, **_kwargs):
    return None


def check_contract() -> None:
    from agent.distill import distill_records, redact, run_distill

    note = redact(r"E:\UEProject\AIWork\AIAgentProject\Source\Test.cpp api_key=abc")
    assert "<path>" in note and "api_key=<redacted>" in note and "abc" not in note
    rows = distill_records([
        {"type": "intent", "actions": ["edit_cpp"], "unresolved": [], "_run": "r1"},
        {"type": "intent", "actions": ["edit_cpp"], "unresolved": [], "_run": "r1"},
        {"type": "confirm", "path": "/Game/BP_TestActor", "_run": "r1"},
        {"type": "window_result", "window": "W1", "ok": False,
         "note": r"E:\UEProject\Secret\Test.cpp(1): error C3861", "_run": "r1"},
        {"type": "learned_route", "route_id": "mark-interactable", "_run": "r1"},
    ], [{"run_id": "r2", "windows": [{"action": "compile_project"}], "unresolved": ["半句"]}])
    assert len(rows["intent_examples"]) == 1
    assert rows["clarification_examples"][0]["missing_slot"] == "asset_refPath"
    assert "<path>" in rows["failure_patterns"][0]["note"]
    assert "C3861" in rows["failure_patterns"][0]["symptom"]
    assert rows["failure_patterns"][0]["status"] == "shadow"
    assert rows["skill_candidates"][-1]["status"] == "shadow"
    assert all(item.get("executed") is not True for bucket in rows.values() for item in bucket)

    with tempfile.TemporaryDirectory() as tmp_name:
        root = Path(tmp_name)
        report = run_distill(dry_run=True, root=root)
        assert report["dry_run"] is True and report["wrote"] is False
        assert report["executed"] is False and report["model"] is False
        assert not (root / "distill").exists()
        written = run_distill(dry_run=False, root=root, out_dir=root / "distill")
        assert written["wrote"] is True and written["executed"] is False
        assert (root / "distill" / "candidates.jsonl").is_file()


def check_skills() -> None:
    from agent.plan_windows import match_learned, remember_intent_route
    from agent.skill_memory import (
        drop_skill,
        load_skills,
        promote_from_route,
        remember_shadow,
        validate_skill,
        validate_store,
    )

    assert validate_store() == []
    route = {
        "id": "skill-demo",
        "when": ["可交互"],
        "roles": ["target", "picker"],
        "windows": [
            {"action": "set_pickable", "slots": {"target": "target"}},
            {"action": "judge_pickup", "slots": {"target": "target", "picker": "picker"}, "depends_on": ["W1"]},
        ],
    }
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        saved_routes = os.environ.get("UE_LEARNED_ROUTES")
        saved_skills = os.environ.get("UE_LEARNED_SKILLS")
        os.environ["UE_LEARNED_ROUTES"] = str(tmp / "learned_routes.json")
        os.environ["UE_LEARNED_SKILLS"] = str(tmp / "learned_skills.json")
        try:
            assert remember_shadow(route) == "skill-demo"
            assert all(item.get("status") == "shadow" for item in load_skills())
            assert match_learned("给 BP_A 打上可交互，让 BP_B 与它交互") is None
            assert promote_from_route(route, source_run="run-1") == "skill-demo"
            hit = match_learned("给 BP_A 打上可交互，让 BP_B 与它交互")
            assert hit is not None
            assert [step.get("action") for step in hit] == ["set_pickable", "judge_pickup"]
            assert remember_intent_route(
                "给 BP_A 打上可交互，让 BP_B 与它交互",
                {"unresolved": ["还有半句"], "goals": []},
                [],
            ) is None
            before = len(load_skills())
            assert drop_skill("skill-demo") is True
            assert len(load_skills()) == before - 1
            broken = {"id": "x"}
            assert validate_skill(broken)
        finally:
            if saved_routes is None:
                os.environ.pop("UE_LEARNED_ROUTES", None)
            else:
                os.environ["UE_LEARNED_ROUTES"] = saved_routes
            if saved_skills is None:
                os.environ.pop("UE_LEARNED_SKILLS", None)
            else:
                os.environ["UE_LEARNED_SKILLS"] = saved_skills


def check_failure() -> None:
    from agent.failure_memory import add_shadow, lookup_pattern, observe_success
    from agent.nodes import fix_node

    hit = lookup_pattern("Test.cpp(16,2): error C3861: “ThisFunctionDoesNotExist”: 找不到标识符")
    assert hit and hit["id"] == "err-c3861-undeclared" and int(hit["successes"]) >= 2
    assert lookup_pattern("error C2065: 别的错") is None
    assert lookup_pattern("error C3861: 另一句对不上的说明") is None

    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "failure_patterns.json"
        saved = os.environ.get("UE_FAILURE_PATTERNS")
        os.environ["UE_FAILURE_PATTERNS"] = str(path)
        try:
            assert add_shadow({
                "id": "err-demo",
                "symptom": "error C9999",
                "symptom_detail": "演示",
                "error_kind": "compile",
                "root_cause": "演示用，不是历史",
                "fix_tools": ["read_source"],
                "validation": "退出码 0",
                "confidence": 0.2,
                "source_runs": [],
                "successes": 0,
                "status": "shadow",
                "conflicts": [],
            }) == "err-demo"
            assert lookup_pattern("error C9999 演示") is None
            observe_success("err-demo", source_run="once")
            assert lookup_pattern("error C9999 演示") is None
            observe_success("err-demo", source_run="twice")
            assert lookup_pattern("error C9999 演示")["status"] == "promoted"
        finally:
            if saved is None:
                os.environ.pop("UE_FAILURE_PATTERNS", None)
            else:
                os.environ["UE_FAILURE_PATTERNS"] = saved

    state = {
        "build_judge": {"ok": False, "errors": [{"message": "error C3861: 找不到标识符"}]},
        "test_judge": {},
        "failure_log": [],
    }
    out = fix_node(state)
    text = out["messages"][0]["content"]
    assert "err-c3861-undeclared" in text
    other = fix_node({
        "build_judge": {"ok": False, "errors": [{"message": "error C2065: 未声明的标识符"}]},
        "test_judge": {},
        "failure_log": [],
    })
    assert "err-c3861-undeclared" not in other["messages"][0]["content"]


def check_clarify() -> None:
    from agent.clarify_policy import apply_note, question_for, remember_reply
    from agent.grounding import _pause

    assert "只给一次" in question_for("path")
    assert "Public/" in question_for("source")
    paused = _pause("path", "索引里没有 BP_Nobody，也推不出父类，不猜。只给一次：r /Game/某目录/BP_名字。")
    assert paused["plan"] == "pause_r" and "只给一次" in paused["error"]
    assert paused["error"].count("只给一次") == 1

    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "clarify_policy.json"
        saved = os.environ.get("UE_CLARIFY_POLICY")
        os.environ["UE_CLARIFY_POLICY"] = str(path)
        try:
            path.write_text(json.dumps({"policies": []}, ensure_ascii=False), encoding="utf-8")
            remembered = remember_reply("path", "/Game/Blueprints", source_run="20261014-121440")
            assert remembered["missing_slot"] == "asset_refPath"
            assert "/Game/Blueprints" in remembered["accepted_forms"]
            assert "只给一次" in apply_note("blueprint", "推不出。")
        finally:
            if saved is None:
                os.environ.pop("UE_CLARIFY_POLICY", None)
            else:
                os.environ["UE_CLARIFY_POLICY"] = saved


def check_index() -> None:
    import agent.index_refresh as index_mod
    from agent.grounding import _linked
    from agent.index_refresh import index_is_fresh, schedule_index_refresh

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        ran = []
        saved_auto = os.environ.get("UE_INDEX_AUTO_RUN")
        saved_status = index_mod.status_path
        try:
            queued = schedule_index_refresh(editor_open=False, queue_path=tmp / "index_queue.jsonl")
            assert queued["mode"] == "queued" and queued["auto_run"] is False
            hint = schedule_index_refresh(editor_open=True, queue_path=tmp / "index_queue.jsonl", runner=lambda: ran.append(1))
            assert hint["mode"] == "hint" and ran == []
            os.environ["UE_INDEX_AUTO_RUN"] = "1"
            auto = schedule_index_refresh(editor_open=False, queue_path=tmp / "index_queue.jsonl", runner=lambda: ran.append(1))
            assert auto["auto_run"] is True and ran == [1]
            lines = (tmp / "index_queue.jsonl").read_text(encoding="utf-8").splitlines()
            assert len(lines) == 3
            status = tmp / "index_status.json"
            index_mod.mark_stale("过期", path=status)
            index_mod.status_path = lambda: status
            linked = _linked({"ref_path": "/Game/BP_Gun.BP_Gun"})
            assert linked["plan"] == "linked" and linked["index_fresh"] is False
            assert "不改猜" in linked["index_note"]
            assert index_is_fresh() is False
        finally:
            index_mod.status_path = saved_status
            if saved_auto is None:
                os.environ.pop("UE_INDEX_AUTO_RUN", None)
            else:
                os.environ["UE_INDEX_AUTO_RUN"] = saved_auto


def check_catalog() -> None:
    from agent.catalog import CATALOG, action_meta, allows, load_candidates, note_candidate, promote_candidate
    from agent.plan_windows import plan_windows

    for name in ("compile_project", "set_pickable", "judge_pickup"):
        meta = action_meta(name)
        assert meta["version"] >= 1 and isinstance(meta["conflicts"], list) and meta["verification"]

    with tempfile.TemporaryDirectory() as tmp_name:
        saved = os.environ.get("UE_LEARNED_ROUTES")
        os.environ["UE_LEARNED_ROUTES"] = str(Path(tmp_name) / "learned_routes.json")
        try:
            def market(_messages):
                return json.dumps({
                    "windows": [{
                        "id": "W1", "title": "商店", "action": "open_marketplace",
                        "slots": {}, "depends_on": [], "max_retries": 0,
                    }]
                }, ensure_ascii=False)

            blocked = plan_windows("打开虚幻商城看道具", complete=market)
            assert blocked["windows"][0]["realizable"] is False
            assert "表外" in (blocked["windows"][0].get("error") or "")
            assert "open_marketplace" not in CATALOG
            assert allows("open_marketplace") is False
            names = [item.get("name") for item in load_candidates()]
            assert "open_marketplace" in names
            assert all(item.get("status") == "review" for item in load_candidates() if item.get("name") == "open_marketplace")
            assert promote_candidate("open_marketplace", reviewed=False, verification=["有验证"]) is None
            assert allows("open_marketplace") is False
            record = promote_candidate(
                "open_marketplace", reviewed=True, verification=["人审后的只读核对"], conflicts=[],
            )
            assert record["version"] >= 1 and record["verification"] and record["conflicts"] == []
            assert allows("open_marketplace") is True
            assert "open_marketplace" not in CATALOG
            assert note_candidate("compile_project") is None
        finally:
            if saved is None:
                os.environ.pop("UE_LEARNED_ROUTES", None)
            else:
                os.environ["UE_LEARNED_ROUTES"] = saved


def check_menu2() -> None:
    import agent.brief as brief_mod
    from agent.plan_windows import steps_for_menu2
    from agent.supervise import plan_sequence

    saved = brief_mod._retrieval_titles
    brief_mod._retrieval_titles = lambda _query, k=3: []
    try:
        def boom(_messages):
            raise AssertionError("强命中不应叫模型")

        strong = steps_for_menu2("编译工程后启动 UE 编辑器", complete=boom)
        assert strong[0].get("title") == "编译工程" or strong[0].get("verify")

        calls = {"n": 0}

        def nojson(_messages):
            calls["n"] += 1
            return "不是 JSON"

        sentence = (
            "给 ATestActor 新增一个是否可被拾取的属性，tag=InteractableActor，"
            "修改完编译后启动UE编辑器并创建蓝图丢到场景中测试是否可以被pawn拾取"
        )
        offline = plan_sequence(sentence)
        assert offline[0]["title"] == "改 C++ 并编译"
        layered = steps_for_menu2(sentence, complete=nojson)
        assert calls["n"] >= 1
        assert layered[0]["title"] == "改 C++ 并编译"
        assert any(step.get("blocked") or step.get("ok") is False for step in layered)

        def understood(messages):
            calls["understood"] = messages
            return json.dumps({
                "query": "原句",
                "goals": [{"action": "move_actor", "slots": {"actor": "Cube", "location": "1,2,3"}}],
                "constraints": {},
                "unresolved": [],
            }, ensure_ascii=False)

        moved = steps_for_menu2("把今天的笔记整理一下", complete=understood)
        blob = json.dumps(calls["understood"], ensure_ascii=False)
        assert "action 只能取动作表的名字" in blob
        assert moved and moved[0].get("action") == "move_actor"
    finally:
        brief_mod._retrieval_titles = saved


def check_brief_unfinished() -> None:
    from agent import ledger as ledger_mod
    from agent.brief import render_brief_md, unfinished_text

    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "ledger.json"
        path.write_text(json.dumps({
            "scopes": [
                {"id": "A", "title": "编译", "status": "done", "pending": []},
                {"id": "B", "title": "敌人", "status": "pending", "pending": ["接线"]},
            ]
        }, ensure_ascii=False), encoding="utf-8")
        text = unfinished_text(path, unresolved=["动画由人绑"])
        assert "A 完成" in text and "B 未完成" in text and "动画由人绑" in text
        saved = ledger_mod.LEDGER_PATH
        saved_routes = os.environ.get("UE_LEARNED_ROUTES")
        os.environ["UE_LEARNED_ROUTES"] = str(Path(tmp_name) / "learned_routes.json")
        ledger_mod.LEDGER_PATH = path
        try:
            md = render_brief_md({
                "query": "巡逻",
                "scopes": [{
                    "id": "A", "title": "编译", "deliverables": ["编译通过"],
                    "estimate_hours": [1, 2],
                }],
                "integration": {},
                "external_dependencies": [],
                "total_estimate": {"min_hours": 1, "max_hours": 2},
                "unresolved": ["动画由人绑"],
            })
        finally:
            ledger_mod.LEDGER_PATH = saved
            if saved_routes is None:
                os.environ.pop("UE_LEARNED_ROUTES", None)
            else:
                os.environ["UE_LEARNED_ROUTES"] = saved_routes
        assert "尚未完成清单" in md and "A 完成" in md and "B 未完成" in md


def check_patrol() -> None:
    from agent.patrol_manual import patrol_manual_text, write_patrol_plan

    text = patrol_manual_text()
    for token in ("OnPatrol", "OnChase", "OnMeleeAttack", "人绑", "不改图", "不 PIE"):
        assert token in text, token
    with tempfile.TemporaryDirectory() as tmp_name:
        path = write_patrol_plan(Path(tmp_name))
        assert path.is_file()
        assert "行为树" in path.read_text(encoding="utf-8")


def check_board_and_ab() -> None:
    from agent.board import Board
    from agent.skill_memory import compare_actions

    board = Board()
    assert board.expanded is False and board.distill_count == 0
    board.event({"type": "distill_candidate", "status": "shadow"})
    assert board.distill_count == 1 and board.expanded is False

    compared = compare_actions("给 BP_Sphere 打上可交互标签，让 BP_TestPawn 与它交互")
    assert compared["executed"] is False
    assert compared["skill_actions"] == compared["route_actions"]
    assert compared["skill_actions"][:2] == ["set_pickable", "judge_pickup"]


def main() -> int:
    check_contract()
    check_skills()
    check_failure()
    check_clarify()
    check_index()
    check_catalog()
    check_menu2()
    check_brief_unfinished()
    check_patrol()
    check_board_and_ab()
    print("distill ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
