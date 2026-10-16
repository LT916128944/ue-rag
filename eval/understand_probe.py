# eval/understand_probe.py
"""理解器固定句式跑 20 次。通过率 = validate_intent 为 ok 的次数 / 20。"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.understand import understand_once

FIXED_QUERY = "把 BP_Gun 放到场景，再把 Cube 移到 (1200, 400, 150)"
RUNS = 20
_CACHE_KEYS = ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cached_tokens")


def _cell(value) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "/")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    rows = []
    for index in range(1, RUNS + 1):
        detail = understand_once(FIXED_QUERY)
        row = {
            "n": index,
            "ok": bool(detail.get("ok")),
            "model": detail.get("model"),
            "elapsed": detail.get("elapsed"),
            "prompt_tokens": detail.get("prompt_tokens"),
            "completion_tokens": detail.get("completion_tokens"),
            "error": detail.get("error") or "",
        }
        for key in _CACHE_KEYS:
            if key in detail:
                row[key] = detail[key]
        actions = []
        intent = detail.get("intent") or {}
        if isinstance(intent, dict):
            actions = [goal.get("action") or "" for goal in intent.get("goals") or []]
        row["actions"] = actions
        rows.append(row)
        print(
            f"{index}/{RUNS} ok={row['ok']} elapsed={row['elapsed']} "
            f"prompt={row['prompt_tokens']} error={row['error']}",
            flush=True,
        )
    passed = sum(1 for row in rows if row["ok"])
    cache_rows = sum(1 for row in rows if any(key in row for key in _CACHE_KEYS))
    hit = miss = None
    for row in rows:
        if "prompt_cache_hit_tokens" in row:
            hit = (0 if hit is None else hit) + row["prompt_cache_hit_tokens"]
        if "prompt_cache_miss_tokens" in row:
            miss = (0 if miss is None else miss) + row["prompt_cache_miss_tokens"]
    if hit is None or miss is None or (hit + miss) <= 0:
        cache_rate = ""
    else:
        cache_rate = f"{hit / (hit + miss):.4f}"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = Path(__file__).resolve().parent / "reports" / f"{stamp}-understand.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# 理解器 {RUNS} 次",
        "",
        f"固定句：{FIXED_QUERY}",
        "",
        f"通过 `validate_intent`：{passed}/{RUNS}，比例 {passed / RUNS:.2f}。",
        f"模型：{rows[0]['model'] if rows else ''}。供应商仍是 DeepSeek，`base_url` 为 `https://api.deepseek.com`。",
        f"响应里带缓存字段的次数：{cache_rows}。",
        f"prompt_cache_hit_tokens 合计：{'' if hit is None else hit}。prompt_cache_miss_tokens 合计：{'' if miss is None else miss}。命中率：{cache_rate}。没有字段的合计留空，不估算。",
        "",
        "| n | ok | 耗时 | prompt_tokens | completion_tokens | 缓存 | 动作 | 错误 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        cache = {key: row[key] for key in _CACHE_KEYS if key in row}
        cache_text = json.dumps(cache, ensure_ascii=False) if cache else ""
        lines.append(
            "| {n} | {ok} | {elapsed} | {prompt} | {completion} | {cache} | {actions} | {error} |".format(
                n=row["n"],
                ok=row["ok"],
                elapsed=_cell(row["elapsed"]),
                prompt=_cell(row["prompt_tokens"]),
                completion=_cell(row["completion_tokens"]),
                cache=_cell(cache_text),
                actions=_cell("、".join(row["actions"])),
                error=_cell(row["error"]),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"passed={passed}/{RUNS}")
    print(f"report={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
