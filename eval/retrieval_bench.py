# eval/retrieval_bench.py
"""题集上的 Recall@5 和单次查询耗时。对比纯向量、路由+BM25、再加上短名单重打分。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.rag_core import retrieve_dense
from core.retrieve_funnel import retrieve_funnel

QUESTIONS = Path(__file__).resolve().parent / "retrieval_questions.json"


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


def _measure(questions: list[dict], fn) -> tuple[float, float, list[str]]:
    hits = 0
    elapsed = []
    misses = []
    for question in questions:
        started = time.perf_counter()
        rows = fn(question["query"])
        elapsed.append(time.perf_counter() - started)
        if _hit(rows, question):
            hits += 1
        else:
            top = ",".join((row.get("id") or "")[:48] for row in rows[:3]) or "-"
            misses.append(f"{question['query']} -> {top}")
    recall = hits / len(questions) if questions else 0.0
    mean_ms = (sum(elapsed) / len(elapsed) * 1000.0) if elapsed else 0.0
    return recall, mean_ms, misses


def main() -> int:
    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    retrieve_dense(questions[0]["query"], top_k=5)
    retrieve_funnel(questions[0]["query"], top_k=5, rerank=False)
    modes = [
        ("dense", lambda query: retrieve_dense(query, top_k=5)),
        ("route+bm25", lambda query: retrieve_funnel(query, top_k=5, rerank=False)),
        ("route+bm25+rerank", lambda query: retrieve_funnel(query, top_k=5, rerank=True)),
    ]
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"{'mode':<22} {'Recall@5':>10} {'ms':>8}")
    for name, fn in modes:
        recall, mean_ms, misses = _measure(questions, fn)
        print(f"{name:<22} {recall:10.3f} {mean_ms:8.1f}")
        for line in misses:
            print(f"  miss {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
