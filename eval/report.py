# eval/report.py
"""读一份 trace，打印 id / ok / retry_count / tool_calls / elapsed / token 合计。
目录再加 P50/P95、平均 token、重试、已有失败分类，并写出一页静态 HTML。"""
import argparse
import html
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.failure_class import FAILURE_CLASSES, failure_code, failure_label


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


_CLASSES = tuple(label for _code, label in FAILURE_CLASSES)


def classify_row(row: dict) -> str | None:
    """失败行归到固定类。对不上就是其他。成功行不分类。trace 里已有的枚举优先。"""
    return failure_label(row)


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


def percentile(values: list[float], p: float) -> float | None:
    """线性插值。空列表留空，不填 0。"""
    if not values:
        return None
    ordered = sorted(float(item) for item in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (p / 100.0)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def route_counts(events_dir: Path) -> dict:
    """只数 run_start。L0 是 rules 与 learned，理解器是 understood，老规划者是 planner。"""
    counts = {
        "l0": 0,
        "rules": 0,
        "learned": 0,
        "understood": 0,
        "planner": 0,
        "other": 0,
        "runs": 0,
    }
    if not events_dir.is_dir():
        return counts
    for path in sorted(events_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("type") != "run_start":
                continue
            source = str(row.get("source") or "")
            counts["runs"] += 1
            if source in ("rules", "learned"):
                counts["l0"] += 1
                counts[source] += 1
            elif source == "understood":
                counts["understood"] += 1
            elif source == "planner":
                counts["planner"] += 1
            else:
                counts["other"] += 1
    return counts


def _cache_totals(rows: list[dict]) -> dict:
    hit = None
    miss = None
    cached = None
    n = 0
    for row in rows:
        h = _num(row.get("prompt_cache_hit_tokens")) if "prompt_cache_hit_tokens" in row else None
        m = _num(row.get("prompt_cache_miss_tokens")) if "prompt_cache_miss_tokens" in row else None
        c = _num(row.get("cached_tokens")) if "cached_tokens" in row else None
        if h is None and m is None and c is None:
            continue
        n += 1
        if h is not None:
            hit = (0 if hit is None else hit) + h
        if m is not None:
            miss = (0 if miss is None else miss) + m
        if c is not None:
            cached = (0 if cached is None else cached) + c
    rate = None
    if hit is not None and miss is not None and (hit + miss) > 0:
        rate = hit / (hit + miss)
    return {"hit": hit, "miss": miss, "cached": cached, "rows": n, "rate": rate}


def _mean(total, count: int):
    if total is None or count <= 0:
        return None
    return total / count


def summarize_traces(raw_rows: list[dict], tasks: list[dict], routes: dict) -> dict:
    elapsed = []
    for task in tasks:
        number = _num(task.get("elapsed"))
        if number is not None:
            elapsed.append(float(number))
    prompt = None
    completion = None
    prompt_n = 0
    completion_n = 0
    retries = 0
    for task in tasks:
        retries += int(task.get("retry_count") or 0)
        p = _num(task.get("prompt_tokens"))
        c = _num(task.get("completion_tokens"))
        if p is not None:
            prompt = (0 if prompt is None else prompt) + p
            prompt_n += 1
        if c is not None:
            completion = (0 if completion is None else completion) + c
            completion_n += 1
    return {
        "tasks": len(tasks),
        "ok": sum(1 for task in tasks if task.get("ok") is True),
        "p50": percentile(elapsed, 50),
        "p95": percentile(elapsed, 95),
        "avg_prompt": _mean(prompt, prompt_n),
        "avg_completion": _mean(completion, completion_n),
        "prompt_tasks": prompt_n,
        "completion_tasks": completion_n,
        "retries": retries,
        "classes": class_counts(raw_rows),
        "routes": routes,
        "cache": _cache_totals(raw_rows),
    }


def _fmt(value, digits: int = 3) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def _print_directory_stats(stats: dict) -> None:
    print("latency")
    print(f"p50\t{_fmt(stats.get('p50'))}")
    print(f"p95\t{_fmt(stats.get('p95'))}")
    print("tokens")
    print(f"avg_prompt_tokens\t{_fmt(stats.get('avg_prompt'))}")
    print(f"avg_completion_tokens\t{_fmt(stats.get('avg_completion'))}")
    print(f"retries\t{stats.get('retries')}")
    routes = stats.get("routes") or {}
    print("route")
    print(f"L0\t{routes.get('l0', 0)}")
    print(f"rules\t{routes.get('rules', 0)}")
    print(f"learned\t{routes.get('learned', 0)}")
    print(f"understood\t{routes.get('understood', 0)}")
    print(f"planner\t{routes.get('planner', 0)}")
    print(f"other\t{routes.get('other', 0)}")
    print(f"runs\t{routes.get('runs', 0)}")
    cache = stats.get("cache") or {}
    print("cache")
    print(f"prompt_cache_hit_tokens\t{_fmt(cache.get('hit'), 0) if cache.get('hit') is not None else ''}")
    print(f"prompt_cache_miss_tokens\t{_fmt(cache.get('miss'), 0) if cache.get('miss') is not None else ''}")
    print(f"cached_tokens\t{_fmt(cache.get('cached'), 0) if cache.get('cached') is not None else ''}")
    print(f"rate\t{_fmt(cache.get('rate'), 4) if cache.get('rate') is not None else ''}")


def _cell_html(value) -> str:
    text = _fmt(value) if not isinstance(value, str) else value
    if text == "":
        return "<td></td>"
    return f"<td>{html.escape(text)}</td>"


def render_html(stats: dict, *, trace_dir: str = "") -> str:
    """一页静态 HTML。缓存命中率只在响应带回字段时填数字。"""
    routes = stats.get("routes") or {}
    runs = int(routes.get("runs") or 0)
    l0 = int(routes.get("l0") or 0)
    l0_rate = "" if runs <= 0 else _fmt(l0 / runs, 4)
    cache = stats.get("cache") or {}
    classes = stats.get("classes") or {}
    class_rows = "".join(
        f"<tr><td><code>{html.escape(code)}</code></td><td>{html.escape(label)}</td><td>{classes.get(label, 0)}</td></tr>"
        for code, label in FAILURE_CLASSES
    )
    cache_rate = "" if cache.get("rate") is None else _fmt(cache.get("rate"), 4)
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>观测</title>
<style>
body {{ font-family: sans-serif; margin: 2rem; color: #1a1a1a; }}
table {{ border-collapse: collapse; margin: 0.5rem 0 1.5rem; }}
td, th {{ border: 1px solid #ccc; padding: 0.3rem 0.7rem; text-align: left; }}
h1 {{ font-size: 1.4rem; }}
h2 {{ font-size: 1.05rem; margin-top: 1.6rem; }}
.note {{ max-width: 42rem; line-height: 1.5; }}
</style>
</head>
<body>
<h1>观测</h1>
<p>数据来自已有 trace{html.escape(trace_dir)}。没有采集服务。空着的格子是这次没有读数。</p>
<table>
<tr><th>任务数</th>{_cell_html(stats.get("tasks"))}</tr>
<tr><th>ok</th>{_cell_html(stats.get("ok"))}</tr>
<tr><th>耗时 P50</th>{_cell_html(stats.get("p50"))}</tr>
<tr><th>耗时 P95</th>{_cell_html(stats.get("p95"))}</tr>
<tr><th>平均 prompt_tokens</th>{_cell_html(stats.get("avg_prompt"))}</tr>
<tr><th>平均 completion_tokens</th>{_cell_html(stats.get("avg_completion"))}</tr>
<tr><th>有 prompt 读数的任务</th>{_cell_html(stats.get("prompt_tasks"))}</tr>
<tr><th>重试合计</th>{_cell_html(stats.get("retries"))}</tr>
</table>
<h2>失败分类</h2>
<table>
<tr><th>枚举</th><th>类</th><th>次数</th></tr>
{class_rows}
</table>
<h2>路由</h2>
<p>只数 <code>data/events</code> 里的 <code>run_start</code>。L0 是手写规则和已学句式，不叫模型。</p>
<table>
<tr><th>L0 直接归地</th>{_cell_html(routes.get("l0"))}</tr>
<tr><th>其中 rules</th>{_cell_html(routes.get("rules"))}</tr>
<tr><th>其中 learned</th>{_cell_html(routes.get("learned"))}</tr>
<tr><th>L1 理解器</th>{_cell_html(routes.get("understood"))}</tr>
<tr><th>老规划者</th>{_cell_html(routes.get("planner"))}</tr>
<tr><th>其他 source</th>{_cell_html(routes.get("other"))}</tr>
<tr><th>run_start</th>{_cell_html(runs)}</tr>
<tr><th>L0 占比</th>{_cell_html(l0_rate)}</tr>
</table>
<h2>缓存</h2>
<table>
<tr><th>prompt_cache_hit_tokens</th>{_cell_html(cache.get("hit"))}</tr>
<tr><th>prompt_cache_miss_tokens</th>{_cell_html(cache.get("miss"))}</tr>
<tr><th>cached_tokens</th>{_cell_html(cache.get("cached"))}</tr>
<tr><th>命中率</th>{_cell_html(cache_rate)}</tr>
<tr><th>带回缓存字段的行</th>{_cell_html(cache.get("rows"))}</tr>
</table>
<h2>本地推理</h2>
<p class="note">Ollama 适合在这一台机器上先看一个小模型的延迟，不放进现在这条闭环：工具调用和 strict JSON 还没在默认三条任务上量过。vLLM 适合多卡把同一个权重做成服务，本机没有这组卡，也不把引擎和推理服务放进同一个进程。两条都不部署。换模型仍然只改 <code>UE_LLM_MODEL</code>，供应商和 <code>base_url</code> 不动。</p>
</body>
</html>
"""


def write_observability_html(stats: dict, trace_dir: Path, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = reports_dir / f"{stamp}-observability.html"
    path.write_text(render_html(stats, trace_dir=f"：{trace_dir}"), encoding="utf-8")
    return path


_REFUSAL_TOKENS = ("role refused", "write refused", "blueprint edit disabled")


def _refusal_text(row: dict) -> bool:
    error = str(row.get("error") or "")
    return any(token in error for token in _REFUSAL_TOKENS)


def compute_scoreboard(rows: list[dict], source: str = "") -> dict:
    """纯函数：tasks / ok / first_pass / prompt_tokens_per_ok / tool_refused / source。

    输入是现有 trace 行（任务行或步骤行）。没有 usage 的字段留 null，不填 0，不估算金额。
    first_pass：该任务 ok 且 retry 为 0，且没有拒绝事件（安全越界是硬门槛，不计入首次通过率）。
    tool_refused：failure_class 为 tool_refused 的次数优先；没有分类时按 trace 里拒绝事件文本计。
    """
    tasks = _tasks_of(rows)
    refused_by_task = {}
    primary = 0
    fallback = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if failure_code(row) == "tool_refused":
            primary += 1
        elif _refusal_text(row):
            fallback += 1
        else:
            continue
        key = str(row.get("task_id") or row.get("id") or "")
        refused_by_task[key] = refused_by_task.get(key, 0) + 1
    tool_refused = primary if primary > 0 else fallback
    ok = 0
    first_pass = 0
    prompt = None
    for task in tasks:
        if task.get("ok") is not True:
            continue
        ok += 1
        try:
            retry = int(task.get("retry_count") or 0)
        except (TypeError, ValueError):
            retry = 0
        if retry == 0 and refused_by_task.get(str(task.get("id") or ""), 0) == 0:
            first_pass += 1
        number = _num(task.get("prompt_tokens"))
        if number is not None:
            prompt = (0 if prompt is None else prompt) + number
    return {
        "tasks": len(tasks),
        "ok": ok,
        "first_pass": first_pass,
        "prompt_tokens_per_ok": (prompt / ok) if (ok > 0 and prompt is not None) else None,
        "tool_refused": tool_refused,
        "source": str(source or ""),
    }


def write_scoreboard(rows: list[dict], source) -> Path:
    """现有命令把记分板写进 data/harness/scoreboard.json。"""
    from config import DATA_DIR

    board = compute_scoreboard(rows, source=str(source))
    target = DATA_DIR / "harness" / "scoreboard.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(board, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


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
        root = Path(__file__).resolve().parent.parent
        tasks = []
        for item in files:
            tasks.extend(_tasks_of(_load(item)))
        stats = summarize_traces(combined, tasks, route_counts(root / "data" / "events"))
        _print_directory_stats(stats)
        html_path = write_observability_html(stats, path, Path(__file__).resolve().parent / "reports")
        print(f"html={html_path}")
        print(f"scoreboard={write_scoreboard(combined, path)}")
        return 0
    raw = _load(path)
    if not _print_intent_events(raw):
        _print_tasks(_tasks_of(raw))
        _print_classes(raw)
        print(f"scoreboard={write_scoreboard(raw, path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
