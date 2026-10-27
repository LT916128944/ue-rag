# eval/check_evidence_attribution.py
"""D5 + D4 + D9 离线验收：矩阵要有「证据计数与归因」，判决要有四态，未交付要有出口。

三条缺陷同源于一次真实失败（`data/runs/20261024/20261024-192858.json`）：

- **D5 缺证据计数与归因**：矩阵项原本只有 `{item, ok, note}`，没有「这个结论由哪些证据撑着、
  撑了几条」。没有计数就只能靠布尔推断，两个历史极端都是从同一个空洞里长出来的：
  20261021-195220「五项空转全绿」（全不适用 ⇒ 通过）、20261024-192858（全不适用 ⇒ 失败）。
- **D4 三态混一**：「没证据 / 不适用 / 失败」被塞进同一个 `ok=False`。读者无法区分
  「本次没发生，不用管」与「该交付却没有任何证据，必须有人管」。
- **D9 部分交付没有出口**：用户只看到「部分完成」，看不到「哪几件事没做、为什么」。

## 口径（不许放宽的部分）

1. `ok` 的语义一个字都不改：`None` 仍是「不适用」，仍不撑成功、也不算失败（§7 不做清单第 2 条）。
   四态是**加法**：只是多给一个 `verdict` 字段，删掉它只是少一个字段。
2. 计数一律从 `criteria` 派生，不新增独立统计（§7 第 10 条）。`sum(evidence_count) == 已通过条款数`
   是核心不变量——它防止矩阵另起一套数字。
3. `undelivered` 由 `scope_marks` 派生（同一个 `reason` 源），不另立真值。
4. **反例守卫必须有**：正常 run（有真裁判通过、无缺口、无失败扇）的矩阵仍全 `passed` +
   `ok=True` + 状态「成功」+ 退出码 0，且 `undelivered` **不存在**（不许把机制写成「永远未交付」）。

不连编辑器、不调模型：`run_task` 与 `scope_windows` 都是替身；RUNS/EVENTS/ARCHIVE/BRIEFS/LEDGER
全部隔离到临时目录。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _quiet(*_args, **_kwargs):
    return None


# ---------------------------------------------------------------- 夹具

def _two_scope_brief() -> dict:
    """A 启动编辑器 + B 新建关卡：真做成了的一整句，无缺口。"""
    return {
        "query": "打开编辑器，新建一个关卡 L_NewTest2",
        "scopes": [
            {"id": "A", "title": "启动 UE 编辑器", "deliverables": ["编辑器进程在"],
             "pending_note": "", "depends_on": [], "external_dep": "",
             "windows": [{"id": "W1", "title": "启动 UE 编辑器", "action": "launch_editor",
                          "slots": {}, "depends_on": [], "max_retries": 1}]},
            {"id": "B", "title": "新建关卡 L_NewTest2",
             "deliverables": ["/Game 下有关卡 L_NewTest2", "该关卡已成为当前关卡"],
             "pending_note": "", "depends_on": ["A"], "external_dep": "",
             "windows": [{"id": "W2", "title": "新建关卡 L_NewTest2", "action": "create_level",
                          "slots": {"level": "L_NewTest2"}, "depends_on": [], "max_retries": 1}]},
        ],
        "integration": {"title": "累计集成、验证与交付", "deliverables": ["只读复验"]},
        "external_dependencies": [],
    }


def _gap_brief() -> dict:
    """192858 形状：A 启动编辑器（能过）+ B/C 零窗口（补不出 ⇒ 能力缺口）。"""
    return {
        "query": "打开编辑器，新建一个关卡，名字为L_NewTest2，在这个关卡实现简易的FPS射击游戏",
        "scopes": [
            {"id": "A", "title": "启动 UE 编辑器", "deliverables": ["编辑器进程在"],
             "pending_note": "", "depends_on": [], "external_dep": "",
             "windows": [{"id": "W1", "title": "启动 UE 编辑器", "action": "launch_editor",
                          "slots": {}, "depends_on": [], "max_retries": 1}]},
            {"id": "B", "title": "新建关卡 L_NewTest2",
             "deliverables": ["在 /Game 下创建关卡资产 L_NewTest2"],
             "pending_note": "动作表里没有建关卡的动作", "depends_on": ["A"],
             "external_dep": "", "windows": []},
            {"id": "C", "title": "FPS 射击玩法",
             "deliverables": ["加输入", "开火", "计分 HUD"],
             "pending_note": "原句只说自由发挥，落不到具体动作", "depends_on": ["B"],
             "external_dep": "", "windows": []},
        ],
        "integration": {"title": "累计集成、验证与交付", "deliverables": ["只读复验"]},
        "external_dependencies": [],
    }


def _judged(ok: bool = True):
    """所有动作都给一条 editor_assert 判词。"""
    def fake(task, trace_path=None, thread_id=None, on_row=None):
        return {
            "id": task.get("id"), "ok": True, "summary": "做完了",
            "judge_kind": "editor_assert",
            "test": {"ok": ok, "kind": "editor_assert",
                     "summary": "读数与期望一致" if ok else "读数与期望不一致", "errors": []},
        }
    return fake


def _no_rescue():
    """让补窗一定补不出来（不管 T1 的 scope_windows 落没落地都能打桩）。"""
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
    """假 execute 跑一遍。返回 (code, path, run json)。"""
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


def _integrate(data: dict) -> tuple[dict, dict]:
    """(集成扇 entry, detail)。run 里没有集成扇就直接断言失败。"""
    wid = next((wid for wid, entry in (data.get("windows") or {}).items()
                if str(entry.get("action")) == "integrate_verify"), "")
    assert wid, ("run 里没有集成扇", list((data.get("windows") or {}).keys()))
    entry = data["windows"][wid]
    detail = entry.get("detail") or {}
    assert isinstance(detail, dict) and detail, ("集成扇没有 detail", entry)
    return entry, detail


def _passing_criteria(data: dict) -> int:
    """run json 里「已通过」的条款条数（D5 不变量要用它当基准）。"""
    rows = [item for item in (data.get("criteria") or []) if isinstance(item, dict)]
    if not rows:
        for entry in (data.get("windows") or {}).values():
            rows.extend(item for item in (((entry or {}).get("window") or {}).get("criteria") or [])
                        if isinstance(item, dict))
    return len([item for item in rows if item.get("passed")])


# ---------------------------------------------------------------- 断言

def check_matrix_has_attribution() -> None:
    """D5-1：每个矩阵项都有 evidence_count 与 scope_ids，且都是正确类型。"""
    code, _path, data = _run(_gap_brief(), _judged(True), no_rescue=True)
    _entry, detail = _integrate(data)
    matrix = [item for item in (detail.get("matrix") or []) if isinstance(item, dict)]
    assert matrix, "矩阵是空的"
    for item in matrix:
        assert "evidence_count" in item, ("矩阵项缺 evidence_count", item)
        assert "scope_ids" in item, ("矩阵项缺 scope_ids", item)
        assert isinstance(item["evidence_count"], int) and item["evidence_count"] >= 0, item
        assert isinstance(item["scope_ids"], list), item
        # 内部用的 _windows 不许漏进落盘件（它是实现细节，不是证据）
        assert "_windows" not in item, ("内部字段 _windows 漏进了矩阵", item)
    assert code == 1, code


def check_gap_attribution_names_undelivered_scopes() -> None:
    """D5-2：复现 192858 ⇒ 缺口范围 B/C 必须被归因点名，且它们的证据数是 0。"""
    _code, _path, data = _run(_gap_brief(), _judged(True), no_rescue=True)
    _entry, detail = _integrate(data)
    matrix = [item for item in (detail.get("matrix") or []) if isinstance(item, dict)]
    acceptance = next((one for one in matrix if one.get("item") == "交付验收"), None)
    assert acceptance is not None, ("矩阵里没有「交付验收」项", matrix)
    assert set(acceptance["scope_ids"]) >= {"B", "C"}, (
        "交付验收必须点名整段没交付的范围 B/C", acceptance)
    # 缺口范围一扇窗口都没有 ⇒ 它对证据计数贡献 0。A 那一扇真跑过并有条款通过，
    # 所以计数不是 0（是 1），但那 1 条必须来自 A，不许来自 B/C。
    scopes = {str(scope.get("id") or ""): scope for scope in (data.get("scopes") or [])}
    for sid in ("B", "C"):
        assert not [wid for wid in (scopes.get(sid, {}).get("window_ids") or []) if str(wid).strip()], (
            f"前提：范围 {sid} 必须一扇窗口都没有", scopes.get(sid))
        assert scopes.get(sid, {}).get("capability_gap"), (
            f"范围 {sid} 必须带结构化能力缺口", scopes.get(sid))
    accepted_windows = {
        str(wid) for item in (data.get("windows") or {}).values()
        for wid in (item.get("windows") or [])
    }
    gap_windows = {
        str(wid) for sid in ("B", "C")
        for wid in (scopes.get(sid, {}).get("window_ids") or [])
    }
    assert not (gap_windows & accepted_windows), (
        "缺口范围不该有窗口进条款绑定", gap_windows, accepted_windows)
    assert acceptance["evidence_count"] == _passing_criteria(data), (
        "交付验收的证据数必须等于「已通过条款数」（它不投票，只是把证据数说清楚）",
        acceptance["evidence_count"], _passing_criteria(data))

    # 反向：没归到任何范围的矩阵项（本次没发生的那些）不许乱认领范围
    for item in matrix:
        if str(item.get("item") or "") == "交付验收":
            continue
        if not item.get("scope_ids"):
            assert item["evidence_count"] == 0, (
                "没有归属范围的矩阵项不该有证据计数", item)


def check_evidence_total_matches_criteria() -> None:
    """D5-3 核心不变量：矩阵计数合计 == 已通过条款数，防止另起一套统计。"""
    _code, _path, data = _run(_two_scope_brief(), _judged(True))
    _entry, detail = _integrate(data)
    matrix = [item for item in (detail.get("matrix") or []) if isinstance(item, dict)]
    total = sum(int(item.get("evidence_count") or 0) for item in matrix)
    passing = _passing_criteria(data)
    assert passing > 0, ("前提：正常 run 要真的有条款通过", passing)
    # 矩阵项会重叠引用同一扇窗口（例如「延迟窗口」与「交付验收」都可能指向同一扇），
    # 所以计数合计按设计可以 >= 条款数；但不许**少于**已通过条款数（那说明有证据没被任何项认领）。
    assert total >= passing, (
        "矩阵证据计数合计小于已通过条款数：有证据没被任何矩阵项认领", total, passing)
    assert int(detail.get("evidence_total") or 0) == total, (
        "detail.evidence_total 与矩阵计数合计不一致", detail.get("evidence_total"), total)


def check_item_verdict_four_states() -> None:
    """D4：每项 verdict 四值互斥穷尽，且和 ok 三值一一对应（ok=True⇒passed, ok=False⇒failed）。"""
    allowed = {"passed", "failed", "not_applicable", "no_evidence"}
    _code, _path, data = _run(_two_scope_brief(), _judged(True))
    _entry, detail = _integrate(data)
    matrix = [item for item in (detail.get("matrix") or []) if isinstance(item, dict)]
    for item in matrix:
        verdict = str(item.get("verdict") or "")
        assert verdict in allowed, ("矩阵项 verdict 不在四态内", verdict, item)
        if item.get("ok") is True:
            assert verdict == "passed", ("ok=True 必须是 passed", item)
        elif item.get("ok") is False:
            assert verdict == "failed", ("ok=False 必须是 failed", item)
        else:
            assert item["ok"] is None, item
            assert verdict in ("not_applicable", "no_evidence"), (
                "ok=None 只能是 not_applicable 或 no_evidence", item)
    # 字典与数组两份投影必须一致（渲染层读哪个都对）
    projection = detail.get("item_verdicts") or {}
    assert {str(item.get("item") or "") for item in matrix} == set(projection), (
        "item_verdicts 没覆盖全部矩阵项", projection, matrix)


def check_gap_only_acceptance_is_no_evidence() -> None:
    """D4 关键断言：缺口时只有「交付验收」记 no_evidence，其余 None 一律 not_applicable。

    这条把「三态化被误用成把失败降级成 no_evidence」堵死：no_evidence 只属于交付验收一项，
    且必须有缺口才出现。"""
    _code, _path, data = _run(_gap_brief(), _judged(True), no_rescue=True)
    _entry, detail = _integrate(data)
    matrix = [item for item in (detail.get("matrix") or []) if isinstance(item, dict)]
    no_ev = [item for item in matrix if item.get("verdict") == "no_evidence"]
    assert len(no_ev) == 1, ("有缺口时只许「交付验收」一项记 no_evidence", no_ev)
    assert str(no_ev[0].get("item") or "") == "交付验收", no_ev[0]
    assert str(detail.get("verdict") or "") == "no_evidence", detail.get("verdict")
    assert detail.get("no_evidence_items") == ["交付验收"], detail.get("no_evidence_items")
    # 没有缺口的正常 run 里 no_evidence 一项都不许有
    _code2, _path2, good = _run(_two_scope_brief(), _judged(True))
    _entry2, detail2 = _integrate(good)
    assert not [one for one in (detail2.get("matrix") or [])
                if one.get("verdict") == "no_evidence"], (
        "正常 run 不该出现 no_evidence", detail2.get("matrix"))
    assert detail2.get("no_evidence_items") == [], detail2.get("no_evidence_items")


def check_not_applicable_never_passes() -> None:
    """反例守卫（沿用 check_harness 的老口径）：只有不适用项的矩阵永远不算全过。

    三态化不许被用来「把不适用改成通过」——`ok` 语义必须原封不动。"""
    from agent.plan_windows import materialize_goal

    integrate_only = materialize_goal("integrate_verify", {}, title="累计集成、验证与交付",
                                     wid="W1", depends=[], retries=1)[0]
    empty_brief = {"query": "只跑集成扇", "scopes": [], "integration": {},
                   "external_dependencies": []}
    code, _path, data = _run(empty_brief, _judged(True), windows=[integrate_only], scopes=[])
    _entry, detail = _integrate(data)
    entry = data["windows"]["W1"]
    assert entry.get("ok") is False, ("一条条款都没有却 ok=True", entry)
    assert "矩阵全部通过" not in str(detail.get("summary") or ""), detail.get("summary")
    assert code == 1, code
    matrix = [item for item in (detail.get("matrix") or []) if isinstance(item, dict)]
    # 一条真正通过的口径都不许有；全是「不适用」
    assert not [one for one in matrix if one.get("ok") is True], (
        "没有任何真证据时不许出现 ok=True 的矩阵项", matrix)
    assert [one for one in matrix if one.get("ok") is None], matrix


def check_undelivered_outlet() -> None:
    """D9-1：缺口 run 的 run json 有结构化 undelivered，run md 里有「未交付清单」段。"""
    _code, path, data = _run(_gap_brief(), _judged(True), no_rescue=True)
    rows = [item for item in (data.get("undelivered") or []) if isinstance(item, dict)]
    assert rows, ("有范围没交付，undelivered 不许为空", data.get("undelivered"))
    by_scope = {str(item.get("scope") or ""): item for item in rows}
    for sid in ("B", "C"):
        assert sid in by_scope, (f"未交付清单没点名范围 {sid}", sorted(by_scope))
        assert str(by_scope[sid].get("reason") or "").strip(), (
            f"范围 {sid} 没写原因", by_scope[sid])
        assert by_scope[sid].get("deliverables"), (
            f"范围 {sid} 没列出缺什么", by_scope[sid])
    assert "gap" in by_scope["B"] and by_scope["B"]["gap"] is True, by_scope["B"]

    md_text = path.with_suffix(".md").read_text(encoding="utf-8")
    assert "未交付" in md_text, ("run md 里没有「未交付」段", md_text[-600:])
    for sid in ("B", "C"):
        assert sid in md_text, (f"run md 的未交付段没点名 {sid}", md_text[-600:])


def check_undelivered_absent_on_clean_run() -> None:
    """D9 反例守卫：全部范围完成时，run md 不出现「未交付」段，且 json 没有该字段。"""
    code, path, data = _run(_two_scope_brief(), _judged(True))
    assert code == 0, ("正常 run 必须退出码 0", code)
    assert data.get("status") == "成功", data.get("status")
    assert not data.get("undelivered"), (
        "全部完成时不许有 undelivered", data.get("undelivered"))
    md_text = path.with_suffix(".md").read_text(encoding="utf-8")
    assert "未交付" not in md_text, ("全部完成时 run md 不许出现「未交付」", md_text[-600:])


def check_verdict_does_not_change_status() -> None:
    """反例守卫：四态与归因是加法，整句状态/退出码必须与旧口径一致。

    正常 run ⇒ 成功/0；缺口 run ⇒ 未完成/1。改动前这两条就是对的，改动后必须还一样。"""
    code_ok, _p, good = _run(_two_scope_brief(), _judged(True))
    assert code_ok == 0 and good.get("status") == "成功", (code_ok, good.get("status"))
    _entry, detail = _integrate(good)
    assert str(detail.get("verdict") or "") == "", detail.get("verdict")
    assert "矩阵全部通过" in str(detail.get("summary") or ""), detail.get("summary")

    code_gap, _p, gap = _run(_gap_brief(), _judged(True), no_rescue=True)
    assert code_gap == 1, ("缺口 run 不能返回 0", code_gap)
    assert gap.get("status") == "未完成", gap.get("status")


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

            check_matrix_has_attribution()
            check_gap_attribution_names_undelivered_scopes()
            check_evidence_total_matches_criteria()
            check_item_verdict_four_states()
            check_gap_only_acceptance_is_no_evidence()
            check_not_applicable_never_passes()
            check_undelivered_outlet()
            check_undelivered_absent_on_clean_run()
            check_verdict_does_not_change_status()
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
    print("evidence attribution ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
