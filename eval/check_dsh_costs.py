# eval/check_dsh_costs.py
"""离线检查 DSH 会话日志 → costs 行的导入口径。

只读 fixture，数据目录与输出全部落在临时目录，不碰真实 data/costs.jsonl。
覆盖：两条 usage 行入库 / 无 usage 行跳过并计数 / 只留 usage 字段（无 prompt 正文、路径、elapsed）/
fixture- 默认跳过 / 缓存字段有则写无则不写（不填 0）/ report.py 的「DSH 用量」节有则出现、无则省略。
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMPORTER = ROOT / "scripts" / "import_dsh_costs.py"
FIXTURE = ROOT / "data" / "dsh" / "exports" / "fixture-session.jsonl"
REAL_COSTS = ROOT / "data" / "costs.jsonl"

ALLOWED_KEYS = {
    "caller",
    "model",
    "prompt_tokens",
    "completion_tokens",
    "error",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "cached_tokens",
    "session",
    "ts",
}
FORBIDDEN_KEYS = {
    "prompt",
    "content",
    "text",
    "path",
    "messages",
    "api_key",
    "authorization",
    "reasoning",
    "elapsed",
    "response",
    "body",
    "tools",
}

sys.path.insert(0, str(ROOT))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _fingerprint(path: Path) -> tuple[int, str] | None:
    if not path.is_file():
        return None
    return path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()


def _load_importer():
    spec = importlib.util.spec_from_file_location("import_dsh_costs", IMPORTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(module, argv: list[str]) -> dict:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = module.main(argv)
    check(code == 0, f"导入退出码不是 0: {code}")
    text = buffer.getvalue().strip()
    check(text.count("\n") == 0, f"摘要应是单行 JSON: {text!r}")
    return json.loads(text)


def _rows_of(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _report_section(costs_path: Path) -> tuple[bool, str]:
    from eval.report import print_dsh_usage

    buffer = io.StringIO()
    previous = os.environ.get("UE_COSTS_PATH")
    os.environ["UE_COSTS_PATH"] = str(costs_path)
    try:
        with contextlib.redirect_stdout(buffer):
            shown = print_dsh_usage()
    finally:
        if previous is None:
            os.environ.pop("UE_COSTS_PATH", None)
        else:
            os.environ["UE_COSTS_PATH"] = previous
    return shown, buffer.getvalue()


def main() -> int:
    check(FIXTURE.is_file(), f"fixture 不存在: {FIXTURE}")
    check(len(FIXTURE.read_text(encoding="utf-8").splitlines()) == 3, "fixture 应是 3 行")
    module = _load_importer()
    real_before = _fingerprint(REAL_COSTS)
    tmp = Path(tempfile.mkdtemp(prefix="dsh-costs-"))
    exports = tmp / "exports"
    exports.mkdir(parents=True, exist_ok=True)

    # 1) fixture 改名成不带前缀的文件，导入到临时 costs
    shutil.copyfile(FIXTURE, exports / "session.jsonl")
    out = tmp / "costs.jsonl"
    summary = _run(module, ["--in", str(exports), "--out", str(out)])
    check(summary == {"files": 1, "rows": 2, "skipped": 1}, f"摘要不对: {summary}")
    rows = _rows_of(out)
    check(len(rows) == 2, f"应落 2 行: {rows}")

    # 2) 每行只留 usage 字段：caller=dsh、有 model/ts/session/error、无正文与 elapsed
    for row in rows:
        check(row.get("caller") == "dsh", f"caller 不是 dsh: {row}")
        check(set(row) <= ALLOWED_KEYS, f"出现不该抄的字段: {sorted(set(row) - ALLOWED_KEYS)}")
        check(not (set(row) & FORBIDDEN_KEYS), f"出现禁止字段: {sorted(set(row) & FORBIDDEN_KEYS)}")
        check("elapsed" not in row, f"不该写 elapsed: {row}")
        check(isinstance(row.get("model"), str) and row["model"], f"缺 model: {row}")
        check(isinstance(row.get("session"), str) and len(row["session"]) == 8, f"session 不是 8 位短 id: {row}")
        check(row.get("error") is None, f"error 应是 null: {row}")
        check(len(str(row.get("ts") or "")) == 19, f"ts 不是秒级 ISO: {row}")

    # 3) 原生格式：缓存三件套照抄
    native = rows[0]
    check(native["prompt_tokens"] == 1200 and native["completion_tokens"] == 140, f"原生 token 不对: {native}")
    check(native["prompt_cache_hit_tokens"] == 960, f"原生命中不对: {native}")
    check(native["prompt_cache_miss_tokens"] == 240, f"原生未命中不对: {native}")
    check(native["cached_tokens"] == 960, f"原生 cached_tokens 不对: {native}")
    check(native["session"] == "sess-9f2", f"session 短 id 不对: {native}")
    check(native["ts"] == "2026-10-23T09:15:02", f"ts 不对: {native}")

    # 4) DSH 投影：prompt = uncached + cacheRead，cacheRead 同时是 cached_tokens，
    #    uncached 是未命中；cacheWriteTokens 不硬套成 miss（DeepSeek 账里没这个字段）
    projected = rows[1]
    check(projected["prompt_tokens"] == 400, f"投影 prompt_tokens 不对: {projected}")
    check(projected["completion_tokens"] == 40, f"投影 completion_tokens 不对: {projected}")
    check(projected["prompt_cache_hit_tokens"] == 100, f"投影命中不对: {projected}")
    check(projected["prompt_cache_miss_tokens"] == 300, f"投影未命中不对: {projected}")
    check(projected["cached_tokens"] == 100, f"投影 cached_tokens 不对: {projected}")
    check(projected["session"] == "sess-9f2", f"sessionId 没被认: {projected}")

    # 5) fixture- 前缀默认跳过，--include-fixture 才导
    guarded = tmp / "guarded"
    guarded.mkdir()
    shutil.copyfile(FIXTURE, guarded / "fixture-session.jsonl")
    guarded_out = tmp / "guarded-costs.jsonl"
    skipped_all = _run(module, ["--in", str(guarded), "--out", str(guarded_out)])
    check(skipped_all == {"files": 0, "rows": 0, "skipped": 0}, f"fixture 没被默认跳过: {skipped_all}")
    check(not guarded_out.exists(), "默认跳过时不该生成 costs 文件")
    with_fixture = _run(module, ["--in", str(guarded), "--out", str(guarded_out), "--include-fixture"])
    check(with_fixture == {"files": 1, "rows": 2, "skipped": 1}, f"--include-fixture 没生效: {with_fixture}")

    # 6) 只有输出 token 的行：不补 prompt、不补缓存、不填 0
    partial = tmp / "partial"
    partial.mkdir()
    (partial / "partial.jsonl").write_text(
        json.dumps({"type": "message", "model": "deepseek-chat", "ts": "2026-10-23T10:00:00", "usage": {"outputTokens": 7}})
        + "\n",
        encoding="utf-8",
    )
    partial_out = tmp / "partial-costs.jsonl"
    partial_summary = _run(module, ["--in", str(partial), "--out", str(partial_out)])
    check(partial_summary == {"files": 1, "rows": 1, "skipped": 0}, f"部分 usage 摘要不对: {partial_summary}")
    partial_row = _rows_of(partial_out)[0]
    check(partial_row.get("completion_tokens") == 7, f"部分 usage completion 不对: {partial_row}")
    for key in ("prompt_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cached_tokens"):
        check(key not in partial_row, f"没有读数不该写 {key}: {partial_row}")

    # 7) report.py：有 dsh 行才出现「DSH 用量」，按天汇总；没有就整节省略
    shown, text = _report_section(out)
    check(shown is True, "有 dsh 行时 report 没打印 DSH 用量")
    check("DSH 用量" in text, f"缺 DSH 用量标题: {text!r}")
    check("day\tcalls\tprompt\tcompletion\tcache_hit" in text, f"缺表头: {text!r}")
    check("2026-10-23\t2\t1600\t180\t1060" in text, f"按天汇总不对: {text!r}")
    plain = tmp / "plain-costs.jsonl"
    plain.write_text(
        json.dumps({"caller": "planner", "model": "deepseek-chat", "prompt_tokens": 10, "completion_tokens": 1, "ts": "2026-10-23T11:00:00"})
        + "\n",
        encoding="utf-8",
    )
    shown_plain, text_plain = _report_section(plain)
    check(shown_plain is False and text_plain == "", f"没有 dsh 行时不该出现该节: {text_plain!r}")
    missing = tmp / "missing-costs.jsonl"
    shown_missing, text_missing = _report_section(missing)
    check(shown_missing is False and text_missing == "", f"costs 文件不存在时不该出现该节: {text_missing!r}")

    # 8) 真实 costs.jsonl 一个字都没动
    check(_fingerprint(REAL_COSTS) == real_before, "真实 data/costs.jsonl 被改动了")

    print("dsh-costs ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
