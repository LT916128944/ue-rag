# eval/run_bench.py
"""可恢复评测。默认三条仍走 eval/run_eval.py。

--check 只核对清单和两个仓库的 HEAD，不打开编辑器。
--profile pipeline 跑 judge_first 的蓝图编译。
--profile slow 再加整工程编译（会关编辑器）。
--profile agent 跑要调用模型、或会改未保存关卡的任务。
改关卡的任务不进成功率。结束后 Source 必须回到开跑时的 HEAD。
"""
import argparse
import json
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.task_schema import load_task
from config import PROJECT_ROOT, TRACES_DIR, UE_PROJECT_DIR

MANIFEST_PATH = Path(__file__).resolve().parent / "bench_manifest.json"
REPORTS_DIR = Path(__file__).resolve().parent / "reports"
RAG_ROOT = PROJECT_ROOT
ALLOWED_RESET = {"readonly", "self_restore", "unsaved_level"}
ALLOWED_PROFILE = {"pipeline", "agent", "slow"}


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def validate_manifest() -> list[str]:
    errors = []
    data = load_manifest()
    tasks = data.get("tasks") or []
    if not tasks:
        return ["bench_manifest.json 没有 tasks"]
    seen = set()
    for item in tasks:
        task_id = item.get("id") or ""
        if task_id in seen:
            errors.append(f"重复 id {task_id}")
        seen.add(task_id)
        if item.get("reset") not in ALLOWED_RESET:
            errors.append(f"{task_id} reset 非法")
        if item.get("profile") not in ALLOWED_PROFILE:
            errors.append(f"{task_id} profile 非法")
        if item.get("counts") and item.get("reset") == "unsaved_level":
            errors.append(f"{task_id} 改关卡，不能计入成功率")
        path = RAG_ROOT / item.get("path", "")
        if not path.is_file():
            errors.append(f"{task_id} 找不到 {path}")
            continue
        try:
            task = load_task(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{task_id} 读任务失败 {exc}")
            continue
        if task.get("id") != task_id:
            errors.append(f"{task_id} 与文件 id {task.get('id')} 不一致")
        if task.get("save") or task.get("pie"):
            errors.append(f"{task_id} 不许 save 或 pie")
        if item.get("profile") == "pipeline" and not task.get("judge_first"):
            errors.append(f"{task_id} pipeline 任务要 judge_first")
    default = [
        RAG_ROOT / "eval" / "tasks" / "editor-cube.json",
        RAG_ROOT / "eval" / "tasks" / "bp-gun-compile.json",
        RAG_ROOT / "eval" / "tasks" / "cpp-typo.json",
    ]
    for path in default:
        if not path.is_file():
            errors.append(f"默认三条缺少 {path.name}")
    return errors


def _git(cwd: Path, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _heads() -> dict:
    out = {}
    for name, cwd in (("ue", UE_PROJECT_DIR), ("rag", RAG_ROOT)):
        code, rev, err = _git(cwd, "rev-parse", "HEAD")
        out[name] = {"ok": code == 0, "head": rev, "error": err}
    return out


def _source_porcelain() -> str:
    code, text, err = _git(UE_PROJECT_DIR, "status", "--porcelain", "--", "Source/AIAgentProject")
    if code != 0:
        return err or "git status 失败"
    return text


def _rag_porcelain() -> str:
    code, text, _err = _git(RAG_ROOT, "status", "--porcelain")
    if code != 0:
        return ""
    lines = []
    for line in text.splitlines():
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path.startswith("eval/reports/") or path.startswith('"eval/reports/'):
            continue
        lines.append(line)
    return "\n".join(lines)


def _restore_source() -> str:
    code, _out, err = _git(UE_PROJECT_DIR, "checkout", "--", "Source/AIAgentProject")
    if code != 0:
        return err or "git checkout 失败"
    return _source_porcelain()


def _usage_from_trace(path: str) -> tuple[int | None, int | None]:
    file = Path(path)
    if not path or not file.is_file():
        return None, None
    prompt = None
    completion = None
    for line in file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        for key, bucket in (("prompt_tokens", "prompt"), ("completion_tokens", "completion")):
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if key == "prompt_tokens":
                prompt = (0 if prompt is None else prompt) + value
            else:
                completion = (0 if completion is None else completion) + value
    return prompt, completion


def _port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8000), 1.5):
            return True
    except OSError:
        return False


