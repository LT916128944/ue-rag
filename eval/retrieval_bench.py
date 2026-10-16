# eval/retrieval_bench.py
"""题集上的 Recall@5 和单次查询耗时。

四列：纯向量、BM25-only、路由+BM25（混合）、混合+短名单重打分。
前 22 题是扩题前的原题集，用来和「重打分没有提高 Recall@5」对照。
"""
import json
import sys
from datetime import datetime
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.rag_core import retrieve_dense
from core.retrieve_funnel import retrieve_bm25, retrieve_funnel

QUESTIONS = Path(__file__).resolve().parent / "retrieval_questions.json"
REPORTS = Path(__file__).resolve().parent / "reports"
# 文件前 22 条是 2026-10-15 的原题集。后面的题只在能填 expect_id 或 expect_source_path 时追加。
BASELINE_COUNT = 22


def _hit(rows: list[dict], question: dict) -> bool:
    expect_id = question.get("expect_id") or ""
    expect_path = question.get("expect_source_path") or ""
    for row in rows[:5]:
        if expect_id and row.get("id") == expect_id:
            return True
        meta = row.get("metadata") or {}
        for key in ("source_path", "file_path", "doc_path"):
            if expect_path and str(meta.get(key) or "") == expect_path:
                return True
    return False


def _top_ids(rows: list[dict]) -> str:
    ids = [row.get("id") or "" for row in rows[:3]]
    ids = [item for item in ids if item]
    return ", ".join(ids) if ids else "-"


def _measure(questions: list[dict], fn) -> list[dict]:
    rows_out = []
    for index, question in enumerate(questions, start=1):
        started = time.perf_counter()
        rows = fn(question["query"])
        elapsed = time.perf_counter() - started
        rows_out.append({
            "n": index,
            "query": question["query"],
            "hit": _hit(rows, question),
            "ms": elapsed * 1000.0,
            "top": _top_ids(rows),
        })
    return rows_out


def _summarize(records: list[dict]) -> dict:
    hits = sum(1 for row in records if row["hit"])
    total = len(records)
    recall = hits / total if total else 0.0
    mean_ms = (sum(row["ms"] for row in records) / total) if total else 0.0
    misses = [
        {"n": row["n"], "query": row["query"], "top": row["top"]}
        for row in records if not row["hit"]
    ]
    return {
        "recall": recall,
        "mean_ms": mean_ms,
        "hits": hits,
        "total": total,
        "misses": misses,
    }


def _judgment(hybrid: float, rerank: float) -> str:
    if rerank > hybrid:
        return (
            f"重打分 Recall@5（{rerank:.3f}）高于混合（{hybrid:.3f}）。"
            "热路径仍是 `RERANK = False`，这一段没有把它打开。"
        )
    return (
        f"重打分 Recall@5（{rerank:.3f}）不高于混合（{hybrid:.3f}）。"
        "热路径继续不加，`RERANK` 仍是 False。"
    )


def _table(results: list[tuple[str, dict]]) -> list[str]:
    lines = [
        "| 模式 | Recall@5 | 平均毫秒 | 命中 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, row in results:
        lines.append(
            f"| {name} | {row['recall']:.3f} | {row['mean_ms']:.1f} | {row['hits']}/{row['total']} |"
        )
    return lines


def _miss_lines(results: list[tuple[str, dict]]) -> list[str]:
    lines = []
    any_miss = False
    for name, row in results:
        if not row["misses"]:
            continue
        any_miss = True
        lines.append(f"#### {name}")
        lines.append("")
        for miss in row["misses"]:
            lines.append(f"- {miss['n']}. {miss['query']} → {miss['top']}")
        lines.append("")
    if not any_miss:
        lines.append("没有没命中的题。")
        lines.append("")
    return lines


def _write_report(stamp: str, baseline: list[tuple[str, dict]], full: list[tuple[str, dict]], added: int) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"{stamp}-retrieval.md"
    hybrid = next(row for name, row in baseline if name.startswith("路由"))
    rerank = next(row for name, row in baseline if name.startswith("混合+"))
    full_hybrid = next(row for name, row in full if name.startswith("路由"))
    full_rerank = next(row for name, row in full if name.startswith("混合+"))
    lines = [
        f"# {stamp} 检索对照",
        "",
        f"题集 `eval/retrieval_questions.json`，共 {full[0][1]['total']} 题。前 {BASELINE_COUNT} 题是扩题前的原题集，后 {added} 题来自项目里的类、Actor、错误原文和文档标题，每题都有 `expect_id` 或 `expect_source_path`。",
        "",
        "命中：前 5 条里出现 `expect_id`，或 metadata 的 `source_path` / `file_path` / `doc_path` 等于 `expect_source_path`。平均毫秒是预热一次之后的单次查询，不含加载模型和第一次读出语料。向量模型仍是 `BAAI/bge-small-zh-v1.5`。",
        "",
        f"## 原题集 {BASELINE_COUNT} 题",
        "",
        *_table(baseline),
        "",
        _judgment(hybrid["recall"], rerank["recall"]),
        "",
        f"## 全题集 {full[0][1]['total']} 题",
        "",
        *_table(full),
        "",
        _judgment(full_hybrid["recall"], full_rerank["recall"]),
        "",
        "## 没命中",
        "",
        f"### 原题集 {BASELINE_COUNT} 题",
        "",
        *_miss_lines(baseline),
        f"### 全题集 {full[0][1]['total']} 题",
        "",
        *_miss_lines(full),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    missing = [
        index for index, question in enumerate(questions, start=1)
        if not (question.get("expect_id") or question.get("expect_source_path"))
    ]
    if missing:
        print("questions missing expect:", ",".join(str(index) for index in missing))
        return 1
    if len(questions) < BASELINE_COUNT:
        print(f"need at least {BASELINE_COUNT} questions, got {len(questions)}")
        return 1

    sys.stdout.reconfigure(encoding="utf-8")
    retrieve_dense(questions[0]["query"], top_k=5)
    retrieve_bm25(questions[0]["query"], top_k=5)
    retrieve_funnel(questions[0]["query"], top_k=5, rerank=False)
    retrieve_funnel(questions[0]["query"], top_k=5, rerank=True)
    modes = [
        ("纯向量", lambda query: retrieve_dense(query, top_k=5)),
        ("BM25-only", lambda query: retrieve_bm25(query, top_k=5)),
        ("路由+BM25（混合）", lambda query: retrieve_funnel(query, top_k=5, rerank=False)),
        ("混合+短名单重打分", lambda query: retrieve_funnel(query, top_k=5, rerank=True)),
    ]
    measured = [(name, _measure(questions, fn)) for name, fn in modes]
    baseline = [(name, _summarize(records[:BASELINE_COUNT])) for name, records in measured]
    full = [(name, _summarize(records)) for name, records in measured]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = _write_report(stamp, baseline, full, len(questions) - BASELINE_COUNT)

    print(f"{'mode':<22} {'Recall@5':>10} {'ms':>8} {'hit':>8}")
    print(f"baseline {BASELINE_COUNT}")
    for name, row in baseline:
        print(f"{name:<22} {row['recall']:10.3f} {row['mean_ms']:8.1f} {row['hits']}/{row['total']}")
        for miss in row["misses"]:
            print(f"  miss {miss['n']}. {miss['query']} -> {miss['top']}")
    print(f"full {len(questions)}")
    for name, row in full:
        print(f"{name:<22} {row['recall']:10.3f} {row['mean_ms']:8.1f} {row['hits']}/{row['total']}")
        for miss in row["misses"]:
            print(f"  miss {miss['n']}. {miss['query']} -> {miss['top']}")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
