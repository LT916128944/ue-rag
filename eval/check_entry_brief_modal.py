# eval/check_entry_brief_modal.py
"""任务书人审弹窗（20261118）：把「写任务书」从后台日志剥离成可读可改的网页表单。

背景：任务卡在「出任务书」阶段时，后台日志里那张 rich ASCII 表在宽表里会截列
（「尚未完… / 估时(小…」），人读不全也改不了，而且这一等就是几分钟。改为：
`question(kind=brief)` 事件随事件流带上当前这版 brief，入口页用它渲染可编辑弹窗；
用户改完后点「确认 / 应用修改 / 放弃」，答案以 `@@brief@@<json>` 一行写进收件箱，
`brief._parse_brief_answer` 把它当成结构化编辑/裁决，走老分支之前先合并文本字段。

全离线：不连编辑器、不调模型、不写 data/（brief 目录在临时目录里造）。
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


SCOPE = {
    "id": "A", "title": "旧标题", "deliverables": ["d1"], "pending_note": "",
    "depends_on": [], "external_dep": "",
    "windows": [{"id": "W1", "title": "t", "action": "set_pickable",
                 "slots": {}, "depends_on": [], "max_retries": 1}],
    "estimate_hours": [1.0, 2.0],
}
INTEG = {"title": "累计集成、验证与交付", "deliverables": ["i1"], "estimate_hours": [1.0, 1.0]}
EDITED = {
    "scopes": [{"id": "A", "title": "新标题", "deliverables": ["d1", "d2"],
                "pending_note": "n", "estimate_hours": [3, 5]}],
    "integration": {"title": "累计集成、验证与交付", "deliverables": ["i1", "i2"], "estimate_hours": [2, 2]},
}


def _sample() -> dict:
    return {"query": "原句", "scopes": [copy.deepcopy(SCOPE)], "integration": copy.deepcopy(INTEG),
            "external_dependencies": []}


def check_page_has_modal() -> str:
    from server import entry

    text = entry._page().read_text(encoding="utf-8")
    for token in ('id="briefmodal"', 'id="bm-body"', 'id="bm-decide"', "function renderBriefModal",
                  'BRIEF_ANS_PREFIX = "@@brief@@"'):
        check(token in text, f"入口页缺任务书弹窗接线：{token}")
    check("renderBriefModal(ev)" in text and "state.briefCollapsed" in text,
          "renderQuestion 没有把 brief question 接到弹窗（或没有收起回退）")
    return "入口页有任务书弹窗骨架（#briefmodal / renderBriefModal / BRIEF_ANS_PREFIX）"


def check_prefix_matches() -> str:
    """前后端前缀必须逐字一致，否则弹窗提交的答案后端认不出。"""
    from agent import brief as B

    check(B.BRIEF_ANSWER_PREFIX == "@@brief@@", f"后端前缀变了：{B.BRIEF_ANSWER_PREFIX!r}")
    from server import entry  # noqa: F401

    text = entry._page().read_text(encoding="utf-8")
    check('BRIEF_ANS_PREFIX = "@@brief@@"' in text, "前端 BRIEF_ANS_PREFIX 与后端不一致")
    return "前后端 @@brief@@ 前缀逐字一致"


def check_parse() -> str:
    from agent import brief as B

    check(B._parse_brief_answer("确认") is None, "老口令不该被判成结构化答案")
    check(B._parse_brief_answer("") is None, "空答案不该被判成结构化答案")
    action, edited = B._parse_brief_answer(
        B.BRIEF_ANSWER_PREFIX + json.dumps({"action": "confirm", "brief": {"scopes": []}})
    )
    check(action == "confirm" and edited == {"scopes": []}, f"解析失败：{action!r}/{edited!r}")
    check(B._parse_brief_answer(B.BRIEF_ANSWER_PREFIX + "坏 json") is None, "坏 JSON 应返回 None")
    check(B._parse_brief_answer(B.BRIEF_ANSWER_PREFIX + json.dumps({"action": "bogus"})) is None,
          "未知 action 应返回 None")
    return "结构化答案解析：confirm/gap_ack/abort/edit，坏输入退回老分支"


def check_apply_edit() -> str:
    from agent import brief as B

    brief = _sample()
    ok, _note = B._apply_structured_edit(brief, EDITED)
    check(ok, "应用结构化编辑失败")
    scope = brief["scopes"][0]
    check(scope["title"] == "新标题", f"标题没改：{scope['title']!r}")
    check(scope["deliverables"] == ["d1", "d2"], f"交付没改：{scope['deliverables']!r}")
    check(scope["estimate_hours"] == [3.0, 5.0], f"估时没改：{scope['estimate_hours']!r}")
    check(scope["windows"][0]["action"] == "set_pickable", "手改不该动窗口结构")
    check(brief["integration"]["deliverables"] == ["i1", "i2"], "集成行交付没改")
    check(brief["total_estimate"] == {"min_hours": 5.0, "max_hours": 7.0},
          f"合计没随估时刷新：{brief['total_estimate']!r}")
    return "结构化编辑只合并文本字段、不动窗口结构，合计随估时刷新"


def check_flow_offline() -> str:
    """整条 brief_flow：edit 后 confirm，reader 必须收到带 brief 的 extra。"""
    from agent import brief as B

    sample = _sample()
    orig_gen, orig_prev = B.generate_brief, B.preview_coverage
    B.generate_brief = lambda *a, **k: copy.deepcopy(sample)
    B.preview_coverage = lambda brief: brief.__setitem__(
        "coverage", {"scopes": [], "gaps": [], "dry_run": True}
    ) or brief["coverage"]
    try:
        calls = {"n": 0}
        seen = {"extra": False}

        def reader(prompt="", extra=None):
            calls["n"] += 1
            if isinstance(extra, dict) and isinstance(extra.get("brief"), dict):
                seen["extra"] = True
            if calls["n"] == 1:
                return B.BRIEF_ANSWER_PREFIX + json.dumps({"action": "edit", "brief": EDITED})
            return B.BRIEF_ANSWER_PREFIX + json.dumps({"action": "confirm", "brief": EDITED})

        with tempfile.TemporaryDirectory(prefix="entry-brief-modal-") as tmp:
            out = B.brief_flow("原句", windows=[], reader=reader, printer=lambda *a, **k: None,
                               directory=Path(tmp))
            check(out is not None, "brief_flow 没返回")
            status, brief, md, js = out
            check(status == "confirm", f"确认后状态应为 confirm，实际 {status!r}")
            check(seen["extra"], "reader 没收到带 brief 的 extra（弹窗无从渲染）")
            check(brief["scopes"][0]["title"] == "新标题", "确认前没先应用编辑")
            check(brief["scopes"][0]["deliverables"] == ["d1", "d2"], "确认前没先应用交付编辑")
            check(brief.get("manual_edits") == 2, f"manual_edits 应为 2，实际 {brief.get('manual_edits')!r}")
            check(Path(md).is_file() and Path(js).is_file(), "任务书 .md/.json 没落盘")
    finally:
        B.generate_brief, B.preview_coverage = orig_gen, orig_prev
    return "brief_flow 端到端：收到 brief → 应用编辑 → 确认 → 落盘（全程离线）"


def check_ask_extra() -> str:
    """Channel.ask 把 extra 原样带进 question 事件，不改老字段。"""
    from agent import ui_ask

    captured = []

    class _Rec:
        def __call__(self, etype, **fields):
            captured.append({"type": etype, **fields})

    ui_ask.reset()
    ch = ui_ask.configure(inbox=None, stdin=lambda prompt="": "y", printer=lambda *a, **k: None)
    ch.run_id = "20261118-000000"
    ch.emit = _Rec()
    ch.ask(kind="brief", prompt="任务书审阅: ", options=[{"value": "", "label": "确认"}],
           extra={"brief": {"query": "q"}, "gap_phrase": "确认缺口"})
    question = next((row for row in captured if row.get("type") == "question"), None)
    check(question is not None, "没发 question 事件")
    check(isinstance(question.get("brief"), dict) and question["brief"].get("query") == "q",
          f"额外的 brief 没进事件：{question.get('brief')!r}")
    check(question.get("question_id") and "options" in question, "老字段（question_id/options）被破坏")
    ui_ask.reset()
    return "Channel.ask 把 extra 的 brief 带进 question 事件，老字段不变"


def main() -> int:
    try:
        rows = [
            check_page_has_modal(),
            check_prefix_matches(),
            check_parse(),
            check_apply_edit(),
            check_flow_offline(),
            check_ask_extra(),
        ]
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            from agent import ui_ask

            ui_ask.reset()
        except Exception:  # noqa: BLE001
            pass
    for row in rows:
        print("  " + row)
    print("entry brief modal ok（弹窗渲染 + 结构化编辑/确认 + 事件带 brief）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
