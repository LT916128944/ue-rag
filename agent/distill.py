# agent/distill.py
"""离线蒸馏。只读 run / event / trace，写出候选，不执行，不叫模型，不写场景。

候选进 data/distill/。影子模式不写 learned_skills，不进执行链。"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _data_root() -> Path:
    override = os.environ.get("UE_DISTILL_ROOT", "").strip()
    if override:
        return Path(override)
    from config import DATA_DIR

    return DATA_DIR


def _out_dir() -> Path:
    override = os.environ.get("UE_DISTILL_OUT", "").strip()
    if override:
        return Path(override)
    return _data_root() / "distill"


def redact(text: str) -> str:
    """去掉本机绝对路径和用户目录。密钥样式的赋值整段打掉。"""
    raw = str(text or "")
    raw = re.sub(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*\S+", r"\1=<redacted>", raw)
    raw = re.sub(r"[A-Za-z]:\\Users\\[^\\\s\"']+", "<user>", raw)
    raw = re.sub(r"[A-Za-z]:\\[^\s\"']+", "<path>", raw)
    return raw


def _sha(text: str) -> str:
    return hashlib.sha1(redact(text).encode("utf-8")).hexdigest()


def _read_jsonl(path: Path, limit: int = 400) -> list[dict]:
    rows = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines[:limit]:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _load_disk(root: Path) -> tuple[list[dict], list[dict]]:
    events: list[dict] = []
    runs: list[dict] = []
    event_dir = root / "events"
    if event_dir.is_dir():
        for path in sorted(event_dir.glob("*.jsonl"))[:40]:
            for row in _read_jsonl(path):
                row = dict(row)
                row.setdefault("_run", path.stem)
                events.append(row)
    trace_dir = root / "traces"
    if trace_dir.is_dir():
        for path in sorted(trace_dir.glob("*.jsonl"))[:20]:
            for row in _read_jsonl(path, limit=80):
                row = dict(row)
                row.setdefault("type", row.get("node") or "trace")
                row.setdefault("_run", path.stem)
                events.append(row)
    run_dir = root / "runs"
    if run_dir.is_dir():
        for path in sorted(run_dir.glob("*.json"))[:40]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                runs.append(data)
        for path in sorted(run_dir.glob("*/*.json"))[:40]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                runs.append(data)
    _attach_archives(root, runs)
    return events, runs


def _attach_archives(root: Path, runs: list[dict]) -> None:
    """把归档里的哈希和矩阵结论挂到对应 run 上。不读工具正文。"""
    archive = root / "archive"
    if not archive.is_dir():
        return
    by_id = {str(run.get("run_id") or ""): run for run in runs if isinstance(run, dict)}
    folders = [path for path in sorted(archive.iterdir()) if path.is_dir()][:40]
    for folder in folders:
        deliver = folder / "deliverables.json"
        if not deliver.is_file():
            continue
        try:
            data = json.loads(deliver.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        rid = str(data.get("run_id") or folder.name)
        target = by_id.get(rid)
        if target is None:
            target = {"run_id": rid, "windows": []}
            runs.append(target)
            by_id[rid] = target
        target["hashes"] = data.get("hashes") or []
        target["matrix"] = data.get("matrix") or []


def _outcome(event: dict) -> str:
    if event.get("type") == "confirm":
        return "correct"
    if event.get("ok") is True:
        return "success"
    if event.get("ok") is False:
        return "fail"
    state = str(event.get("state") or "")
    if state == "paused":
        return "pause"
    if "拒" in str(event.get("note") or "") or "表外" in str(event.get("error") or ""):
        return "reject"
    return "success" if event.get("type") in ("learned_route", "integration") else "pause"


def distill_records(events: list[dict], runs: list[dict] | None = None) -> dict:
    """纯函数。相同 sha 只留一条。不写盘。"""
    intents = []
    actions = []
    failures = []
    clarifies = []
    skills = []
    seen: set[str] = set()

    def keep(bucket: list[dict], record: dict, key: str) -> None:
        digest = _sha(key)
        if digest in seen:
            return
        seen.add(digest)
        record = dict(record)
        record["sha1"] = digest
        record["outcome"] = record.get("outcome") or _outcome(record)
        bucket.append(record)

    for event in events or []:
        if not isinstance(event, dict):
            continue
        etype = str(event.get("type") or "")
        run_id = str(event.get("_run") or event.get("run_id") or "")
        if etype == "intent":
            blob = json.dumps(
                {"actions": event.get("actions"), "unresolved": event.get("unresolved")},
                ensure_ascii=False,
                sort_keys=True,
            )
            keep(intents, {
                "run_id": run_id,
                "actions": event.get("actions") or [],
                "unresolved": event.get("unresolved") or [],
                "outcome": "pause" if event.get("unresolved") else "success",
            }, "intent:" + blob)
        if etype in ("window_result", "tool_call"):
            blob = json.dumps(
                {"window": event.get("window"), "tool": event.get("tool"), "ok": event.get("ok")},
                ensure_ascii=False,
                sort_keys=True,
            )
            keep(actions, {
                "run_id": run_id,
                "window": event.get("window"),
                "action": event.get("tool") or event.get("action") or "",
                "ok": event.get("ok"),
                "outcome": _outcome(event),
            }, "action:" + blob)
        note = redact(str(event.get("note") or event.get("error") or ""))
        if "error C" in note:
            coded = re.search(r"error C\d+", note, re.I)
            keep(failures, {
                "run_id": run_id,
                "symptom": coded.group(0) if coded else "error C",
                "error_kind": "compile",
                "note": note[:240],
                "status": "shadow",
                "outcome": "fail",
            }, "fail:" + note)
        if etype == "confirm":
            supplied = str(event.get("path") or "").strip()
            if supplied.startswith("/Game") or supplied.startswith("Public/") or supplied.startswith("Private/"):
                slot = "source_file" if supplied[:7] in ("Public/", "Private") else "asset_refPath"
                keep(clarifies, {
                    "run_id": run_id,
                    "missing_slot": slot,
                    "accepted": redact(supplied),
                    "question_template": "只给一次 r",
                    "outcome": "correct",
                }, "clarify:" + slot + ":" + supplied)
        if etype == "learned_route":
            keep(skills, {
                "run_id": run_id,
                "route_id": event.get("route_id"),
                "status": "shadow",
                "outcome": "success",
            }, "skill:" + str(event.get("route_id") or "") + ":" + run_id)

    for run in runs or []:
        if not isinstance(run, dict):
            continue
        run_id = str(run.get("run_id") or "")
        windows = run.get("windows")
        if isinstance(windows, dict):
            sequence = [
                str((item or {}).get("action") or "")
                for item in windows.values()
                if isinstance(item, dict)
            ]
        elif isinstance(windows, list):
            sequence = [str((item or {}).get("action") or "") for item in windows if isinstance(item, dict)]
        else:
            sequence = []
        if not sequence:
            continue
        unresolved = run.get("unresolved") or []
        keep(skills, {
            "run_id": run_id,
            "action_sequence": [item for item in sequence if item],
            "status": "shadow" if unresolved else "candidate",
            "outcome": "pause" if unresolved else "success",
        }, "runskill:" + run_id + ":" + ">".join(sequence))
        hashes = run.get("hashes") or []
        matrix = run.get("matrix") or []
        if hashes or matrix:
            brief = []
            if isinstance(matrix, list):
                for item in matrix[:8]:
                    if not isinstance(item, dict):
                        continue
                    # ok=None 是「不适用」，既不算通过也不算失败，蒸馏时原样保留
                    raw = item.get("ok")
                    brief.append({
                        "item": str(item.get("item") or "")[:80],
                        "ok": None if raw is None else bool(raw),
                    })
            keep(skills, {
                "run_id": run_id,
                "hash_count": len(hashes) if isinstance(hashes, list) else 0,
                "matrix": brief,
                "status": "shadow",
                "outcome": (
                    "success"
                    if brief and all(item.get("ok") is True for item in brief if item.get("ok") is not None)
                    else "fail"
                ),
            }, "archive:" + run_id)

    return {
        "intent_examples": intents,
        "action_sequences": actions,
        "failure_patterns": failures,
        "clarification_examples": clarifies,
        "skill_candidates": skills,
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _teacher_enabled() -> bool:
    return os.environ.get("UE_DISTILL_TEACHER", "").strip() == "1"


def _teach(buckets: dict) -> list[dict]:
    """只在 UE_DISTILL_TEACHER=1 时调用现有模型。结果不写入 learned_skills。"""
    from core.llm_call import complete

    payload = {
        "failure_patterns": (buckets.get("failure_patterns") or [])[:8],
        "skill_candidates": (buckets.get("skill_candidates") or [])[:8],
    }
    messages = [
        {
            "role": "system",
            "content": (
                "只输出一个 JSON 对象，键是 skills。"
                "每条含 id、when、goals、action_sequence、status。"
                "status 只能是 shadow。不要写解释，不要写 learned_skills。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)[:4000]},
    ]
    response, _record = complete(messages, temperature=0, caller="distill")
    text = response.choices[0].message.content or ""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return []
    data = json.loads(text[start:end + 1])
    rows = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    taught = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["status"] = "shadow"
        item["executed"] = False
        taught.append(item)
    return taught


def run_distill(*, dry_run: bool = False, root: Path | None = None, out_dir: Path | None = None) -> dict:
    """dry_run 只统计。非 dry_run 才把候选写到 distill 目录，仍不执行、不提升。"""
    base = Path(root) if root is not None else _data_root()
    events, runs = _load_disk(base)
    buckets = distill_records(events, runs)
    counts = {name: len(rows) for name, rows in buckets.items()}
    wrote = False
    model = False
    taught: list[dict] = []
    if _teacher_enabled() and not dry_run:
        try:
            taught = _teach(buckets)
            model = True
        except Exception:
            taught = []
            model = False
    if not dry_run:
        target = Path(out_dir) if out_dir is not None else _out_dir()
        for name, rows in buckets.items():
            _write_jsonl(target / f"{name}.jsonl", rows)
        shadow = []
        for name, rows in buckets.items():
            for row in rows:
                item = dict(row)
                item["bucket"] = name
                shadow.append(item)
        for row in taught:
            item = dict(row)
            item["bucket"] = "teacher"
            item["executed"] = False
            shadow.append(item)
        _write_jsonl(target / "candidates.jsonl", shadow)
        wrote = True
    return {
        "dry_run": bool(dry_run),
        "wrote": wrote,
        "executed": False,
        "model": model,
        "events": len(events),
        "runs": len(runs),
        "counts": counts,
    }


def main() -> int:
    import sys

    dry = "--dry-run" in sys.argv
    report = run_distill(dry_run=dry)
    counts = report["counts"]
    print(
        "distill dry-run"
        if dry
        else "distill wrote candidates"
    )
    print(
        "events={events} runs={runs} intents={intent_examples} actions={action_sequences} "
        "failures={failure_patterns} clarifies={clarification_examples} skills={skill_candidates} "
        "executed={executed}".format(events=report["events"], runs=report["runs"], executed=report["executed"], **counts)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
