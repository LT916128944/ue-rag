# eval/cancel_build.py
"""第 7 段证据：把编译等待收成 1 秒，trace 记失败；再写一条带选项的确认事件。

不关编辑器，不跑 Build.bat。生产路径的截止仍是 UE_MCP_TIMEOUT（300 秒），走同一套 run_command 和 cpp_build_verdict。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.run_windows import execute_windows
from config import DATA_DIR, UE_MCP_TIMEOUT
from core.build_job import JOB_PATH, run_command
from core.judge import cpp_build_verdict

TRACES = DATA_DIR / "traces"
REPORTS = Path(__file__).resolve().parent / "reports"


def _trace_rows(verdict: dict, elapsed: float) -> list[dict]:
    errors = verdict.get("errors") or []
    if errors:
        error = str(errors[0].get("message") or "")
    elif verdict.get("ok") is False:
        error = str(verdict.get("summary") or "")
    else:
        error = ""
    return [
        {
            "task_id": "cpp-build-cancel",
            "node": "build",
            "role": "judge",
            "tool": "cpp_build",
            "ok": verdict.get("ok") is True,
            "elapsed": round(elapsed, 3),
            "retry_count": 0,
            "prompt_tokens": None,
            "completion_tokens": None,
            "summary": verdict.get("summary") or "",
            "judge_kind": "cpp_build",
        },
        {
            "task_id": "cpp-build-cancel",
            "node": "observe",
            "role": "judge",
            "tool": "",
            "ok": verdict.get("ok") is True,
            "elapsed": 0,
            "retry_count": 0 if verdict.get("ok") else 1,
            "prompt_tokens": None,
            "completion_tokens": None,
            "judge_kind": "cpp_build",
            "error": error[:200],
        },
    ]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    started = time.perf_counter()
    outcome = run_command(argv, 1)
    verdict = cpp_build_verdict(outcome)
    elapsed = time.perf_counter() - started
    if verdict.get("ok") is not False or outcome.get("reason") != "timeout":
        print(json.dumps({"outcome": outcome, "verdict": verdict}, ensure_ascii=False))
        return 1
    TRACES.mkdir(parents=True, exist_ok=True)
    trace_path = TRACES / f"cpp-build-cancel-{stamp}.jsonl"
    rows = _trace_rows(verdict, elapsed)
    trace_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    job = json.loads(JOB_PATH.read_text(encoding="utf-8"))

    windows = [{
        "id": "W1",
        "title": "编译蓝图",
        "action": "compile_blueprint",
        "realizable": False,
        "error": "写不出唯一的蓝图 refPath，不猜。",
        "depends_on": [],
    }]
    code, run_path = execute_windows(
        "第7段确认事件：补一条不存在的蓝图路径",
        planned={"source": "rules", "windows": windows},
        windows=windows,
        brief={"review": {"rounds": 1, "status": "通过"}},
        echo=lambda *_args, **_kwargs: None,
        decide=lambda _window, _entry: "/Game/NotAReal",
    )
    run_id = run_path.stem
    events_path = DATA_DIR / "events" / f"{run_id}.jsonl"
    confirms = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("type") == "confirm"
    ]
    chosen = next(event for event in confirms if event.get("option") == "r")

    REPORTS.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS / f"{stamp}-cancel.md"
    build_row = rows[0]
    observe_row = rows[1]
    lines = [
        f"# {stamp} 编译取消与确认事件",
        "",
        "这次没有启动 `Build.bat`，也没有关编辑器。进程是 `python -c` 睡眠 30 秒，截止收成 1 秒，用来人为制造超时。生产编译仍用 `UE_MCP_TIMEOUT`，当前是 "
        f"{UE_MCP_TIMEOUT:g} 秒，走同一个 `run_command` 和 `cpp_build_verdict`。超时先记失败，不看退出码。",
        "",
        f"任务记录 `{JOB_PATH}`：pid={job.get('pid')}，started_at={job.get('started_at')}，deadline={job.get('deadline')}，reason={job.get('reason')}，returncode={job.get('returncode')}。",
        "",
        f"裁判：ok={verdict.get('ok')}，summary={verdict.get('summary')}，error={verdict.get('errors')}",
        "",
        f"trace `{trace_path.name}`：",
        "",
        "```json",
        json.dumps(build_row, ensure_ascii=False),
        json.dumps(observe_row, ensure_ascii=False),
        "```",
        "",
        f"build 行 ok={build_row['ok']}，tool={build_row['tool']}。observe 行 ok={observe_row['ok']}，judge_kind={observe_row['judge_kind']}，error={observe_row['error']}。",
        "",
        f"确认事件在 `{events_path.name}`，run `{run_id}`，这次执行退出码 {code}。带路径的那条：",
        "",
        "```json",
        json.dumps(chosen, ensure_ascii=False),
        "```",
        "",
        "选项是 `r`，补上的路径是 `/Game/NotAReal`。这条路径没有唯一蓝图名，窗口保持暂停，没有进编辑器。同一份事件里还有任务书的 `option=确认`。",
        "",
        "`ue_mcp_index.json` 里没有 Transaction 或 Undo。回滚仍是 WORKFLOW 的三句，不接编辑器 Undo。",
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"trace {trace_path}")
    print(f"event {events_path}")
    print(f"report {report_path}")
    print(f"ok={verdict.get('ok')} summary={verdict.get('summary')} confirm={chosen.get('option')} {chosen.get('path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
