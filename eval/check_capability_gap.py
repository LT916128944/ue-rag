# eval/check_capability_gap.py
"""T2 离线验收（20261024-192858）：零窗口范围不许静默降级成「只记录待办」。

真事：任务书 data/briefs/20261024-192830-L_NewTest2-FPS.json 的范围 B（新建关卡 L_NewTest2）、
C（在关卡里实现简易 FPS 玩法）交付清单非空、windows 为空。旧逻辑里 validate_brief 只写一句
「没有可执行的窗口，只记录待办，不进执行。」放行，人审门照样显示「审阅 1：通过」，
materialize_scopes 只物化出 W1 启动编辑器 + W2 集成扇；集成矩阵 checked 为空 → ok=False、
stop_reason 记笼统的 judge_failed，收尾 1545 行先记「部分完成」，于是 B/C 的缺口一个字都没落地，
report/台账里读起来像「只差集成」。

本用例钉四件事：
1) 真实任务书走 materialize_scopes：B/C 要么被 agent.plan_windows.scope_windows 补出窗口，
   要么带 capability_gap 进 scopes_meta；不允许「既没窗口也没缺口」的静默。
2) 人审门（render_brief_md 的 md 表 + 人审门表格）显式出现「能力缺口」，旧任务书文件里
   存的「只记录待办」也被换掉，不再是一句无后果的降级。
3) 假 execute 构造「只有 launch_editor 成功 + 集成扇」：综合状态是「未完成」，note 点名 B/C，
   集成 verdict=no_evidence、集成扇 stop_reason=no_evidence（不是 judge_failed）。
4) 正常范围（窗口跑到 done）仍记「成功」，防回归。

不连编辑器、不调模型、不进 PIE：run_task 与 scope_windows 都是替身，目录用临时目录隔离。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REAL_BRIEF = Path(__file__).resolve().parent.parent / "data" / "briefs" / "20261024-192830-L_NewTest2-FPS.json"


def _quiet(*_args, **_kwargs):
    return None


def _load_real_brief() -> tuple[dict, dict]:
    """真实任务书原文（含旧 block_note）和走完 validate_brief 的那份。
    data/briefs 是 gitignore 的，CI 上可能没有这份文件：这时用同形状的内联替身
    （query/B/C 的 pending_note 与真实文件一字不差，B/C 仍是零窗口 + 旧 block_note），
    断言照样跑，不假装验过真实件。"""
    from agent.brief import validate_brief

    if REAL_BRIEF.is_file():
        raw = json.loads(REAL_BRIEF.read_text(encoding="utf-8"))
    else:
        raw = _inline_brief()
    return raw, validate_brief(json.loads(json.dumps(raw, ensure_ascii=False)), raw["query"])


def _inline_brief() -> dict:
    """真实任务书 20261024-192830 的同形状替身（只在内联文件不在时用）。"""
    from agent.brief import _LEGACY_ZERO_WINDOW_NOTE

    return {
        "query": "打开编辑器，新建一个关卡，名字为L_NewTest2，在这个关卡实现逻辑如下：需要实现简易的FPS射击游戏，内容你自由发挥",
        "scopes": [
            {"id": "A", "title": "启动 UE 编辑器并等待 MCP 端口连通",
             "deliverables": ["UE 编辑器进程启动完成", "MCP 端口可连通，后续动作可下发"],
             "pending_note": "", "depends_on": [], "external_dep": "", "estimate_hours": [0.1, 0.5],
             "windows": [{"id": "W1", "title": "启动 UE 编辑器", "action": "launch_editor",
                          "slots": {}, "depends_on": [], "max_retries": 1}]},
            {"id": "B", "title": "新建关卡 L_NewTest2",
             "deliverables": ["在 /Game 下创建关卡资产 L_NewTest2", "把 L_NewTest2 设为当前编辑关卡"],
             "pending_note": "动作表里没有建关卡的动作，需要在编辑器内完成；代码/资产尚无，缺创建与切换当前关卡的接线和验证",
             "depends_on": ["A"], "external_dep": "", "estimate_hours": [2.0, 8.0],
             "windows": [], "block_note": _LEGACY_ZERO_WINDOW_NOTE},
            {"id": "C", "title": "在 L_NewTest2 内实现简易 FPS 射击玩法",
             "deliverables": ["玩家角色：移动、视角、开火输入", "射击命中判定与可击倒/可拾取目标",
                              "计分或结算反馈（HUD/日志择一）", "把上述内容放进 L_NewTest2 并完成接线",
                              "在关卡内跑通一次射击到命中的验证"],
             "pending_note": "原句只说“自由发挥”，未点名具体蓝图或资产；实现清单、资产命名与接线都还没定，也没验证",
             "depends_on": ["B"], "external_dep": "", "estimate_hours": [5.0, 20.0],
             "windows": [], "block_note": _LEGACY_ZERO_WINDOW_NOTE},
        ],
        "integration": {"title": "累计集成、验证与交付",
                        "deliverables": ["按 A→B→C 的顺序串起，确认编辑器已连通后再操作关卡",
                                         "在 L_NewTest2 内完整跑一次：启动→移动→射击→命中反馈",
                                         "对修改做一次只读复验", "记录未接线/未验证的遗留条目"],
                        "estimate_hours": [2.0, 8.0]},
        "external_dependencies": [],
    }


def _patch_scope_windows(fake):
    """把 T1 的 scope_windows 换成替身（没落地时也要能打桩）。返回恢复用句柄。"""
    from agent import plan_windows

    had = hasattr(plan_windows, "scope_windows")
    saved = getattr(plan_windows, "scope_windows", None)
    plan_windows.scope_windows = fake
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


def check_gate_wording() -> None:
    """人审门必须在 B/C 上写出「能力缺口」，旧任务书原文也一样。md 和表格都要有。"""
    import contextlib
    import io

    from agent.brief import _print_brief_pretty, _zero_window_scope, capability_gap_note, render_brief_md

    raw, brief = _load_real_brief()
    for view, label in ((brief, "validate 后"), (raw, "旧任务书原文")):
        md = render_brief_md(view)
        assert "能力缺口" in md, (label, md)
        assert "只记录待办" not in md, (label, md)
        for sid in ("B", "C"):
            scope = next(item for item in view["scopes"] if item["id"] == sid)
            assert _zero_window_scope(scope) is True, (label, sid, scope)
            assert "能力缺口" in capability_gap_note(scope), (label, sid)
    # 人审门表格（rich）也要显式出现，而且要在截断之前：窄列里 rich 会截到 20 字左右
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _print_brief_pretty(brief, printer=_quiet)
    table = buf.getvalue()
    assert "能力缺口" in table, table
    assert "只记录待办" not in table, table
    # 有窗口的范围和集成行不许被误标
    a_scope = next(item for item in brief["scopes"] if item["id"] == "A")
    assert _zero_window_scope(a_scope) is False, a_scope
    assert _zero_window_scope(brief["integration"]) is False, brief["integration"]


def check_rescue_paths() -> None:
    """补窗补不出来 → capability_gap 进 scopes_meta；补得出来 → 拿到窗口、不标缺口。"""
    from agent.brief import materialize_scopes
    from agent.catalog import load_candidates
    from agent.harness import contract_status

    _raw, brief = _load_real_brief()

    # (a) 补不出来：B/C 结构化能力缺口 + 候选表只记真能提名的动作
    handle = _patch_scope_windows(lambda scope, query="": [])
    try:
        windows, meta = materialize_scopes(brief)
    finally:
        _restore_scope_windows(*handle)
    assert [win["id"] for win in windows] == ["W1", "W2"], windows
    assert [win["action"] for win in windows] == ["launch_editor", "integrate_verify"], windows
    by_id = {item["id"]: item for item in meta}
    for sid in ("B", "C"):
        assert by_id[sid]["window_ids"] == [], by_id[sid]
        gap = by_id[sid].get("capability_gap")
        assert isinstance(gap, dict) and gap, ("缺口没进 scopes_meta", sid, by_id[sid])
        assert gap.get("deliverables"), (sid, gap)
        assert "能力缺口" in str(gap.get("reason") or ""), (sid, gap)
    assert by_id["B"]["capability_gap"].get("missing_actions") == ["建关卡"], by_id["B"]
    assert by_id["C"]["capability_gap"].get("missing_actions") == [], "抠不到动作名就不许编"
    names = [str(item.get("name") or "") for item in load_candidates()]
    assert "建关卡" in names, ("能提名的动作要进候选表", names)
    assert all("自由发挥" not in name for name in names), ("不许拿原话片段瞎编动作名", names)

    # 人工拼的零窗口范围（没有 capability_gap 字段）保持老口径：check_harness 那条老用例
    assert contract_status([], "成功", [{"id": "B", "window_ids": [], "criteria": []}]) == "部分完成"

    # (b) 补得出来：返回的 SBW1 当本范围声明的窗口，继续走 id 重映射，不标缺口
    seen: list[str] = []

    def fake_scope_windows(scope, query=""):
        seen.append(str(scope.get("id") or ""))
        return [{"id": "SBW1", "title": "补窗", "action": "launch_editor", "slots": {},
                 "depends_on": [], "max_retries": 1}]

    handle = _patch_scope_windows(fake_scope_windows)
    try:
        windows2, meta2 = materialize_scopes(brief)
    finally:
        _restore_scope_windows(*handle)
    assert seen == ["B", "C"], seen
    assert [win["action"] for win in windows2] == [
        "launch_editor", "launch_editor", "launch_editor", "integrate_verify"
    ], windows2
    assert [win["id"] for win in windows2] == ["W1", "W2", "W3", "W4"], windows2
    by2 = {item["id"]: item for item in meta2}
    for sid in ("B", "C"):
        assert by2[sid]["window_ids"], by2[sid]
        assert not by2[sid].get("capability_gap"), by2[sid]
    # 范围级依赖照旧换算：B 的第一扇接 A 的最后一扇，C 的第一扇接 B 的最后一扇
    assert windows2[1]["depends_on"] == ["W1"], windows2[1]
    assert windows2[2]["depends_on"] == ["W2"], windows2[2]
    assert windows2[3]["depends_on"] == ["W1", "W2", "W3"], windows2[3]

    # (c) 不打桩：T1 落地了就用它，没落地就走缺口。两种都行，就是不许静默
    windows3, meta3 = materialize_scopes(brief)
    by3 = {item["id"]: item for item in meta3}
    for sid in ("B", "C"):
        assert by3[sid]["window_ids"] or by3[sid].get("capability_gap"), (
            f"范围 {sid} 既没补出窗口也没记能力缺口：又静默了", by3[sid]
        )
    assert len(windows3) >= 2, windows3


def _fake_launch_editor(calls: list) -> object:
    def fake(task, trace_path=None, thread_id=None, on_row=None):
        calls.append(str(task.get("action") or ""))
        return {
            "id": task.get("id"), "ok": True,
            "summary": "已启动编辑器，MCP 已连通。未编译、未改场景、未保存、未进 PIE。",
            "test": {"ok": True, "kind": "skip", "summary": "启动扇没有验证 spec", "errors": []},
            "thread_id": thread_id,
        }

    return fake


def _run_fake(query: str, windows: list, scopes: list, brief: dict, fake) -> tuple[int, Path, list]:
    from agent import run_task as run_task_mod
    from agent import run_windows as rw

    events: list[dict] = []
    original = run_task_mod.run_task
    run_task_mod.run_task = fake
    try:
        code, path = rw.execute_windows(
            query, planned={"source": "brief"}, windows=windows, brief=brief, scopes=scopes,
            echo=_quiet, emit=lambda event: events.append(event),
        )
    finally:
        run_task_mod.run_task = original
    return code, path, events


def check_no_evidence_run() -> None:
    """只有 launch_editor 成功 + 集成扇：整句「未完成」，集成 no_evidence，note 点名 B/C。"""
    from agent.brief import materialize_scopes

    raw, brief = _load_real_brief()
    handle = _patch_scope_windows(lambda scope, query="": [])
    try:
        windows, scopes = materialize_scopes(brief)
    finally:
        _restore_scope_windows(*handle)

    calls: list[str] = []
    code, path, events = _run_fake(raw["query"], windows, scopes, brief, _fake_launch_editor(calls))
    assert code == 1, "缺口整句不能返回 0"
    assert calls == ["launch_editor"], calls

    data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert data["status"] == "未完成", data["status"]
    integrated = data["integration"]
    assert integrated["ok"] is False and integrated["status"] == "未完成", integrated
    note = str(integrated.get("summary") or "")
    assert "能力缺口" in note, note
    for sid in ("B", "C"):
        assert sid in note, (sid, note)

    text = path.read_text(encoding="utf-8")
    assert "- 状态：未完成" in text, text
    assert "未完成（能力缺口）" in text, text

    integrate_id = windows[-1]["id"]
    entry = data["windows"][integrate_id]
    assert entry["state"] == "failed" and entry["ok"] is False, entry
    assert entry["stop_reason"] == "no_evidence", entry.get("stop_reason")
    detail = entry.get("detail") or {}
    assert detail.get("verdict") == "no_evidence", detail.get("verdict")
    matrix = detail.get("matrix") or []
    assert matrix and all(item.get("ok") is None for item in matrix), matrix
    summary_text = str(detail.get("summary") or "")
    assert "能力缺口" in summary_text, summary_text
    for sid in ("B", "C"):
        assert sid in summary_text, (sid, summary_text)

    run_end = [event for event in events if event.get("type") == "run_end"]
    assert run_end, events
    assert run_end[-1].get("stop_reason") == "no_evidence", run_end[-1]
    assert run_end[-1].get("status") == "未完成", run_end[-1]


def check_normal_scope_success() -> None:
    """一条正常范围跑到 done 仍记「成功」：别把成功路径弄坏。"""
    from agent.catalog import INTEGRATE_ACTION
    from agent.brief import materialize_scopes

    brief = {
        "query": "只读裁决 BP_TestActor 能否拾取",
        "scopes": [{
            "id": "A", "title": "只读裁决", "deliverables": ["裁决能否拾取"], "pending_note": "",
            "depends_on": [], "external_dep": "",
            "windows": [{"id": "W1", "title": "裁决", "action": "judge_pickup",
                         "slots": {"target": "BP_TestActor", "picker": "BP_TestPawn"},
                         "depends_on": [], "max_retries": 1}],
        }],
        "integration": {"title": "累计集成、验证与交付", "deliverables": ["只读复验"]},
        "external_dependencies": [],
    }
    windows, scopes = materialize_scopes(brief)
    assert [win["action"] for win in windows] == ["judge_pickup", INTEGRATE_ACTION], windows
    assert scopes[0]["window_ids"] == ["W1"], scopes[0]

    def fake(task, trace_path=None, thread_id=None, on_row=None):
        return {
            "id": task.get("id"), "ok": True, "summary": "唯一命中，距离 90 uu",
            "judge": {"ok": True, "distance": 90.0},
            "test": {"ok": True, "kind": "editor_assert", "summary": "唯一命中", "errors": []},
            "thread_id": thread_id,
        }

    code, path, events = _run_fake(brief["query"], windows, scopes, brief, fake)
    assert code == 0, "正常范围应当成功"
    data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert data["status"] == "成功", data["status"]
    assert data["integration"]["status"] == "成功", data["integration"]
    assert not (data.get("scopes") or [{}])[0].get("capability_gap"), data.get("scopes")
    integrate_id = windows[-1]["id"]
    entry = data["windows"][integrate_id]
    assert entry["state"] == "done" and entry["ok"] is True, entry
    assert entry["stop_reason"] == "ok", entry.get("stop_reason")
    assert (entry.get("detail") or {}).get("verdict") == "", entry.get("detail")
    assert "no_evidence" not in str(entry.get("detail", {}).get("summary") or "")


def check_real_rescue_mixed_run() -> None:
    """T1 的 scope_windows 真落地后：补出窗口的范围与仍是缺口的范围混在一起时，
    整句照样是「未完成」、note 点名缺口范围、集成 no_evidence。补全了就不适用（返回）。"""
    from agent.brief import materialize_scopes

    raw, brief = _load_real_brief()
    windows, scopes = materialize_scopes(brief)
    gaps = [
        str(scope.get("id") or "")
        for scope in scopes
        if isinstance(scope.get("capability_gap"), dict) and scope.get("capability_gap")
    ]
    if not gaps:
        return
    calls: list[str] = []
    code, path, events = _run_fake(raw["query"], windows, scopes, brief, _fake_launch_editor(calls))
    assert code == 1, "有缺口就不许返回 0"
    data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert data["status"] == "未完成", data["status"]
    note = str(data["integration"].get("summary") or "")
    assert "能力缺口" in note, note
    for sid in gaps:
        assert sid in note, (sid, note)
    integrate_id = windows[-1]["id"]
    entry = data["windows"][integrate_id]
    assert entry["ok"] is False and entry["stop_reason"] == "no_evidence", entry.get("stop_reason")
    assert (entry.get("detail") or {}).get("verdict") == "no_evidence", entry.get("detail")


def check_create_level_binding() -> None:
    """T6：create_level 的裁判口径必须绑得上。漏了 _KIND_BY_ACTION 的映射时，
    build_criteria 产不出条款、_window_ids_for 也绑不上，一个真建成关卡的 run 条款数会是 0，
    集成矩阵 checked 为空 ⇒ 又变成「矩阵没有可验证的项，不算全过」的假失败。"""
    from agent.harness import _window_ids_for, apply_judge, build_criteria

    window = {"id": "W1", "title": "新建关卡 L_NewTest2", "action": "create_level",
              "slots": {"level": "L_NewTest2"}, "depends_on": [], "max_retries": 1}
    other = {"id": "W2", "title": "表外动作", "action": "no_such_action", "slots": {},
             "depends_on": [], "max_retries": 1}

    rows = build_criteria(windows=[window])
    kinds = [str(item.get("evidence_kind") or "") for item in rows]
    assert "editor_assert" in kinds, ("create_level 产不出 editor_assert 条款", rows)
    clause = next(item for item in rows if item.get("evidence_kind") == "editor_assert")
    assert clause.get("windows") == ["W1"], ("条款没绑到 create_level 那一扇", clause)

    # 绑定性：表外动作的裁判勾不动它，create_level 那一扇的裁判才勾得动
    both = build_criteria(windows=[window, other])
    stamped_other = apply_judge(both, {"ok": True, "kind": "editor_assert", "window": "W2"})
    assert [
        item.get("passed") for item in stamped_other if item.get("evidence_kind") == "editor_assert"
    ] == [False], ("别的窗口的裁判顺手勾上了 create_level 的条款", stamped_other)
    stamped_own = apply_judge(both, {"ok": True, "kind": "editor_assert", "window": "W1"})
    assert [
        item.get("passed") for item in stamped_own if item.get("evidence_kind") == "editor_assert"
    ] == [True], stamped_own

    # 反例守卫：表里没有的动作仍不产条款、不绑定（别把默认值放宽成「认不出也绑」）
    assert build_criteria(windows=[other]) == [], build_criteria(windows=[other])
    assert _window_ids_for("editor_assert", [other]) == [], _window_ids_for("editor_assert", [other])
    assert _window_ids_for("editor_assert", [window]) == ["W1"]


def check_binding_parity() -> None:
    """T8：声明点两侧必须对齐 —— 表里给映射，窗口就得真报得出那种判词，否则条款永远过不了。

    实测（20261024）：`create_widget_blueprint` 与 `bind_blueprint` 的 catalog 口径都是 editor_assert，
    但它们都是 DIRECT_ACTIONS，sequence 侧**没有**声明判词（`_create_widget_blueprint` 与
    `_run_action` 里的 bind_slot 段都不产出 judge_kind/test；只有 `_create_level` 产 editor_assert）。
    补上映射的实测后果：条款 editor_assert 永远 passed=False ⇒ 范围记「条款未过」、集成记
    「矩阵未全过」—— 把「没判过」说成「做坏了」。所以这两处**已回滚**，留给 parity 守卫在 producer
    侧补判词后两侧一起加。这里把口径钉住：编辑控件图仍是 blueprint_compile（创建 ≠ 编辑，防照抄）；
    认不出的动作仍不产条款、不绑定；producer 没声明判词 ⇒ 表里不许先有映射（反之亦然）。"""
    import inspect

    from agent import sequence
    from agent.harness import _KIND_BY_ACTION, _window_ids_for, build_criteria

    # 1) 反照抄：编辑控件图是 blueprint_compile（控件图编译），不是创建时的 editor_assert
    edit_rows = build_criteria(windows=[{"id": "W1", "action": "edit_widget_blueprint", "slots": {}}])
    assert [str(item.get("evidence_kind") or "") for item in edit_rows] == ["blueprint_compile"], edit_rows

    # 2) 反例守卫：认不出的动作不产条款、不绑定（别把默认值放宽成「认不出也绑」）
    other = {"id": "W2", "title": "表外动作", "action": "no_such_action", "slots": {}}
    assert build_criteria(windows=[other]) == [], build_criteria(windows=[other])
    assert _window_ids_for("editor_assert", [other]) == [], _window_ids_for("editor_assert", [other])

    # 3) 两侧对齐：producer 声明了判词才允许表里有映射，没声明就不许先加（会造出过不了的条款）
    def _declares_judge(func) -> bool | None:
        """producer 源码里有没有声明 judge_kind。读不到源码（冻结/exec 部署）返回 None = 不猜。"""
        try:
            return "judge_kind" in inspect.getsource(func)
        except (OSError, TypeError):
            return None

    producers = {
        "create_widget_blueprint": _declares_judge(sequence._create_widget_blueprint),
        "bind_blueprint": _declares_judge(sequence._run_action),
    }
    for action, declared in producers.items():
        if declared is None:
            continue  # 读不到 producer 源码：这条对齐断言不猜，别制造假红
        rows = build_criteria(windows=[{"id": "W1", "action": action, "slots": {}}])
        if declared:
            assert action in _KIND_BY_ACTION, (
                f"{action} 已经会报判词，_KIND_BY_ACTION 的映射该补上（parity 守卫 P0-1）"
            )
            assert rows and rows[0].get("windows") == ["W1"], rows
        else:
            assert action not in _KIND_BY_ACTION, (
                f"{action} 还没在 sequence 里声明判词（judge_kind），先加映射会造出永远过不了的条款"
            )
            assert rows == [], (action, rows)


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

            check_gate_wording()
            check_rescue_paths()
            check_no_evidence_run()
            check_real_rescue_mixed_run()
            check_normal_scope_success()
            check_create_level_binding()
            check_binding_parity()
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
    print("capability gap ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())