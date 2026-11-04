"""编译回灌：apply_spec / edit_cpp 之后的错误原文进 fix 上下文。

20261028-124154 的人+agent 轨迹里，三连编译迭代（C2039→补 include→C1083→换 include 路径）
就是这个循环；此前它只存在于人手上——get_build_error 只落在网关（contract_server），
执行面的编译没人落记录，apply_spec 写完源码也没有一次强制编译。

这里补三件事：
- build_and_record()：跑编译裁判（core.judge 自落 last_cpp_build.json）；
- apply_spec_loop()：写完 spec 文件 → 强制 build → 失败则把 get_build_error 的错误原文
  当 fix 上下文进下一轮，编译轮次有上限（≤3，含首轮）；
- run_fix()：默认修复器，把错误原文拼成一轮图任务（read_source + apply_unified_diff），
  编译裁判失败会在图内带错误原文再改。

不改写策略：fix 任务只允许 read_source / apply_unified_diff，不碰编辑器。
"""
from __future__ import annotations

import json
import re

# 编译轮次上限（含首轮）。预算烧尽就诚实失败，不假成功。
BUILD_ROUNDS_LIMIT = 3


def build_and_record(task: dict | None = None) -> dict:
    """跑一次真编译并落记录。返回裁判 {ok, kind, summary, errors}。"""
    from core.judge import judge

    return judge({"kind": "cpp_build"}, task=task)


def last_errors() -> list[dict]:
    """get_build_error 的错误原文。没有记录返回空表，不编。"""
    from core.contract_tools import get_build_error

    result = get_build_error()
    if not isinstance(result, dict):
        return []
    return [item for item in (result.get("errors") or []) if isinstance(item, dict)]


def fix_context(errors: list[dict], limit: int = 6) -> str:
    """错误原文 → 给 fix 轮的上下文文本。只留原文，不加解释。"""
    lines = []
    for err in (errors or [])[:limit]:
        if not isinstance(err, dict):
            continue
        file_name = str(err.get("file") or "")
        line_no = err.get("line") or 0
        message = str(err.get("message") or "").strip()
        if not message:
            continue
        where = f"{file_name}({line_no})" if file_name else ""
        lines.append(f"{where}: {message}" if where else message)
    return "\n".join(lines)


def fix_task(errors: list[dict], *, wid: str, query_hint: str = "") -> dict:
    """错误原文 → 一轮图任务。require_build 由 verify 带 cpp_build 隐含，
    图内 build→fix→build 循环的 max_retries 再给 2 轮，总编译轮次 ≤3。"""
    from config import task_policy

    context = fix_context(errors)
    query = "刚写入或修改的 C++ 编译失败。只修下面这些错误，不改别处：\n" + context
    if query_hint:
        query = f"{query_hint}\n{query}"
    query += (
        "\n先 read_source 看相关文件，再用 apply_unified_diff 修。"
        "每段 diff 的旧行来自最近一次 read_source 的返回，写码后行号会偏移，下一段前重新读。"
        "diff 写完就停，不要调用工具，让裁判重新编译。达到重试上限就停，不要换目标。"
    )
    return {
        "id": f"{wid}fix" if wid else "fix",
        "title": "修编译错误",
        "query": query,
        "verify": [{"kind": "cpp_build"}],
        "allow_writes": ["read_source", "apply_unified_diff"],
        "save": False,
        "pie": False,
        # 图内只给一次修复 + 一次重编译；总轮次上限由外层 apply_spec_loop 控制（≤3）。
        "max_retries": 0,
        **task_policy(save=False, pie=False),
    }


def run_fix(errors: list[dict], *, wid: str = "", query_hint: str = "") -> dict:
    """默认修复器：跑一轮图任务。返回该任务的摘要（含最终编译裁判）。"""
    from agent.run_task import new_thread_id, run_task

    task = fix_task(errors, wid=wid, query_hint=query_hint)
    try:
        return run_task(task, thread_id=new_thread_id())
    except Exception as exc:
        return {"id": task.get("id"), "ok": False, "error": f"{type(exc).__name__}: {exc}",
                "summary": f"{type(exc).__name__}: {exc}"}


def apply_spec_loop(
    files: list[dict],
    *,
    wid: str = "",
    write=None,
    build=None,
    fix=None,
) -> dict:
    """apply_spec 的落地 + 编译回灌循环。

    write(files) 落盘；build() 跑编译裁判；fix(errors) 拿错误原文修源码。
    三个都可注入：eval 里注入假的，真跑用默认实现。编译轮次 ≤BUILD_ROUNDS_LIMIT，
    烧尽还没过就 ok=False 带错误原文，不假成功。
    """
    writer = write if write is not None else _default_write
    build_fn = build if build is not None else build_and_record
    written = writer(files)
    note = "已写入 " + "、".join(written)
    verdict = build_fn()
    rounds = 1
    fixes: list[dict] = []
    while not verdict.get("ok") and rounds < BUILD_ROUNDS_LIMIT:
        errors = [item for item in (verdict.get("errors") or []) if isinstance(item, dict)]
        if not errors:
            errors = last_errors()
        if not errors:
            # 连错误原文都拿不到：fix 没有上下文，再编译也只会是同一条结果，停。
            break
        if fix is None:
            summary = run_fix(errors, wid=wid)
        else:
            summary = fix(errors)
        row = {"round": rounds + 1, "errors": len(errors), "ok": bool(summary.get("ok"))}
        if summary.get("error"):
            row["error"] = str(summary["error"])[:200]
        fixes.append(row)
        rounds += 1
        if not summary.get("ok"):
            # 修复器没走通时停——但只对「没写码」那种失败停（20261028 载具实测：
            # fix 写了码、内部重编译仍报 C2027，外层却 break，白烧了预算内的回灌轮）。
            # 错误原文是编译错误形状 ⇒ 修复器真动了代码，把新错误喂下一轮继续回灌。
            fixer_error = str(summary.get("error") or "")
            if not re.search(r"error C\d+|error LNK\d+|fatal error", fixer_error):
                break
            verdict = {"ok": False, "kind": "cpp_build", "summary": fixer_error[:200],
                       "errors": [{"file": "", "line": 0, "message": fixer_error[:300]}]}
            continue
        verdict = build_fn()
    out = {
        "id": wid or "apply_spec",
        "ok": bool(verdict.get("ok")),
        "build_rounds": rounds,
        "fixes": fixes,
    }
    if verdict.get("ok"):
        out["summary"] = f"{note}，编译通过（{rounds} 轮）"
        out["index_note"] = out["summary"]
        out["build"] = verdict
        out["test"] = verdict
        out["judge_kind"] = "cpp_build"
        return out
    errors = [item for item in (verdict.get("errors") or []) if isinstance(item, dict)]
    first = str((errors[0].get("message") if errors else "") or verdict.get("summary") or "编译失败")
    out["summary"] = f"{note}，编译失败：{first}"
    out["error"] = first
    out["index_note"] = out["summary"]
    out["build"] = verdict
    out["build_errors"] = errors
    return out


def _default_write(files: list[dict]) -> list[str]:
    from config import UE_SOURCE_ROOT

    written = []
    for item in files:
        path = UE_SOURCE_ROOT / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["content"], encoding="utf-8")
        written.append(item["path"])
    return written