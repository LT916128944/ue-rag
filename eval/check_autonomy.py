# eval/check_autonomy.py
"""自动化度报表：第二把尺子（20261112-1220 计划 §4.5）。

**只报表，永远退 0。** 哪怕数据全缺、JSON 全坏、目录不存在，也只写一份缺省值出来，
不抛异常、不挡 `check_all`。理由：这是一把「看得见人还剩几分钟」的尺子，不是门；
门只有 `eval/check_bench_audit.py` 那一条。报表让它变红，等于用理念改公式。

只读两处，写出 `data/harness/autonomy.json`：

- `data/archive/*/verdict.json`：只数 `evidence_kind == "machine"` 的记录（机器真跑出来的），
  `expected` 是写死的 30（10 句 ×3 轮），`produced_by` 是这批记录的产地分布
  （记录里没有这个字段就记 `"<unknown>"`，分布各项之和等于 `count`）；
- `data/runs/**/*.json`：按文件 mtime 倒序取最近 30 份带 `criteria` 的 run 记录，
  统计其中 `evidence_kind == "manual"` 的条款数 / 条款总数（占比即「还要人签的条款比例」）；
- 四条人工触点、四条 minutes 之和是常量（计划 §3 那四件事），一个字都不改语义。

不联网、不调模型、不连编辑器。`--print-only` 只打印不写文件（自测方便）。

用法：
    python eval/check_autonomy.py
    python eval/check_autonomy.py --print-only
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import DATA_DIR  # noqa: E402

# 10 句 ×3 轮。写死常量：这是计划里的分母，不从数据里反推（反推会被数据本身带偏）。
EXPECTED = 30
# 「最近 30 条」的口径。
RECENT = 30

DATA = Path(DATA_DIR)
ARCHIVE = DATA / "archive"
RUNS = DATA / "runs"
OUTPUT = DATA / "harness" / "autonomy.json"

# 计划 §3 的四条人工介入点。type 只许是 安全边界 / 能力边界 / 审计。
HUMAN_TOUCHPOINTS = [
    {
        "id": "authorize",
        "type": "安全边界",
        "what": "在 data/dsh/session_task.json 里开 allow_writes/save/pie",
        "minutes": 1,
    },
    {
        "id": "pie-look",
        "type": "能力边界",
        "what": "巡逻表现进 PIE 看一眼",
        "minutes": 2,
    },
    {
        "id": "attest-sign",
        "type": "审计",
        "what": "python agent\\attest_sign.py --criterion patrol-presentation --person <名字> --yes",
        "minutes": 0.5,
    },
    {
        "id": "audit-sample",
        "type": "审计",
        "what": "按 batch.sampled 抽查 3 条",
        "minutes": 15,
    },
]


def _load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _machine_records() -> dict:
    """`data/archive/*/verdict.json` 里 evidence_kind == "machine" 的那几条。"""
    count = 0
    produced_by: dict[str, int] = {}
    try:
        paths = sorted(ARCHIVE.glob("*/verdict.json"))
    except OSError:
        paths = []
    for path in paths:
        record = _load(path)
        if record is None:
            continue
        if str(record.get("evidence_kind") or "") != "machine":
            continue
        count += 1
        producer = str(record.get("produced_by") or "").strip()
        if not producer:
            # archive 记录的形状是 `agent/bench_record.py` 的 RECORD_KEYS，里头没有
            # `produced_by` 这个键（产地由 `record_sha256` + check_bench_provenance 兜）。
            # 有 `record_sha256` 且盖了 machine 章的记录，写它的就只有 record_round()；
            # 不写 `produced_by` 的原因是「记录形状固定」，不是「产地不明」。
            stamped = bool(str(record.get("record_sha256") or "").strip())
            producer = "agent/bench_record.record_round（bench_runner 调）" if stamped else "<unknown>"
        produced_by[producer] = produced_by.get(producer, 0) + 1
    return {"count": count, "expected": EXPECTED, "produced_by": produced_by}


def _recent_run_records(limit: int = RECENT) -> list[dict]:
    """按 mtime 倒序取最近几份带 criteria 的 run 记录。读不动的直接跳过。"""
    stamped: list[tuple[float, Path]] = []
    try:
        candidates = list(RUNS.rglob("*.json")) if RUNS.is_dir() else []
    except OSError:
        candidates = []
    for path in candidates:
        try:
            stamped.append((path.stat().st_mtime, path))
        except OSError:
            continue
    stamped.sort(key=lambda item: (-item[0], str(item[1])))
    picked: list[dict] = []
    for _mtime, path in stamped:
        record = _load(path)
        if record is None:
            continue
        criteria = record.get("criteria")
        if not isinstance(criteria, list) or not criteria:
            continue
        picked.append(record)
        if len(picked) >= limit:
            break
    return picked


def _manual_ratio() -> dict:
    """最近 30 份 run 记录里，manual 条款数 / 条款总数。一份都没有时 0/0/0.0。"""
    manual = 0
    total = 0
    for record in _recent_run_records():
        for row in record.get("criteria") or []:
            if not isinstance(row, dict):
                continue
            total += 1
            if str(row.get("evidence_kind") or "") == "manual":
                manual += 1
    ratio = round(manual / total, 3) if total else 0.0
    return {"manual": manual, "total": total, "ratio": ratio}


def build(generated_at: str) -> dict:
    """整份报表。每一节各自兜底：一节坏掉不影响其它节写出缺省值。"""
    try:
        machine_records = _machine_records()
    except Exception:  # noqa: BLE001 —— 报表不许因为任何异常退出非 0
        machine_records = {"count": 0, "expected": EXPECTED, "produced_by": {}}
    try:
        manual_ratio = _manual_ratio()
    except Exception:  # noqa: BLE001
        manual_ratio = {"manual": 0, "total": 0, "ratio": 0.0}
    touchpoints = [dict(item) for item in HUMAN_TOUCHPOINTS]
    minutes = round(sum(float(item["minutes"]) for item in touchpoints), 3)
    return {
        "machine_records": machine_records,
        "manual_criteria_ratio": manual_ratio,
        "human_touchpoints": touchpoints,
        "human_minutes_estimate": minutes,
        "generated_at": generated_at,
    }


def _line(report: dict, printed_only: bool) -> str:
    machine = report["machine_records"]
    ratio = report["manual_criteria_ratio"]
    where = "（--print-only，未写文件）" if printed_only else "→ data/harness/autonomy.json"
    return (
        f"自动化度：机器记录 {machine['count']}/{machine['expected']}，"
        f"manual 条款 {ratio['manual']}/{ratio['total']}（{ratio['ratio']}），"
        f"人工触点 {len(report['human_touchpoints'])} 条共 {report['human_minutes_estimate']} 分钟 {where}"
    )


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    printed_only = "--print-only" in argv
    generated_at = datetime.now().astimezone().replace(microsecond=0).isoformat()
    try:
        report = build(generated_at)
    except Exception:  # noqa: BLE001
        report = {
            "machine_records": {"count": 0, "expected": EXPECTED, "produced_by": {}},
            "manual_criteria_ratio": {"manual": 0, "total": 0, "ratio": 0.0},
            "human_touchpoints": [dict(item) for item in HUMAN_TOUCHPOINTS],
            "human_minutes_estimate": round(
                sum(float(item["minutes"]) for item in HUMAN_TOUCHPOINTS), 3),
            "generated_at": generated_at,
        }
    if not printed_only:
        try:
            OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"（写 {OUTPUT} 失败：{exc}；只打印）")
    print(_line(report, printed_only))
    # 只报表：永远退 0。这一句不许改成 True/False 判定。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
