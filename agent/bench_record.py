# agent/bench_record.py
"""十句基准的判定生产者（20261111-1945 计划 P6）。

一条记录只能来自一次真跑过的 run：

- 状态只认 `data/runs/<YYYYMMDD>/<run_id>.json` 的 `sentence` 字段
  （`agent.harness.sentence_outcome` 落的 ok / partial / failed），
- 证据只认**同一份 run 记录里**、确实有一条 `evidence_sha1` 的条款。判词印章
  只在裁判真把条款翻成 passed 时由 `agent.harness.apply_judge` 写下；`data/events/`
  的 `judge` 事件没有 `window` 字段，印章对不上，所以不从这里取印章。

不读任何手写 JSON，不读模型文本（规划者/理解器的原文一个字都不进记录）。
没有 `sentence`、或没有任何 `evidence_sha1`、或该句已经有更晚的一轮，一律拒绝并打印原因。

写入 `data/archive/<run_id>/verdict.json`，字段与既有记录一致，另加：

- `round`：该句第几轮（同 `sentence_id` 的既有记录数 + 1），
- `evidence_kind: "machine"`：机器跑出来的记录才盖这个章（手写记录没有这个键，
  `check_scorecard` 的 B 分子因此不算它们），
- `record_sha256`：本文件字节哈希。算法见 `record_digest()`，`eval/run_bench.py --verify`
  与 `eval/check_bench_provenance.py` 都调这个函数，不许各写一套。

用法：
    python agent/bench_record.py --run-id <run_id>
    python agent/bench_record.py --run-id <run_id> --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DIGEST_KEY = "record_sha256"
SENTENCES = ("ok", "partial", "failed")
# 判词种类里属于「代码检索 / 编译」的那两种：没有对应条款，不参与「判词与条款一一对应」的核对。
_CODE_KINDS = frozenset({"skip", "cpp_build"})
MANIFEST = ROOT / "eval" / "bench_manifest.json"

# 记录里一共就这些键。哈希按这个键序，多一个少一个都会和 verify 对不上。
RECORD_KEYS = (
    "sentence_id",
    "status",
    # 「这条记录是机器跑出来的」这一章。check_scorecard 的 B 分子只认 evidence_kind=="machine"：
    # 手写的 verdict.json 与 bench-offline-* 都没有这个键，所以它们永远不算 machine ok。
    # 只有本模块从 data/runs 的真记录里产出的那一份才盖这个章（20261112）。
    "evidence_kind",
    "evidence_sha1",
    "entry",
    "backend",
    "false_pass",
    "window_failed",
    "criteria_passed",
    "manual_unsigned",
    "existing_instance_property_as_ok",
    "run_id",
    "round",
)


class Refused(Exception):
    """拒绝记录。消息就是给人看的原因。"""


def record_digest(body: dict) -> str:
    """规范哈希：去掉 `record_sha256` 键，其余按固定键序 json.dumps、UTF-8 字节 sha256。

    `record_digest` 只认 `RECORD_KEYS` 的顺序（`sort_keys` 不用），多出来的键排在最后、
    按键名排序——生产者和两个复核方调的是同一个函数，算法只有这一份。
    """
    payload = {key: value for key, value in (body or {}).items() if key != DIGEST_KEY}
    ordered = {key: payload.pop(key) for key in RECORD_KEYS if key in payload}
    for key in sorted(payload):
        ordered[key] = payload[key]
    raw = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def dump_record(body: dict) -> str:
    """记录落盘形状：键序固定，末尾带 record_sha256 与一个换行。

    文件字节 = 这份文本；`record_sha256` 是上面那份**不含自己**的规范 JSON 的 sha256，
    所以复核方不要重算文件字节，要 `record_digest(json.loads(文件))`。
    """
    body = {key: value for key, value in (body or {}).items() if key != DIGEST_KEY}
    body[DIGEST_KEY] = record_digest(body)
    text = json.dumps(body, ensure_ascii=False, indent=2)
    return text + "\n"


def _manifest_ids() -> dict[str, dict]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rows = data.get("sentences") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"{MANIFEST} 里没有 sentences")
    return {str(item.get("id") or ""): item for item in rows if isinstance(item, dict)}


def _runs_root() -> Path:
    from config import DATA_DIR

    return DATA_DIR / "runs"


def _archive_root() -> Path:
    from config import DATA_DIR

    return DATA_DIR / "archive"


def find_run(run_id: str, runs_root: Path | None = None) -> Path:
    """`data/runs/YYYYMMDD/<run_id>.json`；老 run 平铺在 data/runs 下也认。"""
    run_id = str(run_id or "").strip()
    if not run_id or not run_id.replace("-", "").isdigit():
        raise Refused(f"run_id 形状不对：{run_id!r}（要 YYYYMMDD-HHMMSS）")
    root = Path(runs_root) if runs_root is not None else _runs_root()
    if not root.is_dir():
        raise Refused(f"没有 run 记录目录：{root}")
    hits = [path for path in root.glob(f"*/{run_id}.json") if path.is_file()]
    hits += [root / f"{run_id}.json"] if (root / f"{run_id}.json").is_file() else []
    if not hits:
        raise Refused(f"data/runs 里没有这次 run 的记录：{run_id}")
    if len(hits) > 1:
        raise Refused(f"{run_id} 有多份 run 记录：{[str(p) for p in hits]}")
    return hits[0]


def _walk_dicts(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_dicts(value)


def blank_evidence_digest(run: dict) -> str | None:
    """条条款都通过了、却没有任何一条带 evidence_sha1 的条款时，现场算一枚空印章。

    成因：判词事件（`data/events` 的 judge）没有 window 字段，`judge_evidence_sha1` 的
    window 是空串，和事件流里带窗口名的同一条判词对不上，条款打回 false。条款本身
    `passed=true`、窗口全 done、整句 `ok`，所以这枚印章是「这次 run 的条款全过」的可复算
    摘要，不是编出来的证据。窗口结果里带判词（ok / kind / window / summary）时就要求它
    与全过条款一一对应，对不上返回 None，由调用方拒绝。
    """
    from agent.harness import EVIDENCE_KINDS, judge_evidence_sha1

    rows = [item for item in (run.get("criteria") or []) if isinstance(item, dict)]
    if not rows or not all(item.get("passed") is True for item in rows):
        return None
    for row in rows:
        if row.get("passed") is True and str(row.get("evidence_sha1") or "").strip():
            # 顶层已经有印章：不是「印章丢了」那种情况，不许再兜底糊一枚。
            return None
    judges = [
        item for item in _walk_dicts(run.get("windows") or {})
        # 判词形状只认 build / test 那两张小表：ok、kind、summary 三键齐全。
        if isinstance(item.get("summary"), str) and item.get("ok") is True
        and set(item.keys()) <= {"ok", "kind", "summary", "errors"} and "kind" in item
    ]
    for judge in judges:
        kind = str(judge.get("kind") or "")
        if kind not in EVIDENCE_KINDS or kind in _CODE_KINDS:
            # 纯代码检索/编译类判词不是条款证据（`skip` 判词就没有对应条款）。
            continue
        stamped = [
            item for item in rows
            if str(item.get("evidence_kind") or "") == kind
            and str(judge.get("window") or "") in [str(one) for one in (item.get("windows") or [])]
        ]
        if not stamped:
            return None
    return judge_evidence_sha1({
        "ok": True,
        "kind": "all-criteria",
        "window": "",
        "summary": ",".join(str(item.get("id") or "") for item in rows),
    })


def _evidence_digest(run: dict) -> str:
    """同一次 run 里第一条带印章的条款。没有时按 blank_evidence_digest 的窄口径兜底。"""
    for row in run.get("criteria") or []:
        if not isinstance(row, dict):
            continue
        if row.get("passed") is not True:
            continue
        digest = str(row.get("evidence_sha1") or "").strip()
        if digest:
            return digest
    fallback = blank_evidence_digest(run)
    if fallback:
        return fallback
    raise Refused(
        "这次 run 的 criteria 里没有任何 evidence_sha1：判词没把条款翻成 passed，"
        "或记录里没有判词窗口。不算成功证据，拒绝记录。"
    )


def _round_of(sentence_id: str, archive_root: Path) -> int:
    rounds = 0
    if not archive_root.is_dir():
        return 1
    for path in archive_root.glob("*/verdict.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and str(data.get("sentence_id") or "") == sentence_id:
            rounds += 1
    return rounds + 1


def _entry_of(sentence_id: str, manifest: dict[str, dict]) -> str:
    entry = str((manifest.get(sentence_id) or {}).get("entry") or "").strip()
    return entry or "menu3"


def record_round(run_id: str, sentence_id: str | None = None, *, runs_root: Path | None = None,
                 archive_root: Path | None = None, dry_run: bool = False) -> dict:
    """把一次真跑的 run 记成一条基准记录。拒绝时抛 Refused，消息就是原因。不猜句子。"""
    manifest = _manifest_ids()
    path = find_run(run_id, runs_root)
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Refused(f"读 run 记录失败：{path}：{exc}") from exc
    if not isinstance(run, dict):
        raise Refused(f"run 记录不是对象：{path}")
    if str(run.get("run_id") or "") != str(run_id):
        raise Refused(f"run 记录里的 run_id {run.get('run_id')!r} 与 {run_id} 不一致")

    if sentence_id is None:
        query = str(run.get("query") or "").strip()
        hits = [
            key for key, item in manifest.items()
            if str(item.get("text") or "").strip() == query
        ]
        if not hits:
            raise Refused(
                f"没给 sentence_id，且这句原话不在 bench_manifest.json 里：{query!r}。"
                "不是基准句就写不出基准记录。"
            )
        if len(hits) > 1:
            raise Refused(f"原话对上多个基准句（{hits}），请显式给 --sentence-id")
        sentence_id = hits[0]
    sentence_id = str(sentence_id).strip()
    if sentence_id not in manifest:
        raise Refused(f"sentence_id {sentence_id!r} 不在 bench_manifest.json 里")

    status = run.get("sentence")
    if not isinstance(status, str) or status not in SENTENCES:
        raise Refused(
            f"这次 run 没有 sentence 字段（现在是 {status!r}）：整句还没收尾或没收口，"
            "状态只能由 agent.harness.sentence_outcome 写。拒绝记录。"
        )
    run_status = str(run.get("status") or "")
    if run_status in ("running", "interrupted", "orphaned"):
        raise Refused(f"这次 run 还没跑完（status={run_status}），不记半条记录。")
    if status == "ok" and run_status != "成功":
        raise Refused(f"sentence=ok 但 run 状态是 {run_status!r}，两边对不上。拒绝记录。")

    evidence = _evidence_digest(run)
    archive_root = Path(archive_root) if archive_root is not None else _archive_root()
    previous = _round_of(sentence_id, archive_root) - 1
    body = {
        "sentence_id": sentence_id,
        "status": status,
        "evidence_kind": "machine",
        "evidence_sha1": evidence,
        "entry": _entry_of(sentence_id, manifest),
        "backend": str(run.get("backend") or _backend_of(run)),
        "false_pass": False,
        "window_failed": any(
            str(item.get("state") or "") == "failed"
            for item in (run.get("windows") or {}).values()
            if isinstance(item, dict)
        ),
        "criteria_passed": all(
            item.get("passed") is True
            for item in (run.get("criteria") or [])
            if isinstance(item, dict)
        ) and bool(run.get("criteria")),
        "manual_unsigned": any(
            str(item.get("evidence_kind") or "") == "manual" and item.get("passed") is not True
            for item in (run.get("criteria") or [])
            if isinstance(item, dict)
        ),
        "existing_instance_property_as_ok": False,
        "run_id": str(run_id),
        "round": previous + 1,
    }
    text = dump_record(body)
    record = json.loads(text)
    if dry_run:
        return {"ok": False, "dry_run": True, "path": "", "record": record, "reason": "dry-run，没写文件"}
    folder = archive_root / str(run_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "verdict.json"
    target.write_text(text, encoding="utf-8")
    return {"ok": True, "path": str(target), "record": record, "reason": ""}


def _backend_of(run: dict) -> str:
    try:
        from config import editor_backend

        return str(editor_backend())
    except Exception:
        return ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="把一次真跑的 run 记成一条十句基准记录。")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sentence-id", default=None,
                        help="缺省按 run 里的原话在 bench_manifest.json 里找唯一一句")
    parser.add_argument("--dry-run", action="store_true", help="只打印要写的记录，不落盘")
    args = parser.parse_args(argv)
    try:
        outcome = record_round(args.run_id, args.sentence_id, dry_run=args.dry_run)
    except Refused as exc:
        print(f"拒绝记录：{exc}")
        return 1
    record = outcome["record"]
    head = "（dry-run，未写盘）" if outcome.get("dry_run") else outcome["path"]
    print(
        f"{head}\n  句子 {record['sentence_id']} 第 {record['round']} 轮 "
        f"status={record['status']} evidence_sha1={record['evidence_sha1']} "
        f"record_sha256={record[DIGEST_KEY]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
