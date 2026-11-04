# eval/model_compare.py
"""同一三条默认任务、两个模型名各跑一遍。子进程只改 UE_LLM_MODEL。"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = Path(__file__).resolve().parent / "reports"
# 2026-10-16 开跑前从 /models 选定。当时列表是 deepseek-flash 与 deepseek-v4-pro。
# 缺省仍是 deepseek-chat（同一 base_url，探活能返回）。第二个用 flash，不改供应商。
SECOND_MODEL = "deepseek-flash"


def _configured_model() -> str:
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            if key.strip() == "UE_LLM_MODEL" and value.strip():
                return value.strip().strip('"').strip("'")
    return "deepseek-chat"


def _tokens(path: str) -> dict:
    sys.path.insert(0, str(ROOT))
    from eval.report import _sum_usage

    prompt = None
    completion = None
    seen = None
    cache = {}
    file = Path(path) if path else None
    if file is None or not file.is_file():
        return {"prompt_tokens": None, "completion_tokens": None, "model_seen": None, "cache": {}}
    for line in file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        prompt = _sum_usage(prompt, row.get("prompt_tokens"))
        completion = _sum_usage(completion, row.get("completion_tokens"))
        if row.get("model"):
            seen = row["model"]
        for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cached_tokens"):
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            cache[key] = cache.get(key, 0) + value
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "model_seen": seen,
        "cache": cache,
    }


def _only(model: str, out_path: Path) -> int:
    os.environ["UE_LLM_MODEL"] = model
    sys.path.insert(0, str(ROOT))
    from agent.run_task import run_task
    from agent.task_schema import load_task
    from config import eval_base_url
    from core.llm_call import model_name, set_base_url
    from eval.run_eval import TASKS, _source_dirty

    set_base_url(eval_base_url())
    payload = {
        "model_requested": model,
        "model_seen": model_name(),
        "base_url": eval_base_url(),
        "rows": [],
        "error": "",
    }
    dirty = _source_dirty()
    if dirty:
        payload["error"] = "Source/AIAgentProject 不干净，已停止\n" + dirty
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(payload["error"])
        return 1
    try:
        for path in TASKS:
            task = load_task(path)
            summary = run_task(task)
            usage = _tokens(str(summary.get("trace") or ""))
            payload["rows"].append({
                "id": task["id"],
                "ok": summary.get("ok"),
                "elapsed": summary.get("elapsed"),
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "model_seen": usage["model_seen"] or model_name(),
                "cache": usage["cache"],
                "error": str(summary.get("error") or ""),
                "trace": str(summary.get("trace") or ""),
            })
            print(
                f"{model} {task['id']} ok={summary.get('ok')} elapsed={summary.get('elapsed')}",
                flush=True,
            )
    except Exception as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def _md_cell(value) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "/").replace("\n", " ")


def _write_report(first: dict, second: dict, path: Path) -> None:
    sys.path.insert(0, str(ROOT))
    from config import eval_base_url

    lines = [
        "# 两个模型，同一三条任务",
        "",
        f"第一个模型是当前配置 `{first.get('model_requested')}`。",
        f"第二个模型在开跑前选定为 `{second.get('model_requested')}`。",
        f"两次都只用 `UE_LLM_MODEL`，`base_url` 都是 `{eval_base_url()}`。",
        "任务是 `eval/run_eval.py` 的默认三条，顺序不变。失败的格子留下错误原文。",
        "",
        "| 模型 | 任务 | ok | prompt_tokens | completion_tokens | 耗时 | 错误 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for payload in (first, second):
        if payload.get("error") and not payload.get("rows"):
            lines.append(
                f"| {_md_cell(payload.get('model_requested'))} |  |  |  |  |  | {_md_cell(payload.get('error'))} |"
            )
        for row in payload.get("rows") or []:
            lines.append(
                "| {model} | {id} | {ok} | {prompt} | {completion} | {elapsed} | {error} |".format(
                    model=_md_cell(row.get("model_seen") or payload.get("model_requested")),
                    id=_md_cell(row.get("id")),
                    ok=_md_cell(row.get("ok")),
                    prompt=_md_cell(row.get("prompt_tokens")),
                    completion=_md_cell(row.get("completion_tokens")),
                    elapsed=_md_cell(row.get("elapsed")),
                    error=_md_cell(row.get("error"))[:300],
                )
            )
    lines.append("")
    lines.append("缓存字段若响应里没有，上表不填命中率。逐次调用的原始记录在 `data/costs.jsonl`，不进这张表。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) >= 4 and sys.argv[1] == "--only":
        return _only(sys.argv[2], Path(sys.argv[3]))
    first_name = _configured_model()
    if SECOND_MODEL == first_name:
        print("第二个模型名和当前配置相同，已停止")
        return 2
    sys.path.insert(0, str(ROOT))
    from eval.run_eval import _source_dirty

    dirty = _source_dirty()
    if dirty:
        print("Source/AIAgentProject 不干净，已停止")
        print(dirty)
        return 1
    REPORTS.mkdir(parents=True, exist_ok=True)
    payloads = []
    for model in (first_name, SECOND_MODEL):
        out = REPORTS / f"_model-{model}.json"
        print(f"开始 {model}", flush=True)
        proc = subprocess.run(
            [sys.executable, "-u", str(Path(__file__).resolve()), "--only", model, str(out)],
            cwd=str(ROOT),
        )
        if out.is_file():
            payloads.append(json.loads(out.read_text(encoding="utf-8")))
        else:
            payloads.append({
                "model_requested": model,
                "rows": [],
                "error": f"子进程退出码 {proc.returncode}，没有写出结果",
            })
        dirty = _source_dirty()
        if dirty:
            print("跑完 Source/AIAgentProject 不干净，已停止")
            print(dirty)
            break
    while len(payloads) < 2:
        payloads.append({"model_requested": SECOND_MODEL, "rows": [], "error": "未跑"})
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report = REPORTS / f"{stamp}-models.md"
    _write_report(payloads[0], payloads[1], report)
    for model in (first_name, SECOND_MODEL):
        partial = REPORTS / f"_model-{model}.json"
        partial.unlink(missing_ok=True)
    print(f"report={report}")
    return 0 if not payloads[0].get("error") and not payloads[1].get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
