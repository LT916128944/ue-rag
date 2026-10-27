# eval/check_integrate_verdict.py
"""T7 离线验收：交付全过不许被记假失败 —— 集成矩阵第六项「交付验收」四态 + 归档时序。

背景（Lead 已确认必修）：`_run_integrate` 原有的 5 项（重复请求幂等 / 拒答路径 / 延迟窗口 /
断线重连 / 未知态恢复）全是**运行期鲁棒性行为**。一个只有 launch_editor + create_level 的 run
即使条款全过，5 项仍全 None ⇒ checked 为空 ⇒ ok=False +「矩阵没有可验证的项，不算全过」
+ stop_reason=judge_failed + 整句「部分完成」+ 退出码 1 —— 真做成了却被记成失败（20261024-192858
同一族缺陷的镜像面）。

四态（照 task-7 描述）：
  a) 有任何条款 passed=False ⇒ ok=False（真失败要看得见）；
  b) 一条条款都没有（run 级与窗口级都没有）⇒ ok=None（不许把没验过的撑成通过）；
  c) 条款非空且全 passed、且不存在零窗口/整段没交付的范围 ⇒ ok=True；
  d) 条款非空且全 passed、但存在 capability_gap 范围 ⇒ ok=None
     （缺口由 contract_status=未完成 + 集成 verdict=no_evidence 点名；判 False 会把「没做」与
     「做坏了」混一起，也会吃掉 no_evidence 三态）。
条款只算「所属窗口都跑完（done）」的那些：一扇失败的窗口可能已经带着通过的 build 判词
（20261021-195220 空转扇），不许它给矩阵撑出 ok=True（check_harness 的老口径）。

归档时序：finalize 归档必须排在 state.save() 之后，归档里的 run json 要和 runs 目录最终态一致。
不连编辑器、不调模型：run_task 与 scope_windows 都是替身；RUNS/EVENTS/ARCHIVE 用临时目录隔离。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _quiet(*_args, **_kwargs):
    return None


def _two_scope_brief() -> dict:
    """A 启动编辑器 + B 新建关卡（create_level）：真做成了的一整句，无缺口。"""
    return {
        "query": "打开编辑器，新建一个关卡 L_NewTest2",
        "scopes": [
            {"id": "A", "title": "启动 UE 编辑器", "deliverables": ["编辑器进程在"],
             "pending_note": "", "depends_on": [], "external_dep": "",
             "windows": [{"id": "W1", "title": "启动 UE 编辑器", "action": "launch_editor",
                          "slots": {}, "depends_on": [], "max_retries": 1}]},
            {"id": "B", "title": "新建关卡 L_NewTest2", "deliverables": ["/Game 下有关卡 L_NewTest2",
                                                                  "该关卡已成为当前关卡"],
             "pending_note": "", "depends_on": ["A"], "external_dep": "",
             "windows": [{"id": "W2", "title": "新建关卡 L_NewTest2", "action": "create_level",
                          "slots": {"level": "L_NewTest2"}, "depends_on": [], "max_retries": 1}]},
        ],
        "integration": {"title": "累计集成、验证与交付", "deliverables": ["只读复验"]},
        "external_dependencies": [],
    }


def _gap_brief() -> dict:
    """192858 形状：A 启动编辑器（能过）+ B 零窗口（补不出 ⇒ 能力缺口）。"""
    return {
        "query": "打开编辑器，新建一个关卡，名字为L_NewTest2",
        "scopes": [
            {"id": "A", "title": "启动 UE 编辑器", "deliverables": ["编辑器进程在"],
             "pending_note": "", "depends_on": [], "external_dep": "",
             "windows": [{"id": "W1", "title": "启动 UE 编辑器", "action": "launch_editor",
                          "slots": {}, "depends_on": [], "max_retries": 1}]},
            {"id": "B", "title": "新建关卡 L_NewTest2",
             "deliverables": ["在 /Game 下创建关卡资产 L_NewTest2", "把 L_NewTest2 设为当前编辑关卡"],
             "pending_note": "动作表里没有建关卡的动作，需要在编辑器内完成；代码/资产尚无，缺创建与切换当前关卡的接线和验证",
             "depends_on": ["A"], "external_dep": "", "windows": []},
        ],
        "integration": {"title": "累计集成、验证与交付", "deliverables": ["只读复验"]},
        "external_dependencies": [],
    }


def _no_rescue():
    """让 T1 的 scope_windows 补不出窗口（不管它落没落地都能打桩）。返回恢复句柄。"""
    from agent import plan_windows

    had = hasattr(plan_windows, "scope_windows")
    saved = getattr(plan_windows, "scope_windows", None)
    plan_windows.scope_windows = lambda scope, query="": []
    return saved, had


def _restore_scope_windows(saved, had) -> None:
    from agent import plan_windows

    if had:
        plan_windows.scope_windows = saved
        return
    try:
        delattr(plan_windows, "scope_windows")
    except AttributeError:
        pass


def _run(brief: dict, fake, *, no_rescue: bool = False, windows=None, scopes=None):
    """假 execute 跑一遍；默认用 materialize_scopes 物化。返回 (code, path, run json)。"""
    from agent import run_task as run_task_mod
    from agent import run_windows as rw
    from agent.brief import materialize_scopes

    if windows is None:
        if no_rescue:
            handle = _no_rescue()
            try:
                windows, scopes = materialize_scopes(brief)
            finally:
                _restore_scope_windows(*handle)
        else:
            windows, scopes = materialize_scopes(brief)
    original = run_task_mod.run_task
    run_task_mod.run_task = fake
    try:
        code, path = rw.execute_windows(
            brief["query"], planned={"source": "brief"}, windows=windows, brief=brief,
            scopes=scopes, echo=_quiet,
        )
    finally:
        run_task_mod.run_task = original
    return code, path, json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))


def _judged(action_ok: bool):
    """所有动作都给一条 editor_assert 判词（真做成的那条路）。"""
    def fake(task, trace_path=None, thread_id=None, on_row=None):
        return {
            "id": task.get("id"), "ok": True, "summary": "做完了",
            "judge_kind": "editor_assert",
            "test": {"ok": True, "kind": "editor_assert", "summary": "读数与期望一致", "errors": []},
            "thread_id": thread_id,
        }

    return fake


def _unjudged(task, trace_path=None, thread_id=None, on_row=None):
    """窗口跑完但一条判词都不给：条款留在 passed=False。"""
    return {
        "id": task.get("id"), "ok": True, "summary": "做完了，但没跑裁判",
        "test": {"ok": True, "kind": "skip", "summary": "没有可跑的裁判", "errors": []},
        "thread_id": thread_id,
    }


def _acceptance(data: dict, integrate_id: str) -> dict:
    detail = data["windows"][integrate_id].get("detail") or {}
    matrix = detail.get("matrix") or []
    item = next((one for one in matrix if one.get("item") == "交付验收"), None)
    assert item is not None, ("矩阵里没有「交付验收」项", matrix)
    return {"item": item, "matrix": matrix, "detail": detail,
            "checked": [one for one in matrix if one.get("ok") is not None]}


def _integrate_id(data: dict) -> str:
    found = next((wid for wid, entry in data["windows"].items()
                  if str(entry.get("action")) == "integrate_verify"), None)
    assert found, ("run 里没有集成扇", list(data["windows"]))
    return str(found)


def _matrix_item(view: dict, name: str) -> dict:
    item = next((one for one in view["matrix"] if one.get("item") == name), None)
    assert item is not None, (f"矩阵里没有「{name}」项", view["matrix"])
    return item


def _flaky_launch():
    """第一次失败、第二次成功：让「延迟窗口」凑出 ok=True（T3 面一的诱因）。"""
    calls = {"n": 0}

    def fake(task, trace_path=None, thread_id=None, on_row=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"id": task.get("id"), "ok": False, "summary": "第一次没起来", "thread_id": thread_id}
        return {"id": task.get("id"), "ok": True, "summary": "起来了", "thread_id": thread_id,
                "test": {"ok": True, "kind": "editor_assert", "summary": "编辑器在", "errors": []}}

    return fake


def _create_level_fails():
    """create_level 真的失败（duplicate 撞名），别的窗口照常成功：T3 面二/失败扇的诱因。"""
    def fake(task, trace_path=None, thread_id=None, on_row=None):
        if str(task.get("action") or "") == "create_level":
            return {"id": task.get("id"), "ok": False,
                    "summary": "duplicate 失败：/Game/Maps/L_NewTest2 已存在",
                    "error": "duplicate 失败：/Game/Maps/L_NewTest2 已存在",
                    "thread_id": thread_id}
        return _judged(True)(task, thread_id=thread_id, on_row=on_row)

    return fake


def _paused_brief() -> dict:
    """A 启动编辑器 + B 新建关卡但关卡名缺失（暂停，不猜）：T3 面二的最小形状。"""
    return {
        "query": "打开编辑器，新建一个关卡",
        "scopes": [
            {"id": "A", "title": "启动 UE 编辑器", "deliverables": ["编辑器在"],
             "pending_note": "", "depends_on": [], "external_dep": "",
             "windows": [{"id": "W1", "title": "启动 UE 编辑器", "action": "launch_editor",
                          "slots": {}, "depends_on": [], "max_retries": 1}]},
            {"id": "B", "title": "新建关卡（名字待定）", "deliverables": ["关卡建好"],
             "pending_note": "", "depends_on": ["A"], "external_dep": "",
             "windows": [{"id": "W2", "title": "新建关卡", "action": "create_level",
                          "slots": {}, "depends_on": [], "max_retries": 1}]},
        ],
        "integration": {"title": "累计集成、验证与交付", "deliverables": ["只读复验"]},
        "external_dependencies": [],
    }


def check_c_all_pass_no_gap() -> None:
    """c) 条款全过 + 无缺口 ⇒ 交付验收 ok=True，集成 ok=True、「矩阵全部通过」、verdict=""。"""
    brief = _two_scope_brief()
    code, _path, data = _run(brief, _judged(True))
    integrate_id = next(wid for wid, e in data["windows"].items()
                        if str(e.get("action")) == "integrate_verify")
    view = _acceptance(data, integrate_id)
    assert view["item"]["ok"] is True, view["item"]
    assert view["checked"], ("checked 不该为空（真做成了不许记假失败）", view["matrix"])
    entry = data["windows"][integrate_id]
    assert entry["ok"] is True and entry["state"] == "done", entry
    assert entry["stop_reason"] == "ok", entry.get("stop_reason")
    assert "矩阵全部通过" in str(view["detail"].get("summary") or ""), view["detail"].get("summary")
    assert view["detail"].get("verdict") == "", view["detail"].get("verdict")
    assert code == 0 and data["status"] == "成功", (code, data["status"])


def check_a_clause_failed() -> None:
    """a) 有条款 passed=False ⇒ 交付验收 ok=False，集成「矩阵未全过」。"""
    brief = _two_scope_brief()
    code, _path, data = _run(brief, _unjudged)
    integrate_id = next(wid for wid, e in data["windows"].items()
                        if str(e.get("action")) == "integrate_verify")
    view = _acceptance(data, integrate_id)
    assert view["item"]["ok"] is False, view["item"]
    entry = data["windows"][integrate_id]
    assert entry["ok"] is False, entry
    assert "矩阵未全过" in str(view["detail"].get("summary") or ""), view["detail"].get("summary")
    assert code == 1, code


def check_d_gap_still_no_evidence() -> None:
    """d) 192858 形状（有缺口范围）⇒ checked 为空、verdict=no_evidence、label 含能力缺口。"""
    brief = _gap_brief()
    code, _path, data = _run(brief, _judged(True), no_rescue=True)
    integrate_id = next(wid for wid, e in data["windows"].items()
                        if str(e.get("action")) == "integrate_verify")
    view = _acceptance(data, integrate_id)
    assert view["item"]["ok"] is None, view["item"]
    assert "有范围没交付" in str(view["item"].get("note") or ""), view["item"]
    assert view["checked"] == [], ("缺口形状必须 checked 为空", view["matrix"])
    entry = data["windows"][integrate_id]
    assert entry["stop_reason"] == "no_evidence", entry.get("stop_reason")
    assert view["detail"].get("verdict") == "no_evidence", view["detail"].get("verdict")
    summary = str(view["detail"].get("summary") or "")
    assert "能力缺口" in summary and "B" in summary, summary
    assert data["status"] == "未完成", data["status"]
    assert code == 1, code


def check_b_no_clause_guard() -> None:
    """b) 一条条款都没有且无缺口 ⇒ 交付验收 ok=None、checked 为空、ok=False（不许撑成功）。"""
    from agent.plan_windows import materialize

    brief = {"query": "只跑集成扇", "scopes": [], "integration": {}, "external_dependencies": []}
    integrate = materialize("integrate_verify", {}, title="累计集成、验证与交付",
                            wid="W1", depends=[], retries=1)
    code, _path, data = _run(brief, _unjudged, windows=[integrate], scopes=[])
    view = _acceptance(data, "W1")
    assert view["item"]["ok"] is None, view["item"]
    assert view["checked"] == [], ("没有条款时不许有 checked 项", view["matrix"])
    assert data["windows"]["W1"]["ok"] is False, data["windows"]["W1"]
    assert code == 1, code
    # 反例守卫：run 级没有条款时回落窗口级；窗口级也空才叫「一条都没有」
    from agent.harness import build_criteria

    assert build_criteria(criteria=None, windows=[integrate]) == [], "集成扇本身不该产条款"


def check_archive_timing() -> None:
    """归档时序：archive/<run_id>.json 的 status 与 runs 最终态一致；证据合并语义不变。"""
    from agent import run_windows as rw

    brief = _two_scope_brief()
    code, path, data = _run(brief, _judged(True))
    run_id = path.stem
    archived = json.loads((rw.ARCHIVE_DIR / run_id / f"{run_id}.json").read_text(encoding="utf-8"))
    assert archived["status"] == data["status"], ("归档副本还停在旧状态", archived["status"], data["status"])
    assert archived["status"] != "running", archived["status"]
    assert (archived.get("integration") or {}).get("status") == data["integration"]["status"], archived
    deliverables = json.loads(
        (rw.ARCHIVE_DIR / run_id / "deliverables.json").read_text(encoding="utf-8")
    )
    integrate_id = next(wid for wid, e in data["windows"].items()
                        if str(e.get("action")) == "integrate_verify")
    run_matrix = (data["windows"][integrate_id].get("detail") or {}).get("matrix") or []
    assert len(deliverables["matrix"]) == len(run_matrix), ("归档丢了矩阵", deliverables, run_matrix)
    assert deliverables.get("written_by") == "finalize", deliverables
    assert (rw.ARCHIVE_DIR / run_id / f"{run_id}.md").is_file(), "最终 md 要进归档"
    assert code == 0, code


def check_gap_with_retry_veto() -> None:
    """T9-a：有缺口 + 「延迟窗口」凑出 True ⇒ 集成必须否决，不许报「矩阵全部通过」。

    T3 证伪的假通过面：`checked = [矩阵项 ok is not None]` + `all(checked)`，而「延迟窗口」
    只要窗口有终态（失败也算）就给 True、有缺口时「交付验收」按设计不投票 ⇒ 一项 True 就能
    凑出「矩阵全部通过」，即使另一个范围整段没交付。"""
    brief = _gap_brief()
    code, _path, data = _run(brief, _flaky_launch(), no_rescue=True)
    integrate_id = _integrate_id(data)
    view = _acceptance(data, integrate_id)
    retried = _matrix_item(view, "延迟窗口")
    assert retried.get("ok") is True, ("前提：这一项要凑出 True 才有假通过可谈", view["matrix"])
    assert view["item"]["ok"] is None, ("交付验收按设计不投票", view["item"])
    entry = data["windows"][integrate_id]
    assert entry["ok"] is False and entry["state"] == "failed", entry
    assert entry["stop_reason"] == "no_evidence", entry.get("stop_reason")
    summary = str(view["detail"].get("summary") or "")
    assert "矩阵全部通过" not in summary, ("假通过：缺口被延迟窗口凑成了全过", summary)
    assert "矩阵未全过" in summary and "能力缺口" in summary, summary
    assert view["detail"].get("verdict") == "no_evidence", view["detail"].get("verdict")
    assert data["status"] == "未完成", data["status"]
    assert code == 1, code


def check_failed_window_veto() -> None:
    """T9-b：有真实失败扇（create_level duplicate 失败）+ 无缺口 ⇒ 集成否决并点名该扇。"""
    brief = _two_scope_brief()
    code, _path, data = _run(brief, _create_level_fails())
    integrate_id = _integrate_id(data)
    view = _acceptance(data, integrate_id)
    failed_ids = [
        wid for wid, entry in data["windows"].items()
        if entry.get("state") == "failed" and wid != integrate_id
    ]
    assert failed_ids, ("前提：要有一扇真失败", {k: v.get("state") for k, v in data["windows"].items()})
    entry = data["windows"][integrate_id]
    assert entry["ok"] is False, entry
    summary = str(view["detail"].get("summary") or "")
    assert "矩阵全部通过" not in summary, summary
    assert "有窗口失败" in summary, summary
    for wid in failed_ids:
        assert wid in summary, ("失败扇要按 id 点名", wid, summary)
    assert view["detail"].get("verdict") == "", ("没有缺口就不该记 no_evidence", view["detail"].get("verdict"))
    assert code == 1, code


def check_paused_window_veto() -> None:
    """T9-c：有暂停扇 ⇒ 集成否决；收尾 note 必须点名暂停的扇（不许只写「完成 W1」）。"""
    brief = _paused_brief()
    code, _path, data = _run(brief, _judged(True))
    integrate_id = _integrate_id(data)
    view = _acceptance(data, integrate_id)
    paused = [wid for wid, entry in data["windows"].items() if entry.get("state") == "paused"]
    assert paused, ("前提：要有一扇暂停", {k: v.get("state") for k, v in data["windows"].items()})
    # 「拒答路径」这一项本身仍记 True：那是「拒答行为正确」，不是「交付完成」
    refused = _matrix_item(view, "拒答路径")
    assert refused.get("ok") is True, ("拒答行为正确，这一项仍记 True", view["matrix"])
    entry = data["windows"][integrate_id]
    assert entry["ok"] is False, entry
    summary = str(view["detail"].get("summary") or "")
    assert "矩阵全部通过" not in summary and "有窗口暂停" in summary, summary
    note = str(data["integration"].get("summary") or "")
    for wid in paused:
        assert wid in summary, ("集成说明要按 id 点名暂停的扇", wid, summary)
        assert wid in note, ("收尾 note 没点名暂停的扇", wid, note)
    assert "完成 W1" in note, ("原有「完成 W1」这类信息要保留", note)
    assert code == 1, code


def check_clean_run_not_vetoed() -> None:
    """T9-d 反例守卫：干净 run（无缺口/无失败/无暂停、条款全过）仍必须「矩阵全部通过」。"""
    brief = _two_scope_brief()
    code, _path, data = _run(brief, _judged(True))
    integrate_id = _integrate_id(data)
    view = _acceptance(data, integrate_id)
    entry = data["windows"][integrate_id]
    assert entry["ok"] is True and entry["state"] == "done", entry
    summary = str(view["detail"].get("summary") or "")
    assert "矩阵全部通过" in summary, summary
    assert "矩阵未全过" not in summary and "能力缺口" not in summary, summary
    assert view["detail"].get("verdict") == "", view["detail"].get("verdict")
    assert data["status"] == "成功" and code == 0, (data["status"], code)


def _stuck_state(run_id: str, *, stuck: bool):
    """纯假 RunState：W1 done，另一扇按 stuck 决定停在 running 还是 done，集成扇自己是 running。"""
    from agent import run_windows as rw

    state = rw._RunState(run_id, "中断形态", "brief", [])
    state.data["windows"] = {
        "W1": {"id": "W1", "action": "launch_editor", "state": "done", "ok": True,
               "result": {"id": "W1", "ok": True, "summary": "ok"}, "window": {}, "detail": {}},
        "W2": {"id": "W2", "action": "create_level", "state": "running" if stuck else "done",
               "ok": True, "result": {"id": "W2", "ok": True, "summary": "建好了"}, "window": {},
               "detail": {}},
        "W3": {"id": "W3", "action": "integrate_verify", "state": "running", "ok": None,
               "result": None, "window": {}, "detail": {}},
    }
    state.data["criteria"] = [
        {"id": "c-editor_assert", "evidence_kind": "editor_assert", "passed": True, "windows": ["W1"]},
    ]
    return state


def check_stuck_window_marks_false() -> None:
    """T10-A：矩阵第 5 项「未知态恢复」的语义不许反转。

    有窗口卡在 running/retry/revising/interrupted ⇒ 这一项必须是 False（它查的就是「每扇都有
    明确终态」）；写成 True 会反过来占一个 checked 名额去撑「矩阵全部通过」。
    没有卡住的 ⇒ None（不适用），不占 checked 名额。"""
    from agent import run_windows as rw

    state = _stuck_state("20260909-160001", stuck=True)
    out = rw._run_integrate(state.data["windows"]["W3"], state, "q", {}, rw._EventBus("20260909-160001"))
    item = next((one for one in out["matrix"] if one.get("item") == "未知态恢复"), None)
    assert item is not None, ("矩阵里没有「未知态恢复」项", out["matrix"])
    assert "W2=running" in str(item.get("note") or ""), ("前提：这一项真的看见了卡住的窗口", item)
    assert item.get("ok") is False, ("有窗口悬在半路，这一项必须是 False（语义反转会变成假通过）", item)
    summary = str(out.get("summary") or "")
    assert "矩阵全部通过" not in summary, ("有窗口悬在半路，不许报「矩阵全部通过」", summary)
    assert out.get("ok") is False, ("有窗口悬在半路，集成扇不许 ok=True", out.get("ok"))

    clean = _stuck_state("20260909-160002", stuck=False)
    out2 = rw._run_integrate(clean.data["windows"]["W3"], clean, "q", {}, rw._EventBus("20260909-160002"))
    item2 = next((one for one in out2["matrix"] if one.get("item") == "未知态恢复"), None)
    assert item2.get("ok") is None, ("没有卡住的窗口时这一项是不适用（None），不占 checked", item2)
    assert "矩阵全部通过" in str(out2.get("summary") or ""), out2.get("summary")


def check_external_scope_wording() -> None:
    """T10-B：等外部的范围不许被说成「只记待办」，label 也不许不加限定地说「全部通过」。

    红线：等外部**不进**交付否决（run 级部分完成 / exit 1 的既有口径是对的），只修话术与 label。"""
    from agent.harness import scope_marks

    # (1) 判定口径：零窗口 + 等外部 必须说「等待外部 M-GO」，不许说「只记待办」
    marks = scope_marks([{
        "id": "C", "window_ids": [], "external_dep": "M-GO", "criteria": [],
        "deliverables": ["接口恢复后核对"], "window_states": [],
    }])
    reason = str((marks[0] if marks else {}).get("reason") or "")
    assert "等待外部 M-GO" in reason, ("零窗口 + 等外部应当说「等待外部 M-GO」，现在是", reason)
    assert "只记待办" not in reason, ("等外部不是「只记待办」", reason)
    assert marks[0].get("ok") is False, marks[0]
    # 有窗口 + 等外部仍说「等待外部」（顺序改动不许把这条弄丢）
    with_windows = scope_marks([{
        "id": "C", "window_ids": ["W1"], "external_dep": "M-GO", "criteria": [],
        "deliverables": ["x"], "window_states": ["waiting_external"],
    }])
    assert "等待外部 M-GO" in str(with_windows[0].get("reason") or ""), with_windows[0]
    # 反例守卫：没有 external_dep 的零窗口范围仍走「没有可执行的窗口，只记待办」
    plain = scope_marks([{"id": "D", "window_ids": [], "external_dep": "", "criteria": [],
                          "deliverables": ["y"], "window_states": []}])
    assert "只记待办" in str(plain[0].get("reason") or ""), plain[0]

    # (2) 端到端：等外部的 run 若 label 出现「矩阵全部通过」，必须同时带等外部范围的限定说明
    brief = {
        "query": "打开编辑器，新建一个关卡 L_NewTest2，再等 M-GO 接口",
        "scopes": [
            {"id": "A", "title": "启动 UE 编辑器", "deliverables": ["编辑器进程在"],
             "pending_note": "", "depends_on": [], "external_dep": "",
             "windows": [{"id": "W1", "title": "启动", "action": "launch_editor", "slots": {},
                          "depends_on": [], "max_retries": 1}]},
            {"id": "B", "title": "新建关卡 L_NewTest2", "deliverables": ["关卡在"],
             "pending_note": "", "depends_on": ["A"], "external_dep": "",
             "windows": [{"id": "W2", "title": "建关卡", "action": "create_level",
                          "slots": {"level": "L_NewTest2"}, "depends_on": [], "max_retries": 1}]},
            {"id": "C", "title": "等 M-GO 接口", "deliverables": ["接口恢复后核对"],
             "pending_note": "", "depends_on": ["B"], "external_dep": "M-GO", "windows": []},
        ],
        "integration": {"title": "累计集成、验证与交付", "deliverables": ["只读复验"]},
        "external_dependencies": [{"name": "M-GO", "provides": "接口", "status": "未恢复"}],
    }
    code, _path, data = _run(brief, _judged(True))
    integrate_id = _integrate_id(data)
    view = _acceptance(data, integrate_id)
    label = str(view["detail"].get("summary") or "")
    note = str(data["integration"].get("summary") or "")
    assert "等待外部" in note, ("收尾要点名等外部的范围", note)
    # 等外部不是 veto：run 仍是部分完成 + exit 1（既有正确口径，不许变成失败）
    assert code == 1 and data["status"] == "部分完成", (code, data["status"])
    if "矩阵全部通过" in label:
        assert "另有范围" in label or "未完成" in label or "等外部" in label, (
            "收尾 note 说没完成，集成 label 却不加限定地说「矩阵全部通过」", label)
        assert "C" in label, ("限定说明要点名等外部的范围 id", label)


def main() -> int:
    from agent import brief as brief_mod
    from agent import ledger as ledger_mod
    from agent import run_windows as rw
    from agent import snapshot as snapshot_mod

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        saved = {
            "runs": rw.RUNS_DIR, "events": rw.EVENTS_DIR, "archive": rw.ARCHIVE_DIR,
            "src": rw.UE_SOURCE_ROOT, "project": rw.UE_PROJECT_DIR,
            "snap_src": snapshot_mod.UE_SOURCE_ROOT, "snaps": snapshot_mod.SNAPSHOTS_DIR,
            "briefs": brief_mod.BRIEFS_DIR, "ledger": ledger_mod.LEDGER_PATH,
        }
        prev_candidates = os.environ.get("UE_ACTION_CANDIDATES")
        prev_routes = os.environ.get("UE_LEARNED_ROUTES")
        os.environ["UE_ACTION_CANDIDATES"] = str(tmp / "action_candidates.json")
        os.environ["UE_LEARNED_ROUTES"] = str(tmp / "learned_routes.json")
        try:
            rw.RUNS_DIR = tmp / "runs"
            rw.EVENTS_DIR = tmp / "events"
            rw.ARCHIVE_DIR = tmp / "archive"
            rw.UE_SOURCE_ROOT = tmp / "source"
            rw.UE_PROJECT_DIR = tmp
            snapshot_mod.SNAPSHOTS_DIR = tmp / "snapshots"
            snapshot_mod.UE_SOURCE_ROOT = tmp / "source"
            brief_mod.BRIEFS_DIR = tmp / "briefs"
            ledger_mod.LEDGER_PATH = tmp / "ledger.json"

            check_c_all_pass_no_gap()
            check_a_clause_failed()
            check_d_gap_still_no_evidence()
            check_b_no_clause_guard()
            check_archive_timing()
            check_gap_with_retry_veto()
            check_failed_window_veto()
            check_paused_window_veto()
            check_clean_run_not_vetoed()
            check_stuck_window_marks_false()
            check_external_scope_wording()
        finally:
            rw.RUNS_DIR = saved["runs"]
            rw.EVENTS_DIR = saved["events"]
            rw.ARCHIVE_DIR = saved["archive"]
            rw.UE_SOURCE_ROOT = saved["src"]
            rw.UE_PROJECT_DIR = saved["project"]
            snapshot_mod.UE_SOURCE_ROOT = saved["snap_src"]
            snapshot_mod.SNAPSHOTS_DIR = saved["snaps"]
            brief_mod.BRIEFS_DIR = saved["briefs"]
            ledger_mod.LEDGER_PATH = saved["ledger"]
            for key, value in (("UE_ACTION_CANDIDATES", prev_candidates),
                               ("UE_LEARNED_ROUTES", prev_routes)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    print("integrate verdict ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())