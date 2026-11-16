# eval/check_bench_provenance.py
"""十句基准的产地谓词（20261111-1945 计划 P6）。

只问一件事：`data/archive/*/verdict.json` 里的每条完成记录，是不是真从 `data/runs/`
那次真跑里产出来的、并且没被手改过。三条断言：

1. 手写一份 `verdict.json`（不带 `record_sha256`）放进临时归档目录，`run_bench --verify` 必须红；
2. 把一份真记录的 `status` 从 failed 改成 ok（`record_sha256` 不动），必须红；
3. 用一段没有 `sentence` 字段的 run 记录调 `record_round`，必须拒绝。

外加两条反向断言：同一份输入不改时 `--verify` 必须绿（否则上面两条红是恒红，等于没验）；
`record_round` 正常写出的记录，`--verify` 必须认。

全程在临时目录里造输入：不动真的 `data/archive` / `data/runs`，不往里写一条记录。
不连编辑器、不调模型。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RUN_ID = "20261111-120000"
SENTENCE_ID = "open-level"


def _real_run(source_id: str = "20261111-175240") -> dict:
    """真 run 记录当模板（只读）。找不到时用同形状的内联替身，并在输出里说一句。"""
    for path in (ROOT / "data" / "runs").glob(f"*/{source_id}.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or not data.get("query") or not data.get("windows"):
            continue
        if any(isinstance(item, dict) and str(item.get("evidence_sha1") or "").strip()
               for item in (data.get("criteria") or [])):
            return data
    print(f"（data/runs 里没有带判词印章的 {source_id}，用同形状内联替身）")
    return {
        "run_id": source_id,
        "query": "编译工程",
        "source": "rules",
        "status": "成功",
        "windows": {
            "W1": {"id": "W1", "action": "compile_project", "state": "done", "ok": True,
                   "detail": {"ok": True, "criteria": [
                       {"id": "c-cpp_build", "text": "C++ 编译通过，退出码 0 且没有 error C",
                        "evidence_kind": "cpp_build", "passed": True, "windows": ["W1"],
                        "evidence_sha1": "0" * 40}]}},
        },
        "criteria": [{"id": "c-cpp_build", "text": "C++ 编译通过，退出码 0 且没有 error C",
                      "evidence_kind": "cpp_build", "passed": True, "windows": ["W1"],
                      "evidence_sha1": "0" * 40}],
        "sentence": "ok",
    }


def _stage(tmp: Path) -> tuple[Path, Path, dict]:
    """临时 runs 与 archive：拷一份真记录，改成本次的 run_id，整句记 failed。"""
    runs = tmp / "runs"
    archive = tmp / "archive"
    day = runs / "20261111"
    day.mkdir(parents=True, exist_ok=True)
    run = _real_run()
    run = json.loads(json.dumps(run))
    run["run_id"] = RUN_ID
    run["sentence"] = "failed"
    # 模板里 status 是 interrupted / 运行中：真跑收尾后是「失败」或「部分完成」才有 sentence。
    if str(run.get("status") or "") in ("running", "interrupted", "orphaned", ""):
        run["status"] = "部分完成"
    (day / f"{RUN_ID}.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    return runs, archive, run


def _record(runs: Path, archive: Path) -> dict:
    from agent.bench_record import record_round

    return record_round(RUN_ID, SENTENCE_ID, runs_root=runs, archive_root=archive)


def _verify(archive: Path, runs: Path, monkeypatch=None) -> tuple[int, list[str]]:
    """跑 run_bench 的 --verify 同一个函数，把 stdout 收成行。"""
    import contextlib
    import io

    from eval import run_bench

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = run_bench._live(run_bench._load_manifest(), verify=True,
                               archive=archive, runs_root=runs)
    return code, [line for line in buffer.getvalue().splitlines() if line.strip()]


def check_handwritten_verdict_is_red() -> None:
    """断言 1：手写的 verdict.json（没有 record_sha256）必须让 --verify 红。"""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        runs, archive, _run = _stage(tmp)
        handwritten = archive / RUN_ID
        handwritten.mkdir(parents=True, exist_ok=True)
        (handwritten / "verdict.json").write_text(json.dumps({
            "sentence_id": SENTENCE_ID,
            "status": "failed",
            "evidence_sha1": "",
            "entry": "menu3",
            "backend": "monolith",
            "false_pass": False,
            "window_failed": True,
            "criteria_passed": False,
            "manual_unsigned": False,
            "existing_instance_property_as_ok": False,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        code, lines = _verify(archive, runs)
        assert code == 1, ("手写的 verdict.json 必须让 --verify 红", code, lines)
        text = " ".join(lines)
        assert "record_sha256" in text, ("红了但要指向 record_sha256 缺失", lines)
        assert RUN_ID in text, ("红了但没指到那一份记录", lines)


def check_tampered_status_is_red() -> None:
    """断言 2：把真记录的 status 从 failed 改成 ok、record_sha256 不动，必须红。"""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        runs, archive, _run = _stage(tmp)
        outcome = _record(runs, archive)
        assert outcome["ok"], outcome
        path = Path(outcome["path"])
        assert outcome["record"]["status"] == "failed", ("这轮的 status 必须是 failed", outcome["record"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "ok"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        code, lines = _verify(archive, runs)
        assert code == 1, ("改过 status 的记录必须让 --verify 红", code, lines)
        text = " ".join(lines)
        assert "record_sha256 对不上" in text, ("红了但没说是哈希对不上", lines)
        # 反面：同一份输入不改时必须是绿的，否则上面这条红是恒红，等于没验。
        # 另起一份临时目录重放，不在被改过的那份上再记第二轮（那个目录已经被改脏了）。
        fresh = tmp / "fresh"
        fresh.mkdir(parents=True, exist_ok=True)
        runs2, archive2, _run2 = _stage(fresh)
        outcome_again = _record(runs2, archive2)
        assert outcome_again["ok"], outcome_again
        code, lines = _verify(archive2, runs2)
        assert code == 2, ("产地核过、但记录不满 30 条时退出码是 2（不是百分比）", code, lines)
        assert not any("成功率" in line for line in lines), ("不满 30 条不许打印百分比", lines)
        assert "产地核对通过" in " ".join(lines), ("绿要说出核过", lines)


def check_run_without_sentence_is_refused() -> None:
    """断言 3：没有 sentence 字段的 run 记录，record_round 必须拒绝。"""
    from agent.bench_record import Refused, record_round

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        runs, archive, run = _stage(tmp)
        run.pop("sentence", None)
        (runs / "20261111" / f"{RUN_ID}.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            record_round(RUN_ID, SENTENCE_ID, runs_root=runs, archive_root=archive)
        except Refused as exc:
            assert "sentence" in str(exc), ("拒绝原因要说清缺 sentence", str(exc))
        else:
            raise AssertionError("没有 sentence 字段的 run 记录必须被拒绝")
        assert not (archive / RUN_ID / "verdict.json").exists(), "拒绝之后不许留下记录"
        # 反向：把它补回 sentence（failed）后必须能记 —— 红不是因为别的原因。
        run["sentence"] = "failed"
        (runs / "20261111" / f"{RUN_ID}.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
        outcome = record_round(RUN_ID, SENTENCE_ID, runs_root=runs, archive_root=archive)
        assert outcome["ok"], outcome
        code, lines = _verify(archive, runs)
        assert code == 2, ("产地核过，但记录不满 30 条，退出码 2", code, lines)


def check_unknown_sentence_is_refused() -> None:
    """清单外句子不许记：sentence_id 必须能在 bench_manifest.json 里找到。"""
    from agent.bench_record import Refused, record_round

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        runs, archive, _run = _stage(tmp)
        try:
            record_round(RUN_ID, "not-a-bench-sentence", runs_root=runs, archive_root=archive)
        except Refused as exc:
            assert "bench_manifest.json" in str(exc), str(exc)
        else:
            raise AssertionError("清单外的 sentence_id 必须被拒绝")


def check_real_archive_provenance() -> None:
    """真归档现状要说得出话：绿（每份都核过）或红（哪些记录手改/没产地），不许含糊。"""
    from eval import run_bench

    rows = run_bench._verdicts()
    problems = run_bench.verify_records(rows)
    if problems:
        print(f"  data/archive 里 {len(rows)} 份记录，{len(problems)} 份没有产地")
        for line in problems[:3]:
            print("    - " + line.split("\\")[-2] if "\\" in line else "    - " + line)
    else:
        print(f"  data/archive 里 {len(rows)} 份记录都带产地并核过")


def main() -> int:
    checks = [
        ("手写 verdict.json 必红", check_handwritten_verdict_is_red),
        ("改 status 必红", check_tampered_status_is_red),
        ("无 sentence 的 run 必被拒", check_run_without_sentence_is_refused),
        ("清单外句子必被拒", check_unknown_sentence_is_refused),
        ("真归档产地现状", check_real_archive_provenance),
    ]
    failed = []
    for title, func in checks:
        try:
            func()
        except AssertionError as exc:
            print(f"FAIL {title}: {exc}")
            failed.append(title)
        else:
            print(f"ok   {title}")
    if failed:
        print(f"产地谓词 {len(failed)} 条没过：" + "；".join(failed))
        return 1
    print("产地谓词全部通过（三条正向断言都亲自造过红）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
