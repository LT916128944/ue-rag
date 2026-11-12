# eval/check_bench_audit.py
"""机器跑基准 + 人抽查的**真门**（20261112-1220 计划 §4.4）。

只读三处：`data/bench/batch.json`（机器跑出来的那一批）、`data/bench/audit.json`
（人抽查后签的那一份）、`data/archive/*/verdict.json`（每轮落下来的记录）。
判七条，任一不满足就红：

1. `batch.json` 存在、是合法 JSON 对象、`batch_id`/`seed`/`sentences`/`sampled` 齐全；
2. `produced_by == "agent/bench_runner.py"`（不是手搓的批次）；
3. archive 的 `run_id` 集合与 `batch.sentences` 的 `run_id` 集合一致（打印两边差集）；
4. `batch.sampled` 与用 `batch.seed` 重算的 `random.Random(seed).sample(sorted(all_run_ids), 3)`
   是同一组（只比内容不比分序；多一条少一条都红）——否则就是名单被改过；
5. `audit.json` 存在，且 `person`、`signed_at` 都不为空（否则就是还没抽查）；
6. `audit.verdict == "ok"`，否则红并打印要删的目录清单（整批作废、重跑）；
7. `audit.sampled` 每条 `run_id` 真在 archive 里、抄的 `status` 与 archive 那份一致，
   `audit.batch_id` 与 `batch.batch_id` 一致，抽查名单与 `batch.sampled` 同一组。

**为什么没有真数据也必须红**：这一条是门，不是文档承诺。没有 batch/audit 时红正是设计
意图（计划 §4.4 第 1 条）。所以这里没有任何「缺数据就放行」的宽免分支——为了让
`check_all` 变绿而加宽免，等于把抽查退回成一句承诺。

只读、不联网、不调模型、不连编辑器。写盘一概不写；自测只在 `tempfile` 里造输入。

用法：
    python eval/check_bench_audit.py              # 查真仓库（现在是红的：还没有真实批次）
    python eval/check_bench_audit.py --selftest   # 五个子用例，全部过才退 0
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import DATA_DIR  # noqa: E402

PRODUCER = "agent/bench_runner.py"
SAMPLED_SIZE = 3
_KEYS = ("batch_id", "seed", "sentences", "sampled")

# 自测用的种子：32 位小写 hex，与 agent/bench_runner.py 写出来的形状一致。
_SELFTEST_SEED = "8f14e45fceea167a5a36dedd4bea2543"


def _rel(path: Path) -> str:
    """打印用的相对路径：仓库内的文件说相对路径，临时目录里的说绝对路径。"""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def _load_object(path: Path) -> tuple[dict | None, str]:
    """读一个必须是 JSON 对象的文件。失败时不抛，把原因当字符串交回去。"""
    if not path.is_file():
        return None, f"{_rel(path)} 不存在"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{_rel(path)} 读不出来：{exc}"
    if not isinstance(data, dict):
        return None, f"{_rel(path)} 不是对象（是 {type(data).__name__}）"
    return data, ""


def _archive_records(archive: Path) -> dict[str, dict]:
    """`run_id -> verdict.json 内容`。`run_id` 字段为准，缺了才用目录名。"""
    records: dict[str, dict] = {}
    if not archive.is_dir():
        return records
    for path in sorted(archive.glob("*/verdict.json")):
        data, _problem = _load_object(path)
        if data is None:
            continue
        run_id = str(data.get("run_id") or "").strip() or path.parent.name
        records[run_id] = data
    return records


def _cleanup_list(batch: dict | None, records: dict[str, dict]) -> list[str]:
    """整批作废时要删的目录/文件。批次判不了时按 archive 里现存的 run_id 列。"""
    if isinstance(batch, dict) and isinstance(batch.get("sentences"), list):
        run_ids = sorted({
            str(row.get("run_id")).strip()
            for row in batch["sentences"]
            if isinstance(row, dict) and str(row.get("run_id") or "").strip()
        })
    else:
        run_ids = sorted(records)
    return [f"data/archive/{run_id}/" for run_id in run_ids]


def check(root: Path) -> tuple[bool, list[str]]:
    """纯函数：给一个仓库根，判七条，返回 (全绿?, 逐行输出)。"""
    lines: list[str] = []

    def good(name: str) -> None:
        lines.append(f"ok {name}")

    def bad(name: str, reason: str) -> None:
        lines.append(f"红 {name}：{reason}")

    data = Path(root) / "data"
    batch_path = data / "bench" / "batch.json"
    audit_path = data / "bench" / "audit.json"
    archive = data / "archive"
    records = _archive_records(archive)

    # 1. 有一批机器基准（存在 + 是对象 + 键齐）。
    batch: dict | None = None
    raw, problem = _load_object(batch_path)
    if raw is None:
        bad("有一批机器基准", f"没有一批机器基准（{problem}）")
    else:
        missing = [key for key in _KEYS if key not in raw]
        sentences = raw.get("sentences")
        rows_ok = isinstance(sentences, list) and bool(sentences) and all(
            isinstance(row, dict) and str(row.get("run_id") or "").strip() for row in sentences
        )
        if missing or not rows_ok:
            detail = f"缺键 {missing}" if missing else ""
            if not rows_ok:
                detail = (detail + "；" if detail else "") + "sentences 必须是带 run_id 的非空数组"
            bad("有一批机器基准", f"batch.json 结构不对：{detail}")
        else:
            batch = raw
            good("有一批机器基准")

    # 2. 产地：只认 agent/bench_runner.py。
    if batch is None:
        bad("产地是 agent/bench_runner.py", "没有可判的批次（见第 1 条）")
    else:
        produced_by = str(batch.get("produced_by") or "")
        if produced_by != PRODUCER:
            bad("产地是 agent/bench_runner.py",
                f"batch.produced_by={produced_by!r}，不是 {PRODUCER}")
        else:
            good("产地是 agent/bench_runner.py")

    batch_ids = set(records)
    if batch is not None:
        batch_ids = {str(row["run_id"]).strip() for row in batch["sentences"]}

    # 3. batch 的记录集合与 archive 一一对应。
    if batch is None:
        bad("batch 的记录与 archive 一一对应", "没有可判的批次（见第 1 条）")
    else:
        only_archive = sorted(set(records) - batch_ids)
        only_batch = sorted(batch_ids - set(records))
        if only_archive or only_batch:
            bad("batch 的记录与 archive 一一对应",
                f"两边差集：只有 archive 有 {only_archive or '无'}；只有 batch 有 {only_batch or '无'}")
        else:
            good("batch 的记录与 archive 一一对应")

    # 4. 抽样名单必须等于用 seed 重算的结果（只比内容，不比顺序）。
    if batch is None:
        bad("抽样名单未被改过", "没有可判的批次（见第 1 条）")
    else:
        sampled = batch.get("sampled")
        all_run_ids = sorted(batch_ids)
        if len(all_run_ids) < SAMPLED_SIZE:
            # 先报根因：一批不满 3 条 run 时，抽样本来就算不出来（不是名单被改）。
            bad("抽样名单未被改过",
                f"只有 {len(all_run_ids)} 条 run，重算不出 {SAMPLED_SIZE} 条抽样")
        elif not isinstance(sampled, list) or len(sampled) != SAMPLED_SIZE:
            bad("抽样名单未被改过",
                f"名单不是 {SAMPLED_SIZE} 条：{sampled!r}")
        else:
            expect = random.Random(str(batch["seed"])).sample(sorted(all_run_ids), SAMPLED_SIZE)
            got = sorted(str(item) for item in sampled)
            if got != sorted(expect):
                bad("抽样名单未被改过",
                    f"名单被改过：用 seed 重算应是 {sorted(expect)}，batch.sampled 写的是 {got}")
            else:
                good("抽样名单未被改过")

    # 5. 人抽查过并且签了名。
    audit, problem = _load_object(audit_path)
    if audit is None:
        bad("人已抽查并签名", f"还没抽查（{problem}）")
    else:
        person = str(audit.get("person") or "").strip()
        signed_at = str(audit.get("signed_at") or "").strip()
        if not person or not signed_at:
            bad("人已抽查并签名",
                f"还没抽查（person={'有' if person else '空'}，"
                f"signed_at={'有' if signed_at else '空'}）")
        else:
            good("人已抽查并签名")

    # 6. 抽查结论必须是 ok；不是就整批作废，并把要删的东西列出来。
    if audit is None:
        bad("抽查结论是 ok", f"没有 {_rel(audit_path)}，判不了（见第 5 条）")
    else:
        verdict = str(audit.get("verdict") or "")
        if verdict != "ok":
            bad("抽查结论是 ok",
                f"audit.verdict={verdict!r}，不是 ok：这一批的记录整批作废")
            lines.append("  要删（整批作废，重跑）：")
            for path in _cleanup_list(batch, records):
                lines.append(f"    - {path}")
            lines.append("    - data/bench/batch.json")
            lines.append("    - data/bench/audit.json")
            lines.append("  整批作废，重跑")
        else:
            good("抽查结论是 ok")

    # 7. 抽查那几条必须真在 archive 里，且抄的 status 与 archive 一致。
    if audit is None or batch is None:
        lacking = [name for name, value in (("batch.json", batch), ("audit.json", audit))
                   if value is None]
        bad("抽查的每条与 archive 对得上",
            f"缺 {' / '.join(lacking)}，判不了（见上面的红条）")
    else:
        problems: list[str] = []
        rows = audit.get("sampled")
        if not isinstance(rows, list) or not rows:
            problems.append("audit.sampled 不是非空数组")
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                problems.append(f"audit.sampled 里有非对象：{row!r}")
                continue
            run_id = str(row.get("run_id") or "").strip()
            if run_id not in records:
                problems.append(f"{run_id or '<空>'} 不在 archive 里")
                continue
            copied = str(row.get("status") or "")
            archived = str(records[run_id].get("status") or "")
            if copied != archived:
                problems.append(
                    f"{run_id} 抄的 status={copied!r} 与 archive 的 status={archived!r} 不一致")
        if str(audit.get("batch_id") or "") != str(batch.get("batch_id") or ""):
            problems.append(
                f"audit.batch_id={audit.get('batch_id')!r} 与 batch.batch_id={batch.get('batch_id')!r} 不一致")
        audit_ids = {str(row.get("run_id") or "").strip() for row in rows if isinstance(row, dict)}
        batch_sampled = batch.get("sampled")
        batch_sampled_ids = (
            {str(item).strip() for item in batch_sampled}
            if isinstance(batch_sampled, list) else set()
        )
        if audit_ids != batch_sampled_ids:
            problems.append(
                f"audit 抽查名单 {sorted(audit_ids)} 与 batch.sampled "
                f"{sorted(batch_sampled_ids)} 不是同一组")
        if problems:
            bad("抽查的每条与 archive 对得上", "；".join(problems))
        else:
            good("抽查的每条与 archive 对得上")

    passed = not any(line.startswith("红") for line in lines)
    return passed, lines


def _summary(lines: list[str]) -> str:
    green = sum(1 for line in lines if line.startswith("ok"))
    red = sum(1 for line in lines if line.startswith("红"))
    if red == 0:
        return f"抽查门：{green} 条全过（绿），机器基准的这一批可以算数"
    return f"抽查门：绿 {green} 条 / 红 {red} 条 —— 机器基准抽查没过，退 1"


# --- 自测 -------------------------------------------------------------------

def _write_json(path: Path, body: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _stage(root: Path) -> None:
    """在 root 下造一批「合法」的 batch + archive + audit，五个 run 一句一条。"""
    sentence_ids = ["compile", "open-level", "hud", "place-coins", "patrol"]
    statuses = ["ok", "ok", "failed", "ok", "partial"]
    run_ids = [f"20261112-{130100 + index * 100:06d}" for index in range(len(sentence_ids))]
    sentences = [
        {
            "id": sentence_id,
            "round": 1,
            "run_id": run_id,
            "status": status,
            "attempts": 1,
            "seconds": 412.5,
        }
        for sentence_id, status, run_id in zip(sentence_ids, statuses, run_ids)
    ]
    sampled = random.Random(_SELFTEST_SEED).sample(sorted(run_ids), SAMPLED_SIZE)
    _write_json(root / "data" / "bench" / "batch.json", {
        "batch_id": "20261112-130000-3r",
        "started_at": "2026-11-12T13:00:00",
        "finished_at": "2026-11-12T19:00:00",
        "rounds": 3,
        "seed": _SELFTEST_SEED,
        "produced_by": PRODUCER,
        "note": "自测造的一批",
        "sentences": sentences,
        "missing": [],
        "sampled": sampled,
    })
    by_run = {row["run_id"]: row for row in sentences}
    for run_id in run_ids:
        row = by_run[run_id]
        _write_json(root / "data" / "archive" / run_id / "verdict.json", {
            "sentence_id": row["id"],
            "status": row["status"],
            "evidence_kind": "machine",
            "evidence_sha1": "1" * 40,
            "entry": "menu3",
            "backend": "official",
            "false_pass": False,
            "window_failed": row["status"] != "ok",
            "criteria_passed": row["status"] == "ok",
            "manual_unsigned": False,
            "existing_instance_property_as_ok": False,
            "run_id": run_id,
            "round": 1,
            "record_sha256": "0" * 64,
        })
    _write_json(root / "data" / "bench" / "audit.json", {
        "batch_id": "20261112-130000-3r",
        "person": "张三",
        "signed_at": "2026-11-12T19:10:00",
        "verdict": "ok",
        "produced_by": "agent/bench_gate.py",
        "notes": "自测造的一次抽查",
        "sampled": [
            {
                "run_id": run_id,
                "status": by_run[run_id]["status"],
                "ok": True,
                "note": "人看的一句话",
            }
            for run_id in sampled
        ],
    })


def _case_legal(root: Path) -> None:
    """(a) 合法的一批 → 退 0。没有这条，下面四条红是恒红，等于没验。"""
    _stage(root)
    passed, lines = check(root)
    assert passed, ("合法的一批必须判绿", lines)
    assert all(line.startswith("ok") for line in lines), ("绿的时候不该有别的行", lines)


def _case_sampled_tampered(root: Path) -> None:
    """(b) 换掉名单里一条 → 退 1，且原因里出现「名单」。"""
    _stage(root)
    batch_path = root / "data" / "bench" / "batch.json"
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    all_ids = sorted({row["run_id"] for row in batch["sentences"]})
    replacement = [run_id for run_id in all_ids if run_id not in batch["sampled"]][0]
    batch["sampled"] = [replacement, *batch["sampled"][1:]]
    _write_json(batch_path, batch)
    passed, lines = check(root)
    assert not passed, ("名单被改过必须判红", lines)
    assert any(line.startswith("红") and "名单" in line for line in lines), (
        "红字里要说名单被改过", lines)


def _case_no_audit(root: Path) -> None:
    """(c) 删掉 audit.json → 退 1。"""
    _stage(root)
    (root / "data" / "bench" / "audit.json").unlink()
    passed, lines = check(root)
    assert not passed, ("没有 audit.json 必须判红", lines)
    assert any(line.startswith("红") and "还没抽查" in line for line in lines), (
        "红字里要说还没抽查", lines)


def _case_fake_verdict(root: Path) -> None:
    """(d) audit.verdict=fake → 退 1，且输出里有要删的目录清单。"""
    _stage(root)
    audit_path = root / "data" / "bench" / "audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["verdict"] = "fake"
    _write_json(audit_path, audit)
    passed, lines = check(root)
    assert not passed, ("verdict 不是 ok 必须判红", lines)
    text = "\n".join(lines)
    for want in ("data/archive/", "data/bench/batch.json", "data/bench/audit.json", "整批作废"):
        assert want in text, (f"要删的清单里缺 {want}", lines)


def _case_wrong_producer(root: Path) -> None:
    """(e) produced_by 改成别的 → 退 1。"""
    _stage(root)
    batch_path = root / "data" / "bench" / "batch.json"
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    batch["produced_by"] = "hand/written.py"
    _write_json(batch_path, batch)
    passed, lines = check(root)
    assert not passed, ("不是 agent/bench_runner.py 产出的批次必须判红", lines)
    assert any(line.startswith("红") and "produced_by" in line for line in lines), (
        "红字里要指向 produced_by", lines)


def _selftest() -> int:
    cases = [
        ("(a) 合法的一批退 0", _case_legal),
        ("(b) 换掉一条抽样退 1 且说「名单」", _case_sampled_tampered),
        ("(c) 删 audit.json 退 1", _case_no_audit),
        ("(d) verdict=fake 退 1 且列出要删的目录", _case_fake_verdict),
        ("(e) produced_by 不对退 1", _case_wrong_producer),
    ]
    failed: list[str] = []
    for title, runner in cases:
        with tempfile.TemporaryDirectory() as raw:
            try:
                runner(Path(raw))
            except AssertionError as exc:
                print(f"FAIL {title}: {exc}")
                failed.append(title)
            else:
                print(f"ok   {title}")
    if failed:
        print(f"抽查门自测 {len(failed)} 条没过：" + "；".join(failed))
        return 1
    print(f"抽查门自测 {len(cases)} 条全过（含 (a) 合法批次必须判绿的反面）")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    data_root = ROOT / "data"
    if Path(DATA_DIR) != data_root:
        # config.DATA_DIR 是仓库的数据根；两边不是一处时说出来，不静默按另一边判。
        print(f"（注意：config.DATA_DIR={DATA_DIR}，本检查按 {data_root} 读）")
    passed, lines = check(ROOT)
    for line in lines:
        print(line)
    print(_summary(lines))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
