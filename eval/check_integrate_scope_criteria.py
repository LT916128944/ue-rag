# eval/check_integrate_scope_criteria.py
"""集成矩阵要看得见「范围级条款」——20261117-140229 的 W3 假失败。

真事：任务「在 WBP_FpsHUD 左下角加血条」经求解兜底走任务书物化路径，合同被 build_criteria
落在**范围** `scope["criteria"]`（scope A 的 c-automation，passed=True，windows=[W1,W2]）；
run 级 `state.data["criteria"]` 被 stitch 把「没有窗口能证明」的条款摘空，窗口 dict 也不带
criteria。`_delivery_criteria` 旧口径只读 run 级 + 窗口级 ⇒ 一条都读不到 ⇒ 交付验收退回
「本次没有产出任何条款」⇒ 6 项全不适用 ⇒ `矩阵没有可验证的项，不算全过` + stop_reason=
judge_failed + 整句失败。真做成了被记成假失败（check_integrate_verdict 同族缺陷的又一镜像面）。

本检查钉两点（全离线：run_task / 窗口执行都是替身，归档写临时目录）：
1. run 级与窗口级都空、但范围条款已 passed ⇒ 交付验收 ok=True，"矩阵全部通过"，集成 ok=True；
2. 同一形状但范围条款 passed=False ⇒ 交付验收 ok=False，集成 ok=False（真失败仍要看得见），
   证明「看得见范围条款」不会把失败也撑成成功。

不连编辑器、不调模型、不真执行窗口。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _state(run_id: str, *, passed: bool):
    """W1/W2 solve 都成功、W3 集成；run 级 criteria 空、窗口不带 criteria、条款只在 scope A 上。"""
    from agent import run_windows as rw

    state = rw._RunState(run_id, "在 WBP_FpsHUD 左下角加血条", "solver-fallback", [])
    state.data["criteria"] = []
    state.data["windows"] = {
        "W1": {"id": "W1", "action": "solve", "state": "done", "ok": True,
               "result": {"id": "W1", "ok": True, "summary": "ok"}, "window": {}, "detail": {}},
        "W2": {"id": "W2", "action": "solve", "state": "done", "ok": True,
               "result": {"id": "W2", "ok": True, "summary": "ok"}, "window": {}, "detail": {}},
        "W3": {"id": "W3", "action": "integrate_verify", "state": "running", "ok": None,
               "result": None, "window": {}, "detail": {}},
    }
    state.data["scopes"] = [{
        "id": "A", "title": "在 WBP_FpsHUD 左下角新增上限100的血条控件",
        "window_ids": ["W1", "W2"], "external_dep": "", "deliverables": ["血条"],
        "criteria": [{
            "id": "c-automation", "text": "Automation 过滤器失败数为 0",
            "evidence_kind": "automation", "passed": bool(passed),
            "windows": ["W1", "W2"],
            "evidence_sha1": "f82aed14e07d727b0013ff00da5929b88999333f" if passed else "",
        }],
    }]
    return state


def check_scope_criteria_pass_makes_success() -> None:
    from agent import run_windows as rw

    state = _state("20261117-140229", passed=True)
    item = rw._delivery_acceptance_item(state)
    check(item.get("ok") is True, ("范围条款已 passed，交付验收必须 True，实际", item))
    out = rw._run_integrate(state.data["windows"]["W3"], state, "q", {}, rw._EventBus("20261117-140229"))
    check(out.get("ok") is True, ("集成必须 ok=True（真做成的不许记假失败）", out.get("summary")))
    check(str(out.get("verdict") or "") == "", ("无缺口时 verdict 应为空", out.get("verdict")))
    check("矩阵全部通过" in str(out.get("summary") or ""), out.get("summary"))


def check_scope_criteria_fail_stays_failed() -> None:
    from agent import run_windows as rw

    state = _state("20261117-140230", passed=False)
    item = rw._delivery_acceptance_item(state)
    check(item.get("ok") is False, ("范围条款未过，交付验收必须 False（真失败要看得见）", item))
    out = rw._run_integrate(state.data["windows"]["W3"], state, "q", {}, rw._EventBus("20261117-140230"))
    check(out.get("ok") is False, ("集成必须 ok=False", out.get("summary")))
    check("矩阵全部通过" not in str(out.get("summary") or ""), out.get("summary"))


def main() -> int:
    from agent import run_windows as rw

    with tempfile.TemporaryDirectory(prefix="integrate-scope-") as raw:
        tmp = Path(raw)
        saved = {"runs": rw.RUNS_DIR, "events": rw.EVENTS_DIR, "archive": rw.ARCHIVE_DIR}
        try:
            rw.RUNS_DIR = tmp / "runs"
            rw.EVENTS_DIR = tmp / "events"
            rw.ARCHIVE_DIR = tmp / "archive"
            rw.RUNS_DIR.mkdir(parents=True, exist_ok=True)
            rw.EVENTS_DIR.mkdir(parents=True, exist_ok=True)
            check_scope_criteria_pass_makes_success()
            check_scope_criteria_fail_stays_failed()
        finally:
            rw.RUNS_DIR = saved["runs"]
            rw.EVENTS_DIR = saved["events"]
            rw.ARCHIVE_DIR = saved["archive"]
    print("  run/窗口级条款为空、条款只在 scope 上：passed ⇒ 成功；未过 ⇒ 失败")
    print("integrate scope criteria ok（范围级条款能被交付验收看见）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
