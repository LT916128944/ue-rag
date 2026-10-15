# eval/report.py
"""读一份 trace，打印 id / ok / retry_count / tool_calls / elapsed / token 合计。"""
import argparse
import json
import sys
from pathlib import Path


def _load(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _usage_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _sum_usage(total, value):
    number = _usage_number(value)
    if number is None:
        return total
    return (0 if total is None else total) + number


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


_CLASSES = ("编译", "测试", "工具拒绝", "MCP 连不上", "超过重试", "关卡预检", "其他")


def classify_row(row: dict) -> str | None:
    """失败行归到固定类。对不上就是其他。成功行不分类。"""
    if row.get("ok") is not False:
        return None
    error = str(row.get("error") or "")
    kind = str(row.get("judge_kind") or "")
    node = str(row.get("node") or "")
    if node == "precheck" or "请打开关卡" in error:
        return "关卡预检"
    if any(token in error for token in ("WinError 10061", "10061", "拒绝连接", "Connection refused")):
        return "MCP 连不上"
    if any(token in error for token in ("write refused", "role refused", "blueprint edit disabled")):
        return "工具拒绝"
    if kind in ("cpp_build", "blueprint_compile") or "error C" in error:
        return "编译"
    if kind in ("editor_assert", "automation") or error.startswith("failed="):
        return "测试"
    try:
        retry = int(row.get("retry_count") or 0)
    except (TypeError, ValueError):
        retry = 0
    if retry >= 3:
        return "超过重试"
    return "其他"


def class_counts(rows: list[dict]) -> dict[str, int]:
    counts = {name: 0 for name in _CLASSES}
    for row in rows:
        label = classify_row(row)
        if label:
            counts[label] += 1
    return counts


def _from_steps(rows: list[dict]) -> list[dict]:
    by_id = {}
    for row in rows:
        task_id = row.get("task_id") or row.get("id")
        if not task_id:
            continue
        item = by_id.setdefault(
            task_id,
            {
                "id": task_id,
                "ok": None,
                "retry_count": 0,
                "tool_calls": 0,
                "elapsed": 0.0,
                "prompt_tokens": None,
                "completion_tokens": None,
            },
        )
        if row.get("tool"):
            item["tool_calls"] += 1
        item["retry_count"] = max(item["retry_count"], int(row.get("retry_count") or 0))
        item["elapsed"] += float(row.get("elapsed") or 0)
        if "ok" in row and row.get("node") in ("observe", "precheck"):
            item["ok"] = row.get("ok")
        item["prompt_tokens"] = _sum_usage(item["prompt_tokens"], row.get("prompt_tokens"))
        item["completion_tokens"] = _sum_usage(item["completion_tokens"], row.get("completion_tokens"))
    return list(by_id.values())


def _tasks_of(rows: list[dict]) -> list[dict]:
    if rows and "task_id" in rows[0] and "id" not in rows[0]:
        return _from_steps(rows)
    return rows


def _print_tasks(rows: list[dict]) -> None:
    print(
        f"{'id':<18} {'ok':<6} {'retry':<6} {'tools':<6} "
        f"{'elapsed':<10} {'prompt_tokens':<14} completion_tokens"
    )
    for row in rows:
        print(
            f"{str(row.get('id', '')):<18} {str(row.get('ok')):<6} "
            f"{row.get('retry_count', 0):<6} {row.get('tool_calls', 0):<6} "
            f"{str(row.get('elapsed', 0)):<10} {_cell(row.get('prompt_tokens')):<14} "
            f"{_cell(row.get('completion_tokens'))}"
        )
    ok_count = sum(1 for row in rows if row.get("ok") is True)
    print(f"tasks={len(rows)} ok={ok_count}")


def _print_classes(rows: list[dict]) -> None:
    counts = class_counts(rows)
    print("class")
    for name in _CLASSES:
        print(f"{name}\t{counts[name]}")


def _print_intent_events(rows: list[dict]) -> bool:
    """事件流文件（data/events/<run_id>.jsonl，20261015-1705 P4）：
    每句的 route_hit / understand_called 与理解器 token。不是事件流返回 False。"""
    intents = [row for row in rows if isinstance(row, dict) and row.get("type") == "intent"]
    learned = [row for row in rows if isinstance(row, dict) and row.get("type") == "learned_route"]
    if not intents and not learned:
        return False
    hits = sum(1 for row in intents if row.get("route_hit"))
    understood = sum(1 for row in intents if row.get("understand_called"))
    prompt = None
    completion = None
    for row in intents:
        prompt = _sum_usage(prompt, row.get("prompt_tokens"))
        completion = _sum_usage(completion, row.get("completion_tokens"))
    print("intent")
    print(
        f"sentences={len(intents)} route_hit={hits} understand_called={understood} "
        f"learned_routes={len(learned)} prompt_tokens={_cell(prompt)} completion_tokens={_cell(completion)}"
    )
    return True


def _file_line(path: Path) -> str:
    raw = _load(path)
    tasks = _tasks_of(raw)
    prompt = None
    completion = None
    for row in tasks:
        prompt = _sum_usage(prompt, row.get("prompt_tokens"))
        completion = _sum_usage(completion, row.get("completion_tokens"))
    ok_count = sum(1 for row in tasks if row.get("ok") is True)
    return (
        f"{path.stem:<28} {len(tasks):<6} {ok_count:<6} "
        f"{_cell(prompt):<14} {_cell(completion)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace")
    ns = parser.parse_args()
    path = Path(ns.trace)
    sys.stdout.reconfigure(encoding="utf-8")
    if path.is_dir():
        files = sorted(path.glob("*.jsonl"))
        print(f"{'file':<28} {'tasks':<6} {'ok':<6} {'prompt_tokens':<14} completion_tokens")
        for item in files:
            print(_file_line(item))
        combined = []
        for item in files:
            combined.extend(_load(item))
        _print_classes(combined)
        _print_intent_events(combined)
        return 0
    raw = _load(path)
    if not _print_intent_events(raw):
        _print_tasks(_tasks_of(raw))
        _print_classes(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
