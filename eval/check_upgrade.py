# eval/check_upgrade.py
"""升级离线验收（对应 Plan/20261014-1633 第 8 节）：
阶段一 事件流与机读 run 状态；阶段二 面板渲染；阶段三 任务书与人审门；
阶段四 续接、快照还原、环/悬空诊断；阶段五 只读并行与工件；阶段六 矩阵、台账、归档。
不叫真实模型，不写场景，不进 PIE。"""
import io
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eval._pin_offline import close_now

close_now()  # 离线封闭：本机 .env 的求解/放行开关不进断言（20261028 check_upgrade 漂红教训）


def _quiet(*_args, **_kwargs):
    return None


def check_gate() -> None:
    """闸门单测：两个读并行持有；写在读退出前拿不到。"""
    from agent import run_windows as rw

    gate = rw._EditorGate()
    order: list[str] = []
    gate.enter("judge_pickup")
    gate.enter("check_pickup")

    def writer() -> None:
        order.append("writer-wait")
        gate.enter("compile_project")
        order.append("writer-in")
        gate.exit("compile_project")

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    time.sleep(0.2)
    assert order == ["writer-wait"], order
    gate.exit("judge_pickup")
    gate.exit("check_pickup")
    thread.join(timeout=2)
    assert not thread.is_alive(), "写锁没有被释放"
    assert order[-1] == "writer-in", order


def check_events_and_state(runs: Path, events_dir: Path) -> None:
    """阶段一：重跑 mark 句，.md 同构，jsonl 覆盖每扇起止与集成结论，run json 机读。"""
    from agent import run_task as run_task_mod
    from agent import run_windows as rw

    mark = "给场景的 BP_TestActor 的 tag 标记为可交互物，使得 BP_TestPawn 可以与它交互"
    good = json.dumps({
        "windows": [
            {"id": "W1", "title": "把 BP_TestActor 设为可拾取", "action": "set_pickable",
             "slots": {"target": "BP_TestActor"}, "depends_on": [], "max_retries": 1},
            {"id": "W2", "title": "只读裁决能否被 BP_TestPawn 拾取", "action": "judge_pickup",
             "slots": {"target": "BP_TestActor", "picker": "BP_TestPawn"}, "depends_on": ["W1"], "max_retries": 0},
        ]
    }, ensure_ascii=False)
    planner = lambda _messages: good

    calls: list[tuple] = []

    def fake_ok(task, trace_path=None, thread_id=None, on_row=None):
        calls.append((task.get("action"), thread_id, task.get("target_ref")))
        if task.get("action") in ("judge_pickup", "check_pickup"):
            if on_row:
                on_row({"node": "direct", "tool": task.get("action"), "ok": True, "elapsed": 0})
            return {"id": task.get("id"), "ok": True, "summary": "不能拾取：距离 150.0 uu，超过 100",
                    "judge": "不能", "thread_id": thread_id}
        if on_row:
            on_row({"node": "direct", "tool": task.get("action"), "ok": True, "elapsed": 0})
        return {"id": task.get("id"), "ok": True, "summary": "bPickable 为真，带 InteractableActor",
                "ref": "ActorRef", "in_level": True, "thread_id": thread_id}

    original = run_task_mod.run_task
    run_task_mod.run_task = fake_ok
    try:
        code, run_path = rw.execute_windows(mark, complete=planner, echo=_quiet)
    finally:
        run_task_mod.run_task = original
    assert code == 0, code
    text = run_path.read_text(encoding="utf-8")
    assert "依赖：W1" in text
    assert "不能" in text
    assert "集成不调用模型" in text
    assert "状态：成功" in text
    assert "来源：规划者" in text

    run_id = run_path.stem
    events_path = events_dir / f"{run_id}.jsonl"
    assert events_path.is_file()
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    types = [event["type"] for event in events]
    for needed in ("run_start", "window_state", "tool_call", "window_result", "integration", "run_end"):
        assert needed in types, (needed, types)
    states = {(event.get("window"), event.get("state")) for event in events if event["type"] == "window_state"}
    for wid in ("W1", "W2"):
        assert (wid, "running") in states, (wid, states)
        assert (wid, "done") in states or any(w == wid and s == "done" for w, s in states), states
    assert any(event["type"] == "files_modified" for event in events), "场景写扇应发 files_modified"
    integration_event = next(event for event in events if event["type"] == "integration")
    assert integration_event.get("ok") is True

    run_json_path = run_path.with_suffix(".json")
    assert run_json_path.is_file()
    data = json.loads(run_json_path.read_text(encoding="utf-8"))
    assert data["run_id"] == run_id and data["query"] == mark
    assert [w["state"] for w in data["windows"].values()] == ["done", "done"]
    assert data["artifacts"]["by_name"]["BP_TestActor"]["ref"] == "ActorRef"
    assert data["integration"]["ok"] is True
    assert calls[1][2] == "ActorRef", calls


