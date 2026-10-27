# scripts/import_dsh_costs.py
"""把 DSH 导出的会话日志（data/dsh/exports/*.jsonl）里的 usage 抄成 costs 行。

只抄日志里真正有的用量字段，字段名与 core/llm_call.py 的 append_cost 一致：
caller / model / prompt_tokens / completion_tokens / 缓存字段 / ts，另加 session 短 id。
导出里没有 elapsed，不估算也不填 0；没有 usage 的行跳过并计数，不填 0。
不抄 prompt 正文、消息正文、路径、密钥。

输入行两种格式都认：
    DeepSeek 原生  usage.prompt_tokens / completion_tokens /
                  prompt_cache_hit_tokens / prompt_cache_miss_tokens /
                  prompt_tokens_details.cached_tokens
    DSH 投影      uncachedInputTokens / outputTokens / cacheReadTokens / cacheWriteTokens
                  （行级或 usage 子对象里都行；prompt_tokens = uncachedInputTokens + cacheReadTokens，
                  cacheReadTokens → prompt_cache_hit_tokens + cached_tokens，
                  uncachedInputTokens → prompt_cache_miss_tokens；
                  cacheWriteTokens 在 DeepSeek 账里没有对应字段，不抄）
文件名以 fixture- 开头的默认跳过，避免 fixture 被导进真实 costs；--include-fixture 打开。

用法：
    python scripts/import_dsh_costs.py
    python scripts/import_dsh_costs.py --in data/dsh/exports --out data/costs.jsonl
    python scripts/import_dsh_costs.py --include-fixture
输出单行 JSON 摘要：{"files": n, "rows": n, "skipped": n}
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IN = ROOT / "data" / "dsh" / "exports"
DEFAULT_OUT = ROOT / "data" / "costs.jsonl"

_NATIVE_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
)
_DSH_KEYS = ("uncachedInputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")
_TOKEN_KEYS = _NATIVE_KEYS + _DSH_KEYS
_TS_KEYS = ("ts", "timestamp", "created_at", "createdAt", "time")
_SESSION_KEYS = ("session_id", "sessionId", "session")
_MODEL_KEYS = ("model", "model_name", "modelName")
_FIXTURE_PREFIX = "fixture-"
_CACHE_ORDER = ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cached_tokens")


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _first(mapping: dict, keys) -> object:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _dict_of(value) -> dict:
    return value if isinstance(value, dict) else {}


def _usage_fields(row: dict) -> dict | None:
    """行级与 usage 子对象合并。没有任何 token 读数的行返回 None。"""
    usage = _dict_of(row.get("usage"))
    merged: dict = {}
    for key in _TOKEN_KEYS:
        if key in row:
            merged[key] = row[key]
    for key in _TOKEN_KEYS:
        if key in usage:
            merged[key] = usage[key]
    details = _dict_of(usage.get("prompt_tokens_details"))
    if not details:
        details = _dict_of(row.get("prompt_tokens_details"))
    cached = _num(details.get("cached_tokens"))

    readings = {key: _num(value) for key, value in merged.items()}
    if cached is None and all(value is None for value in readings.values()):
        return None

    out: dict = {}
    prompt = readings.get("prompt_tokens")
    uncached = readings.get("uncachedInputTokens")
    read = readings.get("cacheReadTokens")
    if prompt is not None:
        out["prompt_tokens"] = prompt
    elif uncached is not None:
        # DSH 投影把输入拆成「未命中 + 命中缓存」，加起来才是 prompt_tokens。
        out["prompt_tokens"] = uncached + (read if read is not None else 0)
    completion = readings.get("completion_tokens")
    if completion is None:
        completion = readings.get("outputTokens")
    if completion is not None:
        out["completion_tokens"] = completion

    hit = readings.get("prompt_cache_hit_tokens")
    if hit is None:
        # DSH 的 cacheReadTokens 就是「从缓存读出」的那一份，与 prompt_cache_hit_tokens 同义。
        hit = read
    miss = readings.get("prompt_cache_miss_tokens")
    if miss is None:
        # DSH 的 uncachedInputTokens 就是「没命中缓存」的输入，与 prompt_cache_miss_tokens 同义。
        # cacheWriteTokens 是「本次写进缓存」的量，DeepSeek 账里没有对应字段，不硬套成 miss。
        miss = uncached
    if hit is not None:
        out["prompt_cache_hit_tokens"] = hit
    if miss is not None:
        out["prompt_cache_miss_tokens"] = miss
    if cached is None and hit is not None and read is not None:
        # DSH 的 cacheReadTokens 就是「命中缓存」的那一份，与 cached_tokens 同义。
        cached = hit
    if cached is not None:
        out["cached_tokens"] = cached
    return out


def _ts_of(row: dict, fallback: float) -> str:
    raw = _first(row, _TS_KEYS)
    if isinstance(raw, str) and raw.strip():
        text = raw.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
    number = _num(raw)
    if number is not None:
        seconds = number / 1000.0 if number > 1e11 else float(number)
        try:
            return datetime.fromtimestamp(seconds).strftime("%Y-%m-%dT%H:%M:%S")
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.fromtimestamp(fallback).strftime("%Y-%m-%dT%H:%M:%S")


def _session_of(row: dict, stem: str) -> str:
    for mapping in (row, _dict_of(row.get("metadata"))):
        raw = _first(mapping, _SESSION_KEYS)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()[:8]
    return stem[:8]


def _model_of(row: dict) -> str | None:
    for mapping in (row, _dict_of(row.get("usage")), _dict_of(row.get("metadata"))):
        raw = _first(mapping, _MODEL_KEYS)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def cost_row(row: dict, *, stem: str, mtime: float) -> dict | None:
    """一行导出 → 一行 costs。没有 usage 返回 None（由调用方计入 skipped）。"""
    fields = _usage_fields(row)
    if fields is None:
        return None
    out: dict = {"caller": "dsh"}
    model = _model_of(row)
    if model:
        out["model"] = model
    for key in ("prompt_tokens", "completion_tokens"):
        if key in fields:
            out[key] = fields[key]
    out["error"] = None
    for key in _CACHE_ORDER:
        if key in fields:
            out[key] = fields[key]
    out["session"] = _session_of(row, stem)
    out["ts"] = _ts_of(row, mtime)
    return out


def import_dir(in_dir: Path, out_path: Path, *, include_fixture: bool = False) -> dict:
    """读目录里的 *.jsonl，把 costs 行追加到 out_path。返回 {"files","rows","skipped"}。"""
    summary = {"files": 0, "rows": 0, "skipped": 0}
    rows: list[dict] = []
    paths = sorted(in_dir.glob("*.jsonl")) if in_dir.is_dir() else []
    for path in paths:
        if not include_fixture and path.name.startswith(_FIXTURE_PREFIX):
            continue
        summary["files"] += 1
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                summary["skipped"] += 1
                continue
            if not isinstance(item, dict):
                summary["skipped"] += 1
                continue
            record = cost_row(item, stem=path.stem, mtime=mtime)
            if record is None:
                summary["skipped"] += 1
                continue
            rows.append(record)
    summary["rows"] = len(rows)
    if rows:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("a", encoding="utf-8") as handle:
            for record in rows:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 DSH 导出的会话日志 usage 导入 costs.jsonl")
    parser.add_argument("--in", dest="in_dir", default=str(DEFAULT_IN), help="导出目录，默认 data/dsh/exports")
    parser.add_argument("--out", dest="out_path", default=str(DEFAULT_OUT), help="成本文件，默认 data/costs.jsonl")
    parser.add_argument("--include-fixture", action="store_true", help="连 fixture- 开头的文件一起导（默认跳过）")
    ns = parser.parse_args(argv)
    summary = import_dir(Path(ns.in_dir), Path(ns.out_path), include_fixture=ns.include_fixture)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
