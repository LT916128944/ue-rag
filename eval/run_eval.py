# eval/run_eval.py
"""按顺序跑三条任务。Source 不干净则退出。关卡不对时 editor-cube 记失败，另外两条继续。"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.run_task import run_task
from agent.task_schema import load_task
from config import TRACES_DIR, UE_PROJECT_DIR
from core.editor_exec import exec_tool

TASKS = [
    Path(__file__).resolve().parent / "tasks" / "editor-cube.json",
    Path(__file__).resolve().parent / "tasks" / "bp-gun-compile.json",
    Path(__file__).resolve().parent / "tasks" / "cpp-typo.json",
]


def _source_dirty() -> str:
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", "Source/AIAgentProject"],
        cwd=str(UE_PROJECT_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _level_ok() -> tuple[bool, str]:
    result = exec_tool(
        "editor_toolset.toolsets.scene.SceneTools",
        "get_current_level",
        {},
        task={"allow_writes": [], "save": False, "pie": False},
    )
    if not result.get("ok"):
        return False, str(result.get("error"))
    data = result.get("data")
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return "L_NewTest" in text, text


def main() -> int:
    dirty = _source_dirty()
    if dirty:
        print("Source/AIAgentProject 不干净，已退出")
        print(dirty)
        return 1
    level_ok, level_text = _level_ok()
    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    summary_path = TRACES_DIR / f"eval-{stamp}.jsonl"
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"{'id':<18} {'ok':<6} {'retry':<6} {'tools':<6} elapsed")
    exit_code = 0
    for path in TASKS:
        task = load_task(path)
        if task["id"] == "editor-cube" and not level_ok:
            row = {
                "id": task["id"],
                "ok": False,
                "retry_count": 0,
                "tool_calls": 0,
                "elapsed": 0,
                "error": f"请打开关卡 L_NewTest。当前: {level_text}",
            }
        else:
            row = run_task(task)
        slim = {key: row.get(key) for key in ("id", "ok", "retry_count", "tool_calls", "elapsed")}
        if row.get("error"):
            slim["error"] = row["error"]
        if row.get("index_note"):
            slim["index_note"] = row["index_note"]
        with summary_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(slim, ensure_ascii=False) + "\n")
        print(f"{slim['id']:<18} {str(slim['ok']):<6} {slim['retry_count']:<6} {slim['tool_calls']:<6} {slim['elapsed']}")
        if task["id"] in ("bp-gun-compile", "cpp-typo") and not slim["ok"]:
            exit_code = 1
        if task["id"] == "editor-cube" and level_ok and not slim["ok"]:
            exit_code = 1
    print(f"trace: {summary_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
