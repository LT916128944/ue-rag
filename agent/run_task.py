# agent/run_task.py
"""跑一份任务 JSON。退出码 0 表示裁判通过。"""
import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.checkpoint.sqlite import SqliteSaver

from agent.graph import builder
from agent.roles import trace_role
from agent.task_schema import load_task
from config import PROJECT_ROOT, TRACES_DIR, UE_PROJECT_DIR, UE_SOURCE_ROOT
from core.editor_exec import exec_tool
from core.error_memory import remember_fixed
from core.history_log import append_history
from core.task_log import append_task_record

TYPO_NEEDLE = "void ATestActor::TestPrintDebugInfo()\n{\n}"
TYPO_BODY = "void ATestActor::TestPrintDebugInfo()\n{\n\tThisFunctionDoesNotExist();\n}"


def _initial_state(task: dict) -> dict:
    verify = task.get("verify") or []
    return {
        "messages": [{"role": "user", "content": task["query"]}],
        "user_query": task["query"],
        "available_tools": [],
        "tool_calls": [],
        "tool_results": [],
        "retry_count": 0,
        "is_completed": False,
        "require_build": bool(verify),
        "build_result": "",
        "test_result": "",
        "fix_plan": "",
        "last_error": "",
        "final_success": False,
        "task": task,
        "max_retries": int(task.get("max_retries") or 3),
        "build_judge": {},
        "test_judge": {},
        "source_written": False,
        "tools_used": [],
        "failure_log": [],
        "llm_usage": {},
        "step_log": [],
    }


def _level_text() -> tuple[bool, str]:
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
    return True, text


def _snapshot_typo(task: dict) -> tuple[Path, bytes] | None:
    if not task.get("inject_typo"):
        return None
    rel = task.get("typo_path") or "Private/TestActor.cpp"
    path = (UE_SOURCE_ROOT / rel).resolve()
    original = path.read_bytes()
    text = original.decode("utf-8")
    norm = text.replace("\r\n", "\n")
    if "ThisFunctionDoesNotExist" in norm:
        raise RuntimeError("typo already present, refuse to stack another")
    if TYPO_NEEDLE not in norm:
        raise RuntimeError("TestPrintDebugInfo body not found")
    updated = norm.replace(TYPO_NEEDLE, TYPO_BODY, 1)
    if "\r\n" in text:
        updated = updated.replace("\n", "\r\n")
    path.write_bytes(updated.encode("utf-8"))
    return path, original


def _restore(saved: tuple[Path, bytes] | None) -> None:
    if saved is None:
        return
    path, original = saved
    path.write_bytes(original)


def _git_clean(rel: str) -> bool:
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", rel],
        cwd=str(UE_PROJECT_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return False
    return proc.stdout.strip() == ""


def _run_script(name: str) -> bool:
    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / name)],
        cwd=str(PROJECT_ROOT),
    )
    return proc.returncode == 0


def _reexport_cpp() -> bool:
    """走 shortcuts/cpp.bat，避免每次手工刷 C++ 索引。nopause 给闭环用。"""
    bat = PROJECT_ROOT / "shortcuts" / "cpp.bat"
    proc = subprocess.run(
        ["cmd.exe", "/d", "/c", str(bat), "nopause"],
        cwd=str(PROJECT_ROOT),
    )
    return proc.returncode == 0


