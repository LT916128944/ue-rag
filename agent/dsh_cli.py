# agent/dsh_cli.py
"""DSH 证据 CLI：只读合同/交接、按裁判写 passed、窗口快照。

只被 server/dsh_bridge.py 以子进程调用，也可人手跑；每个子命令输出单行 JSON。
写 passed 的唯一路径是 core.judge.judge -> agent.harness.apply_judge；本文件不接受模型文本。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import harness  # noqa: E402
from agent.resume_context import _load, build_line, latest_handoff_unmet  # noqa: E402
from agent.snapshot import restore_source, snapshot_source  # noqa: E402
from config import DATA_DIR  # noqa: E402
from core.judge import judge as run_judge  # noqa: E402

RUNS_ROOT = DATA_DIR / "runs"
LOCKS_DIR = DATA_DIR / "locks"
_STALE_LOCK = 120.0


def _runs_root() -> Path:
    override = os.environ.get("UE_DSH_RUNS_DIR", "").strip()
    return Path(override) if override else RUNS_ROOT


def _run_path(run_id: str) -> Path | None:
    root = _runs_root()
    for path in (root / f"{run_id}.json", *sorted(root.glob(f"*/{run_id}.json"))):
        if path.is_file():
            return path
    return None


def _load_run(run_id: str) -> tuple[dict, Path] | tuple[None, None]:
    path = _run_path(run_id)
    if path is None:
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, path
    return (data, path) if isinstance(data, dict) else (None, path)


class _RunLock:
    def __init__(self, run_id: str):
        LOCKS_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOCKS_DIR / f"run-{run_id}.lock"

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0
                if age > _STALE_LOCK:
                    self.path.unlink(missing_ok=True)
                    continue
                raise RuntimeError("run busy")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "ts": time.time()}, handle)
            return self
        raise RuntimeError("run busy")

    def __exit__(self, *_exc):
        self.path.unlink(missing_ok=True)
        return False


def _atomic_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _emit(payload: dict) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _criteria(run: dict) -> list[dict]:
    return [dict(item) for item in (run.get("criteria") or []) if isinstance(item, dict)]


def _find_row(criteria: list[dict], cid: str) -> dict | None:
    return next((item for item in criteria if str(item.get("id")) == cid), None)


def _clip_text(value, limit: int = 240) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit]


def _judge_blob(verdict: dict) -> str:
    raw = {
        "ok": verdict.get("ok"),
        "kind": verdict.get("kind"),
        "summary": verdict.get("summary"),
    }
    if verdict.get("errors"):
        raw["errors"] = verdict.get("errors")
    if verdict.get("window"):
        raw["window"] = verdict.get("window")
    return _clip_text(raw)


def _refuse(result: dict, task: dict | None = None, verdict: dict | None = None) -> dict:
    """拒绝时附上当时的 policy。裁判自带 policy 就用它，否则记当前开关。"""
    if result.get("ok") is True:
        return result
    policy = verdict.get("policy") if isinstance(verdict, dict) else None
    if not isinstance(policy, dict):
        policy = harness.policy_snapshot(task if isinstance(task, dict) else None)
    result["policy"] = policy
    return result


def _verify_specs(entry: dict, run: dict | None = None) -> tuple[list, dict, str]:
    """从 run 记录里取回这一窗真正用过的 verify spec、任务策略，以及 spec 的出处。

    取 spec 的来源按可靠性排，**取到就不再往后走**——记录里有空 verify 就是「这扇
    本来没留 spec」，不许拿规则再补一条出来：
    1) `entry["task"]`：写侧（`run_windows._task_snapshot`）落的运行期任务快照，
       spec 原件 + allow_writes/save/pie。新记录走这条。
    2) `entry["window"]["task"]`：任务书/人工补的窗口，规划期就带 task。
    3) `entry["verify"]`：旧夹具的平铺写法。
    4) 三处都没有（`_task_snapshot` 之前写出来的旧记录）：用运行期本来就会走的确定性
       规则 `_prepare_task` 把规划窗口复原成任务再取 spec。复原不出来返回空，不猜——
       place_actor 这类直接动作运行期根本不跑裁判，复原出来也仍然是空。

    `detail.build` / `detail.test` 一律不当 spec：那是裁判结论（`kind: skip` 那种）。"""
    window = entry.get("window") if isinstance(entry.get("window"), dict) else {}
    stored = entry.get("task") if isinstance(entry.get("task"), dict) else {}
    if stored:
        return harness.effective_verify_specs(stored), dict(stored), "task"
    task = window.get("task") if isinstance(window.get("task"), dict) else {}
    if task:
        return harness.effective_verify_specs(task), dict(task), "window.task"
    flat = entry.get("verify")
    if isinstance(flat, list):
        return _as_specs(flat), {}, "entry.verify"
    rebuilt = _rebuilt_task(window, run)
    if rebuilt is None:
        return [], {}, "none"
    specs = harness.effective_verify_specs(rebuilt)
    return specs, dict(rebuilt), ("rebuilt" if specs else "none")


def _as_specs(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [dict(spec) for spec in value if isinstance(spec, dict)]


def _rebuilt_task(window: dict, run: dict | None) -> dict | None:
    """旧 run 记录的最后一处回落：按当时的确定性规则复原任务。纯本地、不连编辑器、不叫模型。"""
    if not isinstance(window, dict) or not window:
        return None
    try:
        from agent.run_windows import legacy_task_snapshot
    except Exception:
        return None
    run = run if isinstance(run, dict) else {}
    return legacy_task_snapshot(
        window,
        str(run.get("query") or ""),
        str(run.get("run_id") or ""),
    )


def _verify_one(run: dict, criteria: list[dict], cid: str, dry_run: bool) -> dict:
    row = _find_row(criteria, cid)
    if row is None:
        return _refuse({
            "ok": False,
            "criterion_id": cid,
            "summary": f"条款不存在: {cid}",
            "error": f"条款不存在: {cid}",
            "passed": False,
        })
    kind = str(row.get("evidence_kind") or "")
    if kind == "manual":
        message = "manual 条款不由模型或裁判改成通过，交给人"
        return _refuse({
            "ok": False,
            "criterion_id": cid,
            "kind": "manual",
            "summary": message,
            "error": message,
            "passed": False,
        })
    owners = [str(item) for item in (row.get("windows") or []) if str(item).strip()]
    if not owners:
        message = "条款没有绑定窗口，不猜裁判"
        return _refuse({
            "ok": False,
            "criterion_id": cid,
            "kind": kind,
            "summary": message,
            "error": message,
            "passed": False,
        })
    windows = run.get("windows") if isinstance(run.get("windows"), dict) else {}
    candidates: list[tuple[str, dict, dict]] = []
    checked: list[dict] = []
    for wid in owners:
        entry = windows.get(wid) if isinstance(windows.get(wid), dict) else {}
        specs, task, source = _verify_specs(entry, run)
        checked.append({
            "window": wid,
            "specs": len(specs),
            "kinds": [str(spec.get("kind") or "") for spec in specs],
            "source": source,
        })
        for spec in specs:
            if str(spec.get("kind") or "") == kind:
                candidates.append((wid, task, spec))
    if not candidates:
        message = f"窗口里没有 {kind} 的 verify spec"
        return _refuse({
            "ok": False,
            "criterion_id": cid,
            "kind": kind,
            "summary": message,
            "error": message,
            "passed": False,
            "windows": owners,
            "checked": checked,
        })
    if dry_run:
        return {
            "ok": True,
            "criterion_id": cid,
            "kind": kind,
            "window": owners[0] if len(owners) == 1 else owners,
            "dry_run": True,
            "candidates": len(candidates),
            "summary": f"{len(candidates)} 个 {kind} spec，未写合同",
            "passed": False,
        }
    for wid, task, spec in candidates:
        verdict = run_judge(spec, task)
        if not isinstance(verdict, dict):
            continue
        judge_kind = str(verdict.get("kind") or "")
        if not judge_kind or judge_kind != kind or judge_kind not in harness.EVIDENCE_KINDS:
            continue
        stamped = dict(verdict)
        stamped["window"] = wid
        criteria[:] = harness.apply_judge(criteria, stamped)
        current = _find_row(criteria, cid) or {}
        passed = bool(current.get("passed"))
        summary = _clip_text(stamped.get("summary") or "")
        result = {
            "ok": passed,
            "criterion_id": cid,
            "window": wid,
            "kind": kind,
            "summary": summary,
            "passed": passed,
            "judge": _judge_blob(stamped),
        }
        if not passed:
            result["error"] = summary or "裁判未通过"
            _refuse(result, task, stamped)
        return result
    message = "没有 kind 匹配的裁判结果"
    return _refuse({
        "ok": False,
        "criterion_id": cid,
        "kind": kind,
        "summary": message,
        "error": message,
        "passed": False,
        "windows": owners,
    })


def cmd_resume_line(_args) -> int:
    progress = _load(DATA_DIR / "harness" / "progress.json")
    features = _load(DATA_DIR / "harness" / "feature_list.json")
    unmet = latest_handoff_unmet(DATA_DIR / "handoffs")
    return _emit({"ok": True, "line": build_line(progress, features, unmet)})


def cmd_contract_status(args) -> int:
    run, path = _load_run(args.run)
    if run is None:
        return _emit({"ok": False, "error": f"run 不存在: {args.run}"})
    criteria = _criteria(run)
    integration = run.get("integration") if isinstance(run.get("integration"), dict) else {}
    raw_status = str(integration.get("status") or run.get("status") or "失败")
    scopes = run.get("scopes") if isinstance(run.get("scopes"), list) else []
    status = harness.contract_status(criteria, raw_status, scopes)
    return _emit({
        "ok": True,
        "run_id": args.run,
        "status": status,
        "raw_status": raw_status,
        "criteria": criteria,
        "path": str(path) if path else "",
    })


def cmd_verify(args) -> int:
    run, path = _load_run(args.run)
    if run is None or path is None:
        return _emit({"ok": False, "error": f"run 不存在: {args.run}"})
    criteria = _criteria(run)
    if args.all:
        targets = [
            str(item.get("id"))
            for item in criteria
            if not item.get("passed") and str(item.get("evidence_kind")) != "manual"
        ]
    else:
        targets = [args.criterion]
    if not targets:
        return _emit({"ok": True, "run_id": args.run, "verified": [], "passed": False, "summary": "没有可验证的自动条款"})
    results = []
    changed = False
    for cid in targets:
        try:
            result = _verify_one(run, criteria, cid, args.dry_run)
        except Exception as exc:
            result = _refuse({
                "ok": False,
                "criterion_id": cid,
                "summary": _clip_text(exc),
                "error": _clip_text(exc),
                "passed": False,
            })
        results.append(result)
        changed = changed or bool(result.get("passed"))
    if changed and not args.dry_run:
        with _RunLock(args.run):
            run["criteria"] = criteria
            run["updated_at"] = _now()
            _atomic_json(path, run)
    payload = {
        "ok": all(bool(item.get("ok")) for item in results) if results else True,
        "run_id": args.run,
        "dry_run": bool(args.dry_run),
        "verified": results,
        "passed": all(bool(item.get("passed")) for item in results) if results else False,
        "changed": changed and not args.dry_run,
    }
    if len(results) == 1:
        one = results[0]
        for key in ("criterion_id", "window", "kind", "summary", "judge", "policy", "error",
                    "checked", "candidates"):
            if key in one:
                payload[key] = one[key]
    return _emit(payload)


def cmd_handoff(args) -> int:
    root = harness.handoff_root()
    run_dir = root / args.run
    if args.window:
        payload = harness.read_handoff(args.run, args.window, root)
        if payload is None:
            return _emit({"ok": False, "error": f"没有交接: {args.run}/{args.window}"})
        return _emit({"ok": True, "run_id": args.run, "window": args.window, "handoff": payload})
    if not run_dir.is_dir():
        return _emit({"ok": False, "error": f"没有交接目录: {args.run}"})
    files = sorted(run_dir.glob("*.json"))[:8]
    items = []
    for file in files:
        doc = _load(file)
        items.append({"window": file.stem, "handoff": doc})
    return _emit({"ok": True, "run_id": args.run, "count": len(items), "items": items})


_HANDOFF_REQUIRED = ("did", "evidence", "unmet", "unmet_total", "unmet_omitted", "next", "artifacts")
_HANDOFF_OPTIONAL = ("unmet_ids", "goal")
_HANDOFF_CLIP = 240
# build_handoff 只按 `judge.get("kind")` 非空就收，所以生产方能写出的 kind 不止 EVIDENCE_KINDS：
# nodes.py 在没有 verify/build spec 时给 kind="skip"（"no verify" / "no build verify"），
# run_windows 的处置扇也写 skip。校验收下这个占位，否则会把真交接判成非法。
# 它不构成证据：passed 仍只由裁判改写，交接里的 evidence 只是文字。
_HANDOFF_EVIDENCE_KINDS = tuple(harness.EVIDENCE_KINDS) + ("skip",)


def _handoff_errors(doc: dict) -> list[str]:
    """按 harness.build_handoff 的字段与截断口径校验一份交接，不猜、不补字段。"""
    errors: list[str] = []
    for key in _HANDOFF_REQUIRED:
        if key not in doc:
            errors.append(f"缺字段 {key}")
    for key in doc:
        if key not in _HANDOFF_REQUIRED and key not in _HANDOFF_OPTIONAL:
            errors.append(f"未知字段 {key}（build_handoff 没有这个字段）")

    if "did" in doc:
        did = doc.get("did")
        if not isinstance(did, str):
            errors.append("did 不是字符串")
        elif len(did) > 200:
            errors.append(f"did 超过 200 字: {len(did)}")

    evidence = doc.get("evidence")
    if "evidence" in doc and not isinstance(evidence, list):
        errors.append("evidence 不是列表")
    elif isinstance(evidence, list):
        for item in evidence:
            if not isinstance(item, dict):
                errors.append("evidence 条目不是对象")
                continue
            for key in item:
                if key not in ("kind", "ok", "summary"):
                    errors.append(f"evidence 多出字段 {key}（build_handoff 只写 kind/ok/summary）")
            kind = str(item.get("kind") or "")
            if kind not in _HANDOFF_EVIDENCE_KINDS:
                errors.append(f"evidence kind 非法: {kind}")
            if "ok" not in item:
                errors.append("evidence 缺 ok")
            elif not isinstance(item.get("ok"), bool):
                errors.append("evidence ok 不是布尔")
            summary = item.get("summary", "")
            if not isinstance(summary, str):
                errors.append("evidence summary 不是字符串")
            elif len(summary) > _HANDOFF_CLIP:
                errors.append(f"evidence summary 超过 {_HANDOFF_CLIP} 字: {len(summary)}")

    unmet = doc.get("unmet")
    if "unmet" in doc and not isinstance(unmet, list):
        errors.append("unmet 不是列表")
    elif isinstance(unmet, list):
        if len(unmet) > 8:
            errors.append(f"unmet 超过 8 条: {len(unmet)}")
        for index, item in enumerate(unmet):
            if not isinstance(item, str):
                errors.append(f"unmet[{index}] 不是字符串")
            elif len(item) > _HANDOFF_CLIP:
                errors.append(f"unmet[{index}] 超过 {_HANDOFF_CLIP} 字: {len(item)}")
        if "unmet_total" in doc and "unmet_omitted" in doc:
            try:
                total = int(doc.get("unmet_total"))
                omitted = int(doc.get("unmet_omitted"))
            except (TypeError, ValueError):
                errors.append("unmet_total/unmet_omitted 不是整数")
            else:
                if omitted < 0:
                    errors.append("unmet_omitted 不能为负")
                if total - len(unmet) != omitted:
                    errors.append("unmet_total - len(unmet) != unmet_omitted")

    for key in ("next", "goal"):
        if key in doc:
            value = doc.get(key)
            if not isinstance(value, str):
                errors.append(f"{key} 不是字符串")
            elif len(value) > _HANDOFF_CLIP:
                errors.append(f"{key} 超过 {_HANDOFF_CLIP} 字: {len(value)}")

    if "unmet_ids" in doc:
        ids = doc.get("unmet_ids")
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            errors.append("unmet_ids 必须是字符串列表")

    artifacts = doc.get("artifacts")
    if "artifacts" in doc and not isinstance(artifacts, dict):
        errors.append("artifacts 不是对象")
    elif isinstance(artifacts, dict):
        for name, value in artifacts.items():
            if not isinstance(value, dict):
                errors.append(f"artifacts[{name}] 不是对象")
                continue
            for key in value:
                if key not in ("ref", "in_level"):
                    errors.append(f"artifacts[{name}] 多出字段 {key}（build_handoff 只留 ref/in_level）")

    if "read_source" in json.dumps(doc, ensure_ascii=False):
        errors.append("交接里出现 read_source 正文")
    return errors


def _handoff_doc(path: Path) -> tuple[dict, list[str]]:
    doc = _load(path)
    if not doc:
        return {}, [f"读不到或为空: {path}"]
    if not isinstance(doc, dict):
        return {}, [f"交接不是对象: {path}"]
    return doc, _handoff_errors(doc)


def cmd_handoff_validate(args) -> int:
    path = Path(args.file)
    _doc, errors = _handoff_doc(path)
    if errors:
        return _emit({"ok": False, "errors": errors, "path": str(path)})
    return _emit({"ok": True, "path": str(path)})


def cmd_handoff_merge(args) -> int:
    """校验通过后把 DSH 子代理的单窗交接并进 data/handoffs/<run>/<window>.json；不覆盖。"""
    source = Path(args.file)
    run_id = str(args.run or "").strip()
    if not run_id or "/" in run_id or "\\" in run_id:
        return _emit({"ok": False, "error": f"run id 非法: {run_id!r}"})
    if not source.is_file():
        return _emit({"ok": False, "error": f"源文件不存在: {source}"})
    if source.suffix.lower() != ".json":
        return _emit({"ok": False, "error": "只收 .json 单窗交接"})
    doc, errors = _handoff_doc(source)
    if errors:
        return _emit({"ok": False, "error": "校验没过，不合并", "errors": errors, "path": str(source)})
    if "windows" in doc or "criteria" in doc:
        return _emit({"ok": False, "error": "这是 run 记录，不是单窗交接；不合并", "path": str(source)})
    window_id = str(args.window or source.stem).strip()
    if not window_id or "/" in window_id or "\\" in window_id:
        return _emit({"ok": False, "error": f"window id 非法: {window_id!r}"})
    target = harness.handoff_root() / run_id / f"{window_id}.json"
    if target.exists():
        return _emit({"ok": False, "error": "目标已存在，不覆盖", "target": str(target)})
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(target, doc)
    return _emit({
        "ok": True,
        "merged": str(target),
        "run_id": run_id,
        "window": window_id,
        "bytes": target.stat().st_size,
    })


def cmd_window_state(args) -> int:
    run, path = _load_run(args.run)
    if run is None or path is None:
        return _emit({"ok": False, "error": f"run 不存在: {args.run}"})
    if args.action == "snapshot":
        snap = snapshot_source(args.run, args.window)
        return _emit({"ok": True, "action": "snapshot", "run_id": args.run, "window": args.window,
                      "dir": snap.get("dir", ""), "files": len(snap.get("files") or {})})
    snap = None
    windows = run.get("windows") if isinstance(run.get("windows"), dict) else {}
    entry = windows.get(args.window) if isinstance(windows.get(args.window), dict) else {}
    if isinstance(entry.get("snapshot"), dict) and entry["snapshot"].get("files"):
        snap = entry["snapshot"]
    if snap is None:
        manifest = Path(str(entry.get("snapshot", {}).get("dir") or "")) / "manifest.json"
        if manifest.is_file():
            data = _load(manifest)
            if isinstance(data, dict):
                snap = {"dir": str(manifest.parent), "files": data.get("files") or {}}
    if not snap:
        return _emit({"ok": False, "error": "没有可用的源码快照"})
    restored = restore_source(args.run, args.window, snap)
    return _emit({"ok": True, "action": "restore", "run_id": args.run, "window": args.window, "restored": restored})


def main() -> int:
    parser = argparse.ArgumentParser(description="DSH evidence CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("resume-line")
    p_contract = sub.add_parser("contract-status")
    p_contract.add_argument("--run", required=True)

    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--run", required=True)
    group = p_verify.add_mutually_exclusive_group(required=True)
    group.add_argument("--criterion")
    group.add_argument("--all", action="store_true")
    p_verify.add_argument("--dry-run", action="store_true")

    p_handoff = sub.add_parser("handoff")
    p_handoff.add_argument("--run", required=True)
    p_handoff.add_argument("--window", default="")

    p_validate = sub.add_parser("handoff-validate")
    p_validate.add_argument("--file", required=True)

    p_merge = sub.add_parser("handoff-merge")
    p_merge.add_argument("--file", required=True)
    p_merge.add_argument("--run", required=True)
    p_merge.add_argument("--window", default="")

    p_state = sub.add_parser("window-state")
    p_state.add_argument("action", choices=["snapshot", "restore"])
    p_state.add_argument("--run", required=True)
    p_state.add_argument("--window", required=True)

    args = parser.parse_args()
    handlers = {
        "resume-line": cmd_resume_line,
        "contract-status": cmd_contract_status,
        "verify": cmd_verify,
        "handoff": cmd_handoff,
        "handoff-validate": cmd_handoff_validate,
        "handoff-merge": cmd_handoff_merge,
        "window-state": cmd_window_state,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