def check_resume(runs: Path, events_dir: Path) -> None:
    """阶段四：W1 done 复用结果；W2 running 用原 thread_id 续跑；W3 queued 跑一次。"""
    from agent import run_task as run_task_mod
    from agent import run_windows as rw

    window_w1 = {
        "id": "W1", "title": "把 BP_TestActor 设为可拾取", "action": "set_pickable",
        "slots": {"target": "BP_TestActor"}, "depends_on": [], "max_retries": 0,
        "realizable": True, "ok": True, "blocked": False, "save": False, "pie": False,
        "actor_name": "BP_TestActor", "asset_name": "BP_TestActor",
        "asset_path": "/Game/BP_TestActor", "folder_path": "/Game", "ref_path": "/Game/BP_TestActor.BP_TestActor",
    }
    window_w2 = {
        "id": "W2", "title": "只读裁决能否被 BP_TestPawn 拾取", "action": "judge_pickup",
        "slots": {"target": "BP_TestActor", "picker": "BP_TestPawn"}, "depends_on": ["W1"], "max_retries": 0,
        "realizable": True, "ok": True, "blocked": False, "save": False, "pie": False,
        "actor_name": "BP_TestActor", "picker_name": "BP_TestPawn",
    }
    window_w3 = {
        "id": "W3", "title": "移动测试", "action": "probe_move", "slots": {}, "depends_on": ["W1"], "max_retries": 1,
        "realizable": True, "ok": True, "blocked": False, "save": False, "pie": False,
        "task": {"id": "W3", "action": "probe_move", "title": "移动测试", "query": "测试移动",
                 "verify": [{"kind": "automation", "filter": "AIAgent.Pawn.Wasd"}],
                 "max_retries": 1, "save": False, "pie": False},
    }
    state = {
        "run_id": "20261014-170000",
        "query": "把 BP_TestActor 设为可拾取再测试",
        "source": "rules",
        "status": "interrupted",
        "started_at": "2026-10-14T17:00:00",
        "review": {}, "brief": None, "scopes": [],
        "artifacts": {"by_name": {"BP_TestActor": {"ref": "OldRef", "in_level": True}},
                      "target": {"ref": "OldRef", "in_level": True}},
        "windows": {
            "W1": {"id": "W1", "title": window_w1["title"], "action": "set_pickable", "depends_on": [],
                   "state": "done", "ok": True,
                   "result": {"id": "W1", "ok": True, "summary": "已可拾取"},
                   "detail": {"summary": "已可拾取"}, "thread_id": "task_old_1",
                   "elapsed": 1.0, "retries": 0, "summary": "已可拾取", "snapshot": None,
                   "realizable": True, "error": "", "external_dep": "", "window": dict(window_w1)},
            "W2": {"id": "W2", "title": window_w2["title"], "action": "judge_pickup", "depends_on": ["W1"],
                   "state": "running", "ok": None, "result": None, "detail": None, "thread_id": "task_old_2",
                   "elapsed": None, "retries": 0, "summary": "", "snapshot": None,
                   "realizable": True, "error": "", "external_dep": "", "window": dict(window_w2)},
            "W3": {"id": "W3", "title": window_w3["title"], "action": "probe_move", "depends_on": ["W1"],
                   "state": "queued", "ok": None, "result": None, "detail": None, "thread_id": None,
                   "elapsed": None, "retries": 0, "summary": "", "snapshot": None,
                   "realizable": True, "error": "", "external_dep": "", "window": dict(window_w3)},
        },
        "integration": {},
    }
    calls: list[dict] = []

    def fake(task, trace_path=None, thread_id=None, on_row=None):
        calls.append({"action": task.get("action"), "thread": thread_id, "target_ref": task.get("target_ref")})
        return {"id": task.get("id"), "ok": True, "summary": "复跑成功", "judge": "能",
                "ref": "NewRef", "in_level": True, "thread_id": thread_id}

    original = run_task_mod.run_task
    run_task_mod.run_task = fake
    try:
        code, run_path = rw.execute_windows(state["query"], resume_state=state, echo=_quiet)
    finally:
        run_task_mod.run_task = original
    assert code == 0, code
    actions = [call["action"] for call in calls]
    assert actions == ["judge_pickup", "probe_move"], actions      # W1 done 不重跑
    assert calls[0]["thread"] == "task_old_2", calls               # 断点续跑沿用原 thread_id
    assert calls[0]["target_ref"] == "OldRef", calls               # 工件从 run 状态恢复
    text = run_path.read_text(encoding="utf-8")
    assert "已可拾取" in text                                        # done 扇结果被复用
    assert text.count("状态：成功") == 4  # 三扇 + 集成行
    data = json.loads(run_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert [w["state"] for w in data["windows"].values()] == ["done", "done", "done"]
    assert data["status"] == "成功"


def check_cycle_dangling(runs: Path) -> None:
    """阶段四：环依赖报「环：W2→W3→W2」；悬空依赖报「未知窗口 id」，不拖到集成段。"""
    from agent import run_windows as rw
    from agent.plan_windows import materialize

    w1 = materialize("judge_pickup", {"target": "BP_TestActor", "picker": "BP_TestPawn"},
                     title="悬空裁决", wid="W1", depends=["W9"], retries=0)
    w2 = materialize("set_pickable", {"target": "BP_TestActor"}, title="设A", wid="W2", depends=[], retries=1)
    code, run_path = rw.execute_windows("悬空句", planned={"source": "planner"}, windows=[w1, w2], echo=_quiet)
    assert code == 1
    text = run_path.read_text(encoding="utf-8")
    assert "未知窗口 id W9" in text
    assert "## W1" in text and "状态：暂停" in text
    assert "## W2" in text and "W2" in text.split("## 集成")[0]

    a = materialize("set_pickable", {"target": "BP_TestActor"}, title="环A", wid="W2", depends=["W3"], retries=1)
    b = materialize("judge_pickup", {"target": "BP_TestActor", "picker": "BP_TestPawn"},
                     title="环B", wid="W3", depends=["W2"], retries=0)
    code, run_path = rw.execute_windows("环句", planned={"source": "planner"}, windows=[a, b], echo=_quiet)
    assert code == 1
    text = run_path.read_text(encoding="utf-8")
    assert "环：W2→W3→W2" in text, text


def check_snapshot_restore(src: Path, snaps: Path) -> None:
    """阶段四：会写源码的窗口先拍快照；失败且无 keep_source 时字节还原；keep_source 保留。"""
    from agent import run_task as run_task_mod
    from agent import run_windows as rw
    from agent.plan_windows import materialize

    target = src / "Private" / "TestPawn.cpp"
    original_bytes = target.read_bytes()
    window = materialize("edit_cpp", {"class": "ATestPawn"}, title="修改 ATestPawn 的移速", wid="W1", depends=[], retries=1)
    assert window["realizable"] is True, window.get("error")

    def breaking(task, trace_path=None, thread_id=None, on_row=None):
        # 只有 edit_cpp 才写坏源码。stitch 在有失败扇时会叫一次真模型并可能补出别的扇
        # （如 compile_project），那些扇不该替这扇重写文件。
        if str(task.get("action") or "") == "edit_cpp":
            target.write_bytes(b"broken by window\n")
        return {"id": task.get("id"), "ok": False, "summary": "error C9999: 编译失败", "thread_id": thread_id}

    import core.llm_call as llm_mod

    original_llm = llm_mod.complete_logged
    llm_mod.complete_logged = lambda *args, **kwargs: ("", {"error": "离线检查不调模型"})

    original_run = run_task_mod.run_task
    run_task_mod.run_task = breaking
    try:
        code, run_path = rw.execute_windows("修改 ATestPawn 的移速并编译", planned={"source": "rules"},
                                            windows=[dict(window)], echo=_quiet)
    finally:
        run_task_mod.run_task = original_run
        llm_mod.complete_logged = original_llm
    assert code == 1
    assert target.read_bytes() == original_bytes, "失败编辑窗口应还原源码字节"

    kept = dict(window)
    kept["task"] = {
        "id": "W1", "action": "edit_cpp", "title": "修改并保留", "query": "修改并保留",
        "verify": [{"kind": "cpp_build"}], "keep_source": True, "max_retries": 0,
        "save": False, "pie": False,
    }
    run_task_mod.run_task = breaking
    llm_mod.complete_logged = lambda *args, **kwargs: ("", {"error": "离线检查不调模型"})
    try:
        code, _run_path = rw.execute_windows("修改 ATestPawn 的移速并编译", planned={"source": "rules"},
                                             windows=[kept], echo=_quiet)
    finally:
        run_task_mod.run_task = original_run
        llm_mod.complete_logged = original_llm
    assert code == 1
    assert target.read_bytes() == b"broken by window\n", "keep_source 不应还原"
    target.write_bytes(original_bytes)
    assert (snaps / "20261014-170000").exists() is False  # 快照目录挂在 run_id 下，与 run 记录同名即可


def check_parallel() -> None:
    """阶段五：两只读窗重叠执行；编译窗独占闸门，与只读窗不重叠。"""
    from agent import run_task as run_task_mod
    from agent import run_windows as rw
    from agent.plan_windows import materialize

    w1 = materialize("judge_pickup", {"target": "BP_TestActor", "picker": "BP_TestPawn"},
                     title="只读裁决 A", wid="W1", depends=[], retries=0)
    w2 = materialize("judge_pickup", {"target": "BP_Sphere", "picker": "BP_TestPawn"},
                     title="只读裁决 B", wid="W2", depends=[], retries=0)
    w3 = materialize("compile_project", {}, title="编译工程", wid="W3", depends=[], retries=0)
    assert w1["realizable"] and w2["realizable"] and w3["realizable"]
    spans: dict[str, tuple[float, float]] = {}

    def fake(task, trace_path=None, thread_id=None, on_row=None):
        start = time.perf_counter()
        key = str(task.get("id") or "")
        if key in ("W1", "W2"):
            time.sleep(0.35)
        spans[key] = (start, time.perf_counter())
        return {"id": task.get("id"), "ok": True, "summary": "能拾取", "judge": "能", "thread_id": thread_id}

    original = run_task_mod.run_task
    run_task_mod.run_task = fake
    try:
        code, _run_path = rw.execute_windows(
            "测试 A 且测试 B，再编译工程",
            planned={"source": "planner"}, windows=[w1, w2, w3], echo=_quiet,
        )
    finally:
        run_task_mod.run_task = original
    assert code == 0, code
    assert set(spans) == {"W1", "W2", "W3"}, spans
    a_start, a_end = spans["W1"]
    b_start, b_end = spans["W2"]
    c_start, c_end = spans["W3"]
    assert a_start < b_end and b_start < a_end, "两只读窗应重叠执行"
    assert c_start >= max(a_end, b_end) - 1e-6 or c_end <= min(a_start, b_start) + 1e-6, "编译窗不应与只读窗重叠"


def check_artifacts() -> None:
    """阶段五：工件按名字传递，多物任务各取各的 ref。"""
    from agent import run_task as run_task_mod
    from agent import run_windows as rw
    from agent.plan_windows import materialize

    w1 = materialize("set_pickable", {"target": "BP_TestActor"}, title="设 A", wid="W1", depends=[], retries=0)
    w2 = materialize("set_pickable", {"target": "BP_Sphere"}, title="设 B", wid="W2", depends=[], retries=0)
    w3 = materialize("judge_pickup", {"target": "BP_TestActor", "picker": "BP_TestPawn"},
                      title="裁决 A", wid="W3", depends=["W1"], retries=0)
    w4 = materialize("judge_pickup", {"target": "BP_Sphere", "picker": "BP_TestPawn"},
                      title="裁决 B", wid="W4", depends=["W2"], retries=0)
    got: dict[str, str] = {}

    def fake(task, trace_path=None, thread_id=None, on_row=None):
        action = str(task.get("action") or "")
        if action == "set_pickable":
            return {"id": task.get("id"), "ok": True, "summary": "ok",
                    "ref": f"ref-{task.get('actor_name')}", "in_level": True, "thread_id": thread_id}
        got[str(task.get("actor_name"))] = str(task.get("target_ref"))
        return {"id": task.get("id"), "ok": True, "summary": "能拾取", "judge": "能", "thread_id": thread_id}

    original = run_task_mod.run_task
    run_task_mod.run_task = fake
    try:
        code, _run_path = rw.execute_windows(
            "把 A 和 B 都设为可拾取再分别裁决",
            planned={"source": "planner"}, windows=[w1, w2, w3, w4], echo=_quiet,
        )
    finally:
        run_task_mod.run_task = original
    assert code == 0, code
    assert got == {"BP_TestActor": "ref-BP_TestActor", "BP_Sphere": "ref-BP_Sphere"}, got


def check_brief(briefs: Path, ledger_path: Path) -> None:
    """阶段三：needs_brief、生成边界、schema 验收、人审门记轮次、范围物化。"""
    from agent import brief as brief_mod
    from agent import ledger as ledger_mod
    from agent.brief import generate_brief, materialize_scopes, needs_brief, validate_brief
    from agent.catalog import INTEGRATE_ACTION

    assert needs_brief("移动 Cube 到 x=100", [{"id": "W1"}]) is False
    assert needs_brief("给项目做个新功能", []) is True
    assert needs_brief("普通句子", [{"id": f"W{i}"} for i in range(1, 5)]) is True

    raw = {
        "query": "给项目做新功能：血量系统",
        "bogus_top": 1,
        "scopes": [
            {"id": "A", "title": "局外客户端", "deliverables": ["登录、选角", "钱包与背包"],
             "pending_note": "代码已有，缺接线", "estimate_hours": [16, 40], "depends_on": [],
             "external_dep": "", "unknown_scope_field": True,
             "windows": [
                 {"id": "W1", "title": "把 BP_TestActor 设为可拾取", "action": "set_pickable",
                  "slots": {"target": "BP_TestActor"}, "depends_on": [], "max_retries": 1, "junk": 2}]},
            {"id": "B", "title": "外部等待", "deliverables": ["等 M-GO 接口"], "depends_on": ["A"],
             "external_dep": "M-GO",
             "windows": [
                 {"id": "W2", "title": "裁决", "action": "judge_pickup",
                  "slots": {"target": "BP_TestActor", "picker": "BP_TestPawn"}, "depends_on": ["W1"], "max_retries": 0}]},
            {"id": "C", "title": "空范围", "deliverables": [], "windows": []},
        ],
        "integration": {"title": "累计集成、验证与交付", "deliverables": ["对修改做一次只读复验"], "bogus": 3},
        "external_dependencies": [{"name": "M-GO", "provides": "正式契约", "status": "服务未恢复", "bogus": 4}],
    }
    seen: dict[str, object] = {}

    def fake_complete(messages):
        seen["messages"] = messages
        return json.dumps(raw, ensure_ascii=False)

    brief = generate_brief("给项目做新功能：血量系统", windows=[{"title": "候选", "action": "set_pickable", "slots": {}}],
                           complete=fake_complete)
    assert brief is not None
    blob = json.dumps(seen["messages"], ensure_ascii=False)
    for banned in ("input_schema", "toolset", "search_ue_project", "ue_mcp_index"):
        assert banned not in blob, banned
    assert len(blob) < 6000, "生成器请求体应保持在受控摘要量级"
    # schema 验收：未知字段丢弃并注明
    dropped_text = "；".join(brief["dropped"])
    assert "unknown_scope_field" in dropped_text and "bogus" in dropped_text, brief["dropped"]
    assert brief["scopes"][0]["windows"][0].get("junk") is None
    # 空交付范围暂停
    assert brief["scopes"][2].get("blocked") is True
    # 估时：LLM 给的保留，没给的按动作表算
    assert brief["scopes"][0]["estimate_hours"] == [16.0, 40.0]
    assert brief["scopes"][1]["estimate_hours"][0] <= 2.1
    assert brief["disclaimer"] == ["任务之间有重叠", "估算是粗的，不构成承诺", "不含等待外部依赖的时间"]
    assert brief["total_estimate"]["min_hours"] >= 16.0

    # 人审门：一条意见重出第二版，记审阅 2，附修改
    def complete_rounds(messages):
        seen["count"] = seen.get("count", 0) + 1
        user = messages[1]["content"]
        if "上一轮审阅意见" in user:
            assert "把 A 范围补一条断线重连" in user
            fresh = json.loads(json.dumps(raw, ensure_ascii=False))
            fresh["scopes"][0]["deliverables"].append("断线重连")
            return json.dumps(fresh, ensure_ascii=False)
        return json.dumps(raw, ensure_ascii=False)

    answers = iter(["把 A 范围补一条断线重连", "确认"])
    from agent.brief import brief_flow
    outcome = brief_flow("给项目做新功能：血量系统", windows=[], complete=complete_rounds,
                         reader=lambda _prompt: next(answers), printer=_quiet, directory=briefs)
    assert outcome is not None
    status, brief2, md, js = outcome
    assert status == "confirm"
    assert brief2["review"]["rounds"] == 2
    assert brief2["review"]["status"] == "通过，附修改"
    assert "断线重连" in brief2["scopes"][0]["deliverables"]
    assert md.is_file() and js.is_file()
    md_text = md.read_text(encoding="utf-8")
    for token in ("任务范围", "尚未完成的全部主要内容", "估时区间", "总工作量", "免责", "断线重连", "审阅：2"):
        assert token in md_text, token
    assert "外部依赖" in md_text and "M-GO" in md_text

    # 范围物化：等外部范围标 waiting_external；末尾追加 integrate_verify
    windows, scopes_meta = materialize_scopes(brief2)
    actions = [w["action"] for w in windows]
    assert actions == ["set_pickable", "judge_pickup", INTEGRATE_ACTION], actions
    assert windows[1].get("waiting_external") is True and windows[1]["external_dep"] == "M-GO"
    integrate = windows[-1]
    assert integrate["depends_on"] == ["W1", "W2"]
    assert scopes_meta[0]["window_ids"] == ["W1"]
    assert scopes_meta[-1].get("kind") == "integration"
    # 台账为空时的摘要
    text = ledger_mod.ledger_summary_text(path=ledger_path)
    assert "台账为空" in text


def check_matrix_ledger(briefs: Path, ledger_path: Path, arch: Path, events_dir: Path) -> None:
    """阶段六：B 失败不阻塞 A；报告标出范围格局；矩阵、哈希、归档、台账落盘。"""
    from agent import brief as brief_mod
    from agent import ledger as ledger_mod
    from agent import run_task as run_task_mod
    from agent import run_windows as rw
    from agent.brief import materialize_scopes

    raw = {
        "query": "范围 A 设标签，范围 B 裁决",
        "scopes": [
            {"id": "A", "title": "范围A", "deliverables": ["把物体设为可拾取"], "depends_on": [],
             "windows": [{"id": "W1", "title": "把 BP_TestActor 设为可拾取", "action": "set_pickable",
                          "slots": {"target": "BP_TestActor"}, "depends_on": [], "max_retries": 1}]},
            {"id": "B", "title": "范围B", "deliverables": ["只读裁决能否拾取"], "depends_on": [],
             "windows": [{"id": "W2", "title": "裁决", "action": "judge_pickup",
                          "slots": {"target": "BP_TestActor", "picker": "BP_TestPawn"}, "depends_on": [], "max_retries": 1}]},
        ],
        "integration": {"title": "累计集成、验证与交付", "deliverables": ["状态矩阵", "哈希与归档"]},
        "external_dependencies": [],
    }
    brief = brief_mod.validate_brief(raw, raw["query"])

    def fake(task, trace_path=None, thread_id=None, on_row=None):
        action = str(task.get("action") or "")
        if action == "set_pickable":
            return {"id": task.get("id"), "ok": True, "summary": "bPickable 为真",
                    "test": {"ok": True, "kind": "editor_assert", "summary": "bPickable 为真", "errors": []},
                    "judge_kind": "editor_assert",
                    "ref": "ref-BP_TestActor", "in_level": True, "thread_id": thread_id}
        return {"id": task.get("id"), "ok": False, "summary": "场景里没有 BP_TestActor，失败",
                "test": {"ok": False, "kind": "editor_assert", "summary": "场景里没有 BP_TestActor",
                         "errors": [{"file": "", "line": 0, "message": "场景里没有 BP_TestActor"}]},
                "judge_kind": "editor_assert",
                "error": "场景里没有 BP_TestActor", "thread_id": thread_id}

    original = run_task_mod.run_task
    run_task_mod.run_task = fake
    try:
        windows, scopes_meta = materialize_scopes(brief)
        code, run_path = rw.execute_windows(
            raw["query"], planned={"source": "brief"}, windows=windows, brief=brief, scopes=scopes_meta,
            echo=_quiet,
        )
    finally:
        run_task_mod.run_task = original
    assert code == 1, "B 失败，整句不满 ok"
    text = run_path.read_text(encoding="utf-8")
    assert "范围：A 完成；B 失败" in text, text
    assert "- 状态：部分完成" in text
    assert "矩阵 重复请求幂等" in text and "矩阵 断线重连" in text
    assert "汇总：elapsed=" in text

    run_id = run_path.stem
    folder = arch / run_id
    assert folder.is_dir()
    for name in (f"{run_id}.json", f"{run_id}.jsonl", f"{run_id}.md", "deliverables.json"):
        assert (folder / name).is_file(), name

    ledger = ledger_mod.read_ledger(path=ledger_path)
    by_id = {scope["id"]: scope for scope in ledger["scopes"]}
    assert by_id["A"]["status"] == "done"
    assert by_id["B"]["status"] == "pending"
    assert "只读裁决能否拾取" in by_id["B"]["pending"]
    summary = ledger_mod.ledger_summary_text(path=ledger_path)
    assert "范围B" in summary and "只读裁决能否拾取" in summary


def check_widget_slots() -> None:
    """create_widget_blueprint 按句子里的目录和名字建，不限 WBP_HUD。槽位写了名字就不能报「槽位是空的」。"""
    import config
    from agent.plan_windows import materialize
    from agent.run_windows import _dependency_met

    saved = config.allow_umg
    slots = {"folder": "/Game/UI", "name": "WBP_HUD", "parent": "User Widget"}
    try:
        config.allow_umg = lambda: False
        held = materialize(
            "create_widget_blueprint", slots,
            title="创建左上角金币计数与倒计时 HUD 控件蓝图", wid="W1", depends=[], retries=1,
        )
        assert "槽位是空的" not in str(held.get("error") or ""), held
        assert held.get("waiting_external") is True
        assert held.get("external_dep") == "UE_ALLOW_UMG"
        assert held.get("realizable") is False

        config.allow_umg = lambda: True
        opened = materialize(
            "create_widget_blueprint", slots,
            title="创建左上角金币计数与倒计时 HUD 控件蓝图", wid="W1", depends=[], retries=1,
        )
        assert opened.get("realizable") is True, opened
        assert opened.get("folder_path") == "/Game/UI"
        assert opened.get("asset_name") == "WBP_HUD"
        assert opened.get("parent") == "/Script/UMG.UserWidget"
        assert "槽位是空的" not in str(opened.get("error") or "")

        other = materialize(
            "create_widget_blueprint",
            {"folder": "/Game/UI", "name": "WBP_Other", "parent": "User Widget"},
            title="别的控件", wid="W9", depends=[], retries=1,
        )
        assert other.get("realizable") is True, other
        assert other.get("folder_path") == "/Game/UI", other
        assert other.get("asset_name") == "WBP_Other", other
        assert other.get("ref_path") == "/Game/UI/WBP_Other.WBP_Other", other
        assert "只新建 /Game/UI/WBP_HUD" not in str(other.get("error") or ""), other

        # 20261021-183206 的现场：规划者把 folder 写成 "UI"，旧代码判不可执行，W3-W5 连带全断。
        for raw_folder in ("UI", "Game/UI", "/Game/UI/", "/game/ui"):
            loose = materialize(
                "create_widget_blueprint",
                {"folder": raw_folder, "name": "WBP_HUD", "parent": "User Widget"},
                title="创建左上角金币计数与倒计时 HUD 控件蓝图", wid="W2", depends=[], retries=1,
            )
            assert loose.get("realizable") is True, (raw_folder, loose)
            assert loose.get("folder_path") == "/Game/UI", (raw_folder, loose)
            assert loose.get("asset_name") == "WBP_HUD", (raw_folder, loose)
    finally:
        config.allow_umg = saved
    assert _dependency_met("done", "run_automation_test") is True
    assert _dependency_met("waiting_external", "run_automation_test") is True
    assert _dependency_met("waiting_external", "edit_widget_blueprint") is False
    assert _dependency_met("failed", "run_automation_test") is False


def check_unasked_compile() -> None:
    """没说编译工程时，规划者的 compile_project 直接消失，不再停下来问路径。"""
    from agent.plan_windows import _without_unasked_project_compile

    windows = [
        {"id": "W1", "action": "compile_project", "title": "编译整个工程", "depends_on": []},
        {"id": "W2", "action": "create_widget_blueprint", "title": "新建 HUD", "depends_on": ["W1"]},
        {"id": "W3", "action": "edit_widget_blueprint", "title": "搭计数", "depends_on": ["W2"]},
    ]
    kept = _without_unasked_project_compile("做一个平台跳跃关卡，左上角计数", windows)
    assert [item["id"] for item in kept] == ["W2", "W3"]
    assert kept[0]["depends_on"] == []
    assert kept[1]["depends_on"] == ["W2"]
    asked = _without_unasked_project_compile("请编译工程再启动编辑器", [
        {"id": "W1", "action": "compile_project", "title": "编译工程", "depends_on": []},
        {"id": "W2", "action": "create_widget_blueprint", "title": "新建 HUD", "depends_on": ["W1"]},
    ])
    assert [item["id"] for item in asked] == ["W1", "W2"]
    assert asked[1]["depends_on"] == ["W1"]
    # 全被拿掉时不能留空清单：留一扇暂停窗写清原因（20261022-113402 报「没有窗口」）
    empty = _without_unasked_project_compile("启动UE编辑器，其他啥也别干", [
        {"id": "W1", "action": "compile_project", "title": "编译工程", "depends_on": []},
    ])
    assert len(empty) == 1 and empty[0].get("realizable") is False, empty
    assert "launch_editor" in str(empty[0].get("error") or ""), empty


def check_launch_only() -> None:
    """「启动UE编辑器，其他啥也别干」要拆成一扇 launch_editor，不能整句 0 扇。
    20261022-113402：动作表里没有启动编辑器，理解器报缺动作 → 回退规划者写 compile_project
    → 被 _without_unasked_project_compile 拿掉 → 0 扇 → 集成只报「没有窗口」。"""
    from agent.catalog import CATALOG, DIRECT_ACTIONS, EDITOR_LOCK_ACTIONS, allows
    from agent.plan_windows import plan_windows, windows_from_rules
    from agent.supervise import _plan_rules

    query = "启动UE编辑器，其他啥也别干"
    assert "launch_editor" in CATALOG and allows("launch_editor")
    assert "launch_editor" in DIRECT_ACTIONS and "launch_editor" in EDITOR_LOCK_ACTIONS

    ruled = _plan_rules(query)
    assert isinstance(ruled, list) and ruled, ruled
    assert ruled[0].get("action") == "launch_editor", ruled
    assert ruled[0].get("save") is False and ruled[0].get("pie") is False, ruled

    windows = windows_from_rules(ruled)
    assert len(windows) == 1 and windows[0]["action"] == "launch_editor", windows
    assert windows[0].get("save") is False and windows[0].get("pie") is False, windows

    planned = plan_windows(query, complete=lambda _messages: "")
    assert planned["source"] == "rules", planned["source"]
    assert len(planned["windows"]) == 1, planned["windows"]
    assert planned["windows"][0]["action"] == "launch_editor", planned["windows"]

    # 商店句不能被当成启动编辑器
    from agent.supervise import _wants_launch_editor
    assert _wants_launch_editor(query) is True
    assert _wants_launch_editor("打开虚幻商城看道具") is False
    assert _wants_launch_editor("打开 UE 编辑器") is True


def check_board() -> None:
    """主代理画路线图；运行中的窗口是一块 CLI；完成后收起；E 切换详细日志。"""
    from rich.console import Console

    from agent.board import Board
    from agent.cli_log import bind_window, cli_record, reset_window
    from agent.run_windows import _EventBus, append_cli_log, runs_cli_path

    out = io.StringIO()
    console = Console(file=out, width=100, height=40, force_terminal=False, legacy_windows=False)
    logged = []
    board = Board(console=console, on_log=logged.append)
    board.event({
        "type": "run_start", "run_id": "20990101-000001",
        "query": "把 BP_TestActor 设为可拾取再裁决",
        "windows": [
            {"id": "W1", "title": "设为可拾取", "action": "set_pickable", "depends_on": []},
            {"id": "W2", "title": "只读裁决", "action": "judge_pickup", "depends_on": ["W1"]},
        ],
    })
    board.event({"type": "window_state", "window": "W1", "state": "running"})
    board.event({
        "type": "tool_call", "window": "W1", "node": "plan", "tool": "llm", "ok": True,
        "reasoning": "先读控件再改可拾取", "calls": ["set_actor_property"],
    })
    token = bind_window("W1")
    try:
        board.feed_line(">> [UE] ActorTools::set_actor_transform")
    finally:
        reset_window(token)
    board.expanded = True
    console.print(board.render())
    live = out.getvalue()
    for token in ("主代理", "路线图", "设为可拾取", "只读裁决", "子代理", "思考", "先读控件",
                 "set_actor_transform", "E 简略"):
        assert token in live, (token, live)
    assert "结果" not in live

    out.seek(0)
    out.truncate(0)
    board.event({"type": "window_result", "window": "W1", "ok": True, "state": "done",
                 "summary": "bPickable 为真", "elapsed": 1.2, "retries": 0})
    board.event({"type": "files_modified", "window": "W1", "kinds": "scene"})
    console.print(board.render())
    mid = out.getvalue()
    assert "排队" in mid and "只读裁决" in mid
    assert "bPickable" not in mid  # W1 的 CLI 已收起，结果等全部结束再给

    out.seek(0)
    out.truncate(0)
    board.event({"type": "window_state", "window": "W2", "state": "running"})
    board.event({"type": "judge", "window": "W2", "node": "test", "tool": "automation", "ok": True,
                 "summary": "能拾取"})
    board.event({"type": "window_result", "window": "W2", "ok": True, "state": "done",
                 "summary": "能拾取", "elapsed": 0.8, "retries": 0})
    board.event({"type": "integration", "ok": True, "status": "成功", "summary": "全部窗口完成"})
    board.event({"type": "run_end", "status": "成功", "ok": True})
    console.print(board.render())
    done = out.getvalue()
    for token in ("结果", "子代理已关闭", "修改了文件", "bPickable 为真", "能拾取", "1.2",
                 "全部窗口完成", "20990101-000001.cli.jsonl", "先读控件"):
        assert token in done, (token, done)
    assert logged and logged[-1]["window"] == "W1" and "set_actor_transform" in logged[-1]["text"]

    rec = cli_record({
        "type": "tool_call", "run_id": "20990101-000001", "window": "W1",
        "node": "plan", "tool": "llm", "ok": True, "reasoning": "先读控件再改可拾取",
    })
    assert rec and rec["kind"] == "think" and "先读控件" in rec["text"]
    bus = _EventBus("20990101-000001")
    bus.emit("tool_call", window="W1", node="plan", tool="llm", ok=True, reasoning="先读控件再改可拾取")
    path = runs_cli_path("20990101-000001")
    saved = path.read_text(encoding="utf-8")
    assert "先读控件再改可拾取" in saved
    append_cli_log("20990101-000001", {"ts": "", "run_id": "20990101-000001", "window": "W1",
                                      "kind": "stdout", "text": ">> [UE] ActorTools::set_actor_transform"})
    assert "set_actor_transform" in path.read_text(encoding="utf-8")


def check_rules_dechain() -> None:
    """阶段五（P6）：规则窗口去假链——独立读写不虚连，测试接编译，创建蓝图按数据流接改码。"""
    from agent.plan_windows import windows_from_rules
    from agent.supervise import plan_sequence

    # 独立的两件事：改 ATestActor 的移速 + 移动 Cube。目标不同，不该虚连（旧假链会给 W2 挂 W1）
    independent = "把场景里的 Cube 移到 (X=100,Y=200,Z=50)，并给 ATestActor 的 MoveSpeed 改成 200 再编译"
    windows = windows_from_rules(plan_sequence(independent))
    by_action = {str(w.get("action") or ""): w for w in windows}
    assert set(by_action) >= {"move_actor", "edit_cpp"}, set(by_action)
    move = by_action["move_actor"]
    edit = by_action["edit_cpp"]
    assert edit["depends_on"] == [], edit            # 改码不虚连移动
    assert move["depends_on"] == [], move           # 移动不虚连改码

    # 长句：创建蓝图接改码（同目标数据流），放进场景与裁决各自按名字流接上游
    long_sentence = (
        "给 ATestActor 新增一个是否可被拾取的属性，tag=InteractableActor，"
        "修改完编译后启动UE编辑器并创建蓝图丢到场景中测试是否可以被pawn拾取"
    )
    windows2 = windows_from_rules(plan_sequence(long_sentence))
    actions = [str(w.get("action") or "") for w in windows2]
    assert actions == ["edit_cpp", "create_blueprint", "place_blueprint", "judge_pickup"], actions
    assert windows2[1]["depends_on"] == ["W1"], windows2[1]        # 创建蓝图按同目标接改码
    assert "W2" in windows2[2]["depends_on"], windows2[2]          # 放进场景接创建
    assert "W3" in windows2[3]["depends_on"], windows2[3]          # 裁决接放置

    # 测试仍接在编译后面（回归）
    wasd = ("修改ATestPawn代码，使得它可以通过WASD前后左右移动，"
            "目前测试下来是无法移动的，你帮我修复它并且测试")
    windows3 = windows_from_rules(plan_sequence(wasd))
    assert [w["action"] for w in windows3] == ["edit_cpp", "probe_move"]
    assert windows3[1]["depends_on"] == ["W1"], windows3


def check_cpp_window_context() -> None:
    """20261024-0215 的两个工作流缺陷（WF-1 / WF-2）离线钉桩：不叫模型、不写场景。

    WF-1：`edit_cpp` 窗口按规划者的 title + class 重建任务句时把原句需求整段丢了
    （`_try_cpp(f"{title}。类 {class_name}。编译")`）。20261024-011917 的 W1 只拿到
    「改 C++ 并编译。类 ATestPawn。编译」，模型只好猜，最后改了个无关的
    `bCanBePossessed = true`，编译 exit 0 就算成功——低 token 的代价被转嫁到正确性上。

    WF-2：句子里唯一点名一个没有 UCLASS 的 .cpp 时，`_pinned_class` 返回 None →
    `_try_cpp` 回落到 `_unique_class` 猜句子里的别的类。20261024-013644 的 W1 因此生成
    「只改 Public/TestPawn.h 与 Private/TestPawn.cpp」，与用户点名的
    Private/PlatformRunTest.cpp 自相矛盾，模型一次 diff 都没写。
    """
    import agent.supervise as sm
    from agent import run_windows as rw

    with tempfile.TemporaryDirectory() as tmp_name:
        root = Path(tmp_name) / "source"
        (root / "Public").mkdir(parents=True)
        (root / "Private").mkdir(parents=True)
        (root / "Public" / "TestPawn.h").write_text("class ATestPawn {};\n", encoding="utf-8")
        (root / "Private" / "TestPawn.cpp").write_text("// pawn\n", encoding="utf-8")
        # 点名的这个文件没有 UCLASS（真实工程里 PlatformRunTest.cpp 是 AutomationTest）
        (root / "Private" / "PlatformRunTest.cpp").write_text(
            "// IMPLEMENT_SIMPLE_AUTOMATION_TEST\n", encoding="utf-8"
        )

        def records(source_type=None):
            if source_type == "ue_cpp":
                return [{"id": "c1", "content": "C++ 类 ATestPawn，文件 TestPawn.h", "metadata": {
                    "source_type": "ue_cpp", "class_name": "ATestPawn", "file_name": "TestPawn.h"}}]
            return []

        saved = (sm._records, sm.UE_SOURCE_ROOT, rw.UE_SOURCE_ROOT)
        sm._records = records
        sm.UE_SOURCE_ROOT = root
        rw.UE_SOURCE_ROOT = root
        try:
            origin = (
                "在 L_NewTest 里做一个平台跳跃关卡：玩家操纵 ATestPawn，WASD 移动、空格跳跃，"
                "金币会旋转发光。修改 Private/TestPawn.cpp 的跳跃落地逻辑，然后编译工程。"
            )
            window = {"id": "W1", "action": "edit_cpp", "title": "改 C++ 并编译", "class_name": "ATestPawn"}
            task = rw._build_graph_task(window, origin)
            assert task and task.get("ok") is not False, task

            # WF-1：原句需求必须带到这一扇的任务句里，不许只剩标题与类名
            text = str(task.get("query") or "")
            assert "跳跃落地逻辑" in text, text
            assert origin.split("。")[1] in text, text          # 需求半句整句在，不是只留几个词
            pin_lines = [line for line in text.splitlines() if "只改" in line]
            assert pin_lines, text
            assert "Public/TestPawn.h" in pin_lines[0] and "Private/TestPawn.cpp" in pin_lines[0], pin_lines[0]
            # 这一扇的判定语义（20261021-195220 的教训）不许被改动
            assert task["verify"] == [{"kind": "cpp_build"}], task.get("verify")
            assert task["judge_first"] is False and task["expect_source_write"] is True, task
            assert task["id"] == "W1" and task["title"] == "改 C++ 并编译", task

            # 同一个修复要能以「类」为入口复用，供 cpp_task_for_file 之外的地方调用
            assert hasattr(sm, "cpp_task_for_class"), "WF-1 缺 cpp_task_for_class"
            by_class = sm.cpp_task_for_class(origin, "ATestPawn")
            assert by_class and by_class.get("ok") is not False, by_class
            assert "跳跃落地逻辑" in str(by_class.get("query") or ""), by_class.get("query")
            assert sm.cpp_task_for_class(origin, "ANoSuchClass") is None, "解析不出类时不许编一个"

            # 索引里没有这个类时回落旧写法，且仍然不猜：返回 ok=False 而不是硬造一扇
            fallback = rw._build_graph_task({**window, "class_name": "ANoSuchClass"}, origin)
            assert fallback.get("ok") is False, fallback

            # WF-2：唯一有名的文件没有 UCLASS，钉死这个文件，绝不许回落到句子里的别的类。
            # 这句刻意同时提到 ATestPawn（真实 20261024-013644 就是这样），旧代码正是在这里换类。
            named = (
                "修改 Private/PlatformRunTest.cpp 的 AIAgent.Platform.Run 测试："
                "新增一段跳跃落地与可达性断言；把 ATestPawn 的 MoveSpeed 设为 300，然后编译工程。"
            )
            pinned = sm._try_cpp(named)
            assert pinned and pinned.get("ok") is not False, pinned
            pinned_text = str(pinned.get("query") or "")
            pinned_lines = [line for line in pinned_text.splitlines() if "只改" in line]
            assert pinned_lines, pinned_text
            pin = pinned_lines[0]
            assert "PlatformRunTest.cpp" in pin, pin
            assert "TestPawn" not in pin, f"WF-2 回落到别的类了：{pin}"
            assert "PlatformRunTest.h" not in pin, f"凭空补了不存在的头文件：{pin}"
            assert pinned["verify"] == [{"kind": "cpp_build"}], pinned.get("verify")
            assert rw._build_graph_task(
                {"id": "W1", "action": "edit_cpp", "title": "改 C++ 并编译",
                 "class_name": "PlatformRunTest"}, named
            ).get("ok") is not False

            # 原句唯一点名的文件优先级最高：规划者的 class 槽位写错了也不许把点名文件甩掉。
            # （WF-1 新分支如果先按 class 槽位命中，WF-2 就会从这条路上原样复发。）
            def _pin_of(task: dict) -> str:
                lines = [line for line in str((task or {}).get("query") or "").splitlines() if "只改" in line]
                assert lines, (task or {}).get("query")
                return lines[0].split("。")[0]

            named_window = {"id": "W1", "action": "edit_cpp", "title": "改 C++ 并编译"}
            conflict = rw._build_graph_task({**named_window, "class_name": "ATestPawn"}, named)
            assert conflict.get("ok") is not False, conflict
            assert "PlatformRunTest.cpp" in _pin_of(conflict), _pin_of(conflict)
            assert "TestPawn" not in _pin_of(conflict), f"WF-2 从 WF-1 的路上复发：{_pin_of(conflict)}"

            # class 槽位为空、但原句点名了文件：同样要落到点名的那个文件，不许直接失败
            bare_slot = rw._build_graph_task(named_window, named)
            assert bare_slot.get("ok") is not False, bare_slot
            assert "PlatformRunTest.cpp" in _pin_of(bare_slot), _pin_of(bare_slot)

            # 点名的文件在源码树里找不到唯一一份：停下来让人 r 补，不许编路径
            missing = sm._try_cpp("修改 Private/NoSuchFile.cpp 的跳跃落地逻辑，然后编译工程。")
            assert missing.get("ok") is False, missing
        finally:
            sm._records, sm.UE_SOURCE_ROOT, rw.UE_SOURCE_ROOT = saved


def check_menu2_no_fake_success() -> None:
    """WF-3（20261024-0215）：菜单 2 的规划者失败回退不许假成功。

    `steps_for_menu2` 在 `_planner_failed` 时直接吃 `_plan_rules` 的裸结果，绕过菜单 3 也走的
    `_rules_cover` 那道闸。实测平台跳跃整句只剩 1 步 `place_blueprint(BP_Coin)`：平台/门/进度中枢
    全丢、没有 PIE 验证步，`run_sequence` 却一路 return 0。同一句在菜单 3 必落 `_paused` 暂停扇。

    这里只断言对外行为（不依赖内部 helper 名字）：弱命中不许给出「全部 ok」的步骤清单，
    强命中仍必须原样放行。
    """
    from agent.plan_windows import _rules_cover, steps_for_menu2

    # 必须用需求原文的整段（含「这些逻辑已经做好了，不用改代码也不用改蓝图」）。
    # 截断成「金币用 BP_Coin…最后进 PIE」时它反而是强命中（place_blueprint + probe_play 全 ok），
    # 那属于 L0 正常放行，跟这里的「回退分支不许假成功」是两回事，别混为一谈。
    # 原句落在 data/dsh/query_platform.txt，但 data/dsh/* 是 gitignore 的，CI 上没有这个文件，
    # 所以整段内联在这里；句子内容与那次 run 的 run.query 一字不差。
    platform = (
        "在当前关卡 L_NewTest 里摆好平台跳跃关卡并验证能玩：金币用 BP_Coin，平台用 BP_Platform，"
        "门用 BP_Door，进度中枢用 BP_Progress，每个都要放进当前关卡并保存。"
        "金币会旋转发光、前段平台不动、后段平台会动、90 秒倒计时、收齐金币后开门并在 UI 显示通关，"
        "这些逻辑已经在 C++ 和 WBP_HUD 里做好了，不用改代码也不用改蓝图。最后进 PIE 验证能玩。"
    )

    # 检索标题固定为空：这条用例只验拆句闸门，不该去拉检索模型（CI 上没有 data/chroma）
    import agent.brief as brief_mod

    saved_titles = brief_mod._retrieval_titles
    brief_mod._retrieval_titles = lambda _query, k=3: []
    try:
        # 前提：这句确实是弱命中，走的才是回退分支（否则下面验的不是同一条路）
        from agent.supervise import _plan_rules

        weak_ruled = _plan_rules(platform) or []
        assert weak_ruled and _rules_cover(platform, weak_ruled) is False, (
            "这条用例要求弱命中（_rules_cover=False），现在被判成强命中了，用例失效",
            [step.get("action") for step in weak_ruled],
        )

        def no_model(_messages):
            return "不是 JSON"

        weak = steps_for_menu2(platform, complete=no_model)
        assert weak, "弱命中至少要保留规则拆出的步骤"
        unfinished = [step for step in weak if step.get("ok") is False or step.get("blocked")]
        assert unfinished, f"WF-3 假成功：规则只拆出 {[s.get('action') for s in weak]} 却全部 ok"
        text = " ".join(str(step.get("error") or "") for step in unfinished)
        assert "未完成" in text, text
        assert "没有一次带 spec 的验证" in text, text
        # 收尾步不许抢跑：它排在最后，前面拆出来的步骤照样执行
        assert weak[-1].get("ok") is False and weak[-1].get("blocked") is True, weak[-1]
        # 规则本来拆得出的可执行步骤不许被这条收尾步吞掉
        assert [step.get("action") for step in weak[:-1]] == [
            step.get("action") for step in weak_ruled
        ], ([s.get("action") for s in weak], [s.get("action") for s in weak_ruled])

        # 强命中（_rules_cover 为真）路径一字不改：不许被加尾巴，也不许叫模型
        strong_sentence = "编译工程后启动 UE 编辑器"
        ruled = _plan_rules(strong_sentence) or []
        assert _rules_cover(strong_sentence, ruled) is True, ruled

        def boom(_messages):
            raise AssertionError("强命中不应叫模型")

        strong = steps_for_menu2(strong_sentence, complete=boom)
        assert strong == ruled, (strong, ruled)
        assert all(step.get("ok") is not False and not step.get("blocked") for step in strong), strong
        assert not any("未完成" in str(step.get("error") or "") for step in strong), strong
    finally:
        brief_mod._retrieval_titles = saved_titles


def check_stall_fixes() -> None:
    """180852 失败的修复：build 前打转熔断、diff 冲突带定位、execute 记失败原文。不叫模型，不写场景。"""
    from agent import nodes as nodes_mod
    from agent.nodes import _editor_read_stall, _pre_build_stall
    from core import source_edit as se

    def state(**fields):
        base = {"require_build": True, "build_result": "", "tool_outcomes": []}
        base.update(fields)
        return base

    def reads(count, ok=True):
        return [{"tool": "read_source", "ok": ok, "error": ""} for _ in range(count)]

    # 写码成功后连续 8 次只读 → 动作是 build（借现有空工具调用→build 路由），不再判失败
    text, action = _pre_build_stall(state(source_written=True, tool_outcomes=reads(8)))
    assert action == "build" and "直接进入编译裁判" in text, (action, text)
    # 没写码时只读不算（还没到该编译的时候），次数也不够 12
    assert _pre_build_stall(state(source_written=False, tool_outcomes=reads(8))) == ("", "")
    # build 已经跑过 → 不熔断（那种打转归 graph._read_stall 管）
    assert _pre_build_stall(state(source_written=True, tool_outcomes=reads(8), build_result="{}")) == ("", "")
    # 失败过半且无一次写码成功：12 次全在读/diff，6 次失败 → 诊断带最近一次错误原文，动作是 stop
    outcomes = []
    for _ in range(6):
        outcomes.append({"tool": "apply_unified_diff", "ok": False, "error": "diff conflict：旧行对不上"})
        outcomes.append({"tool": "read_source", "ok": True, "error": ""})
    text, action = _pre_build_stall(state(source_written=True, tool_outcomes=outcomes))
    assert action == "stop" and "6 次失败" in text and "没有一次写码成功" in text and "旧行对不上" in text, (action, text)
    # 窗口内有成功的 diff：还在前进，不熔断（190500 那次误杀的形态）
    converging = list(outcomes)
    converging[-1] = {"tool": "apply_unified_diff", "ok": True, "error": ""}
    assert _pre_build_stall(state(source_written=True, tool_outcomes=converging)) == ("", "")
    # 全是成功读 → 不熔断
    assert _pre_build_stall(state(source_written=False, tool_outcomes=reads(20))) == ("", "")
    # 最近掺了别的工具 → 不熔断
    mixed = reads(10) + [{"tool": "find_actors", "ok": True, "error": ""}]
    assert _pre_build_stall(state(source_written=False, tool_outcomes=mixed)) == ("", "")

    # 第一形态重定向：plan_node 不调 LLM，返回空 tool_calls（路由进 build），不结束任务
    redirect = nodes_mod.plan_node({
        **state(source_written=True, tool_outcomes=reads(8)),
        "messages": [], "user_query": "改 TestPawn", "available_tools": [],
        "task": {"verify": [{"kind": "cpp_build"}], "allow_writes": []},
        "retry_count": 0, "max_retries": 3,
    })
    assert redirect["tool_calls"] == []
    assert not redirect.get("is_completed")
    assert not redirect.get("last_error")
    from agent.graph import route_after_plan
    assert route_after_plan({
        "is_completed": False, "retry_count": 0, "max_retries": 3,
        "tool_calls": [], "require_build": True, "build_result": "",
    }) == "build"

    # 20261021-121520：控件只读打转。没写过不能进编译（会把没改的 WBP 判成通过）。
    widget_reads = [{"tool": "GetWidgets", "ok": True, "error": ""}, {"tool": "GetWidgetDescription", "ok": True, "error": ""}] * 4
    text, action = _editor_read_stall(state(tool_outcomes=widget_reads))
    assert action == "stop" and "没有一次写入" in text, (action, text)
    assert _editor_read_stall(state(tool_outcomes=widget_reads[:6])) == ("", "")
    wrote_then_read = [{"tool": "AddWidget", "ok": True, "error": ""}] + widget_reads
    text, action = _editor_read_stall(state(tool_outcomes=wrote_then_read))
    assert action == "build" and "编译裁判" in text, (action, text)
    stopped = nodes_mod.plan_node({
        **state(tool_outcomes=widget_reads),
        "messages": [], "user_query": "改 WBP_HUD", "available_tools": [],
        "task": {"verify": [{"kind": "blueprint_compile"}], "allow_writes": ["GetWidgets", "AddWidget"]},
        "retry_count": 0, "max_retries": 3,
    })
    assert stopped.get("is_completed") is True and stopped.get("final_success") is False
    assert "打转" in (stopped.get("last_error") or "")
    assert route_after_plan({
        "is_completed": True, "retry_count": 0, "max_retries": 3,
        "tool_calls": [], "require_build": True, "build_result": "",
        "final_success": False,
    }) == "end"

    # read_source：text 是纯代码不带行号，行号只看 numbered
    out = se.read_source("Public/TestPawn.h", 1, 4)
    assert out["ok"] is True
    numbered = out["data"]["numbered"].splitlines()
    assert numbered[0].startswith("1|")
    assert out["data"]["text"].splitlines()[0] == numbered[0][2:]

    # diff 冲突带定位：指出是哪一行、diff 给了什么、文件里是什么
    bad = se.apply_unified_diff("Public/TestPawn.h", "@@ -1,1 +1,1 @@\n-not this line\n+new\n")
    assert bad["ok"] is False
    assert "旧行对不上" in bad["error"] and "not this line" in bad["error"] and "文件里是" in bad["error"]
    # 行号超界 → 指出超界
    far = se.apply_unified_diff("Public/TestPawn.h", "@@ -99999,1 +1,1 @@\n-whatever\n+new\n")
    assert far["ok"] is False and "超出文件" in far["error"]
    # 非 unified diff → 提示格式
    junk = se.apply_unified_diff("Public/TestPawn.h", "就改一下这行")
    assert junk["ok"] is False and "unified diff" in junk["error"]

    # execute_node 的 step_log / tool_outcomes 记失败原文（走 role refused 分支，不调模型、不碰编辑器）
    update = nodes_mod.execute_node({
        "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "set_actor_transform", "arguments": "{}"},
        }],
        "tools_used": [], "tool_outcomes": [], "source_written": False,
        "task": {"verify": [{"kind": "cpp_build"}], "allow_writes": []},
    })
    item = update["step_log"][0]
    assert item["ok"] is False and item.get("error") == "role refused", item
    assert update["tool_outcomes"][0]["tool"] == "set_actor_transform"
    assert update["tool_outcomes"][0]["ok"] is False