def new_thread_id() -> str:
    return datetime.now().strftime("task_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]


def _run_direct(task: dict, thread_id: str, on_row=None) -> dict:
    """不进图。消息历史留在这一扇里，调用方只拿走摘要。"""
    from agent.sequence import _run_action

    try:
        result = _run_action(task)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if on_row is not None:
        try:
            on_row({
                "task_id": task.get("id"),
                "node": "direct",
                "tool": str(task.get("action") or ""),
                "ok": bool(result.get("ok")),
                "elapsed": 0,
                "retry_count": 0,
            })
        except Exception:
            pass
    text = str(result.get("index_note") or result.get("error") or "")
    summary = {
        "id": task.get("id"),
        "ok": bool(result.get("ok")),
        "retry_count": 0,
        "tool_calls": 0,
        "elapsed": 0,
        "index_note": text,
        "summary": text,
        "thread_id": thread_id,
        "trace": "",
    }
    if result.get("verdict"):
        summary["judge"] = result["verdict"]
    if result.get("ref"):
        summary["ref"] = result["ref"]
    if "in_level" in result:
        summary["in_level"] = result["in_level"]
    if not summary["ok"]:
        summary["error"] = str(result.get("error") or text)
    return summary


def run_task(task: dict, trace_path: Path | None = None, thread_id: str | None = None, on_row=None) -> dict:
    from agent.catalog import DIRECT_ACTIONS

    if str(task.get("action") or "") in DIRECT_ACTIONS:
        return _run_direct(task, thread_id or new_thread_id(), on_row=on_row)
    saved = None
    started = time.perf_counter()
    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    if trace_path is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        trace_path = TRACES_DIR / f"{task['id']}-{stamp}.jsonl"
    rows = []
    editor_was_closed = False

    def emit(row: dict) -> None:
        row.setdefault("role", trace_role(str(row.get("node") or ""), task))
        rows.append(row)
        with trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        if on_row is not None:
            try:
                on_row(row)
            except Exception:
                pass

    try:
        if task.get("supervised"):
            emit({"task_id": task["id"], "node": "supervise", "role": "supervisor", "tool": "", "ok": True, "elapsed": 0, "retry_count": 0, "prompt_tokens": None, "completion_tokens": None})
        if task.get("require_level"):
            ok, text = _level_text()
            if not ok or task["require_level"] not in text:
                message = f"请打开关卡 {task['require_level']}。当前: {text}。本任务不切换关卡、不保存。"
                emit({"task_id": task["id"], "node": "precheck", "tool": "get_current_level", "ok": False, "elapsed": 0, "retry_count": 0, "prompt_tokens": None, "completion_tokens": None, "error": message})
                return {"id": task["id"], "ok": False, "retry_count": 0, "tool_calls": 0, "elapsed": 0, "index_note": "未更新", "error": message, "trace": str(trace_path)}
        from core.editor_launch import task_touches_cpp
        if task_touches_cpp(task):
            from core.editor_launch import close_editor
            closed = close_editor()
            editor_was_closed = bool(closed.get("closed"))
            if not closed.get("ok"):
                message = str(closed.get("note") or "编辑器未能关闭")
                emit({"task_id": task["id"], "node": "precheck", "tool": "close_editor", "ok": False, "elapsed": 0, "retry_count": 0, "prompt_tokens": None, "completion_tokens": None, "error": message})
                return {"id": task["id"], "ok": False, "retry_count": 0, "tool_calls": 0, "elapsed": 0, "index_note": "未更新", "error": message, "trace": str(trace_path)}
        if task.get("inject_typo"):
            rel = "Source/AIAgentProject/" + (task.get("typo_path") or "Private/TestActor.cpp")
            if not _git_clean(rel):
                # 没有 git 时用文件快照还原，不因此拒绝。有 git 且不干净则停。
                probe = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=str(UE_PROJECT_DIR), capture_output=True, text=True)
                if probe.returncode == 0:
                    message = f"git status -- {rel} 不干净，已停止"
                    return {"id": task["id"], "ok": False, "retry_count": 0, "tool_calls": 0, "elapsed": 0, "index_note": "未更新", "error": message, "trace": str(trace_path)}
            saved = _snapshot_typo(task)

        thread_id = thread_id or new_thread_id()
        emit({
            "task_id": task["id"],
            "node": "task",
            "tool": "",
            "ok": True,
            "elapsed": 0,
            "retry_count": 0,
            "prompt_tokens": None,
            "completion_tokens": None,
            "query": task.get("query") or "",
        })
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 80}
        final_state = None
        retry = 0
        t_node = time.perf_counter()
        with SqliteSaver.from_conn_string(str(PROJECT_ROOT / "agent_checkpoints.db")) as checkpointer:
            graph = builder.compile(checkpointer=checkpointer)
            for mode, chunk in graph.stream(_initial_state(task), config=config, stream_mode=["updates", "values"]):
                if mode == "values":
                    final_state = chunk
                    retry = chunk.get("retry_count", retry)
                    continue
                if mode != "updates" or not chunk:
                    continue
                elapsed = time.perf_counter() - t_node
                t_node = time.perf_counter()
                for node_name, update in chunk.items():
                    if not update:
                        continue
                    usage = update.get("llm_usage") or {}
                    logs = update.get("step_log") or [{}]
                    for item in logs:
                        row = {
                            "task_id": task["id"],
                            "node": node_name,
                            "tool": (item or {}).get("tool", ""),
                            "ok": (item or {}).get("ok", update.get("final_success")),
                            "elapsed": round(elapsed, 3),
                            "retry_count": update.get("retry_count", retry),
                            "prompt_tokens": usage.get("prompt_tokens"),
                            "completion_tokens": usage.get("completion_tokens"),
                        }
                        if (item or {}).get("judge_kind"):
                            row["judge_kind"] = item["judge_kind"]
                        if (item or {}).get("summary"):
                            row["summary"] = str(item["summary"])[:500]
                        if (item or {}).get("error"):
                            row["error"] = str(item["error"])[:200]
                        emit(row)

        ok = bool(final_state and final_state.get("final_success"))
        tools = list((final_state or {}).get("tools_used") or [])
        history_written = False
        index_note = "未更新"
        wrote = False
        keep = bool(task.get("keep_source"))
        try:
            if ok and (final_state or {}).get("failure_log") and tools:
                errors = []
                for item in final_state["failure_log"]:
                    errors.extend(item.get("errors") or [])
                remember_fixed(errors, tools)
                history_written = append_history(errors, tools) > 0
            wrote = bool((final_state or {}).get("source_written"))
            keep = bool(task.get("keep_source"))
            exported_cpp = False
            if ok and wrote and (not task.get("inject_typo") or keep):
                append_task_record(task, tools)
                exported_cpp = True
            exported_docs = _run_script("pipelines/export_docs.py") if history_written else False
            if exported_cpp:
                exported_cpp = _reexport_cpp()
                index_note = "C++ 索引已更新" if exported_cpp else "未更新"
            elif exported_docs:
                ingested = _run_script("pipelines/ingest_assets.py")
                index_note = "历史已入库" if ingested else "未更新"
        except OSError as exc:
            print(f"落盘失败: {exc}")
            index_note = "未更新"
        editor_note = ""
        launch_ok = True
        reopen = editor_was_closed or (
            ok
            and task.get("launch_editor")
            and (wrote or task.get("launch_without_source"))
            and (not task.get("inject_typo") or keep)
        )
        if reopen:
            if ok and (task.get("wait_mcp") or task.get("launch_without_source")):
                from core.editor_launch import ensure_editor
                launched = ensure_editor()
                editor_note = str(launched.get("note") or "")
                launch_ok = bool(launched.get("ok"))
            else:
                from core.editor_launch import launch_editor
                editor_note = str(launch_editor().get("note") or "")
        if ok and not launch_ok:
            ok = False
        elapsed = time.perf_counter() - started
        summary = {
            "id": task["id"],
            "ok": ok,
            "retry_count": int((final_state or {}).get("retry_count") or 0),
            "tool_calls": len([name for name in tools if name]),
            "elapsed": round(elapsed, 3),
            "index_note": index_note,
            "trace": str(trace_path),
            "thread_id": thread_id,
            "build": (final_state or {}).get("build_judge"),
            "test": (final_state or {}).get("test_judge"),
            "editor": editor_note,
        }
        if (final_state or {}).get("source_written"):
            summary["source_written"] = True
        if summary["ok"] and summary.get("index_note") in ("", "未更新"):
            test = summary.get("test") or {}
            build = summary.get("build") or {}
            text = ""
            if isinstance(test, dict) and test.get("kind") in ("automation", "editor_assert"):
                text = str(test.get("summary") or "")
            elif isinstance(build, dict) and build.get("ok"):
                text = str(build.get("summary") or "")
            summary["summary"] = (text or "裁判通过")[:500]
        if not summary["ok"] and not summary.get("error"):
            build = summary.get("build") or {}
            if isinstance(build, dict) and build.get("ok") is False:
                errs = build.get("errors") or []
                text = str(errs[0].get("message") or "") if errs else ""
                summary["error"] = (text or str(build.get("summary") or ""))[:500]
            else:
                last = str((final_state or {}).get("last_error") or "")
                summary["error"] = (last or "图结束时没有完成")[:500]
            if summary.get("index_note") in ("", "未更新"):
                summary["index_note"] = summary["error"]
                summary["summary"] = summary["error"]
        return summary
    except Exception as exc:
        if editor_was_closed:
            from core.editor_launch import launch_editor
            launch_editor()
        build = (final_state or {}).get("build_judge") if isinstance(final_state, dict) else {}
        errs = build.get("errors") or [] if isinstance(build, dict) else []
        text = str(errs[0].get("message") or "") if errs else ""
        if not text and isinstance(build, dict):
            text = str(build.get("summary") or "")
        if not text:
            text = f"{type(exc).__name__}: {exc}"
        return {
            "id": task.get("id"),
            "ok": False,
            "retry_count": 0,
            "tool_calls": 0,
            "elapsed": round(time.perf_counter() - started, 3),
            "index_note": text[:500],
            "summary": text[:500],
            "error": text[:500],
            "trace": str(trace_path),
        }
    finally:
        if not task.get("keep_source"):
            _restore(saved)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="")
    parser.add_argument("--query", default="")
    ns = parser.parse_args()
    if bool(ns.task) == bool(ns.query):
        print("请只提供 --task 或 --query 其中一个")
        return 2
    if ns.query:
        from agent.sequence import run_sequence
        sys.stdout.reconfigure(encoding="utf-8")
        code, path = run_sequence(ns.query, lambda _step, _summary: "stop")
        print(f"记录：{path}")
        return code
    else:
        task = load_task(ns.task)
    summary = run_task(task)
    show = {key: summary.get(key) for key in ("id", "ok", "retry_count", "tool_calls", "elapsed", "index_note")}
    show["build"] = summary.get("build")
    show["test"] = summary.get("test")
    if summary.get("editor"):
        show["editor"] = summary["editor"]
    if summary.get("error"):
        show["error"] = summary["error"]
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(show, ensure_ascii=False, indent=2))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
