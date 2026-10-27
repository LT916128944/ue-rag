# eval/dsh_ab.py
"""DSH A/B 表：menu2 基线聚合 + dsh 人工填写模板。

只做一件事：读 data/costs.jsonl、data/traces/*.jsonl、data/runs/<日期>/<run_id>.json，
聚合三条固定任务（editor-cube / bp-gun-compile / cpp-typo）的 menu2 基线，
再生成 data/harness/dsh_ab.json 模板；dsh 侧数值由人从 DSH 会话用量填入，脚本不编。

不调模型、不连编辑器、不改别的文件；只读 data，只写 data/harness/dsh_ab.json。

匹配规则（关键词在下面的 TASKS 常量里，每个任务给若干「关键词组」）：
1. 一组内任一关键词命中即算该组命中；所有组都命中才算该任务命中。比较时统一转小写。
2. 先找 run json（data/runs/<日期>/<run_id>.json）的 query；一个都没有时，退回
   data/traces/<窗口>-<run_id>.jsonl 行里的 query（文件名带 run_id）。
3. 同一任务命中多条 run 时，取 run_id 最大（最新）的一条；不跨 run 求和。
4. prompt_tokens：该 run_id 下所有 trace 文件的 prompt_tokens 非空行求和。trace 里同一次
   模型调用会写成 context_slice 与 llm 两行、token 相同，按
   (task_id, node, prompt_tokens, completion_tokens) 去重，避免双计。
5. ok：只认 run json 的 status == "成功"；没有 run json 就是 None，不拿 trace 的 ok 顶替。
6. costs.jsonl 没有 run_id/query，不能直接归属任务；只在 run json 带 started_at/updated_at
   时按时间窗兜底 prompt_tokens，且 trace 有数时不覆盖。
7. 缺数一律 None：不填 0、不估算、不宣称谁更快。dsh_ab.json 已存在时只补缺失的
   任务/键，绝不覆盖人已填的 dsh 数值和已聚合的 menu2 数值（generated_at 可更新）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "data" / "runs"
TRACES = ROOT / "data" / "traces"
COSTS = ROOT / "data" / "costs.jsonl"
OUT = ROOT / "data" / "harness" / "dsh_ab.json"

# 任务 -> 关键词组；组内任一命中即可，所有组都要命中。
TASKS = {
    # 编辑器里挪 Cube 这个 Actor
    "editor-cube": (("cube",),),
    # 编译 BP_Gun 蓝图
    "bp-gun-compile": (("bp_gun", "枪"),),
    # C++ 改错/拼写错误后编译：必须同时是 C++ 改码语义，避免命中平台关卡的 HUD 窗
    "cpp-typo": (
        ("编译",),
        ("改 c++", "拼写", "typo", "error c", "thisfunctiondoesnotexist"),
    ),
}
NOTE = "dsh 侧数值由人从 DSH 会话用量填入；缺数留 null，脚本打 -，不估算、不宣称谁更快。"
HEADER = ("task", "menu2_prompt", "dsh_prompt", "menu2_ok", "dsh_ok")


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _load_runs() -> list[dict]:
    """data/runs 下的 run json，只要带 run_id 的。"""
    runs: list[dict] = []
    if not RUNS.is_dir():
        return runs
    for path in sorted(RUNS.glob("*/*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(item, dict) and item.get("run_id"):
            runs.append(item)
    return runs


def _load_traces() -> list[dict]:
    """data/traces 的行，附上文件名里的 run_id（<窗口>-<run_id>.jsonl）。"""
    rows: list[dict] = []
    if not TRACES.is_dir():
        return rows
    for path in sorted(TRACES.glob("*.jsonl")):
        parts = path.stem.split("-", 1)
        if len(parts) != 2:
            continue
        run_id = parts[1]
        for row in _read_jsonl(path):
            row["_run_id"] = run_id
            rows.append(row)
    return rows


def _hit(groups: tuple[tuple[str, ...], ...], text: str) -> bool:
    low = text.lower()
    return all(any(word in low for word in group) for group in groups)


def _ts(text: object) -> datetime | None:
    value = str(text or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None


def _trace_tokens(rows: list[dict], run_id: str) -> int | None:
    seen: dict[tuple, int] = {}
    for row in rows:
        if row.get("_run_id") != run_id or row.get("prompt_tokens") is None:
            continue
        key = (row.get("task_id"), row.get("node"), row["prompt_tokens"], row.get("completion_tokens"))
        seen.setdefault(key, int(row["prompt_tokens"]))
    if not seen:
        return None
    return sum(seen.values())


def _cost_tokens(run: dict) -> int | None:
    """时间窗兜底：run 的 started_at..updated_at 之间的 costs 行。没有窗就 None。"""
    start, end = _ts(run.get("started_at")), _ts(run.get("updated_at"))
    if start is None or end is None or not COSTS.is_file():
        return None
    values = []
    for row in _read_jsonl(COSTS):
        if row.get("prompt_tokens") is None or row.get("error"):
            continue
        stamp = _ts(row.get("ts"))
        if stamp is not None and start <= stamp <= end:
            values.append(int(row["prompt_tokens"]))
    return sum(values) if values else None


def baseline() -> dict[str, dict]:
    """三条任务的 menu2 基线；没数据就是 None。"""
    runs = _load_runs()
    traces = _load_traces()
    out: dict[str, dict] = {}
    for task, groups in TASKS.items():
        matched = [run for run in runs if _hit(groups, str(run.get("query") or ""))]
        run: dict = {}
        source = ""
        if matched:
            run = max(matched, key=lambda item: str(item.get("run_id")))
            source = f"run {run['run_id']}"
        else:
            hit_ids = sorted(
                {row["_run_id"] for row in traces if _hit(groups, str(row.get("query") or ""))}
            )
            if hit_ids:
                run = {"run_id": hit_ids[-1]}
                source = f"trace {hit_ids[-1]}（无 run json）"
        run_id = str(run.get("run_id") or "")
        tokens = _trace_tokens(traces, run_id) if run_id else None
        if tokens is None:
            tokens = _cost_tokens(run)
        ok = None
        if run.get("status"):
            ok = run["status"] == "成功"
        out[task] = {"prompt_tokens": tokens, "ok": ok, "source": source}
    return out


def _cell(value: object) -> str:
    """表里的单元格：缺数打 -，不是 0、不是空。"""
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def _fill(task: dict, key: str, value: dict) -> None:
    """只补缺失的键；已有非空值一律不动（人填的 dsh 数值、已聚合的 menu2 数值）。"""
    side = dict(task.get(key) or {})
    for name, fallback in (("prompt_tokens", None), ("ok", None), ("source", "")):
        if name not in side or side[name] is None or side[name] == "":
            if name in value and value[name] not in (None, ""):
                side[name] = value[name]
            elif name not in side:
                side[name] = fallback
    task[key] = side


def main() -> int:
    old: dict = {}
    if OUT.is_file():
        try:
            loaded = json.loads(OUT.read_text(encoding="utf-8"))
            old = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            old = {}
    old_tasks = old.get("tasks") if isinstance(old.get("tasks"), dict) else {}
    fresh = baseline()
    tasks: dict[str, dict] = {}
    for task in TASKS:
        item = dict(old_tasks.get(task) or {})
        _fill(item, "menu2", fresh[task])
        # dsh 侧只补键，永远不写数值
        _fill(item, "dsh", {"prompt_tokens": None, "ok": None, "source": ""})
        tasks[task] = item
    for task, item in old_tasks.items():  # 人加的任务不丢
        if task not in tasks and isinstance(item, dict):
            tasks[task] = item
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "note": old.get("note") or NOTE,
        "tasks": tasks,
    }
    for key, value in old.items():  # 别的顶层键也不丢
        if key not in payload:
            payload[key] = value
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    rows = []
    for task, item in tasks.items():
        menu2 = item.get("menu2") or {}
        dsh = item.get("dsh") or {}
        rows.append(
            [
                task,
                _cell(menu2.get("prompt_tokens")),
                _cell(dsh.get("prompt_tokens")),
                _cell(menu2.get("ok")),
                _cell(dsh.get("ok")),
            ]
        )
    widths = [len(name) for name in HEADER]
    for row in rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]
    print(" | ".join(name.ljust(width) for name, width in zip(HEADER, widths)))
    for row in rows:
        print(" | ".join(cell.ljust(width) for cell, width in zip(row, widths)))
    print(f"模板 -> {OUT.relative_to(ROOT)}；DSH 侧待人工填 3 个数。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
