# eval/check_entry_run_scaffold.py
"""入口 UI 的「先落空壳 run，再规划/任务书人审」（20261116「输入指令卡死」根因修复）。

问题：`_run_query` 原先把「规划（理解+检索，几十秒）+ 任务书人审门（可到几百秒）」都跑在
`execute_windows` 之前，而 run 记录（`data/runs/<id>.json`）要到 `execute_windows` 里才创建。
于是这两段时间里 `/api/runs` 根本没有这条 run：
  * 页面 `waitNewRun` 等 12 秒 → 「没等到新 run」，看起来卡死；
  * 任务书人审的 `question` 事件没有 run_id / 事件出口可落，收件箱更按空 run_id 永远匹配
    不上，进程空转到 600 秒超时才放弃（实测日志 20261116-191154：任务书被放弃 / exit 1）。

修法：`_run_query` 一进来就建空壳 run（`_RunState` + `_EventBus` + `ui_ask.attach`）并落盘，
规划完成后由 `execute_windows(pre_state=...)` 复用同一个 run_id 填真窗口。

本检查全离线（不连编辑器、不调模型、不真执行窗口）：把 `plan_windows` / `brief_flow` /
`execute_windows` 换成替身，断言：
1. 进入任务书人审那一刻，`data/runs` 里**已经有**这条 run 的 json；
2. 人审通道挂着的 run_id 就是这条 run；
3. `execute_windows` 收到的 `pre_run_id` / `pre_state.run_id` 与之一致（没有另起第二条 run）；
4. 复用的 `pre_state` 里已经填进规划出的窗口。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


WINDOW = {"id": "W1", "action": "compile_project", "title": "编译工程", "depends_on": []}


def main() -> int:
    import agent.run_windows as rw
    from agent import brief, ui_ask

    saved = {
        "RUNS_DIR": rw.RUNS_DIR, "EVENTS_DIR": rw.EVENTS_DIR,
        "plan_windows": rw.plan_windows, "_needs_brief": rw._needs_brief,
        "execute_windows": rw.execute_windows, "_escalate_gaps": rw._escalate_gaps,
        "brief_flow": brief.brief_flow, "materialize_scopes": brief.materialize_scopes,
    }
    recorded: dict = {}
    try:
        with tempfile.TemporaryDirectory(prefix="entry-scaffold-") as raw:
            tmp = Path(raw)
            runs = tmp / "runs"
            events = tmp / "events"
            runs.mkdir()
            events.mkdir()
            rw.RUNS_DIR = runs
            rw.EVENTS_DIR = events

            rw.plan_windows = lambda query, **k: {"source": "rules", "windows": [dict(WINDOW)]}
            rw._needs_brief = lambda planned, query: True

            def fake_brief_flow(query, windows=None, reader=None, printer=None, **k):
                # 进任务书人审的这一刻：空壳 run 必须已经落盘，且通道挂着它的 run_id。
                # run json 落在按日期分的子目录（runs/YYYYMMDD/<id>.json），要递归找。
                recorded["runs_at_brief"] = sorted(p.name for p in runs.rglob("*.json"))
                recorded["channel_run_id"] = str(getattr(ui_ask.active(), "run_id", "") or "")
                return ("confirm", {"review": {"rounds": 1, "status": "待确认"}}, tmp / "brief.md", None)

            brief.brief_flow = fake_brief_flow
            brief.materialize_scopes = lambda b, candidates=None, complete=None: ([dict(WINDOW)], [])
            rw._escalate_gaps = lambda scopes, ask, show, brief: (True, [])

            def fake_execute(query, **kwargs):
                recorded["exec_run_id"] = str(kwargs.get("pre_run_id") or "")
                pre = kwargs.get("pre_state")
                recorded["exec_pre_state_run_id"] = str(getattr(pre, "run_id", "") or "")
                recorded["exec_windows"] = [w.get("id") for w in (kwargs.get("windows") or [])]
                return (0, tmp / "run.md")

            rw.execute_windows = fake_execute

            ui_ask.reset()
            ui_ask.configure(inbox=tmp / "inbox", stdin=input,
                             printer=lambda *a, **k: None, wait=0.2)

            code, _path = rw._run_query("把这件大事做好", None, lambda *a, **k: None, lambda *a, **k: None)

            check(code == 0, f"替身执行应成功，实际 code={code}")
            check(len(recorded.get("runs_at_brief") or []) == 1,
                  f"进任务书人审前必须已有 1 条 run 记录，实际 {recorded.get('runs_at_brief')}")
            stem = str(recorded["runs_at_brief"][0])[:-5]  # 去掉 .json
            check(recorded.get("channel_run_id") == stem,
                  f"人审通道 run_id 必须就是这条 run：通道 {recorded.get('channel_run_id')!r} vs {stem!r}")
            check(recorded.get("exec_run_id") == stem,
                  f"execute_windows 必须复用同一个 pre_run_id，实际 {recorded.get('exec_run_id')!r} vs {stem!r}")
            check(recorded.get("exec_pre_state_run_id") == stem,
                  f"pre_state.run_id 必须一致，实际 {recorded.get('exec_pre_state_run_id')!r}")
            check(recorded.get("exec_windows") == ["W1"],
                  f"execute_windows 应收到规划出的窗口，实际 {recorded.get('exec_windows')}")

            # execute_windows 真身会用 set_windows 把规划窗口填进复用的 pre_state；
            # 这里直接验 set_windows（真身那段由 check_upgrade 等全链路覆盖）。
            probe = rw._RunState("20261116-999999", "q", "src", [])
            probe.set_windows([dict(WINDOW)])
            check([e["id"] for e in probe.entries()] == ["W1"],
                  f"set_windows 应把窗口填进状态，实际 {[e['id'] for e in probe.entries()]}")
    except AssertionError as exc:
        print(f"FAIL {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    finally:
        rw.RUNS_DIR = saved["RUNS_DIR"]
        rw.EVENTS_DIR = saved["EVENTS_DIR"]
        rw.plan_windows = saved["plan_windows"]
        rw._needs_brief = saved["_needs_brief"]
        rw.execute_windows = saved["execute_windows"]
        rw._escalate_gaps = saved["_escalate_gaps"]
        brief.brief_flow = saved["brief_flow"]
        brief.materialize_scopes = saved["materialize_scopes"]
        try:
            ui_ask.reset()
        except Exception:
            pass
    print("  run 记录在人审前已落盘；人审通道与 execute_windows 复用同一 run_id；窗口已填入")
    print("entry run scaffold ok（不再等任务书之后才有 run）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