def check_intent_layer(runs: Path, events_dir: Path) -> None:
    """20261015-1705 P1-P3：意图层 schema 验收、理解器请求体受限、stub 意图经 L1→L2 拆窗、
    三种现场归地（受控语料）、学习回写与 L0 重放。不叫真模型，不写场景。"""
    import agent.plan_windows as pw
    from agent import run_task as run_task_mod
    from agent import run_windows as rw
    from agent import supervise as supervise_mod
    from agent.grounding import link_asset
    from agent.intent import validate_intent
    from agent.plan_windows import _save_routes, load_routes, plan_windows, remember_intent_route
    from agent.understand import understand

    # 前一项 check_events_and_state 的规划者路径会把 mark-interactable（windows 旧格式）
    # 种进同一个临时 learned 文件；这里先清空，P1-P3 验收各自从零开始。
    if pw.routes_path().is_file():
        pw.routes_path().unlink(missing_ok=True)

    # P1：validate_intent——表外动作拒收、未声明槽位丢弃并列进 dropped、未知字段丢弃，save 如实记录
    intent = validate_intent({
        "bogus_top": 1,
        "goals": [
            {"action": "open_marketplace", "slots": {}, "extra": 2},
            {"action": "place_actor", "slots": {"blueprint": "BP_TestEnemy", "junk": "x"}, "junk2": 3},
            {"action": "compile_project", "slots": {}},
        ],
        "constraints": {"editor": True, "save": True, "bogus": 4},
        "unresolved": ["顺带看看商城"],
    }, "原句")
    assert intent["ok"] is True
    assert [goal["action"] for goal in intent["goals"]] == ["place_actor", "compile_project"]
    dropped = "；".join(intent["dropped"])
    assert "表外动作 open_marketplace" in dropped, dropped
    assert "junk" in dropped and "junk2" in dropped and "bogus" in dropped, dropped
    assert intent["constraints"]["save"] is True
    assert intent["unresolved"] == ["顺带看看商城"]
    assert validate_intent({"goals": [{"action": "nope"}]}, "原句")["ok"] is False

    # 任务书闸门：理解器把半句扔进 unresolved（哪怕扇数不到）也先出任务书——
    # 没着落的半句正是「尚未完成的全部主要内容」该记的（20261015-180219 的教训）
    from agent.brief import needs_brief

    assert needs_brief("普通句子", [{"id": "W1"}], {"unresolved": ["实现巡逻的逻辑"]}) is True
    assert needs_brief("普通句子", [{"id": "W1"}], {"unresolved": []}) is False
    assert needs_brief("普通句子", [{"id": "W1"}]) is False

    # P1：understand 请求体只带动作卡片 + 原句 + 3 条检索标题（1KB 上下），三条铁律进系统提示
    seen: dict[str, object] = {}

    def ok_intent(messages):
        seen["messages"] = messages
        return json.dumps({
            "query": "原句",
            "goals": [{"action": "place_actor", "slots": {"blueprint": "BP_Gun"}}],
            "constraints": {},
            "unresolved": [],
        }, ensure_ascii=False)

    got = understand("把 BP_Gun 放到场景", complete=ok_intent)
    assert got is not None and got["ok"], got
    assert got["goals"][0]["slots"]["blueprint"] == "BP_Gun"
    blob = json.dumps(seen["messages"], ensure_ascii=False)
    for banned in ("input_schema", "toolset", "ue_mcp_index", "search_ue_project"):
        assert banned not in blob, banned
    system_text = seen["messages"][0]["content"]
    assert "action 只能取动作表的名字" in system_text and "unresolved" in system_text
    assert len(seen["messages"][1]["content"]) < 1024, "用户消息应控制在 1KB 上下"
    assert understand("句子", complete=lambda _m: "不是 JSON") is None

    # 受控语料：索引里只有 BP_Gun / BP_Gun2，C++ 索引只有 ATestEnemy / ATestPawn；磁盘不查
    def controlled_records(source_type=None):
        if source_type == "ue_asset":
            return [
                {"id": "a1", "content": "蓝图 BP_Gun。/Game/BP_Gun.BP_Gun", "metadata": {
                    "source_type": "ue_asset", "asset_name": "BP_Gun", "asset_class": "Blueprint",
                    "package_name": "/Game/BP_Gun", "object_path": "/Game/BP_Gun.BP_Gun"}},
                {"id": "a2", "content": "蓝图 BP_Gun2。/Game/Props/BP_Gun2.BP_Gun2", "metadata": {
                    "source_type": "ue_asset", "asset_name": "BP_Gun2", "asset_class": "Blueprint",
                    "package_name": "/Game/Props/BP_Gun2", "object_path": "/Game/Props/BP_Gun2.BP_Gun2"}},
            ]
        if source_type == "ue_cpp":
            return [
                {"id": "c1", "content": "C++ 类 ATestEnemy，文件 TestEnemy.h", "metadata": {
                    "source_type": "ue_cpp", "class_name": "ATestEnemy", "file_name": "TestEnemy.h"}},
                {"id": "c2", "content": "C++ 类 ATestPawn，文件 TestPawn.h", "metadata": {
                    "source_type": "ue_cpp", "class_name": "ATestPawn", "file_name": "TestPawn.h"}},
            ]
        return []

    saved_records = supervise_mod._records
    saved_disk = pw._disk_blueprint
    supervise_mod._records = controlled_records
    pw._disk_blueprint = lambda _name: None

    def norm(action: str) -> str:
        return "place" if action in ("place_actor", "place_blueprint") else action

    def to_norm(actions) -> list:
        return [norm(str(item)) for item in actions]

    try:
        # P2 验收：同一句的三种现场——索引命中 linked / 类可推导 create_planned / 都推不出 pause_r
        hit = link_asset("BP_Gun")
        assert hit["plan"] == "linked" and hit["fields"]["ref_path"] == "/Game/BP_Gun.BP_Gun", hit
        derived = link_asset("BP_TestEnemy")
        assert derived["plan"] == "create_planned", derived
        assert derived["create"]["asset_name"] == "BP_TestEnemy"
        assert derived["create"]["parent"] == "/Script/AIAgentProject.TestEnemy"
        assert derived["create"].get("needs") == "path", "没给目录：create 扇自己等一次 r"
        assert derived["fields"] is None
        nobody = link_asset("BP_Nobody")
        assert nobody["plan"] == "pause_r" and nobody["ok"] is False
        assert nobody["needs"] and "只给一次" in nobody["error"]
        with_path = link_asset("BP_TestEnemy", paths=["/Game/BP_TestEnemy"])
        assert with_path["plan"] == "create_planned"
        assert with_path["create"].get("ok") is True
        assert with_path["create"]["ref_path"] == "/Game/BP_TestEnemy.BP_TestEnemy"

        # P1 验收：五次失败的原话（planning-corpus.json）拆出的窗口动作序列
        corpus_path = Path(__file__).resolve().parent / "tasks" / "side" / "planning-corpus.json"
        corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
        cases = [case for case in corpus["cases"] if case.get("intent")]
        assert cases, corpus

        def intent_stub(messages):
            payload = json.loads(messages[1]["content"])
            origin = str(payload.get("原句") or "")
            case = next((item for item in cases if item["query"] == origin), None)
            assert case is not None, f"语料里没有这句：{origin}"
            return json.dumps({**case["intent"], "query": case["query"]}, ensure_ascii=False)

        for case in cases:
            planned_case = plan_windows(case["query"], complete=intent_stub)
            actions = to_norm(window.get("action") or "" for window in planned_case["windows"])
            assert actions == to_norm(case["expect_actions"]), (case["id"], actions)
            assert planned_case["understand_called"] is False or planned_case["source"] == "understood"
            blocked = [window for window in planned_case["windows"] if window.get("blocked")]
            assert not blocked, (case["id"], [window.get("error") for window in blocked])
            if planned_case["source"] == "understood":
                assert planned_case["route_hit"] is False
            # 181645 的教训：unresolved 非空的意图必须触发任务书闸门，需求半句不许静默丢
            if (case["intent"] or {}).get("unresolved"):
                assert needs_brief(case["query"], planned_case["windows"], case["intent"]) is True, case["id"]

        # 需求词兜底：句里有「实现/逻辑/随机/功能/需求/行为」而没有任何改码扇 → 不算 L0 强命中
        from agent.plan_windows import _rules_cover, _unbound_demand

        assert _unbound_demand("实现生成位置随机", {"compile_project", "place_actor"}) is True
        assert _unbound_demand("实现生成位置随机", {"edit_cpp"}) is False
        assert _unbound_demand("把 Cube 移到 (100,200,50)", {"move_actor"}) is False
        assert _rules_cover("普通编译", [{"ok": True, "verify": [{"kind": "cpp_build"}]}]) is True

        # L0：手写规则现在能拆的句子 understand_called=false（stub 只会说意图，绝不给窗口清单）
        def boom(_messages):
            raise AssertionError("快路径不应叫模型")

        compiled = plan_windows("编译工程后启动 UE 编辑器", complete=boom)
        assert compiled["source"] == "rules"
        assert compiled["understand_called"] is False and compiled["route_hit"] is True

        # L1 重试上限 1 次：第一次解析失败，第二次注入合法意图，不再走老规划者
        counter = {"n": 0}

        def flaky_intent(messages):
            counter["n"] += 1
            if counter["n"] == 1:
                return "不是 JSON"
            return json.dumps({
                "query": "原句",
                "goals": [{"action": "place_actor", "slots": {"blueprint": "BP_Gun"}}],
                "constraints": {}, "unresolved": [],
            }, ensure_ascii=False)

        retried = plan_windows("把这个东西丢到场景里面去", complete=flaky_intent)
        assert counter["n"] == 2, counter
        assert retried["source"] == "understood" and retried["planner_called"] is False
        assert [window.get("action") for window in retried["windows"]] == ["place_actor"]

        # P3：跑赢一句新句式 → 事件流有回写记录 → 变体句零模型调用命中 L0 并归地成功
        mark_query = "给 BP_Gun 打上可交互标签，让 BP_Gun2 与它交互"
        counter["n"] = 0

        def mark_intent(messages):
            counter["n"] += 1
            return json.dumps({
                "query": mark_query,
                "goals": [
                    {"action": "set_pickable", "slots": {"target": "BP_Gun"}},
                    {"action": "judge_pickup", "slots": {"target": "BP_Gun", "picker": "BP_Gun2"}},
                ],
                "constraints": {}, "unresolved": [],
            }, ensure_ascii=False)

        def fake_ok(task, trace_path=None, thread_id=None, on_row=None):
            if on_row:
                on_row({"node": "direct", "tool": task.get("action"), "ok": True, "elapsed": 0})
            return {
                "id": task.get("id"), "ok": True,
                "summary": "bPickable 为真，带 InteractableActor" if task.get("action") == "set_pickable" else "不能拾取：距离远",
                "judge": "不能" if task.get("action") != "set_pickable" else None,
                "ref": "ActorRef", "in_level": True, "thread_id": thread_id,
            }

        original = run_task_mod.run_task
        run_task_mod.run_task = fake_ok
        try:
            code, run_path = rw.execute_windows(mark_query, complete=mark_intent, echo=_quiet)
        finally:
            run_task_mod.run_task = original
        assert code == 0, code
        run_id = Path(run_path).stem
        event_rows = [
            json.loads(line) for line in (events_dir / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        types = [row.get("type") for row in event_rows]
        assert "intent" in types and "learned_route" in types, types
        intent_event = next(row for row in event_rows if row.get("type") == "intent")
        assert intent_event.get("understand_called") is True and intent_event.get("route_hit") is False
        learned_event = next(row for row in event_rows if row.get("type") == "learned_route")
        assert learned_event.get("route_id") == "mark-interactable", learned_event
        routes = load_routes()
        entry = next(item for item in routes if item.get("id") == "mark-interactable")
        assert entry["when"] == ["标签", "可交互"], entry
        assert entry["roles"] == ["target", "picker"], entry
        assert [goal["action"] for goal in entry["goals"]] == ["set_pickable", "judge_pickup"], entry
        assert entry["goals"][0]["slots"] == {"target": "target"}, entry
        assert entry["goals"][1]["depends_on"] == ["G1"], entry
        raw_text = json.dumps(entry, ensure_ascii=False)
        assert "BP_Gun" not in raw_text, "回写不得记资产名"

        variant = plan_windows("给 BP_Gun2 打上可交互标签，让 BP_Gun 与它交互", complete=boom)
        assert variant["source"] == "learned" and variant["planner_called"] is False
        assert variant["understand_called"] is False and variant["route_hit"] is True
        assert [window.get("action") for window in variant["windows"]] == ["set_pickable", "judge_pickup"]
        assert all(window.get("realizable") for window in variant["windows"]), "变体句应在 L0 命中并归地成功"
        assert variant["windows"][1]["depends_on"] == ["W1"]
        assert variant["windows"][0]["slots"]["target"] == "BP_Gun2"

        # P3 护栏：unresolved 非空的意图不回写（半猜测不进缓存）
        goal_windows = pw.materialize_goal(
            "set_pickable", {"target": "BP_Gun"}, title="把 BP_Gun 设为可拾取", wid="W1", depends=[], retries=1,
        )
        before_count = len(load_routes())
        assert goal_windows[0].get("goal") is True
        assert remember_intent_route(
            mark_query, {"goals": [{"action": "set_pickable", "slots": {"target": "BP_Gun"}}],
                         "unresolved": ["还有半句没着落"]}, goal_windows,
        ) is None
        assert len(load_routes()) == before_count

        # P3 护栏：条目 LRU 上限 50
        _save_routes([{"id": f"r{index}", "when": ["标签"], "roles": ["target"], "windows": []} for index in range(60)])
        assert len(load_routes()) == 50
    finally:
        supervise_mod._records = saved_records
        pw._disk_blueprint = saved_disk


def check_index_stale(events_dir: Path) -> None:
    """P5：真新建蓝图资产的 run 收尾发 index_stale 事件并写进 run json 与 md；
    「不重复新建」和没有 create 扇的 run 不提示。不叫模型，不写场景。"""
    from agent import run_task as run_task_mod
    from agent import run_windows as rw

    def window(summary: str) -> dict:
        return {
            "id": "W1", "title": "创建蓝图", "action": "create_blueprint", "slots": {},
            "depends_on": [], "max_retries": 1, "realizable": True, "ok": True, "blocked": False,
            "save": False, "pie": False, "goal": True,
            "task": {"id": "W1", "action": "create_blueprint", "title": "创建蓝图",
                     "query": "创建蓝图 BP_Gun", "folder_path": "/Game", "asset_name": "BP_Gun",
                     "ref_path": "/Game/BP_Gun.BP_Gun", "save": False, "pie": False, "max_retries": 1,
                     "summary": summary},
        }

    def fake(task, trace_path=None, thread_id=None, on_row=None):
        return {"id": task.get("id"), "ok": True, "summary": str(task.get("summary") or task.get("ref_path") or ""),
                "thread_id": thread_id}

    original = run_task_mod.run_task
    run_task_mod.run_task = fake
    try:
        code, run_path = rw.execute_windows("创建蓝图 BP_Gun", planned={"source": "rules"},
                                            windows=[window("/Game/BP_Gun.BP_Gun")], echo=_quiet)
        assert code == 0, code
        run_id = Path(run_path).stem
        types = [json.loads(line).get("type") for line in (events_dir / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines()]
        assert "index_stale" in types, types
        data = json.loads(run_path.with_suffix(".json").read_text(encoding="utf-8"))
        assert "asset.bat" in str(data.get("index_hint") or ""), data.get("index_hint")
        assert "asset.bat" in run_path.read_text(encoding="utf-8")
        (events_dir / f"{run_id}.jsonl").unlink(missing_ok=True)

        code, run_path = rw.execute_windows("重建同名蓝图", planned={"source": "rules"},
                                            windows=[window("/Game/BP_Gun.BP_Gun（索引里已有同名蓝图，不重复新建）")],
                                            echo=_quiet)
        assert code == 0, code
        run_id = Path(run_path).stem
        types = [json.loads(line).get("type") for line in (events_dir / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines()]
        assert "index_stale" not in types, types
        data = json.loads(run_path.with_suffix(".json").read_text(encoding="utf-8"))
        assert not data.get("index_hint"), data.get("index_hint")
        (events_dir / f"{run_id}.jsonl").unlink(missing_ok=True)
    finally:
        run_task_mod.run_task = original


def check_repl_commands() -> None:
    """185550 的修复：REPL 认 :resume（含误敲的 --resume），--xxx 不再当任务句进规划者，
    无动作的规划者伪窗口不算可续接。不叫模型、不碰编辑器。"""
    import subprocess
    import tempfile

    from agent import run_windows as rw

    assert rw._resume_command(":resume") == "latest"
    assert rw._resume_command(":resume latest") == "latest"
    assert rw._resume_command("--resume latest") == "latest"        # 误敲的也认，不再当任务句
    assert rw._resume_command("resume latest") == "latest"          # 裸写法也认（190429 那次漏网的）
    assert rw._resume_command("resume 20261014-180852") == "20261014-180852"
    assert rw._resume_command("resume") == "latest"
    assert rw._resume_command(":resume 20261014-180852") == "20261014-180852"
    assert rw._resume_command(":r 180417") == "180417"
    assert rw._resume_command("编译工程") is None
    assert rw._resume_command("移动 Cube 到 x=100") is None
    assert rw._resume_command("恢复编译工程后重启服务 resume") is None   # resume 只在词首才算指令
    assert rw._resume_command("--plain") is None                    # 只拦 --resume，别的 --xxx 走提示

    with tempfile.TemporaryDirectory() as tmp_name:
        runs = Path(tmp_name) / "runs"
        runs.mkdir(parents=True)
        saved_runs = rw.RUNS_DIR
        rw.RUNS_DIR = runs
        try:
            # 无动作的规划者伪窗口（185550 形态）不算可续接
            (runs / "20261014-185550.json").write_text(json.dumps({
                "run_id": "20261014-185550",
                "windows": {"W1": {"id": "W1", "state": "paused", "action": ""}},
            }, ensure_ascii=False), encoding="utf-8")
            # 有动作的失败扇（180852 形态）算
            (runs / "20261014-180852.json").write_text(json.dumps({
                "run_id": "20261014-180852",
                "windows": {"W1": {"id": "W1", "state": "failed", "action": "edit_cpp"}},
            }, ensure_ascii=False), encoding="utf-8")
            assert rw._resumable_runs() == ["20261014-180852"], rw._resumable_runs()
        finally:
            rw.RUNS_DIR = saved_runs

    # 管道冒烟：误敲的 --plain 被拦下提示，不再进规划者
    root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, str(root / "agent" / "run_windows.py"), "--plain"],
        input="--plain\n:quit\n", capture_output=True, text=True, encoding="utf-8", timeout=120,
        cwd=str(root),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "不当任务执行" in proc.stdout, proc.stdout
    assert "没有写出窗口清单" not in proc.stdout


def main() -> int:
    check_gate()
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        runs = tmp / "runs"
        events_dir = tmp / "events"
        snaps = tmp / "snapshots"
        arch = tmp / "archive"
        src = tmp / "source"
        (src / "Private").mkdir(parents=True)
        (src / "Private" / "TestPawn.cpp").write_text("original body\n", encoding="utf-8")

        from agent import brief as brief_mod
        from agent import ledger as ledger_mod
        from agent import run_windows as rw
        from agent import snapshot as snapshot_mod

        saved = {
            "runs": rw.RUNS_DIR, "events": rw.EVENTS_DIR, "snaps": snapshot_mod.SNAPSHOTS_DIR,
            "archive": rw.ARCHIVE_DIR, "src": rw.UE_SOURCE_ROOT, "project": rw.UE_PROJECT_DIR,
            "snap_src": snapshot_mod.UE_SOURCE_ROOT,
            "briefs": brief_mod.BRIEFS_DIR, "ledger": ledger_mod.LEDGER_PATH,
            "titles": brief_mod._retrieval_titles,
            # 20261028：eval 必须封闭——本机 .env 开了 UE_SOLVER_ENABLED 后，零窗口范围
            # 会多出一扇 solve，固定序列断言（如 check_brief 的 set_pickable…）随环境漂移。
            # 补窗救援整体替身掉，与 check_solve_gap_fallback 同口径。
            "rescue": brief_mod._rescue_scope_windows,
        }
        prev_routes = os.environ.get("UE_LEARNED_ROUTES")
        os.environ["UE_LEARNED_ROUTES"] = str(tmp / "learned_routes.json")
        try:
            brief_mod._rescue_scope_windows = lambda scope, query, complete=None, titles=None: []
            rw.RUNS_DIR = runs
            rw.EVENTS_DIR = events_dir
            snapshot_mod.SNAPSHOTS_DIR = snaps
            snapshot_mod.UE_SOURCE_ROOT = src
            rw.ARCHIVE_DIR = arch
            rw.UE_SOURCE_ROOT = src
            rw.UE_PROJECT_DIR = tmp
            brief_mod.BRIEFS_DIR = tmp / "briefs"
            ledger_mod.LEDGER_PATH = tmp / "ledger.json"
            brief_mod._retrieval_titles = lambda query, k=3: ["BP_TestActor 蓝图", "ATestPawn 类"]

            check_events_and_state(runs, events_dir)
            check_resume(runs, events_dir)
            check_intent_layer(runs, events_dir)
            check_index_stale(events_dir)
            check_cycle_dangling(runs)
            check_snapshot_restore(src, snaps)
            check_parallel()
            check_artifacts()
            check_brief(tmp / "briefs", tmp / "ledger.json")
            check_matrix_ledger(tmp / "briefs", tmp / "ledger.json", arch, events_dir)
            check_widget_slots()
            check_unasked_compile()
            check_launch_only()
            check_board()
            check_rules_dechain()
            check_cpp_window_context()
            check_menu2_no_fake_success()
            check_stall_fixes()
            check_repl_commands()
        finally:
            rw.RUNS_DIR = saved["runs"]
            rw.EVENTS_DIR = saved["events"]
            snapshot_mod.SNAPSHOTS_DIR = saved["snaps"]
            snapshot_mod.UE_SOURCE_ROOT = saved["snap_src"]
            rw.ARCHIVE_DIR = saved["archive"]
            rw.UE_SOURCE_ROOT = saved["src"]
            rw.UE_PROJECT_DIR = saved["project"]
            brief_mod.BRIEFS_DIR = saved["briefs"]
            ledger_mod.LEDGER_PATH = saved["ledger"]
            brief_mod._retrieval_titles = saved["titles"]
            brief_mod._rescue_scope_windows = saved["rescue"]
            if prev_routes is None:
                os.environ.pop("UE_LEARNED_ROUTES", None)
            else:
                os.environ["UE_LEARNED_ROUTES"] = prev_routes
    print("upgrade ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