def _select(tasks: list[dict], profile: str) -> list[dict]:
    if profile == "pipeline":
        return [item for item in tasks if item["profile"] == "pipeline"]
    if profile == "slow":
        return [item for item in tasks if item["profile"] in ("pipeline", "slow")]
    if profile == "agent":
        return list(tasks)
    return []


def _metric(ran: bool, value) -> str:
    if not ran:
        return "未跑"
    if value is None or value == "":
        return ""
    return str(value)


def write_report(payload: dict) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = payload["stamp"]
    json_path = REPORTS_DIR / f"{stamp}-bench.json"
    md_path = REPORTS_DIR / f"{stamp}-bench.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        f"# {stamp} 评测基线",
        "",
        f"开跑 HEAD：UE `{payload['heads']['ue'].get('head', '')}` ，RAG `{payload['heads']['rag'].get('head', '')}`。",
        f"结束时 Source 干净：{payload['source_clean']}。RAG 除 `eval/reports/` 外干净：{payload['rag_clean']}。",
        f"本次 profile：{payload['profile']}。编辑器端口 8000：{payload['editor_open']}。",
        "",
        "成功率只算 `counts=true` 且已经跑过的任务。改关卡且不保存的不进分子分母。Content 与关卡不在 git 里，重载 `L_NewTest` 才回到打开时的场景；本脚本不调用 `load_level`，避免未保存对话框卡住编辑器。",
        "",
        f"计入：{payload['counted_ok']}/{payload['counted_ran']}。未跑的计入项不在分母里。",
        "",
        "| id | 类别 | 计入 | 重置 | ok | retry | tools | elapsed | prompt | completion |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in payload["rows"]:
        ran = row.get("ok") is not None
        lines.append(
            f"| {row['id']} | {row['category']} | {row['counts']} | {row['reset']} | "
            f"{_metric(ran, row.get('ok'))} | {_metric(ran, row.get('retry_count'))} | {_metric(ran, row.get('tool_calls'))} | "
            f"{_metric(ran, row.get('elapsed'))} | {_metric(ran, row.get('prompt_tokens'))} | {_metric(ran, row.get('completion_tokens'))} |"
        )
    lines.extend(["", "## 没编进清单的", ""])
    for item in payload.get("not_built") or []:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## 手工 Cursor 对照",
            "",
            "Agent 列来自上面这次运行。手工列留给同句各做一遍，不外推。",
            "",
            "| id | Agent ok | Agent token | Agent tools | 手工一次成功 | 手工改动文件 | 手工轮数 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["rows"]:
        if not row.get("human_compare"):
            continue
        token = row.get("prompt_tokens")
        ran = row.get("ok") is not None
        lines.append(
            f"| {row['id']} | {_metric(ran, row.get('ok'))} | {_metric(ran, token)} | {_metric(ran, row.get('tool_calls'))} | 待填写 | 待填写 | 待填写 |"
        )
    if payload.get("notes"):
        lines.extend(["", "## 备注", ""])
        for note in payload["notes"]:
            lines.append(f"- {note}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path


def execute(profile: str) -> dict:
    errors = validate_manifest()
    if errors:
        raise SystemExit("\n".join(errors))
    data = load_manifest()
    heads = _heads()
    if not heads["ue"]["ok"] or not heads["rag"]["ok"]:
        raise SystemExit("两个仓库都要有 HEAD 才能开跑")
    dirty = _source_porcelain()
    if dirty:
        raise SystemExit("Source/AIAgentProject 不干净，已退出\n" + dirty)
    notes = []
    editor_open = _port_open()
    selected = _select(data["tasks"], profile)
    rows = []
    if profile != "check" and not editor_open:
        notes.append("127.0.0.1:8000 没开。不启动编辑器，任务保持未跑。")
        selected = []
    ran = []
    for item in data["tasks"]:
        row = {
            "id": item["id"],
            "category": item["category"],
            "counts": bool(item["counts"]),
            "reset": item["reset"],
            "human_compare": bool(item["human_compare"]),
            "profile": item["profile"],
            "ok": None,
            "retry_count": None,
            "tool_calls": None,
            "elapsed": None,
            "prompt_tokens": None,
            "completion_tokens": None,
        }
        if item not in selected:
            rows.append(row)
            continue
        task = load_task(RAG_ROOT / item["path"])
        from agent.run_task import run_task

        result = run_task(task)
        ran.append(item["id"])
        prompt, completion = _usage_from_trace(str(result.get("trace") or ""))
        row.update(
            {
                "ok": result.get("ok"),
                "retry_count": result.get("retry_count"),
                "tool_calls": result.get("tool_calls"),
                "elapsed": result.get("elapsed"),
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "error": result.get("error"),
            }
        )
        after = _source_porcelain()
        if after:
            restored = _restore_source()
            row["ok"] = False
            row["error"] = "跑完 Source 不干净，已 git checkout 还原"
            notes.append(f"{item['id']} 留下源码改动，已还原。剩余: {restored or '干净'}")
        rows.append(row)
    counted = [row for row in rows if row["counts"] and row["ok"] is not None]
    payload = {
        "stamp": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "profile": profile,
        "heads": heads,
        "editor_open": editor_open,
        "source_clean": _source_porcelain() == "",
        "rag_clean": _rag_porcelain() == "",
        "counted_ran": len(counted),
        "counted_ok": sum(1 for row in counted if row["ok"] is True),
        "ran": ran,
        "not_built": data.get("not_built") or [],
        "notes": notes,
        "rows": rows,
    }
    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="check", choices=["check", "pipeline", "slow", "agent"])
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.profile == "check":
        errors = validate_manifest()
        if errors:
            print("\n".join(errors))
            return 1
        heads = _heads()
        data = load_manifest()
        counts = sum(1 for item in data["tasks"] if item["counts"])
        print(f"bench ok tasks={len(data['tasks'])} counts={counts}")
        print(f"ue {heads['ue'].get('head', heads['ue'].get('error'))}")
        print(f"rag {heads['rag'].get('head', heads['rag'].get('error'))}")
        payload = {
            "stamp": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "profile": "check",
            "heads": heads,
            "editor_open": _port_open(),
            "source_clean": _source_porcelain() == "",
            "rag_clean": _rag_porcelain() == "",
            "counted_ran": 0,
            "counted_ok": 0,
            "ran": [],
            "not_built": data.get("not_built") or [],
            "notes": ["--check 不调用编辑器，也不调用模型。"],
            "rows": [
                {
                    "id": item["id"],
                    "category": item["category"],
                    "counts": bool(item["counts"]),
                    "reset": item["reset"],
                    "human_compare": bool(item["human_compare"]),
                    "ok": None,
                    "retry_count": None,
                    "tool_calls": None,
                    "elapsed": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                }
                for item in data["tasks"]
            ],
        }
        path = write_report(payload)
        print(f"report: {path}")
        return 0 if payload["source_clean"] else 1
    payload = execute(args.profile)
    path = write_report(payload)
    print(
        f"counted {payload['counted_ok']}/{payload['counted_ran']} "
        f"source_clean={payload['source_clean']} report={path}"
    )
    return 0 if payload["source_clean"] and payload["rag_clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
