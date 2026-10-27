# eval/check_escalation.py
"""D2 离线验收：能力缺口必须改变流程走向 —— 补窗后仍空 ⇒ 人工显式确认，否则不执行。

## 缺陷
失败 run（`data/runs/20261024/20261024-192858.json`）里范围 B/C 一扇窗口都没有，
代码只补了一句「没有可执行的窗口，只记录待办，不进执行。」（原 `agent/brief.py:236-237`），
**这句话在后续物化、执行、裁决、人审 4 个环节里没有一处改变流程走向**。
T2 已把它换成结构化 `capability_gap`——但那只治「说不说」，不治「谁会管」：

1. 缺口必须让本句**停**（不留「可执行窗口以外的假进展」）；
2. 缺口进人审门必须被**显式确认**；
3. 缺口有 owner 与到期（候选表的 aging，见 `check_candidate_aging.py`）。

本文件管第 1、2 两条。

## 口径（不许放宽的部分）
- 判定条件严格保持 `_zero_window_scope` 的形状：「`deliverables` 非空 + `windows` 空 +
  非 `blocked` + 无 `external_dep` + 非集成末行」，**不放宽**。
- `UE_ESCALATE_GAPS=false` 只允许「只记录不阻塞」，**默认必须是开**。
- **反例守卫必须有**：一个正常 run（有真实裁判通过、无零窗口范围）仍记「成功」、
  仍返回 0、**不弹**任何确认——不许把缺口机制写成「永远要人确认」。

不连编辑器、不调模型、不联网；写盘只进临时目录。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _scope(sid: str, *, windows=None, deliverables=None, **extra) -> dict:
    row = {
        "id": sid, "title": f"范围 {sid}",
        "deliverables": deliverables if deliverables is not None else [f"{sid} 的交付"],
        "window_ids": [w["id"] for w in (windows or [])],
        "windows": windows or [],
        "external_dep": "", "blocked": False,
    }
    row.update(extra)
    return row


def _window(wid: str) -> dict:
    return {"id": wid, "title": wid, "action": "launch_editor", "slots": {}, "depends_on": [],
            "max_retries": 1}


def check_gap_detected_after_materialize() -> None:
    """D2-1：补窗后仍空的范围必须被认成缺口（与集成/收尾同一口径）。"""
    from agent import run_windows as rw

    gap_scope = _scope("B", windows=[])
    gap_scope["capability_gap"] = {
        "reason": "能力缺口：没有可执行的窗口（不是待办）",
        "deliverables": ["建关卡"], "missing_actions": ["建关卡"], "pending_note": "",
    }
    ok_scope = _scope("A", windows=[_window("W1")])

    gaps = rw._gap_scopes_of([ok_scope, gap_scope])
    assert [str(item.get("id")) for item in gaps] == ["B"], gaps
    assert rw._gap_scopes_of([ok_scope]) == [], "没有缺口时不许报缺口"
    assert rw._gap_scopes_of([]) == []

    # 等外部的零窗口范围**不是**缺口（既有口径，不许放宽）
    waiting = _scope("C", windows=[], external_dep="M-GO")
    assert rw._gap_scopes_of([waiting]) == [], ("等外部的范围不该算缺口", waiting)
    blocked = _scope("D", windows=[], blocked=True)
    assert rw._gap_scopes_of([blocked]) == [], ("暂停的范围不该算缺口", blocked)


def check_gap_blocks_execution_when_not_confirmed() -> None:
    """D2-2 核心：缺口未确认 ⇒ 不执行（返回 False），且要打出清单与拒绝话术。"""
    from agent import run_windows as rw

    gap = _scope("B", windows=[])
    gap["capability_gap"] = {"reason": "能力缺口", "deliverables": ["建关卡"],
                             "missing_actions": ["建关卡"], "pending_note": ""}
    printed: list[str] = []
    asked: list[str] = []

    def ask(prompt=""):
        asked.append(prompt)
        return ""  # 直接回车 = 不确认

    def show(text):
        printed.append(str(text))

    old = os.environ.pop("UE_ESCALATE_GAPS", None)
    try:
        allowed, gaps = rw._escalate_gaps([gap], ask, show)
    finally:
        if old is not None:
            os.environ["UE_ESCALATE_GAPS"] = old
    assert allowed is False, ("缺口未确认却放行执行", allowed)
    assert [str(item.get("id")) for item in gaps] == ["B"], gaps
    assert asked, "没有问人"
    joined = "\n".join(printed)
    assert "能力缺口" in joined and "B" in joined, ("没点名缺口范围", joined)
    assert "不执行" in joined, ("没说不执行", joined)


def check_gap_proceeds_only_on_explicit_token() -> None:
    """D2-3：只有明确确认词才放行；乱输一律不执行。确认词与 brief 侧同源。"""
    from agent import run_windows as rw

    gap = _scope("B", windows=[])
    gap["capability_gap"] = {"reason": "能力缺口", "deliverables": ["x"],
                             "missing_actions": [], "pending_note": ""}
    phrase = rw._gap_ack_phrase()
    # 确认词必须与 brief 侧（人审门）是同一个：两侧各要一个词会把人问两遍、且容易只过一道闸
    try:
        from agent.brief import GAP_ACK_PHRASE

        assert phrase == GAP_ACK_PHRASE, (
            "执行前的确认词与人审门的确认词必须同源", phrase, GAP_ACK_PHRASE)
    except ImportError:
        pass
    old = os.environ.pop("UE_ESCALATE_GAPS", None)
    try:
        for answer in ("", "y", "yes", "确认", "ok", "通过", "confirm", "confirm-gaps"):
            allowed, _gaps = rw._escalate_gaps([gap], lambda _p="", a=answer: a, lambda _t: None)
            assert allowed is False, (f"输入 {answer!r} 不该放行缺口", answer)
        allowed, gaps = rw._escalate_gaps(
            [gap], lambda _p="": phrase, lambda _t: None)
        assert allowed is True and gaps, "明确确认后应当放行"
    finally:
        if old is not None:
            os.environ["UE_ESCALATE_GAPS"] = old


def check_acknowledged_gap_not_asked_twice() -> None:
    """D2-6：人审门已确认过的缺口，执行前**不再问第二遍**（但也没变成不问）。

    这条防的是「两道闸互相不知道」：人审门问一次、执行前再问一次，人会以为系统没记住。
    同时也防反向退化——`gap_ack` 里记的不是这批范围时，仍然必须问。"""
    from agent import run_windows as rw

    gap = _scope("B", windows=[])
    gap["capability_gap"] = {"reason": "能力缺口", "deliverables": ["x"],
                             "missing_actions": [], "pending_note": ""}
    asked: list[str] = []
    printed: list[str] = []
    brief = {"gap_ack": {"acknowledged": True, "scopes": ["B"], "by": "人"}}
    allowed, gaps = rw._escalate_gaps(
        [gap], lambda p="": asked.append(p) or "", lambda t: printed.append(str(t)), brief)
    assert allowed is True and gaps, ("人审已确认却仍不放行", allowed)
    assert asked == [], ("人审确认过同一批范围，不该再问第二遍", asked)
    assert any("已由人审确认" in line for line in printed), (
        "复核通过也要留一行记录，不能无声放行", printed)

    # 反例：gap_ack 里记的是**别的**范围 ⇒ 这一批没被确认，必须问
    asked2: list[str] = []
    rw._escalate_gaps(
        [gap], lambda p="": asked2.append(p) or "", lambda _t: None,
        {"gap_ack": {"acknowledged": True, "scopes": ["C"]}})
    assert asked2, ("没被确认过的范围必须问人", asked2)

    # 反例：acknowledged=False ⇒ 不算确认
    asked3: list[str] = []
    rw._escalate_gaps(
        [gap], lambda p="": asked3.append(p) or "", lambda _t: None,
        {"gap_ack": {"acknowledged": False, "scopes": ["B"]}})
    assert asked3, ("acknowledged=False 不许当成确认", asked3)

    # 反例：没有 gap_ack 字段（老任务书）⇒ 照旧问
    asked4: list[str] = []
    rw._escalate_gaps([gap], lambda p="": asked4.append(p) or "", lambda _t: None, {})
    assert asked4, ("没有 gap_ack 时必须问人", asked4)


def check_normal_run_not_escalated() -> None:
    """反例守卫：没有缺口时**不问人**、不弹确认（不许退化成人肉开关）。"""
    from agent import run_windows as rw

    asked: list[str] = []
    printed: list[str] = []
    allowed, gaps = rw._escalate_gaps(
        [_scope("A", windows=[_window("W1")])],
        lambda p="": asked.append(p) or "confirm-gaps",
        lambda t: printed.append(str(t)),
    )
    assert allowed is True and gaps == [], (allowed, gaps)
    assert asked == [], ("没有缺口却问了人：缺口机制退化成日常确认", asked)
    assert printed == [], ("没有缺口却打印了缺口话术", printed)


def check_bypass_records_without_blocking() -> None:
    """D2-4：`UE_ESCALATE_GAPS=false` 只记录不阻塞；且默认必须是开。"""
    from agent import run_windows as rw

    gap = _scope("B", windows=[])
    gap["capability_gap"] = {"reason": "能力缺口", "deliverables": ["x"],
                             "missing_actions": [], "pending_note": ""}
    prev = os.environ.get("UE_ESCALATE_GAPS")
    printed: list[str] = []
    try:
        os.environ.pop("UE_ESCALATE_GAPS", None)
        assert rw._escalate_gaps_enabled() is True, "默认必须是开（缺失时不许当成关）"
        for value in ("false", "0", "no", "off"):
            os.environ["UE_ESCALATE_GAPS"] = value
            assert rw._escalate_gaps_enabled() is False, value
        allowed, gaps = rw._escalate_gaps([gap], lambda _p="": "", lambda t: printed.append(str(t)))
        assert allowed is True and gaps, ("旁路时应当只记录不阻塞", allowed, gaps)
        assert any("能力缺口" in line for line in printed), ("旁路也必须留下记录", printed)
        # 反例：任何别的取值都不许关掉它（别让一个错拼的变量名静默关掉守卫）。
        # 注意 "False " / "FALSE" 这类是**故意允许**的：容忍大小写与空白是好行为，
        # 不把它们当「错拼」。真正危险的是「拼错的词被当成关」。
        for value in ("true", "1", "on", "", "随便", "FALSE!", "flase"):
            os.environ["UE_ESCALATE_GAPS"] = value
            assert rw._escalate_gaps_enabled() is True, f"取值 {value!r} 不该关掉守卫"
    finally:
        if prev is None:
            os.environ.pop("UE_ESCALATE_GAPS", None)
        else:
            os.environ["UE_ESCALATE_GAPS"] = prev


def check_declined_gap_is_recorded() -> None:
    """D2-5：人被拒绝之后仍必须留下记录（「拒绝」与「静默蒸发」的区别就在这里）。"""
    from agent import run_windows as rw

    gap = _scope("B", windows=[], deliverables=["建关卡", "切关卡"])
    gap["capability_gap"] = {"reason": "能力缺口：没有可执行的窗口", "deliverables": ["建关卡"],
                             "missing_actions": ["建关卡"], "pending_note": ""}
    events: list[tuple] = []
    rw._record_declined_gaps(
        "打开编辑器，新建一个关卡", "brief.md", {"query": "x"}, [gap], [gap],
        lambda etype, **fields: events.append((etype, fields)),
    )
    assert events, "缺口被拒绝后没有发事件"
    etype, fields = events[0]
    assert etype == "capability_gap_declined", etype
    rows = fields.get("scopes") or []
    assert [str(row.get("scope")) for row in rows] == ["B"], rows
    assert rows[0].get("deliverables"), ("拒绝记录要说清缺什么", rows[0])
    assert "能力缺口" in str(rows[0].get("reason") or ""), rows[0]


def check_real_brief_shape_is_gap() -> None:
    """复现 192858 的真实任务书形状 ⇒ 补不出窗口时 B/C 都必须判成缺口。

    用真实的 `data/briefs` 落盘件（只读）；不存在时用同形状内联替身，并在输出里说明。"""
    from agent import run_windows as rw
    from agent.brief import materialize_scopes, validate_brief

    root = Path(__file__).resolve().parents[1]
    real = sorted((root / "data" / "briefs").glob("*192830*L_NewTest2*.json"))
    if real:
        import json

        payload = json.loads(real[0].read_text(encoding="utf-8"))
        source = str(real[0].name)
    else:
        payload = {
            "query": "打开编辑器，新建一个关卡，名字为L_NewTest2，在这个关卡实现简易的FPS射击游戏",
            "scopes": [
                {"id": "A", "title": "启动编辑器", "deliverables": ["编辑器进程在"],
                 "windows": [{"id": "W1", "action": "launch_editor", "slots": {}}]},
                {"id": "B", "title": "新建关卡 L_NewTest2",
                 "deliverables": ["在 /Game 下创建关卡资产 L_NewTest2"],
                 "pending_note": "动作表里没有建关卡的动作", "windows": []},
                {"id": "C", "title": "FPS 射击玩法",
                 "deliverables": ["加输入", "开火", "计分"],
                 "pending_note": "原句只说自由发挥", "windows": []},
            ],
            "integration": {"title": "累计集成、验证与交付", "deliverables": ["复验"]},
            "external_dependencies": [],
        }
        source = "内联替身（仓库里没有 192830 的任务书落盘件）"

    brief = validate_brief(payload, str(payload.get("query") or ""))
    from agent import plan_windows

    had = hasattr(plan_windows, "scope_windows")
    saved = getattr(plan_windows, "scope_windows", None)
    plan_windows.scope_windows = lambda scope, query="": []
    try:
        _windows, scopes = materialize_scopes(brief)
    finally:
        if had:
            plan_windows.scope_windows = saved
        else:
            try:
                delattr(plan_windows, "scope_windows")
            except AttributeError:
                pass

    gaps = rw._gap_scopes_of(scopes)
    ids = {str(item.get("id")) for item in gaps}
    assert {"B", "C"} <= ids, (f"（证据来源：{source}）B/C 补不出窗时必须判成缺口", sorted(ids), scopes)
    for scope in gaps:
        assert scope.get("capability_gap"), ("缺口范围必须带结构化 capability_gap", scope)


def main() -> int:
    from agent import brief as brief_mod
    from agent import ledger as ledger_mod
    from agent import run_windows as rw
    from agent import snapshot as snapshot_mod

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        saved = {
            "runs": rw.RUNS_DIR, "events": rw.EVENTS_DIR, "archive": rw.ARCHIVE_DIR,
            "briefs": brief_mod.BRIEFS_DIR, "ledger": ledger_mod.LEDGER_PATH,
        }
        prev_candidates = os.environ.get("UE_ACTION_CANDIDATES")
        prev_promoted = os.environ.get("UE_PROMOTED_ACTIONS")
        os.environ["UE_ACTION_CANDIDATES"] = str(tmp / "action_candidates.json")
        os.environ["UE_PROMOTED_ACTIONS"] = str(tmp / "promoted_actions.json")
        try:
            rw.RUNS_DIR = tmp / "runs"
            rw.EVENTS_DIR = tmp / "events"
            rw.ARCHIVE_DIR = tmp / "archive"
            brief_mod.BRIEFS_DIR = tmp / "briefs"
            ledger_mod.LEDGER_PATH = tmp / "ledger.json"

            check_gap_detected_after_materialize()
            check_gap_blocks_execution_when_not_confirmed()
            check_gap_proceeds_only_on_explicit_token()
            check_acknowledged_gap_not_asked_twice()
            check_normal_run_not_escalated()
            check_bypass_records_without_blocking()
            check_declined_gap_is_recorded()
            check_real_brief_shape_is_gap()
        finally:
            rw.RUNS_DIR = saved["runs"]
            rw.EVENTS_DIR = saved["events"]
            rw.ARCHIVE_DIR = saved["archive"]
            brief_mod.BRIEFS_DIR = saved["briefs"]
            ledger_mod.LEDGER_PATH = saved["ledger"]
            for key, value in (("UE_ACTION_CANDIDATES", prev_candidates),
                               ("UE_PROMOTED_ACTIONS", prev_promoted)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    print("escalation ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
